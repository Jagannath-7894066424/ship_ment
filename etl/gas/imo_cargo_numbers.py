#!/usr/bin/env python3
"""
Load the "IMO cargo numbers" table into cargo_gas, cargo_gas_property_values
and synonyms + cargo_gas_synonym.

SOURCE
------
"IMO Cargo.XLSX" (source.json, category 'gas'). The SIXTH gas source, after
VAPDENS.XLSX, Products_info, Properties of Gases Doc, Cargo Data and
Thermodynamic Data_Properties - and the first of them that is REGULATORY rather
than physical. It measures nothing: every column is an identifier assigned by a
regulation or a guide (IMO hazard class, UN transport number, MFAG table), so
it is ranked for `rank_regulatory` and given no physical rank at all.

It overlaps the other five by name; nothing is merged. cargo_gas is keyed
(gas_name, source_id), so each source keeps its own rows and a disagreement
between them stays visible - and there is one worth seeing here: this file puts
vinyl chloride in class 2, where Cargo Data puts it in class 3.

INPUT
-----
"IMO cargo numbers.xls", one populated sheet (Sheet1; Sheet2 and Sheet3 are
empty) holding 16 cargoes under a TWO-ROW header, because two headings are
split across both rows:

    Compound | Synonyms | Class [/ IMO] | UN No. | MFAG [/ No.]

The sheet is indented by one blank spacer column and padded with blank rows
above and below the header. Both header rows are matched exactly and the
spacer column is required to be empty - a re-export that shifts or reorders
the columns stops the loader rather than loading a UN number into an MFAG
table number.

FIELDS
------
Two columns reuse fields the other gas branches already define, so the same
identifier lands in the same field whichever source it came from:

  * "Class IMO"  -> imo_class, the same field Cargo Data writes, and for the
    same reason: NOT imdg_class. A source printing a class beside a UN number
    does not always agree with the IMDG class for that number, and equating the
    two would assert a regulatory fact this file never made.
  * "UN No."     -> UN_NUMBER.

One is new (see etl/gas/_gas.py):

  * "MFAG No."   -> mfag_number. The table number in the IMO Medical First Aid
    Guide - a pointer to a page of emergency treatment, not a measurement.

THE FOURTH COLUMN IS NOT A PROPERTY
-----------------------------------
"Synonyms" does not go to cargo_gas_property_values at all. A name is not a
property of a cargo: it belongs in the shared `synonyms` table, reached from
cargo_gas through cargo_gas_synonym (prisma/migrations/
20260904000000_cargo_gas_synonym - the gas twin of cargo_synonym and
crude_oil_synonym, added for this source). Three things follow from that:

  * One row of text serves all three branches. `synonyms` is keyed on
    normalized_text, so if the chemical branch has already published "VCM",
    this source links to that row instead of storing the string again.
  * "which cargo is 'BUT'?" becomes an indexed join rather than a scan of
    property text.
  * The relationship is attributable. The LINK carries its own source_id, so
    the fact that IMO Cargo.XLSX calls this gas "BUT" is recorded separately
    from whoever first published the text "BUT" into `synonyms`.

relationship_type is "abbreviation", not "synonym". The heading says Synonyms,
but AMA, BDI and VCM are contractions of the compound name rather than other
names for it, and recording which kind of name this is stops a three-letter
code being served back as the cargo's name.

Two of the codes name more than one cargo within this one source - BUT is given
to both Butane-n and Butane-i, PPL to Propylene and Propylene (refinery) - so a
lookup on them cannot resolve to a single gas. The link is written on every
cargo the source gives the code to and ambiguity_flag is set on each, rather
than the name being attached to one of them by guesswork.

EVERYTHING IS TEXT
------------------
All three property values are numerals that are not numbers: 1005 is a name for ammonia,
310 is a name for a table, and 2.1 is a class label whose two halves are read
separately, not a quantity 2.1. None of them is stored with a normalized_value
or a unit - averaging or comparing them would be meaningless, and leaving the
numeric columns NULL is what says so.

The spreadsheet stores them as numbers, so the loader renders each cell back to
what the sheet displays: an integral value loses its ".0" (1005.0 -> "1005")
and a fractional one keeps its digits (2.1 -> "2.1").

WHAT THE FILE REPEATS, AND WHY IT IS NOT AN ERROR
--------------------------------------------------
Several rows share a value, and a shared identifier here is normal rather than
a contradiction, so a repeat is recorded on the value and listed in the run
log, never flagged as a fault.

Only two columns get that note. A hazard class and an MFAG table number are
CATEGORY labels - class 2.1 covers every flammable gas here and MFAG 310 names
one treatment regime for all of them - so being shared is their ordinary state
and saying so on each value would be noise. A synonym and a UN number read as
identifiers of a particular cargo, so a second row carrying the same one is
worth seeing, and the note names the rows that share it so it can be checked:

  * Butane-n and Butane-i are given the same UN number (1011) and the same
    synonym (BUT), though they are distinct isomers listed on distinct rows.
  * Propylene and "Propylene (refinery)" are given identical values throughout.
    The grade is part of the name here, so both rows are kept: unlike a
    reference marker, "(refinery)" is what the cargo is called.

THE PARENT CLASS
----------------
Butadiene and Vinyl Chloride are given class "2" where every other classified
row carries a division (2.1, 2.2, 2.3). Class 2 is the parent of those
divisions, so the cell is less specific rather than wrong. It is stored exactly
as printed and noted; refining it to a division would be asserting a
classification the source declined to make.

NAMES ARE VERBATIM
------------------
gas_name is what the source prints, including "Nitrogene", "Carbondioxide" and
"Carbonmonoxide". Names are source-scoped; correcting them here would make this
source's rows unfindable from the file they came from. Carbonmonoxide is a name
with no other cell filled in at all - the cargo_gas row is still created,
because the source listing a cargo is itself the fact this table records.

IDEMPOTENCY
-----------
Upsert on (gas_name, source_id), (cargo_gas_id, source_id, field_name) and
(cargo_gas_id, synonym_id). The whole sheet is validated before anything is
written; one transaction.

Usage:
    python3 etl/gas/imo_cargo_numbers.py
    python3 etl/gas/imo_cargo_numbers.py --dry-run
    python3 etl/gas/imo_cargo_numbers.py "/path/to/IMO cargo numbers.xls"
"""

