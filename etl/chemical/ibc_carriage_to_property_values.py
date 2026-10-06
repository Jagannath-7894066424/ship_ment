#!/usr/bin/env python3
"""
Move the IBC Code's carriage requirements out of the wide cargo_chemical
columns and into cargo_property_values, where every other per-source statement
in this database lives.

WHY
---
cargo_chemical is keyed (source_id, canonical_name) - one row per chemical PER
SOURCE - and carries ~60 wide columns. Those columns are shared by every
source, so a column can only ever hold one source's answer for a given row, and
a reader cannot tell which source a figure came from without looking at the
row's source_id. cargo_property_values exists for exactly that reason: it is
keyed (cargo_id, source_id, field_name) and every value records its own
provenance, units and notes.

The IBC Code (source 'IBC Code', chapter 17's summary of minimum requirements)
is loaded entirely into the wide columns. master_loader.py has a property map
for its MIRACLE, LARS and CHEM formats - each writes the wide column AND a
cargo_property_values row - but IBC_MAPPING has no counterpart, so the IBC
data never reaches the property table. This script closes that gap for the data
already loaded; see LOADER CHANGE below for keeping it closed.

WHAT MOVES
----------
The thirteen columns IBC_MAPPING fills (etl/chemical/cargo_chemicals.py). All
thirteen are REGULATORY CODES, not measurements - 'Y', 'S/P', '2G', 'Cont',
'IIA', 'T2', 'C', 'FT', 'AC' - so every value is stored as text or boolean with
no unit and no normalized_value. ship_type is '1', '2' or '3': a ship type
label, not a quantity, and never arithmetic. This is the same rule the gas
branch applies to the IGC Code's equivalent columns (etl/gas/_gas.py).

WHAT DOES *NOT* HAPPEN, AND WHY
-------------------------------
"Remove them from cargo_chemical" is done by setting the thirteen columns to
NULL on this source's rows. Two more literal readings would both destroy data,
and neither is what the request means:

  * DROP COLUMN is impossible. Two other sources fill the same columns -
    Miracle Tank Cleaning Guide on ~725 of its 791 rows and LARS on up to 815
    of its 993 - and dropping a column takes their data with it. The columns
    stay; only THIS source's rows are cleared.

  * DELETE the 800 cargo_chemical rows is impossible. Eight tables FK
    cargo_chemical.id ON DELETE CASCADE, including cargo_property_values - so
    deleting the rows would take away the very values this script just wrote,
    plus 215 cargo_reactive_group rows. Worse, cleaning_process.from_cargo_id /
    to_cargo_id are polymorphic and carry NO foreign key, so 9,884 cleaning
    rows would be silently orphaned and nothing would complain. The master row
    is the cargo's identity and must survive; it is only its property columns
    that move.

FIELD NAMES ARE GENERIC ON PURPOSE
----------------------------------
The fields are `ship_type`, `gauging`, `tank_type` and so on, NOT `ibc_*`. The
gas branch prefixes its equivalents `igc_` because the IGC Code is the only gas
source that states them; here Miracle and LARS fill the same columns, so the
same requirement from a second chemical source must land in the same field for
the comparison to be possible at all. Which source said it is already recorded
by source_id on every row.

field_definitions is FK'd by cargo_property_values ON DELETE CASCADE, and
etl/common/field_definition.py DELETES every catalog row missing from its own
FIELDS list. The twelve new fields are therefore added to that list too, as
catalog_only entries - seeded in the catalog, with no new cargo_chemical column
created. Without that, the next run of field_definition.py would cascade away
everything this script writes.

LOADER CHANGE
-------------
This script only migrates data already in the database. master_loader.py still
writes the wide columns for this source, so a later `run_chemical.sh` would put
them straight back. Adding an IBC property map there is the permanent fix and
is NOT done here - this script is re-runnable and will move them again.

IDEMPOTENCY
-----------
Upsert on (cargo_id, source_id, field_name), then NULL the columns. Running it
twice is a no-op the second time: the values are already in the property table
and the columns are already NULL. Everything is validated and counted inside
one transaction, which is rolled back unless the counts add up.

Usage:
    python3 etl/chemical/ibc_carriage_to_property_values.py --dry-run
    python3 etl/chemical/ibc_carriage_to_property_values.py
"""

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import List, NamedTuple, Optional

import psycopg2
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("ibc_carriage_migration")

SOURCE_NAME = "IBC Code"
ENTERED_BY = "ibc_carriage_to_property_values.py"
ENTRY_TYPE = "import"


class Col(NamedTuple):
    column: str        # cargo_chemical column
    field: str         # field_definitions.field_name
    value_type: str    # text | boolean
    display: str
    category: str
    description: str


