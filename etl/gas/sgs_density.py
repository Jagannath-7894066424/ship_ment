#!/usr/bin/env python3
"""
Load the SGS density tables into cargo_gas and cargo_gas_thermodynamic_property.

SOURCE
------
"Cargo Library SGS Density Table" (source.json, category 'gas'). An SGS cargo
inspector's density book: one sheet per gas, each a table down the saturation
line giving the vapour pressure and the two phase densities at closely spaced
temperatures. These are the figures a surveyor reads to turn a measured tank
volume into a weight at custody transfer, which is why the steps are fine
(0.5 C, and 0.1 C for ethylene) and the range is the carriage range rather than
the whole liquid range.

WHAT IT ADDS OVER THE THERMODYNAMIC WORKBOOK
--------------------------------------------
It is the SECOND source of saturated data for cargo_gas, and it disagrees with
the first in resolution and in purpose, not in kind. Both land in
cargo_gas_thermodynamic_property with property_type = SATURATED; they are kept
apart by source_id, and cargo_gas is keyed (gas_name, source_id), so each source
keeps its own gas rows and its own figures. Nothing here overwrites the
workbook.

Only three of the workbook's eight series are present - vapour pressure and the
two densities. There is no specific volume, no enthalpy and no latent heat, so
no enthalpy datum is needed and none is stated.

THE SHEETS PRINT NO UNITS, SO THE UNITS ARE PROVEN FROM THE DATA
-----------------------------------------------------------------
Every column is headed by a bare name - "Vap Density vac", "Liq Density vac" -
with no unit anywhere on any sheet. They are NOT the same unit, and guessing
would put a figure off by a factor of 1000 into the database:

  * Vapour density is kg/m3. Every sheet's first row agrees with the ideal gas
    PM/RT to a few percent, in the direction a real gas deviates. In kg/L the
    butene vapour at 0.177 bar would be 450 kg/m3, denser than its own liquid.

  * Liquid density is kg/L. The butene sheet prints the liquid density twice,
    in vacuum and in air, and the difference is 0.0011 on all 152 rows. That
    difference is the buoyancy correction, i.e. the density of air, and air is
    0.0012 kg/L - a figure that is only dimensionally consistent if the column
    itself is kg/L. In kg/m3 the correction would have to be 1.2.

So the two density columns of one row carry different units. They are stored
with the unit each was proven to be, and the proof is recorded in the notes.

DENSITY IN VACUUM AND DENSITY IN AIR ARE TWO DIFFERENT PROPERTIES
-------------------------------------------------------------------
"Liq Density vac" is the true density; "Liq Density air" is what the same
liquid weighs against brass in air, which is the number that settles a bill of
lading. They are stored as two property_names against the same phase and
temperature rather than as one property with a qualifier, because a reader
asking for "the" density has to be told which one they are getting.

TEMP F AND DRUK ABS PSI ARE STORED AS PRINTED
---------------------------------------------
The butene sheet also prints Temp F and Druk abs PSI. They are unit conversions
of columns already read - F is C*9/5+32 on all 152 rows, and PSI is bar*14.5038
to within rounding on all but 10, which differ by 0.02 - but a reader asking
butene for its temperature in Fahrenheit or its pressure in psi should find the
sheet's own figure, so both are stored: one SATURATED row per temperature with
property_name 'temperature_fahrenheit' (unit °F) and 'vapour_pressure_psi'
(unit psi (absolute)), each exactly as the sheet prints it and not recomputed.
The row's `temperature` column stays the Celsius key; the notes say so.

THE SHEETS ARE PAGINATED, AND THE PAGE BREAKS REPEAT A ROW
------------------------------------------------------------
Each sheet is a printed table broken into pages of about 52 rows. At every
break the header row is reprinted, and the last temperature of the page is
reprinted as the first temperature of the next - butene prints 0 C twice and
25 C twice. A reprinted row is not a second measurement, so it is loaded once.
The loader checks that the repeats agree before collapsing them; a page overlap
whose figures DIFFER would be two claims about one state, and that is an error
rather than something to silently pick a winner from.

'butene density' IS 'butene density table' WITH COLUMNS REMOVED
-----------------------------------------------------------------
The workbook holds seven sheets for six gases. 'butene density table' and
'butene density ' carry the same 152 temperatures and the same figures; the
second simply omits Temp F, PSI and the density in air. Loading both would
double-count butene, so only the fuller sheet is read, and the loader verifies
the two agree before ignoring the shorter one rather than assuming it.

EVERY SHEET CITES ITS OWN LITERATURE, AND THEY ARE NOT THE SAME
------------------------------------------------------------------
Four sheets end with a "ref:" line naming where the numbers came from - the
IUPAC international thermodynamic tables for ethylene and propylene, VDI
Forschungsheft 596 plus a Redlich-Kwong vapour calculation for ammonia, and a
1958 British Chemical Engineering paper for vinyl chloride. That is the real
provenance of the figures; SGS compiled them. It is carried onto every row of
the sheet that states it, because a reader comparing two gases here is
comparing two different pieces of literature.

Butene and butadiene state no reference, and no reference is invented for them.

PHYSICS IS CHECKED ALONG THE SATURATION LINE
----------------------------------------------
Heat a liquid under its own vapour and the pressure rises, the vapour gets
denser and the liquid gets less dense. All three series are checked, and a
reading that turns back on itself is flagged in `notes` and reported - stored
exactly as printed, never corrected.

IDEMPOTENCY
-----------
Upsert on (gas_name, source_id) and on the SATURATED partial unique index
(cargo_gas_id, source_id, temperature, phase, property_name). Every sheet is
validated before anything is written; one transaction for the whole run.

Usage:
    python3 etl/gas/sgs_density.py
    python3 etl/gas/sgs_density.py --dry-run
    python3 etl/gas/sgs_density.py --sheet "ammonia anhydrous"
    python3 etl/gas/sgs_density.py --list-sheets
"""

