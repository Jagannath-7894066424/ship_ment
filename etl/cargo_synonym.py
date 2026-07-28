#!/usr/bin/env python3
"""
Populate the `cargo_synonym` join table (cargo_chemical <-> synonyms).

Source: the same "Chemical Cargo specifications" spreadsheet. For each synonym
in the COMMODITIES column, the owning cargo is the most recent parent name in
the first (unnamed) column above it. We resolve:

    cargo_id   <- cargo_chemical.id   matched by canonical_name (parent name, "Unnamed: 0")
    synonym_id <- synonyms.id         matched by normalized_text (of the COMMODITIES value)

and write one cargo_synonym row per (cargo, synonym) pair:

    relationship_type    = "common"            (default; edit RELATIONSHIP_TYPE)
    ambiguity_flag       = TRUE if the same synonym maps to >1 cargo, else FALSE
    source_id            = id of the SOURCE_NAME row if it exists, else NULL
    preferred_for_search = FALSE
    notes                = NULL

Each real run TRUNCATEs cargo_synonym and reloads it from scratch.

PREREQUISITES: run import_cargo_specs.py and import_synonyms.py first, so the
cargo_chemical and synonyms tables are populated for the lookups.

Usage:
    python3 cargo_synonym.py                 # wipe + reload (uses DEFAULT_FILE)
    python3 cargo_synonym.py path/to/file.csv
    python3 cargo_synonym.py --dry-run       # resolve + log, no writes

Reads DATABASE_URL from the .env file in this directory.
"""

import argparse
import logging
import os
import re
import sys
from pathlib import Path

from _paths import input_file

import pandas as pd
import psycopg2
from psycopg2 import sql
from psycopg2.extras import execute_values
from dotenv import load_dotenv

# ----------------------------------------------------------------------------
DEFAULT_FILE = input_file("Lars Stole Birkeland - Chemical Cargo specifications - 2002.xlsx - CGOSPEC.csv")
TARGET_TABLE = "cargo_synonym"
PARENT_COLUMN = "Unnamed: 0"     # first column: parent chemical name
SYNONYM_COLUMN = "COMMODITIES"   # column: synonym names
SOURCE_NAME = "Lars Stole Birkeland - Chemical Cargo specifications - 2002"
RELATIONSHIP_TYPE = "common"     # iupac|trade_name|common|abbreviation|...
PREFERRED_FOR_SEARCH = False
# ----------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("cargo_synonym")