IBC_NOTE = ("From the IBC Code chapter 17 summary of minimum requirements. A "
            "regulatory REQUIREMENT placed on the ship, not a property measured "
            "of the cargo, so it carries no unit and no normalized value.")

COLUMNS: List[Col] = [
    Col("ibc_pollution_category", "ibc_pollution_category", "text",
        "IBC Pollution Category", "Regulatory",
        "MARPOL Annex II pollution category (X, Y or Z) as the IBC Code assigns it."),
    Col("hazards", "hazards", "text", "Hazards", "Regulatory",
        "The hazard the cargo is regulated for, in the source's own notation - "
        "'S' safety, 'P' pollution, 'S/P' both."),
    Col("ship_type", "ship_type", "text", "Ship Type", "Regulatory",
        "The ship type a cargo may be carried in - 1, 2 or 3, in decreasing "
        "order of the damage the ship must survive. A TYPE LABEL, not a "
        "quantity: it is text and is never averaged or compared numerically."),
    Col("tank_type", "tank_type", "text", "Tank Type", "Regulatory",
        "The cargo tank type required for this cargo."),
    Col("tank_vents", "tank_vents", "text", "Tank Vents", "Regulatory",
        "The tank venting the code requires - controlled ('Cont') or open."),
    Col("tank_environment_control", "tank_environment_control", "text",
        "Tank Environment Control", "Regulatory",
        "What the cargo tank's atmosphere must be kept as - inerted, padded, "
        "dried, or no requirement. Controls the space around the cargo, which "
        "is a carriage requirement rather than a property of it."),
    Col("electrical_equipment_apparatus_group", "electrical_equipment_apparatus_group",
        "text", "Electrical Apparatus Group", "Regulatory",
        "Explosion-protection apparatus group required for electrical "
        "equipment in spaces containing this cargo - IIA, IIB or IIC."),
    Col("electrical_equipment_temperature_class", "electrical_equipment_temperature_class",
        "text", "Electrical Temperature Class", "Regulatory",
        "Temperature class required for electrical equipment in spaces "
        "containing this cargo - T1 to T6, by decreasing surface temperature."),
    Col("flashpoint_requirement", "flashpoint_requirement", "text",
        "Flashpoint Requirement", "Regulatory",
        "Whether the code imposes a flashpoint requirement on this cargo. Text "
        "rather than boolean: the source qualifies its Yes and No with "
        "footnote letters ('Yes(a)', 'No(c)') that change what is required."),
    Col("gauging", "gauging", "text", "Gauging", "Regulatory",
        "The cargo gauging type the code permits - closed, restricted or open."),
    Col("vapour_detection", "vapour_detection", "text", "Vapour Detection", "Regulatory",
        "The vapour detection equipment the code requires - flammable, toxic, "
        "or both. States what the ship must be able to detect, not what the "
        "vapour is."),
    Col("fire_protection", "fire_protection", "text", "Fire Protection", "Regulatory",
        "The fire-fighting media the code requires, as a string of letter "
        "codes ('ABC', 'BD'); each letter is one medium, so the entry lists "
        "every medium required."),
    Col("emergency_equipment", "emergency_equipment", "boolean",
        "Emergency Equipment Required", "Regulatory",
        "Whether the code requires additional emergency equipment for this cargo."),
]


# Booleans arrive from the SELECT as the TEXT 'true'/'false', because every
# column is cast ::text so the two enum columns come back as strings. The
# string 'false' is TRUTHY in Python, so a bare `if value` silently turns every
# false into a true - it did exactly that on the first run of this script, and
# all 800 emergency_equipment values had to be repaired. Parse the text, and
# raise on anything unrecognised rather than guessing.
TRUE_TEXT = {"true", "t", "yes", "y", "1"}
FALSE_TEXT = {"false", "f", "no", "n", "0"}


