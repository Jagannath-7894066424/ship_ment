#!/usr/bin/env python3
"""
Import "Sittig's Handbook of Toxic & Hazardous Chemicals" (CSV, one row per entry).

Layout of the load:
  * cargo_chemical        -> canonical_name (chemical_name) + source_id ONLY.
  * cargo_property_values -> every other CSV column, one row per (cargo, column).
                             field_name comes from COLUMN_TO_FIELD; the raw cell
                             text goes in `value`, a parsed number (when the cell
                             is unambiguously numeric) in `normalized_value`.
  * synonyms / cargo_synonym -> the `synonyms` column, split on ';' and newlines.
                             Each name is looked up in synonyms by normalized_text;
                             an existing row is reused, otherwise a new synonyms row
                             is created. The link row carries this source's id.

A few CSV columns are deliberately mapped onto field names that other sources
already use (cas_registry_number -> cas_number, molecular_weight ->
molecular_weight_g_mol, vapor_pressure -> vapour_pressure, ...) so the same
physical property lines up across sources in cargo_property_values. Everything
else keeps its CSV column name. See COLUMN_TO_FIELD.

  ⚠ SOURCE FILE DAMAGE
  The CSV is not correctly quoted: values containing commas were written
  unquoted, so those rows split into more than the 73 header columns (every raw
  line is then padded out to 138 fields). ``repair_row`` undoes the two
  recoverable cases:
      1. a ", " join  -> the continuation field starts with a space; merge it
         back into the previous field;
      2. an R-/S-phrase list ("R25,R33,...") -> rebuild risk_phrases /
         safety_phrases from the fields between hazard_symbol and the
         entry_complete anchor.
  Rows damaged in other ways cannot be realigned deterministically. They are
  detected by ``misalignment`` (anchor columns that no longer look like
  themselves), loaded anyway with a marker in cargo_chemical.notes, and counted
  in the summary. Pass --strict to skip them instead. The real fix is to
  re-export the CSV with proper quoting.

field_definitions rows for the new property keys are seeded here (ON CONFLICT DO
NOTHING). SITTIG_FIELDS is also spliced into field_definition.py's FIELDS so a
later run of that script does not prune them (which would CASCADE-delete every
value loaded here).

Idempotent: cargo_chemical upserts on (source_id, canonical_name),
cargo_property_values on (cargo_id, source_id, field_name), cargo_synonym on
(cargo_id, synonym_id). Re-running updates in place.

Usage:
    python3 etl/sittig_handbook.py
    python3 etl/sittig_handbook.py --dry-run
    python3 etl/sittig_handbook.py --strict          # skip misaligned rows
    python3 etl/sittig_handbook.py --file "/path/Sittigs Handbook....csv"

Reads DATABASE_URL from the repo-root .env.
"""

import argparse
import csv
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

from _paths import input_file
from cargo_chemicals import get_source_id, get_source_id_partial, create_source

DEFAULT_FILE = str(input_file("Sittigs Handbook of Toxic & Hazardous Chemicals.csv"))
SOURCE_NAME = "Sittigs Handbook of Toxic & Hazardous Chemicals"

NAME_COLUMN = "chemical_name"      # -> cargo_chemical.canonical_name
SYNONYM_COLUMN = "synonyms"        # -> synonyms / cargo_synonym
PAGE_COLUMN = "page_number"        # -> cargo_property_values.source_page_ref

RELATIONSHIP_TYPE = "common"
PREFERRED_FOR_SEARCH = False
LANGUAGE = "en"
ENTERED_BY = "sittig_handbook.py"
ENTRY_TYPE = "import"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("sittig_handbook")