import argparse
import logging
import math
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

import pandas as pd
import psycopg2
from dotenv import load_dotenv

# Loaders are run as scripts (python3 etl/<branch>/<file>.py), so only their
# own directory is on sys.path. Add the shared helpers.
_ETL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ETL_ROOT / "common"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _gas import (clean_text, ensure_field_definitions, link_synonym,  # noqa: E402
                  upsert_gas, upsert_property, upsert_synonym)
from _paths import input_file  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("gas_imo_cargo_numbers")

SOURCE_NAME = "IMO Cargo.XLSX"
ENTERED_BY = "imo_cargo_numbers.py"
DEFAULT_FILE = input_file("IMO cargo numbers.xls")
DEFAULT_SHEET = "Sheet1"

# The sheet is indented by one blank column; the table starts at column 1.
SPACER_COLUMNS = 1
NAME_HEADING = "Compound"


class Column(NamedTuple):
    heading: str        # header row 1, exactly as printed
    sub: str            # header row 2, exactly as printed ('' when not split)
    label: str          # what to call it in a log line
    field: str          # field_definitions.field_name
    note: str           # what the column heading means, recorded on every value
    # Whether a second row carrying the same value is worth recording. False for
    # the two columns that are category labels: a hazard class and an MFAG table
    # cover many cargoes by design, so "shared" is their normal state and saying
    # so on every value would be noise, not information.
    repeat_notable: bool
    # Where the column lands. PROPERTY means cargo_gas_property_values, keyed on
    # `field`; SYNONYM means the shared `synonyms` table via cargo_gas_synonym,
    # and `field` is then the relationship_type rather than a field_name.
    target: str


PROPERTY = "property"
SYNONYM = "synonym"

