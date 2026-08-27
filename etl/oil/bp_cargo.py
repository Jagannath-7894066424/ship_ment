#!/usr/bin/env python3
"""
Load the BP Tank Cleaning Guide cargo list into crude_oil, with the guide's
per-cargo instruction in crude_oil_property_values.

SOURCE
------
"BP Tank Cleaning Guide" (source.json, category 'oil') - the same source row as
bp_procedure_templates.py and bp_cargo_matrix.py. This loader must run BEFORE
bp_cargo_matrix.py, which resolves its from/to columns against the rows created
here.

INPUT
-----
"... - cargo names.xlsx": 22 rows of

    id | cargo_name | cargo_type | final_fresh_water_wash_required |
    cargo_specific_instruction

The sheet's `id` is its own 1..22 numbering, not a database id. It is NOT
stored - cargo_name is the identity, and crude_oil's key is (oil_name,
source_id). bp_cargo_matrix.py joins on the name for the same reason.

WHY crude_oil
-------------
cargo_type is OIL on all 22 rows, and crude_oil is the oil-cargo master that
shell_cargo_master.py and hm50_cargo_matrix.py already use for refined products.
These rows are per-source, so BP's "Kerosenes (dyed)" sits alongside HM 50's
"Kerosene (dyed)" without either being merged into the other.

THE INSTRUCTION
---------------
`cargo_specific_instruction` is one sentence, repeated verbatim on the 7 cargoes
that need a final fresh-water wash. It has no crude_oil column, so it is stored
as a property value under BP_CARGO_INSTRUCTION - the same treatment
HM50_CARGO_GUIDANCE gets, and for the same reason: it is per-cargo prose owned
by one source.

`final_fresh_water_wash_required` is deliberately NOT stored. It is Yes on
exactly the rows carrying that sentence and No on the rest, so it restates the
presence of the instruction rather than adding to it; a row's flag is recovered
by asking whether it has a BP_CARGO_INSTRUCTION value. The loader VERIFIES that
correspondence and fails if the sheet ever breaks it, so the omission cannot
silently lose a case where the flag means something new.

IDEMPOTENCY
-----------
Upsert on (oil_name, source_id) for the master and
(crude_oil_id, source_id, field_name) for the property. One transaction.

Usage:
    python3 etl/oil/bp_cargo.py
    python3 etl/oil/bp_cargo.py --dry-run
"""

import argparse
import logging
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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
log = logging.getLogger("bp_cargo")

SOURCE_NAME = "BP Tank Cleaning Guide"
FIELD_NAME = "BP_CARGO_INSTRUCTION"
ENTERED_BY = "bp_cargo.py"
CARGO_TYPE = "OIL"
DEFAULT_FILE = input_file("BP Tank Cleaning Guide - cargo names.xlsx")

COLS = ["cargo_name", "cargo_type", "final_fresh_water_wash_required",
        "cargo_specific_instruction"]


def clean(value) -> Optional[str]:
    if value is None:
        return None
    s = re.sub(r"[ \t]+", " ", str(value).replace("\n", " ")).strip()
    return s or None


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
        name = clean(r["cargo_name"])
        if name is None:
            continue
        if name in seen:
            errors.append(f"row {i}: duplicate cargo_name {name!r}")
            continue
        seen.add(name)

        sheet_type = (clean(r["cargo_type"]) or CARGO_TYPE).upper()
        if sheet_type != CARGO_TYPE:
            errors.append(f"row {i}: {name!r} says cargo_type {sheet_type!r}; this "
                          f"loader writes to the {CARGO_TYPE} master (crude_oil)")
            continue

        flag_raw = clean(r["final_fresh_water_wash_required"])
        flag = (flag_raw or "").strip().lower() in {"yes", "y", "true", "1", "1.0"}
        instruction = clean(r["cargo_specific_instruction"])

        # The flag is not stored, so it must stay derivable - see THE
        # INSTRUCTION. A row that breaks the correspondence means the flag has
        # started to say something of its own, and must not be dropped silently.
        if flag != (instruction is not None):
            errors.append(
                f"row {i}: {name!r} has final_fresh_water_wash_required="
                f"{flag_raw!r} but {'no' if instruction is None else 'an'} "
                f"instruction. The flag is not stored because it restates the "
                f"instruction; this row breaks that, so it needs a decision.")
            continue

        rows.append({"oil_name": name, "instruction": instruction})
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

    with_instruction = sum(1 for r in rows if r["instruction"])
    log.info("%s: %d cargo(es), %d carrying an instruction", path.name, len(rows),
             with_instruction)

    if args.dry_run:
        for r in rows:
            log.info("  %-36s instruction=%s", r["oil_name"], "yes" if r["instruction"] else "-")
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
                if r["instruction"]:
                    upsert_property(
                        cur, oil_id, source_id, FIELD_NAME,
                        Parsed(value=r["instruction"], normalized_value=None,
                               normalized_min=None, normalized_max=None, unit=None,
                               value_type="text",
                               notes="BP states this instruction for the cargo; it "
                                     "is the guide's final-fresh-water-wash rule."),
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