# ---------------------------------------------------------------------------
# Column -> field_definitions.field_name
# ---------------------------------------------------------------------------
# chemical_name and synonyms are handled by their own tables and are NOT
# property values; every other column below becomes one cargo_property_values
# row per chemical. Values on the right that differ from the CSV column name are
# deliberate aliases onto field names other sources already write, so the same
# property is comparable across sources.
COLUMN_TO_FIELD: Dict[str, str] = {
    "common_formula":                 "common_formula",
    "molecular_formula":              "molecular_formula",
    "cas_registry_number":            "cas_number",                # alias
    "rtecs_number":                   "rtecs_number",
    "un_na_number":                   "un_na_number",
    "ec_number":                      "ec_number",
    "regulatory_authority":           "regulatory_authority",
    "carcinogenicity_status":         "carcinogenicity_status",
    "rcra_waste_numbers":             "rcra_waste_numbers",
    "epcra_status":                   "epcra_status",
    "clean_air_act":                  "clean_air_act",
    "clean_water_act":                "clean_water_act",
    "safe_drinking_water_act":        "safe_drinking_water_act",
    "international_regulations":      "international_regulations",
    "molecular_weight":               "molecular_weight_g_mol",    # alias
    "density":                        "density",                   # alias
    "boiling_point":                  "boiling_point",             # alias
    "melting_point":                  "melting_point",             # alias
    "vapor_pressure":                 "vapour_pressure",           # alias
    "flash_point":                    "flash_point",               # alias
    "water_solubility":               "water_solubility",          # alias
    "octanol_water_coefficient":      "octanol_water_coefficient",
    "nfpa_ratings":                   "nfpa_ratings",
    "description":                    "product_description",       # alias
    "potential_exposure":             "potential_exposure",
    "incompatibilities":              "incompatibilities",
    "osha_pel":                       "osha_pel",
    "niosh_rel":                      "niosh_rel",
    "acgih_tlv":                      "acgih_tlv",
    "niosh_idlh":                     "niosh_idlh",
    "teel":                           "teel",
    "pac1":                           "pac1",
    "pac2":                           "pac2",
    "pac3":                           "pac3",
    "dfg_mak":                        "dfg_mak",
    "other_national_oels":            "other_national_oels",
    "determination_in_air":           "determination_in_air",
    "freshwater_aquatic_life_acute":  "freshwater_aquatic_life_acute",
    "freshwater_aquatic_life_chronic": "freshwater_aquatic_life_chronic",
    "saltwater_aquatic_life_acute":   "saltwater_aquatic_life_acute",
    "saltwater_aquatic_life_chronic": "saltwater_aquatic_life_chronic",
    "human_health_water":             "human_health_water",
    "mexico_drinking_water":          "mexico_drinking_water",
    "rcra_wastewater_standard":       "rcra_wastewater_standard",
    "rcra_nonwastewater_standard":    "rcra_nonwastewater_standard",
    "determination_in_water":         "determination_in_water",
    "iarc_classification":            "carcinogen_iarc",           # alias
    "ld50":                           "ld50",
    "routes_of_entry":                "routes_of_entry",
    "target_organs":                  "target_organs",
    "short_term_effects":             "short_term_effects",
    "long_term_effects":              "long_term_effects",
    "points_of_attack":               "points_of_attack",
    "mutagenicity":                   "mutagenicity",
    "medical_surveillance":           "medical_surveillance",
    "first_aid":                      "first_aid",
    "personal_protective_methods":    "personal_protective_methods",
    "respirator_selection":           "respirator_selection",
    "storage":                        "storage",
    "shipping":                       "shipping",
    "hazard_class":                   "hazard_class",
    "packing_group":                  "packing_group",
    "hazard_symbol":                  "hazard_symbol",
    "risk_phrases":                   "risk_phrases",
    "safety_phrases":                 "safety_phrases",
    "spill_handling":                 "spill_handling",
    "fire_extinguishing":             "fire_extinguishing",
    "disposal_method":                "disposal_method",
    "page_number":                    "page_number",
    "extraction_notes":               "extraction_notes",
    "entry_complete":                 "entry_complete",
}

# field_definitions catalog for the keys above. `existing: True` marks a field
# another loader already seeds (field_definition.py FIELDS) — kept here only so
# data_type/unit are known locally; those rows are never re-created.
#   field_name / display_name / data_type / unit / category / catalog_only
_F = lambda name, disp, dtype, unit, cat, existing=False: {          # noqa: E731
    "field_name": name, "display_name": disp, "data_type": dtype,
    "unit": unit, "category": cat, "catalog_only": True, "existing": existing,
}