import argparse
import logging
import math
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import psycopg2
from dotenv import load_dotenv

# Loaders are run as scripts (python3 etl/<branch>/<file>.py), so only their
# own directory is on sys.path. Add the shared helpers.
_ETL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ETL_ROOT / "common"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _gas import upsert_gas  # noqa: E402
from _paths import input_file  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("gas_sgs_density")

SOURCE_NAME = "Cargo Library SGS Density Table"
DEFAULT_FILE = input_file("Cargo Library  SGS Density Table.xlsx")

# sheet -> the gas it is about. The sheet names carry a "density" suffix that
# is sheet naming rather than part of any cargo's name.
#
# 'butene density ' is deliberately absent: it is a column-reduced copy of
# 'butene density table' and is verified against it instead of being loaded.
SHEETS = {
    "butene density table":   "Butene",
    "ammonia anhydrous":      "Ammonia (anhydrous)",
    "butadiene density":      "Butadiene",
    "ethylene ":              "Ethylene",
    "propylene density":      "Propylene",
    "vinyl chloride monomer": "Vinyl Chloride Monomer",
}
DUPLICATE_SHEET = "butene density "
DUPLICATE_OF = "butene density table"

# Header keyword -> (property_name, phase, unit). Matched on the header cell
# with case and whitespace normalised, never by column index: the wide butene
# sheet puts the vapour density in column 4 and the others in column 2.
#
# "vap"/"liq" are tested before "density" can claim the cell, and the pressure
# keyword before either - "Druk P abs" is Dutch for absolute pressure.
COLUMNS = [
    ("temp",        None),
    ("druk",        ("vapour_pressure", "NONE",   "bar (absolute)")),
    ("vap density", ("density",         "VAPOUR", "kg/m³")),
    ("liq density", ("density",         "LIQUID", "kg/L")),
]

# Distinguishes the butene sheet's two liquid columns. Anything else is the
# true (vacuum) density; only a column that says "air" is the apparent one.
AIR_PROPERTY = "density_in_air"

# Columns the sheet prints in imperial units beside the metric ones. Stored as
# printed (not recomputed), keyed by the normalised header text.
IMPERIAL = {
    "temp °f":      ("temperature_fahrenheit", "NONE", "°F"),
    "druk abs psi": ("vapour_pressure_psi",    "NONE", "psi (absolute)"),
}
IMPERIAL_NOTE = {
    "temperature_fahrenheit": (
        "The sheet's own Temp °F column, stored as printed. It is the same "
        "temperature as this row's `temperature` (°C), which remains the key."),
    "vapour_pressure_psi": (
        "The sheet's own 'Druk abs PSI' column, stored as printed: the "
        "saturation pressure in psi absolute, the same state as this "
        "temperature's vapour_pressure in bar. It is the sheet's figure and "
        "is not recomputed from bar."),
}

