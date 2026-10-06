#!/usr/bin/env python3
"""
Load the Products_info gas cargo table into cargo_gas +
cargo_gas_property_values.

SOURCE
------
"Products_info" (source.json, category 'gas'). The SECOND gas source, after
VAPDENS.XLSX. The two overlap by name - both list ammonia, propane, ethylene -
but nothing here merges them: identity in cargo_gas is (gas_name, source_id),
so each source keeps its own row and its own figures, and a disagreement
between them stays visible instead of one silently overwriting the other.

INPUT
-----
"Products_info.xls - Cargoes.csv": 39 products, one per row, nine property
columns:

    Product Name | Formula | UN number | Molecular weight (Kg/Kmole) |
    Boiling point at Atm.pres. (C°) | Specific Gravity at Boiling point |
    Flash point (C°) | Flammabale limits (%Vol) | TLV (ppm) |
    Odour threshold (ppm)

Product Name becomes cargo_gas.gas_name; the other nine become
cargo_gas_property_values rows. Header text is matched exactly, typos
("Flammabale") and all - if the spreadsheet is re-exported with different
columns this loader stops rather than loading the wrong column into a field.

FIELDS ARE THE SHARED ONES
--------------------------
All nine map onto field_definitions that already exist because the chemical and
oil branches use them (molecular_formula, UN_NUMBER, molecular_weight_g_mol,
boiling_point_c, specific_gravity, flash_point_c, flammable_limits,
tlv_twa_ppm, odour_limit). A gas boiling point is the same measurement as a
chemical one, so this loader reuses the field rather than minting a gas-only
twin that nothing could compare against.

THE ASTERISKS IN THE SPECIFIC GRAVITY COLUMN
--------------------------------------------
The column is headed "Specific Gravity at Boiling point", but the last row of
the file is a legend:

    * Specific gravity at 0°C   ** Specific gravity at +15°C   *** Specific gravity at +20°C

so "0,6615*" is NOT at the boiling point - it is at 0°C, and the heading is
wrong for every marked row (23 of the 33 figures in that column). The marker is
stripped from the number and what it means is written to that value's notes.
Storing 0.6615 under a "at Boiling point" heading with the marker dropped would
silently claim a measurement basis the source never gave. Markers are expected
in that column only; one anywhere else is a validation error, because the
legend's wording ("Specific gravity at ...") cannot be true of another column.

WORDS WHERE A NUMBER WAS EXPECTED
---------------------------------
Several cells answer in prose: "composition" and "depends on composition" (the
product is a mixture - Crude C4, LPG, Pentanes), "as per data sheet",
"as per shipper`s information", "see graph A1.2.", "Odourless", "too high",
"less than 1", "up to 2000". Each is kept verbatim as the value with
value_type 'text' and a note saying the source gave words, not a figure -
dropping them would turn "Odourless" into "unknown", which is the opposite of
what the source says. "less than X" and "up to X" additionally get
normalized_max = X, since that much IS a number.

"n/a" and blanks are the absence of data and are not written at all.

REFRIGERANT GASES: ONE ROW, TWO PRODUCTS
----------------------------------------
The "Refregerant gases" row prints R12 and R22 together, with several cells
holding both figures on two lines ("120,9 / 86,5"). Splitting it into two
cargoes would mean inventing two product names the source never printed, so the
row is loaded as the source's own single row and every value on it says so.
A cell that holds two figures is stored as the multi-valued text it is, with no
normalized number and its footnote markers left exactly as printed; a cell that
holds one figure for both products (their shared TLV) is parsed normally. The
rule is the file's shape - a line break inside a cell - not a hard-coded
product name.

NAMES ARE VERBATIM
------------------
gas_name is what the source prints, including its typos ("Isoprene (ihibited)",
"Carbonmonoxid", "Refregerant gases"). Names are source-scoped, so correcting
them here would make this source's rows unfindable from the file it came from;
matching across sources is a later, separate concern.

IDEMPOTENCY
-----------
Upsert on (gas_name, source_id) and (cargo_gas_id, source_id, field_name). The
whole file is validated before anything is written; one transaction.

Usage:
    python3 etl/gas/products_info.py
    python3 etl/gas/products_info.py --dry-run
    python3 etl/gas/products_info.py "/path/to/Products_info.xls - Cargoes.csv"
"""

import argparse
import csv
import logging
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

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
log = logging.getLogger("gas_products_info")

