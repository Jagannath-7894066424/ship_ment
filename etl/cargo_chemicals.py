#!/usr/bin/env python3
"""
Multi-format ETL loader for chemical cargo data.

Reads CSV or Excel (.xlsx) files in two supported formats:

  1. LARS FORMAT (Legacy format with parent-child hierarchy)
     - Source: "Chemical Cargo specifications" spreadsheet
     - Parent rows: Chemical name + properties
     - Child rows: Synonyms (indented, properties omitted)
     - Column: "COMMODITIES" contains synonyms

  2. IBC FORMAT (Standard tabular format)
     - Source: IBC or other regulatory databases
     - All rows are independent cargo records
     - No parent-child relationships
     - No synonym handling

The script auto-detects the format based on column names and applies
the appropriate mapping dictionary. For LARS format, parent rows are
inserted into cargo_chemical and child rows into the synonym table.

Usage:
    python3 cargo_chemicals.py <path/to/file.csv|.xlsx>
    python3 cargo_chemicals.py data/cargo.xlsx --sheet Sheet1
    python3 cargo_chemicals.py data/cargo.csv --dry-run
    python3 cargo_chemicals.py data/cargo.csv --truncate

Reads DATABASE_URL from the .env file in this directory.
"""

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path

from _paths import input_file
from typing import Dict, Optional, Tuple, Any

import pandas as pd
import psycopg2
from psycopg2 import sql
from psycopg2.extras import execute_values
from dotenv import load_dotenv

# Console logger: prints a timestamped line for every step of the load.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("cargo_chemicals")

# ===========================================================================
# FILE FORMAT DETECTION & MAPPINGS
# ===========================================================================

# LARS FORMAT: File column name -> Database column name
# Source: "Chemical Cargo specifications" spreadsheet (2002)
# Characteristics: Parent-child hierarchy, COMMODITIES synonym column
LARS_MAPPING = {
    "Unnamed: 0": "canonical_name",           # parent chemical name (first, unnamed column)
    "COMMODITIES": "synonym_text",            # synonym names (for synonym table)
    "SpGr": "density_g_cm3",                  # Specific gravity
    "Temp": "reference_temperature_c",        # Reference temperature
    "Correction factor": "correction_factor",
    "Ship Type": "ship_type",
    "Tank Type": "tank_type",
    "Pollution cat": "ibc_pollution_category",
    "Compliance": "compliance",
    "USCG compat": "uscg_compatibility_group",
    "Boiling point": "boiling_point_c",
    "Melting point": "melting_point_c",
    "Flash point": "flash_point_c",
    # "Heat adjacent" / "Heat req V" / "Heat req D" are NOT mapped 1:1 here: each
    # source cell mixes a Y/N flag with an optional temperature and is expanded
    # into a *_required Boolean + *_temperature column (see HEAT_FIELD_MAP /
    # expand_heating_fields, applied in normalize_row_to_json).
    "Colour": "colour",
    "Solubility": "water_solubility",
    "UnNr": "un_number",
    "Comments": "stowage_notes",
}

# IBC FORMAT: File column name -> Database column name
# Source: IBC regulations and similar formal standards
# Characteristics: All rows are independent (no parent-child), no synonyms
IBC_MAPPING = {
    "product_name": "canonical_name",
    "pollution_category": "ibc_pollution_category",
    "hazards": "hazards",
    "ship_type": "ship_type",
    "tank_type": "tank_type",
    "tank_vents": "tank_vents",
    "tank_environmental_control": "tank_environment_control",
    "electrical_equipment_temperature_class": "electrical_equipment_temperature_class",
    "electrical_equipment_apparatus_group": "electrical_equipment_apparatus_group",
    "flashpoint_requirement": "flashpoint_requirement",
    "gauging": "gauging",
    "vapour_detection": "vapour_detection",
    "fire_protection": "fire_protection",
    "emergency_equipment": "emergency_equipment",
}

# CHEM FORMAT: File column name -> Database column name
# Source: "Unknown - Products CHEM - 1996.XLS" density/correction lookup table.
# Characteristics: a preamble title row above the header, a junk A-Z grouping
# marker in the first (unnamed) column, and only three useful columns. The
# density is measured at 15 C, so reference_temperature_c is injected as 15
# (see FILE_FORMAT_CONFIG["chem"]["constant_fields"]).
# NOTE: column names are whitespace-collapsed in read_file, so the double space
# in the raw 'DENSITY  AT 15°C' header becomes a single space here.
CHEM_MAPPING = {
    "PRODUCT": "canonical_name",
    "DENSITY AT 15°C": "density_g_cm3",
    "CORR. FACTOR": "correction_factor",
}

