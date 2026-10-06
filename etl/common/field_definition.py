#!/usr/bin/env python3
"""
Seed the `field_definitions` catalog and keep cargo_chemical columns in sync.

field_definitions is the catalog of every property a cargo can have. For each
hard-coded field below this script:
  1. Inserts the definition into field_definitions (skipped if field_name
     already exists -- ON CONFLICT DO NOTHING).
  2. Checks whether a matching column exists in cargo_chemical:
       * exists  -> skip (✓ already there)
       * missing -> create it with ALTER TABLE ... ADD COLUMN (✚ created)

FIELDS is the source of truth: if you DELETE a field from the list, its
field_definitions row is removed AND its cargo_chemical column is dropped
(DROP COLUMN -- the data in that column is lost). Structural columns in
PROTECTED_COLUMNS are never dropped.

Usage:
    python3 field_definition.py            # apply
    python3 field_definition.py --dry-run  # show what would happen, no writes

Reads DATABASE_URL from the .env file in this directory.
"""

import argparse
import logging
import os
import sys
from pathlib import Path

import psycopg2
from psycopg2 import sql
from psycopg2.extras import execute_values
from dotenv import load_dotenv

CARGO_TABLE = "cargo_chemical"

# Allowed SQL types for ADD COLUMN (whitelist -> safe to inject as identifiers).
SQL_TYPES = {"text", "integer", "boolean", "timestamp"}

# Structural columns that must NEVER be dropped, even if absent from FIELDS.
# lel/uel/appearance/odour are Prisma-owned cargo_chemical columns that also
# appear as catalog vocabulary below; protect them so a future FIELDS edit can
# never DROP the real column out from under Prisma.
PROTECTED_COLUMNS = {
    "id", "canonical_name", "canonical_name_source_id",
    "notes", "created_at", "updated_at",
    "lel", "uel", "appearance", "odour",
}

