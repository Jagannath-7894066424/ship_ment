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
# defined next to their loader (etl/sittig_handbook.py) and spliced in here so
# the prune pass below sees them as desired — without this, a run of this script
# would DELETE those field_definitions rows and CASCADE-delete every
# cargo_property_values row the Sittig import wrote.
from sittig_handbook import NEW_FIELDS as SITTIG_FIELDS  # noqa: E402

FIELDS += SITTIG_FIELDS
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

    load_dotenv(Path(__file__).parent.parent / ".env")
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
