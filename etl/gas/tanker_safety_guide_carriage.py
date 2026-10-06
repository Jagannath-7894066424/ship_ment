#!/usr/bin/env python3
"""
Load the Tanker Safety Guide's CARRIAGE CONDITIONS table into cargo_gas and
cargo_gas_property_values.

SOURCE
------
"Tanker Safety Guide - Liquefied Gas" (source.json, category 'gas') - the SAME
source row as etl/gas/tanker_safety_guide.py, because this is a second table in
the same publication rather than a second publication.

WHY IT NEEDS ITS OWN FIELDS
---------------------------
Sharing a source_id with the properties table is what makes this loader
delicate. cargo_gas_property_values is keyed (cargo_gas_id, source_id,
field_name) and its upsert UPDATES on conflict, so if this table wrote to the
fields the properties table already fills, one table in the book would silently
overwrite the other - and it would lose by every measure:

    cargo       properties table              this table
    --------    --------------------------    -----------------------------
    Ethylene    boiling point -103.7°C        -104
    Propylene   -47.7°C                       -48
    Propane     vapour pressure 1.01 at       15.6
                -42°C; 4.9 at 0°C;
                9.53 at 25°C

So all three of its columns load under their own field names:

  * "Boiling point at atmospheric pressure (°C)" -> boiling_point_atmospheric_c,
    a deliberate twin of boiling_point_c. Same quantity; this table is the one
    that states the pressure basis in its heading.
  * "Vapour pressure at 45°C (bar)" -> vapour_pressure_45c, which is NOT a twin.
    The properties table gives vapour pressure at the cargo's boiling point;
    45°C is a fixed design reference, the temperature a pressurised carrier's
    tanks must hold the cargo at. The two answer different questions.
  * "Practical carriage conditions" -> practical_carriage_conditions, a twin of
    normal_carriage_condition. The guide phrases the two columns differently
    ("Fully-pressurised, semi-pressurised or fully-refrigerated" against
    "Pressurised or Fully-refrigerated") and neither should overwrite the other.

Where this table's boiling point disagrees with the figure the properties table
already loaded for the same cargo, the disagreement is written onto this value
and listed in the run log. Both figures stay; neither is corrected.

CARGO NAMES
-----------
This table names its cargoes more loosely than the properties table, and six of
the eleven resolve to a row that already exists (_gas.upsert_gas matches within
a source ignoring case, spaces and hyphens, so "Vinyl chloride" finds "Vinyl
Chloride"). Five do not:

    n-butane, iso-butane   the properties table has "Butanes (All Isomers)"
    Butene                 ... has "Butenes (All Isomers)"
    Butadiene              ... has "Butadiene (Inhibited)"
    Ammonia                ... has "Ammonia (Anhydrous)"

Those five become their own cargo_gas rows. That is deliberate and is NOT the
"one cargo spelled two ways" case upsert_gas guards against: "Butanes (All
Isomers)" is a single entry covering both isomers, while this table gives
n-butane and iso-butane separate rows with different figures (-0.5 against -12,
4.3 against 6.1). Collapsing them would have to throw one of those figures
away. The same is true of the qualifiers: the properties table's entry is for
INHIBITED butadiene and ANHYDROUS ammonia, and this table does not say its rows
are either. Each of the five records on every one of its values which entry in
the properties table covers the same ground, so the pair can be found.

THE MERGED COLUMN
-----------------
"Practical carriage conditions" is printed once per GROUP of cargoes, not once
per cargo - in the CSV the cell is filled on the first row of a group and empty
on the rest:

    n-butane .. Propylene   Fully-pressurised, semi-pressurised or fully-refrigerated
    Ethane, Ethylene        Semi-pressurised or fully-refrigerated
    Methane/LNG             Fully-refrigerated

An empty cell here means "as above", not "no data", so the value is carried
down and every cargo gets one. Each value records that the guide stated it for
a group and names the group, because a reader must not take it as a statement
the guide made about that cargo alone. A blank in the FIRST row would mean the
grouping cannot be read and is a validation error.

"ABOVE CRITICAL TEMPERATURE"
----------------------------
Ethane, ethylene and methane are given those words instead of a pressure. That
is not a missing figure: 45°C is above their critical temperature, so they have
no vapour pressure at it and cannot be carried pressurised at all - which is
why the carriage column puts exactly those three cargoes in its refrigerated
groups. The words are stored verbatim as text with no normalized value.

IDEMPOTENCY
-----------
Upsert on (gas_name, source_id) and (cargo_gas_id, source_id, field_name). The
whole file is validated before anything is written; one transaction.

Usage:
    python3 etl/gas/tanker_safety_guide_carriage.py
    python3 etl/gas/tanker_safety_guide_carriage.py --dry-run
    python3 etl/gas/tanker_safety_guide_carriage.py "/path/to/...Liquefied Gas.csv"
"""