# ----------------------------------------------------------------------------
# Hard-coded field catalog. One dict per cargo_chemical property.
#   field_name   : snake_case identifier (PK in field_definitions, column name)
#   display_name : human label
#   data_type    : number | text | enum | boolean | date | json
#   unit         : unit of measure or None
#   category     : Identity | Regulatory | Physical | Health | Carriage | Cleaning
#   sql_type     : Postgres type used if the cargo_chemical column must be created
#   catalog_only : if True, seed the field_definitions row ONLY -- do NOT create
#                  a cargo_chemical column. Used for per-source physical
#                  properties that live in cargo_property_values (keyed by
#                  field_name), not as canonical columns. sql_type is ignored.
# ----------------------------------------------------------------------------
FIELDS = [
    {"field_name": "cas_number",               "display_name": "CAS Number",              "data_type": "text",    "unit": None,      "category": "Identity",   "sql_type": "text"},
    {"field_name": "chris_code",               "display_name": "CHRIS Code",              "data_type": "text",    "unit": None,      "category": "Regulatory", "sql_type": "text"},
    {"field_name": "ibc_chapter",              "display_name": "IBC Chapter",             "data_type": "text",    "unit": None,      "category": "Regulatory", "sql_type": "text"},
    {"field_name": "ibc_product_name",         "display_name": "IBC Product Name",        "data_type": "text",    "unit": None,      "category": "Regulatory", "sql_type": "text"},
    {"field_name": "ibc_pollution_category",   "display_name": "IBC Pollution Category",  "data_type": "enum",    "unit": None,      "category": "Regulatory", "sql_type": "text"},
    {"field_name": "marpol_category",          "display_name": "MARPOL Category",         "data_type": "text",    "unit": None,      "category": "Regulatory", "sql_type": "text"},
    {"field_name": "imdg_class",               "display_name": "IMDG Class",              "data_type": "text",    "unit": None,      "category": "Regulatory", "sql_type": "text"},
    {"field_name": "dot_hazmat_id",            "display_name": "DOT Hazmat ID",           "data_type": "text",    "unit": None,      "category": "Regulatory", "sql_type": "text"},
    {"field_name": "physical_state_20c",       "display_name": "Physical State (20°C)",   "data_type": "enum",    "unit": None,      "category": "Physical",   "sql_type": "text"},
    {"field_name": "molecular_weight_g_mol",   "display_name": "Molecular Weight",        "data_type": "number",  "unit": "g/mol",   "category": "Physical",   "sql_type": "integer"},
    {"field_name": "boiling_point_c",          "display_name": "Boiling Point",           "data_type": "number",  "unit": "°C",      "category": "Physical",   "sql_type": "integer"},
    {"field_name": "melting_point_c",          "display_name": "Melting Point",           "data_type": "number",  "unit": "°C",      "category": "Physical",   "sql_type": "integer"},
    {"field_name": "density_g_cm3",            "display_name": "Density",                 "data_type": "number",  "unit": "g/cm³",   "category": "Physical",   "sql_type": "integer"},
    {"field_name": "vapor_pressure_kpa_20c",   "display_name": "Vapor Pressure (20°C)",   "data_type": "number",  "unit": "kPa",     "category": "Physical",   "sql_type": "integer"},
    {"field_name": "viscosity_cp_20c",         "display_name": "Viscosity (20°C)",        "data_type": "number",  "unit": "cP",      "category": "Physical",   "sql_type": "integer"},
    {"field_name": "flash_point_c",            "display_name": "Flash Point",             "data_type": "number",  "unit": "°C",      "category": "Physical",   "sql_type": "integer"},
    {"field_name": "autoignition_temp_c",      "display_name": "Autoignition Temperature","data_type": "number",  "unit": "°C",      "category": "Physical",   "sql_type": "integer"},
    {"field_name": "water_solubility",         "display_name": "Water Solubility",        "data_type": "text",    "unit": None,      "category": "Physical",   "sql_type": "text"},
    {"field_name": "water_reactive",           "display_name": "Water Reactive",          "data_type": "boolean", "unit": None,      "category": "Health",     "sql_type": "boolean"},
    {"field_name": "ghs_pictograms",           "display_name": "GHS Pictograms",          "data_type": "text",    "unit": None,      "category": "Health",     "sql_type": "text"},
    {"field_name": "ghs_signal_word",          "display_name": "GHS Signal Word",         "data_type": "text",    "unit": None,      "category": "Health",     "sql_type": "text"},
    {"field_name": "h_statements",             "display_name": "Hazard Statements",       "data_type": "text",    "unit": None,      "category": "Health",     "sql_type": "text"},
    {"field_name": "carcinogen_iarc",          "display_name": "IARC Carcinogen Class",   "data_type": "text",    "unit": None,      "category": "Health",     "sql_type": "text"},
    {"field_name": "tlv_twa_ppm",              "display_name": "TLV-TWA",                 "data_type": "number",  "unit": "ppm",     "category": "Health",     "sql_type": "integer"},
    {"field_name": "idlh_ppm",                 "display_name": "IDLH",                    "data_type": "number",  "unit": "ppm",     "category": "Health",     "sql_type": "integer"},
    {"field_name": "inert_gas_required",       "display_name": "Inert Gas Required",      "data_type": "boolean", "unit": None,      "category": "Carriage",   "sql_type": "boolean"},
    {"field_name": "heating_temp_min_c",       "display_name": "Heating Temp Min",        "data_type": "number",  "unit": "°C",      "category": "Carriage",   "sql_type": "integer"},
    {"field_name": "heating_temp_max_c",       "display_name": "Heating Temp Max",        "data_type": "number",  "unit": "°C",      "category": "Carriage",   "sql_type": "integer"},
    {"field_name": "permitted_tank_materials", "display_name": "Permitted Tank Materials","data_type": "text",    "unit": None,      "category": "Carriage",   "sql_type": "text"},
    {"field_name": "permitted_coatings",       "display_name": "Permitted Coatings",      "data_type": "text",    "unit": None,      "category": "Carriage",   "sql_type": "text"},
    {"field_name": "stowage_notes",            "display_name": "Stowage Notes",           "data_type": "text",    "unit": None,      "category": "Carriage",   "sql_type": "text"},
    {"field_name": "data_completeness_score",  "display_name": "Data Completeness Score", "data_type": "number",  "unit": None,      "category": "Identity",   "sql_type": "integer"},
    {"field_name": "date_added",               "display_name": "Date Added",              "data_type": "date",    "unit": None,      "category": "Identity",   "sql_type": "timestamp"},
    {"field_name": "date_last_updated",        "display_name": "Date Last Updated",       "data_type": "date",    "unit": None,      "category": "Identity",   "sql_type": "timestamp"},
    {"field_name": "date_example",             "display_name": "Date Example",            "data_type": "date",    "unit": None,      "category": "Identity",   "sql_type": "timestamp"},

    # ---- IBC Code chapter 17 carriage requirements -------------------------
    # Twelve of these thirteen ALREADY EXIST as cargo_chemical columns, owned by
    # Prisma and filled for three sources (IBC Code, Miracle, LARS). They are
    # listed here as catalog_only so that:
    #
    #   * the catalog row exists - cargo_property_values.field_name FKs
    #     field_definitions, so a value cannot be written without it;
    #   * this script never DROPs the columns. `stale = catalogued - desired`
    #     above, so a name absent from FIELDS is deleted from the catalog and
    #     CASCADEs away every cargo_property_values row using it. Removing any
    #     line below silently destroys the 9,315 values that
    #     etl/chemical/ibc_carriage_to_property_values.py moved out of the wide
    #     columns.
    #
    # catalog_only, not a managed column: the columns already exist and belong
    # to Prisma, and this script must not try to create or own them.
    #
    # Names are deliberately NOT prefixed `ibc_`. Miracle and LARS fill the same
    # columns, so a second chemical source's answer to the same regulatory
    # question has to land in the same field for the comparison to be possible;
    # which source said it is carried by source_id on each value.
    {"field_name": "hazards",                                "display_name": "Hazards",                        "data_type": "text",    "unit": None,      "category": "Regulatory", "catalog_only": True},
    {"field_name": "ship_type",                              "display_name": "Ship Type",                      "data_type": "text",    "unit": None,      "category": "Regulatory", "catalog_only": True},
    {"field_name": "tank_type",                              "display_name": "Tank Type",                      "data_type": "text",    "unit": None,      "category": "Regulatory", "catalog_only": True},
    # CHEM products sheet (source 16): readings at another temperature, pointers to an
    # outside correction table, and density cells kept exactly as printed.
    {"field_name": "density_20c", "display_name": "Density at 20°C", "data_type": "number", "unit": "kg/l", "category": "Physical", "catalog_only": True},
    {"field_name": "density_50c", "display_name": "Density at 50°C", "data_type": "number", "unit": "kg/l", "category": "Physical", "catalog_only": True},
    {"field_name": "correction_factor_table", "display_name": "Correction Factor Table", "data_type": "text", "unit": None, "category": "Physical", "catalog_only": True},
    {"field_name": "density_as_printed", "display_name": "Density As Printed", "data_type": "text", "unit": None, "category": "Physical", "catalog_only": True},
    # Lars Stole Birkeland CGOSPEC columns, named as the sheet prints them (source 13).
    {"field_name": "SpGr", "display_name": "SpGr", "data_type": "number", "unit": "kg/l", "category": "Physical", "catalog_only": True},
    {"field_name": "Temp", "display_name": "Temp", "data_type": "number", "unit": "°C", "category": "Physical", "catalog_only": True},
    {"field_name": "Correction factor", "display_name": "Correction factor", "data_type": "number", "unit": None, "category": "Physical", "catalog_only": True},
    {"field_name": "Ship Type", "display_name": "Ship Type", "data_type": "text", "unit": None, "category": "Regulatory", "catalog_only": True},
    {"field_name": "Tank Type", "display_name": "Tank Type", "data_type": "text", "unit": None, "category": "Regulatory", "catalog_only": True},
    {"field_name": "Pollution cat", "display_name": "Pollution cat", "data_type": "text", "unit": None, "category": "Regulatory", "catalog_only": True},
    {"field_name": "Compliance", "display_name": "Compliance", "data_type": "text", "unit": None, "category": "Regulatory", "catalog_only": True},
    {"field_name": "USCG compat", "display_name": "USCG compat", "data_type": "text", "unit": None, "category": "Regulatory", "catalog_only": True},
    {"field_name": "Boiling point", "display_name": "Boiling point", "data_type": "number", "unit": "°C", "category": "Physical", "catalog_only": True},
    {"field_name": "Melting point", "display_name": "Melting point", "data_type": "number", "unit": "°C", "category": "Physical", "catalog_only": True},
    {"field_name": "Flash point", "display_name": "Flash point", "data_type": "number", "unit": "°C", "category": "Physical", "catalog_only": True},
    {"field_name": "Heat adjacent", "display_name": "Heat adjacent", "data_type": "number", "unit": "°C", "category": "Carriage", "catalog_only": True},
    {"field_name": "Heat req V", "display_name": "Heat req V", "data_type": "number", "unit": "°C", "category": "Carriage", "catalog_only": True},
    {"field_name": "Heat req D", "display_name": "Heat req D", "data_type": "number", "unit": "°C", "category": "Carriage", "catalog_only": True},
    {"field_name": "Colour", "display_name": "Colour", "data_type": "text", "unit": None, "category": "Physical", "catalog_only": True},
    {"field_name": "Solubility", "display_name": "Solubility", "data_type": "text", "unit": "g/g", "category": "Physical", "catalog_only": True},
    {"field_name": "UnNr", "display_name": "UnNr", "data_type": "text", "unit": None, "category": "Regulatory", "catalog_only": True},
    {"field_name": "tank_vents",                             "display_name": "Tank Vents",                     "data_type": "text",    "unit": None,      "category": "Regulatory", "catalog_only": True},
    {"field_name": "tank_environment_control",               "display_name": "Tank Environment Control",       "data_type": "text",    "unit": None,      "category": "Regulatory", "catalog_only": True},
    {"field_name": "electrical_equipment_apparatus_group",   "display_name": "Electrical Apparatus Group",     "data_type": "text",    "unit": None,      "category": "Regulatory", "catalog_only": True},
    {"field_name": "electrical_equipment_temperature_class", "display_name": "Electrical Temperature Class",   "data_type": "text",    "unit": None,      "category": "Regulatory", "catalog_only": True},
    {"field_name": "flashpoint_requirement",                 "display_name": "Flashpoint Requirement",         "data_type": "text",    "unit": None,      "category": "Regulatory", "catalog_only": True},
    {"field_name": "gauging",                                "display_name": "Gauging",                        "data_type": "text",    "unit": None,      "category": "Regulatory", "catalog_only": True},
    {"field_name": "vapour_detection",                       "display_name": "Vapour Detection",               "data_type": "text",    "unit": None,      "category": "Regulatory", "catalog_only": True},
    {"field_name": "fire_protection",                        "display_name": "Fire Protection",                "data_type": "text",    "unit": None,      "category": "Regulatory", "catalog_only": True},
    {"field_name": "emergency_equipment",                    "display_name": "Emergency Equipment Required",   "data_type": "boolean", "unit": None,      "category": "Regulatory", "catalog_only": True},

    # ---- Catalog-only property vocabulary (cargo_property_values field_name) --
    # These define valid property keys for per-source physical values. They are
    # NOT materialized as cargo_chemical columns (catalog_only=True).
    {"field_name": "appearance",                "display_name": "Appearance",                "data_type": "text",   "unit": None,       "category": "Physical", "catalog_only": True},
    {"field_name": "odour_limit",               "display_name": "Odour Limit",               "data_type": "number", "unit": "ppm",      "category": "Physical", "catalog_only": True},
    {"field_name": "density",                   "display_name": "Density",                   "data_type": "number", "unit": "kg/l",     "category": "Physical", "catalog_only": True},
    {"field_name": "specific_gravity",          "display_name": "Specific Gravity",          "data_type": "number", "unit": None,       "category": "Physical", "catalog_only": True},
    {"field_name": "correction_factor",         "display_name": "Correction Factor",         "data_type": "number", "unit": None,       "category": "Physical", "catalog_only": True},
    {"field_name": "boiling_point",             "display_name": "Boiling Point",             "data_type": "number", "unit": "°C",       "category": "Physical", "catalog_only": True},
    {"field_name": "melting_point",             "display_name": "Melting Point",             "data_type": "number", "unit": "°C",       "category": "Physical", "catalog_only": True},
    {"field_name": "flash_point",               "display_name": "Flash Point",               "data_type": "number", "unit": "°C",       "category": "Physical", "catalog_only": True},
    {"field_name": "vapour_pressure",           "display_name": "Vapour Pressure",           "data_type": "number", "unit": "bar",      "category": "Physical", "catalog_only": True},
    {"field_name": "vapour_density",            "display_name": "Vapour Density",            "data_type": "number", "unit": None,       "category": "Physical", "catalog_only": True},
    {"field_name": "viscosity",                 "display_name": "Viscosity",                 "data_type": "number", "unit": "mPa·s",    "category": "Physical", "catalog_only": True},
    {"field_name": "surface_tension",           "display_name": "Surface Tension",           "data_type": "number", "unit": "mN/m",     "category": "Physical", "catalog_only": True},
    {"field_name": "conductivity",              "display_name": "Electrical Conductivity",   "data_type": "number", "unit": "pS/m",     "category": "Physical", "catalog_only": True},
    {"field_name": "auto_ignition_temperature", "display_name": "Auto-Ignition Temperature", "data_type": "number", "unit": "°C",       "category": "Physical", "catalog_only": True},
    {"field_name": "lel",                       "display_name": "Lower Explosive Limit",     "data_type": "number", "unit": "% vol",    "category": "Physical", "catalog_only": True},
    {"field_name": "uel",                       "display_name": "Upper Explosive Limit",     "data_type": "number", "unit": "% vol",    "category": "Physical", "catalog_only": True},
    # Heating temperatures (°C). Some sources (e.g. LARS) record these where the
    # cargo_chemical column is only a Boolean "heating required" flag, so the
    # actual temperature is preserved here in cargo_property_values.
    {"field_name": "heat_adjacent_temp_c",      "display_name": "Heat Adjacent Temperature", "data_type": "number", "unit": "°C",       "category": "Carriage", "catalog_only": True},
    {"field_name": "heating_voyage_temp_c",     "display_name": "Heating Temp (Voyage)",     "data_type": "number", "unit": "°C",       "category": "Carriage", "catalog_only": True},
    {"field_name": "heating_discharge_temp_c",  "display_name": "Heating Temp (Discharge)",  "data_type": "number", "unit": "°C",       "category": "Carriage", "catalog_only": True},
    # Reid Vapor Pressure and flammable (explosive) limits. The USCG 1990 guide
    # reports RVP in psi and flammable_limits as a text range (e.g. "2.5 to 12.8%"),
    # so the latter is text, not a single number.
    {"field_name": "reid_vapor_pressure",       "display_name": "Reid Vapor Pressure",       "data_type": "number", "unit": "psi",      "category": "Physical", "catalog_only": True},
    {"field_name": "flammable_limits",          "display_name": "Flammable Limits",          "data_type": "text",   "unit": None,       "category": "Physical", "catalog_only": True},
]

