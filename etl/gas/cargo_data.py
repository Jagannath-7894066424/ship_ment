#!/usr/bin/env python3
"""
Load the "Cargo Data" gas list into cargo_gas + cargo_gas_property_values.

SOURCE
------
"Cargo Data" (source.json, category 'gas'). The FOURTH gas source, after
VAPDENS.XLSX, Products_info and Properties of Gases Doc. It overlaps all three
by name; nothing is merged. cargo_gas is keyed (gas_name, source_id), so each
source keeps its own rows and a disagreement between them stays visible - and
there is one worth seeing here: this file gives ethane UN 1061, Products_info
gives it UN 1961.

It is ranked last of the four for physical properties: it is the thinnest
table, it gives no units, and one of its cells is in the wrong column (below).

INPUT
-----
"Cargo Data.csv": 19 cargoes, four property columns.

    cargo_name | formula | molecular_weight | un_no_imo_class |
    density_15c_header_value

The header is matched exactly, malformed last column name and all - a
re-export with different columns stops the loader rather than loading a
molecular weight into a density.

TWO FACTS IN ONE COLUMN
-----------------------
`un_no_imo_class` holds both, as its name says: "1005/2" is UN 1005, IMO class
2. They are split into UN_NUMBER and imo_class, and each value's notes record
the pair as printed so the split can always be checked against the source. LPG
Mix is written "- / 2": no UN number, class 2, and only the class is stored.

imo_class is its own field rather than the existing imdg_class. The two are not
reliably the same thing - this file puts vinyl chloride in class 3, where the
IMDG class for UN 1086 is 2.1 - so writing these values into imdg_class would
assert a regulatory fact the source never made.

DENSITY, NOT SPECIFIC GRAVITY
-----------------------------
The last column gives a liquid density at 15°C with no unit. It is stored in
`density` (kg/l), not `specific_gravity`: the column says "density", and
saying "relative to water" on the source's behalf would be inventing the claim.
For these products the two readings differ only past the third decimal (water
at 15°C is 0.999 kg/l), and every value says so in its notes.

FIGURES THAT ARE IN THE WRONG COLUMN
------------------------------------
Propylene oxide's density reads 58.08000. No liquid has a density of 58 kg/l,
and that row's molecular_weight cell - filled in on every other row that has a
figure - is empty. The number is a molecular weight that landed one column
right.

The loader does not move it. It is stored where the source puts it, flagged in
`notes`, and reported in the run log: moving a value between fields on a guess
would put a figure in the database that the source never printed there.

The same discipline catches a second inconsistency from the file's own data:
n-butane and i-butane are both given the formula C4H10 but different molecular
weights (58.124 and 58.023). Isomers share a molecular weight by definition, so
one of the two is wrong; both are loaded as printed and both are flagged.

NAMES ARE VERBATIM
------------------
gas_name is what the source prints, including "SO-Pentane" and "Acetaldehyd".
Names are source-scoped; correcting them here would make this source's rows
unfindable from the file they came from. "Acetaldehyd" is a name with no
properties at all in this file - the cargo_gas row is still created, because
the source listing a cargo is itself the fact this table records.

IDEMPOTENCY
-----------
Upsert on (gas_name, source_id) and (cargo_gas_id, source_id, field_name). The
whole file is validated before anything is written; one transaction.

Usage:
    python3 etl/gas/cargo_data.py
    python3 etl/gas/cargo_data.py --dry-run
    python3 etl/gas/cargo_data.py "/path/to/Cargo Data.csv"
"""

import argparse
import csv
import logging
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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
log = logging.getLogger("gas_cargo_data")

SOURCE_NAME = "Cargo Data"
ENTERED_BY = "cargo_data.py"
DEFAULT_FILE = input_file("Cargo Data.csv")

HEADER = ["cargo_name", "formula", "molecular_weight", "un_no_imo_class",
          "density_15c_header_value"]

# Fields this loader writes, in the order they are reported.
FIELDS = ["molecular_formula", "molecular_weight_g_mol", "UN_NUMBER",
          "imo_class", "density"]