import argparse
import csv
import logging
import os
import re
import sys
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
log = logging.getLogger("gas_tsg_carriage")

SOURCE_NAME = "Tanker Safety Guide - Liquefied Gas"
ENTERED_BY = "tanker_safety_guide_carriage.py"
DEFAULT_FILE = input_file("Tanker Safety Guide - Liquefied Gas.csv")

NAME_HEADER = "Cargo"
CARRIAGE_FIELD = "practical_carriage_conditions"
CARRIAGE_FIELD_HEADING = "Practical carriage conditions"
# The properties table's field for the same quantity, read back to see whether
# the two tables in this book agree. See WHY IT NEEDS ITS OWN FIELDS.
TWIN_OF_BOILING_POINT = "boiling_point_c"

# The properties-table entry that covers the same ground as a cargo this table
# names differently. Recorded on the value so the pair can be found; see
# CARGO NAMES in the module docstring.
COVERED_BY: Dict[str, str] = {
    "n-butane": "Butanes (All Isomers)",
    "iso-butane": "Butanes (All Isomers)",
    "Butene": "Butenes (All Isomers)",
    "Butadiene": "Butadiene (Inhibited)",
    "Ammonia": "Ammonia (Anhydrous)",
}
COVERED_NOTE = ("This guide's properties table covers the same cargo under the "
                "entry {other!r} and its figures are stored against that name. "
                "The two are kept apart because they are not the same claim: "
                "{why}")
WHY: Dict[str, str] = {
    "n-butane": "that entry is one row for both butane isomers, while this "
                "table gives n-butane and iso-butane separate figures.",
    "iso-butane": "that entry is one row for both butane isomers, while this "
                  "table gives n-butane and iso-butane separate figures.",
    "Butene": "that entry is one row for all butene isomers.",
    "Butadiene": "that entry is for INHIBITED butadiene; this table does not "
                 "say its row is.",
    "Ammonia": "that entry is for ANHYDROUS ammonia; this table does not say "
               "its row is.",
}


class Column(NamedTuple):
    heading: str          # the CSV header, exactly as printed
    field: str            # field_definitions.field_name
    label: str            # what to call it in a log line
    numeric: bool
    unit: Optional[str]
    note: str             # what the column heading says, on every value


COLUMNS: List[Column] = [
    Column("Boiling point at atmospheric pressure (°C)",
           "boiling_point_atmospheric_c", "Boiling Point", True, "°C",
           "At atmospheric pressure, per the column heading. This guide's "
           "properties table gives the same quantity in `boiling_point_c`, "
           "sometimes to another decimal place - query both."),
    Column("Vapour pressure at 45°C (bar)", "vapour_pressure_45c",
           "Vapour Pressure at 45°C", True, "bar",
           "At 45°C, per the column heading: a fixed design reference, not the "
           "cargo's carriage temperature. NOT comparable with `vapour_pressure`, "
           "which this guide's properties table gives at the boiling point."),
    Column("Practical carriage conditions", CARRIAGE_FIELD,
           "Practical Carriage Conditions", False, None,
           "How the cargo can be carried in practice, in the guide's own words."),
]

ABOVE_CRITICAL = "Above critical temperature"
ABOVE_CRITICAL_NOTE = (
    "The guide prints {words!r} here rather than a pressure. That is a "
    "statement, not a gap: 45°C is above this cargo's critical temperature, so "
    "it has no vapour pressure at that temperature and cannot be carried "
    "pressurised at all. Stored verbatim with no normalized value.")
WORDS_NOTE = ("The guide answers this column in words rather than with a "
              "figure; kept verbatim, with no normalized value.")
