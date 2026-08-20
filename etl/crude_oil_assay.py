#!/usr/bin/env python3
"""Load 'Crudeoildata.XLS' into crude_oil + crude_oil_property_values.

Source: "Crude Oil Assay and Operational Properties" (source.json, category 'oil').

Sheet layout (ANNEX 1):
    rows 0-3  a four-line stacked header
    row  4+   data
    Most quantities are published as a Min/Max column PAIR, so they land as
    value_type 'range' with normalized_min / normalized_max. Missing cells are
    written as '-' by the source, not left empty.

Two departures from the original field mapping, both forced by the actual sheet:

  * COW: the sheet has TWO columns, "COW REQ CODE# (DBT)" and "(SBT)" - dirty
    versus segregated ballast tankers - not the LOAD/CARRIAGE/DISCHARGE triple.
    That triple belongs to "MINIMUM TEMPERATURE REQUIRED", which is mapped as
    specified. So COW is loaded as COW_DBT / COW_SBT.

  * WAX vs GAS_GT_C4: col 6 is "GAS>C4 (%Wt)" and col 7 is "TOTAL WAX (%wt)".
    They are mapped by header, so for Abu Al Bu Khoosh WAX = 4.61 and
    GAS_GT_C4 = 1.15.

Pour point here is in DEGREES CELSIUS; the basic source publishes it in
Fahrenheit. Neither is converted on load.
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
    clean_text,
    ensure_field_definitions,
    get_source_id,
    normalize_oil_name,
    parse_assay_date,
    parse_range,
    parse_scalar,
    Parsed,
    upsert_crude_oil,
    upsert_property,
)
from _paths import input_file  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("crude_oil_assay")

SOURCE_NAME = "Crude Oil Assay and Operational Properties"
ENTERED_BY = "crude_oil_assay.py"
DEFAULT_FILE = str(input_file("Crudeoildata.XLS"))
SHEET = "ANNEX 1"
FIRST_DATA_ROW = 4

COL_NAME, COL_ASSAY_DATE = 0, 1

# (field_name, min_col, max_col, unit) - a Min/Max pair.
RANGE_COLUMNS = [
    ("API",         2,  3,  "°API"),
    ("RVP",         4,  5,  "psi"),
    ("POUR_POINT",  8,  9,  "°C"),
    ("CLOUD_POINT", 10, 11, "°C"),
]

# (field_name, col, unit) - a single column.
SCALAR_COLUMNS = [
    ("GAS_GT_C4",                    6,  "wt%"),
    ("WAX",                          7,  "wt%"),
    ("VISCOSITY_T1",                 12, "°C"),
    ("VISCOSITY_X1",                 13, "cSt"),
    ("VISCOSITY_T2",                 14, "°C"),
    ("VISCOSITY_X2",                 15, "cSt"),
    ("MINIMUM_TEMPERATURE_LOAD",     16, "°C"),
    ("MINIMUM_TEMPERATURE_CARRIAGE", 17, "°C"),
    ("MINIMUM_TEMPERATURE_DISCHARGE", 18, "°C"),
    ("COW_DBT",                      19, None),
    ("COW_SBT",                      20, None),
    ("H2S_OIL_PHASE_NORMAL",         21, "ppm"),
    ("H2S_OIL_PHASE_MAX",            22, "ppm"),
    ("BENZENE",                      23, "wt%"),
    ("REMARKS",                      24, None),
]

FIELDS = ([c[0] for c in RANGE_COLUMNS]
          + [c[0] for c in SCALAR_COLUMNS]
          + ["ASSAY_DATE"])


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

    book = xlrd.open_workbook(str(path))
    sheet = book.sheet_by_name(SHEET)
    log.info("Reading %s [%s] rows=%d", path.name, SHEET, sheet.nrows)

    conn = psycopg2.connect(db_url)
    n_oils = n_created = n_props = n_blank = 0
    n_dated = n_undated = n_partial_date = 0
    per_field = {}
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

                # The assay date applies to every property measured in this row,
                # so it is stamped on all of them, not just kept in one place.
                as_of, printed, date_note = parse_assay_date(
                    sheet.cell_value(r, COL_ASSAY_DATE),
                    sheet.cell_type(r, COL_ASSAY_DATE),
                    book.datemode,
                )
                if as_of is not None:
                    n_dated += 1
                    if date_note:
                        n_partial_date += 1
                else:
                    n_undated += 1

                # No country column in this source; left NULL rather than
                # borrowed from the other source, which is a different record.
                oil_id, created = upsert_crude_oil(cur, oil_name, source_id, None)
                n_oils += 1
                n_created += int(created)

                row_props = 0
                for field_name, lo_col, hi_col, unit in RANGE_COLUMNS:
                    parsed = parse_range(sheet.cell_value(r, lo_col),
                                         sheet.cell_value(r, hi_col), unit)
                    if parsed is None:
                        continue
                    upsert_property(cur, oil_id, source_id, field_name, parsed,
                                    ENTERED_BY, as_of_date=as_of)
                    per_field[field_name] = per_field.get(field_name, 0) + 1
                    row_props += 1

                for field_name, col, unit in SCALAR_COLUMNS:
                    parsed = parse_scalar(sheet.cell_value(r, col), unit)
                    if parsed is None:
                        continue
                    upsert_property(cur, oil_id, source_id, field_name, parsed,
                                    ENTERED_BY, as_of_date=as_of)
                    per_field[field_name] = per_field.get(field_name, 0) + 1
                    row_props += 1

                # Keep the assay date's printed form verbatim - "Dec_92" and
                # "1973" are not dates and would otherwise be lost to the
                # convention applied in as_of_date.
                if printed:
                    parsed_date = Parsed(value=printed, normalized_value=None,
                                         normalized_min=None, normalized_max=None,
                                         unit=None, value_type="text", notes=None)
                    upsert_property(cur, oil_id, source_id, "ASSAY_DATE", parsed_date,
                                    ENTERED_BY, as_of_date=as_of, extra_note=date_note)
                    per_field["ASSAY_DATE"] = per_field.get("ASSAY_DATE", 0) + 1
                    row_props += 1

                n_props += row_props

            log.info("=" * 60)
            log.info("crude oils      : %d (%d new)", n_oils, n_created)
            log.info("property values : %d", n_props)
            log.info("blank rows      : %d", n_blank)
            log.info("assay dates     : %d parsed (%d month/year-only), %d unparsed",
                     n_dated, n_partial_date, n_undated)
            log.info("per field:")
            for f in FIELDS:
                log.info("    %-30s %5d", f, per_field.get(f, 0))
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
