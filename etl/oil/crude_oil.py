#!/usr/bin/env python3
"""Load the crude-oil sources into crude_oil + crude_oil_property_values.

Both sources feed the same pair of tables and differ only in where the columns
sit and what shape the cells take, so they are described here as data - one
SourceSpec each - and driven by one loader rather than one script apiece.

    --source basic    "Crude Oil Basic Properties"                 Crude Oils-Prop.xls
    --source assay    "Crude Oil Assay and Operational Properties" Crudeoildata.XLS
    --source all      both, in one transaction (the default)

Each source keeps its OWN crude_oil rows: identity is (oil_name, source_id), so
the same crude appears twice with each source's own figures and neither can
overwrite the other. etl/oil/crude_oil_match_report.py reconciles them without
merging anything.

Nothing here touches cargo_chemical or cargo_property_values. Crude oils are a
separate entity; the two branches meet only at `source`.

BASIC - 'Crude Oils-Prop.xls', Sheet1
    rows 0-4  banner and a two-line header
    row  5+   data
    col 0 Crude Oil | col 1 Country of Origin | col 2 Gravity (API)
    col 3 Sulfur (Weight%) | col 4 Pour Point (F)

    The sheet ends with a footnote typed into the name column ("NOTE : ... PPM =
    PARTS PER MILLION ..."). A real entry always has a country or at least one
    measured property; that footnote has neither, so drop_rows_without_data
    rejects it without hardcoding its text.

ASSAY - 'Crudeoildata.XLS', ANNEX 1
    rows 0-3  a four-line stacked header
    row  4+   data
    Most quantities are published as a Min/Max column PAIR, so they land as
    value_type 'range' with normalized_min / normalized_max. Missing cells are
    written as '-' by the source, not left empty.

    Two departures from the original field mapping, both forced by the sheet:

      * COW: the sheet has TWO columns, "COW REQ CODE# (DBT)" and "(SBT)" -
        dirty versus segregated ballast tankers - not the LOAD/CARRIAGE/DISCHARGE
        triple. That triple belongs to "MINIMUM TEMPERATURE REQUIRED", which is
        mapped as specified. So COW is loaded as COW_DBT / COW_SBT.

      * WAX vs GAS_GT_C4: col 6 is "GAS>C4 (%Wt)" and col 7 is "TOTAL WAX (%wt)".
        They are mapped by header, so for Abu Al Bu Khoosh WAX = 4.61 and
        GAS_GT_C4 = 1.15.

    There is no country column; it is left NULL rather than borrowed from the
    basic source, which is a different record.

POUR POINT UNITS
    Basic publishes it in DEGREES FAHRENHEIT, assay in DEGREES CELSIUS. Neither
    is converted on load - every value row carries its own unit, and the match
    report converts only for comparison.

Usage:
    python3 etl/oil/crude_oil.py                                  # both sources
    python3 etl/oil/crude_oil.py --source basic
    python3 etl/oil/crude_oil.py --source assay path/to/other.XLS
    python3 etl/oil/crude_oil.py --dry-run                        # parse, write nothing
"""

import argparse
import logging
import os
import sys
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import psycopg2
import xlrd
from dotenv import load_dotenv


# Loaders are run as scripts (python3 etl/<branch>/<file>.py), so only their
# own directory is on sys.path. Add the shared helpers.
_ETL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ETL_ROOT / "common"))

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
log = logging.getLogger("crude_oil")

# One loader now writes both sources, so entered_by names this file for both.
# It is not what tells them apart - source_id is, and that is part of the key.
ENTERED_BY = "crude_oil.py"


# ---------------------------------------------------------------------------
# Source definitions
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SourceSpec:
    """Everything that differs between the two sheets."""

    key: str                     # --source value
    source_name: str             # must match source.json
    default_file: str
    sheet: str
    first_data_row: int
    col_name: int
    col_country: Optional[int] = None
    col_assay_date: Optional[int] = None
    # (field_name, min_col, max_col, unit) - a Min/Max pair.
    range_columns: Tuple[Tuple[str, int, int, Optional[str]], ...] = ()
    # (field_name, col, unit, whether the cell may carry its own unit).
    scalar_columns: Tuple[Tuple[str, int, Optional[str], bool], ...] = ()
    # Reject rows carrying neither a country nor any measured property.
    drop_rows_without_data: bool = False

    @property
    def fields(self) -> List[str]:
        """Every field_name this source can write, in report order."""
        names = [c[0] for c in self.range_columns]
        names += [c[0] for c in self.scalar_columns]
        if self.col_assay_date is not None:
            names.append("ASSAY_DATE")
        return names