# Detector: file columns that indicate each format
LARS_DETECTOR = {"Unnamed: 0", "COMMODITIES", "SpGr", "Temp"}
IBC_DETECTOR = {"product_name", "pollution_category", "hazards"}
CHEM_DETECTOR = {"PRODUCT", "DENSITY AT 15°C", "CORR. FACTOR"}

# Configuration for each format
FILE_FORMAT_CONFIG = {
    "lars": {
        "mapping": LARS_MAPPING,
        "detector": LARS_DETECTOR,
        "key_column": "canonical_name",
        "has_parent_child": True,
        "synonym_column": "COMMODITIES",
        "parent_column": "Unnamed: 0",
    },
    "ibc": {
        "mapping": IBC_MAPPING,
        "detector": IBC_DETECTOR,
        "key_column": "canonical_name",
        "has_parent_child": False,
        "synonym_column": None,
        "parent_column": None,
    },
    "chem": {
        "mapping": CHEM_MAPPING,
        "detector": CHEM_DETECTOR,
        "key_column": "canonical_name",
        "has_parent_child": False,
        "synonym_column": None,
        "parent_column": None,
        # density is quoted "at 15°C", so pair every density row with that temp
        "constant_fields": {"reference_temperature_c": 15},
        # skip stray single-letter A-Z section markers that leak into PRODUCT
        "min_name_length": 2,
    },
}

# The source name is derived from the file name (text before the first
# extension). It is looked up in source.name; if found its id is written into
# SOURCE_ID_COLUMN on every row, otherwise the whole load is skipped.
SOURCE_ID_COLUMN = "source_id"

# Values that should be treated as boolean True / False when loading into a
# boolean column.
TRUE_SET = {"true", "t", "yes", "y", "1"}
FALSE_SET = {"false", "f", "no", "n", "0"}

# Heating columns. A single source cell mixes a Y/N flag with an optional
# temperature, so each maps to a (required Boolean, temperature DECIMAL) pair.
#   source column -> (required_column, temperature_column)
HEAT_FIELD_MAP = {
    "Heat adjacent": ("heat_adjacent_required", "heat_adjacent_temperature"),
    "Heat req V":    ("heating_required_voyage", "heating_voyage_temperature"),
    "Heat req D":    ("heating_required_discharge", "heating_discharge_temperature"),
}

# Valid PostgreSQL ENUM values for each enum column
# Maps PostgreSQL enum column name -> set of valid values (case-sensitive)
ENUM_VALID_VALUES = {
    "electrical_equipment_temperature_class": {"T1", "T2", "T3", "T4", "T5", "T6"},
    "electrical_equipment_apparatus_group": {"IIA", "IIB", "IIC"},
    "flashpoint_requirement": {"Yes", "No", "NF"},
    "gauging": {"O", "R", "C"},
    "vapour_detection": {"F", "T", "FT", "No"},
    "fire_protection": {"A", "B", "C", "D", "NO", "AC"},
}


# ===========================================================================
# FILE I/O & DETECTION
# ===========================================================================

def find_header_row(path: Path, sheet, suffix: str, max_scan: int = 8) -> int:
    """Locate the real header row of a messy Excel file (0 = first row).

    Some legacy sheets (e.g. the CHEM products table) carry a title/preamble row
    above the actual column headers. We scan the first few rows for the CHEM
    header signature ('PRODUCT' plus a 'CORR...' column) and return its index.
    CSV files and normal sheets fall through to 0, leaving existing behaviour
    unchanged.
    """
    if suffix not in (".xls", ".xlsx"):
        return 0
    try:
        raw = pd.read_excel(path, sheet_name=sheet if sheet is not None else 0,
                            header=None, nrows=max_scan, dtype=str, keep_default_na=False)
    except Exception:
        return 0
    for i, row in raw.iterrows():
        cells = {re.sub(r"\s+", " ", str(v).strip()).upper() for v in row.values}
        if "PRODUCT" in cells and any("CORR" in c for c in cells):
            return int(i)
    return 0


