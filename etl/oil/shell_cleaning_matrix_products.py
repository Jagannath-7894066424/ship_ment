#!/usr/bin/env python3
"""
Load the Shell cleaning matrix's product list into crude_oil (+ UN_NUMBER).

Source: "Shell Cleaning Matrix" (source.json, category 'oil').

WHAT THIS FILE IS
-----------------
Three columns and 28 rows:

    product_id | un_number | product_name

It is the KEY TABLE of the cargo-to-cargo regime matrix. Every row of
"Shell Tank Cleaning Guide 2016 cargo to cargo regime code.csv" identifies its
two cargoes by these ids, and reprints this file's name and UN number beside
each one. That was checked before this loader was written: across all 784
matrix rows - 1,568 cargo references - every id resolves here, and every
reprinted name and UN number agrees with this file exactly. So this is the
matrix's cargo master, and loading it is what gives that matrix something to
point at under this source.

WHERE THE COLUMNS GO
--------------------
    product_name  -> crude_oil.oil_name        (the identity)
    product_name  -> crude_oil.aggregated_name (the join key, see below)
    un_number     -> property UN_NUMBER
    product_id    -> NOT STORED, see below

oil_name and aggregated_name deliberately get the SAME text. On the older Shell
source they differ - oil_name is a short title ("ULSD - 10ppm") and
aggregated_name the guide's full wording ("AD10 - 10ppm Diesel fuel, ADO, AGO")
- because that workbook printed both. This file prints only the full wording.
Inventing a short title would be fabricating a name the source never used, and
leaving aggregated_name NULL would leave the matrix nothing to join on, because
shell_cargo_matrix.py joins through aggregated_name. So the one name the file
gives is written to both columns.

THESE ARE NOT CRUDE OILS
------------------------
Jet A1, AVGAS, MTBE, Toluene, FAME, vegetable oils. crude_oil is being used as
a general oil-cargo master, the same deliberate choice shell_cargo_master.py
records. Noted here too because a reader comparing this source against the
crude-assay sources will find no API and no pour point.

product_id IS NOT STORED
------------------------
It is a row number in a spreadsheet, meaningful only inside these two files,
and crude_oil has no column for a foreign key space. Storing it would invite a
later reader to join on it, and it would be wrong the moment the sheet is
re-exported with a row inserted. The matrix loader resolves cargoes by name
through aggregated_name, so nothing needs it.

THE "UN " PREFIX IS DROPPED, THE REST IS VERBATIM
---------------------------------------------------
The file prints "UN 1202" and "UN 1223 & 1202". "UN" is the name of the
numbering scheme, not part of the number, and it is on every row that has a
number at all, so it carries no information. The number itself is kept exactly
as printed, separator included: "1223 & 1202" stays "1223 & 1202" and is not
rewritten to "1223/1202". Both forms already exist under UN_NUMBER from other
sources, and the field's own definition says "as the source prints it" - so the
separator is the source's, not this loader's, and the raw cell is recorded in
the row's notes either way.

TWO VALUES ARE NOT NUMBERS, AND BOTH ARE KEPT
----------------------------------------------
    "No UN Number"  (2 products: FAME, vegetable oils)
    "Various"       (1 product: the Black Oils group)

Neither is missing data. The first is the source positively stating that the
product has no UN number; the second states that the group covers several and
declines to pick one. Dropping them would make three products look unassessed
when the source in fact assessed them, so both are stored as printed, each with
a note saying which of the two it is. They are listed on every run.

A row whose UN cell is genuinely empty gets NO property row - and there are
none in this file.

NO 'nan' REACHES THE DATABASE
------------------------------
The older Shell load left a crude_oil named 'nan' and a UN_NUMBER of 'nan',
both from a pandas NaN stringified on the way in. This loader reads the file
with csv, not pandas, and rejects 'nan'/'none'/'null' as a name or a UN value
rather than storing the word.

IDEMPOTENCY
-----------
Upsert on (oil_name, source_id) and on (crude_oil_id, source_id, field_name).
The file is fully validated before anything is written; one transaction.

Usage:
    python3 etl/oil/shell_cleaning_matrix_products.py
    python3 etl/oil/shell_cleaning_matrix_products.py --dry-run
    python3 etl/oil/shell_cleaning_matrix_products.py <file>
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

# Loaders are run as scripts, so only their own directory is on sys.path.
_ETL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ETL_ROOT / "common"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _crude_oil import (Parsed, ensure_field_definitions, get_source_id,  # noqa: E402
                        upsert_crude_oil, upsert_property)
from _paths import input_file  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("shell_cleaning_matrix_products")

SOURCE_NAME = "Shell Cleaning Matrix"
ENTERED_BY = "shell_cleaning_matrix_products.py"
DEFAULT_FILE = input_file("Shell_cleaning_matrix_ products.csv")

COL_ID = "product_id"
COL_UN = "un_number"
COL_NAME = "product_name"
EXPECTED_HEADER = [COL_ID, COL_UN, COL_NAME]

UN_FIELD = "UN_NUMBER"

# Words a spreadsheet export leaves behind that are not data. Rejected rather
# than stored - see the header.
NOT_DATA = {"nan", "none", "null", "n/a", "na", "-", "--", "?"}

# The scheme label, stripped wherever it is attached to a number. Matches
# "UN1202" and "UN 1202", and the second "UN" in "UN 1223 & UN 1202" if a
# re-export ever writes it that way.
UN_PREFIX = re.compile(r"\bUN\s*(?=\d)", re.IGNORECASE)

# Cells that state something other than a number. Mapped to the note they earn;
# the cell itself is still stored exactly as printed.
NON_NUMERIC = {
    "no un number":
        "The source states this product has NO UN number. That is a positive "
        "assessment, not a gap in the file, so the cell is stored as printed "
        "rather than left out.",
    "various":
        "The source prints 'Various' here: the entry is a group of products "
        "carrying several UN numbers and the source declines to pick one. "
        "Stored as printed; do not read it as a single number.",
}

RAW_NOTE = "The source prints this UN cell as {raw!r}."


def clean(text: Optional[str]) -> str:
    """Trim and collapse whitespace, including non-breaking spaces."""
    return re.sub(r"\s+", " ", (text or "").replace(" ", " ")).strip()


def parse_un(raw: str) -> Tuple[Optional[str], Optional[str]]:
    """(value to store, note) for a UN cell, or (None, None) when there is none.

    The scheme prefix is dropped; everything else - digits, separators, spacing
    - is the source's and is kept.
    """
    if not raw or raw.lower() in NOT_DATA:
        return None, None

    note = NON_NUMERIC.get(raw.lower())
    if note:
        return raw, f"{note} {RAW_NOTE.format(raw=raw)}"

    value = clean(UN_PREFIX.sub("", raw))
    if not value:
        return None, None
    return value, RAW_NOTE.format(raw=raw)


def read_file(path: Path) -> Tuple[List[dict], List[str], List[str]]:
    """Parse and validate. Returns (products, errors, warnings)."""
    errors: List[str] = []
    warnings: List[str] = []

    with path.open(newline="", encoding="utf-8-sig") as fh:
        raw_rows = list(csv.reader(fh))
    if not raw_rows:
        return [], [f"{path.name} is empty"], []

    header = [clean(c) for c in raw_rows[0]]
    if header != EXPECTED_HEADER:
        return [], [f"unexpected header.\n        found    {header!r}\n"
                    f"        expected {EXPECTED_HEADER!r}"], []
    index = {name: i for i, name in enumerate(header)}

    products: List[dict] = []
    seen_names: Dict[str, int] = {}
    for line_no, raw_row in enumerate(raw_rows[1:], start=2):
        if not any(clean(c) for c in raw_row):
            continue
        if len(raw_row) != len(header):
            errors.append(f"line {line_no}: {len(raw_row)} field(s), "
                          f"expected {len(header)}")
            continue

        name = clean(raw_row[index[COL_NAME]])
        un_raw = clean(raw_row[index[COL_UN]])
        product_id = clean(raw_row[index[COL_ID]])

        if not name:
            errors.append(f"line {line_no}: no {COL_NAME!r}")
            continue
        # oil_name is half the identity key; a spreadsheet artifact must never
        # become one. See the header.
        if name.lower() in NOT_DATA:
            errors.append(f"line {line_no}: {COL_NAME!r} is {name!r}, which is "
                          f"a spreadsheet artifact and not a product name")
            continue
        if name.lower() in seen_names:
            errors.append(f"line {line_no}: product name {name!r} already "
                          f"appeared on line {seen_names[name.lower()]}; "
                          f"(oil_name, source_id) must identify one row")
            continue
        seen_names[name.lower()] = line_no

        un_value, un_note = parse_un(un_raw)
        if un_raw and un_value is None:
            warnings.append(f"line {line_no}: {name!r} has UN cell {un_raw!r}, "
                            f"which carries no value; no UN_NUMBER row written")
        if not un_raw:
            warnings.append(f"line {line_no}: {name!r} has an empty UN cell; "
                            f"no UN_NUMBER row written")

        products.append({"line": line_no, "product_id": product_id,
                         "name": name, "un_raw": un_raw,
                         "un_value": un_value, "un_note": un_note})

    if not products and not errors:
        errors.append(f"{path.name} has a header but no data rows")
    return products, errors, warnings


def report(products: List[dict], warnings: List[str]) -> None:
    with_un = [p for p in products if p["un_value"]]
    log.info("%d product(s); %d carry a UN value", len(products), len(with_un))

    non_numeric = [p for p in with_un if p["un_raw"].lower() in NON_NUMERIC]
    if non_numeric:
        log.info("    %d UN cell(s) state something other than a number, "
                 "stored as printed:", len(non_numeric))
        for p in non_numeric:
            log.info("        %-52s %s", p["name"][:52], p["un_raw"])

    multi = [p for p in with_un if re.search(r"[&/,]", p["un_value"])]
    if multi:
        log.info("    %d product(s) cite more than one UN number, kept in the "
                 "source's own notation:", len(multi))
        for p in multi:
            log.info("        %-52s %s", p["name"][:52], p["un_value"])

    for w in warnings:
        log.warning("    %s", w)


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
    log.info("file: %s", path)

    products, errors, warnings = read_file(path)
    if errors:
        log.error("%d validation error(s) - nothing was imported:", len(errors))
        for e in errors:
            log.error("    %s", e)
        return 1

    report(products, warnings)
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
            source_id = get_source_id(cur, SOURCE_NAME)
            log.info("Source id=%s (%r)", source_id, SOURCE_NAME)

            added = ensure_field_definitions(cur, only=[UN_FIELD])
            if added:
                log.info("field_definitions: %d added", added)

            created = updated = un_rows = 0
            for p in products:
                # The file gives one name; it is both the identity and the
                # matrix's join key. See the header.
                oil_id, is_new = upsert_crude_oil(
                    cur, p["name"], source_id, country=None,
                    aggregated_name=p["name"])
                created += is_new
                updated += not is_new

                if p["un_value"] is None:
                    continue
                upsert_property(
                    cur, oil_id, source_id, UN_FIELD,
                    Parsed(value=p["un_value"], normalized_value=None,
                           normalized_min=None, normalized_max=None,
                           unit=None, value_type="text", notes=p["un_note"]),
                    entered_by=ENTERED_BY)
                un_rows += 1

        conn.commit()
        log.info("✓ Committed. crude_oil: %d (%d created, %d already present) | "
                 "%s: %d", len(products), created, updated, UN_FIELD, un_rows)
        return 0
    except Exception:
        conn.rollback()
        log.exception("Import failed - rolled back, nothing written.")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
