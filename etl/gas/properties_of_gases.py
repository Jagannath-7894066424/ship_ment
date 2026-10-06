#!/usr/bin/env python3
"""
Load the "properties of commercial gases" table into cargo_gas +
cargo_gas_property_values.

SOURCE
------
"Properties of Gases Doc" (source.json, category 'gas'). The THIRD gas source,
after VAPDENS.XLSX and Products_info. It overlaps both by name - all three list
methane, propane, ammonia - and nothing here merges them: cargo_gas is keyed
(gas_name, source_id), so each source keeps its own rows and a disagreement
between them stays visible.

It is ranked first of the three for physical properties because it says where
its figures come from: the footer cites GPSA's SI Engineering Data Book for
columns marked (1) and Chemiekaarten for the flammable range, and this loader
carries those citations onto every value they cover.

INPUT
-----
"Properties of Gases Doc - properties of commercial gases.csv": 15 gases and a
TWO-ROW header, because three headings span more than one column:

    Name | Molecular Mass | Atm. Boiling Point | Critical Temp. | Critical
    Pressure | Liquid Relative Density | Vapour Relative Density |
    Flammable Range [Min | Max] | 100% Odor Recognition | Tlv [ppm | (mg/m³)]

Both header rows are matched exactly - if the sheet is re-exported with columns
in a different order this loader stops rather than loading a critical pressure
into a boiling point.

FIELDS
------
Eight of the eleven columns reuse fields the other branches already define, so
the same measurement lands in the same field whichever source it came from -
including RELATIVE_VAPOUR_DENSITY, which is the one thing VAPDENS.XLSX loads,
making the two directly comparable. Three are new (see etl/gas/_gas.py):
critical_temperature_c, critical_pressure_kpa and tlv_twa_mg_m3.

Two reuses are worth stating outright, because the column heading and the field
name are not word-for-word the same measurement, and the difference is written
onto every value:

  * "Liquid Relative Density 15°C/15°C" -> specific_gravity. Density relative
    to water with both at 15°C, which is what specific gravity means; the
    temperature basis is the part that varies by source, so it goes on the value.
  * "100% Odor Recognition (ppm)" -> odour_limit. NOT a detection threshold:
    it is the concentration at which the odour is recognised by everyone, which
    is a higher figure than the threshold another source might print in the same
    field. Every value says so.

WHAT THE FOOTER MEANS
---------------------
The rows under the table are a legend, and they change what the cells mean:

    Note  ( ) : estimated value
    n.p. : not possible, temperature above critical temperature
    n.d. = not determined-  = non toxic
    (1) Ref. GPSA, SI Engineering Data Book, 1980, section 16, fig. 16-1.
    (2) Ref. Chemiekaarten, ..., 1993/1994.
    (3) LGI, Liquid Gas Guide, 2nd edition, 1990.
    Ref. 1. 1994-1995 Threshold Limit Values ...; ACGIH.

So a cell is not always a number:

  * '-' means NON TOXIC - the source saying this product has no TLV, not a
    missing figure. It is stored as the legend's own words rather than as '-',
    because a bare dash reads as "no data" everywhere else in this database
    (see MISSING in _gas.py), which is the opposite of what the file says.
  * 'n.p.' means the product has no liquid density at 15°C because it is above
    its critical temperature there (ethene: critical temp 9.2°C). Also real
    information, also stored.
  * 'n.d.' means not determined. That IS missing, and is not written at all.
  * '(0.1 - 2)' is an estimated range: stored verbatim, normalized into
    normalized_min/max, and marked estimated in its notes.

The (1)/(2)/(3) markers are read from the header cells and from the product
name ("Vinylchloride (3)"), and each value carries the reference its column -
or, for vinyl chloride, its row - is marked with. The marker is stripped from
the name, since the cargo is not called "Vinylchloride (3)".

FIGURES THAT CANNOT BE MEASUREMENTS
-----------------------------------
Five cells carry a minus sign that no measurement can: methane's liquid
relative density (-0.3), and the critical pressure (-4502, -3850) and flammable
range (-2, -12) of 1,2-butadiene and isoprene. A negative absolute pressure or
density is not a surprising value, it is an impossible one - the original
document renders some figures with a stray minus. Two more are impossible on
the file's own evidence: 1,2-butadiene and isoprene are given a critical
temperature BELOW their boiling point (-171 vs 10.85, -211 vs 34.07), and a
substance's critical temperature is always above it.

They are loaded exactly as printed, flagged in `notes`, and listed in the run
log. This loader does not guess what the intended figure was - dropping the
minus would be inventing data - but neither does it pass an impossible number
off as a measurement.

IDEMPOTENCY
-----------
Upsert on (gas_name, source_id) and (cargo_gas_id, source_id, field_name). The
whole file is validated before anything is written; one transaction.

Usage:
    python3 etl/gas/properties_of_gases.py
    python3 etl/gas/properties_of_gases.py --dry-run
    python3 etl/gas/properties_of_gases.py "/path/to/...commercial gases.csv"
"""

