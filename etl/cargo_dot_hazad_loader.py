#!/usr/bin/env python3
"""Import the 49 CFR 172.101 Hazardous Materials Table into cargo_dot_hazad.

Design goals (production regulatory import):
  * Preserve EVERY source row verbatim — normal materials, generic (n.o.s.)
    names, forbidden entries, and "see ..." cross-references.
  * NEVER create cargo_chemical rows. cargo_id is left NULL unless the row's
    proper_shipping_name exactly matches an existing cargo_chemical.canonical_name.
  * Idempotent: re-running the same file updates in place, never duplicates,
    and never clobbers a cargo_id that was set by a later linking pass.

Idempotency key
---------------
The HMT has NO natural unique key: 435 proper_shipping_names repeat (up to 19x).
So each row's identity is a content hash (`row_hash`) over all CFR columns.
UNIQUE (source_id, row_hash) is the upsert target.

Usage:
    python3 etl/cargo_dot_hazad_loader.py                 # default file + .env DB
    python3 etl/cargo_dot_hazad_loader.py <input.csv>
    python3 etl/cargo_dot_hazad_loader.py --dry-run       # parse + report only
    python3 etl/cargo_dot_hazad_loader.py --relink        # only re-match NULL cargo_id rows
"""
from __future__ import annotations

import csv
import hashlib
import os
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

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

# CFR fields in canonical order — used for the row hash AND the DB tuple.
# (extractor key, db column)
CFR_FIELDS = [
    ("symbol",                   "symbol"),
    ("proper_shipping_name",     "proper_shipping_name"),
    ("hazard_class",             "hazard_class"),
    ("un_number",                "identification_number"),
    ("packing_group",            "packing_group"),
    ("label_codes",              "label_codes"),
    ("special_provisions",       "special_provisions"),
    ("exceptions_packaging",     "packaging_exception"),
    ("non_bulk_packaging",       "packaging_non_bulk"),
    ("bulk_packaging",           "packaging_bulk"),
    ("passenger_aircraft_limit", "passenger_quantity_limit"),
    ("cargo_aircraft_limit",     "cargo_quantity_limit"),
    ("vessel_stowage_location",  "vessel_location"),
    ("vessel_stowage_other",     "vessel_other"),
]

_SEE_RE = re.compile(r",\s*see\b", re.IGNORECASE)


def flatten(value) -> Optional[str]:
    """List -> comma-joined string; str -> str; None -> None (for storage/hash)."""
    if value is None:
        return None
    if isinstance(value, list):
        return ", ".join(value) if value else None
    return str(value)