SITTIG_FIELDS: List[dict] = [
    # ---- Identity ---------------------------------------------------------
    _F("common_formula",            "Common Formula",             "text",   None, "Identity"),
    _F("molecular_formula",         "Molecular Formula",          "text",   None, "Identity"),
    _F("cas_number",                "CAS Number",                 "text",   None, "Identity", True),
    _F("rtecs_number",              "RTECS Number",               "text",   None, "Identity"),
    _F("un_na_number",              "UN/NA Number",               "text",   None, "Identity"),
    _F("ec_number",                 "EC Number",                  "text",   None, "Identity"),
    _F("product_description",       "Product Description",        "text",   None, "Identity"),
    _F("page_number",               "Source Page Number",         "text",   None, "Identity"),
    _F("extraction_notes",          "Extraction Notes",           "text",   None, "Identity"),
    _F("entry_complete",            "Entry Complete",             "boolean", None, "Identity"),
    # ---- Regulatory -------------------------------------------------------
    _F("regulatory_authority",      "Regulatory Authority",       "text",   None, "Regulatory"),
    _F("carcinogenicity_status",    "Carcinogenicity Status",     "text",   None, "Regulatory"),
    _F("rcra_waste_numbers",        "RCRA Waste Numbers",         "text",   None, "Regulatory"),
    _F("epcra_status",              "EPCRA Status",               "text",   None, "Regulatory"),
    _F("clean_air_act",             "Clean Air Act",              "text",   None, "Regulatory"),
    _F("clean_water_act",           "Clean Water Act",            "text",   None, "Regulatory"),
    _F("safe_drinking_water_act",   "Safe Drinking Water Act",    "text",   None, "Regulatory"),
    _F("international_regulations", "International Regulations",  "text",   None, "Regulatory"),
    _F("hazard_class",              "Hazard Class",               "text",   None, "Regulatory"),
    _F("packing_group",             "Packing Group",              "text",   None, "Regulatory"),
    _F("hazard_symbol",             "Hazard Symbol",              "text",   None, "Regulatory"),
    _F("risk_phrases",              "Risk (R) Phrases",           "text",   None, "Regulatory"),
    _F("safety_phrases",            "Safety (S) Phrases",         "text",   None, "Regulatory"),
    _F("shipping",                  "Shipping Requirements",      "text",   None, "Regulatory"),
    # ---- Physical ---------------------------------------------------------
    _F("molecular_weight_g_mol",    "Molecular Weight",           "number", "g/mol", "Physical", True),
    _F("density",                   "Density",                    "number", "kg/l",  "Physical", True),
    _F("boiling_point",             "Boiling Point",              "number", "°C",    "Physical", True),
    _F("melting_point",             "Melting Point",              "number", "°C",    "Physical", True),
    _F("vapour_pressure",           "Vapour Pressure",            "number", "bar",   "Physical", True),
    _F("flash_point",               "Flash Point",                "number", "°C",    "Physical", True),
    _F("water_solubility",          "Water Solubility",           "text",   None,    "Physical", True),
    _F("octanol_water_coefficient", "Octanol/Water Coefficient",  "text",   None,    "Physical"),
    _F("nfpa_ratings",              "NFPA Ratings",               "text",   None,    "Physical"),
    _F("incompatibilities",         "Incompatibilities",          "text",   None,    "Physical"),
    # ---- Health / exposure limits ----------------------------------------
    _F("potential_exposure",        "Potential Exposure",         "text",   None, "Health"),
    _F("osha_pel",                  "OSHA PEL",                   "text",   None, "Health"),
    _F("niosh_rel",                 "NIOSH REL",                  "text",   None, "Health"),
    _F("acgih_tlv",                 "ACGIH TLV",                  "text",   None, "Health"),
    _F("niosh_idlh",                "NIOSH IDLH",                 "text",   None, "Health"),
    _F("teel",                      "TEEL",                       "text",   None, "Health"),
    _F("pac1",                      "PAC-1",                      "text",   None, "Health"),
    _F("pac2",                      "PAC-2",                      "text",   None, "Health"),
    _F("pac3",                      "PAC-3",                      "text",   None, "Health"),
    _F("dfg_mak",                   "DFG MAK",                    "text",   None, "Health"),
    _F("other_national_oels",       "Other National OELs",        "text",   None, "Health"),
    _F("determination_in_air",      "Determination in Air",       "text",   None, "Health"),
    _F("carcinogen_iarc",           "IARC Carcinogen Class",      "text",   None, "Health", True),
    _F("ld50",                      "LD50",                       "text",   None, "Health"),
    _F("routes_of_entry",           "Routes of Entry",            "text",   None, "Health"),
    _F("target_organs",             "Target Organs",              "text",   None, "Health"),
    _F("short_term_effects",        "Short-Term Effects",         "text",   None, "Health"),
    _F("long_term_effects",         "Long-Term Effects",          "text",   None, "Health"),
    _F("points_of_attack",          "Points of Attack",           "text",   None, "Health"),
    _F("mutagenicity",              "Mutagenicity",               "text",   None, "Health"),
    _F("medical_surveillance",      "Medical Surveillance",       "text",   None, "Health"),
    _F("first_aid",                 "First Aid",                  "text",   None, "Health"),
    _F("personal_protective_methods", "Personal Protective Methods", "text", None, "Health"),
    _F("respirator_selection",      "Respirator Selection",       "text",   None, "Health"),
    # ---- Environmental water-quality criteria ----------------------------
    _F("freshwater_aquatic_life_acute",   "Freshwater Aquatic Life (Acute)",   "text", None, "Health"),
    _F("freshwater_aquatic_life_chronic", "Freshwater Aquatic Life (Chronic)", "text", None, "Health"),
    _F("saltwater_aquatic_life_acute",    "Saltwater Aquatic Life (Acute)",    "text", None, "Health"),
    _F("saltwater_aquatic_life_chronic",  "Saltwater Aquatic Life (Chronic)",  "text", None, "Health"),
    _F("human_health_water",              "Human Health (Water)",              "text", None, "Health"),
    _F("mexico_drinking_water",           "Mexico Drinking Water",             "text", None, "Health"),
    _F("rcra_wastewater_standard",        "RCRA Wastewater Standard",          "text", None, "Regulatory"),
    _F("rcra_nonwastewater_standard",     "RCRA Non-Wastewater Standard",      "text", None, "Regulatory"),
    _F("determination_in_water",          "Determination in Water",            "text", None, "Health"),
    # ---- Carriage / handling ---------------------------------------------
    _F("storage",                   "Storage Requirements",       "text",   None, "Carriage"),
    _F("spill_handling",            "Spill Handling",             "text",   None, "Cleaning"),
    _F("fire_extinguishing",        "Fire Extinguishing",         "text",   None, "Cleaning"),
    _F("disposal_method",           "Disposal Method",            "text",   None, "Cleaning"),
]

