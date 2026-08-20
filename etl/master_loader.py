#!/usr/bin/env python3
"""
MASTER ETL loader — ingest one source file into the full cargo schema.

For a given input file this orchestrates, in a single transaction:

  1. cargo_chemical          — one row per chemical (mapped, type-coerced)
  2. synonyms                — inserted only if the normalized text is new
  3. cargo_synonym           — link every chemical to its synonyms
  4. cargo_property_values   — per-source physical properties (density, LEL, …)
  5. cleaning_process        — one row per (chemical, cleaning method)
  6. cleaning_process_step   — the ordered steps of each method

It follows the same "format registry" idea as cargo_chemicals.py. Four formats
are recognised by their column signature:

    LARS   — parent/child spreadsheet (synonyms in COMMODITIES)
    IBC    — flat regulatory records
    CHEM   — density/correction lookup
    MIRACLE— "Miracle Tank Cleaning Guide" workbook

The FULL pipeline applies to ANY file — it is not tied to a specific format.
Each stage runs only where the file actually provides that data (see
active_stages): every file loads cargo_chemical; synonyms load if the format
declares a synonym column that is present; properties load if its property
columns are present; cleaning loads if its cleaning sheet exists in the
workbook. So MIRACLE (which has all of them) runs every stage, while a file that
only has chemicals just loads chemicals — same code path, no special-casing.
Mappings and helpers are reused from cargo_chemicals.py and miracle_2007.py.

Usage:
    python3 master_loader.py                         # DEFAULT_FILE, auto-detect
    python3 master_loader.py path/to/file.xlsx
    python3 master_loader.py --sheet Chemicals
    python3 master_loader.py --wipe-source           # delete this source's rows first (idempotent reload)
    python3 master_loader.py --dry-run               # parse + log, write nothing
    python3 master_loader.py --limit 20              # only process first N chemicals

Reads DATABASE_URL from the .env file in this directory.
"""

import argparse
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from _paths import input_file
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import psycopg2
from psycopg2 import sql
from psycopg2.extras import execute_values
from dotenv import load_dotenv

# Reuse the existing mappings + pure helpers (importing these modules is safe;
# their main() only runs under __main__).
from cargo_chemicals import (
    coerce_value,
    LARS_MAPPING, IBC_MAPPING, CHEM_MAPPING,
    LARS_DETECTOR, IBC_DETECTOR, CHEM_DETECTOR,
    expand_heating_fields, parse_un_numbers,
    get_source_id, get_source_id_partial, create_source, derive_source_name,
)
from miracle_2007 import MIRACLE_MAPPING, clean as mclean

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("master_loader")

# ---------------------------------------------------------------------------
DEFAULT_FILE = input_file("Miracle Tank Cleaning Guide.xlsx")
ENTERED_BY = "etl:master_loader"
ENTRY_TYPE = "import"
DEFAULT_RELATIONSHIP = "common"     # cargo_synonym.relationship_type
DEFAULT_LANGUAGE = "en"             # synonyms.language
# ---------------------------------------------------------------------------

# MIRACLE workbook sheet names
MIRACLE_CHEM_SHEET = "Chemicals"
MIRACLE_STEPS_SHEET = "Cleaning Steps"
MIRACLE_DEFAULT_STAGE = "AFTER_DISCHARGE"   # tank cleaning guide = after discharge

# Detector for the Miracle format (Chemicals sheet columns)
MIRACLE_DETECTOR = {"Chemical Name", "IBC Name", "Synonyms", "CAS No.", "Entry No."}

# MIRACLE per-source property values: file column -> (field_name, unit, value_type)
# field_name must exist in field_definitions (seeded by field_definition.py).
MIRACLE_PROPERTY_MAP = {
    "Density (Kg/l)":           ("density", "kg/l", "number"),
    "Mol. Weight (g/mol)":      ("molecular_weight_g_mol", "g/mol", "number"),
    "Melting Point (deg C)":    ("melting_point", "°C", "number"),
    "Boiling Point (deg C)":    ("boiling_point", "°C", "number"),
    "Vapour Pressure (bar)":    ("vapour_pressure", "bar", "number"),
    "Viscosity (mPa*s)":        ("viscosity", "mPa·s", "number"),
    "Flash Point (deg C)":      ("flash_point", "°C", "number"),
    "LEL (% vol)":              ("lel", "% vol", "number"),
    "UEL (% vol)":              ("uel", "% vol", "number"),
    "Odour Limit (ppm)":        ("odour_limit", "ppm", "number"),
    "Water Solubility (% g/g)": ("water_solubility", "% g/g", "text"),
}