def read_file(path: Path, sheet) -> pd.DataFrame:
    """Read a CSV or Excel file into a DataFrame with cleaned column names."""
    suffix = path.suffix.lower()
    log.info("Detecting file type from suffix '%s'", suffix)
    if suffix == ".csv":
        log.info("Reading CSV file: %s", path)
        df = pd.read_csv(path, keep_default_na=False, na_values=[])
    elif suffix in (".xlsx", ".xls"):
        # sheet defaults to the first sheet when sheet is None
        header_row = find_header_row(path, sheet, suffix)
        if header_row:
            log.info("Header detected on row %d (%d preamble row(s) above it)",
                     header_row, header_row)
        log.info("Reading Excel file: %s (sheet=%s)", path, sheet if sheet is not None else "<first>")
        df = pd.read_excel(path, sheet_name=sheet if sheet is not None else 0,
                           header=header_row, keep_default_na=False, na_values=[])
    else:
        raise ValueError(f"Unsupported file type '{suffix}'. Use .csv or .xlsx.")

    # Normalize column names: strip and collapse internal whitespace
    # (so 'DENSITY  AT 15°C' -> 'DENSITY AT 15°C').
    df.columns = [re.sub(r"\s+", " ", str(c).strip()) for c in df.columns]
    log.info("Read %d rows, %d columns from %s", len(df), len(df.columns), path.name)
    return df


def detect_format(columns: list) -> Tuple[str, Dict[str, Any]]:
    """
    Auto-detect file format based on column names.
    
    Returns:
        Tuple of (format_name: str, config: Dict)
        format_name: 'lars' or 'ibc'
        config: FORMAT_CONFIG for the detected format
    
    Raises:
        ValueError if format cannot be detected
    """
    col_set = set(columns)
    log.info("Detecting format from %d columns: %s", len(col_set), sorted(col_set))
    
    # Check for format indicators (need at least 50% match)
    lars_matches = col_set & LARS_DETECTOR
    ibc_matches = col_set & IBC_DETECTOR
    chem_matches = col_set & CHEM_DETECTOR

    log.info("LARS format detector matches: %s (%d/%d)", lars_matches, len(lars_matches), len(LARS_DETECTOR))
    log.info("IBC format detector matches: %s (%d/%d)", ibc_matches, len(ibc_matches), len(IBC_DETECTOR))
    log.info("CHEM format detector matches: %s (%d/%d)", chem_matches, len(chem_matches), len(CHEM_DETECTOR))

    if len(chem_matches) >= 2:
        log.info("✓ Format detected: CHEM (product density/correction table)")
        return "chem", FILE_FORMAT_CONFIG["chem"]

    if len(lars_matches) >= 2:
        log.info("✓ Format detected: LARS (parent-child with synonyms)")
        return "lars", FILE_FORMAT_CONFIG["lars"]

    if len(ibc_matches) >= 2:
        log.info("✓ Format detected: IBC (independent records, no synonyms)")
        return "ibc", FILE_FORMAT_CONFIG["ibc"]

    raise ValueError(
        f"Could not detect file format. Expected LARS {list(LARS_DETECTOR)}, "
        f"IBC {list(IBC_DETECTOR)}, or CHEM {list(CHEM_DETECTOR)} column names."
    )


def get_table_columns(cur, table: str) -> dict:
    """Return {column_name: data_type} for a table in the public schema."""
    log.info("Querying schema for columns of table '%s'", table)
    cur.execute(
        """
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        """,
        (table,),
    )
    cols = {row[0]: row[1] for row in cur.fetchall()}
    log.info("Table '%s' has %d columns: %s", table, len(cols), list(cols))
    return cols


def derive_source_name(path: Path) -> str:
    """Source name = file name up to the first extension.

    'Lars Stole Birkeland - Chemical Cargo specifications - 2002.xlsx - CGOSPEC.csv'
    -> 'Lars Stole Birkeland - Chemical Cargo specifications - 2002'
    """
    name = path.name
    for ext in (".xlsx", ".xls", ".csv"):
        if ext in name:
            return name.split(ext)[0].strip()
    return path.stem.strip()


def normalize_source_name(s: str) -> str:
    """Normalize a source name for fuzzy comparison.

    - lowercase
    - unify dashes: em/en dash -> hyphen
    - drop a trailing year (e.g. ' - 2002')
    - collapse whitespace
    """
    s = s.lower()
    s = s.replace("—", "-").replace("–", "-")  # — – -> -
    s = re.sub(r"[-\s]*\d{4}\s*$", "", s)                 # trailing year
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def get_source_id(cur, name):
    """Return source.id whose name fuzzy-matches `name`, or None if none do."""
    target = normalize_source_name(name)
    log.info("Looking up source by normalized name: %r", target)
    cur.execute("SELECT id, name FROM source")
    for sid, sname in cur.fetchall():
        if normalize_source_name(sname) == target:
            log.info("Matched source id=%s (%r)", sid, sname)
            return sid
    return None


