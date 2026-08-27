#!/usr/bin/env python3
"""
Load the relative vapour density table into cargo_gas +
cargo_gas_property_values.

SOURCE
------
"VAPDENS.XLSX" (source.json, category 'gas'). This
is the FIRST gas source: cargo_gas and cargo_gas_property_values were created by
20260818000000_gas_master_and_properties and renamed from gas /
gas_property_values by 20260827100000_rename_gas_to_cargo_gas, but until now no
loader had written a row to either.

INPUT
-----
"VAPDENS.xlsx - TABLE.csv": 31 gases in two columns.

    Gases                     RELATIVE VAPOUR DENSITIES
    AIR                       1.00
    NITROGEN                  0.97
    ACETALDEHYDE *            1.52
    ...

WHAT THE OTHER COLUMNS ARE
--------------------------
Columns 4 and 7 are not data. They hold a legend, spread across the first four
gas rows purely because that is where it fitted on the page:

    row 3  UNIT  -  KG / CUB.M
    row 4  AT STANDARD CONDITIONS  -  0 DEG C;
    row 5  1,033 BARS
    row 6  * AT 20 DEG C

Read as a row those cells would say AIR is measured in KG/CUB.M and NITROGEN at
standard conditions, which is not what the page means - they apply to the whole
table. The loader collects them as the legend they are and records them on every
value row, never as per-gas data.

DIMENSIONLESS
-------------
The values are RELATIVE to air, which the table itself lists as 1.00, so `unit`
is NULL. The 'KG / CUB.M' legend describes the densities the ratio is computed
from; storing it as the unit of a ratio would be wrong, so it lives in notes.

THE ASTERISK
------------
'ACETALDEHYDE *' carries a footnote marker, not a name. The marker is stripped
from gas_name - a cargo is not called "ACETALDEHYDE *" - and what it means is
recorded on that gas's value row: measured at 20 DEG C rather than the table's
standard 0 DEG C. Dropping the marker without recording it would silently claim
the figure was measured on the same basis as the rest.

AIR, NITROGEN AND INERT GAS
---------------------------
Loaded like everything else. They are reference baselines rather than cargoes -
AIR is the 1.00 the other figures are relative to - but they are rows in the
source's own table, and deciding which of a source's rows are "real" is not this
loader's call. Their `notes` says what they are.

IDEMPOTENCY
-----------
Upsert on (gas_name, source_id) and (cargo_gas_id, source_id, field_name).
The whole file is validated before anything is written; one transaction.

Usage:
    python3 etl/gas/vapour_density.py
    python3 etl/gas/vapour_density.py --dry-run
    python3 etl/gas/vapour_density.py "/path/to/VAPDENS.xlsx - TABLE.csv"
"""

import argparse
import csv
import logging
import os
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import psycopg2
from dotenv import load_dotenv

# Loaders are run as scripts (python3 etl/<branch>/<file>.py), so only their
# own directory is on sys.path. Add the shared helpers.
_ETL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ETL_ROOT / "common"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _gas import (clean_text, ensure_field_definitions, upsert_gas,  # noqa: E402
                  upsert_property)
from _paths import input_file  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("gas_vapour_density")

SOURCE_NAME = "VAPDENS.XLSX"
FIELD_NAME = "RELATIVE_VAPOUR_DENSITY"
ENTERED_BY = "vapour_density.py"
DEFAULT_FILE = input_file("VAPDENS.xlsx - TABLE.csv")

HEADER_NAME = "Gases"
HEADER_VALUE = "RELATIVE VAPOUR DENSITIES"

# Rows that are a measuring stick rather than a cargo. Loaded either way; this
# only decides what their notes say.
REFERENCE_GASES = {"AIR", "NITROGEN", "INERT GAS"}


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