COLUMNS: List[Column] = [
    # relationship_type, not a field_name: this column goes to the synonyms
    # table. "abbreviation" is what the cells are - the heading says Synonyms,
    # but AMA, BDI and VCM are contractions of the compound name rather than other
    # names for it - and recording the kind is what stops a three-letter code
    # being served back as the cargo's name.
    Column("Synonyms", "", "Synonyms", "abbreviation",
           "The source heads this column 'Synonyms' but fills it with a short "
           "code (AMA, BDI, RAF - 1) rather than an alternative chemical name, "
           "so this is the abbreviation this source uses for the cargo, not a "
           "name the cargo is otherwise known by. Linked verbatim.",
           True, SYNONYM),
    Column("Class", "IMO", "Class IMO", "imo_class",
           "Hazard class as the source's own 'Class IMO' column prints it. "
           "Stored verbatim in `imo_class` rather than `imdg_class`: this file "
           "gives a class beside a UN number without claiming it is the IMDG "
           "class for that number.", False, PROPERTY),
    Column("UN No.", "", "UN No.", "UN_NUMBER",
           "UN transport number, per the source's 'UN No.' column. A name for "
           "the substance in transport, not a quantity, so it is stored as "
           "text with no normalized value.", True, PROPERTY),
    Column("MFAG", "No.", "MFAG No.", "mfag_number",
           "Table number in the IMO Medical First Aid Guide, per the source's "
           "'MFAG No.' column: a pointer to a page of emergency treatment for "
           "this cargo, not a measurement.", False, PROPERTY),
]

# field_definitions this loader writes, in the order they are reported. The
# Synonyms column is absent on purpose: it is not a property of the cargo.
FIELDS = [c.field for c in COLUMNS if c.target == PROPERTY]

# Divisions of IMO class 2 that this file uses. A bare "2" is their parent.
PARENT_CLASS = "2"
PARENT_CLASS_NOTE = (
    "The source prints the parent class {value!r} here while giving a division "
    "({divisions}) on other rows of the same column. Less specific than those "
    "rows rather than inconsistent with them; stored exactly as printed, and "
    "NOT refined to a division - choosing one would assert a classification "
    "this source declined to make.")
SHARED_NOTE = (
    "The source gives this same {label} to {others} on {another} row of this "
    "file. Recorded, not flagged: an identifier of this kind can legitimately "
    "cover more than one cargo, and it is stored exactly as printed.")
# The same repeat, in the synonyms table, is what ambiguity_flag is for: a
# lookup on the name cannot pick one of the cargoes that carry it.
AMBIGUOUS_NOTE = (
    "AMBIGUOUS WITHIN THIS SOURCE: the same source also applies this name to "
    "{others}, so a lookup on it cannot resolve to a single cargo. The link is "
    "kept on every cargo the source gives it to and flagged, rather than "
    "attached to one of them by guesswork.")


def collapse(value: Any) -> str:
    """Trim a cell and collapse its whitespace. Header cells hold line breaks.

    An empty cell reaches us as NaN, since pandas reads the sheet's mixed
    columns as floats; str() would turn that into the word "nan", so it is
    mapped to the empty string first.
    """
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return re.sub(r"\s+", " ", str(value).replace("\n", " ")).strip()


def cell_text(value: Any) -> Optional[str]:
    """One sheet cell -> the text the sheet displays, or None when empty.

    The workbook stores every column of this table as a number even though none
    of them is one, so an integral float is rendered without its ".0": the UN
    number is 1005, not 1005.0.
    """
    if value is None:
        return None
    if isinstance(value, float):
        if math.isnan(value):
            return None
        if value.is_integer():
            return str(int(value))
        return repr(value)
    if isinstance(value, int):
        return str(value)
    return clean_text(value)


def find_header(grid: List[List[Any]]) -> Tuple[Optional[int], List[str]]:
    """Locate the two-row header. Returns (row index of its first row, errors)."""
    errors: List[str] = []
    for i, row in enumerate(grid):
        cells = [collapse(c) for c in row]
        if NAME_HEADING in cells:
            if cells.index(NAME_HEADING) != SPACER_COLUMNS:
                errors.append(f"row {i + 1}: {NAME_HEADING!r} is in column "
                              f"{cells.index(NAME_HEADING)}, expected column "
                              f"{SPACER_COLUMNS} - the sheet's layout has moved")
                return None, errors
            return i, errors
    errors.append(f"no header row containing {NAME_HEADING!r} - is this the "
                  f"'IMO cargo numbers' sheet?")
    return None, errors