def _alnum(s: str) -> str:
    """Lowercase and reduce to space-separated alphanumeric tokens."""
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def get_source_id_partial(cur, name):
    """Return source.id that PARTIALLY matches `name`, or None.

    Compares on alphanumeric-lowercased text: a match is when either the stored
    source name is contained in `name` or vice versa (e.g. file
    'Unknown - Products CHEM - 1996' vs a source named 'Products CHEM'). The
    longest such match wins so a short generic name can't shadow a specific one.
    """
    target = _alnum(name)
    log.info("Looking up source by PARTIAL name match: %r", target)
    cur.execute("SELECT id, name FROM source")
    best = None  # (match_len, id, name)
    for sid, sname in cur.fetchall():
        ns = _alnum(sname)
        if ns and (ns in target or target in ns):
            if best is None or len(ns) > best[0]:
                best = (len(ns), sid, sname)
    if best is None:
        return None
    log.info("Partial source match id=%s (%r) for %r", best[1], best[2], name)
    return best[1]


def create_source(cur, name):
    """Insert a new source row named `name` and return its id."""
    log.info("Creating new source row: %r", name)
    cur.execute(
        "INSERT INTO source (name, source_type, notes, date_ingested, created_at, updated_at) "
        "VALUES (%s, %s, %s, now(), now(), now()) RETURNING id",
        (name, "reference", "Auto-created during cargo_chemicals import"),
    )
    sid = cur.fetchone()[0]
    log.info("✓ Created source id=%s (%r)", sid, name)
    return sid




# ===========================================================================
# DATA NORMALIZATION & VALUE COERCION
# ===========================================================================

def normalize_value(value: Any) -> Optional[Any]:
    """
    Normalize a single cell value for database insertion.
    
    Handles:
      - Empty values and NaN -> None (SQL NULL)
      - Placeholder characters (dashes, ?, N/A) -> None (SQL NULL)
      - Boolean conversions: Y/N, Yes/No, TRUE/FALSE
      - Numeric string conversions: int or float
      - Whitespace trimming
      - Blank strings -> None
    
    Returns:
        Normalized value or None if empty/invalid
    """
    # pandas uses NaN/NaT for empty cells -> store as SQL NULL
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if pd.isna(value):
        return None
    
    # Blank / whitespace-only cells -> SQL NULL
    s = str(value).strip()
    if s == "":
        return None
    
    # Placeholder characters used to indicate missing/unknown values -> SQL NULL
    # Common placeholders: dashes (-, –, —), question mark, N/A, etc.
    placeholders = {"–", "—", "-", "?", "n/a", "na", "n.a", "none", "unknown", ""}
    if s.lower() in placeholders:
        return None
    
    return s


def coerce_value(value: Any, data_type: str) -> Optional[Any]:
    """
    Convert a single cell value to the target database type.
    
    Calls normalize_value() first, then applies type-specific conversion.
    
    Args:
        value: Raw value from DataFrame
        data_type: PostgreSQL data type (boolean, integer, numeric, text, etc.)
    
    Returns:
        Coerced value suitable for psycopg2 insertion, or None for NULL
    
    Raises:
        ValueError if value cannot be coerced to target type
    """
    # First normalize the raw value
    norm = normalize_value(value)
    if norm is None:
        return None
    
    s = norm  # Already normalized string
    
    if data_type == "boolean":
        low = s.lower()
        if low in TRUE_SET:
            return True
        if low in FALSE_SET:
            return False
        # Don't raise error; return None for unrecognized booleans
        log.warning("Cannot interpret '%s' as boolean -> storing NULL", value)
        return None
    
    if data_type in ("integer", "bigint", "smallint"):
        # Extract the leading (optionally negative) integer from messy cells.
        m = re.search(r"-?\d+", s)
        if not m:
            return None
        return int(m.group())
    
    if data_type in ("numeric", "double precision", "real"):
        # Accept a leading-dot decimal like ".790" (no integer part). The old
        # pattern required a digit before the point, so ".790" matched "790".
        m = re.search(r"-?(?:\d+\.?\d*|\.\d+)", s)
        if not m:
            return None
        return float(m.group())
    
    # Check if this is an enum type with known valid values
    if data_type in ENUM_VALID_VALUES:
        valid_values = ENUM_VALID_VALUES[data_type]
        if s in valid_values:
            return s
        # Invalid enum value -> treat as NULL
        log.warning("Invalid value '%s' for enum %s (valid: %s) -> storing NULL", 
                    s, data_type, valid_values)
        return None
    
    # text / varchar / timestamp / json etc. -> return the normalized string
    return s


