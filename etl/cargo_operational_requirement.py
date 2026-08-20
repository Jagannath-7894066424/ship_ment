#!/usr/bin/env python3
"""
Populate `operational_requirement` and its join table `cargo_operational_requirement`
from the IBC Code data, which is split across TWO vendored files:

1. Requirement DEFINITIONS  <- Operational_References_Master.csv
       Reference           -> code            (e.g. "15.11.2")   [unique]
       (code minus last .N) -> section         ("15.11.2" -> "15.11")
       Title / Description  -> title
       Full Text            -> explanation
       ""                   -> description     (column is NOT NULL, not in file)

2. Cargo LINKS              <- IBC Code.xlsx
       product_name                        -> cargo_chemical (under the IBC source)
       specific_operational_requirements   -> codes, split on ";"  (one link each)

Any code referenced by a cargo but missing a definition row gets a minimal stub
(title = the code) so the link is never dropped.

Each real run TRUNCATEs both tables (CASCADE) and reloads them from scratch.
PREREQUISITE: cargo_chemical must already be populated (master_loader IBC Code).

Usage:
    python3 cargo_operational_requirement.py                       # both default files
    python3 cargo_operational_requirement.py --requirements X.csv --links Y.xlsx
    python3 cargo_operational_requirement.py --dry-run
    python3 cargo_operational_requirement.py --source-id 1         # force source
"""

import argparse
import logging
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple

from _paths import input_file

import pandas as pd
import psycopg2
from psycopg2 import sql
from psycopg2.extras import execute_values
from dotenv import load_dotenv

# ----------------------------------------------------------------------------
DEFAULT_REQUIREMENTS = input_file("Operational_References_Master.csv")
DEFAULT_LINKS = input_file("IBC Code.xlsx")
REQ_TABLE = "operational_requirement"
LINK_TABLE = "cargo_operational_requirement"
SOURCE_NAME = "IBC Code"

CODE_RE = re.compile(r"^\d+(?:\.\d+)*$")     # "15", "15.11", "15.11.2"
# ----------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("cargo_operational_requirement")


def section_of(code: str) -> str:
    """Section = code with its last dotted segment removed (15.11.2 -> 15.11)."""
    code = code.strip()
    return code.rsplit(".", 1)[0] if "." in code else code


def _read_raw(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path, header=None, dtype=str, keep_default_na=False)
    return pd.read_excel(path, header=None, dtype=str, keep_default_na=False)


def parse_requirements(path: Path) -> Dict[str, Tuple[str, str, str, str]]:
    """Master references file -> {code: (section, title, description, explanation)}.

    Skips the banner rows by locating the header row that contains 'Reference'.
    """
    raw = _read_raw(path)
    hdr = None
    for i in range(min(12, len(raw))):
        vals = [str(v).strip() for v in raw.iloc[i].tolist()]
        if "Reference" in vals:
            hdr, cols = i, vals
            break
    if hdr is None:
        sys.exit(f"Error: no 'Reference' header found in {path.name}")
    ci = {c: j for j, c in enumerate(cols)}
    c_ref = ci["Reference"]
    c_title = ci.get("Title / Description", ci.get("Title"))
    c_full = ci.get("Full Text", ci.get("Explanation"))

    out: Dict[str, Tuple[str, str, str, str]] = {}
    for i in range(hdr + 1, len(raw)):
        row = [str(v).strip() for v in raw.iloc[i].tolist()]
        code = row[c_ref] if c_ref < len(row) else ""
        if not CODE_RE.match(code) or code in out:
            continue
        title = row[c_title] if c_title is not None and c_title < len(row) else ""
        full = row[c_full] if c_full is not None and c_full < len(row) else ""
        out[code] = (section_of(code), title, "", full or None)
    log.info("Parsed %d requirement definitions from %s", len(out), path.name)
    return out


def parse_links(path: Path) -> List[Tuple[str, str]]:
    """IBC Code file -> [(chemical_name, code), ...] (codes split on ';')."""
    df = pd.read_excel(path, dtype=str, keep_default_na=False) if path.suffix.lower() != ".csv" \
        else pd.read_csv(path, dtype=str, keep_default_na=False)
    df.columns = [str(c).strip() for c in df.columns]
    for col in ("product_name", "specific_operational_requirements"):
        if col not in df.columns:
            sys.exit(f"Error: column '{col}' not found in {path.name}")
    pairs: List[Tuple[str, str]] = []
    for _, row in df.iterrows():
        chem = str(row["product_name"]).strip()
        codes = str(row["specific_operational_requirements"]).strip()
        if not chem or not codes:
            continue
        for code in re.split(r"[;,]", codes):
            code = code.strip()
            if CODE_RE.match(code):
                pairs.append((chem, code))
    log.info("Parsed %d cargo/code links from %s", len(pairs), path.name)
    return pairs