import argparse
import csv
import logging
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

import psycopg2
from dotenv import load_dotenv

# Loaders are run as scripts (python3 etl/<branch>/<file>.py), so only their
# own directory is on sys.path. Add the shared helpers.
_ETL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ETL_ROOT / "common"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _gas import ensure_field_definitions, upsert_gas, upsert_property  # noqa: E402
from _paths import input_file  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("gas_properties_of_gases")

SOURCE_NAME = "Properties of Gases Doc"
ENTERED_BY = "properties_of_gases.py"
DEFAULT_FILE = input_file("Properties of Gases Doc - properties of commercial gases.csv")

NAME_HEADER = "Name"

# _gas.clean_text is deliberately NOT used for cells here: it maps '-' to None,
# and in this file '-' is a value ("non toxic"), not an empty cell.
def collapse(value: Optional[str]) -> str:
    """Trim a cell and collapse its whitespace. Header cells hold line breaks."""
    return re.sub(r"\s+", " ", (value or "").replace("\n", " ")).strip()


# Legend tokens, and what the file's own footer says they mean. Stored as the
# legend's words: see WHAT THE FOOTER MEANS in the module docstring.
NOT_DETERMINED = "n.d."
LEGEND_TOKENS: Dict[str, Tuple[str, str]] = {
    "-": ("non toxic",
          "The source prints '-' here. Its legend reads '- = non toxic', so this "
          "is the source stating the product has no TLV, not a missing figure; "
          "it is stored as the legend's words because a bare '-' reads as "
          "'no data' everywhere else in this database."),
    "n.p.": ("not possible",
             "The source prints 'n.p.' here. Its legend reads 'n.p. : not "
             "possible, temperature above critical temperature' - the product is "
             "above its critical temperature at the column's 15°C, so it has no "
             "liquid density to report. Not a missing figure."),
}

# Footer lines this loader relies on. If a re-export drops or rewords one, the
# cells above stop meaning what the code assumes, so it is a hard error.
REQUIRED_LEGEND = [
    ("( ) : estimated value", "( ) marks an estimated value"),
    ("not possible, temperature above critical", "n.p. means above the critical temperature"),
    ("not determined", "n.d. means not determined"),
    ("non toxic", "- means non toxic"),
]
# The footer line that says where the TLV figures come from. It names ACGIH's
# Threshold Limit Values outright, so it is attached to the two TLV columns.
TLV_REFERENCE_PREFIX = "Ref. 1."


class Column(NamedTuple):
    heading: str           # row 1 of the header, exactly as printed ('' if spanned)
    sub: str               # row 2 of the header, exactly as printed
    label: str             # what to call it in a log line
    field: str             # field_definitions.field_name
    unit: Optional[str]    # unit of the value as stored
    positive: bool         # a negative here is impossible, not merely unusual
    note: Optional[str]    # what the column heading says, recorded on every value