def parse_heating_value(value: Any) -> Tuple[Optional[bool], Optional[float]]:
    """Split a heating cell into (required, temperature).

    The source cells for "Heat adjacent" / "Heat req V" / "Heat req D" mix a
    Y/N flag with an optional temperature:

        "N"/"No"  -> (False, None)   heating not required
        "Y"/"Yes" -> (True,  None)   required, no temperature specified
        number    -> (True,  float)  required, at the given min/max temperature
        blank     -> (None,  None)   no data (both columns are omitted)
    """
    norm = normalize_value(value)
    if norm is None:
        return (None, None)
    low = norm.lower()
    if low in FALSE_SET:
        return (False, None)
    if low in TRUE_SET:
        return (True, None)
    m = re.search(r"-?(?:\d+\.?\d*|\.\d+)", norm)
    if m:
        return (True, float(m.group()))
    log.warning("Cannot interpret '%s' as a heating value -> storing NULL", value)
    return (None, None)


def expand_heating_fields(record: "pd.Series", result: Dict[str, Any],
                          table_cols: Dict[str, str]) -> Dict[str, Any]:
    """Populate the normalized heating columns from their raw source cells.

    For each source column in HEAT_FIELD_MAP that is present in the row (and
    whose target columns exist in the schema), set the *_required Boolean and,
    when the cell carries a number, the *_temperature. Mutates and returns
    `result`. Columns are skipped silently if the schema has not been migrated
    yet, so this stays safe against an older database.
    """
    for src_col, (req_col, temp_col) in HEAT_FIELD_MAP.items():
        if src_col not in record or req_col not in table_cols:
            continue
        required, temperature = parse_heating_value(record[src_col])
        if required is not None:
            result[req_col] = required
        if temperature is not None and temp_col in table_cols:
            result[temp_col] = temperature
    return result


# ===========================================================================
# UN NUMBER PARSING
# ===========================================================================

# Qualifier tokens that indicate a "physical_state" UN-number entry. Anything
# containing '%' is treated as "concentration"; everything else defaults to
# "other" so no information is silently dropped.
UN_PHYSICAL_STATE_WORDS = {
    "compressed", "refrigerated", "molten", "solid", "liquid", "gas", "gaseous",
    "crude", "refined", "frozen", "solution", "solutions", "soln", "aqueous",
    "anhydrous", "hydrated", "fused", "granular", "powder", "flake", "flaked",
    "pellet", "pellets", "dry", "wet",
}


def _classify_un_qualifier(qualifier: str) -> str:
    """Infer qualifier_type ('concentration' | 'physical_state' | 'other')."""
    q = qualifier.lower()
    if "%" in q:
        return "concentration"
    if any(word in q for word in UN_PHYSICAL_STATE_WORDS):
        return "physical_state"
    return "other"


def parse_un_numbers(value: Any) -> "list[Tuple[str, Optional[str], Optional[str]]]":
    """Split a raw UN-number cell into (un_number, qualifier_type, qualifier_value).

    Entries are separated by ';'. Within an entry, an optional 'qualifier: number'
    colon marks a qualifier; its type is inferred and the label is stored (with a
    capitalised first letter) as qualifier_value. Without a qualifier both columns
    are NULL. Only 3-4 digit UN numbers are accepted, so junk cells ("Y",
    "MIXTURE", chemical formulas) yield no rows.

        "1089"                                 -> [("1089", None, None)]
        "10%-80%: 2790; Glacial, >80%: 2789"   -> [("2790","concentration","10%-80%"),
                                                    ("2789","concentration","Glacial, >80%")]
        "compressed: 1971; refrigerated: 1972" -> [("1971","physical_state","Compressed"),
                                                    ("1972","physical_state","Refrigerated")]
        "Molten crude or refined: 2304; 1334"  -> [("2304","physical_state","Molten crude or refined"),
                                                    ("1334", None, None)]
    """
    if value is None:
        return []
    text = str(value).strip()
    if not text:
        return []

    out: list = []
    for part in text.split(";"):
        part = part.strip()
        if not part:
            continue
        qualifier = None
        num_str = part
        if ":" in part:
            left, right = part.rsplit(":", 1)
            # Only treat the colon as a qualifier separator if a UN number
            # actually follows it (avoids splitting stray colons).
            if re.search(r"\d{3,4}", right):
                qualifier = left.strip() or None
                num_str = right
        m = re.search(r"\d{3,4}", num_str)
        if not m:
            continue
        un_number = m.group()
        if qualifier:
            qtype = _classify_un_qualifier(qualifier)
            qval = qualifier[:1].upper() + qualifier[1:]
        else:
            qtype = qval = None
        out.append((un_number, qtype, qval))
    return out


