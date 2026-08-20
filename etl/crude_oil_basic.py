#!/usr/bin/env python3
"""Load 'Crude Oils-Prop.xls' into crude_oil + crude_oil_property_values.

Source: "Crude Oil Basic Properties" (source.json, category 'oil').

Sheet layout (Sheet1):
    rows 0-4  banner and a two-line header
    row  5+   data
    col 0 Crude Oil | col 1 Country of Origin | col 2 Gravity (API)
    col 3 Sulfur (Weight%) | col 4 Pour Point (F)

Pour point here is in DEGREES FAHRENHEIT. The other crude-oil source publishes
it in Celsius. Both are stored with their own unit and never converted in
place - see etl/crude_oil_match_report.py, which converts only for comparison.

This loader writes nothing outside the crude-oil tables.
"""

import argparse
import logging
import os
import sys
from pathlib import Path

import psycopg2
import xlrd
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _crude_oil import (  # noqa: E402
    ensure_field_definitions,
    get_source_id,
    normalize_oil_name,
    parse_scalar,
    clean_text,
    upsert_crude_oil,
    upsert_property,
)
from _paths import input_file  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("crude_oil_basic")

SOURCE_NAME = "Crude Oil Basic Properties"
ENTERED_BY = "crude_oil_basic.py"
DEFAULT_FILE = str(input_file("Crude Oils-Prop.xls"))
SHEET = "Sheet1"
FIRST_DATA_ROW = 5

COL_NAME, COL_COUNTRY, COL_API, COL_SULFUR, COL_POUR = 0, 1, 2, 3, 4

# field_name, column, unit, whether the cell may carry its own unit
COLUMNS = [
    ("API", COL_API, "°API", False),
    ("SULFUR", COL_SULFUR, "wt%", True),
    ("POUR_POINT", COL_POUR, "°F", False),
]
FIELDS = [c[0] for c in COLUMNS]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", default=DEFAULT_FILE)
    ap.add_argument("--dry-run", action="store_true", help="parse and report, write nothing")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.is_file():
        sys.exit(f"Error: file not found: {path}")

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        sys.exit("Error: DATABASE_URL not set (checked .env).")

    sheet = xlrd.open_workbook(str(path)).sheet_by_name(SHEET)
    log.info("Reading %s [%s] rows=%d", path.name, SHEET, sheet.nrows)

    conn = psycopg2.connect(db_url)
    n_oils = n_created = n_props = n_blank = 0
    skipped_no_data = []
    try:
        with conn.cursor() as cur:
            source_id = get_source_id(cur, SOURCE_NAME)
            log.info("source_id=%s (%s)", source_id, SOURCE_NAME)
            log.info("field_definitions added: %d",
                     ensure_field_definitions(cur, only=FIELDS))

            for r in range(FIRST_DATA_ROW, sheet.nrows):
                raw_name = clean_text(sheet.cell_value(r, COL_NAME))
                if not raw_name:
                    n_blank += 1
                    continue

                oil_name = normalize_oil_name(raw_name)
                country = clean_text(sheet.cell_value(r, COL_COUNTRY))

                parsed_props = []
                for field_name, col, unit, allow_override in COLUMNS:
                    parsed = parse_scalar(sheet.cell_value(r, col), unit,
                                          allow_unit_override=allow_override)
                    if parsed is not None:
                        parsed_props.append((field_name, parsed))

                # The sheet ends with a footnote typed into the name column
                # ("NOTE : ... PPM = PARTS PER MILLION ..."). A real entry always
                # has a country or at least one measured property; that footnote
                # has neither, so this rejects it without hardcoding its text.
                if country is None and not parsed_props:
                    skipped_no_data.append(raw_name)
                    continue

                oil_id, created = upsert_crude_oil(cur, oil_name, source_id, country)
                n_oils += 1
                n_created += int(created)

                for field_name, parsed in parsed_props:
                    upsert_property(cur, oil_id, source_id, field_name, parsed, ENTERED_BY)
                n_props += len(parsed_props)

            log.info("=" * 60)
            log.info("crude oils      : %d (%d new)", n_oils, n_created)
            log.info("property values : %d", n_props)
            log.info("blank rows      : %d", n_blank)
            if skipped_no_data:
                log.info("non-data rows skipped: %d", len(skipped_no_data))
                for name in skipped_no_data:
                    log.info("    %r", name[:70])
            log.info("=" * 60)

            if args.dry_run:
                conn.rollback()
                log.info("Dry run: rolled back.")
            else:
                conn.commit()
                log.info("✓ Committed.")
    except Exception:
        conn.rollback()
        log.exception("Load failed - rolled back")
        raise
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