GROUP_NOTE = ("The guide prints this once for a GROUP of cargoes rather than "
              "for this one - {members} share the entry - and the cell is "
              "blank on the rest of the group. It is carried down here so "
              "every cargo in the group has it, but it is not a statement the "
              "guide made about this cargo alone.")
DISAGREE_NOTE = ("This guide's properties table gives {twin} for the same "
                 "cargo, which is not the same figure. Both are what the book "
                 "prints, in two different tables; neither has been corrected.")


def collapse(value: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (value or "").replace("\n", " ")).strip()


def to_float(token: str) -> Optional[float]:
    try:
        return float(token.replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def read_table(path: Path) -> Tuple[List[dict], List[str]]:
    """Parse the CSV into cargo rows. Returns (rows, errors)."""
    with path.open(newline="", encoding="utf-8-sig") as fh:
        grid = list(csv.reader(fh))
    errors: List[str] = []
    if len(grid) < 2:
        return [], [f"{path.name} has no data rows"]

    want = [NAME_HEADER] + [c.heading for c in COLUMNS]
    width = len(want)
    got = [collapse(c) for c in (grid[0] + [""] * width)[:width]]
    if got != want or len(grid[0]) != width:
        for i, (g, w) in enumerate(zip(got, want)):
            if g != w:
                errors.append(f"column {i} is headed {g!r}, expected {w!r}")
        if len(grid[0]) != width:
            errors.append(f"the sheet has {len(grid[0])} columns, expected {width}")
        errors.append("is this the Tanker Safety Guide carriage-conditions table?")
        return [], errors
    index = {h: i for i, h in enumerate(want)}

    # Pass one: the cargoes, and where each carriage-condition group starts.
    parsed: List[dict] = []
    seen: Dict[str, int] = {}
    for line, raw in enumerate(grid[1:], start=2):
        cells = [collapse(c) for c in (list(raw) + [""] * width)[:width]]
        name = cells[index[NAME_HEADER]]
        if not name and not any(cells):
            continue
        if not name:
            errors.append(f"row {line}: figures with no cargo name")
            continue
        if name in seen:
            errors.append(f"row {line}: duplicate cargo {name!r} "
                          f"(also row {seen[name]})")
            continue
        seen[name] = line
        parsed.append({"gas_name": name, "line": line, "cells": cells})

    if not parsed:
        errors.append(f"{path.name} has a header but no cargo rows")
        return parsed, errors
    if not parsed[0]["cells"][index[CARRIAGE_FIELD_HEADING]]:
        errors.append(
            f"row {parsed[0]['line']}: the first cargo has no "
            f"{CARRIAGE_FIELD_HEADING!r}. That column is printed once per group "
            f"and carried down, so a blank on the first row leaves every cargo "
            f"below it with nothing to inherit")
        return parsed, errors

    # Pass two: carry the merged column down, and record who shares each entry.
    groups: List[List[int]] = []
    for i, r in enumerate(parsed):
        if r["cells"][index[CARRIAGE_FIELD_HEADING]]:
            groups.append([])
        groups[-1].append(i)
    group_of = {i: g for g in groups for i in g}

    rows: List[dict] = []
    for i, r in enumerate(parsed):
        group = group_of[i]
        carriage = parsed[group[0]]["cells"][index[CARRIAGE_FIELD_HEADING]]
        values = []
        for col in COLUMNS:
            notes = [col.note]
            if col.field == CARRIAGE_FIELD:
                text = carriage
                if len(group) > 1:
                    notes.append(GROUP_NOTE.format(
                        members=", ".join(parsed[j]["gas_name"] for j in group)))
                parsed_value = ("text", None)
            else:
                text = r["cells"][index[col.heading]]
                if not text:
                    errors.append(f"row {r['line']}: {r['gas_name']!r} has no "
                                  f"{col.label}")
                    continue
                number = to_float(text)
                if number is not None:
                    parsed_value = ("number", number)
                else:
                    parsed_value = ("text", None)
                    notes.append(ABOVE_CRITICAL_NOTE.format(words=text)
                                 if text == ABOVE_CRITICAL else WORDS_NOTE)

            other = COVERED_BY.get(r["gas_name"])
            if other:
                notes.append(COVERED_NOTE.format(other=other,
                                                 why=WHY[r["gas_name"]]))
            value_type, normalized = parsed_value
            values.append({
                "field": col.field, "column": col.label, "value": text,
                "value_type": value_type, "normalized_value": normalized,
                "unit": col.unit if value_type != "text" else None,
                "notes": notes,
            })
        rows.append({"gas_name": r["gas_name"], "line": r["line"],
                     "values": values, "group": [parsed[j]["gas_name"] for j in group]})

    return rows, errors


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


def report(rows: List[dict]) -> None:
    log.info("%d cargo(es), %d property value(s)", len(rows),
             sum(len(r["values"]) for r in rows))
    for col in COLUMNS:
        n = sum(1 for r in rows for v in r["values"] if v["field"] == col.field)
        log.info("    %-28s -> %-30s %2d value(s)", col.label, col.field, n)

    seen_groups = []
    for r in rows:
        if r["group"] not in seen_groups:
            seen_groups.append(r["group"])
    log.info("carriage conditions are printed once per group of cargoes: "
             "%d group(s)", len(seen_groups))
    for group in seen_groups:
        carriage = next(v["value"] for r in rows if r["gas_name"] == group[0]
                        for v in r["values"] if v["field"] == CARRIAGE_FIELD)
        log.info("    %-42s %s", ", ".join(group), carriage)

    words = [(r["gas_name"], v["column"], v["value"]) for r in rows
             for v in r["values"] if v["value_type"] == "text"
             and v["field"] != CARRIAGE_FIELD]
    log.info("cells the guide answers in words rather than figures: %d", len(words))
    for name, column, value in words:
        log.info("    %-16s %-24s %s", name, column, value)

    covered = [(r["gas_name"], COVERED_BY[r["gas_name"]]) for r in rows
               if r["gas_name"] in COVERED_BY]
    log.info("cargoes this table names differently from the properties table, "
             "so they get their own cargo_gas row: %d", len(covered))
    for name, other in covered:
        log.info("    %-16s also in the properties table as %r", name, other)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", default=str(DEFAULT_FILE))
    ap.add_argument("--dry-run", action="store_true", help="parse and report, write nothing")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.is_file():
        sys.exit(f"Error: file not found: {path}")

    rows, errors = read_table(path)
    if errors:
        log.error("%d validation error(s) - nothing was imported:", len(errors))
        for e in errors:
            log.error("    %s", e)
        return 1

    log.info("%s", path.name)
    report(rows)

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
            log.info("Source id=%s (%r) - the same source row as the properties "
                     "table", source_id, SOURCE_NAME)

            fields = [c.field for c in COLUMNS]
            added = ensure_field_definitions(cur, only=fields)
            log.info("field_definitions: %d created, %d already present",
                     added, len(fields) - added)

            created = written = 0
            disagreements = []
            for r in rows:
                gas_id, is_new = upsert_gas(cur, source_id, r["gas_name"])
                created += is_new

                # What the properties table already says about this cargo, so a
                # disagreement between two tables in one book is visible on the
                # value rather than only to whoever compares them by hand.
                cur.execute(
                    "SELECT value FROM cargo_gas_property_values WHERE "
                    "cargo_gas_id = %s AND source_id = %s AND field_name = %s",
                    (gas_id, source_id, TWIN_OF_BOILING_POINT))
                twin = cur.fetchone()

                for v in r["values"]:
                    notes = list(v["notes"])
                    if (v["field"] == "boiling_point_atmospheric_c" and twin
                            and collapse(twin[0]).rstrip("°C") != v["value"]):
                        notes.append(DISAGREE_NOTE.format(twin=twin[0]))
                        disagreements.append((r["gas_name"], v["value"], twin[0]))
                    upsert_property(
                        cur, gas_id, source_id, v["field"],
                        value=v["value"],
                        normalized_value=v["normalized_value"],
                        unit=v["unit"], value_type=v["value_type"],
                        entered_by=ENTERED_BY,
                        source_page_ref="carriage conditions table",
                        notes=" ".join(notes) or None,
                    )
                    written += 1

            if disagreements:
                log.warning("%d boiling point(s) the guide's two tables do not "
                            "agree on - both kept, neither corrected:",
                            len(disagreements))
                for name, here, there in disagreements:
                    log.warning("    %-16s this table %-8s properties table %s",
                                name, here, there)

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
