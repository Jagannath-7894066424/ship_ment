#!/usr/bin/env python3
"""Extract the DOT Hazardous Materials Table (49 CFR 172.101) into JSON.

Source: "Cargo Library 2 DOT_hazardous_materials.xlsx - HM Table.csv" — a direct
CSV export of the HMT with the regulation's own column markers (1)..(10B) on the
header rows. This script is a *faithful* converter: it copies cell text exactly,
turns blank cells into null, and never infers a value that isn't in the source.

Column layout (0-indexed CSV column -> JSON field):
  0  (1)   symbol                    (raw text; may combine letters e.g. "A W")
  1  (2)   proper_shipping_name
  2  (3)   hazard_class
  3  (4)   un_number
  4  (5)   packing_group             (header "PG")
  5  (6)   label_codes               -> array, comma-split
  6  (7)   special_provisions        -> array, comma-split
  7  (8A)  exceptions_packaging
  8  (8B)  non_bulk_packaging
  9  (8C)  bulk_packaging
  10 (9A)  passenger_aircraft_limit
  11 (9B)  cargo_aircraft_limit
  12 (10A) vessel_stowage_location
  13 (10B) vessel_stowage_other

Data starts after the four header rows (0..3). A row is kept when it has a
proper_shipping_name (column 2); fully blank rows are skipped.

Usage:
    python3 etl/dot_hmt_extract.py                 # uses default file, writes JSON next to it
    python3 etl/dot_hmt_extract.py <input.csv> <output.json>
"""
from __future__ import annotations

import csv
import json
import sys
from typing import List, Optional

from _paths import input_file

DEFAULT_INPUT = "Cargo Library 2 DOT_hazardous_materials.xlsx - HM Table.csv"

# JSON field name for each CSV column index, in output order.
FIELDS = [
    (0, "symbol"),
    (1, "proper_shipping_name"),
    (2, "hazard_class"),
    (3, "un_number"),
    (4, "packing_group"),
    (5, "label_codes"),            # array
    (6, "special_provisions"),     # array
    (7, "exceptions_packaging"),
    (8, "non_bulk_packaging"),
    (9, "bulk_packaging"),
    (10, "passenger_aircraft_limit"),
    (11, "cargo_aircraft_limit"),
    (12, "vessel_stowage_location"),
    (13, "vessel_stowage_other"),
]
ARRAY_FIELDS = {"label_codes", "special_provisions"}
HEADER_ROWS = 4          # rows 0..3 are titles / column markers
NAME_COL = 1             # proper_shipping_name; blank => not a data row


def clean(value: Optional[str]) -> Optional[str]:
    """Trim; blank -> None. Inner text is otherwise preserved exactly."""
    if value is None:
        return None
    v = value.strip()
    return v or None


def split_list(value: Optional[str]) -> Optional[List[str]]:
    """Comma-separated cell -> list of trimmed tokens; blank -> None."""
    v = clean(value)
    if v is None:
        return None
    return [tok.strip() for tok in v.split(",") if tok.strip()]


def cell(row: List[str], idx: int) -> Optional[str]:
    return row[idx] if idx < len(row) else None


def extract(rows: List[List[str]]) -> List[dict]:
    out: List[dict] = []
    for row in rows[HEADER_ROWS:]:
        if not clean(cell(row, NAME_COL)):     # skip rows with no shipping name
            continue
        obj: dict = {}
        for idx, field in FIELDS:
            raw = cell(row, idx)
            obj[field] = split_list(raw) if field in ARRAY_FIELDS else clean(raw)
        out.append(obj)
    return out


def main() -> None:
    if len(sys.argv) >= 2:
        src = sys.argv[1]
    else:
        src = input_file(DEFAULT_INPUT)

    with open(src, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))

    records = extract(rows)

    if len(sys.argv) >= 3:
        dest = sys.argv[2]
    else:
        dest = str(src).rsplit(".", 1)[0] + ".json"

    with open(dest, "w", encoding="utf-8") as fh:
        json.dump(records, fh, ensure_ascii=False, indent=2)

    print(f"read   : {src}")
    print(f"wrote  : {dest}")
    print(f"records: {len(records)}")


if __name__ == "__main__":
    main()
