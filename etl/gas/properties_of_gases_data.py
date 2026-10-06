#!/usr/bin/env python3
"""
Load "Properties of Gases Doc Data.xlsx" into cargo_gas + cargo_gas_property_values.

SOURCE
------
"Properties of Gases Doc" (source.json, category 'gas', source id 33) - the same
source as properties_of_gases.py, whose CSV is the uncleaned form of this table.
This workbook is the cleaned extract: 33 rows instead of 15 gases, the same 11
columns, plus three new ones:

    Liquid to Gas Expansion Ratio (15°C and 1 bar) -> liquid_to_gas_expansion_ratio
    Liquid volume in ml of ideal gas (60°F, 760 mmHg) -> liquid_volume_per_ideal_gas_60f
    Specific gravity 60/60°F (vac.)                -> specific_gravity_60_60f

The first 11 columns reuse the CSV loader's COLUMNS (same field, unit and note),
so the same measurement lands in the same field whichever file it came from.

EXISTING ROWS ARE NOT OVERWRITTEN
---------------------------------
The CSV loader's rows carry the footer's references (GPSA, Chemiekaarten, ACGIH)
and legend wording this sheet no longer has. Where (gas, source, field) already
holds the same figure it is left alone; where it holds a DIFFERENT figure it is
left alone and reported. Only missing values are written.

The sheet lists Isoprene twice (rows 18 and 34). They are merged: the first
figure wins per field, and a disagreement (molecular mass 68.119 vs 68.12) is
reported, not resolved.

Usage:
    python3 etl/gas/properties_of_gases_data.py
    python3 etl/gas/properties_of_gases_data.py --dry-run
"""

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

import openpyxl
import psycopg2
from dotenv import load_dotenv

_ETL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ETL_ROOT / "common"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _gas import ensure_field_definitions, upsert_gas, upsert_property  # noqa: E402
from _paths import input_file  # noqa: E402
from properties_of_gases import (COLUMNS, CRITICAL_NOTE, IMPOSSIBLE_NOTE,  # noqa: E402
                                 SOURCE_NAME, collapse)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("properties_of_gases_data")

DEFAULT_FILE = input_file("Properties of Gases Doc Data.xlsx")
ENTERED_BY = "properties_of_gases_data_loader"

# Header (collapsed) -> (field, unit, note). The first 11 come from COLUMNS.
NEW_COLUMNS = [
    ("Liquid to Gas Expansion Ratio (15°C And 1 Bar)", "liquid_to_gas_expansion_ratio",
     None, "At 15°C and 1 bar, per the column heading. DIMENSIONLESS."),
    ("Liquid volume in ml of ideal gas At 60 °F and 760 mmHg",
     "liquid_volume_per_ideal_gas_60f", None,
     "Printed as 'Liquid volume in ml of ideal gas at 60 °F and 760 mmHg'; "
     "stored as printed, no further unit is given."),
    ("Specific gravity 60/60F (vac.)", "specific_gravity_60_60f", None,
     "Specific gravity 60/60°F (vac.) per the column heading; not the 15°C/15°C "
     "basis of `specific_gravity`. DIMENSIONLESS."),
]
# Sheet headings for the 11 shared columns, in COLUMNS order.
SHARED_HEADERS = [
    "Molecular Mass (g/mol)", "Atmospheric Boiling Point (°C)",
    "Critical Temperature (°C)", "Critical Pressure (kPa abs)",
    "Liquid Relative Density (15°C/15°C)", "Vapour Relative Density (Air=1)",
    "Flammable Min (% vol)", "Flammable Max (% vol)", "Odor Recognition (ppm)",
    "TLV (ppm)", "TLV (mg/m³)",
]
POSITIVE = {c.field for c in COLUMNS if c.positive} | {
    "liquid_to_gas_expansion_ratio", "liquid_volume_per_ideal_gas_60f",
    "specific_gravity_60_60f"}


def fmt(x: float) -> str:
    return repr(x) if isinstance(x, float) and x != int(x) else str(int(x)) if isinstance(x, float) else str(x)


