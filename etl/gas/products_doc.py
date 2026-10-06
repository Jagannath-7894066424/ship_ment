#!/usr/bin/env python3
"""
Load the "PROPERTIES OF PRODUCTS" table into cargo_gas,
cargo_gas_property_values and synonyms + cargo_gas_synonym.

SOURCE
------
"Products.doc" (source.json, category 'gas'), a one-page Word document titled
PROPERTIES OF PRODUCTS, last saved in September 1998. The SEVENTH gas source
and the broadest of them: it is the only one that gives identity, regulatory
status, physical properties, a health limit and alternative names in a single
table, which is why it is ranked for both rank_regulatory and rank_physical.

It overlaps every other gas source by name; nothing is merged. cargo_gas is
keyed (gas_name, source_id), so each source keeps its own rows and a
disagreement between them stays visible - and this file disagrees with its
neighbours in ways worth seeing. It gives butadiene class 2.0 where Cargo Data
gives 2 and IMO Cargo.XLSX gives 2; it gives propylene oxide class 3.1; and it
gives iso-Butane UN 1011, the same number it gives n-Butane.

INPUT
-----
The document is a .doc, which no loader can read, so two CSV exports of its two
tables are committed alongside it:

  * "Products.doc - PROPERTIES OF PRODUCTS.csv" - the main table: 16 products
    and thirteen columns, header matched exactly INCLUDING the document's own
    misspelling "Synonims" and its two Symbol-font degree signs.
  * "Products.doc - mixtures.csv" - the second table, which the document prints
    with no header at all: two C4 streams and the composition of each. The
    export adds "Product,Composition" so the file is self-describing.

Both are re-exported by converting the .doc with LibreOffice; the header match
is what catches a re-export that came out different.

FIELDS
------
Ten columns reuse fields the other branches already define, so the same
measurement lands in the same field whichever source it came from:
imo_class, UN_NUMBER, mfag_number, molecular_weight_g_mol, molecular_formula,
flammable_limits, flash_point_c, boiling_point_c, tlv_twa_ppm and odour_limit.

Two are new (see etl/gas/_gas.py):

  * "Ideal Vapour Density (kg/m³)" -> vapour_density_kg_m3. An ABSOLUTE vapour
    density, which is not what either existing vapour-density field holds:
    RELATIVE_VAPOUR_DENSITY is the dimensionless ratio to air, and the chemical
    branch's `vapour_density` carries no unit and no statement of basis.
  * The mixtures table's composition -> composition.

The thirteenth column, "Synonims", is not a property at all: names go to the
shared `synonyms` table through cargo_gas_synonym, the route added for IMO
Cargo.XLSX. See THE NAMES COLUMN below.

THE LIQUID DENSITY COLUMN IS IMPOSSIBLE AS PRINTED
---------------------------------------------------
"Ideal Liquid Density (kg/m³)" gives ammonia 0.6818 and butadiene 0.6503. Read
in the unit the column names, those are the densities of a near-vacuum: liquid
ammonia is about 682 kg/m³, a thousand times what is printed. The figures are
kg/LITRE under a kg/m³ heading.

That is not an outside opinion about the document. The file proves it against
itself: the column beside it gives the IDEAL VAPOUR density of the same product
in the same stated unit, and every row that fills in both puts the liquid BELOW
its own vapour - ammonia 0.6818 liquid against 0.8541 vapour, butadiene 0.6503
against 2.4208. A liquid is never lighter than the vapour it boils into. Six
rows say so.

The loader does not convert them. Each value is stored exactly as printed, with
the unit exactly as the column heads it, and:

  * `notes` records what is wrong and how the file itself shows it;
  * is_winning is FALSE and conflict_flag is TRUE, so a query asking for this
    product's density does not get handed a figure that is wrong by three
    orders of magnitude;
  * the run log lists every one of them.

Multiplying by 1000 would put a number in the database that the source never
printed, and dropping the column would hide a defect a reader should see. The
flags are the part of the schema that exists for exactly this.

WHAT THE FILE SAYS IN WORDS
---------------------------
Four cells are not figures, and three of them are statements rather than gaps:

  * 'nil' in Flammable Limits and Flash Point, on carbon dioxide, nitrogen and
    oxygen. Those three do not burn, so the source is saying the property does
    not exist for them - not that it failed to look it up. Stored verbatim as
    text with that reading recorded.
  * 'stench' in Odour Threshold, on butene and propane: the source answering
    "how much before you smell it" with a description instead of a figure.
    Stored verbatim; `odour_limit` is defined to allow exactly this.
  * '>1' in Odour Threshold, on isoprene: a lower bound with no upper. Stored
    as printed with normalized_min set and normalized_max left NULL.
  * 'n/a' in TLV, on isoprene. THAT one is a gap, and no row is written.

DECIMAL COMMAS
--------------
The flammable-limits column is written with European decimal commas - '1,5-9'
is 1.5 to 9 percent, and '1-12,5' is 1 to 12.5. The raw string is stored in
`value` exactly as printed and the two ends are normalized into
normalized_min / normalized_max, which is what `flammable_limits` is for. A
comma is only ever read as a decimal point INSIDE a number; the loader fails
rather than guessing if a cell does not parse as a range.

THE NAMES COLUMN
----------------
"Synonims" holds a comma-separated list per product, mixing this source's own
three-letter codes with real alternative chemical names: "AMA, NH3, Ammonia
gas, Anhydrous ammonia". Every entry goes to `synonyms` and is linked through
cargo_gas_synonym with source_id set, so the link records that Products.doc
applies this name to this cargo while the `synonyms` row keeps whoever first
published the text.

relationship_type is 'common' rather than IMO Cargo.XLSX's 'abbreviation':
that source's column was codes only, this one's is mixed, and calling the whole
list abbreviations would be wrong about most of it.

Two details the splitting has to get right, and one it cannot:

  * A comma inside a chemical name is not a separator. Isoprene's list ends
    "2 methyl 1,3 butadiene", where the comma is a locant. The loader splits on
    a comma only when a digit does not immediately follow it - the locant comma
    never has a space after it, and a separator comma in this file always does
    or is followed by a letter.
  * The source repeats a name across products: 'BUT' is not reused here, but
    codes and names that do repeat are linked to every product carrying them
    and flagged ambiguous, the same rule IMO Cargo.XLSX uses.
  * Butene's list reads "BTN, Butenes, iso-Butene iso-Butylene" - two names
    with the separator missing between them. There is no way to tell a missing
    comma from a two-word name ("Ammonia gas", "Methyl ethyl methane") without
    guessing, so the cell is stored as the source prints it and reported in the
    run log for a human to decide.

THE SECOND TABLE
----------------
The document ends with two C4 refinery streams, RICH C4 and SPENT C4, given
only as compositions. They are products the source lists, so they get cargo_gas
rows and a `composition` value each. SPENT C4's percentages total 83, not 100 -
the source names no balance - and that is recorded on the value rather than
being filled in.

NAMES ARE VERBATIM
------------------
gas_name is what the source prints, including "Carbondioxide", "Synonims" and
the bare "Raffinate" that IMO Cargo.XLSX splits into Raffinate 1 and 2. Names
are source-scoped; correcting them here would make this source's rows
unfindable from the file they came from.

IDEMPOTENCY
-----------
Upsert on (gas_name, source_id), (cargo_gas_id, source_id, field_name) and
(cargo_gas_id, synonym_id). Both files are validated before anything is
written; one transaction.

Usage:
    python3 etl/gas/products_doc.py
    python3 etl/gas/products_doc.py --dry-run
    python3 etl/gas/products_doc.py "/path/to/...PROPERTIES OF PRODUCTS.csv"
"""