def read_table(path: Path) -> Tuple[List[dict], str, List[str]]:
    """Parse the CSV into gas rows plus the table-wide legend."""
    with path.open(newline="", encoding="utf-8-sig") as fh:
        grid = [row for row in csv.reader(fh)]
    errors: List[str] = []
    if not grid:
        return [], "", [f"{path.name} is empty"]

    header = [clean_text(c) for c in grid[0][:2]]
    if header[0] != HEADER_NAME or header[1] != HEADER_VALUE:
        errors.append(f"row 1 is {header!r}, expected [{HEADER_NAME!r}, "
                      f"{HEADER_VALUE!r}] - is this the VAPDENS table?")

    rows: List[dict] = []
    legend_parts: List[str] = []
    footnote: Optional[str] = None
    seen: set = set()

    for i, raw in enumerate(grid[1:], start=2):
        # Columns beyond the second are the page legend, not this row's data.
        for cell in raw[2:]:
            text = clean_text(cell)
            if text is None:
                continue
            if text.startswith("*"):
                footnote = text.lstrip("* ").strip()
            else:
                legend_parts.append(text)

        name = clean_text(raw[0] if raw else None)
        value = clean_text(raw[1] if len(raw) > 1 else None)
        if name is None and value is None:
            continue
        if name is None:
            errors.append(f"row {i}: a density with no gas name")
            continue
        if value is None:
            errors.append(f"row {i}: gas {name!r} has no density")
            continue

        # The asterisk is a footnote marker, not part of the name.
        starred = name.endswith("*")
        if starred:
            name = name.rstrip("* ").strip()

        if name in seen:
            errors.append(f"row {i}: duplicate gas {name!r}")
            continue
        seen.add(name)

        try:
            density = float(value.replace(",", ""))
        except ValueError:
            errors.append(f"row {i}: gas {name!r} has density {value!r}, "
                          f"which is not a number")
            continue

        rows.append({"gas_name": name, "value": value, "density": density,
                     "starred": starred, "line": i})

    legend = " ".join(legend_parts)
    if footnote:
        for r in rows:
            if r["starred"]:
                r["footnote"] = footnote
    unmarked = [r["gas_name"] for r in rows if r["starred"] and not r.get("footnote")]
    if unmarked:
        errors.append(f"gas(es) {unmarked} carry a footnote marker but the file "
                      f"defines no footnote text")
    return rows, legend, errors


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", default=str(DEFAULT_FILE))
    ap.add_argument("--dry-run", action="store_true", help="parse and report, write nothing")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.is_file():
        sys.exit(f"Error: file not found: {path}")

    rows, legend, errors = read_table(path)
    if errors:
        log.error("%d validation error(s) - nothing was imported:", len(errors))
        for e in errors:
            log.error("    %s", e)
        return 1

    log.info("%s: %d gas(es)", path.name, len(rows))
    log.info("legend (applies to the whole table): %s", legend or "(none)")
    starred = [r["gas_name"] for r in rows if r["starred"]]
    if starred:
        log.info("footnoted gas(es): %s -> %s", ", ".join(starred),
                 rows[[r["gas_name"] for r in rows].index(starred[0])].get("footnote"))
    present_refs = sorted(r["gas_name"] for r in rows if r["gas_name"] in REFERENCE_GASES)
    if present_refs:
        log.info("reference baselines (loaded, flagged in notes): %s",
                 ", ".join(present_refs))

    if args.dry_run:
        for r in rows:
            log.info("  %-28s %s", r["gas_name"], r["value"])
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

            created = 0
            for r in rows:
                gas_id, is_new = upsert_gas(cur, source_id, r["gas_name"])
                created += is_new

                note = f"Measurement basis: {legend}." if legend else None
                if r.get("footnote"):
                    note = ((note + " ") if note else "") + \
                        (f"This gas is footnoted in the source: {r['footnote']} - "
                         f"so it does NOT share the table's standard conditions.")
                if r["gas_name"] in REFERENCE_GASES:
                    note = ((note + " ") if note else "") + \
                        ("Reference baseline in the source's table rather than a "
                         "cargo; AIR = 1.00 is what the other figures are relative to.")

                upsert_property(
                    cur, gas_id, source_id, FIELD_NAME,
                    value=r["value"], normalized_value=r["density"],
                    unit=None,           # a ratio: see DIMENSIONLESS
                    value_type="number", entered_by=ENTERED_BY, notes=note,
                )

        conn.commit()
        log.info("✓ Committed. cargo_gas: %d (%d created this run) | "
                 "cargo_gas_property_values (%s): %d",
                 len(rows), created, FIELD_NAME, len(rows))
        return 0
    except Exception:
        conn.rollback()
        log.exception("Import failed - rolled back, nothing written.")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