# Only the fields this loader introduces; field_definition.py splices these into
# its FIELDS so its prune pass does not delete them.
NEW_FIELDS: List[dict] = [f for f in SITTIG_FIELDS if not f["existing"]]

FIELD_META: Dict[str, Tuple[str, Optional[str]]] = {
    f["field_name"]: (f["data_type"], f["unit"]) for f in SITTIG_FIELDS
}


# ---------------------------------------------------------------------------
# CSV repair (see the ⚠ note in the module docstring)
# ---------------------------------------------------------------------------
BOOLS = {"TRUE", "FALSE"}
# Anchor columns used both by repair_row and by misalignment().
I_RISK, I_SAFETY, I_SPILL, I_ENTRY = 65, 66, 67, 72
N_COLS = 73


def repair_row(row: List[str]) -> List[str]:
    """Undo the recoverable unquoted-comma splits; return exactly N_COLS fields."""
    # 1. ", " joins: a continuation field starts with a space and has content.
    fields: List[str] = []
    for cell in row:
        if fields and cell[:1] == " " and cell.strip():
            fields[-1] += "," + cell
        else:
            fields.append(cell)

    while fields and fields[-1].strip() == "":     # drop the writer's padding
        fields.pop()
    if len(fields) <= N_COLS:
        return fields + [""] * (N_COLS - len(fields))

    # 2. Still too wide: the surplus is an R-/S-phrase list written without
    #    spaces. entry_complete (TRUE/FALSE) anchors the tail, so everything
    #    between hazard_symbol and spill_handling is the two phrase columns.
    anchor = max((i for i, v in enumerate(fields) if v.strip().upper() in BOOLS),
                 default=-1)
    tail_len = I_ENTRY - I_SPILL + 1
    if anchor < I_ENTRY or anchor - tail_len < I_RISK:
        return fields[:N_COLS]                      # unrecoverable: truncate
    head = fields[:I_RISK]
    tail = fields[anchor - tail_len + 1:anchor + 1]
    middle = [f for f in fields[I_RISK:anchor - tail_len + 1] if f.strip()]
    risk = [f for f in middle if f.strip().upper().startswith("R")]
    safety = [f for f in middle if f.strip().upper().startswith("S")]
    other = [f for f in middle if f not in risk and f not in safety]
    return head + [",".join(risk + other), ",".join(safety)] + tail