def check_header(grid: List[List[Any]], top: int) -> List[str]:
    """Match both header rows and the blank spacer column exactly."""
    errors: List[str] = []
    width = SPACER_COLUMNS + 1 + len(COLUMNS)

    want_top = [""] * SPACER_COLUMNS + [NAME_HEADING] + [c.heading for c in COLUMNS]
    want_sub = [""] * SPACER_COLUMNS + [""] + [c.sub for c in COLUMNS]
    got_top = [collapse(c) for c in (grid[top] + [""] * width)[:width]]
    got_sub = ([collapse(c) for c in (grid[top + 1] + [""] * width)[:width]]
               if top + 1 < len(grid) else [""] * width)
    if got_top != want_top or got_sub != want_sub:
        errors.append(f"header rows are {got_top!r} / {got_sub!r}, expected "
                      f"{want_top!r} / {want_sub!r} - is this the "
                      f"'IMO cargo numbers' sheet?")
        return errors

    # Everything left of the table must stay empty, or the columns below the
    # header are not the columns the header names.
    for i, row in enumerate(grid, start=1):
        for col in range(SPACER_COLUMNS):
            if collapse(row[col] if col < len(row) else ""):
                errors.append(f"row {i}: spacer column {col} is not empty "
                              f"({collapse(row[col])!r}) - the sheet's layout "
                              f"has moved")
    return errors


def read_table(path: Path, sheet: str) -> Tuple[List[dict], List[str]]:
    """Parse the sheet into cargo rows. Nothing is written unless errors is empty."""
    df = pd.read_excel(path, sheet_name=sheet, header=None)
    grid = [list(r) for r in df.itertuples(index=False, name=None)]
    if not grid:
        return [], [f"{path.name} sheet {sheet!r} is empty"]

    top, errors = find_header(grid)
    if top is None:
        return [], errors
    errors += check_header(grid, top)
    if errors:
        return [], errors

    width = SPACER_COLUMNS + 1 + len(COLUMNS)
    rows: List[dict] = []
    seen: Dict[str, int] = {}

    for i, raw in enumerate(grid[top + 2:], start=top + 3):
        cells = [cell_text(c) for c in (list(raw) + [None] * width)[:width]]
        name, properties = cells[SPACER_COLUMNS], cells[SPACER_COLUMNS + 1:]
        if not any(c is not None for c in cells):
            continue
        if name is None:
            errors.append(f"row {i}: values with no compound name")
            continue
        if name in seen:
            errors.append(f"row {i}: duplicate compound {name!r} "
                          f"(also row {seen[name]})")
            continue
        seen[name] = i

        values: List[dict] = []
        synonyms: List[dict] = []
        for col, text in zip(COLUMNS, properties):
            if text is None:
                continue
            entry = {"field": col.field, "column": col.label, "value": text,
                     "notes": [col.note]}
            (synonyms if col.target == SYNONYM else values).append(entry)

        rows.append({"gas_name": name, "line": i, "values": values,
                     "synonyms": synonyms})

    if not rows:
        errors.append(f"{path.name} sheet {sheet!r} has a header but no cargo rows")
        return rows, errors

    note_parent_class(rows)
    note_shared_values(rows)
    return rows, errors


def entries(row: dict) -> List[dict]:
    """Every cell parsed from one row, whichever table it is bound for."""
    return row["values"] + row["synonyms"]


def note_parent_class(rows: List[dict]) -> None:
    """Note the rows given the bare parent class where others carry a division."""
    divisions = sorted({v["value"] for r in rows for v in r["values"]
                        if v["field"] == "imo_class" and v["value"] != PARENT_CLASS})
    if not divisions:
        return
    for r in rows:
        for v in r["values"]:
            if v["field"] == "imo_class" and v["value"] == PARENT_CLASS:
                v["notes"].append(PARENT_CLASS_NOTE.format(
                    value=v["value"], divisions=", ".join(divisions)))