UNIT_NOTE = {
    "kg/m³": (
        "The sheet states no unit for this column. It is kg/m³: the figure "
        "agrees with the ideal-gas density PM/RT at this temperature and "
        "pressure to within a few percent, in the direction a real vapour "
        "deviates. Read as kg/L it would make the vapour denser than the "
        "liquid in the same row. The number is unchanged."),
    "kg/L": (
        "The sheet states no unit for this column. It is kg/L: the butene "
        "sheet prints this density both in vacuum and in air, and the "
        "difference is 0.0011 on every row. That difference is the air "
        "buoyancy correction, and air is 0.0012 kg/L - a correction only "
        "consistent with a column already in kg/L. The number is unchanged."),
}

AIR_NOTE = (
    "This is the density weighed IN AIR, not the true density: the same "
    "liquid's in-vacuum density is stored at this temperature as property "
    "'density'. The two differ by the buoyancy of the air the liquid "
    "displaces. In-air density is the figure a bill of lading is settled on; "
    "in-vacuum is the physical property.")

PRESSURE_NOTE = (
    "The saturation pressure AT this temperature - a reading, not a state the "
    "table was measured at, which is why the row's `pressure` column is NULL "
    "and this is stored as a property.")

# Along the saturation line these can only run one way, as a matter of physics
# rather than of this data. Non-strict: the tables round, and equal neighbours
# are common at the fine steps these sheets use.
MONOTONIC = [
    ("vapour_pressure", "NONE",   "up"),
    ("density",         "VAPOUR", "up"),
    ("density",         "LIQUID", "down"),
    (AIR_PROPERTY,      "LIQUID", "down"),
]