# Sittig's Handbook contributes ~60 further catalog-only property keys. They are
# defined next to their loader (etl/chemical/sittig_handbook.py) and spliced in
# here so the prune pass below sees them as desired — without this, a run of this
# script would DELETE those field_definitions rows and CASCADE-delete every
# cargo_property_values row the Sittig import wrote.
#
# This is the one place a common/ module reaches into a branch, and it is the
# reason field_definition.py is shared rather than chemical: field_definitions is
# the vocabulary for BOTH branches (crude_oil_property_values FKs it too), while
# these particular keys happen to be chemical. Prune correctness needs the full
# desired set, so the import stays.
_ETL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ETL_ROOT / "chemical"))

from sittig_handbook import NEW_FIELDS as SITTIG_FIELDS  # noqa: E402

FIELDS += SITTIG_FIELDS

# ----------------------------------------------------------------------------
# Miracle Tank Cleaning Guide "Chemicals" sheet: columns that had no home.
# All catalog_only - they are per-source statements from one guide, not facts
# about the chemical that every source would restate, so they belong in
# cargo_property_values rather than as cargo_chemical columns.
# ----------------------------------------------------------------------------
MIRACLE_FIELDS = [
    # The sheet heads this "Env. Hazard" but prints tank-atmosphere control -
    # Inert / Dry / Open, or "No" for none required. It is ALSO mapped to the
    # cargo_chemical.tank_environment_control column, which is what those values
    # mean; this field keeps the guide's own wording under its own heading, so
    # the source stays quotable without reading the interpretation back.
    {"field_name": "env_hazard",           "display_name": "Env. Hazard (tank atmosphere)", "data_type": "text", "unit": None, "category": "Carriage",   "sql_type": "text", "catalog_only": True},
    {"field_name": "eri_card",             "display_name": "ERI Card",                "data_type": "text",   "unit": None, "category": "Regulatory", "sql_type": "text", "catalog_only": True},
    {"field_name": "cleanliness_standard", "display_name": "Cleanliness Standard",    "data_type": "text",   "unit": None, "category": "Cleaning",   "sql_type": "text", "catalog_only": True},
    {"field_name": "fosfa_niop_status",    "display_name": "FOSFA / NIOP Status",     "data_type": "text",   "unit": None, "category": "Regulatory", "sql_type": "text", "catalog_only": True},
    {"field_name": "safety_remarks",       "display_name": "Safety Remarks",          "data_type": "text",   "unit": None, "category": "Health",     "sql_type": "text", "catalog_only": True},
    {"field_name": "info_after_discharge", "display_name": "Info After Discharge",    "data_type": "text",   "unit": None, "category": "Cleaning",   "sql_type": "text", "catalog_only": True},
    {"field_name": "cleaning_method_count","display_name": "Number of Cleaning Methods","data_type": "number","unit": None, "category": "Cleaning",   "sql_type": "integer", "catalog_only": True},
]