# ===========================================================================
# ROW CLASSIFICATION & NORMALIZATION
# ===========================================================================

def is_parent_row(record: pd.Series, format_config: Dict[str, Any]) -> bool:
    """
    Determine if a row is a parent (cargo) or child (synonym) in LARS format.
    
    Parent rows have:
      - Non-empty canonical_name (first column)
      - At least one non-name property field set
    
    Child rows have:
      - Non-empty synonym_text (COMMODITIES column)
      - Empty canonical_name
    
    Args:
        record: DataFrame row
        format_config: Configuration for detected format
    
    Returns:
        True if this is a parent (cargo) row, False if child (synonym)
    """
    if not format_config.get("has_parent_child"):
        return True  # IBC format: all rows are parents
    
    parent_col = format_config.get("parent_column", "canonical_name")
    synonym_col = format_config.get("synonym_column")
    
    parent_name = str(record.get(parent_col, "")).strip()
    synonym_text = str(record.get(synonym_col, "")).strip()
    
    # If has parent name but no synonym, it's a parent
    if parent_name and not synonym_text:
        return True
    
    # If has synonym but no parent name, it's a child
    if synonym_text and not parent_name:
        return False
    
    # If has both, it's a parent (the synonym is an alternate name, not a child)
    if parent_name and synonym_text:
        return True
    
    # If neither, it's empty (will be skipped anyway)
    return True