def cell(value) -> str:
    """Trim a cell and collapse its whitespace; NaN becomes ''."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return re.sub(r"\s+", " ", str(value).replace("\n", " ")).strip()


def number(value) -> Optional[float]:
    """A cell as a number, or None. NaN is not a number here."""
    try:
        out = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) else out


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text).replace("\n", " ").lower()).strip()


def map_columns(header: List[str],
                errors: List[str]) -> Tuple[Optional[int], Dict[int, tuple]]:
    """Locate each series by READING the header row.

    Returns (temperature column, {column -> (property_name, phase, unit)}).
    The wide butene sheet and the six narrow ones put the same series in
    different columns, so nothing is read by index.
    """
    temp_col: Optional[int] = None
    series: Dict[int, tuple] = {}
    for col, raw in enumerate(header):
        low = norm(raw)
        if not low:
            continue
        if low in IMPERIAL:
            series[col] = IMPERIAL[low]
            continue
        for keyword, spec in COLUMNS:
            if keyword not in low:
                continue
            if spec is None:
                if temp_col is None:
                    temp_col = col
                break
            name, phase, unit = spec
            if phase == "LIQUID" and "air" in low:
                name = AIR_PROPERTY
            series[col] = (name, phase, unit)
            break

    if temp_col is None:
        errors.append(f"no temperature column in the header {header!r}")
    wanted = {("vapour_pressure", "NONE"), ("density", "VAPOUR"),
              ("density", "LIQUID")}
    got = {(n, p) for n, p, _ in series.values()}
    missing = wanted - got
    if missing:
        errors.append(f"the header does not describe {sorted(missing)}; "
                      f"it reads {header!r}")
    return temp_col, series


def read_sheet(path: Path, sheet: str, gas_name: str) -> Tuple[dict, List[str]]:
    """Parse one gas sheet. Returns (parsed, errors); nothing is written unless
    errors is empty."""
    grid = [[c for c in row] for row in
            pd.read_excel(path, sheet_name=sheet, header=None).values.tolist()]
    errors: List[str] = []
    if not grid:
        return {}, [f"sheet {sheet!r} is empty"]

    header = [cell(c) for c in grid[0]]
    temp_col, series = map_columns(header, errors)
    if errors:
        return {}, errors

    header_norm = [norm(c) for c in header]
    rows: List[dict] = []
    seen: Dict[float, dict] = {}
    repeated: List[float] = []
    reprinted_headers = 0
    reference = ""

    for i in range(1, len(grid)):
        raw_row = grid[i]
        temp = number(raw_row[temp_col]) if temp_col < len(raw_row) else None
        if temp is None:
            texts = [cell(c) for c in raw_row]
            if [norm(t) for t in texts[:len(header_norm)]] == header_norm:
                reprinted_headers += 1      # a page break, not data
                continue
            joined = " ".join(t for t in texts if t)
            if joined:
                # The trailing "ref:" block, which may run over several rows.
                reference = (reference + " " + joined).strip() if reference else joined
            continue

        values = []
        for col, (name, phase, unit) in sorted(series.items()):
            value = number(raw_row[col]) if col < len(raw_row) else None
            if value is None:
                errors.append(f"{sheet!r} row {i + 1} ({temp:g}°C): {name}"
                              f"/{phase.lower()} is "
                              f"{cell(raw_row[col]) if col < len(raw_row) else ''!r}, "
                              f"which is not a number")
                continue
            values.append({"property_name": name, "phase": phase, "unit": unit,
                           "value": value, "raw": cell(raw_row[col])})

        record = {"temperature": temp, "values": values}
        if temp in seen:
            # A page break reprints its last row as the next page's first.
            before = {(v["property_name"], v["phase"]): v["value"]
                      for v in seen[temp]["values"]}
            now = {(v["property_name"], v["phase"]): v["value"] for v in values}
            if before != now:
                errors.append(
                    f"{sheet!r}: {temp:g}°C is printed twice with DIFFERENT "
                    f"figures ({before} then {now}). A page break reprints its "
                    f"last row, so the two should agree; two different claims "
                    f"about one state cannot both be loaded")
            repeated.append(temp)
            continue
        seen[temp] = record
        rows.append(record)

    if not rows:
        errors.append(f"sheet {sheet!r} has no data rows")
        return {}, errors

    reference = re.sub(r"^ref\s*:\s*", "", reference, flags=re.IGNORECASE).strip()
    for name, phase, direction in MONOTONIC:
        line = [(r["temperature"], v) for r in rows for v in r["values"]
                if v["property_name"] == name and v["phase"] == phase]
        flag_monotonic(line, direction, name.replace("_", " "),
                       "of the " + phase.lower() if phase != "NONE" else "")

    derived = []
    return ({"sheet": sheet, "gas_name": gas_name, "rows": rows,
             "series": series, "reference": reference, "repeated": repeated,
             "reprinted_headers": reprinted_headers, "derived": derived}, errors)


def flag_monotonic(series: List[Tuple[float, dict]], direction: str,
                   what: str, whose: str) -> None:
    """Flag a reading that breaks a run physics does not allow to break.

    Where four consecutive readings identify WHICH of two is the outlier, the
    flag goes on the one further off the line through its neighbours, rather
    than on the innocent reading after the break.
    """
    rising = direction == "up"
    for i in range(1, len(series)):
        (x_prev, prev), (x, cur) = series[i - 1], series[i]
        broken = cur["value"] < prev["value"] if rising else cur["value"] > prev["value"]
        if not broken:
            continue
        culprit, at, other, other_at = cur, x, prev, x_prev
        # Never reason from a reading already called a misprint. Ethylene's
        # vapour density is disturbed over several consecutive rows near -96°C,
        # and using the bad 3.0 as the anchor for the next decision moved the
        # blame onto 3.157, which is the one value in that stretch sitting
        # exactly on the trend. With a corrupt anchor the shape carries no
        # information, so the break itself is flagged instead.
        if i >= 2 and not series[i - 2][1].get("anomaly"):
            x_before, before = series[i - 2]
            spike = ((before["value"] < prev["value"] and cur["value"] > before["value"])
                     if rising else
                     (before["value"] > prev["value"] and cur["value"] < before["value"]))
            if spike and i + 1 < len(series):
                x_next, nxt = series[i + 1]
                span = x_next - x_before
                if span:
                    slope = (nxt["value"] - before["value"]) / span
                    off_prev = abs(prev["value"]
                                   - (before["value"] + slope * (x_prev - x_before)))
                    off_cur = abs(cur["value"]
                                  - (before["value"] + slope * (x - x_before)))
                    spike = off_prev >= off_cur
            if spike:
                culprit, at, other, other_at = prev, x_prev, before, x_before
        way = "rise" if rising else "fall"
        culprit["anomaly"] = (
            f"BREAKS A PHYSICAL RUN: along the saturation line {what} {whose} "
            f"can only {way} with temperature, but the sheet prints "
            f"{other['raw']} at {other_at:g}°C and {culprit['raw']} at "
            f"{at:g}°C. This reading is the one out of line with its "
            f"neighbours on both sides. Stored as printed and NOT corrected.").replace("  ", " ")


def check_duplicate_sheet(path: Path, errors: List[str]) -> Optional[int]:
    """Verify 'butene density ' really is a column-reduced copy before ignoring it.

    Returns the number of temperatures compared, or None if the sheet is absent.
    """
    names = pd.ExcelFile(path).sheet_names
    if DUPLICATE_SHEET not in names or DUPLICATE_OF not in names:
        return None
    full = pd.read_excel(path, sheet_name=DUPLICATE_OF, header=None).values.tolist()
    short = pd.read_excel(path, sheet_name=DUPLICATE_SHEET, header=None).values.tolist()

    def by_temp(grid, cols):
        out = {}
        for row in grid[1:]:
            temp = number(row[0])
            if temp is None:
                continue
            out[temp] = tuple(number(row[c]) if c < len(row) else None for c in cols)
        return out

    # full: temp, bar, vap density, liq density vac | short: temp, bar, vap, liq
    a, b = by_temp(full, (2, 4, 5)), by_temp(short, (1, 2, 3))
    if a.keys() != b.keys():
        errors.append(f"{DUPLICATE_SHEET!r} covers temperatures "
                      f"{sorted(b.keys() - a.keys())[:5]} that "
                      f"{DUPLICATE_OF!r} does not, so it is not a copy of it "
                      f"and cannot be skipped")
        return None
    differing = [t for t in a if a[t] != b[t]]
    if differing:
        errors.append(f"{DUPLICATE_SHEET!r} disagrees with {DUPLICATE_OF!r} at "
                      f"{differing[:5]}, so it is not a copy of it and cannot "
                      f"be skipped")
    return len(a)


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


def upsert_thermo(cur, cargo_gas_id: int, source_id: int, temperature: float,
                  phase: str, property_name: str, value: float, raw_value: str,
                  unit: str, notes: Optional[str], page_ref: str) -> None:
    """Insert or refresh one saturated reading.

    ON CONFLICT names the PARTIAL unique index by repeating its predicate -
    PostgreSQL infers a partial index only when the statement carries the same
    WHERE clause.
    """
    cur.execute(
        """
        INSERT INTO cargo_gas_thermodynamic_property
            (cargo_gas_id, source_id, property_type, temperature,
             temperature_unit, pressure, pressure_unit, phase, property_name,
             value, raw_value, unit, source_page_ref, notes,
             created_at, updated_at)
        VALUES (%s, %s, 'SATURATED', %s, '°C', NULL, NULL, %s, %s, %s, %s, %s,
                %s, %s, now(), now())
        ON CONFLICT (cargo_gas_id, source_id, temperature, phase, property_name)
            WHERE property_type = 'SATURATED'
        DO UPDATE SET value           = EXCLUDED.value,
                      raw_value       = EXCLUDED.raw_value,
                      unit            = EXCLUDED.unit,
                      source_page_ref = EXCLUDED.source_page_ref,
                      notes           = EXCLUDED.notes,
                      updated_at      = now()
        """,
        (cargo_gas_id, source_id, temperature, phase, property_name, value,
         raw_value, unit, page_ref, notes),
    )


def report(parsed: dict) -> None:
    rows = parsed["rows"]
    temps = [r["temperature"] for r in rows]
    steps: Dict[float, int] = {}
    for a, b in zip(temps, temps[1:]):
        step = round(b - a, 4)
        steps[step] = steps.get(step, 0) + 1
    log.info("sheet %r -> gas %r", parsed["sheet"], parsed["gas_name"])
    log.info("    %d temperature(s) %g..%g°C, steps %s", len(rows), temps[0],
             temps[-1], ", ".join(f"{k:g}°C x{v}" for k, v in sorted(steps.items())))
    log.info("    series read from the header: %s",
             ", ".join(f"{n}{'/' + p.lower() if p != 'NONE' else ''} [{u}] =col{c}"
                       for c, (n, p, u) in sorted(parsed["series"].items())))
    log.info("    rows written: %d", sum(len(r["values"]) for r in rows))
    if parsed["reprinted_headers"]:
        log.info("    page breaks skipped: %d reprinted header row(s); "
                 "%d reprinted temperature(s) %s loaded once",
                 parsed["reprinted_headers"], len(parsed["repeated"]),
                 ", ".join(f"{t:g}°C" for t in parsed["repeated"]))
    if parsed["derived"]:
        log.info("    column(s) NOT stored, being exact conversions of a column "
                 "already read: %s", ", ".join(parsed["derived"]))
    if parsed["reference"]:
        log.info("    source's own reference, recorded on every row: %s",
                 parsed["reference"][:150])
    else:
        log.info("    the sheet states no literature reference (none invented)")

    anomalies = [(r["temperature"], v) for r in rows for v in r["values"]
                 if v.get("anomaly")]
    if anomalies:
        log.warning("    %d figure(s) that break a physical run - loaded as "
                    "printed, flagged in notes, NOT corrected:", len(anomalies))
        for temp, v in anomalies:
            log.warning("        %8.1f°C  %-16s %s",
                        temp, f"{v['property_name']}/{v['phase'].lower()}", v["raw"])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", default=str(DEFAULT_FILE))
    ap.add_argument("--sheet", default=None,
                    help="load only this sheet (default: all six)")
    ap.add_argument("--list-sheets", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="parse and report, write nothing")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.is_file():
        sys.exit(f"Error: file not found: {path}")

    if args.list_sheets:
        for name in pd.ExcelFile(path).sheet_names:
            log.info("  %-26s %s", name,
                     SHEETS.get(name, "(not loaded)" if name != DUPLICATE_SHEET
                                else f"(copy of {DUPLICATE_OF!r}, not loaded)"))
        return 0

    if args.sheet and args.sheet not in SHEETS:
        sys.exit(f"Error: {args.sheet!r} is not a loadable sheet. "
                 f"Known: {', '.join(map(repr, SHEETS))}")
    wanted = {args.sheet: SHEETS[args.sheet]} if args.sheet else SHEETS

    errors: List[str] = []
    compared = check_duplicate_sheet(path, errors)
    if compared:
        log.info("%r verified as a column-reduced copy of %r across %d "
                 "temperatures - not loaded separately",
                 DUPLICATE_SHEET, DUPLICATE_OF, compared)

    parsed_all = []
    for sheet, gas_name in wanted.items():
        parsed, sheet_errors = read_sheet(path, sheet, gas_name)
        errors.extend(sheet_errors)
        if parsed:
            parsed_all.append(parsed)

    if errors:
        log.error("%d validation error(s) - nothing was imported:", len(errors))
        for e in errors:
            log.error("    %s", e)
        return 1

    for parsed in parsed_all:
        report(parsed)

    if args.dry_run:
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

            gases = written = 0
            for parsed in parsed_all:
                page_ref = f"{path.name} [{parsed['sheet']}]"
                gas_id, created = upsert_gas(cur, source_id, parsed["gas_name"])
                gases += 1
                log.info("cargo_gas id=%s %r (%s)", gas_id, parsed["gas_name"],
                         "created" if created else "already present")
                for row in parsed["rows"]:
                    for v in row["values"]:
                        notes = [UNIT_NOTE[v["unit"]]] if v["unit"] in UNIT_NOTE else []
                        if v["property_name"] == AIR_PROPERTY:
                            notes.append(AIR_NOTE)
                        if v["property_name"] == "vapour_pressure":
                            notes.append(PRESSURE_NOTE)
                        if v["property_name"] in IMPERIAL_NOTE:
                            notes.append(IMPERIAL_NOTE[v["property_name"]])
                        if parsed["reference"]:
                            notes.append(f"The sheet's own reference for these "
                                         f"figures: {parsed['reference']}")
                        if v.get("anomaly"):
                            notes.append(v["anomaly"])
                        upsert_thermo(cur, gas_id, source_id, row["temperature"],
                                      v["phase"], v["property_name"], v["value"],
                                      v["raw"], v["unit"], " ".join(notes) or None,
                                      page_ref)
                        written += 1

        conn.commit()
        log.info("✓ Committed. cargo_gas: %d | "
                 "cargo_gas_thermodynamic_property: %d", gases, written)
        return 0
    except Exception:
        conn.rollback()
        log.exception("Import failed - rolled back, nothing written.")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