# LARS per-source property values: file column -> (field_name, unit, value_type).
# Column names are the whitespace-normalized headers (e.g. "SpGr " -> "SpGr").
# field_name intentionally matches MIRACLE_PROPERTY_MAP so the same physical
# property aligns across sources in cargo_property_values (its whole purpose).
# Columns without a field_definitions match (Temp, Correction factor, Colour) are
# omitted; they still land in the wide cargo_chemical columns via LARS_MAPPING.
LARS_PROPERTY_MAP = {
    "SpGr":          ("density", "kg/l", "number"),   # specific gravity ≈ density (g/cm³ = kg/l)
    "Boiling point": ("boiling_point", "°C", "number"),
    "Melting point": ("melting_point", "°C", "number"),
    "Flash point":   ("flash_point", "°C", "number"),
    "Solubility":    ("water_solubility", "g/g", "text"),
    # These LARS columns hold a heating TEMPERATURE (°C); the matching
    # cargo_chemical column is only a Boolean flag, so keep the number here.
    "Heat adjacent": ("heat_adjacent_temp_c", "°C", "number"),
    "Heat req V":    ("heating_voyage_temp_c", "°C", "number"),
    "Heat req D":    ("heating_discharge_temp_c", "°C", "number"),
}

# CHEM per-source property values: file column -> (field_name, unit, value_type).
# The CHEM density/correction-factor lookup table also feeds the wide
# cargo_chemical columns (density_g_cm3, correction_factor) via CHEM_MAPPING; the
# same numbers are recorded here per-source in cargo_property_values so they align
# with other sources' density values. field_name must exist in field_definitions.
CHEM_PROPERTY_MAP = {
    "DENSITY AT 15°C": ("density", "kg/l", "number"),
    "CORR. FACTOR":    ("correction_factor", None, "number"),
}

# ---------------------------------------------------------------------------
# USCG Chemical Data Guide (7th Edition) — flat, one row per chemical.
# ---------------------------------------------------------------------------
# Detector: snake_case headers unique to this file.
USCG_DETECTOR = {"chemical_name", "health_hazard_rating", "fire_grade",
                 "spill_or_leak_procedure", "uscg_code"}

# file column -> cargo_chemical column (identity / descriptive text only).
USCG_MAPPING = {
    "chemical_name":     "canonical_name",
    "molecular_formula": "molecular_formula",
    "appearance":        "appearance",
    "water_solubility":  "water_solubility",
    # Not a cargo_chemical column (build_cargo ignores it); this entry only lets
    # un_source_column() find the UN column so it flows to cargo_un_number.
    "un_number":         "un_number",
}

# Measurable properties -> cargo_property_values. Temperatures in the clean °C
# columns are stored as-is. The remaining columns use this 1990 US guide's own
# units (°F, mmHg, psi); per the project decision these are stored VERBATIM with
# the source unit labelled (not converted), so the raw number stays faithful and
# the unit column says exactly what the guide used. flammable_limits is a text
# range (e.g. "2.5 to 12.8%"), so it is stored as text, not coerced to a number.
USCG_PROPERTY_MAP = {
    "specific_gravity":          ("specific_gravity",          None,   "number"),
    "vapor_density":             ("vapour_density",            None,   "number"),
    "boiling_point":             ("boiling_point",             "°C",   "number"),
    "freezing_point":            ("melting_point",             "°C",   "number"),
    # Previously dropped — now stored raw, with the guide's original units.
    "vapor_pressure (mmHg)":     ("vapour_pressure",           "mmHg", "number"),
    "reid vapor pressure":       ("reid_vapor_pressure",       "psi",  "number"),
    "flash_point (F)":           ("flash_point",               "°F",   "number"),
    "autoignition_temperature":  ("auto_ignition_temperature", "°F",   "number"),
    "flammable_limits":          ("flammable_limits",          None,   "text"),
}

# Descriptive hazard columns -> cargo_hazard_data fields. NOTHING measurable goes
# here (those live in cargo_property_values); only free-text hazard narrative.
USCG_HAZARD_MAP = {
    "health_hazard_rating":    "health_hazard_rating",
    "general_note":            "general_hazard",
    "symptoms":                "symptoms",
    "short_term_exposure":     "short_exposure_tolerance",
    "first_aid":               "exposure_procedure",
    "fire_grade":              "fire_grade",
    "electrical_group":        "electrical_group",
    "fire_extinguishing_agent": "extinguishing_agents",
    "special_fire_procedures": "special_fire_procedure",
    "stability":               "stability",
    "incompatibility":         "material_compatibility",
    "cargo_group":             "cargo_compatibility_note",
    "spill_or_leak_procedure": "spill_procedure",
}

# cargo_hazard_data columns in insert order (the value tuple follows this order).
HAZARD_FIELDS = [
    "health_hazard_rating", "general_hazard", "symptoms",
    "short_exposure_tolerance", "exposure_procedure", "fire_grade",
    "electrical_group", "extinguishing_agents", "special_fire_procedure",
    "stability", "material_compatibility", "cargo_compatibility_note",
    "spill_procedure",
]

# The LARS "Heat adjacent" / "Heat req V" / "Heat req D" cells mix a Y/N flag
# with an optional temperature. They are split into a *_required Boolean plus a
# *_temperature column via expand_heating_fields (see build_cargo); the value is
# ALSO recorded in cargo_property_values through LARS_PROPERTY_MAP for
# cross-source alignment.