FIELDS += MIRACLE_FIELDS

# ----------------------------------------------------------------------------
# USCG Chemical Data Guide (7th ed. 1990): columns with no existing field of the
# same meaning. Columns that DO match an existing field (first_aid, osha_pel,
# spill_handling, ...) reuse it; see USCG_PROPERTY_MAP in chemical/master_loader.py.
# Kept separate from look-alikes on purpose: electrical_group is the US NEC
# class (C/D), not the IEC apparatus group; health_hazard_rating is the USCG
# three-digit rating, not NFPA; material_compatibility is corrosion of tank
# materials, not chemical incompatibility.
# ----------------------------------------------------------------------------
USCG_FIELDS = [
    {"field_name": "fire_grade",                       "display_name": "Fire Grade",                       "data_type": "text", "unit": None, "category": "Physical",   "catalog_only": True},
    {"field_name": "electrical_group",                 "display_name": "Electrical Group (NEC)",           "data_type": "text", "unit": None, "category": "Regulatory", "catalog_only": True},
    {"field_name": "special_fire_procedures",          "display_name": "Special Fire Procedures",          "data_type": "text", "unit": None, "category": "Health",     "catalog_only": True},
    {"field_name": "health_hazard_rating",             "display_name": "Health Hazard Rating (USCG)",      "data_type": "text", "unit": None, "category": "Health",     "catalog_only": True},
    {"field_name": "symptoms",                         "display_name": "Symptoms",                         "data_type": "text", "unit": None, "category": "Health",     "catalog_only": True},
    {"field_name": "short_term_exposure",              "display_name": "Short-Term Exposure Tolerance",    "data_type": "text", "unit": None, "category": "Health",     "catalog_only": True},
    {"field_name": "stability",                        "display_name": "Stability",                        "data_type": "text", "unit": None, "category": "Physical",   "catalog_only": True},
    {"field_name": "material_compatibility",           "display_name": "Material Compatibility",           "data_type": "text", "unit": None, "category": "Carriage",   "catalog_only": True},
    {"field_name": "hazardous_decomposition_products", "display_name": "Hazardous Decomposition Products", "data_type": "text", "unit": None, "category": "Health",     "catalog_only": True},
    {"field_name": "hazardous_polymerization",         "display_name": "Hazardous Polymerization",         "data_type": "text", "unit": None, "category": "Physical",   "catalog_only": True},
    {"field_name": "cargo_compatibility_group",        "display_name": "Cargo Compatibility Group",        "data_type": "text", "unit": None, "category": "Carriage",   "catalog_only": True},
    {"field_name": "remarks",                          "display_name": "Remarks",                          "data_type": "text", "unit": None, "category": "Identity",   "catalog_only": True},
    {"field_name": "general_hazard_note",              "display_name": "General Hazard Note",              "data_type": "text", "unit": None, "category": "Health",     "catalog_only": True},
    {"field_name": "note",                             "display_name": "Note",                             "data_type": "text", "unit": None, "category": "Identity",   "catalog_only": True},
]