import argparse
import csv
import logging
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

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
log = logging.getLogger("gas_products_doc")

SOURCE_NAME = "Products.doc"
ENTERED_BY = "products_doc.py"
DEFAULT_FILE = input_file("Products.doc - PROPERTIES OF PRODUCTS.csv")
DEFAULT_MIXTURES = input_file("Products.doc - mixtures.csv")

NAME_HEADING = "Product"
# Names are not properties: relationship_type for the link, not a field_name.
SYNONYM_RELATIONSHIP = "common"

# The mixtures table's header, added by the export (the document prints none).
MIXTURES_HEADER = ["Product", "Composition"]


class Column(NamedTuple):
    heading: str          # header cell, exactly as the document prints it
    label: str            # what to call it in a log line
    field: str            # field_definitions.field_name
    unit: Optional[str]   # unit as published, NULL for text and for ratios
    kind: str             # how to read the cell: see parse_cell
    note: Optional[str]   # what the column heading says, put on every value


# How a column's cells are read.
TEXT = "text"        # an identifier or a word: stored verbatim, never a number
NUMBER = "number"    # a signed decimal
RANGE = "range"      # 'a-b', with European decimal commas

COLUMNS: List[Column] = [
    Column("Class IMO", "Class IMO", "imo_class", None, TEXT,
           "Hazard class as the source's own 'Class IMO' column prints it, "
           "including its trailing zero ('2.0'). Stored verbatim in "
           "`imo_class` rather than `imdg_class`: this file gives a class "
           "beside a UN number without claiming it is the IMDG class for it."),
    Column("UN No.", "UN No.", "UN_NUMBER", None, TEXT,
           "UN transport number, per the source's 'UN No.' column. A name for "
           "the substance in transport, not a quantity, so it is stored as "
           "text with no normalized value."),
    Column("MFAG Table", "MFAG Table", "mfag_number", None, TEXT,
           "Table number in the IMO Medical First Aid Guide, per the source's "
           "'MFAG Table' column: a pointer to a page of emergency treatment, "
           "not a measurement."),
    Column("Molecular Mass (g/mole)", "Molecular Mass", "molecular_weight_g_mol",
           "g/mol", NUMBER,
           "Per the column heading, in g/mole - the same number as the field's "
           "canonical g/mol."),
    Column("Formula", "Formula", "molecular_formula", None, TEXT,
           "Chemical formula as the source prints it. The document sets the "
           "counts as subscripts; they are stored inline (NH3, C4H10)."),
    Column("Flammable Limits (vol.%)", "Flammable Limits", "flammable_limits",
           "% vol", RANGE,
           "Flammable range in air, in % by volume per the column heading. The "
           "source writes its decimals with commas ('1,5-9' is 1.5 to 9); the "
           "string is stored exactly as printed and the two ends are "
           "normalized into normalized_min / normalized_max."),
    Column("Flash Point (°C)", "Flash Point", "flash_point_c", "°C", NUMBER,
           "In °C per the column heading. The source does not say whether it "
           "is a closed- or open-cup figure."),
    Column("Boiling Point (°C)", "Boiling Point", "boiling_point_c", "°C", NUMBER,
           "In °C per the column heading. The source does not state the "
           "pressure; for a table of liquefied gases it is atmospheric, but "
           "that is not written down here because the source does not say it."),
    Column("Ideal Liquid Density (kg/m³)", "Ideal Liquid Density", "density",
           "kg/m³", NUMBER,
           "The source heads this column 'Ideal Liquid Density (kg/m³)'. The "
           "unit is recorded as the column prints it - see the impossibility "
           "note on this value."),
    Column("Ideal Vapour Density (kg/m³)", "Ideal Vapour Density",
           "vapour_density_kg_m3", "kg/m³", NUMBER,
           "ABSOLUTE density of the vapour in kg/m³, per the column heading - "
           "not a ratio to air. The source calls it 'ideal' and gives no "
           "state; the figures match the saturated vapour at each product's "
           "own boiling point, which the column beside it gives."),
    Column("TLV (ppm)", "TLV", "tlv_twa_ppm", "ppm", NUMBER,
           "Threshold limit value in ppm, per the column heading. The source "
           "does not name the averaging period or the authority."),
    Column("Odour Thresh. (ppm)", "Odour Thresh.", "odour_limit", "ppm", NUMBER,
           "The concentration at which the product can be smelled, in ppm per "
           "the column heading. This source measures a THRESHOLD - the point "
           "of detection - which is a lower figure than the recognition "
           "concentration other sources print in the same field."),
]