SOURCE_NAME = "Products_info"
ENTERED_BY = "products_info.py"
DEFAULT_FILE = input_file("Products_info.xls - Cargoes.csv")

NAME_HEADER = "Product Name"

# Cells that state the product is a mixture instead of giving a figure. Kept as
# values (they are the source's answer), but labelled for what they are.
MIXTURE_TEXT = {"composition", "depends on composition", "mix."}


# ---------------------------------------------------------------------------
# Cell parsing
# ---------------------------------------------------------------------------
# A parser turns one cleaned cell into (value_type, value, min, max). Returning
# value_type 'text' means "the source did not give a number here" - the cell is
# still stored, and the caller adds the note that says so.
Parsed = Tuple[str, Optional[float], Optional[float], Optional[float]]

# 21°, -0.5°, -103.8°, -8C, 0,6615, 1,468 - the file writes decimals with a
# comma and marks temperatures with ° or a trailing C.
_NUM = re.compile(r"^[+-]?[\d.,]+$")


def to_float(token: str) -> Optional[float]:
    """One numeric token -> float, or None if it is not one.

    Handles the file's two spellings: a decimal comma ("0,6615") and a degree
    sign or trailing C on temperatures ("-103.8°", "-8C"). A comma is only
    read as a decimal point when the token has no '.' as well; nothing in this
    file uses a thousands separator, and treating "1,468" as 1468 would be a
    three-orders-of-magnitude error rather than a visible one.
    """
    s = token.strip().rstrip("%").replace("°", "").strip()
    s = re.sub(r"\s*C$", "", s, flags=re.IGNORECASE).strip()
    if not s:
        return None
    if "," in s and "." not in s:
        s = s.replace(",", ".")
    if not _NUM.match(s):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_text(_s: str) -> Parsed:
    """A column that is text by nature (formula, UN number)."""
    return "text", None, None, None


def parse_number(s: str) -> Parsed:
    f = to_float(s)
    return ("number", f, None, None) if f is not None else ("text", None, None, None)


def parse_temperature(s: str) -> Parsed:
    """A temperature, or a temperature range ("-8C to 4C")."""
    m = re.match(r"^(.+?)\s+to\s+(.+)$", s, flags=re.IGNORECASE)
    if m:
        lo, hi = to_float(m.group(1)), to_float(m.group(2))
        if lo is not None and hi is not None:
            return "range", None, lo, hi
    return parse_number(s)


def parse_percent_range(s: str) -> Parsed:
    """The flammable range as the file writes it: "1,5-9%", "2,5-100%"."""
    m = re.match(r"^([\d.,]+)\s*-\s*([\d.,]+)\s*%?$", s)
    if m:
        lo, hi = to_float(m.group(1)), to_float(m.group(2))
        if lo is not None and hi is not None:
            return "range", None, lo, hi
    return parse_number(s)


def parse_ppm(s: str) -> Parsed:
    """A ppm figure, or a bound stated in words ("less than 1", "up to 2000").

    A bound is half a number and is stored as one: normalized_max, with the
    wording kept in `value`. "Odourless" and "too high" are not bounds and stay
    text.
    """
    m = re.match(r"^(?:less than|below|under|up to|<)\s*([\d.,]+)$", s, flags=re.IGNORECASE)
    if m:
        hi = to_float(m.group(1))
        if hi is not None:
            return "range", None, None, hi
    return parse_number(s)


class Column(NamedTuple):
    header: str            # exact text in the file's header row
    field: str             # field_definitions.field_name
    unit: Optional[str]    # unit of the value as stored
    parser: Callable[[str], Parsed]
    numeric: bool          # a word here means the source declined to give a figure
    note: Optional[str]    # what the column heading says, recorded on every value


COLUMNS: List[Column] = [
    Column("Formula", "molecular_formula", None, parse_text, False, None),
    Column("UN number", "UN_NUMBER", None, parse_text, False, None),
    Column("Molecular weight (Kg/Kmole)", "molecular_weight_g_mol", "kg/kmol",
           parse_number, True,
           "The source's column is headed Kg/Kmole, numerically identical to "
           "the field's canonical g/mol; figures are stored unchanged, under "
           "the source's own unit."),
    Column("Boiling point at Atm.pres. (C°)", "boiling_point_c", "°C",
           parse_temperature, True,
           "At atmospheric pressure, per the column heading."),
    Column("Specific Gravity at Boiling point", "specific_gravity", None,
           parse_number, True, None),   # basis is per-row: see the footnotes
    Column("Flash point (C°)", "flash_point_c", "°C", parse_number, True, None),
    Column("Flammabale limits (%Vol)", "flammable_limits", "% vol",
           parse_percent_range, True,
           "Per cent by volume in air, per the column heading."),
    Column("TLV (ppm)", "tlv_twa_ppm", "ppm", parse_ppm, True, None),
    Column("Odour threshold (ppm)", "odour_limit", "ppm", parse_ppm, True, None),
]

