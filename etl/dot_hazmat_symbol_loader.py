#!/usr/bin/env python3
"""Load the DOT Hazardous Materials Table symbols into cargo_hazard_data as its
OWN source.

Single-purpose loader for ONE source file:
    "Cargo Library 2 DOT_hazardous_materials.xlsx - HM Table.csv"  (49 CFR 172.101)

Source handling (like the other etl loaders):
  * A source name is derived from the file name (derive_source_name).
  * If a source with that name already exists -> reuse its id.
  * Otherwise a new `source` row is created and its id is used.

It then INSERTS new cargo_hazard_data rows under that DOT source_id — it does NOT
update the existing (e.g. USCG) hazard rows. Each inserted row carries the matched
cargo_id, the DOT source_id, and the DOT Column (1) symbol in dot_symbol. Re-running
is idempotent: ON CONFLICT (cargo_id, source_id) refreshes that DOT row's symbol.

Matching a DOT entry (that has a symbol) to cargo_chemical rows:
  1. UN number — DOT un_number ("UN2789") stripped to digits vs cargo_un_number.
  2. Name — proper_shipping_name vs canonical_name (punctuation-normalised).
Nothing is invented: only DOT rows with a non-empty symbol produce a hazard row.

Usage:
    python3 etl/dot_hazmat_symbol_loader.py                 # default file + .env DB
    python3 etl/dot_hazmat_symbol_loader.py <input.csv>
    python3 etl/dot_hazmat_symbol_loader.py --dry-run       # match + report, no writes
"""
from __future__ import annotations

import csv
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, Optional, Set

import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

from _paths import input_file
from dot_hmt_extract import DEFAULT_INPUT, extract
from cargo_chemicals import (
    derive_source_name,
    get_source_id,
    get_source_id_partial,
    create_source,
)

ENV_PATH = "/home/lap044/projects/ship_project/.env"


def norm_name(name: str) -> str:
    """Lowercase, punctuation -> space, collapse whitespace (match importer style)."""
    return re.sub(r"\s+", " ", re.sub(r"[^0-9a-z]+", " ", name.lower())).strip()


def digits(un: Optional[str]) -> Optional[str]:
    """"UN2789"/"NA1993" -> "2789"/"1993"; None/blank -> None."""
    if not un:
        return None
    m = re.search(r"(\d{3,4})", un)
    return m.group(1) if m else None


def connect():
    load_dotenv(ENV_PATH)
    url = os.environ["DATABASE_URL"]
    last = None
    for attempt in range(6):
        try:
            return psycopg2.connect(url, connect_timeout=8)
        except Exception as e:  # remote link is intermittently flaky
            last = e
            print(f"  connect attempt {attempt + 1} failed: {e}", file=sys.stderr)
            time.sleep(3)
    raise SystemExit(f"could not connect: {last}")


def resolve_source(cur, path: Path) -> int:
    """Reuse the source whose name matches this file, else create a new one."""
    name = derive_source_name(path)
    sid = get_source_id(cur, name) or get_source_id_partial(cur, name)
    if sid is None:
        sid = create_source(cur, name)
        print(f"source: created new  id={sid}  name={name!r}")
    else:
        print(f"source: reusing      id={sid}  name={name!r}")
    return sid


def resolve(symbols: Set[str]) -> str:
    """One symbol -> verbatim; several distinct -> joined, sorted, ' | '-separated."""
    return next(iter(symbols)) if len(symbols) == 1 else " | ".join(sorted(symbols))


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    dry = "--dry-run" in sys.argv
    src = Path(args[0]) if args else Path(input_file(DEFAULT_INPUT))

    with open(src, newline="", encoding="utf-8-sig") as fh:
        records = extract(list(csv.reader(fh)))
    with_symbol = [r for r in records if r.get("symbol")]
    print(f"DOT records: {len(records)} total, {len(with_symbol)} with a symbol")

    conn = connect()
    cur = conn.cursor()

    source_id = resolve_source(cur, src)

    # Reverse indexes over ALL cargoes (not only those with a hazard row):
    #   UN digits -> {cargo_id}, normalized name -> {cargo_id}
    cur.execute("SELECT cargo_id, un_number FROM cargo_un_number")
    un_to_cargos: Dict[str, Set[int]] = defaultdict(set)
    for cid, un in cur.fetchall():
        d = digits(un)
        if d:
            un_to_cargos[d].add(cid)

    cur.execute("SELECT id, canonical_name FROM cargo_chemical")
    name_to_cargos: Dict[str, Set[int]] = defaultdict(set)
    for cid, nm in cur.fetchall():
        if nm:
            name_to_cargos[norm_name(nm)].add(cid)

    # For each DOT row with a symbol, find cargoes and gather symbols per cargo.
    cargo_syms: Dict[int, Set[str]] = defaultdict(set)
    n_unmatched = 0
    matched_via_un = matched_via_name = 0
    for r in with_symbol:
        un = digits(r.get("un_number"))
        cids = set(un_to_cargos.get(un, ())) if un else set()
        via_un = bool(cids)
        if not cids:
            cids = set(name_to_cargos.get(norm_name(r.get("proper_shipping_name") or ""), ()))
        if not cids:
            n_unmatched += 1
            continue
        matched_via_un += via_un
        matched_via_name += (not via_un)
        for cid in cids:
            cargo_syms[cid].add(r["symbol"])

    rows = [
        (cid, source_id, resolve(syms))
        for cid, syms in cargo_syms.items()
    ]
    n_ambig = sum(1 for _, s in cargo_syms.items() if len(s) > 1)

    print(f"DOT rows matched via UN  : {matched_via_un}")
    print(f"DOT rows matched via name: {matched_via_name}")
    print(f"DOT rows unmatched       : {n_unmatched}")
    print(f"cargoes to insert        : {len(rows)}  (ambiguous multi-symbol: {n_ambig})")

    if dry:
        print("\n-- dry run, no writes. sample rows --")
        cur.execute(
            "SELECT id, canonical_name FROM cargo_chemical WHERE id = ANY(%s)",
            ([cid for cid, _, _ in rows[:15]],),
        )
        names = dict(cur.fetchall())
        for cid, sid, sym in rows[:15]:
            print(f"  cargo {cid:>5} {names.get(cid, '')[:34]:34} src={sid} -> {sym!r}")
        cur.close(); conn.close()
        return

    execute_values(
        cur,
        "INSERT INTO cargo_hazard_data (cargo_id, source_id, dot_symbol, created_at, updated_at) "
        "VALUES %s "
        "ON CONFLICT (cargo_id, source_id) DO UPDATE SET "
        "dot_symbol = EXCLUDED.dot_symbol, updated_at = now()",
        [(cid, sid, sym, ) for cid, sid, sym in rows],
        template="(%s, %s, %s, now(), now())",
    )
    conn.commit()

    cur.execute(
        "SELECT count(*) FROM cargo_hazard_data WHERE source_id = %s", (source_id,)
    )
    print(f"\ncargo_hazard_data rows under DOT source {source_id}: {cur.fetchone()[0]}")
    cur.close(); conn.close()


if __name__ == "__main__":
    main()