# No liquid comes near this. Mercury, the densest liquid anyone ships, is 13.5
# kg/l; every liquefied gas in this table is below 1.5. A "density" above 5 is
# not a density at all.
DENSITY_CEILING = 5.0

MW_NOTE = ("The source's column gives no unit. The figures are molecular "
           "weights in g/mol - the field's canonical unit - which is what they "
           "match to three decimals.")
DENSITY_NOTE = ("Liquid density at 15°C, per the column name. The source gives "
                "no unit and does not say the figure is relative to water, so "
                "it is stored as a density in the field's canonical kg/l rather "
                "than as a specific gravity; near 15°C the two readings differ "
                "only past the third decimal (water is 0.999 kg/l there).")
FORMULA_NAME_NOTE = ("The source repeats the product's own name in the formula "
                     "column: a mixture has no single molecular formula, and "
                     "this is the source saying so rather than giving one.")
DENSITY_IMPOSSIBLE_NOTE = (
    "IMPOSSIBLE AS PRINTED: no liquid has a density of {value} kg/l. This row's "
    "molecular_weight cell is empty while other rows fill it, so the figure "
    "looks like a molecular weight that landed one column to the right. It is "
    "stored where the source puts it and has NOT been moved or corrected - "
    "relocating a value on a guess would put a figure in the database that the "
    "source never printed there.")
MW_MISMATCH_NOTE = (
    "INCONSISTENT IN THE SOURCE: {others} {is_are} given the same formula "
    "({formula}) on another row of this file but a different molecular weight "
    "({values}). Isomers share a molecular weight by definition, so one of them "
    "is wrong. Both are stored as printed and neither has been corrected.")


