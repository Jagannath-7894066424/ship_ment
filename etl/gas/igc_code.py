#!/usr/bin/env python3
"""
Load the IGC Code 2016 chapter 19 table into cargo_gas and
cargo_gas_property_values.

SOURCE
------
"IGC Code 2016 Cargo Gas" (source.json, category 'gas'). The International Code
for the Construction and Equipment of Ships Carrying Liquefied Gases in Bulk,
2016 edition, chapter 19 - the "Summary of minimum requirements" table. Like
IMO Cargo.XLSX it is REGULATORY and measures nothing, so it is ranked for
rank_regulatory and given no physical rank.

It is the most authoritative gas source in the project for what a ship must do,
and it is the only one that says anything about the SHIP rather than the cargo.

WHAT ONE ROW STATES
-------------------
Seven columns, all of them requirements imposed on a ship carrying that cargo:

    Ship type                     1G / 2G / 2PG / 3G
    Independent tank type C required
    Control of vapour space within cargo tanks     Inert / Dry
    Vapour detection              flammable, toxic, both, asphyxiant
    Gauging                       indirect / closed / restricted
    Gauging Reference No          13.2.3.1 ...
    Special requirements          14.x and 17.x paragraph numbers

None of them is a measurement, so every value is stored as text with no unit
and no normalized value. "2G/2PG" is a ship type; treating it as a number would
be a category error, and there is nothing to normalise a paragraph list to.

THE LAST TWO COLUMNS ARE POINTERS, NOT REQUIREMENTS
-----------------------------------------------------
"Gauging Reference No" and "Special requirements" hold IGC paragraph numbers.
The requirement itself is the text of that paragraph, which this table does not
reproduce - "17.6.1" is where to look, not what to do. They are stored because
a paragraph list IS the source's own statement of which rules apply, and
resolving them would mean loading chapters 14 and 17, which this file is not.

'-' IS AN ANSWER, NOT A BLANK
-----------------------------
Three columns use an EN DASH for "no requirement under this heading": 33 of the
37 cargoes need no type C tank, 28 need no vapour space control. That is the
code positively stating that nothing is required, which is not the same as the
table declining to say. Both are kept and kept apart:

  * en dash        -> a row IS written, holding the dash as printed, with a
                      note recording that the code means "no requirement".
  * empty cell     -> NO row. Only "Special requirements" has these, for the 11
                      cargoes the code imposes no chapter 14/17 rule on.

Reading the dash as "unknown" would lose a regulatory fact; dropping it would
make 33 ships look unassessed rather than unrestricted.

THE ASTERISK IS A FOOTNOTE MARKER AND IS NOT PART OF THE NAME
---------------------------------------------------------------
Nine products carry an asterisk - "Diethyl ether*", "Isoprene* (all isomers)",
"Isoprene (part refined)*". It marks a footnote in the printed table, and its
position wanders: mid-name on one row, after the qualifier on the next. The
footnote text is NOT in this extract.

The marker is stripped from gas_name, because "Diethyl ether*" is not the name
of a substance and would never match "Diethyl ether" from any other source. It
is not discarded: every affected cargo records in its notes that the code marks
it with an asterisk whose footnote this extract does not carry, so the fact
that something further applies survives even though its text does not.

NAMES ARE CARGO GRADES, NOT SUBSTANCES
--------------------------------------
Nine entries name a group rather than a compound - "Butadiene (all isomers)",
"Mixed C4 Cargoes", "Refrigerant gases", "Butane-propane mixture". They are
loaded under the code's own wording. cargo_gas is keyed (gas_name, source_id),
so this source keeps its own rows and nothing is merged onto a pure substance
that the code never named.

IDEMPOTENCY
-----------
Upsert on (gas_name, source_id) and on (cargo_gas_id, source_id, field_name).
The whole file is validated before anything is written; one transaction.

Usage:
    python3 etl/gas/igc_code.py
    python3 etl/gas/igc_code.py --dry-run
"""

import argparse
import csv
import logging
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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
log = logging.getLogger("gas_igc_code")

SOURCE_NAME = "IGC Code 2016 Cargo Gas"
ENTERED_BY = "igc_code.py"
DEFAULT_FILE = input_file("IGC Code 2016 Cargo Gas Details .csv")

NAME_COLUMN = "Product name"

# The header exactly as the file prints it -> the field it becomes. Matched on
# the header text rather than by position, so a re-export that reorders the
# columns stops the loader instead of writing a gauging type into a ship type.
COLUMNS = [
    ("Ship type", "igc_ship_type"),
    ("Independent tank type C required", "igc_independent_tank_c_required"),
    ("Control of vapour space within cargo tanks", "igc_vapour_space_control"),
    ("Vapour detection", "igc_vapour_detection"),
    ("Gauging", "igc_gauging"),
    ("Gauging Reference No", "igc_gauging_reference"),
    ("Special requirements", "igc_special_requirements"),
]