def read_sheet(path: Path):
    ws = openpyxl.load_workbook(path, data_only=True).worksheets[0]
    grid = [list(r) for r in ws.iter_rows(values_only=True)]
    errors: List[str] = []
    header = [collapse(c) for c in grid[0]]
    spec = {}  # column index -> (field, unit, note, heading)
    want = [("Name", None)] + [(h, None) for h in SHARED_HEADERS]
    if header[:12] != [w[0] for w in want]:
        errors.append(f"header is {header[:12]!r}, expected {[w[0] for w in want]!r}")
        return {}, errors
    for i, col in enumerate(COLUMNS, start=1):
        spec[i] = (col.field, col.unit, col.note, header[i])
    for j, (heading, field, unit, note) in enumerate(NEW_COLUMNS, start=12):
        got = header[j] if j < len(header) else ""
        if got.replace("", "").replace(" ", "") != heading.replace(" ", ""):
            errors.append(f"column {j + 1} is {got!r}, expected {heading!r}")
        spec[j] = (field, unit, note, got)

    gases: Dict[str, Dict[str, dict]] = {}
    order: List[str] = []
    for ln, raw in enumerate(grid[1:], start=2):
        name = collapse(raw[0]) if raw[0] is not None else ""
        if not name:
            continue
        if name not in gases:
            gases[name] = {}
            order.append(name)
        else:
            log.info("row %d: %r listed again - merged into the first", ln, name)
        for idx, (field, unit, note, heading) in spec.items():
            val = raw[idx] if idx < len(raw) else None
            if val is None or collapse(str(val)) == "":
                continue
            if not isinstance(val, (int, float)):
                errors.append(f"row {ln}: {name!r} {heading!r} is {val!r}, not a number")
                continue
            prior = gases[name].get(field)
            if prior is not None:
                if prior["v"] != float(val):
                    log.warning("row %d: %r %s: %s vs %s already read - first kept",
                                ln, name, field, fmt(float(val)), fmt(prior["v"]))
                continue
            notes = [note] if note else []
            gases[name][field] = {"v": float(val), "unit": unit, "notes": notes,
                                  "impossible": field in POSITIVE and float(val) < 0}
        by = gases[name]
        if "critical_temperature_c" in by and "boiling_point_c" in by \
                and by["critical_temperature_c"]["v"] <= by["boiling_point_c"]["v"]:
            by["critical_temperature_c"]["impossible"] = True
            by["critical_temperature_c"]["notes"].append(
                CRITICAL_NOTE.format(bp=fmt(by["boiling_point_c"]["v"])))
    for name in order:
        for f, d in gases[name].items():
            if d["impossible"] and not any(IMPOSSIBLE_NOTE == n or "IMPOSSIBLE" in n
                                           for n in d["notes"]):
                d["notes"].append(IMPOSSIBLE_NOTE)
    return {n: gases[n] for n in order}, errors


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", default=str(DEFAULT_FILE))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    path = Path(args.file)
    if not path.is_file():
        sys.exit(f"Error: file not found: {path}")
    gases, errors = read_sheet(path)
    if errors:
        log.error("%d validation error(s) - nothing was imported:", len(errors))
        for e in errors:
            log.error("    %s", e)
        return 1
    log.info("%s: %d gas(es), %d value(s)", path.name, len(gases),
             sum(len(v) for v in gases.values()))

    load_dotenv(_ETL_ROOT.parent / ".env")
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM source WHERE name = %s", (SOURCE_NAME,))
            row = cur.fetchone()
            if row is None:
                sys.exit(f"Error: source {SOURCE_NAME!r} not found; run etl/common/source.py")
            source_id = row[0]
            log.info("Source id=%s (%r)", source_id, SOURCE_NAME)
            if not args.dry_run:
                fields = [f for (_, f, _, _) in NEW_COLUMNS]
                log.info("field_definitions: %d created",
                         ensure_field_definitions(cur, only=fields))
            written = kept = created = 0
            for name, values in gases.items():
                cur.execute("SELECT id FROM cargo_gas WHERE gas_name=%s AND source_id=%s",
                            (name, source_id))
                found = cur.fetchone()
                if found is None:
                    created += 1
                    gas_id = None if args.dry_run else upsert_gas(cur, source_id, name)[0]
                else:
                    gas_id = found[0]
                for field, d in values.items():
                    if gas_id is not None:
                        cur.execute("SELECT normalized_value FROM cargo_gas_property_values "
                                    "WHERE cargo_gas_id=%s AND source_id=%s AND field_name=%s",
                                    (gas_id, source_id, field))
                        have = cur.fetchone()
                        if have is not None:
                            kept += 1
                            if have[0] is None or abs(float(have[0]) - d["v"]) > 1e-9:
                                log.warning("%s %s: database has %s, sheet has %s - left as is",
                                            name, field, have[0], fmt(d["v"]))
                            continue
                    written += 1
                    if args.dry_run:
                        continue
                    upsert_property(
                        cur, gas_id, source_id, field, value=fmt(d["v"]),
                        normalized_value=d["v"], unit=d["unit"], value_type="number",
                        entered_by=ENTERED_BY, notes=" ".join(d["notes"]) or None,
                        is_winning=not d["impossible"], conflict_flag=d["impossible"])
        if args.dry_run:
            conn.rollback()
            log.info("--dry-run: %d new gas(es), %d value(s) would be written, %d already "
                     "present. Nothing written.", created, written, kept)
            return 0
        conn.commit()
        log.info("✓ Committed. cargo_gas created: %d | cargo_gas_property_values written: %d "
                 "(%d already present, untouched)", created, written, kept)
        return 0
    except Exception:
        conn.rollback()
        log.exception("Import failed - rolled back, nothing written.")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