# Header row of the main table, exactly as the document prints it.
HEADER = [NAME_HEADING] + [c.heading for c in COLUMNS] + ["Synonims"]

# Fields this loader writes, in the order they are reported.
FIELDS = [c.field for c in COLUMNS] + ["composition"]

# Cells that are words rather than figures, and what the source means by them.
NIL = "nil"
NIL_NOTE = ("The source prints 'nil' here. This product does not burn, so this "
            "is the source stating the property does not exist for it rather "
            "than a figure it failed to supply; it is stored as the source's "
            "own word because an empty cell would read as 'no data'.")
WORDS_NOTE = ("The source answers this in words rather than with a figure, and "
              "the wording is kept verbatim with no normalized value.")
LOWER_BOUND_NOTE = ("The source prints a lower bound with no upper one, so "
                    "normalized_min carries the bound and normalized_max is "
                    "deliberately NULL.")

# The liquid-density column, and the proof that it cannot be read in its own
# unit. See THE LIQUID DENSITY COLUMN IS IMPOSSIBLE AS PRINTED.
DENSITY_FIELD = "density"
VAPOUR_FIELD = "vapour_density_kg_m3"
DENSITY_IMPOSSIBLE_NOTE = (
    "IMPOSSIBLE AS PRINTED, AND THE SOURCE SHOWS IT: this column is headed "
    "'Ideal Liquid Density (kg/m³)', but read in kg/m³ the figure is the "
    "density of a near-vacuum. The column beside it gives this same product's "
    "IDEAL VAPOUR density in the same stated unit as {vapour} kg/m³ - higher "
    "than the liquid, and a liquid is never lighter than the vapour it boils "
    "into. The figures are kg/LITRE under a kg/m³ heading (this one is "
    "{value} kg/l, i.e. {scaled:.1f} kg/m³). Stored exactly as printed and NOT "
    "converted; is_winning is false and conflict_flag is true so it is not "
    "served as this product's density.")