# The table's "no requirement" mark. An en dash, not a hyphen: a file saved as
# a hyphen would be a different character and is reported rather than assumed
# equivalent, because these two look alike and mean different things elsewhere.
NO_REQUIREMENT = "–"
NO_REQUIREMENT_NOTE = (
    "The code prints an en dash here, which in this table means NO REQUIREMENT "
    "under this heading - a positive statement that nothing is imposed, not a "
    "gap in the table. Stored as printed so it stays distinguishable from a "
    "cargo the table says nothing about, which gets no row at all.")

ASTERISK_NOTE = (
    "The code marks this product with an asterisk in chapter 19. The asterisk "
    "is a footnote marker, and this extract does not carry the footnote's text, "
    "so a further requirement applies whose wording is not in the database. The "
    "marker is stripped from the name because it is not part of it.")

# Vocabularies the file is expected to use. Not for correcting anything - a
# value outside the list is reported so a new wording is read by a human before
# it is trusted, the same discipline the thermodynamic loader uses for labels.
EXPECTED = {
    "igc_ship_type": {"1G", "2G", "3G", "2G/2PG"},
    "igc_independent_tank_c_required": {"Yes", NO_REQUIREMENT},
    "igc_vapour_space_control": {"Inert", "Dry", NO_REQUIREMENT},
    "igc_vapour_detection": {
        "Flammable vapour detection", "Toxic vapour detection",
        "Flammable and toxic vapour detection", "Asphyxiant", NO_REQUIREMENT},
    "igc_gauging": {"Indirect or closed", "Indirect, closed or restricted"},
}

# Paragraph-number columns: every entry must be a comma-separated list of
# dotted numbers and nothing else. A word appearing here would mean the column
# is not what the header says it is.
REFERENCE_FIELDS = {"igc_gauging_reference", "igc_special_requirements"}
PARAGRAPH = re.compile(r"^\d+(?:\.\d+)*$")


def clean(text: str) -> str:
    """Trim and collapse whitespace. The file has stray leading spaces
    (' Flammable vapour detection') and inconsistent spacing around commas."""
    return re.sub(r"\s+", " ", (text or "").replace(" ", " ")).strip()


def split_marker(name: str) -> Tuple[str, bool]:
    """(product name without the footnote asterisk, whether it had one).

    The asterisk is not always at the end - "Isoprene* (all isomers)" carries
    it in the middle - so it is removed wherever it sits rather than stripped
    from the tail.
    """
    marked = "*" in name
    return clean(name.replace("*", "")), marked


def read_file(path: Path) -> Tuple[List[dict], List[str]]:
    """Parse the CSV. Returns (products, errors); nothing is written unless
    errors is empty."""
    errors: List[str] = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))
    if not rows:
        return [], [f"{path.name} is empty"]

    header = [clean(c) for c in rows[0]]
    if header and header[0] != NAME_COLUMN:
        errors.append(f"the first column is headed {header[0]!r}, expected "
                      f"{NAME_COLUMN!r}; the file's columns are {header!r}")
    index: Dict[str, int] = {}
    for wanted, field in COLUMNS:
        if wanted not in header:
            errors.append(f"no {wanted!r} column in the header {header!r}")
            continue
        index[field] = header.index(wanted)
    extra = [h for h in header[1:] if h not in {w for w, _ in COLUMNS}]
    if extra:
        errors.append(f"the file carries column(s) this loader has no field "
                      f"for: {extra!r}. Add them to COLUMNS and to FIELD_DEFS "
                      f"rather than letting them go unloaded")
    if errors:
        return [], errors

    products: List[dict] = []
    seen: Dict[str, int] = {}
    for line, raw in enumerate(rows[1:], start=2):
        if not any(clean(c) for c in raw):
            continue
        printed = clean(raw[0])
        if not printed:
            errors.append(f"line {line}: a row with no product name")
            continue
        name, marked = split_marker(printed)
        if name in seen:
            errors.append(f"line {line}: product {name!r} already appears on "
                          f"line {seen[name]}")
            continue
        seen[name] = line

        values = []
        for field, col in index.items():
            value = clean(raw[col]) if col < len(raw) else ""
            if not value:
                continue                     # the table says nothing here
            if "-" in value and NO_REQUIREMENT not in value and \
                    field in EXPECTED and NO_REQUIREMENT in EXPECTED[field]:
                errors.append(
                    f"line {line}: {field} reads {value!r} with an ASCII "
                    f"hyphen where this table uses an en dash for 'no "
                    f"requirement'. The two look alike and are not the same "
                    f"character; check the export before loading")
                continue
            if field in EXPECTED and value not in EXPECTED[field]:
                errors.append(
                    f"line {line} ({name!r}): {field} reads {value!r}, which is "
                    f"not one of the wordings this loader knows "
                    f"({', '.join(sorted(map(repr, EXPECTED[field])))}). A new "
                    f"wording is read by a human before it is trusted")
                continue
            if field in REFERENCE_FIELDS:
                parts = [p for p in (q.strip() for q in value.split(",")) if p]
                bad = [p for p in parts if not PARAGRAPH.match(p)]
                if bad:
                    errors.append(
                        f"line {line} ({name!r}): {field} contains {bad!r}, "
                        f"which is not an IGC paragraph number; this column "
                        f"should hold nothing else")
                    continue
                # Stored as printed, but the spacing in this file is erratic
                # ("13.2.3.2 ,13.2.3.3"), so the tidy list travels in `notes`.
                values.append({"field": field, "value": value,
                               "paragraphs": parts})
                continue
            values.append({"field": field, "value": value, "paragraphs": None})

        products.append({"name": name, "printed": printed, "marked": marked,
                         "values": values, "line": line})

    if not products:
        errors.append(f"{path.name} has a header but no product rows")
    return products, errors


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