def note_shared_values(rows: List[dict]) -> None:
    """Record, on each value a second row repeats, which rows share it."""
    notable = {c.field for c in COLUMNS if c.repeat_notable}
    shared: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    for r in rows:
        for v in entries(r):
            shared[(v["field"], v["value"])].append(r["gas_name"])

    for r in rows:
        for v in entries(r):
            if v["field"] not in notable:
                continue
            names = [n for n in shared[(v["field"], v["value"])] if n != r["gas_name"]]
            if not names:
                continue
            v["shared_with"] = names
            if v in r["synonyms"]:
                v["notes"].append(AMBIGUOUS_NOTE.format(others=", ".join(names)))
            else:
                v["notes"].append(SHARED_NOTE.format(
                    label=v["column"], others=", ".join(names),
                    another="another" if len(names) == 1 else f"{len(names)} other"))


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
    per_field = Counter(v["field"] for r in rows for v in entries(r))
    n_values = sum(len(r["values"]) for r in rows)
    n_synonyms = sum(len(r["synonyms"]) for r in rows)
    log.info("%d cargo(es), %d property value(s), %d synonym link(s)",
             len(rows), n_values, n_synonyms)
    for col in COLUMNS:
        table = ("cargo_gas_property_values" if col.target == PROPERTY
                 else "synonyms + cargo_gas_synonym")
        log.info("    %-12s -> %-12s %2d  -> %s", col.label, col.field,
                 per_field.get(col.field, 0), table)

    bare = [r["gas_name"] for r in rows if not entries(r)]
    if bare:
        log.info("cargo(es) the source lists with no other cell filled in: %s",
                 ", ".join(bare))

    missing = [(r["gas_name"], col.label) for r in rows for col in COLUMNS
               if entries(r) and not any(v["field"] == col.field for v in entries(r))]
    if missing:
        log.info("cell(s) the source leaves blank on a row it otherwise fills: %d",
                 len(missing))
        for name, label in missing:
            log.info("    %-22s %s", name, label)

    repeats = [(r["gas_name"], v) for r in rows for v in r["values"] if v.get("shared_with")]
    if repeats:
        log.info("value(s) the source gives to more than one cargo - recorded on "
                 "the value, NOT flagged as faults: %d", len(repeats))
        for name, v in repeats:
            log.info("    %-22s %-12s %-6s also: %s", name, v["column"], v["value"],
                     ", ".join(v["shared_with"]))

    ambiguous = [(r["gas_name"], v) for r in rows for v in r["synonyms"]
                 if v.get("shared_with")]
    if ambiguous:
        log.info("synonym(s) this source gives to more than one cargo - linked to "
                 "each of them with ambiguity_flag set: %d", len(ambiguous))
        for name, v in ambiguous:
            log.info("    %-22s %-8s also: %s", name, v["value"],
                     ", ".join(v["shared_with"]))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", default=str(DEFAULT_FILE))
    ap.add_argument("--sheet", default=DEFAULT_SHEET, help="sheet to load")
    ap.add_argument("--dry-run", action="store_true", help="parse and report, write nothing")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.is_file():
        sys.exit(f"Error: file not found: {path}")

    rows, errors = read_table(path, args.sheet)
    if errors:
        log.error("%d validation error(s) - nothing was imported:", len(errors))
        for e in errors:
            log.error("    %s", e)
        return 1

    log.info("%s [%s]", path.name, args.sheet)
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

            created = written = linked = names = 0
            # One cache for the whole run: the file repeats codes (BUT, PPL)
            # across rows, and they must resolve to the SAME synonyms row.
            cache: Dict[str, int] = {}
            for r in rows:
                gas_id, is_new = upsert_gas(cur, source_id, r["gas_name"])
                created += is_new
                for v in r["values"]:
                    # Every property column here is an identifier, not a
                    # measurement: text, with no normalized value and no unit.
                    upsert_property(
                        cur, gas_id, source_id, v["field"],
                        value=v["value"], value_type="text",
                        entered_by=ENTERED_BY,
                        notes=" ".join(v["notes"]) or None,
                    )
                    written += 1

                for v in r["synonyms"]:
                    synonym_id, is_new_name = upsert_synonym(
                        cur, source_id, v["value"], cache)
                    names += is_new_name
                    link_synonym(
                        cur, gas_id, synonym_id, source_id,
                        relationship_type=v["field"],
                        ambiguity_flag=bool(v.get("shared_with")),
                        notes=" ".join(v["notes"]) or None,
                    )
                    linked += 1

        conn.commit()
        distinct = len(cache)
        log.info("✓ Committed. cargo_gas: %d (%d created this run) | "
                 "cargo_gas_property_values: %d | cargo_gas_synonym: %d link(s) "
                 "over %d distinct name(s), of which %d were new to the shared "
                 "`synonyms` table and %d already existed there",
                 len(rows), created, written, linked, distinct, names,
                 distinct - names)
        return 0
    except Exception:
        conn.rollback()
        log.exception("Import failed - rolled back, nothing written.")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