# Anchor columns whose shape is known; a value that no longer fits means the row
# is still shifted and every column after that point is untrustworthy.
_HAZARD_CLASS_RE = re.compile(r"^\d")                       # 3, 6.1, "8 (solution)"
_PACKING_GROUP_RE = re.compile(r"^(I{1,3}|\d)\b", re.I)     # I / II / III / "II or III"
_PAGE_RE = re.compile(r"\d")


def misalignment(rec: Dict[str, str]) -> Optional[str]:
    """Return a reason string if `rec` still looks column-shifted, else None."""
    checks = (
        ("hazard_class",  _HAZARD_CLASS_RE),
        ("packing_group", _PACKING_GROUP_RE),
        ("page_number",   _PAGE_RE),
    )
    for col, pattern in checks:
        val = rec.get(col, "").strip()
        if val and not pattern.search(val):
            return f"{col}={val[:60]!r}"
    if rec.get("entry_complete", "").strip().upper() not in BOOLS | {""}:
        return f"entry_complete={rec['entry_complete'][:60]!r}"
    return None


def read_rows(path: Path) -> Tuple[List[str], List[Dict[str, str]]]:
    """Parse + repair the CSV into (header, [{column: value}, ...])."""
    with path.open(newline="", encoding="utf-8") as fh:
        raw = list(csv.reader(fh))
    if not raw:
        sys.exit(f"Error: {path} is empty.")
    header = [c.strip() for c in raw[0][:N_COLS]]
    if header[0] != NAME_COLUMN:
        sys.exit(f"Error: expected first column {NAME_COLUMN!r}, got {header[0]!r}.")
    missing = [c for c in COLUMN_TO_FIELD if c not in header]
    if missing:
        sys.exit(f"Error: columns missing from the file: {missing}")
    records = [dict(zip(header, repair_row(r))) for r in raw[1:]]
    log.info("Read %d rows x %d columns from %s", len(records), len(header), path.name)
    return header, records


# ---------------------------------------------------------------------------
# Value coercion
# ---------------------------------------------------------------------------
# A cell counts as numeric only when it is a bare number, optionally with a °C
# suffix and/or a trailing parenthetical qualifier ("1.87 (H2O=1)"). Anything
# with an embedded unit ("244 mmHg at 20°C") stays text so no wrong number is
# recorded against the field's catalog unit.
_NUM_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*(°?\s*[CF])?\s*(\([^)]*\))?\s*$", re.I)