def report(products: List[dict]) -> None:
    log.info("%d product(s)", len(products))
    fields = [f for _, f in COLUMNS]
    width = max(len(p["name"]) for p in products)
    for p in products:
        have = {v["field"] for v in p["values"]}
        log.info("    %-*s %s%s", width, p["name"],
                 " ".join("y" if f in have else "." for f in fields),
                 "   *footnote" if p["marked"] else "")
    log.info("    (columns in order: %s)", ", ".join(fields))

    for field in fields:
        written = sum(1 for p in products for v in p["values"] if v["field"] == field)
        dashes = sum(1 for p in products for v in p["values"]
                     if v["field"] == field and v["value"] == NO_REQUIREMENT)
        blank = len(products) - written
        log.info("    %-34s %3d row(s)%s%s", field, written,
                 f", {dashes} of them 'no requirement'" if dashes else "",
                 f", {blank} cargo(es) with an empty cell and so no row" if blank else "")

    marked = [p["name"] for p in products if p["marked"]]
    if marked:
        log.info("%d product(s) the code marks with a footnote asterisk this "
                 "extract does not carry the text of: %s",
                 len(marked), "; ".join(marked))
    log.info("total cargo_gas_property_values rows: %d",
             sum(len(p["values"]) for p in products))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", default=str(DEFAULT_FILE))
    ap.add_argument("--dry-run", action="store_true",
                    help="parse and report, write nothing")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.is_file():
        sys.exit(f"Error: file not found: {path}")

    products, errors = read_file(path)
    if errors:
        log.error("%d validation error(s) - nothing was imported:", len(errors))
        for e in errors:
            log.error("    %s", e)
        return 1

    report(products)
    if args.dry_run:
        log.info("--dry-run: nothing written.")
        return 0

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        sys.exit("Error: DATABASE_URL not set (checked .env).")

    page_ref = f"{path.name} (IGC Code 2016, chapter 19)"
    conn = psycopg2.connect(db_url)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            source_id = resolve_source(cur, SOURCE_NAME)
            log.info("Source id=%s (%r)", source_id, SOURCE_NAME)

            fields = [f for _, f in COLUMNS]
            added = ensure_field_definitions(cur, only=fields)
            log.info("field_definitions: %d created, %d already present",
                     added, len(fields) - added)

            created = written = 0
            for p in products:
                gas_id, is_new = upsert_gas(cur, source_id, p["name"])
                created += is_new
                for v in p["values"]:
                    notes = []
                    if v["value"] == NO_REQUIREMENT:
                        notes.append(NO_REQUIREMENT_NOTE)
                    if v["paragraphs"]:
                        notes.append(
                            f"IGC Code paragraph(s): "
                            f"{', '.join(v['paragraphs'])}. These are pointers "
                            f"into the code; the requirement is the text of the "
                            f"paragraph, which this table does not reproduce.")
                    if p["marked"]:
                        notes.append(ASTERISK_NOTE)
                    notes.append(f"Read from {page_ref}, the summary of minimum "
                                 f"requirements, under the product name "
                                 f"{p['printed']!r}.")
                    upsert_property(
                        cur, gas_id, source_id, v["field"], value=v["value"],
                        normalized_value=None, unit=None, value_type="text",
                        entered_by=ENTERED_BY, source_page_ref=page_ref,
                        notes=" ".join(notes),
                    )
                    written += 1

        conn.commit()
        log.info("✓ Committed. cargo_gas: %d (%d created) | "
                 "cargo_gas_property_values: %d",
                 len(products), created, written)
        return 0
    except Exception:
        conn.rollback()
        log.exception("Import failed - rolled back, nothing written.")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
