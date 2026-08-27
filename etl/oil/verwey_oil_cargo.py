#!/usr/bin/env python3
"""
Load the Dr Verwey oil cargo list into crude_oil, with the guide's trade code in
crude_oil_property_values.

SOURCE
------
"Dr Verweys Tank Cleaning Guide - Oil" (source.json, category 'oil').

This is a SEPARATE source row from the two chemical Verwey guides already
registered, and deliberately so. The same guide publishes both, but
procedure_templates is keyed on (source_id, procedure_code) and the oil code
letters collide with chemical ones that mean something else: source 7's `D` is
a published five-step procedure ("Butterworthing with cold seawater for 1 hour;
Flushing with freshwater; Steaming; Draining"), while the oil `D` is a
matrix-derived code with its own step list. Loading the oil codes under a
chemical Verwey source would overwrite seven such definitions and cascade their
steps away. A source per cargo type keeps both intact.

INPUT
-----
"... Oil - cargo names.xlsx": 8 rows of

    id | cargo_oil_name | trade_code | cargo_type

The sheet's `id` is its own 1..8 numbering, not a database id, and is not
stored - cargo_oil_name is the identity, and crude_oil is keyed on (oil_name,
source_id). The matrix loader joins on the name for the same reason.

`trade_code` is Verwey's own cargo number (Crude = 84, Naphtha = 279). It has no
crude_oil column, so it is stored as a property value under VERWEY_TRADE_CODE -
the same treatment BP_CARGO_INSTRUCTION and HM50_CARGO_GUIDANCE get. Kept
because it is how the printed guide indexes a cargo, and a reader with the book
open needs it to find the row.

IDEMPOTENCY
-----------
Upsert on (oil_name, source_id) and (crude_oil_id, source_id, field_name).
One transaction.

Usage:
    python3 etl/oil/verwey_oil_cargo.py
    python3 etl/oil/verwey_oil_cargo.py --dry-run
"""

import argparse
import logging
import os
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import openpyxl
import psycopg2
from dotenv import load_dotenv

# Loaders are run as scripts (python3 etl/<branch>/<file>.py), so only their
# own directory is on sys.path. Add the shared helpers.
_ETL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ETL_ROOT / "common"))

from _crude_oil import Parsed, ensure_field_definitions, upsert_property  # noqa: E402
from _paths import input_file  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("verwey_oil_cargo")

SOURCE_NAME = "Dr Verweys Tank Cleaning Guide - Oil"
FIELD_NAME = "VERWEY_TRADE_CODE"
ENTERED_BY = "verwey_oil_cargo.py"
CARGO_TYPE = "OIL"
DEFAULT_FILE = input_file("Dr Verweys Tank Cleaning Guide Oil - cargo names.xlsx")

COLS = ["cargo_oil_name", "trade_code", "cargo_type"]


def clean(value) -> Optional[str]:
    if value is None:
        return None
    s = re.sub(r"[ \t]+", " ", str(value).replace("\n", " ")).strip()
    return s or None


def trade_code(value) -> Optional[str]:
    """Excel stores the codes as numbers, so 84 arrives as '84.0'."""
    s = clean(value)
    if s is None:
        return None
    return s[:-2] if re.fullmatch(r"\d+\.0", s) else s


def resolve_source(cur, name: str) -> int:
    cur.execute("SELECT id FROM source WHERE name = %s", (name,))
    row = cur.fetchone()
    if row is None:
        sys.exit(
            f"Error: source {name!r} not found.\n"
            f"  It is declared in etl/data/source.json - register it with:\n"
            f"      python3 etl/common/source.py"
        )
    return row[0]


def read_cargoes(path: Path) -> Tuple[List[dict], List[str]]:
    ws = openpyxl.load_workbook(path, data_only=True).worksheets[0]
    grid = [list(r) for r in ws.iter_rows(values_only=True)]
    errors: List[str] = []
    if not grid:
        return [], [f"{path.name} is empty"]

    header = [clean(c) for c in grid[0]]
    missing = [c for c in COLS if c not in header]
    if missing:
        sys.exit(f"Error: {path.name} is missing column(s): {', '.join(missing)}")

    rows: List[dict] = []
    seen: set = set()
    for i, raw in enumerate(grid[1:], start=2):
        r = dict(zip(header, raw))
        name = clean(r["cargo_oil_name"])
        if name is None:
            continue
        if name in seen:
            errors.append(f"row {i}: duplicate cargo_oil_name {name!r}")
            continue
        seen.add(name)

        sheet_type = (clean(r["cargo_type"]) or CARGO_TYPE).upper()
        if sheet_type != CARGO_TYPE:
            errors.append(f"row {i}: {name!r} says cargo_type {sheet_type!r}; this "
                          f"loader writes to the {CARGO_TYPE} master (crude_oil)")
            continue

        rows.append({"oil_name": name, "trade_code": trade_code(r["trade_code"])})
    return rows, errors


def upsert_oil(cur, source_id: int, oil_name: str) -> Tuple[int, bool]:
    cur.execute("SELECT id FROM crude_oil WHERE oil_name = %s AND source_id = %s",
                (oil_name, source_id))
    row = cur.fetchone()
    if row:
        return row[0], False
    cur.execute(
        "INSERT INTO crude_oil (oil_name, source_id, created_at, updated_at) "
        "VALUES (%s, %s, now(), now()) RETURNING id",
        (oil_name, source_id),
    )
    return cur.fetchone()[0], True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", default=str(DEFAULT_FILE))
    ap.add_argument("--dry-run", action="store_true", help="parse and report, write nothing")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.is_file():
        sys.exit(f"Error: file not found: {path}")

    rows, errors = read_cargoes(path)
    if errors:
        log.error("%d validation error(s) - nothing was imported:", len(errors))
        for e in errors:
            log.error("    %s", e)
        return 1

    log.info("%s: %d cargo(es), %d with a trade code", path.name, len(rows),
             sum(1 for r in rows if r["trade_code"]))

    if args.dry_run:
        for r in rows:
            log.info("  %-14s trade_code=%s", r["oil_name"], r["trade_code"] or "-")
        log.info("--dry-run: nothing written.")
        return 0

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        sys.exit("Error: DATABASE_URL not set (checked .env).")

    conn = psycopg2.connect(db_url)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            source_id = resolve_source(cur, SOURCE_NAME)
            log.info("Source id=%s (%r)", source_id, SOURCE_NAME)

            added = ensure_field_definitions(cur, only=[FIELD_NAME])
            log.info("field_definitions: %s", f"{FIELD_NAME} created" if added
                     else f"{FIELD_NAME} already present")

            created = props = 0
            for r in rows:
                oil_id, is_new = upsert_oil(cur, source_id, r["oil_name"])
                created += is_new
                if r["trade_code"]:
                    upsert_property(
                        cur, oil_id, source_id, FIELD_NAME,
                        Parsed(value=r["trade_code"], normalized_value=None,
                               normalized_min=None, normalized_max=None, unit=None,
                               value_type="text",
                               notes="The guide's own cargo number, kept as text: it "
                                     "identifies a row in the printed book, it is not "
                                     "a measured quantity."),
                        entered_by=ENTERED_BY,
                    )
                    props += 1

        conn.commit()
        log.info("✓ Committed. crude_oil: %d cargo(es) (%d created this run) | "
                 "crude_oil_property_values (%s): %d", len(rows), created,
                 FIELD_NAME, props)
        return 0
    except Exception:
        conn.rollback()
        log.exception("Import failed - rolled back, nothing written.")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