BASIC = SourceSpec(
    key="basic",
    source_name="Crude Oil Basic Properties",
    default_file=str(input_file("Crude Oils-Prop.xls")),
    sheet="Sheet1",
    first_data_row=5,
    col_name=0,
    col_country=1,
    scalar_columns=(
        ("API",        2, "°API", False),
        ("SULFUR",     3, "wt%",  True),
        ("POUR_POINT", 4, "°F",   False),
    ),
    drop_rows_without_data=True,
)

ASSAY = SourceSpec(
    key="assay",
    source_name="Crude Oil Assay and Operational Properties",
    default_file=str(input_file("Crudeoildata.XLS")),
    sheet="ANNEX 1",
    first_data_row=4,
    col_name=0,
    col_assay_date=1,
    range_columns=(
        ("API",         2,  3,  "°API"),
        ("RVP",         4,  5,  "psi"),
        ("POUR_POINT",  8,  9,  "°C"),
        ("CLOUD_POINT", 10, 11, "°C"),
    ),
    scalar_columns=(
        ("GAS_GT_C4",                     6,  "wt%", False),
        ("WAX",                           7,  "wt%", False),
        ("VISCOSITY_T1",                  12, "°C",  False),
        ("VISCOSITY_X1",                  13, "cSt", False),
        ("VISCOSITY_T2",                  14, "°C",  False),
        ("VISCOSITY_X2",                  15, "cSt", False),
        ("MINIMUM_TEMPERATURE_LOAD",      16, "°C",  False),
        ("MINIMUM_TEMPERATURE_CARRIAGE",  17, "°C",  False),
        ("MINIMUM_TEMPERATURE_DISCHARGE", 18, "°C",  False),
        ("COW_DBT",                       19, None,  False),
        ("COW_SBT",                       20, None,  False),
        ("H2S_OIL_PHASE_NORMAL",          21, "ppm", False),
        ("H2S_OIL_PHASE_MAX",             22, "ppm", False),
        ("BENZENE",                       23, "wt%", False),
        ("REMARKS",                       24, None,  False),
    ),
)

SPECS: Dict[str, SourceSpec] = {s.key: s for s in (BASIC, ASSAY)}


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
@dataclass
class Stats:
    n_oils: int = 0
    n_created: int = 0
    n_props: int = 0
    n_blank: int = 0
    n_dated: int = 0
    n_undated: int = 0
    n_partial_date: int = 0
    skipped_no_data: List[str] = dc_field(default_factory=list)
    per_field: Dict[str, int] = dc_field(default_factory=dict)

    def counted(self, field_name: str) -> None:
        self.per_field[field_name] = self.per_field.get(field_name, 0) + 1
        self.n_props += 1