# Cleaning Steps sheet columns
STEPS_CHEM_COL = "Chemical Name"
STEPS_METHOD_COL = "Method"                 # "Method 1"
STEPS_METHOD_DESC_COL = "Method Description"
STEPS_STEP_COL = "Step"
STEPS_STEP_METHOD_COL = "Step Method"
STEPS_TIME_COL = "Time"
STEPS_TEMP_COL = "Temperature"
STEPS_MEDIUM_COL = "Medium"
STEPS_CLEANER_COL = "Cleaner Description"
STEPS_REMARK_COL = "Step Remark"

# Format registry. The pipeline is NOT hard-coded per format: it is derived from
# what each format declares it has (see active_stages). Any file gets the full
# pipeline — each stage runs only if the file actually provides that data:
#   cargo       — always (the mapping)
#   synonyms    — if "synonym_column" is set and present in the file
#   properties  — if "property_map" is non-empty and its columns are present
#   cleaning    — if "cleaning_sheet" is set and that sheet exists in the workbook
SEMICOLON = re.compile(r"\s*;\s*")

FORMAT_REGISTRY = {
    "miracle": {
        "detector": MIRACLE_DETECTOR, "min_match": 3,
        "mapping": MIRACLE_MAPPING,
        "synonym_column": "Synonyms", "synonym_split": SEMICOLON,
        "property_map": MIRACLE_PROPERTY_MAP,
        "hazard_map": {},
        "cleaning_sheet": MIRACLE_STEPS_SHEET,
    },
    "lars": {
        "detector": LARS_DETECTOR, "min_match": 2,
        "mapping": LARS_MAPPING,
        "synonym_column": "COMMODITIES", "synonym_split": None,  # one name per cell
        "property_map": LARS_PROPERTY_MAP,
        "hazard_map": {},
        "cleaning_sheet": None,
    },
    "ibc": {
        "detector": IBC_DETECTOR, "min_match": 2,
        "mapping": IBC_MAPPING,
        "synonym_column": None, "synonym_split": None,
        "property_map": {},
        "hazard_map": {},
        "cleaning_sheet": None,
    },
    "chem": {
        "detector": CHEM_DETECTOR, "min_match": 2,
        "mapping": CHEM_MAPPING,
        "synonym_column": None, "synonym_split": None,
        "property_map": CHEM_PROPERTY_MAP,
        "hazard_map": {},
        "cleaning_sheet": None,
    },
    "uscg": {
        "detector": USCG_DETECTOR, "min_match": 3,
        "mapping": USCG_MAPPING,
        "synonym_column": "synonyms", "synonym_split": SEMICOLON,
        "property_map": USCG_PROPERTY_MAP,
        "hazard_map": USCG_HAZARD_MAP,
        "cleaning_sheet": None,
    },
}


def un_source_column(mapping: Dict[str, str]) -> Optional[str]:
    """File column that carries the UN number for a format (maps to un_number)."""
    for file_col, db_col in mapping.items():
        if db_col == "un_number":
            return file_col
    return None


def active_stages(fmt: Dict[str, Any], df_columns, has_cleaning_sheet: bool) -> List[str]:
    """Full pipeline for any file — a stage is active only if the data exists."""
    cols = set(df_columns)
    stages = ["cargo"]
    if fmt.get("synonym_column") in cols:
        stages.append("synonyms")
    if any(c in cols for c in fmt.get("property_map", {})):
        stages.append("properties")
    if any(c in cols for c in fmt.get("hazard_map", {})):
        stages.append("hazard")
    if fmt.get("cleaning_sheet") and has_cleaning_sheet:
        stages.append("cleaning")
    return stages


def extract_synonyms(record: pd.Series, syn_col: Optional[str], split_re) -> List[str]:
    """Parse a synonym cell into a de-duplicated, order-preserving list.

    `split_re` splits multi-synonym cells (e.g. Miracle's ';'-separated list);
    when None the whole cell is a single synonym (e.g. LARS COMMODITIES).
    """
    if not syn_col:
        return []
    raw = mclean(record.get(syn_col))
    if raw is None:
        return []
    parts = split_re.split(raw) if split_re is not None else [raw]
    out: List[str] = []
    seen = set()
    for part in parts:
        name = part.strip()
        if not name:
            continue
        key = name.lower()
        if key not in seen:
            seen.add(key)
            out.append(name)
    return out


def queue_synonyms(record: pd.Series, cargo_id: int, fmt: Dict[str, Any],
                   syn_cache: "SynonymCache", source_id: int, dry_run: bool,
                   link_rows: List[tuple], seen_links: set) -> int:
    """Insert this row's synonyms (on miss) and queue cargo_synonym links.

    Returns the number of new links queued. Shared by parent rows (their own
    synonym cell) and, for parent/child files like LARS, by nameless child rows
    that carry a synonym belonging to the previous parent.
    """
    syns = extract_synonyms(record, fmt.get("synonym_column"), fmt.get("synonym_split"))
    added = 0
    for syn_text in syns:
        sid = syn_cache.ensure(syn_text, dry_run)
        if sid is None:
            continue
        key = (cargo_id, sid)
        if key in seen_links:
            continue
        seen_links.add(key)
        link_rows.append((cargo_id, sid, DEFAULT_RELATIONSHIP,
                          False, source_id if source_id != -1 else None,
                          False, None))
        added += 1
    return added