def read_file(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    log.info("Reading file %s (type %s)", path, suffix)
    if suffix == ".csv":
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
    elif suffix in (".xlsx", ".xls"):
        df = pd.read_excel(path, dtype=str, keep_default_na=False)
    else:
        raise ValueError(f"Unsupported file type '{suffix}'. Use .csv or .xlsx.")
    df.columns = [str(c).strip() for c in df.columns]
    log.info("Loaded %d raw rows, columns: %s", len(df), list(df.columns))
    return df


def normalize(text: str) -> str:
    """Same normalization as import_synonyms.py (must match for lookups)."""
    s = text.lower()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def normalize_source_name(s: str) -> str:
    """Normalize a source name for fuzzy comparison against source.name.

    Mirrors cargo_chemicals.normalize_source_name so both scripts resolve the
    same source row. The stored name can differ from SOURCE_NAME by dash style
    (em/en dash vs hyphen) and by an optional trailing year, e.g.
    'Lars Stole Birkeland — Chemical Cargo specifications' (DB) vs
    'Lars Stole Birkeland - Chemical Cargo specifications - 2002' (config).

    - lowercase
    - unify dashes: em/en dash -> hyphen
    - drop a trailing year (e.g. ' - 2002')
    - collapse whitespace
    """
    s = s.lower()
    s = s.replace("—", "-").replace("–", "-")   # — – -> -
    s = re.sub(r"[-\s]*\d{4}\s*$", "", s)          # trailing year
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def load_lookup(cur, query):
    cur.execute(query)
    return {row[0]: row[1] for row in cur.fetchall()}


def get_source_id(cur, name):
    """Look up source.id whose name fuzzy-matches `name`.

    An exact `WHERE name = %s` match is too brittle: the source table stores
    'Lars Stole Birkeland — Chemical Cargo specifications' (em-dash, no year)
    while SOURCE_NAME here carries a plain hyphen and a trailing '- 2002'.
    We compare on normalized names so this resolves to the right source id and
    that id is used for cargo_synonym.source_id. Returns None if nothing matches.
    """
    target = normalize_source_name(name)
    log.info("Looking up source by normalized name: %r", target)
    cur.execute("SELECT id, name FROM source")
    for sid, sname in cur.fetchall():
        if normalize_source_name(sname) == target:
            log.info("Matched source id=%s (%r) for '%s'", sid, sname, name)
            return sid
    log.warning("Source '%s' not found -> source_id will be NULL", name)
    return None


def build_pairs(df):
    """Yield (line, parent_name, synonym_text) walking parent context."""
    current_parent = None
    for idx, row in df.iterrows():
        line = idx + 2  # header + 1-based
        parent = str(row.get(PARENT_COLUMN, "")).strip()
        synonym = str(row.get(SYNONYM_COLUMN, "")).strip()
        if parent != "":
            current_parent = parent
        if synonym != "":
            yield line, current_parent, synonym


def main():
    parser = argparse.ArgumentParser(description="Populate cargo_synonym join table.")
    parser.add_argument("file", nargs="?", default=DEFAULT_FILE, help="CSV/XLSX path")
    parser.add_argument("--dry-run", action="store_true", help="Resolve + log, no writes")
    args = parser.parse_args()

    path = Path(args.file)
    if not path.is_file():
        sys.exit(f"Error: file not found: {path}")

    load_dotenv(Path(__file__).parent.parent / ".env")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        sys.exit("Error: DATABASE_URL not set (checked .env).")

    df = read_file(path)
    for col in (PARENT_COLUMN, SYNONYM_COLUMN):
        if col not in df.columns:
            sys.exit(f"Error: column '{col}' not found in file.")

    # First pass: which normalized synonyms map to more than one cargo?
    parents_per_syn = {}
    for _, parent, synonym in build_pairs(df):
        if parent is None:
            continue
        parents_per_syn.setdefault(normalize(synonym), set()).add(parent)

    log.info("Connecting to database")
    conn = psycopg2.connect(db_url)
    try:
        with conn.cursor() as cur:
            cargo_by_name = load_lookup(cur, "SELECT canonical_name, id FROM cargo_chemical")
            syn_by_norm = load_lookup(cur, "SELECT normalized_text, id FROM synonyms")
            log.info("Loaded %d cargo names and %d synonyms for lookup",
                     len(cargo_by_name), len(syn_by_norm))
            source_id = get_source_id(cur, SOURCE_NAME)

            rows = []
            seen = set()           # (cargo_id, synonym_id) to avoid duplicate links
            skipped = 0
            for line, parent, synonym in build_pairs(df):
                if parent is None:
                    skipped += 1
                    log.info("✗ row %d SKIP (no parent yet): %s", line, synonym)
                    continue
                cargo_id = cargo_by_name.get(parent)
                norm = normalize(synonym)
                synonym_id = syn_by_norm.get(norm)
                if cargo_id is None:
                    skipped += 1
                    log.info("✗ row %d SKIP (cargo not found): %r", line, parent)
                    continue
                if synonym_id is None:
                    skipped += 1
                    log.info("✗ row %d SKIP (synonym not found): %r", line, synonym)
                    continue
                if (cargo_id, synonym_id) in seen:
                    skipped += 1
                    log.info("✗ row %d SKIP (duplicate link): %s -> %s", line, parent, synonym)
                    continue
                seen.add((cargo_id, synonym_id))

                ambiguity = len(parents_per_syn.get(norm, ())) > 1
                rows.append((cargo_id, synonym_id, RELATIONSHIP_TYPE, ambiguity,
                             source_id, PREFERRED_FOR_SEARCH, None))
                log.info("✓ row %d LINK: cargo_id=%s (%s) synonym_id=%s (%s) ambiguity=%s",
                         line, cargo_id, parent, synonym_id, synonym, ambiguity)

            log.info("Prepared %d links, skipped %d", len(rows), skipped)

            if args.dry_run:
                log.info("Dry run: %d links ready, nothing written.", len(rows))
                return

            # Each run wipes the table and reloads it from scratch.
            log.info("Truncating %s before reload", TARGET_TABLE)
            cur.execute(sql.SQL("TRUNCATE TABLE {} RESTART IDENTITY CASCADE")
                        .format(sql.Identifier(TARGET_TABLE)))

            if not rows:
                log.info("No links to insert; table left empty.")
                conn.commit()
                return

            stmt = sql.SQL(
                "INSERT INTO {} (cargo_id, synonym_id, relationship_type, "
                "ambiguity_flag, source_id, preferred_for_search, notes) VALUES %s"
            ).format(sql.Identifier(TARGET_TABLE))
            execute_values(cur, stmt, rows)
            conn.commit()
            log.info("Done: inserted %d links into %s", len(rows), TARGET_TABLE)
    except Exception:
        log.exception("Import failed - rolling back")
        conn.rollback()
        raise
    finally:
        conn.close()
        log.info("Connection closed")


if __name__ == "__main__":
    main()