def split_un_class(text: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """'1005/2' -> ('1005', '2', None); returns (un, imo_class, error)."""
    parts = [p.strip() for p in text.split("/")]
    if len(parts) != 2:
        return None, None, (f"{text!r} is not a 'UN number / IMO class' pair - "
                            f"it has {len(parts)} part(s), not 2")
    # clean_text maps the file's '-' placeholder to None (LPG Mix has no UN).
    return clean_text(parts[0]), clean_text(parts[1]), None


def to_float(text: str) -> Optional[float]:
    try:
        return float(text)
    except ValueError:
        return None


def read_table(path: Path) -> Tuple[List[dict], List[str]]:
    """Parse the CSV into cargo rows. Nothing is written unless errors is empty."""
    with path.open(newline="", encoding="utf-8-sig") as fh:
        grid = list(csv.reader(fh))
    errors: List[str] = []
    if not grid:
        return [], [f"{path.name} is empty"]

    header = [clean_text(c) or "" for c in (grid[0] + [""] * len(HEADER))[:len(HEADER)]]
    if header != HEADER:
        errors.append(f"header row is {header!r}, expected {HEADER!r} - is this "
                      f"the Cargo Data sheet?")
        return [], errors

    rows: List[dict] = []
    seen: Dict[str, int] = {}

    for i, raw in enumerate(grid[1:], start=2):
        cells = [clean_text(c) for c in (list(raw) + [None] * len(HEADER))[:len(HEADER)]]
        name, formula, weight, un_class, density = cells
        if not any(cells):
            continue
        if name is None:
            errors.append(f"row {i}: properties with no cargo name")
            continue
        if name in seen:
            errors.append(f"row {i}: duplicate cargo {name!r} (also row {seen[name]})")
            continue
        seen[name] = i

        values: List[dict] = []

        def add(field: str, value: str, value_type: str = "text",
                normalized: Optional[float] = None, unit: Optional[str] = None,
                notes: Optional[List[str]] = None) -> None:
            values.append({"field": field, "value": value, "value_type": value_type,
                           "normalized_value": normalized, "unit": unit,
                           "notes": list(notes or []), "flagged": False})

        if formula is not None:
            notes = [FORMULA_NAME_NOTE] if formula.lower() == name.lower() else []
            add("molecular_formula", formula, notes=notes)

        if weight is not None:
            mw = to_float(weight)
            if mw is None:
                errors.append(f"row {i}: {name!r} has molecular weight {weight!r}, "
                              f"which is not a number")
            else:
                add("molecular_weight_g_mol", weight, "number", mw, "g/mol", [MW_NOTE])

        if un_class is not None:
            un, imo, error = split_un_class(un_class)
            if error:
                errors.append(f"row {i}: {name!r} has un_no_imo_class {error}")
            else:
                printed = (f"The source prints {un_class!r} in one column headed "
                           f"'un_no_imo_class'; the UN number and the IMO class "
                           f"are split into their own fields.")
                if un is not None:
                    add("UN_NUMBER", un, notes=[printed])
                if imo is not None:
                    note = printed if un is not None else (
                        f"The source prints {un_class!r} in one column headed "
                        f"'un_no_imo_class': it gives this product a class but no "
                        f"UN number, so only the class is stored.")
                    add("imo_class", imo, notes=[note])

        if density is not None:
            value = to_float(density)
            if value is None:
                errors.append(f"row {i}: {name!r} has density {density!r}, which "
                              f"is not a number")
            else:
                notes = [DENSITY_NOTE]
                flagged = value > DENSITY_CEILING
                if flagged:
                    notes.append(DENSITY_IMPOSSIBLE_NOTE.format(value=density))
                add("density", density, "number", value, "kg/l", notes)
                values[-1]["flagged"] = flagged

        rows.append({"gas_name": name, "line": i, "values": values})

    if not rows:
        errors.append(f"{path.name} has a header but no cargo rows")
        return rows, errors

    flag_weight_mismatches(rows)
    return rows, errors


def flag_weight_mismatches(rows: List[dict]) -> None:
    """Flag rows that share a formula but not its molecular weight.

    Isomers have the same molecular weight by definition, so a disagreement
    inside one file is the file contradicting itself - something this loader
    can see without appealing to anything outside the source.
    """
    by_formula: Dict[str, List[Tuple[str, dict]]] = defaultdict(list)
    for r in rows:
        picked = {v["field"]: v for v in r["values"]}
        formula, weight = picked.get("molecular_formula"), picked.get("molecular_weight_g_mol")
        if formula and weight:
            by_formula[formula["value"].upper()].append((r["gas_name"], weight))

    for formula, entries in by_formula.items():
        if len({v["normalized_value"] for _, v in entries}) < 2:
            continue
        for name, value in entries:
            others = [(n, v) for n, v in entries
                      if v["normalized_value"] != value["normalized_value"]]
            value["flagged"] = True
            value["notes"].append(MW_MISMATCH_NOTE.format(
                others=", ".join(n for n, _ in others),
                is_are="is" if len(others) == 1 else "are",
                formula=formula,
                values=", ".join(v["value"] for _, v in others)))


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
    per_field = Counter(v["field"] for r in rows for v in r["values"])
    log.info("%d cargo(es), %d property value(s)", len(rows),
             sum(per_field.values()))
    for field in FIELDS:
        log.info("    %-24s %2d value(s)", field, per_field.get(field, 0))

    bare = [r["gas_name"] for r in rows if not r["values"]]
    if bare:
        log.info("cargo(es) the source lists with no property at all: %s",
                 ", ".join(bare))

    flagged = [(r["gas_name"], v) for r in rows for v in r["values"] if v["flagged"]]
    if flagged:
        log.warning("%d figure(s) the source contradicts itself on - loaded as "
                    "printed, flagged in notes, NOT corrected:", len(flagged))
        for name, v in flagged:
            log.warning("    %-14s %-24s %s", name, v["field"], v["value"])


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
            log.info("Source id=%s (%r)", source_id, SOURCE_NAME)

            added = ensure_field_definitions(cur, only=FIELDS)
            log.info("field_definitions: %d created, %d already present",
                     added, len(FIELDS) - added)

            created = written = 0
            for r in rows:
                gas_id, is_new = upsert_gas(cur, source_id, r["gas_name"])
                created += is_new
                for v in r["values"]:
                    upsert_property(
                        cur, gas_id, source_id, v["field"],
                        value=v["value"],
                        normalized_value=v["normalized_value"],
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