# ===========================================================================
# FILE I/O & FORMAT DETECTION
# ===========================================================================

def _norm_cols(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = [re.sub(r"\s+", " ", str(c).strip()) for c in df.columns]
    return df


def read_sheet(path: Path, sheet, header=0) -> pd.DataFrame:
    """Read one CSV/XLSX sheet with all cells as strings and clean headers.

    `header` is the 0-based row to use as the column header (pandas semantics);
    pass None to read every row as data (used to hunt for a banner-shifted header).
    """
    suffix = path.suffix.lower()
    if suffix == ".csv":
        df = pd.read_csv(path, dtype=str, keep_default_na=False, header=header)
    elif suffix in (".xlsx", ".xls"):
        df = pd.read_excel(path, sheet_name=sheet if sheet is not None else 0,
                           dtype=str, keep_default_na=False, header=header)
    else:
        raise ValueError(f"Unsupported file type '{suffix}'. Use .csv or .xlsx.")
    return _norm_cols(df)


def _detector_score(values) -> int:
    """Best detector overlap across all formats for a candidate header row."""
    cols = {re.sub(r"\s+", " ", str(v).strip()) for v in values}
    return max((len(cols & cfg["detector"]) for cfg in FORMAT_REGISTRY.values()),
               default=0)


def read_primary_sheet(path: Path, sheet, scan_rows: int = 15) -> pd.DataFrame:
    """Read a sheet, skipping any banner/title rows above the real header.

    Some exports (e.g. the CHEM workbook) put a title line above the column
    headers, so pandas takes the banner as the header and format detection
    fails. If row 0 doesn't look like a header, scan the first `scan_rows` rows
    for the one that best matches a known detector signature and promote it.
    """
    df = read_sheet(path, sheet)
    base = _detector_score(df.columns)
    if base >= 2:                       # smallest min_match across formats
        return df
    raw = read_sheet(path, sheet, header=None)
    best_i, best_score = None, base
    for i in range(min(len(raw), scan_rows)):
        score = _detector_score(raw.iloc[i].tolist())
        if score > best_score:
            best_score, best_i = score, i
    if best_i is not None:
        log.info("Header found on row %d (%d banner row(s) above skipped)", best_i, best_i)
        return read_sheet(path, sheet, header=best_i)
    return df


def detect_format(columns) -> Tuple[str, Dict[str, Any]]:
    """Pick the format whose detector signature best matches the columns."""
    col_set = set(columns)
    best = None  # (score, name)
    for name, cfg in FORMAT_REGISTRY.items():
        score = len(col_set & cfg["detector"])
        log.info("format %-8s detector match: %d/%d (need %d)",
                 name, score, len(cfg["detector"]), cfg["min_match"])
        if score >= cfg["min_match"] and (best is None or score > best[0]):
            best = (score, name)
    if best is None:
        raise ValueError("Could not detect file format from columns.")
    name = best[1]
    log.info("✓ Format detected: %s", name)
    return name, FORMAT_REGISTRY[name]


# ===========================================================================
# SCHEMA INTROSPECTION
# ===========================================================================

def get_table_columns(cur, table: str) -> Dict[str, str]:
    cur.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s",
        (table,),
    )
    return {r[0]: r[1] for r in cur.fetchall()}


def get_enum_map(cur, table: str) -> Dict[str, set]:
    """Return {column_name: {valid enum labels}} for USER-DEFINED enum columns."""
    cur.execute(
        """
        SELECT c.column_name, e.enumlabel
        FROM information_schema.columns c
        JOIN pg_type t ON t.typname = c.udt_name
        JOIN pg_enum e ON e.enumtypid = t.oid
        WHERE c.table_schema='public' AND c.table_name=%s
        """,
        (table,),
    )
    out: Dict[str, set] = {}
    for col, label in cur.fetchall():
        out.setdefault(col, set()).add(label)
    return out


def coerce_cell(value, col: str, data_type: str, enum_map: Dict[str, set]):
    """Coerce one cell to its DB type, validating enum columns against labels."""
    if col in enum_map:
        raw = mclean(value)
        if raw is None:
            return None
        if raw in enum_map[col]:
            return raw
        log.warning("row value %r invalid for enum %s (valid: %s) -> NULL",
                    raw, col, sorted(enum_map[col]))
        return None
    return coerce_value(value, data_type)


# ===========================================================================
# ROW -> cargo_chemical
# ===========================================================================