COLUMNS: List[Column] = [
    Column("Molecular Mass (G/Mole) (1)", "", "Molecular Mass",
           "molecular_weight_g_mol", "g/mol", True, None),
    Column("Atm. Boiling Point (°C) (1)", "", "Boiling Point",
           "boiling_point_c", "°C", False,
           "At atmospheric pressure, per the column heading."),
    Column("Critical Temp. (°C) (1)", "", "Critical Temp.",
           "critical_temperature_c", "°C", False, None),
    Column("Critical Pressure (Kpa,Abs) (1)", "", "Critical Pressure",
           "critical_pressure_kpa", "kPa", True,
           "Absolute pressure, per the column heading."),
    Column("Liquid Relative Density 15°C/15°C (1)", "", "Liquid Relative Density",
           "specific_gravity", None, True,
           "Density of the LIQUID relative to water, both at 15°C, per the "
           "column heading - that basis is what this source measured, and it "
           "is not interchangeable with a specific gravity taken at another "
           "temperature. DIMENSIONLESS, so `unit` is NULL."),
    Column("Vapour Relative Density (Air=1) (1)", "", "Vapour Relative Density",
           "RELATIVE_VAPOUR_DENSITY", None, True,
           "Density of the VAPOUR relative to air = 1, per the column heading. "
           "DIMENSIONLESS, so `unit` is NULL."),
    Column("Flammable Range (%Vol. By Air)", "Min (2)", "Flammable Range Min",
           "lel", "% vol", True,
           "The lean end of the flammable range in air, in % by volume, per the "
           "column heading."),
    Column("", "Max (2)", "Flammable Range Max",
           "uel", "% vol", True,
           "The rich end of the flammable range in air, in % by volume, per the "
           "column heading."),
    Column("100% Odor Recognition (ppm)", "", "100% Odor Recognition",
           "odour_limit", "ppm", True,
           "The source measures 100% ODOUR RECOGNITION: the concentration at "
           "which the odour is recognised by everyone. That is not a detection "
           "threshold and is a higher figure than one; sources that print a "
           "threshold in this field say so on their own values."),
    Column("Tlv", "ppm", "TLV (ppm)", "tlv_twa_ppm", "ppm", True, None),
    Column("", "(mg/m³)", "TLV (mg/m³)", "tlv_twa_mg_m3", "mg/m³", True, None),
]

ESTIMATED_NOTE = ("The source prints this value in parentheses, and its legend "
                  "reads '( ) : estimated value', so it is an estimate rather "
                  "than a measurement.")
WORDS_NOTE = ("The source prints words here rather than a figure; kept verbatim, "
              "with no normalized value.")
IMPOSSIBLE_NOTE = ("IMPOSSIBLE AS PRINTED: this quantity cannot be negative. The "
                   "figure is stored exactly as the source prints it and has NOT "
                   "been corrected - the sign appears to be an artefact of the "
                   "original document, but guessing the intended figure would be "
                   "inventing data.")
CRITICAL_NOTE = ("IMPOSSIBLE AS PRINTED: this critical temperature is below the "
                 "atmospheric boiling point the source gives on the same row "
                 "({bp}°C), and a substance's critical temperature is always "
                 "above its boiling point. Stored exactly as printed and NOT "
                 "corrected.")


def parse_cell(text: str) -> Tuple[str, Optional[float], Optional[float],
                                   Optional[float], List[str]]:
    """One cell -> (value_type, value, min, max, notes).

    Handles the file's estimated parentheses and its 'a - b' ranges; anything
    that is not a number after that is kept as text.
    """
    notes: List[str] = []
    inner = text
    m = re.match(r"^\((.+)\)$", text)
    if m:
        inner = m.group(1).strip()
        notes.append(ESTIMATED_NOTE)

    m = re.match(r"^(-?[\d.]+)\s*-\s*(-?[\d.]+)$", inner)
    if m:
        lo, hi = to_float(m.group(1)), to_float(m.group(2))
        if lo is not None and hi is not None:
            return "range", None, lo, hi, notes

    value = to_float(inner)
    if value is not None:
        return "number", value, None, None, notes
    notes.append(WORDS_NOTE)
    return "text", None, None, None, notes


def to_float(token: str) -> Optional[float]:
    try:
        return float(token.strip())
    except (TypeError, ValueError):
        return None


def parse_footer(lines: List[str]) -> Tuple[Dict[str, str], List[str], List[str]]:
    """Split the footer into numbered references, the TLV reference, and the rest."""
    refs: Dict[str, str] = {}
    tlv_ref: List[str] = []
    other: List[str] = []
    for line in lines:
        m = re.match(r"^\((\d+)\)\s*(.+)$", line)
        if m:
            refs[m.group(1)] = m.group(2).strip()
        elif line.startswith(TLV_REFERENCE_PREFIX):
            tlv_ref.append(line)
        else:
            other.append(line)
    return refs, tlv_ref, other


def markers_in(text: str) -> List[str]:
    """The (1)/(2)/(3) reference markers in a header cell or a product name."""
    return re.findall(r"\((\d+)\)", text)