def row_hash(rec: dict) -> str:
    """Stable content hash over all CFR columns (order-independent identity)."""
    payload = "\x1f".join(
        "" if flatten(rec.get(src)) is None else flatten(rec.get(src))
        for src, _ in CFR_FIELDS
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def classify(rec: dict) -> tuple[str, Optional[str]]:
    """Return (entry_type, see_reference).

    Priority: cross_reference > forbidden > generic > material.
    """
    name = rec.get("proper_shipping_name") or ""
    if _SEE_RE.search(name):
        # keep the text after the first ", see "
        after = _SEE_RE.split(name, maxsplit=1)
        return "cross_reference", (after[1].strip() if len(after) > 1 else None)
    # A genuinely *Forbidden* entry is one whose CFR Column (3) hazard class is
    # literally "Forbidden" (no UN/PG). Being merely "Forbidden" on passenger or
    # cargo aircraft is only a transport restriction on an otherwise-normal
    # material, so it must NOT be classed forbidden here.
    if (rec.get("hazard_class") or "").strip().lower() == "forbidden":
        return "forbidden", None
    if "n.o.s" in name.lower():
        return "generic", None
    return "material", None


def norm(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip().lower()


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
    name = derive_source_name(path)
    sid = get_source_id(cur, name) or get_source_id_partial(cur, name)
    if sid is None:
        sid = create_source(cur, name)
        print(f"source: created new  id={sid}  name={name!r}")
    else:
        print(f"source: reusing      id={sid}  name={name!r}")
    return sid


def name_to_cargo_id(cur) -> Dict[str, int]:
    """Exact canonical_name (case-insensitive) -> a single cargo_id.

    canonical_name can repeat across sources, so resolve to the LOWEST id
    deterministically. Only exact matches populate cargo_id (per spec).
    """
    cur.execute("SELECT id, canonical_name FROM cargo_chemical WHERE canonical_name IS NOT NULL")
    out: Dict[str, int] = {}
    for cid, nm in cur.fetchall():
        k = norm(nm)
        if k not in out or cid < out[k]:
            out[k] = cid
    return out


def relink(cur) -> int:
    """Fill cargo_id for rows still NULL whose name now matches a chemical.
    Never touches rows that already have a cargo_id."""
    name_map = name_to_cargo_id(cur)
    cur.execute(
        "SELECT id, proper_shipping_name FROM cargo_dot_hazad "
        "WHERE cargo_id IS NULL AND entry_type <> 'cross_reference'"
    )
    updates = []
    for rid, nm in cur.fetchall():
        cid = name_map.get(norm(nm or ""))
        if cid is not None:
            updates.append((cid, rid))
    for cid, rid in updates:
        cur.execute("UPDATE cargo_dot_hazad SET cargo_id=%s, updated_at=now() WHERE id=%s",
                    (cid, rid))
    return len(updates)


def main() -> None:
    argv = sys.argv[1:]
    dry = "--dry-run" in argv
    relink_only = "--relink" in argv
    files = [a for a in argv if not a.startswith("--")]
    src = Path(files[0]) if files else Path(input_file(DEFAULT_INPUT))

    conn = connect()
    cur = conn.cursor()

    if relink_only:
        n = relink(cur)
        conn.commit()
        print(f"relink: filled cargo_id on {n} rows")
        cur.close(); conn.close()
        return

    with open(src, newline="", encoding="utf-8-sig") as fh:
        records = extract(list(csv.reader(fh)))
    print(f"parsed {len(records)} CFR rows from {src.name}")

    source_id = resolve_source(cur, src)
    name_map = name_to_cargo_id(cur)

    rows: List[tuple] = []
    counts = {"material": 0, "generic": 0, "forbidden": 0, "cross_reference": 0}
    n_linked = 0
    for rec in records:
        entry_type, see_ref = classify(rec)
        counts[entry_type] += 1
        # only non-cross-reference rows are eligible for an exact name link
        cargo_id = None
        if entry_type != "cross_reference":
            cargo_id = name_map.get(norm(rec.get("proper_shipping_name") or ""))
            if cargo_id is not None:
                n_linked += 1
        rows.append((
            source_id, row_hash(rec), entry_type, see_ref,
            *[flatten(rec.get(src_key)) for src_key, _ in CFR_FIELDS],
            cargo_id,
        ))

    print("entry types :", counts)
    print("exact-linked :", n_linked, "rows get a cargo_id (rest NULL)")

    if dry:
        print("\n-- dry run, no writes. sample --")
        for r in rows[:6]:
            print(f"  [{r[2]:<15}] {r[5][:44]:44} cargo_id={r[-1]}")
        cur.close(); conn.close()
        return

    # DB column order MUST match the tuple built above.
    db_cols = ["source_id", "row_hash", "entry_type", "see_reference"] \
        + [db for _, db in CFR_FIELDS] + ["cargo_id"]
    non_key_cols = [c for c in db_cols if c not in ("source_id", "row_hash", "cargo_id")]
    set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in non_key_cols)

    execute_values(
        cur,
        f"INSERT INTO cargo_dot_hazad ({', '.join(db_cols)}) VALUES %s "
        f"ON CONFLICT (source_id, row_hash) DO UPDATE SET {set_clause}, "
        # KEY: keep a cargo_id set by a later linking pass; only fill if still NULL.
        f"cargo_id = COALESCE(cargo_dot_hazad.cargo_id, EXCLUDED.cargo_id), "
        f"updated_at = now()",
        rows,
    )
    conn.commit()

    cur.execute("SELECT count(*), count(cargo_id) FROM cargo_dot_hazad WHERE source_id=%s",
                (source_id,))
    total, linked = cur.fetchone()
    print(f"\ncargo_dot_hazad (source {source_id}): {total} rows, {linked} linked to a cargo")
    cur.close(); conn.close()


if __name__ == "__main__":
    main()