def build_cargo(record: pd.Series, mapping: Dict[str, str],
                table_cols: Dict[str, str], enum_map: Dict[str, set]) -> Optional[Dict[str, Any]]:
    """Map + coerce one row into a cargo_chemical dict (None-valued keys dropped)."""
    out: Dict[str, Any] = {}
    for file_col, val in record.items():
        db_col = mapping.get(file_col, file_col)
        if db_col not in table_cols:
            continue
        coerced = coerce_cell(val, db_col, table_cols[db_col], enum_map)
        if coerced is not None:
            out[db_col] = coerced
    # Split the heating cells into *_required Boolean + *_temperature columns.
    expand_heating_fields(record, out, table_cols)
    return out if out.get("canonical_name") else None


# ===========================================================================
# SYNONYMS
# ===========================================================================

def normalize_synonym(text: str) -> str:
    """Match import_synonyms.normalize: lowercase, strip punctuation, collapse ws."""
    s = text.lower()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


class SynonymCache:
    """Load synonyms.normalized_text -> id once; insert-on-miss and remember."""

    def __init__(self, cur):
        self.cur = cur
        cur.execute("SELECT normalized_text, id FROM synonyms")
        self.by_norm = {r[0]: r[1] for r in cur.fetchall()}
        self.inserted = 0
        self._fake = 0              # unique negative ids for dry-run linking
        log.info("Loaded %d existing synonyms into cache", len(self.by_norm))

    def ensure(self, text: str, dry_run: bool) -> Optional[int]:
        norm = normalize_synonym(text)
        if not norm:
            return None
        if norm in self.by_norm:
            return self.by_norm[norm]
        if dry_run:
            self._fake -= 1
            self.by_norm[norm] = self._fake   # distinct placeholder per synonym
            self.inserted += 1
            return self._fake
        self.cur.execute(
            "INSERT INTO synonyms (synonym_text, normalized_text, language) "
            "VALUES (%s, %s, %s) RETURNING id",
            (text.strip(), norm, DEFAULT_LANGUAGE),
        )
        sid = self.cur.fetchone()[0]
        self.by_norm[norm] = sid
        self.inserted += 1
        return sid


# ===========================================================================
# CLEANING STEPS
# ===========================================================================

def method_number(text: str) -> Optional[int]:
    m = re.search(r"(\d+)", str(text or ""))
    return int(m.group(1)) if m else None


def recipe_from_desc(desc: Optional[str]) -> Optional[str]:
    if not desc:
        return None
    m = re.search(r"Recipe is for ([^)]+)", desc)
    return m.group(1).strip() if m else None


def load_cleaning(cur, steps_df: pd.DataFrame, name_to_id: Dict[str, int],
                  source_id: int, dry_run: bool) -> Tuple[int, int, int]:
    """Insert cleaning_process + cleaning_process_step from the steps sheet.

    Groups the flattened rows by (chemical, method); one cleaning_process per
    group, its rows become ordered cleaning_process_step records.
    Returns (processes, steps, skipped_no_cargo).
    """
    # group key -> {"desc":..., "steps":[row,...]}
    groups: Dict[Tuple[str, str], Dict[str, Any]] = {}
    order: List[Tuple[str, str]] = []
    for _, row in steps_df.iterrows():
        chem = mclean(row.get(STEPS_CHEM_COL))
        method = mclean(row.get(STEPS_METHOD_COL))
        if not chem or not method:
            continue
        key = (chem.strip().lower(), method)
        if key not in groups:
            groups[key] = {"chem": chem, "method": method,
                           "desc": mclean(row.get(STEPS_METHOD_DESC_COL)),
                           "steps": []}
            order.append(key)
        groups[key]["steps"].append(row)

    processes = steps = skipped = 0
    for key in order:
        g = groups[key]
        cargo_id = name_to_id.get(key[0])
        if cargo_id is None:
            skipped += 1
            log.info("✗ cleaning SKIP (no cargo for %r)", g["chem"])
            continue
        mnum = method_number(g["method"]) or 1

        if dry_run:
            processes += 1
            steps += len(g["steps"])
            continue

        cur.execute(
            "INSERT INTO cleaning_process "
            "(cargo_id, source_id, cleaning_stage, method_number, title, recipe_for) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (cargo_id, source_id, cleaning_stage, method_number) DO UPDATE "
            "SET title = EXCLUDED.title RETURNING id",
            (cargo_id, source_id, MIRACLE_DEFAULT_STAGE, mnum,
             g["desc"], recipe_from_desc(g["desc"])),
        )
        proc_id = cur.fetchone()[0]
        processes += 1

        step_rows = []
        for row in g["steps"]:
            so = method_number(row.get(STEPS_STEP_COL))
            if so is None:
                continue
            step_rows.append((
                proc_id, so,
                mclean(row.get(STEPS_STEP_METHOD_COL)),
                mclean(row.get(STEPS_TIME_COL)),
                mclean(row.get(STEPS_TEMP_COL)),
                mclean(row.get(STEPS_MEDIUM_COL)),
                mclean(row.get(STEPS_CLEANER_COL)),
                None,  # description (unused for Miracle)
                mclean(row.get(STEPS_REMARK_COL)),
            ))
        if step_rows:
            execute_values(
                cur,
                "INSERT INTO cleaning_process_step "
                "(cleaning_process_id, step_order, method, duration, temperature, "
                "medium, cleaner, description, remarks) VALUES %s "
                "ON CONFLICT (cleaning_process_id, step_order) DO NOTHING",
                step_rows,
            )
            steps += len(step_rows)

    return processes, steps, skipped