def normalize_row_to_json(record: pd.Series, format_config: Dict[str, Any],
                          table_cols: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """
    Convert a DataFrame row into a normalized JSON object.
    
    Keys are database column names (after mapping). Values are normalized and
    coerced to database types. Empty values are omitted (not included in dict).
    
    Args:
        record: DataFrame row
        format_config: Configuration for detected format
        table_cols: {column_name: data_type} from database schema
    
    Returns:
        Normalized dict with database column names, or None if invalid
    """
    mapping = format_config.get("mapping", {})
    result = {}
    
    for file_col, file_val in record.items():
        # Map file column to database column
        db_col = mapping.get(file_col, file_col)
        
        # Skip if not in database schema
        if db_col not in table_cols:
            continue
        
        # Skip synonym column (handled separately)
        if file_col == format_config.get("synonym_column"):
            continue
        
        # Normalize and coerce the value
        data_type = table_cols[db_col]
        coerced = coerce_value(file_val, data_type)
        
        # Only include non-None values (omit empty)
        if coerced is not None:
            result[db_col] = coerced

    # Heating columns need a source cell -> (required, temperature) split.
    expand_heating_fields(record, result, table_cols)

    return result if result else None


def extract_synonyms_from_row(record: pd.Series, format_config: Dict[str, Any]) -> list:
    """
    Extract synonym names from a row (LARS format only).
    
    In LARS format, the COMMODITIES column contains comma-separated or
    multi-line synonyms. Each non-empty synonym becomes a separate entry.
    
    Args:
        record: DataFrame row
        format_config: Configuration for detected format
    
    Returns:
        List of normalized synonym dicts, or empty list if none
    """
    if not format_config.get("has_parent_child"):
        return []  # IBC format has no synonyms in the row
    
    synonym_col = format_config.get("synonym_column")
    if not synonym_col:
        return []
    
    synonym_text = str(record.get(synonym_col, "")).strip()
    if not synonym_text:
        return []
    
    # Return a single synonym (could split on comma if needed)
    return [{"synonym_text": synonym_text}]



# ===========================================================================
# MAIN ETL PROCESSOR
# ===========================================================================

def main():
    """
    Main ETL workflow: detect format, process rows, and insert into database.
    
    Workflow:
      1. Read the input file (CSV or XLSX)
      2. Auto-detect format based on column names (LARS or IBC)
      3. Connect to database and retrieve schema
      4. Resolve source ID by file name
      5. Process and normalize all rows
      6. For LARS format: separate parent (cargo) and child (synonym) rows
      7. Insert parent rows into cargo_chemical
      8. (Optional: insert synonym rows into synonyms table - see cargo_synonym.py)
      9. Commit transaction on success, rollback on error
    """
    parser = argparse.ArgumentParser(
        description="Multi-format ETL loader for chemical cargo data (LARS & IBC formats)."
    )
    # --- Set these two defaults to run without command-line arguments -------
    DEFAULT_FILE = input_file("Lars Stole Birkeland - Chemical Cargo specifications - 2002.xlsx - CGOSPEC.csv")
    DEFAULT_TABLE = "cargo_chemical"
    # ------------------------------------------------------------------------
    parser.add_argument("file", nargs="?", default=DEFAULT_FILE,
                        help="Path to the .csv or .xlsx file")
    parser.add_argument("--table", default=DEFAULT_TABLE, 
                        help="Target table name (default: cargo_chemical)")
    parser.add_argument("--sheet", default=None, 
                        help="Excel sheet name (Excel only)")
    parser.add_argument("--truncate", action="store_true",
                        help="Empty the table before loading")
    parser.add_argument("--create-source", action="store_true",
                        help="If no source matches the file name, create one "
                             "(CHEM format does this automatically)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and validate, but do not write to the DB")
    args = parser.parse_args()
    
    log.info("=" * 80)
    log.info("CHEMICAL CARGO ETL LOADER - Starting")
    log.info("=" * 80)
    log.info("file=%s table=%s sheet=%s truncate=%s dry_run=%s",
             args.file, args.table, args.sheet, args.truncate, args.dry_run)

    # =========================================================================
    # STEP 1: Validate input file
    # =========================================================================
    path = Path(args.file)
    log.info("Checking that file exists: %s", path)
    if not path.is_file():
        sys.exit(f"Error: file not found: {path}")
    log.info("✓ File exists")

    # =========================================================================
    # STEP 2: Load environment and database connection
    # =========================================================================
    log.info("Loading environment from .env")
    load_dotenv(Path(__file__).parent.parent / ".env")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        sys.exit("Error: DATABASE_URL not set (checked .env).")
    log.info("✓ DATABASE_URL loaded")

    # =========================================================================
    # STEP 3: Read file and detect format
    # =========================================================================
    log.info("Reading input file...")
    df = read_file(path, args.sheet)
    log.info("✓ Loaded %d rows, %d columns", len(df), len(df.columns))
    
    log.info("Detecting file format based on column names...")
    try:
        file_format, format_config = detect_format(df.columns)
        log.info("✓ Format: %s", file_format)
        log.info("  - Has parent-child relationships: %s", format_config["has_parent_child"])
        if format_config["synonym_column"]:
            log.info("  - Synonym column: %s", format_config["synonym_column"])
    except ValueError as e:
        sys.exit(f"Error: {e}")

    # =========================================================================
    # STEP 4: Connect to database
    # =========================================================================
    log.info("Connecting to the database...")
    conn = psycopg2.connect(db_url)
    log.info("✓ Database connection established")
    
    try:
        with conn.cursor() as cur:
            # ===================================================================
            # STEP 5: Retrieve database schema
            # ===================================================================
            table_cols = get_table_columns(cur, args.table)
            if not table_cols:
                sys.exit(f"Error: table '{args.table}' not found in the database.")
            log.info("✓ Retrieved %d columns from %s", len(table_cols), args.table)

            # ===================================================================
            # STEP 6: Resolve source by file name
            # ===================================================================
            source_name = derive_source_name(path)
            log.info("Resolving source from file name: %r", source_name)
            # 1) exact (normalized) match, then 2) partial match by name.
            source_id = get_source_id(cur, source_name)
            if source_id is None:
                source_id = get_source_id_partial(cur, source_name)
            # 3) still nothing: create it (auto for CHEM, or with --create-source).
            if source_id is None:
                if args.dry_run:
                    log.info("Source %r not found; would be CREATED (dry-run).", source_name)
                elif file_format == "chem" or args.create_source:
                    source_id = create_source(cur, source_name)
                else:
                    log.warning("Source %r not found in source table -> skipping load. "
                                "Pass --create-source to create it.", source_name)
                    sys.exit(0)
            log.info("✓ Source resolved: id=%s", source_id)

            # ===================================================================
            # STEP 7: Process and normalize rows
            # ===================================================================
            log.info("=" * 80)
            log.info("PROCESSING ROWS")
            log.info("=" * 80)
            
            parent_rows = []    # Rows for cargo_chemical table
            child_rows = []     # Rows for synonyms table (LARS only)
            skipped_rows = 0
            
            for idx, record in df.iterrows():
                line = idx + 2  # +2: header row + 1-based indexing
                
                # Classify row (parent or child)
                is_parent = is_parent_row(record, format_config)
                
                if is_parent:
                    # ========== PARENT ROW (INSERT TO CARGO_CHEMICAL) ==========
                    normalized = normalize_row_to_json(record, format_config, table_cols)
                    
                    # Check key column is not empty
                    key_col = format_config.get("key_column", "canonical_name")
                    key_val = normalized.get(key_col) if normalized else None

                    if key_val is None:
                        skipped_rows += 1
                        log.info("✗ row %d SKIP (empty %s)", line, key_col)
                        continue

                    # Skip junk names shorter than the format's minimum (e.g. the
                    # stray single-letter A-Z section markers in the CHEM file).
                    min_len = format_config.get("min_name_length", 1)
                    if len(str(key_val).strip()) < min_len:
                        skipped_rows += 1
                        log.info("✗ row %d SKIP (%s too short: %r)", line, key_col, key_val)
                        continue

                    # Inject any constant fields for this format (only when the
                    # measurement they describe is present, e.g. reference temp
                    # is only meaningful alongside a density value).
                    for cf, cv in format_config.get("constant_fields", {}).items():
                        if cf in table_cols and "density_g_cm3" in normalized:
                            normalized.setdefault(cf, cv)

                    # Add source ID
                    normalized[SOURCE_ID_COLUMN] = source_id
                    parent_rows.append(normalized)
                    log.info("✓ row %d PARENT: %s", line, json.dumps(normalized, default=str))
                    
                else:
                    # ========== CHILD ROW (EXTRACT SYNONYM) ==========
                    # For LARS format: extract synonyms for later processing
                    synonyms = extract_synonyms_from_row(record, format_config)
                    for syn in synonyms:
                        child_rows.append(syn)
                        log.info("✓ row %d CHILD (SYNONYM): %s", line, syn)
            
            log.info("=" * 80)
            log.info("PROCESSING COMPLETE")
            log.info("=" * 80)
            log.info("Prepared %d parent rows (inserts to %s)", len(parent_rows), args.table)
            log.info("Prepared %d child rows (synonyms for separate processing)", len(child_rows))
            log.info("Skipped %d rows", skipped_rows)

            if parent_rows:
                preview_rows = parent_rows[:10]
                log.info("Preview of first %d normalized parent rows:\n%s",
                         len(preview_rows), json.dumps(preview_rows, default=str, indent=2))

            # ===================================================================
            # STEP 8: Dry-run or commit
            # ===================================================================
            if args.dry_run:
                log.info("DRY RUN: %d rows ready (nothing written).", len(parent_rows))
                return

            if not parent_rows:
                log.info("No rows to insert.")
                conn.commit()
                return

            # ===================================================================
            # STEP 9: Insert into database
            # ===================================================================
            log.info("=" * 80)
            log.info("INSERTING INTO DATABASE")
            log.info("=" * 80)
            
            if args.truncate:
                log.info("Truncating table '%s' before load", args.table)
                cur.execute(sql.SQL("TRUNCATE TABLE {} RESTART IDENTITY CASCADE")
                            .format(sql.Identifier(args.table)))
                log.info("✓ Truncated '%s'", args.table)

            # Convert normalized dicts to DB-ready tuples
            # All parent rows must have the same columns
            if parent_rows:
                # Get all unique columns from all rows
                all_cols = set()
                for row in parent_rows:
                    all_cols.update(row.keys())
                
                db_cols = sorted(all_cols)
                log.info("Columns to insert: %s", db_cols)
                
                # Convert dicts to tuples in column order
                row_tuples = []
                for row_dict in parent_rows:
                    row_tuple = tuple(row_dict.get(col) for col in db_cols)
                    row_tuples.append(row_tuple)
                
                log.info("Building INSERT statement for %d columns, %d rows", 
                         len(db_cols), len(row_tuples))
                insert = sql.SQL("INSERT INTO {} ({}) VALUES %s").format(
                    sql.Identifier(args.table),
                    sql.SQL(", ").join(sql.Identifier(c) for c in db_cols),
                )
                
                log.info("Executing batch insert...")
                execute_values(cur, insert, row_tuples)
                log.info("✓ Batch insert completed")
            
            # ===================================================================
            # STEP 10: Commit transaction
            # ===================================================================
            log.info("Committing transaction...")
            conn.commit()
            log.info("✓ Committed")
            log.info("Inserted %d rows into '%s'", len(parent_rows), args.table)
            
            if child_rows:
                log.info("")
                log.info("Note: %d synonyms extracted. Run cargo_synonym.py to link them.", 
                         len(child_rows))
            
    except Exception as e:
        log.exception("Load failed - rolling back transaction")
        conn.rollback()
        raise
    finally:
        log.info("Closing database connection")
        conn.close()
        log.info("=" * 80)
        log.info("ETL COMPLETE")
        log.info("=" * 80)


if __name__ == "__main__":
    main()