def parse_number(raw: str, unit: Optional[str]) -> Optional[float]:
    m = _NUM_RE.match(raw)
    if not m:
        return None
    suffix = (m.group(2) or "").replace("°", "").replace(" ", "").upper()
    if suffix and unit != "°C":      # a temperature reading on a non-temp field
        return None
    if suffix == "F":                # °F would need converting; leave it as text
        return None
    return float(m.group(1))


def parse_bool(raw: str) -> Optional[bool]:
    v = raw.strip().upper()
    return True if v == "TRUE" else False if v == "FALSE" else None


def normalize_synonym(text: str) -> str:
    """Lowercase, punctuation -> space, collapse whitespace.

    Must stay identical to import_synonyms.normalize / master_loader's version:
    the synonyms table is looked up by this key, so a divergence would create
    duplicate synonym rows instead of reusing existing ones.
    """
    s = text.lower()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def split_synonyms(cell: str) -> List[str]:
    """Split a synonyms cell on ';' and newlines only.

    Commas are NOT separators here — several entries are single names that
    contain one ("Helium, elemental", "Ethane, 2-bromo-2-chloro-...").
    """
    parts = re.split(r"[;\n]+", cell)
    return [p.strip() for p in parts if p.strip()]


class SynonymCache:
    """synonyms.normalized_text -> id; insert on miss and remember."""

    def __init__(self, cur, dry_run: bool):
        self.cur = cur
        self.dry_run = dry_run
        cur.execute("SELECT normalized_text, id FROM synonyms")
        self.by_norm: Dict[str, int] = {r[0]: r[1] for r in cur.fetchall()}
        self.reused = 0
        self.created = 0
        self._fake = 0
        log.info("Loaded %d existing synonyms into cache", len(self.by_norm))

    def ensure(self, text: str) -> Optional[int]:
        norm = normalize_synonym(text)
        if not norm:
            return None
        if norm in self.by_norm:
            self.reused += 1
            return self.by_norm[norm]
        self.created += 1
        if self.dry_run:
            self._fake -= 1
            self.by_norm[norm] = self._fake
            return self._fake
        self.cur.execute(
            "INSERT INTO synonyms (synonym_text, normalized_text, language) "
            "VALUES (%s, %s, %s) RETURNING id",
            (text.strip(), norm, LANGUAGE),
        )
        sid = self.cur.fetchone()[0]
        self.by_norm[norm] = sid
        return sid


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------
def seed_field_definitions(cur, dry_run: bool) -> int:
    """Insert the property keys this loader writes; existing rows are untouched."""
    if dry_run:
        return len(NEW_FIELDS)
    cur.execute("SELECT coalesce(max(display_order), 0) FROM field_definitions")
    order = cur.fetchone()[0] or 0
    created = 0
    for f in NEW_FIELDS:
        order += 10
        cur.execute(
            """
            INSERT INTO field_definitions
                (field_name, display_name, data_type, unit, category, typical_source,
                 display_order, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, now(), now())
            ON CONFLICT (field_name) DO NOTHING
            """,
            (f["field_name"], f["display_name"], f["data_type"], f["unit"],
             f["category"], SOURCE_NAME, order),
        )
        created += cur.rowcount
    return created


def resolve_source(cur, forced: Optional[int], dry_run: bool) -> int:
    if forced is not None:
        cur.execute("SELECT id FROM source WHERE id=%s", (forced,))
        if cur.fetchone() is None:
            sys.exit(f"Error: --source-id {forced} not found.")
        return forced
    sid = get_source_id(cur, SOURCE_NAME) or get_source_id_partial(cur, SOURCE_NAME)
    if sid is None:
        if dry_run:
            return -1
        sid = create_source(cur, SOURCE_NAME)
        log.info("Created source '%s' id=%s", SOURCE_NAME, sid)
    return sid