MIXTURE_NOTE = ("The document's second table gives this product only as a "
                "composition, in the source's own words and proportions. Text, "
                "not a parsed breakdown - see the `composition` field.")
MIXTURE_TOTAL_NOTE = ("The percentages the source names total {total}%, not "
                      "100%, and it names no balance. Recorded as printed; "
                      "the missing {missing}% is not attributed to anything.")

AMBIGUOUS_NOTE = (
    "AMBIGUOUS WITHIN THIS SOURCE: the same source also applies this name to "
    "{others}, so a lookup on it cannot resolve to a single cargo. The link is "
    "kept on every cargo the source gives it to and flagged, rather than "
    "attached to one of them by guesswork.")
RUN_ON_NOTE = ("The source prints this cell with a separator apparently "
               "missing - it reads as two names run together. It is stored as "
               "printed: splitting on the space would be guessing, since this "
               "column also holds genuine multi-word names.")


# _gas.clean_text is deliberately NOT used for cells in the main table: it maps
# 'nil' to None, and in this file 'nil' is a value - the source saying the
# product does not burn - not an empty cell. Only these two mean "no data": an
# empty cell, and the 'n/a' the source prints in one TLV cell.
MISSING_CELLS = {"", "n/a"}


def collapse(value: Optional[str]) -> str:
    """Trim a cell and collapse its whitespace."""
    return re.sub(r"\s+", " ", (value or "").replace("\n", " ")).strip()


def cell_of(value: Optional[str]) -> Optional[str]:
    """One cell of the main table, or None when the source states nothing."""
    text = collapse(value)
    return None if text.lower() in MISSING_CELLS else text


def to_float(token: str) -> Optional[float]:
    """A signed decimal, accepting the source's European decimal comma."""
    try:
        return float(token.strip().replace(",", ".").lstrip("+"))
    except (TypeError, ValueError):
        return None