def read_table(path: Path) -> Tuple[List[dict], Dict[str, str], List[str], List[str]]:
    """Parse the CSV into product rows, the reference list and the footer.

    Returns (rows, references, footer_lines, errors). Nothing is written unless
    errors is empty.
    """
    with path.open(newline="", encoding="utf-8-sig") as fh:
        grid = list(csv.reader(fh))
    errors: List[str] = []
    if len(grid) < 3:
        return [], {}, [], [f"{path.name} has no data rows"]

    width = 1 + len(COLUMNS)
    want_top = [NAME_HEADER] + [c.heading for c in COLUMNS]
    want_sub = [""] + [c.sub for c in COLUMNS]
    got_top = [collapse(c) for c in (grid[0] + [""] * width)[:width]]
    got_sub = [collapse(c) for c in (grid[1] + [""] * width)[:width]]
    if got_top != want_top or got_sub != want_sub:
        errors.append(f"header rows are {got_top!r} / {got_sub!r}, expected "
                      f"{want_top!r} / {want_sub!r} - is this the "
                      f"'properties of commercial gases' sheet?")
        return [], {}, [], errors

    rows: List[dict] = []
    footer: List[str] = []
    seen: Dict[str, int] = {}

    for i, raw in enumerate(grid[2:], start=3):
        cells = [collapse(c) for c in (list(raw) + [""] * width)[:width]]
        name, properties = cells[0], cells[1:]
        if not name and not any(properties):
            continue
        # Below the table the file turns into a legend: a line of prose in the
        # first column with every property column empty. A product always
        # carries at least one property, so the shape tells them apart.
        if not any(properties):
            footer.append(name)
            continue
        if not name:
            errors.append(f"row {i}: properties with no product name")
            continue

        # "Vinylchloride (3)" is a name plus a reference marker, not a name.
        row_markers = markers_in(name)
        clean_name = re.sub(r"\s*\(\d+\)\s*$", "", name).strip()
        if clean_name in seen:
            errors.append(f"row {i}: duplicate product {clean_name!r} "
                          f"(also row {seen[clean_name]})")
            continue
        seen[clean_name] = i

        values = []
        for col, text in zip(COLUMNS, properties):
            if not text or text == NOT_DETERMINED:
                continue

            notes: List[str] = []
            if col.note:
                notes.append(col.note)

            if text in LEGEND_TOKENS:
                spelled, why = LEGEND_TOKENS[text]
                values.append({"field": col.field, "column": col.label,
                               "value": spelled, "printed": text, "value_type": "text",
                               "normalized_value": None, "normalized_min": None,
                               "normalized_max": None, "unit": None,
                               "impossible": False, "markers": markers_in(col.heading + " " + col.sub) + row_markers,
                               "notes": notes + [why]})
                continue

            value_type, nv, nmin, nmax, cell_notes = parse_cell(text)
            notes += cell_notes

            numbers = [n for n in (nv, nmin, nmax) if n is not None]
            impossible = col.positive and any(n < 0 for n in numbers)
            if impossible:
                notes.append(IMPOSSIBLE_NOTE)

            values.append({
                "field": col.field, "column": col.label, "value": text,
                "printed": text, "value_type": value_type,
                "normalized_value": nv, "normalized_min": nmin,
                "normalized_max": nmax,
                "unit": col.unit if value_type != "text" else None,
                "impossible": impossible,
                "markers": markers_in(col.heading + " " + col.sub) + row_markers,
                "notes": notes,
            })

        rows.append({"gas_name": clean_name, "line": i, "markers": row_markers,
                     "values": values})

    if not rows:
        errors.append(f"{path.name} has a header but no product rows")
        return rows, {}, footer, errors

    references, tlv_ref, other = parse_footer(footer)

    # A legend the code depends on must actually be in the file.
    joined = " | ".join(footer)
    for needle, meaning in REQUIRED_LEGEND:
        if needle not in joined:
            errors.append(f"the file's footer no longer says {meaning!r} "
                          f"(looked for {needle!r}); the cells that rely on it "
                          f"can no longer be read safely")
    if not tlv_ref:
        errors.append(f"the file's footer no longer has the {TLV_REFERENCE_PREFIX!r} "
                      f"line naming where the TLV figures come from")

    # A critical temperature below the row's own boiling point is impossible.
    for r in rows:
        by_field = {v["field"]: v for v in r["values"]}
        crit, boil = by_field.get("critical_temperature_c"), by_field.get("boiling_point_c")
        if crit and boil and crit["normalized_value"] is not None \
                and boil["normalized_value"] is not None \
                and crit["normalized_value"] <= boil["normalized_value"]:
            crit["impossible"] = True
            crit["notes"].append(CRITICAL_NOTE.format(bp=boil["value"]))

    # Attach each value's reference(s), now that the footer has been read.
    for r in rows:
        for v in r["values"]:
            for marker in dict.fromkeys(v.pop("markers")):
                text = references.get(marker)
                if text is None:
                    errors.append(f"row {r['line']}: {r['gas_name']!r} is marked "
                                  f"'({marker})' in {v['column']!r}, which the "
                                  f"file's footer never defines")
                    continue
                v["notes"].append(f"Source reference ({marker}): {text}")
            if v["field"] in ("tlv_twa_ppm", "tlv_twa_mg_m3"):
                v["notes"].extend(f"Source reference: {line}" for line in tlv_ref)

    return rows, references, footer, errors


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