def load_source(cur, spec: SourceSpec, path: Path) -> Stats:
    """Read one sheet into the crude-oil tables. Caller owns the transaction."""
    book = xlrd.open_workbook(str(path))
    sheet = book.sheet_by_name(spec.sheet)
    log.info("Reading %s [%s] rows=%d", path.name, spec.sheet, sheet.nrows)

    source_id = get_source_id(cur, spec.source_name)
    log.info("source_id=%s (%s)", source_id, spec.source_name)
    log.info("field_definitions added: %d",
             ensure_field_definitions(cur, only=spec.fields))

    st = Stats()
    for r in range(spec.first_data_row, sheet.nrows):
        raw_name = clean_text(sheet.cell_value(r, spec.col_name))
        if not raw_name:
            st.n_blank += 1
            continue

        oil_name = normalize_oil_name(raw_name)
        country = (clean_text(sheet.cell_value(r, spec.col_country))
                   if spec.col_country is not None else None)

        # The assay date applies to every property measured in this row, so it
        # is stamped on all of them, not just kept in one place.
        as_of = printed = date_note = None
        if spec.col_assay_date is not None:
            as_of, printed, date_note = parse_assay_date(
                sheet.cell_value(r, spec.col_assay_date),
                sheet.cell_type(r, spec.col_assay_date),
                book.datemode,
            )
            if as_of is not None:
                st.n_dated += 1
                if date_note:
                    st.n_partial_date += 1
            else:
                st.n_undated += 1

        # Parse before writing anything: whether the row is data at all can
        # depend on whether it yielded any property.
        parsed_props: List[Tuple[str, Parsed]] = []
        for field_name, lo_col, hi_col, unit in spec.range_columns:
            parsed = parse_range(sheet.cell_value(r, lo_col),
                                 sheet.cell_value(r, hi_col), unit)
            if parsed is not None:
                parsed_props.append((field_name, parsed))
        for field_name, col, unit, allow_override in spec.scalar_columns:
            parsed = parse_scalar(sheet.cell_value(r, col), unit,
                                  allow_unit_override=allow_override)
            if parsed is not None:
                parsed_props.append((field_name, parsed))

        if spec.drop_rows_without_data and country is None and not parsed_props:
            st.skipped_no_data.append(raw_name)
            continue

        oil_id, created = upsert_crude_oil(cur, oil_name, source_id, country)
        st.n_oils += 1
        st.n_created += int(created)

        for field_name, parsed in parsed_props:
            upsert_property(cur, oil_id, source_id, field_name, parsed,
                            ENTERED_BY, as_of_date=as_of)
            st.counted(field_name)

        # Keep the assay date's printed form verbatim - "Dec_92" and "1973" are
        # not dates and would otherwise be lost to the convention applied in
        # as_of_date.
        if printed:
            parsed_date = Parsed(value=printed, normalized_value=None,
                                 normalized_min=None, normalized_max=None,
                                 unit=None, value_type="text", notes=None)
            upsert_property(cur, oil_id, source_id, "ASSAY_DATE", parsed_date,
                            ENTERED_BY, as_of_date=as_of, extra_note=date_note)
            st.counted("ASSAY_DATE")

    return st


def report(spec: SourceSpec, st: Stats) -> None:
    log.info("=" * 60)
    log.info("%s", spec.source_name)
    log.info("crude oils      : %d (%d new)", st.n_oils, st.n_created)
    log.info("property values : %d", st.n_props)
    log.info("blank rows      : %d", st.n_blank)
    if spec.col_assay_date is not None:
        log.info("assay dates     : %d parsed (%d month/year-only), %d unparsed",
                 st.n_dated, st.n_partial_date, st.n_undated)
    if st.skipped_no_data:
        log.info("non-data rows skipped: %d", len(st.skipped_no_data))
        for name in st.skipped_no_data:
            log.info("    %r", name[:70])
    log.info("per field:")
    for f in spec.fields:
        log.info("    %-30s %5d", f, st.per_field.get(f, 0))
    log.info("=" * 60)


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?",
                    help="override the input workbook; requires a single --source")
    ap.add_argument("--source", choices=[*SPECS, "all"], default="all",
                    help="which source to load (default: all)")
    ap.add_argument("--dry-run", action="store_true", help="parse and report, write nothing")
    args = ap.parse_args()

    specs = list(SPECS.values()) if args.source == "all" else [SPECS[args.source]]

    if args.file and len(specs) > 1:
        sys.exit("Error: a file argument needs --source basic or --source assay - "
                 "the two sources read different sheets and cannot share one file.")

    paths = [Path(args.file) if args.file else Path(spec.default_file) for spec in specs]
    for path in paths:
        if not path.is_file():
            sys.exit(f"Error: file not found: {path}")

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        sys.exit("Error: DATABASE_URL not set (checked .env).")

    conn = psycopg2.connect(db_url)
    try:
        # One transaction for the whole run: a half-loaded pair of sources is
        # harder to reason about than none at all.
        with conn.cursor() as cur:
            results = [(spec, load_source(cur, spec, path))
                       for spec, path in zip(specs, paths)]

            for spec, st in results:
                report(spec, st)
            if len(results) > 1:
                log.info("total: %d crude oils, %d property values",
                         sum(st.n_oils for _, st in results),
                         sum(st.n_props for _, st in results))

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