def parse_cell(col: Column, text: str) -> Tuple[Optional[dict], Optional[str]]:
    """One cell -> (value dict, error). Returns (None, None) for a gap."""
    if col.kind == TEXT:
        return {"value_type": "text", "normalized_value": None,
                "normalized_min": None, "normalized_max": None,
                "unit": None, "notes": []}, None

    if text == NIL:
        return {"value_type": "text", "normalized_value": None,
                "normalized_min": None, "normalized_max": None,
                "unit": None, "notes": [NIL_NOTE]}, None

    if col.kind == RANGE:
        m = re.fullmatch(r"(-?[\d,.]+)\s*-\s*(-?[\d,.]+)", text)
        if not m:
            return None, (f"{col.label}: {text!r} is not an 'a-b' range and is "
                          f"not one of the words this column uses")
        lo, hi = to_float(m.group(1)), to_float(m.group(2))
        if lo is None or hi is None:
            return None, f"{col.label}: {text!r} has an end that is not a number"
        if lo > hi:
            return None, (f"{col.label}: {text!r} reads back-to-front - its "
                          f"lower end ({lo}) exceeds its upper ({hi})")
        return {"value_type": "range", "normalized_value": None,
                "normalized_min": lo, "normalized_max": hi,
                "unit": col.unit, "notes": []}, None

    # NUMBER. A bare '>n' is a bound the source states, not a failure to parse.
    m = re.fullmatch(r">\s*(-?[\d,.]+)", text)
    if m:
        bound = to_float(m.group(1))
        if bound is None:
            return None, f"{col.label}: {text!r} has no number after '>'"
        return {"value_type": "range", "normalized_value": None,
                "normalized_min": bound, "normalized_max": None,
                "unit": col.unit, "notes": [LOWER_BOUND_NOTE]}, None

    number = to_float(text)
    if number is not None:
        return {"value_type": "number", "normalized_value": number,
                "normalized_min": None, "normalized_max": None,
                "unit": col.unit, "notes": []}, None

    # Anything else is the source answering in words ('stench').
    return {"value_type": "text", "normalized_value": None,
            "normalized_min": None, "normalized_max": None,
            "unit": None, "notes": [WORDS_NOTE]}, None


def split_names(cell: str) -> List[str]:
    """Split the Synonims cell into names.

    A comma is a separator UNLESS a digit follows it immediately: the locant
    comma in '2 methyl 1,3 butadiene' never has a space after it, while every
    separator in this file is followed by a space or a letter.
    """
    return [n for n in (collapse(p) for p in re.split(r",(?!\d)", cell)) if n]


# Two adjacent words sharing this many leading characters are two variants of
# one name rather than one name of two words - which is what a missing
# separator looks like. Four is enough to clear the genuine multi-word names in
# this column ("Anhydrous ammonia" shares only "an"; "Methyl ethyl methane"
# shares nothing) while catching "iso-Butene iso-Butylene", which shares "iso-B".
RUN_ON_PREFIX = 4


def looks_run_on(name: str) -> bool:
    """True when a name reads as two names with the separator missing.

    The loader never splits on this - it cannot tell a missing comma from a
    real two-word name well enough to act - but a repeated prefix between
    adjacent words is a signal worth putting in front of a person.
    """
    words = [w.lower() for w in name.split()]
    return any(len(os.path.commonprefix([a, b])) >= RUN_ON_PREFIX
               for a, b in zip(words, words[1:]))


def read_table(path: Path) -> Tuple[List[dict], List[str]]:
    """Parse the main CSV into product rows. Nothing is written unless errors is empty."""
    with path.open(newline="", encoding="utf-8-sig") as fh:
        grid = list(csv.reader(fh))
    errors: List[str] = []
    if not grid:
        return [], [f"{path.name} is empty"]

    width = len(HEADER)
    header = [collapse(c) for c in (grid[0] + [""] * width)[:width]]
    if header != HEADER:
        errors.append(f"header row is {header!r}, expected {HEADER!r} - is this "
                      f"the 'PROPERTIES OF PRODUCTS' table?")
        return [], errors

    rows: List[dict] = []
    seen: Dict[str, int] = {}

    for i, raw in enumerate(grid[1:], start=2):
        cells = [cell_of(c) for c in (list(raw) + [None] * width)[:width]]
        name, properties, names_cell = cells[0], cells[1:-1], cells[-1]
        if not any(cells):
            continue
        if name is None:
            errors.append(f"row {i}: properties with no product name")
            continue
        if name in seen:
            errors.append(f"row {i}: duplicate product {name!r} (also row {seen[name]})")
            continue
        seen[name] = i

        values: List[dict] = []
        for col, text in zip(COLUMNS, properties):
            if text is None:
                continue
            parsed, error = parse_cell(col, text)
            if error:
                errors.append(f"row {i}: {name!r} {error}")
                continue
            notes = ([col.note] if col.note else []) + parsed.pop("notes")
            values.append({"field": col.field, "column": col.label, "value": text,
                           "notes": notes, "is_winning": True,
                           "conflict_flag": False, **parsed})

        synonyms = [{"value": n, "notes": [], "run_on": looks_run_on(n)}
                    for n in split_names(names_cell or "")]

        rows.append({"gas_name": name, "line": i, "values": values,
                     "synonyms": synonyms})

    if not rows:
        errors.append(f"{path.name} has a header but no product rows")
        return rows, errors

    flag_liquid_density(rows)
    flag_ambiguous_names(rows)
    return rows, errors