def report(rows: List[dict], references: Dict[str, str], footer: List[str]) -> None:
    per_field = Counter(v["field"] for r in rows for v in r["values"])
    total = sum(per_field.values())
    log.info("%d product(s), %d property value(s)", len(rows), total)
    for col in COLUMNS:
        log.info("    %-26s -> %-24s %2d value(s)", col.label, col.field,
                 per_field.get(col.field, 0))

    for marker, text in sorted(references.items()):
        log.info("reference (%s): %s", marker, text)

    worded = [(r["gas_name"], v["column"], v["value"]) for r in rows for v in r["values"]
              if v["value_type"] == "text"]
    log.info("cells the source answers in words rather than figures: %d", len(worded))
    for name, column, value in worded:
        log.info("    %-16s %-26s %s", name, column, value)

    estimated = [(r["gas_name"], v["column"], v["printed"]) for r in rows
                 for v in r["values"] if any(n == ESTIMATED_NOTE for n in v["notes"])]
    log.info("estimated values (printed in parentheses): %d", len(estimated))
    for name, column, value in estimated:
        log.info("    %-16s %-26s %s", name, column, value)

    bad = [(r["gas_name"], v["column"], v["printed"]) for r in rows
           for v in r["values"] if v["impossible"]]
    if bad:
        log.warning("%d figure(s) the source prints that cannot be measurements - "
                    "loaded as printed, flagged in notes, NOT corrected:", len(bad))
        for name, column, value in bad:
            log.warning("    %-16s %-26s %s", name, column, value)

    unattached = [line for line in footer
                  if not re.match(r"^\(\d+\)", line)
                  and not line.startswith(TLV_REFERENCE_PREFIX)]
    log.info("footer lines read as legend, not attached to any one value: %d",
             len(unattached))
    for line in unattached:
        log.info("    %s", line)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", default=str(DEFAULT_FILE))
    ap.add_argument("--dry-run", action="store_true", help="parse and report, write nothing")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.is_file():
        sys.exit(f"Error: file not found: {path}")

    rows, references, footer, errors = read_table(path)
    if errors:
        log.error("%d validation error(s) - nothing was imported:", len(errors))
        for e in errors:
            log.error("    %s", e)
        return 1

    log.info("%s", path.name)
    report(rows, references, footer)

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

            fields = list(dict.fromkeys(c.field for c in COLUMNS))
            added = ensure_field_definitions(cur, only=fields)
            log.info("field_definitions: %d created, %d already present",
                     added, len(fields) - added)

            created = written = 0
            for r in rows:
                gas_id, is_new = upsert_gas(cur, source_id, r["gas_name"])
                created += is_new
                for v in r["values"]:
                    upsert_property(
                        cur, gas_id, source_id, v["field"],
                        value=v["value"],
                        normalized_value=v["normalized_value"],
                        normalized_min=v["normalized_min"],
                        normalized_max=v["normalized_max"],
                        unit=v["unit"], value_type=v["value_type"],
                        entered_by=ENTERED_BY,
                        notes=" ".join(v["notes"]) or None,
                    )
                    written += 1

        conn.commit()
        log.info("✓ Committed. cargo_gas: %d (%d created this run) | "
                 "cargo_gas_property_values: %d", len(rows), created, written)
        return 0
    except Exception:
        conn.rollback()
        log.exception("Import failed - rolled back, nothing written.")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