def upsert_cargo(cur, name: str, source_id: int, note: Optional[str],
                 dry_run: bool, fake_id: int) -> int:
    if dry_run:
        return fake_id
    cur.execute(
        """
        INSERT INTO cargo_chemical (canonical_name, source_id, notes, created_at, updated_at)
        VALUES (%s, %s, %s, now(), now())
        ON CONFLICT (source_id, canonical_name) DO UPDATE SET
            notes = EXCLUDED.notes,
            updated_at = now()
        RETURNING id
        """,
        (name, source_id, note),
    )
    return cur.fetchone()[0]


def build_property_rows(rec: Dict[str, str], cargo_id: int, source_id: int,
                        now: datetime) -> List[tuple]:
    """One cargo_property_values row per non-empty mapped column."""
    page_ref = rec.get(PAGE_COLUMN, "").strip() or None
    rows = []
    for column, field_name in COLUMN_TO_FIELD.items():
        raw = rec.get(column, "").strip()
        if not raw:
            continue
        data_type, unit = FIELD_META[field_name]
        normalized = value_unit = None
        if data_type == "number":
            normalized = parse_number(raw, unit)
            value_unit = unit if normalized is not None else None
            value_type = "number" if normalized is not None else "text"
        elif data_type == "boolean":
            value_type = "boolean" if parse_bool(raw) is not None else "text"
        else:
            value_type = "text"
        rows.append((cargo_id, source_id, field_name, raw, normalized, value_unit,
                     value_type, None, page_ref, None, now, ENTERED_BY, ENTRY_TYPE,
                     True, False, None))
    return rows


def flush_properties(cur, rows: List[tuple]) -> None:
    execute_values(
        cur,
        """
        INSERT INTO cargo_property_values
            (cargo_id, source_id, field_name, value, normalized_value, unit,
             value_type, source_synonym_id, source_page_ref, as_of_date,
             entered_date, entered_by, entry_type, is_winning, conflict_flag,
             notes, created_at, updated_at)
        VALUES %s
        ON CONFLICT (cargo_id, source_id, field_name) DO UPDATE SET
            value            = EXCLUDED.value,
            normalized_value = EXCLUDED.normalized_value,
            unit             = EXCLUDED.unit,
            value_type       = EXCLUDED.value_type,
            source_page_ref  = EXCLUDED.source_page_ref,
            entered_date     = EXCLUDED.entered_date,
            entered_by       = EXCLUDED.entered_by,
            entry_type       = EXCLUDED.entry_type,
            is_winning       = EXCLUDED.is_winning,
            conflict_flag    = EXCLUDED.conflict_flag,
            updated_at       = now()
        """,
        rows,
        template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),now())",
        page_size=1000,
    )