def read_mixtures(path: Path) -> Tuple[List[dict], List[str]]:
    """Parse the second table: a product name and the composition of it."""
    with path.open(newline="", encoding="utf-8-sig") as fh:
        grid = list(csv.reader(fh))
    errors: List[str] = []
    if not grid:
        return [], [f"{path.name} is empty"]

    header = [collapse(c) for c in (grid[0] + ["", ""])[:2]]
    if header != MIXTURES_HEADER:
        errors.append(f"header row is {header!r}, expected {MIXTURES_HEADER!r} - "
                      f"is this the mixtures table?")
        return [], errors

    rows: List[dict] = []
    for i, raw in enumerate(grid[1:], start=2):
        name, composition = [clean_text(c) for c in (list(raw) + [None, None])[:2]]
        if name is None and composition is None:
            continue
        if name is None or composition is None:
            errors.append(f"row {i}: the mixtures table needs both a product "
                          f"and a composition; got {name!r} / {composition!r}")
            continue

        notes = [MIXTURE_NOTE]
        # The source names its proportions; when they do not add up, say so
        # rather than inventing a balance.
        percents = [float(p) for p in re.findall(r"\((\d+(?:\.\d+)?)%\)", composition)]
        total = sum(percents)
        if percents and abs(total - 100.0) > 0.5:
            notes.append(MIXTURE_TOTAL_NOTE.format(
                total=f"{total:g}", missing=f"{100.0 - total:g}"))

        rows.append({"gas_name": name, "line": i, "synonyms": [], "values": [
            {"field": "composition", "column": "Composition", "value": composition,
             "value_type": "text", "normalized_value": None, "normalized_min": None,
             "normalized_max": None, "unit": None, "notes": notes,
             "is_winning": True, "conflict_flag": False}]})

    if not rows:
        errors.append(f"{path.name} has a header but no mixture rows")
    return rows, errors


def flag_liquid_density(rows: List[dict]) -> None:
    """Flag every liquid density the file's own vapour column contradicts.

    A liquid is never lighter than the vapour it boils into, so a row giving
    both and putting the liquid below the vapour cannot be read in the unit its
    column names. See the module docstring.
    """
    for r in rows:
        by_field = {v["field"]: v for v in r["values"]}
        liquid, vapour = by_field.get(DENSITY_FIELD), by_field.get(VAPOUR_FIELD)
        if not liquid or not vapour:
            continue
        lo, hi = liquid["normalized_value"], vapour["normalized_value"]
        if lo is None or hi is None or lo >= hi:
            continue
        liquid["is_winning"] = False
        liquid["conflict_flag"] = True
        liquid["notes"].append(DENSITY_IMPOSSIBLE_NOTE.format(
            vapour=vapour["value"], value=liquid["value"], scaled=lo * 1000))