# The one column the file's asterisk legend is about.
FOOTNOTE_COLUMN = "Specific Gravity at Boiling point"

WORDS_NOTE = ("The source prints words here rather than a figure; kept verbatim, "
              "with no normalized value.")
MIXTURE_NOTE = ("The source answers this cell by saying the product is a mixture, "
                "rather than giving a value for it.")
ROW_NOTE = ("This line of the source covers more than one product, printed "
            "together on one row. Loaded as the source's own single row rather "
            "than split into products it never named separately.")
CELL_NOTE = ("The source writes one figure per product in this cell, so it is "
             "kept as the multi-valued text it is, with no normalized value.")


def parse_footnotes(cells: List[str]) -> Dict[str, str]:
    """The legend row: '* Specific gravity at 0°C' -> {'*': 'Specific gravity at 0°C'}."""
    out: Dict[str, str] = {}
    for cell in cells:
        text = clean_text(cell)
        if not text or not text.startswith("*"):
            continue
        m = re.match(r"^(\*+)\s*(.+)$", text)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def read_table(path: Path) -> Tuple[List[dict], Dict[str, str], List[str]]:
    """Parse the CSV into product rows plus the asterisk legend.

    Returns (rows, footnotes, errors). Nothing is written unless errors is empty.
    """
    with path.open(newline="", encoding="utf-8-sig") as fh:
        grid = list(csv.reader(fh))
    errors: List[str] = []
    if not grid:
        return [], {}, [f"{path.name} is empty"]

    expected = [NAME_HEADER] + [c.header for c in COLUMNS]
    header = [clean_text(c) or "" for c in grid[0][:len(expected)]]
    if header != expected:
        errors.append(f"header row is {header!r}, expected {expected!r} - is this "
                      f"the Products_info Cargoes sheet?")
        return [], {}, errors

    rows: List[dict] = []
    footnotes: Dict[str, str] = {}
    seen: Dict[str, int] = {}

    for i, raw in enumerate(grid[1:], start=2):
        cells = list(raw) + [""] * (len(expected) - len(raw))
        if not any(clean_text(c) for c in cells):
            continue

        # The legend row sits at the bottom of the table, in the data columns.
        first = clean_text(cells[0])
        if first and first.startswith("*"):
            footnotes.update(parse_footnotes(cells))
            continue

        name = first
        if name is None:
            errors.append(f"row {i}: properties with no product name")
            continue
        if name in seen:
            errors.append(f"row {i}: duplicate product {name!r} (also row {seen[name]})")
            continue
        seen[name] = i

        # A cell the source wrote on two lines holds one figure per product,
        # which means the row covers more than one. Detect it on the RAW cell -
        # clean_text folds the line break away.
        row_multi = any("\n" in c for c in raw)

        values: List[dict] = []
        for col, raw_cell in zip(COLUMNS, cells[1:]):
            text = clean_text(raw_cell)
            if text is None:
                continue
            cell_multi = "\n" in raw_cell

            notes: List[str] = []
            if col.note:
                notes.append(col.note)
            if row_multi:
                notes.append(ROW_NOTE)

            # A marker is only stripped off a single figure. In a multi-valued
            # cell it sits against one of several figures, so the text is left
            # exactly as printed and the legend is recorded alongside it.
            if cell_multi:
                markers = sorted(set(re.findall(r"\*+", text)))
            else:
                m = re.match(r"^(.*?)\s*(\*+)$", text)
                markers = [m.group(2)] if m else []
                if m:
                    text = m.group(1).strip()
            if markers and col.header != FOOTNOTE_COLUMN:
                errors.append(
                    f"row {i}: {name!r} has a footnote marker in {col.header!r} "
                    f"({text!r}); the file's legend is about {FOOTNOTE_COLUMN!r} "
                    f"only, so what it would mean here is undefined")
                continue

            if cell_multi:
                value_type, nv, nmin, nmax = "text", None, None, None
                notes.append(CELL_NOTE)
            else:
                value_type, nv, nmin, nmax = col.parser(text)

            if text.lower() in MIXTURE_TEXT:
                notes.append(MIXTURE_NOTE)
            elif col.numeric and value_type == "text" and not cell_multi:
                notes.append(WORDS_NOTE)

            values.append({
                "field": col.field, "column": col.header, "value": text,
                "value_type": value_type, "normalized_value": nv,
                "normalized_min": nmin, "normalized_max": nmax,
                "unit": col.unit if value_type != "text" else None,
                "multi": cell_multi, "markers": markers, "notes": notes,
            })

        rows.append({"gas_name": name, "line": i, "multi": row_multi,
                     "values": values})

    # Resolve the markers now that the legend (which is at the BOTTOM of the
    # file) has been read.
    for r in rows:
        for v in r["values"]:
            markers = v.pop("markers")
            if not markers:
                if v["column"] == FOOTNOTE_COLUMN and not v["multi"]:
                    v["notes"].append("At the boiling point, per the column heading.")
                continue
            for marker in markers:
                meaning = footnotes.get(marker)
                if meaning is None:
                    errors.append(f"row {r['line']}: {r['gas_name']!r} carries "
                                  f"footnote marker {marker!r} in {v['column']!r}, "
                                  f"which the file never defines")
                    continue
                if v["multi"]:
                    v["notes"].append(
                        f"Source footnote {marker!r}: {meaning} - it applies to the "
                        f"figure it is printed against in this cell.")
                else:
                    v["notes"].append(
                        f"Source footnote {marker!r}: {meaning}. The column is headed "
                        f"'{FOOTNOTE_COLUMN}', but this figure carries that marker, so "
                        f"it is on that basis instead, NOT at the boiling point.")

    if not rows:
        errors.append(f"{path.name} has a header but no product rows")
    return rows, footnotes, errors


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