FIELDS += USCG_FIELDS
# ----------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("field_definition")


def get_cargo_columns(cur):
    cur.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema='public' AND table_name=%s
        """,
        (CARGO_TABLE,),
    )
    return {r[0] for r in cur.fetchall()}


def main():
    parser = argparse.ArgumentParser(description="Seed field_definitions and sync cargo_chemical columns.")
    parser.add_argument("--dry-run", action="store_true", help="Show actions, write nothing")
    args = parser.parse_args()

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        sys.exit("Error: DATABASE_URL not set (checked .env).")

    log.info("Connecting to database")
    conn = psycopg2.connect(db_url)
    try:
        with conn.cursor() as cur:
            existing = get_cargo_columns(cur)
            log.info("cargo_chemical currently has %d columns", len(existing))

            created = 0
            for i, f in enumerate(FIELDS):
                name = f["field_name"]
                catalog_only = f.get("catalog_only", False)

                # Catalog-only fields define the property vocabulary used by
                # cargo_property_values (per-source values). They are NOT
                # materialized as cargo_chemical columns, so skip column sync.
                if not catalog_only:
                    sql_type = f["sql_type"]
                    if sql_type not in SQL_TYPES:
                        sys.exit(f"Error: unsupported sql_type '{sql_type}' for {name}")

                    # --- ensure the cargo_chemical column exists ----------------
                    if name in existing:
                        log.info("==========================Cargo Chemical Column Check==========================")
                        log.info("✓ column '%s' already exists in %s -> skip", name, CARGO_TABLE)
                        log.info("==========================Cargo Chemical Column Check==========================")
                    else:
                        log.info("==========================Cargo Chemical Column create==========================")
                        log.info("✚ column '%s' missing -> CREATE %s %s", name, name, sql_type)
                        log.info("==========================Cargo Chemical Column create==========================")

                        if not args.dry_run:
                            cur.execute(sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS {} {}").format(
                                sql.Identifier(CARGO_TABLE),
                                sql.Identifier(name),
                                sql.SQL(sql_type),
                            ))
                        created += 1
                        existing.add(name)

                # --- insert the catalog row (skip if already present) -----------
                    log.info("==========================Field Definations Column create==========================")
                
                if not args.dry_run:
                    cur.execute(
                        """
                        INSERT INTO field_definitions
                            (field_name, display_name, data_type, unit, category, display_order)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (field_name) DO NOTHING
                        """,
                        (name, f["display_name"], f["data_type"], f["unit"], f["category"], (i + 1) * 10),
                    )

            # --- prune: a field removed from FIELDS -> drop its column ----------
            # Source of truth = FIELDS. Any field_definitions row that is no
            # longer listed in FIELDS gets deleted, and its cargo_chemical
            # column dropped (unless it is a protected structural column).
            desired = {f["field_name"] for f in FIELDS}
            cur.execute("SELECT field_name FROM field_definitions")
            catalogued = {r[0] for r in cur.fetchall()}
            stale = sorted(catalogued - desired)

            dropped = 0
            for name in stale:
                if name in PROTECTED_COLUMNS:
                    log.warning("• '%s' removed from FIELDS but is PROTECTED -> keeping column", name)
                elif name in existing:
                    log.warning("✗ '%s' removed from FIELDS -> DROP COLUMN from %s (data lost)",
                                name, CARGO_TABLE)
                    if not args.dry_run:
                        cur.execute(sql.SQL("ALTER TABLE {} DROP COLUMN IF EXISTS {}").format(
                            sql.Identifier(CARGO_TABLE), sql.Identifier(name)))
                    dropped += 1
                else:
                    log.info("• '%s' removed from FIELDS (no such column) -> removing catalog row", name)
                # remove the stale catalog row regardless (unless protected)
                if name not in PROTECTED_COLUMNS and not args.dry_run:
                    cur.execute("DELETE FROM field_definitions WHERE field_name = %s", (name,))

            if args.dry_run:
                log.info("Dry run: would create %d column(s), drop %d column(s); nothing written.",
                         created, dropped)
                return

            conn.commit()
            log.info("Done: %d definitions processed, %d column(s) created, %d column(s) dropped in %s",
                     len(FIELDS), created, dropped, CARGO_TABLE)
    except Exception:
        log.exception("Failed - rolling back")
        conn.rollback()
        raise
    finally:
        conn.close()
        log.info("Connection closed")


if __name__ == "__main__":
    main()