def flag_ambiguous_names(rows: List[dict]) -> None:
    """Flag names this one source gives to more than one product."""
    shared: Dict[str, List[str]] = defaultdict(list)
    for r in rows:
        for n in r["synonyms"]:
            shared[n["value"].lower()].append(r["gas_name"])

    for r in rows:
        for n in r["synonyms"]:
            others = [g for g in shared[n["value"].lower()] if g != r["gas_name"]]
            if others:
                n["shared_with"] = others
                n["notes"].append(AMBIGUOUS_NOTE.format(others=", ".join(others)))
            if n["run_on"]:
                n["notes"].append(RUN_ON_NOTE)


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
    n_values = sum(len(r["values"]) for r in rows)
    n_names = sum(len(r["synonyms"]) for r in rows)
    log.info("%d product(s), %d property value(s), %d name(s)",
             len(rows), n_values, n_names)
    for field in FIELDS:
        log.info("    %-24s %2d value(s)", field, per_field.get(field, 0))

    worded = [(r["gas_name"], v) for r in rows for v in r["values"]
              if v["value_type"] == "text" and v["unit"] is None
              and v["field"] not in ("imo_class", "UN_NUMBER", "mfag_number",
                                     "molecular_formula", "composition")]
    if worded:
        log.info("cell(s) the source answers in words rather than figures: %d",
                 len(worded))
        for name, v in worded:
            log.info("    %-18s %-22s %s", name, v["column"], v["value"])

    bounds = [(r["gas_name"], v) for r in rows for v in r["values"]
              if v["value_type"] == "range" and v["normalized_max"] is None]
    for name, v in bounds:
        log.info("lower bound with no upper: %-18s %-22s %s", name, v["column"],
                 v["value"])

    bad = [(r["gas_name"], v) for r in rows for v in r["values"] if v["conflict_flag"]]
    if bad:
        log.warning("%d figure(s) the source's own data shows cannot be read in "
                    "the unit its column names - stored as printed, flagged, "
                    "is_winning=false, NOT converted:", len(bad))
        for name, v in bad:
            log.warning("    %-18s %-22s %s %s", name, v["column"], v["value"],
                        v["unit"])

    ambiguous = [(r["gas_name"], n) for r in rows for n in r["synonyms"]
                 if n.get("shared_with")]
    if ambiguous:
        log.info("name(s) this source gives to more than one product - linked to "
                 "each and flagged: %d", len(ambiguous))
        for name, n in ambiguous:
            log.info("    %-18s %-22s also: %s", name, n["value"],
                     ", ".join(n["shared_with"]))

    run_on = [(r["gas_name"], n["value"]) for r in rows for n in r["synonyms"]
              if n["run_on"]]
    if run_on:
        log.warning("name cell(s) that look like two names with the separator "
                    "missing - stored as printed, NOT split: %d", len(run_on))
        for name, value in run_on:
            log.warning("    %-18s %s", name, value)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", default=str(DEFAULT_FILE))
    ap.add_argument("--mixtures", default=str(DEFAULT_MIXTURES),
                    help="the document's second table (RICH C4 / SPENT C4)")
    ap.add_argument("--dry-run", action="store_true", help="parse and report, write nothing")
    args = ap.parse_args()

    path, mixtures_path = Path(args.file), Path(args.mixtures)
    for p in (path, mixtures_path):
        if not p.is_file():
            sys.exit(f"Error: file not found: {p}")

    rows, errors = read_table(path)
    mixtures, mixture_errors = read_mixtures(mixtures_path)
    errors += mixture_errors
    if errors:
        log.error("%d validation error(s) - nothing was imported:", len(errors))
        for e in errors:
            log.error("    %s", e)
        return 1

    # One product cannot be described by both tables; the document's second
    # table is a separate list, and a name in both would mean it is not.
    clash = {r["gas_name"] for r in rows} & {m["gas_name"] for m in mixtures}
    if clash:
        log.error("product(s) in both tables of the document: %s", ", ".join(sorted(clash)))
        return 1

    all_rows = rows + mixtures
    log.info("%s + %s", path.name, mixtures_path.name)
    report(all_rows)

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
            cache: Dict[str, int] = {}
            for r in all_rows:
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
                        is_winning=v["is_winning"],
                        conflict_flag=v["conflict_flag"],
                    )
                    written += 1

                for n in r["synonyms"]:
                    synonym_id, is_new_name = upsert_synonym(cur, source_id,
                                                             n["value"], cache)
                    names += is_new_name
                    link_synonym(
                        cur, gas_id, synonym_id, source_id,
                        relationship_type=SYNONYM_RELATIONSHIP,
                        ambiguity_flag=bool(n.get("shared_with")),
                        notes=" ".join(n["notes"]) or None,
                    )
                    linked += 1

        conn.commit()
        log.info("✓ Committed. cargo_gas: %d (%d created this run) | "
                 "cargo_gas_property_values: %d | cargo_gas_synonym: %d link(s) "
                 "over %d distinct name(s), of which %d were new to the shared "
                 "`synonyms` table and %d already existed there",
                 len(all_rows), created, written, linked, len(cache), names,
                 len(cache) - names)
        return 0
    except Exception:
        conn.rollback()
        log.exception("Import failed - rolled back, nothing written.")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