def render(value, value_type: str) -> Optional[str]:
    """One column value -> the text stored in cargo_property_values.value."""
    if value is None:
        return None
    if value_type == "boolean":
        if isinstance(value, bool):
            return "true" if value else "false"
        text = str(value).strip().lower()
        if text in TRUE_TEXT:
            return "true"
        if text in FALSE_TEXT:
            return "false"
        raise ValueError(f"expected a boolean, got {value!r}")
    return str(value).strip() or None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="do everything, report, then roll back")
    args = ap.parse_args()

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        sys.exit("Error: DATABASE_URL not set (checked .env).")
    log.info("target: %s", db_url.split("@")[-1])

    conn = psycopg2.connect(db_url)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM source WHERE name = %s", (SOURCE_NAME,))
            row = cur.fetchone()
            if row is None:
                sys.exit(f"Error: source {SOURCE_NAME!r} not found.")
            source_id = row[0]
            log.info("source id=%s (%r)", source_id, SOURCE_NAME)

            # 1. The catalog rows must exist before any value can reference them.
            added = 0
            for c in COLUMNS:
                cur.execute(
                    """INSERT INTO field_definitions
                           (field_name, display_name, data_type, unit, category,
                            description, typical_source, created_at, updated_at)
                       VALUES (%s,%s,%s,NULL,%s,%s,%s, now(), now())
                       ON CONFLICT (field_name) DO NOTHING""",
                    (c.field, c.display, c.value_type, c.category, c.description,
                     "IBC Code chapter 17"))
                added += cur.rowcount
            log.info("field_definitions: %d created, %d already present",
                     added, len(COLUMNS) - added)

            # 2. Read this source's rows and move every non-null column value.
            cols = ", ".join(f'"{c.column}"::text' for c in COLUMNS)
            cur.execute(f"SELECT id, canonical_name, {cols} FROM cargo_chemical "
                        f"WHERE source_id = %s ORDER BY id", (source_id,))
            rows = cur.fetchall()
            log.info("%d cargo_chemical row(s) for this source", len(rows))

            written = 0
            per_field = {c.field: 0 for c in COLUMNS}
            for r in rows:
                cargo_id = r[0]
                for c, raw in zip(COLUMNS, r[2:]):
                    value = render(raw, c.value_type)
                    if value is None:
                        continue
                    cur.execute(
                        """INSERT INTO cargo_property_values
                               (cargo_id, source_id, field_name, value,
                                normalized_value, unit, value_type,
                                source_synonym_id, source_page_ref, as_of_date,
                                entered_date, entered_by, entry_type,
                                is_winning, conflict_flag, notes,
                                created_at, updated_at)
                           VALUES (%s,%s,%s,%s, NULL, NULL, %s,
                                   NULL, %s, NULL, now(), %s, %s, TRUE, FALSE, %s,
                                   now(), now())
                           ON CONFLICT (cargo_id, source_id, field_name) DO UPDATE SET
                               value            = EXCLUDED.value,
                               value_type       = EXCLUDED.value_type,
                               unit             = EXCLUDED.unit,
                               normalized_value = EXCLUDED.normalized_value,
                               source_page_ref  = EXCLUDED.source_page_ref,
                               notes            = EXCLUDED.notes,
                               updated_at       = now()""",
                        (cargo_id, source_id, c.field, value, c.value_type,
                         "IBC Code chapter 17", ENTERED_BY, ENTRY_TYPE, IBC_NOTE))
                    per_field[c.field] += 1
                    written += 1

            for c in COLUMNS:
                log.info("    %-40s -> %-40s %5d value(s)",
                         c.column, c.field, per_field[c.field])
            log.info("%d property value(s) written", written)

            # 3. Verify every value landed BEFORE clearing anything.
            cur.execute("""SELECT count(*) FROM cargo_property_values
                           WHERE source_id = %s AND field_name = ANY(%s)""",
                        (source_id, [c.field for c in COLUMNS]))
            landed = cur.fetchone()[0]
            if landed != written:
                raise RuntimeError(
                    f"{written} values written but {landed} present - refusing "
                    f"to clear the columns")
            log.info("verified: all %d value(s) present in cargo_property_values", landed)

            # 4. Only now clear the columns, and only for THIS source.
            sets = ", ".join(f'"{c.column}" = NULL' for c in COLUMNS)
            cur.execute(f"UPDATE cargo_chemical SET {sets}, updated_at = now() "
                        f"WHERE source_id = %s", (source_id,))
            log.info("cleared 13 column(s) on %d cargo_chemical row(s)", cur.rowcount)

            # 5. The other sources' data in those same columns must be untouched.
            counts = ", ".join(f'count("{c.column}")' for c in COLUMNS)
            cur.execute(f"SELECT source_id, {counts} FROM cargo_chemical "
                        f"WHERE source_id <> %s GROUP BY 1 ORDER BY 1", (source_id,))
            log.info("other sources' values still in those columns (must be non-zero "
                     "for Miracle and LARS):")
            for r in cur.fetchall():
                log.info("    source %-3s %s", r[0], list(r[1:]))
            cur.execute(f"SELECT {counts} FROM cargo_chemical WHERE source_id = %s",
                        (source_id,))
            remaining = sum(cur.fetchone())
            if remaining:
                raise RuntimeError(f"{remaining} value(s) still set on this source")
            log.info("this source's columns are now empty")

        if args.dry_run:
            conn.rollback()
            log.info("--dry-run: rolled back, nothing written.")
            return 0
        conn.commit()
        log.info("✓ Committed. %d value(s) moved into cargo_property_values; "
                 "13 column(s) cleared on %d row(s).", written, len(rows))
        return 0
    except Exception:
        conn.rollback()
        log.exception("Migration failed - rolled back, nothing written.")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