def resolve_source_id(cur, forced) -> int:
    if forced is not None:
        return forced
    cur.execute("SELECT id FROM source WHERE name ILIKE %s ORDER BY id LIMIT 1", (SOURCE_NAME,))
    row = cur.fetchone()
    if row is None:
        cur.execute("SELECT id FROM source WHERE name ILIKE '%ibc%code%' ORDER BY id LIMIT 1")
        row = cur.fetchone()
    if row is None:
        sys.exit("Error: could not resolve the 'IBC Code' source; pass --source-id.")
    return row[0]


def load_cargo_lookup(cur, source_id: int) -> Dict[str, int]:
    """lower(canonical_name) -> cargo_chemical.id under source_id (fallback: any)."""
    cur.execute("SELECT lower(canonical_name), id FROM cargo_chemical WHERE source_id=%s", (source_id,))
    lookup: Dict[str, int] = {}
    for name, cid in cur.fetchall():
        lookup.setdefault(name, cid)
    log.info("Loaded %d cargo names under source_id=%s", len(lookup), source_id)
    return lookup


def main():
    ap = argparse.ArgumentParser(description="Load operational requirements + cargo links.")
    ap.add_argument("--requirements", default=str(DEFAULT_REQUIREMENTS), help="definitions CSV")
    ap.add_argument("--links", default=str(DEFAULT_LINKS), help="IBC Code xlsx (cargo -> codes)")
    ap.add_argument("--source-id", type=int, default=None, help="force source_id")
    ap.add_argument("--dry-run", action="store_true", help="parse + log, no writes")
    args = ap.parse_args()

    load_dotenv(Path(__file__).parent.parent / ".env")
    req_path, link_path = Path(args.requirements), Path(args.links)
    for p in (req_path, link_path):
        if not p.is_file():
            sys.exit(f"Error: file not found: {p}")

    req_by_code = parse_requirements(req_path)
    links = parse_links(link_path)

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            source_id = resolve_source_id(cur, args.source_id)
            log.info("source_id=%s (%s)", source_id, SOURCE_NAME)
            cargo_by_name = load_cargo_lookup(cur, source_id)

            # resolve links -> (cargo_id, code); add stub definitions for any
            # referenced code that has no row in the master references file.
            link_pairs = set()
            missing_cargo = set()
            for chem, code in links:
                cid = cargo_by_name.get(chem.lower())
                if cid is None:
                    missing_cargo.add(chem)
                    continue
                if code not in req_by_code:
                    req_by_code[code] = (section_of(code), code, "", None)   # stub
                link_pairs.add((cid, code))

            log.info("Requirements: %d | links: %d | unmatched cargo names: %d",
                     len(req_by_code), len(link_pairs), len(missing_cargo))
            if missing_cargo:
                log.info("  sample unmatched: %s", sorted(missing_cargo)[:15])

            if args.dry_run:
                log.info("Dry run: nothing written.")
                return

            cur.execute(sql.SQL("TRUNCATE TABLE {} RESTART IDENTITY CASCADE")
                        .format(sql.Identifier(REQ_TABLE)))

            req_rows = [(code, sec, title, desc, expl, source_id)
                        for code, (sec, title, desc, expl) in req_by_code.items()]
            execute_values(
                cur,
                sql.SQL("INSERT INTO {} (code, section, title, description, explanation, source_id) VALUES %s")
                    .format(sql.Identifier(REQ_TABLE)),
                req_rows,
            )
            cur.execute(sql.SQL("SELECT code, id FROM {}").format(sql.Identifier(REQ_TABLE)))
            req_id_by_code = {code: rid for code, rid in cur.fetchall()}

            link_rows = [(cid, req_id_by_code[code], None)
                         for cid, code in link_pairs if code in req_id_by_code]
            if link_rows:
                execute_values(
                    cur,
                    sql.SQL("INSERT INTO {} (cargo_chemical_id, operational_requirement_id, notes) VALUES %s")
                        .format(sql.Identifier(LINK_TABLE)),
                    link_rows,
                )
            conn.commit()
            log.info("Done: %d requirements, %d links inserted.", len(req_rows), len(link_rows))
    except Exception:
        conn.rollback()
        log.exception("Import failed - rolled back")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