# ===========================================================================
# MAIN
# ===========================================================================

def main():
    parser = argparse.ArgumentParser(description="Master ETL loader for the cargo schema.")
    parser.add_argument("file", nargs="?", default=DEFAULT_FILE, help="CSV/XLSX path")
    parser.add_argument("--sheet", default=None, help="primary sheet (default: auto/Chemicals)")
    parser.add_argument("--wipe-source", action="store_true",
                        help="delete this source's existing rows first (cascades to children)")
    parser.add_argument("--create-source", action="store_true",
                        help="create the source row if the file name matches none")
    parser.add_argument("--source-id", type=int, default=None,
                        help="force this source.id (skip file-name matching)")
    parser.add_argument("--limit", type=int, default=None, help="only first N chemicals")
    parser.add_argument("--dry-run", action="store_true", help="parse + log, write nothing")
    args = parser.parse_args()

    path = Path(args.file)
    if not path.is_file():
        sys.exit(f"Error: file not found: {path}")

    load_dotenv(Path(__file__).parent.parent / ".env")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        sys.exit("Error: DATABASE_URL not set (checked .env).")

    # --- read primary sheet + detect format --------------------------------
    is_xlsx = path.suffix.lower() in (".xlsx", ".xls")
    primary_sheet = args.sheet if args.sheet is not None else (MIRACLE_CHEM_SHEET if is_xlsx else None)
    try:
        df = read_primary_sheet(path, primary_sheet)
    except Exception:
        # sheet name may not exist for non-Miracle xlsx -> fall back to first sheet
        df = read_primary_sheet(path, 0)
    log.info("Loaded primary sheet: %d rows, %d cols", len(df), len(df.columns))

    fmt_name, fmt = detect_format(df.columns)

    if args.limit:
        df = df.head(args.limit)
        log.info("Limited to first %d chemicals", len(df))

    # --- optional cleaning sheet (any workbook that declares + contains it) --
    steps_df = None
    cleaning_sheet = fmt.get("cleaning_sheet")
    if cleaning_sheet and is_xlsx:
        try:
            steps_df = read_sheet(path, cleaning_sheet)
            log.info("Loaded cleaning sheet '%s': %d rows", cleaning_sheet, len(steps_df))
        except Exception as e:
            log.warning("Cleaning sheet %r not found: %s", cleaning_sheet, e)

    # Full pipeline for ANY file; each stage is active only where the file has
    # the data (a synonym column, property columns, or a cleaning sheet).
    pipeline = active_stages(fmt, df.columns, steps_df is not None)
    log.info("Pipeline for %s: %s", fmt_name, pipeline)

    # Diagnostic: surface any property source-column mismatch up front so a
    # header change can never silently produce zero cargo_property_values.
    if "properties" in pipeline:
        pmap = fmt["property_map"]
        present = [c for c in pmap if c in df.columns]
        missing = [c for c in pmap if c not in df.columns]
        log.info("Property source columns present: %d/%d %s", len(present), len(pmap), present)
        if missing:
            log.warning("Property source columns MISSING from file (0 values for these): %s", missing)
            log.warning("File columns are: %s", list(df.columns))

    log.info("Connecting to database")
    conn = psycopg2.connect(db_url)
    try:
        with conn.cursor() as cur:
            table_cols = get_table_columns(cur, "cargo_chemical")
            enum_map = get_enum_map(cur, "cargo_chemical")
            if not table_cols:
                sys.exit("Error: cargo_chemical table not found.")

            # --- resolve source -------------------------------------------
            source_name = derive_source_name(path)
            if args.source_id is not None:
                cur.execute("SELECT id FROM source WHERE id=%s", (args.source_id,))
                if cur.fetchone() is None:
                    sys.exit(f"Error: --source-id {args.source_id} not found in source table.")
                source_id = args.source_id
            else:
                source_id = get_source_id(cur, source_name) or get_source_id_partial(cur, source_name)
            if source_id is None:
                if args.dry_run:
                    log.info("Source %r not found; would CREATE (dry-run).", source_name)
                    source_id = -1
                elif args.create_source:
                    source_id = create_source(cur, source_name)
                else:
                    sys.exit(f"Source {source_name!r} not found. Pass --create-source.")
            log.info("Source id=%s (%r)", source_id, source_name)

            # --- optional idempotent wipe ---------------------------------
            if args.wipe_source and not args.dry_run and source_id != -1:
                cur.execute("DELETE FROM cargo_chemical WHERE source_id=%s", (source_id,))
                log.info("Wiped %d existing cargo_chemical rows for source %s (cascaded)",
                         cur.rowcount, source_id)

            # --- property field validation --------------------------------
            valid_fields = set()
            if "properties" in pipeline:
                cur.execute("SELECT field_name FROM field_definitions")
                valid_fields = {r[0] for r in cur.fetchall()}

            syn_cache = SynonymCache(cur) if "synonyms" in pipeline else None

            # --- pass 1: chemicals + synonyms + properties ----------------
            name_to_id: Dict[str, int] = {}
            # keyed by (cargo_id, field_name) so in-file duplicate names (which now
            # collapse to one cargo_id) can't produce a doubled ON CONFLICT key.
            prop_rows: Dict[Tuple[int, str], tuple] = {}
            link_rows: List[tuple] = []
            seen_links = set()
            # UN numbers -> cargo_un_number. Keyed by (cargo_id, un_number,
            # qualifier_value) so duplicate names / re-parsed rows can't double up.
            un_col = un_source_column(fmt["mapping"])
            un_rows: Dict[Tuple[int, str, str], tuple] = {}
            # Descriptive hazard info -> cargo_hazard_data, one row per cargo_id.
            hazard_rows: Dict[int, tuple] = {}
            n_chem = n_syn_links = n_skip = 0
            now = datetime.now()
            last_parent_id: Optional[int] = None   # parent/child files (LARS): child rows attach here

            for idx, record in df.iterrows():
                cargo = build_cargo(record, fmt["mapping"], table_cols, enum_map)
                if cargo is None:
                    # Parent/child layout (e.g. LARS): a nameless row can still
                    # carry a synonym in the synonym column that belongs to the
                    # previous named parent — attach it there instead of dropping it.
                    if "synonyms" in pipeline and last_parent_id is not None:
                        n_syn_links += queue_synonyms(
                            record, last_parent_id, fmt, syn_cache, source_id,
                            args.dry_run, link_rows, seen_links)
                    n_skip += 1
                    continue
                cargo["source_id"] = source_id if source_id != -1 else None
                cname = cargo["canonical_name"].strip()

                # upsert cargo_chemical (unique on source_id + canonical_name)
                if args.dry_run:
                    cargo_id = -(idx + 1)   # fake id for dry-run linking
                else:
                    cols = list(cargo.keys())
                    update_cols = [c for c in cols if c not in ("source_id", "canonical_name")]
                    set_clause = sql.SQL(", ").join(
                        [sql.SQL("{0} = EXCLUDED.{0}").format(sql.Identifier(c)) for c in update_cols]
                        + [sql.SQL("updated_at = now()")]
                    )
                    stmt = sql.SQL(
                        "INSERT INTO cargo_chemical ({}) VALUES ({}) "
                        "ON CONFLICT (source_id, canonical_name) DO UPDATE SET {} RETURNING id"
                    ).format(
                        sql.SQL(", ").join(sql.Identifier(c) for c in cols),
                        sql.SQL(", ").join(sql.Placeholder() for _ in cols),
                        set_clause,
                    )
                    cur.execute(stmt, [cargo[c] for c in cols])
                    cargo_id = cur.fetchone()[0]
                name_to_id[cname.lower()] = cargo_id
                last_parent_id = cargo_id
                n_chem += 1

                # synonyms + links (this row's own synonym cell)
                if "synonyms" in pipeline:
                    n_syn_links += queue_synonyms(
                        record, cargo_id, fmt, syn_cache, source_id,
                        args.dry_run, link_rows, seen_links)

                # property values
                if "properties" in pipeline:
                    for fcol, (fname, unit, vtype) in fmt["property_map"].items():
                        if fname not in valid_fields:
                            continue
                        raw = mclean(record.get(fcol))
                        if raw is None:
                            continue
                        norm = coerce_value(raw, "numeric") if vtype == "number" else None
                        if vtype == "number" and norm is None:
                            continue        # a "number" field with a non-numeric cell (e.g. "N")
                        prop_rows[(cargo_id, fname)] = (
                            cargo_id, source_id if source_id != -1 else None, fname,
                            raw, norm, unit, vtype, now, ENTERED_BY, ENTRY_TYPE, True, False,
                        )

                # UN number(s) -> cargo_un_number (one row per parsed UN number)
                if un_col is not None:
                    for un_number, qtype, qval in parse_un_numbers(record.get(un_col)):
                        key = (cargo_id, un_number, qval or "")
                        un_rows[key] = (cargo_id, source_id if source_id != -1 else None,
                                        un_number, qtype, qval, None, now, now)

                # descriptive hazard info -> cargo_hazard_data (one row per cargo)
                if "hazard" in pipeline:
                    hz = {}
                    for fcol, dbf in fmt["hazard_map"].items():
                        val = mclean(record.get(fcol))
                        if val is not None:
                            hz[dbf] = val
                    if hz:
                        hazard_rows[cargo_id] = (
                            (cargo_id, source_id if source_id != -1 else None)
                            + tuple(hz.get(f) for f in HAZARD_FIELDS)
                            + (None, now, now)   # notes, created_at, updated_at
                        )

            n_props = len(prop_rows)
            n_un = len(un_rows)
            n_hazard = len(hazard_rows)

            # --- flush synonym links + property values --------------------
            if not args.dry_run:
                if link_rows:
                    execute_values(
                        cur,
                        "INSERT INTO cargo_synonym (cargo_id, synonym_id, relationship_type, "
                        "ambiguity_flag, source_id, preferred_for_search, notes) VALUES %s "
                        "ON CONFLICT (cargo_id, synonym_id) DO NOTHING",
                        link_rows,
                    )
                if prop_rows:
                    execute_values(
                        cur,
                        "INSERT INTO cargo_property_values (cargo_id, source_id, field_name, "
                        "value, normalized_value, unit, value_type, entered_date, entered_by, "
                        "entry_type, is_winning, conflict_flag) VALUES %s "
                        "ON CONFLICT (cargo_id, source_id, field_name) DO UPDATE SET "
                        "value = EXCLUDED.value, normalized_value = EXCLUDED.normalized_value, "
                        "unit = EXCLUDED.unit, value_type = EXCLUDED.value_type, "
                        "entered_date = EXCLUDED.entered_date, updated_at = now()",
                        list(prop_rows.values()),
                    )
                if un_rows:
                    # Idempotent: clear this run's cargoes then re-insert their UN
                    # numbers, so re-running the loader never accumulates duplicates.
                    # Scoped to this source so a chemical carrying UN numbers from
                    # several guides keeps the other sources' rows.
                    affected = list({r[0] for r in un_rows.values()})
                    cur.execute(
                        "DELETE FROM cargo_un_number WHERE cargo_id = ANY(%s) "
                        "AND source_id IS NOT DISTINCT FROM %s",
                        (affected, source_id if source_id != -1 else None),
                    )
                    execute_values(
                        cur,
                        "INSERT INTO cargo_un_number (cargo_id, source_id, un_number, "
                        "qualifier_type, qualifier_value, remarks, created_at, updated_at) "
                        "VALUES %s",
                        list(un_rows.values()),
                    )
                if hazard_rows:
                    # Idempotent: one row per (cargo_id, source_id); re-running
                    # updates the existing row instead of duplicating it.
                    execute_values(
                        cur,
                        "INSERT INTO cargo_hazard_data (cargo_id, source_id, "
                        "health_hazard_rating, general_hazard, symptoms, "
                        "short_exposure_tolerance, exposure_procedure, fire_grade, "
                        "electrical_group, extinguishing_agents, special_fire_procedure, "
                        "stability, material_compatibility, cargo_compatibility_note, "
                        "spill_procedure, notes, created_at, updated_at) VALUES %s "
                        "ON CONFLICT (cargo_id, source_id) DO UPDATE SET "
                        "health_hazard_rating = EXCLUDED.health_hazard_rating, "
                        "general_hazard = EXCLUDED.general_hazard, "
                        "symptoms = EXCLUDED.symptoms, "
                        "short_exposure_tolerance = EXCLUDED.short_exposure_tolerance, "
                        "exposure_procedure = EXCLUDED.exposure_procedure, "
                        "fire_grade = EXCLUDED.fire_grade, "
                        "electrical_group = EXCLUDED.electrical_group, "
                        "extinguishing_agents = EXCLUDED.extinguishing_agents, "
                        "special_fire_procedure = EXCLUDED.special_fire_procedure, "
                        "stability = EXCLUDED.stability, "
                        "material_compatibility = EXCLUDED.material_compatibility, "
                        "cargo_compatibility_note = EXCLUDED.cargo_compatibility_note, "
                        "spill_procedure = EXCLUDED.spill_procedure, "
                        "updated_at = now()",
                        list(hazard_rows.values()),
                    )

            # --- pass 2: cleaning -----------------------------------------
            n_proc = n_steps = n_clean_skip = 0
            if "cleaning" in pipeline and steps_df is not None:
                n_proc, n_steps, n_clean_skip = load_cleaning(
                    cur, steps_df, name_to_id, source_id, args.dry_run)

            # --- summary --------------------------------------------------
            log.info("=" * 70)
            log.info("SUMMARY (%s)", "DRY-RUN" if args.dry_run else "COMMIT")
            log.info("  cargo_chemical inserted : %d (skipped %d)", n_chem, n_skip)
            if "synonyms" in pipeline:
                log.info("  synonyms new / links    : %d / %d", syn_cache.inserted, n_syn_links)
            if "properties" in pipeline:
                log.info("  cargo_property_values   : %d", n_props)
            if un_col is not None:
                log.info("  cargo_un_number         : %d", n_un)
            if "hazard" in pipeline:
                log.info("  cargo_hazard_data       : %d", n_hazard)
            if "cleaning" in pipeline:
                log.info("  cleaning proc / steps   : %d / %d (skipped %d)",
                         n_proc, n_steps, n_clean_skip)
            log.info("=" * 70)

            if args.dry_run:
                log.info("Dry run: nothing written.")
                conn.rollback()
                return

            conn.commit()
            log.info("✓ Committed.")
    except Exception:
        log.exception("Load failed - rolling back")
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
