import argparse
import json
import logging
import re
import sys
from pathlib import Path

from _paths import input_file
from typing import Any, Dict, List, Optional

import pandas as pd

# ----------------------------------------------------------------------------
DEFAULT_FILE = input_file("Miracle Tank Cleaning Guide.xlsx")
DEFAULT_SHEET = "Chemicals"
SYNONYM_COLUMN = "Synonyms"          # semicolon-separated synonym names
SYNONYM_SPLIT = re.compile(r"\s*;\s*")  # split the Synonyms cell on ';'
SOURCE_NAME = "Miracle Tank Cleaning Guide - 2007"
# ----------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("miracle_2007")

# MIRACLE FORMAT: File column name -> cargo_chemical database column name.
# Columns with no sensible target (Pages, Entry No., cleaning-guide-only fields,
# LEL/UEL/Odour, Mol. Formula) are intentionally omitted and dropped on load.
# The "Synonyms" column is handled separately (see extract_synonyms).
MIRACLE_MAPPING = {
    "Chemical Name": "canonical_name",
    "IBC Name": "ibc_product_name",
    "Mol. Formula": "molecular_formula",
    "Product Description": "product_description",
    "CAS No.": "cas_number",
    "UN No.": "un_number",
    "Mol. Weight (g/mol)": "molecular_weight_g_mol",
    "USCG Group": "uscg_compatibility_group",
    "Density (Kg/l)": "density_g_cm3",
    "Water Solubility (% g/g)": "water_solubility",
    "Melting Point (deg C)": "melting_point_c",
    "Boiling Point (deg C)": "boiling_point_c",
    "Vapour Pressure (bar)": "vapor_pressure_kpa_20c",
    "Viscosity (mPa*s)": "viscosity_cp_20c",
    "Flash Point (deg C)": "flash_point_c",
    "LEL (% vol)": "lel",
    "UEL (% vol)": "uel",
    "MARPOL Annex": "marpol_category",
    "IBC Chp.": "ibc_chapter",
    "Pollution Cat.": "ibc_pollution_category",
    "Hazard": "hazards",
    "Ship Type": "ship_type",
    "Tank Type": "tank_type",
    "Vent": "tank_vents",
    "E Class": "electrical_equipment_temperature_class",
    "E Group": "electrical_equipment_apparatus_group",
    "Flash Protection": "flashpoint_requirement",
    "Gauging": "gauging",
    "Vapour Det.": "vapour_detection",
    "Fire Protection": "fire_protection",
    "Escape Eq.": "emergency_equipment",
    "Special Requirements": "stowage_notes",
}

# Placeholder cell values that should be treated as missing (SQL NULL).
PLACEHOLDERS = {"", "-", "–", "—", "?", "n/a", "na", "n.a", "none", "unknown"}


def read_file(path: Path, sheet: Optional[str]) -> pd.DataFrame:
    """Read the Miracle spreadsheet sheet into a DataFrame (all cells as str)."""
    suffix = path.suffix.lower()
    log.info("Reading file %s (type %s, sheet %s)", path, suffix,
             sheet if sheet is not None else "<first>")
    if suffix in (".xlsx", ".xls"):
        df = pd.read_excel(path, sheet_name=sheet if sheet is not None else 0,
                           dtype=str, keep_default_na=False)
    elif suffix == ".csv":
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
    else:
        raise ValueError(f"Unsupported file type '{suffix}'. Use .xlsx or .csv.")
    df.columns = [re.sub(r"\s+", " ", str(c).strip()) for c in df.columns]
    log.info("Loaded %d rows, %d columns", len(df), len(df.columns))
    return df


def clean(value: Any) -> Optional[str]:
    """Trim a cell and map placeholder/blank values to None (SQL NULL)."""
    if value is None:
        return None
    s = re.sub(r"\s+", " ", str(value).strip())
    if s.lower() in PLACEHOLDERS:
        return None
    return s


def extract_synonyms(record: pd.Series) -> List[str]:
    """Parse the semicolon-separated Synonyms cell into a de-duplicated list.

    Order is preserved; blank fragments and exact duplicates are dropped.
    """
    raw = clean(record.get(SYNONYM_COLUMN))
    if raw is None:
        return []
    synonyms: List[str] = []
    seen = set()
    for part in SYNONYM_SPLIT.split(raw):
        name = part.strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        synonyms.append(name)
    return synonyms


def row_to_json(record: pd.Series) -> Optional[Dict[str, Any]]:
    """Map one spreadsheet row to a JSON record keyed by DB column names.

    Values are cleaned (blanks/placeholders -> None). The parsed synonym list is
    attached under the "synonyms" key. Rows with no canonical_name are skipped
    (returns None).
    """
    result: Dict[str, Any] = {}
    for file_col, db_col in MIRACLE_MAPPING.items():
        result[db_col] = clean(record.get(file_col))

    if not result.get("canonical_name"):
        return None

    result["synonyms"] = extract_synonyms(record)
    return result


def build_records(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """Convert every row of the sheet into a JSON record, skipping empty ones."""
    records: List[Dict[str, Any]] = []
    skipped = 0
    for idx, record in df.iterrows():
        rec = row_to_json(record)
        if rec is None:
            skipped += 1
            continue
        records.append(rec)
    log.info("Built %d JSON records, skipped %d row(s) with no chemical name",
             len(records), skipped)
    return records


def main():
    parser = argparse.ArgumentParser(
        description="Read Miracle Tank Cleaning Guide chemicals into JSON records."
    )
    parser.add_argument("file", nargs="?", default=DEFAULT_FILE,
                        help="Path to the .xlsx or .csv file")
    parser.add_argument("--sheet", default=DEFAULT_SHEET,
                        help="Excel sheet name (default: Chemicals)")
    parser.add_argument("--limit", type=int, default=10,
                        help="How many records to print as JSON (default: 10)")
    args = parser.parse_args()

    path = Path(args.file)
    if not path.is_file():
        sys.exit(f"Error: file not found: {path}")

    df = read_file(path, args.sheet)

    missing = [c for c in MIRACLE_MAPPING if c not in df.columns]
    if missing:
        log.warning("Columns not found in file (will be NULL): %s", missing)
    if SYNONYM_COLUMN not in df.columns:
        log.warning("Synonym column %r not found; synonyms will be empty.",
                    SYNONYM_COLUMN)

    records = build_records(df)

    preview = records[:args.limit]
    log.info("First %d record(s) as JSON:", len(preview))
    print(json.dumps(preview, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