def report(rows: List[dict], footnotes: Dict[str, str]) -> None:
    per_field = Counter(v["field"] for r in rows for v in r["values"])
    total = sum(per_field.values())
    log.info("%d product(s), %d property value(s)", len(rows), total)
    for col in COLUMNS:
        log.info("    %-28s %3d value(s)", col.field, per_field.get(col.field, 0))

    log.info("footnote legend: %s", ", ".join(f"{k} = {v}" for k, v in
                                              sorted(footnotes.items())) or "(none)")
    marked = sum(1 for r in rows for v in r["values"]
                 if any(n.startswith("Source footnote") for n in v["notes"]))
    log.info("specific gravities NOT at the boiling point (footnoted): %d", marked)

    numeric_columns = {c.header for c in COLUMNS if c.numeric}
    worded = [(r["gas_name"], v["column"], v["value"]) for r in rows for v in r["values"]
              if v["value_type"] == "text" and not v["multi"]
              and v["column"] in numeric_columns]
    log.info("cells answered in words rather than figures: %d", len(worded))
    for name, column, value in worded:
        log.info("    %-30s %-32s %s", name, column, value)

    multi = [r["gas_name"] for r in rows if r["multi"]]
    if multi:
        log.info("row(s) covering more than one product (stored unsplit): %s",
                 ", ".join(multi))
        for r in rows:
            for v in r["values"]:
                if v["multi"]:
                    log.info("    %-30s %-32s %s", r["gas_name"], v["column"],
                             v["value"])
    bare = [r["gas_name"] for r in rows if not r["values"]]
    if bare:
        log.info("product(s) the source lists with no property at all: %s",
                 ", ".join(bare))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", default=str(DEFAULT_FILE))
    ap.add_argument("--dry-run", action="store_true", help="parse and report, write nothing")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.is_file():
        sys.exit(f"Error: file not found: {path}")

    rows, footnotes, errors = read_table(path)
    if errors:
        log.error("%d validation error(s) - nothing was imported:", len(errors))
        for e in errors:
            log.error("    %s", e)
        return 1

    log.info("%s", path.name)
    report(rows, footnotes)

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

            added = ensure_field_definitions(cur, only=[c.field for c in COLUMNS])
            log.info("field_definitions: %d created, %d already present",
                     added, len(COLUMNS) - added)

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
