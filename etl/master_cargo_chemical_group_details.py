#!/usr/bin/env python3
"""
Load cargo_chemicals_groupData.csv into master_cargo_chemical_group_details.

The CSV holds one row per (reactive group, chemical):

    Group ID, Group Name, Chemical Name
    0,        Unassigned Cargoes, Acetone cyanohydrin
    34,       Esters,             ...

mapped to the table columns:

    Group ID      -> group_code
    Group Name    -> group_name
    Chemical Name -> cargo_name

One row per chemical is stored (group_code is NOT unique). Re-runs are
idempotent: an existing (group_code, cargo_name) pair is skipped, and duplicate
rows within the CSV are collapsed.

Usage:
    python master_cargo_chemical_group_details.py
    python master_cargo_chemical_group_details.py --file /path/to.csv
    python master_cargo_chemical_group_details.py --wipe      # truncate first
    python master_cargo_chemical_group_details.py --dry-run

Reads DATABASE_URL from the .env file in this directory.
"""

import argparse
import logging
import os
import sys
from pathlib import Path

from _paths import input_file
from typing import Set, Tuple

import pandas as pd
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("master_cargo_chemical_group_details")

DEFAULT_FILE = input_file("cargo_chemicals_groupData.csv")
TABLE = "master_cargo_chemical_group_details"

# CSV column -> table column
COL_GROUP_CODE = "Group ID"
COL_GROUP_NAME = "Group Name"
COL_CARGO_NAME = "Chemical Name"


def _text(value) -> str:
    """Trim a cell to a string ('' for None/NaN)."""
    if value is None:
        return ""
    s = str(value).strip()
    return "" if s.lower() in ("nan", "nat") else s


def read_rows(path: Path):
    """Read the CSV into cleaned (group_code, group_name, cargo_name) tuples."""
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df.columns = [str(c).strip() for c in df.columns]
    missing = [c for c in (COL_GROUP_CODE, COL_GROUP_NAME, COL_CARGO_NAME) if c not in df.columns]
    if missing:
        sys.exit(f"Error: {path.name} is missing column(s): {missing}. Found: {list(df.columns)}")

    rows = []
    for _, r in df.iterrows():
        code = _text(r[COL_GROUP_CODE])
        name = _text(r[COL_GROUP_NAME])
        cargo = _text(r[COL_CARGO_NAME])
        if not cargo:                       # a chemical name is required
            continue
        rows.append((code, name, cargo))
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Load group->chemical CSV into the DB.")
    parser.add_argument("--file", default=DEFAULT_FILE, help="path to the CSV")
    parser.add_argument("--wipe", action="store_true", help="truncate the table first")
    parser.add_argument("--dry-run", action="store_true", help="report, write nothing")
    args = parser.parse_args()

    path = Path(args.file)
    if not path.is_file():
        sys.exit(f"Error: file not found: {path}")

    load_dotenv(Path(__file__).parent.parent / ".env")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        sys.exit("Error: DATABASE_URL not set (checked .env).")

    rows = read_rows(path)
    log.info("Read %d chemical rows from %s", len(rows), path.name)

    conn = psycopg2.connect(db_url)
    try:
        with conn.cursor() as cur:
            if args.wipe and not args.dry_run:
                cur.execute(f'TRUNCATE TABLE "{TABLE}" RESTART IDENTITY')
                log.info("Truncated %s", TABLE)

            # Existing (group_code, cargo_name) pairs -> idempotent skip.
            existing: Set[Tuple[str, str]] = set()
            if not args.wipe:
                cur.execute(f'SELECT group_code, cargo_name FROM "{TABLE}"')
                existing = {(a, b) for a, b in cur.fetchall()}

            # group_code is a FK to reactive_groups.group_code, so a row whose code
            # isn't a real reactive group (e.g. the "0" = unclassified placeholder)
            # would fail the FK. Drop those rows rather than aborting the load.
            cur.execute("SELECT group_code FROM reactive_groups")
            valid_codes = {r[0] for r in cur.fetchall()}

            to_insert = []
            seen: Set[Tuple[str, str]] = set()
            n_orphan = 0
            for code, name, cargo in rows:
                if code not in valid_codes:
                    n_orphan += 1           # no such reactive group -> skip
                    continue
                key = (code, cargo)
                if key in existing or key in seen:
                    continue                # duplicate row -> skip
                seen.add(key)
                to_insert.append((code, name, cargo))

            log.info("New rows to insert: %d (skipped %d existing/duplicate, %d orphan group_code)",
                     len(to_insert), len(rows) - len(to_insert) - n_orphan, n_orphan)

            if args.dry_run:
                log.info("Dry run: nothing written.")
                conn.rollback()
                return

            if to_insert:
                execute_values(
                    cur,
                    f'INSERT INTO "{TABLE}" '
                    "(group_code, group_name, cargo_name, created_at, updated_at) VALUES %s",
                    to_insert,
                    template="(%s, %s, %s, now(), now())",
                )
            conn.commit()
            log.info("✓ Inserted %d rows into %s.", len(to_insert), TABLE)
    except Exception:
        conn.rollback()
        log.exception("Load failed - rolled back.")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