def flush_links(cur, rows: List[tuple]) -> None:
    execute_values(
        cur,
        """
        INSERT INTO cargo_synonym
            (cargo_id, synonym_id, relationship_type, ambiguity_flag, source_id,
             preferred_for_search, notes, created_at, updated_at)
        VALUES %s
        ON CONFLICT (cargo_id, synonym_id) DO UPDATE SET
            relationship_type = EXCLUDED.relationship_type,
            ambiguity_flag    = EXCLUDED.ambiguity_flag,
            source_id         = EXCLUDED.source_id,
            updated_at        = now()
        """,
        rows,
        template="(%s,%s,%s,%s,%s,%s,%s,now(),now())",
        page_size=1000,
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Import Sittig's Handbook CSV.")
    ap.add_argument("file", nargs="?", default=DEFAULT_FILE, help="the CSV path")
    ap.add_argument("--source-id", type=int, default=None, help="force a source id")
    ap.add_argument("--strict", action="store_true",
                    help="skip rows that still look column-shifted after repair")
    ap.add_argument("--dry-run", action="store_true", help="parse + report, write nothing")
    args = ap.parse_args()

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        sys.exit("Error: DATABASE_URL not set (checked .env).")

    path = Path(args.file)
    if not path.is_file():
        sys.exit(f"File not found: {path}")

    _, records = read_rows(path)

    # First pass: a synonym shared by more than one chemical is ambiguous.
    chemicals_per_syn: Dict[str, set] = {}
    for rec in records:
        name = rec[NAME_COLUMN].strip()
        if not name:
            continue
        for syn in split_synonyms(rec.get(SYNONYM_COLUMN, "")):
            chemicals_per_syn.setdefault(normalize_synonym(syn), set()).add(name.lower())

    conn = psycopg2.connect(db_url)
    try:
        with conn.cursor() as cur:
            source_id = resolve_source(cur, args.source_id, args.dry_run)
            log.info("source_id=%s (%s)", source_id, SOURCE_NAME)

            n_fields = seed_field_definitions(cur, args.dry_run)
            log.info("field_definitions: %d new key(s) seeded", n_fields)

            syn_cache = SynonymCache(cur, args.dry_run)

            prop_rows: Dict[Tuple[int, str], tuple] = {}
            link_rows: Dict[Tuple[int, int], tuple] = {}
            now = datetime.now()
            n_cargo = n_skip_noname = n_skip_shift = n_shifted = 0
            seen_names: Dict[str, int] = {}

            for idx, rec in enumerate(records):
                line = idx + 2                       # header + 1-based
                name = rec[NAME_COLUMN].strip()
                if not name:
                    n_skip_noname += 1
                    log.warning("✗ row %d SKIP (no %s)", line, NAME_COLUMN)
                    continue

                reason = misalignment(rec)
                if reason:
                    n_shifted += 1
                    if args.strict:
                        n_skip_shift += 1
                        log.warning("✗ row %d SKIP (column-shifted): %s | %s",
                                    line, name, reason)
                        continue
                    log.warning("! row %d column-shifted, loading anyway: %s | %s",
                                line, name, reason)
                note = (f"Source CSV row {line} is column-shifted (unquoted commas); "
                        f"anchor check failed: {reason}") if reason else None

                key = name.lower()
                if key in seen_names:                # duplicate name in the file
                    cargo_id = seen_names[key]
                    log.info("• row %d duplicate name %r -> cargo_id=%s", line, name, cargo_id)
                else:
                    cargo_id = upsert_cargo(cur, name, source_id, note,
                                            args.dry_run, -(idx + 1))
                    seen_names[key] = cargo_id
                    n_cargo += 1

                for row in build_property_rows(rec, cargo_id, source_id, now):
                    prop_rows[(row[0], row[2])] = row   # last write wins on dupes

                for syn in split_synonyms(rec.get(SYNONYM_COLUMN, "")):
                    synonym_id = syn_cache.ensure(syn)
                    if synonym_id is None:
                        continue
                    ambiguous = len(chemicals_per_syn.get(normalize_synonym(syn), ())) > 1
                    link_rows[(cargo_id, synonym_id)] = (
                        cargo_id, synonym_id, RELATIONSHIP_TYPE, ambiguous,
                        source_id, PREFERRED_FOR_SEARCH, None,
                    )

            if not args.dry_run:
                if prop_rows:
                    flush_properties(cur, list(prop_rows.values()))
                if link_rows:
                    flush_links(cur, list(link_rows.values()))

            log.info("=" * 64)
            log.info("SUMMARY (%s)", "DRY-RUN" if args.dry_run else "COMMIT")
            log.info("  cargo_chemical rows        : %d", n_cargo)
            log.info("  cargo_property_values rows : %d", len(prop_rows))
            log.info("  synonyms reused / created  : %d / %d",
                     syn_cache.reused, syn_cache.created)
            log.info("  cargo_synonym links        : %d", len(link_rows))
            log.info("  field_definitions seeded   : %d", n_fields)
            log.info("  rows w/ no chemical_name   : %d", n_skip_noname)
            log.info("  column-shifted rows        : %d (%s)", n_shifted,
                     f"{n_skip_shift} skipped" if args.strict else "loaded, flagged in notes")
            log.info("=" * 64)

            if args.dry_run:
                conn.rollback()
                log.info("Dry run: nothing written.")
                return
            conn.commit()
            log.info("✓ Committed.")
    except Exception:
        conn.rollback()
        log.exception("Import failed - rolled back")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
