#!/usr/bin/env python3
"""
Import chemical synonyms into the `synonyms` table.

Source: the same "Chemical Cargo specifications" spreadsheet. The COMMODITIES
column holds the alternate/synonym names (e.g. "Ethyl alcohol", "Alcohol")
listed under each parent chemical. Every non-empty COMMODITIES cell becomes
one synonyms row.

Column mapping (file/derived -> synonyms table):
    COMMODITIES            -> synonym_text      (as spelled, e.g. "Phenyl hydride")
    normalize(synonym_text)-> normalized_text   (lowercased, punctuation-stripped)
    "en"                   -> language          (ISO 639-1)
    source lookup by name  -> source_id         (the source that contributed the name)
    (DB default now())     -> date_added, created_at, updated_at
    (DB serial)            -> id

Usage:
    python3 import_synonyms.py                 # uses DEFAULT_FILE
    python3 import_synonyms.py path/to/file.csv
    python3 import_synonyms.py --dry-run       # parse + show rows, no writes
    python3 import_synonyms.py --truncate      # empty the table first
    python3 import_synonyms.py --source-id 14  # override the source lookup

Reads DATABASE_URL from the .env file in this directory.
"""

import argparse
import logging
import os
import re
import sys
from pathlib import Path

# Loaders are run as scripts (python3 etl/<branch>/<file>.py), so only their
# own directory is on sys.path. Add the shared helpers.
_ETL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ETL_ROOT / "common"))

from _paths import input_file

import pandas as pd
import psycopg2
from psycopg2 import sql
from psycopg2.extras import execute_values
from dotenv import load_dotenv

# ----------------------------------------------------------------------------
DEFAULT_FILE = input_file("Lars Stole Birkeland - Chemical Cargo specifications - 2002.xlsx - CGOSPEC.csv")
TARGET_TABLE = "synonyms"
SYNONYM_COLUMN = "COMMODITIES"   # file column that holds synonym names
SOURCE_NAME = "Lars Stole Birkeland — Chemical Cargo specifications - 2002"
LANGUAGE = "en"                  # ISO 639-1 language tag for every row
DEDUPE = True                    # skip repeated normalized_text values
# ----------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("import_synonyms")


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
    """Lowercase, strip punctuation, collapse whitespace -> fuzzy-search key."""
    s = text.lower()
    s = re.sub(r"[^\w\s]", " ", s)   # punctuation -> space
    s = re.sub(r"\s+", " ", s)       # collapse runs of whitespace
    return s.strip()


def resolve_source(cur, forced):
    """source.id for this spreadsheet - the origin stamped on every row it creates.

    Exact match first, then a LIKE on the leading words: the name carries an
    em dash that is easy to lose when the source row is re-registered by hand.
    """
    if forced is not None:
        cur.execute("SELECT id FROM source WHERE id = %s", (forced,))
        if cur.fetchone() is None:
            sys.exit(f"Error: --source-id {forced} not found in source table.")
        return forced
    cur.execute("SELECT id FROM source WHERE name = %s", (SOURCE_NAME,))
    row = cur.fetchone()
    if row is None:
        cur.execute("SELECT id FROM source WHERE name ILIKE %s ORDER BY id LIMIT 1",
                    ("Lars Stole Birkeland%",))
        row = cur.fetchone()
    if row is None:
        sys.exit(
            f"Error: source {SOURCE_NAME!r} not found.\n"
            f"  It is declared in etl/data/source.json - register it with:\n"
            f"      python3 etl/common/source.py"
        )
    return row[0]


def main():
    parser = argparse.ArgumentParser(description="Import synonyms into the synonyms table.")
    parser.add_argument("file", nargs="?", default=DEFAULT_FILE, help="CSV/XLSX path")
    parser.add_argument("--truncate", action="store_true", help="Empty the table first")
    parser.add_argument("--dry-run", action="store_true", help="Parse + log, no DB writes")
    parser.add_argument("--source-id", type=int, default=None,
                        help="Use this source.id instead of looking up SOURCE_NAME")
    args = parser.parse_args()

    path = Path(args.file)
    if not path.is_file():
        sys.exit(f"Error: file not found: {path}")

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        sys.exit("Error: DATABASE_URL not set (checked .env).")

    df = read_file(path)
    if SYNONYM_COLUMN not in df.columns:
        sys.exit(f"Error: column '{SYNONYM_COLUMN}' not found in file.")

    # Build (synonym_text, normalized_text, language) rows from non-empty cells;
    # source_id is appended once the DB is open and the source resolved.
    rows = []
    seen = set()
    skipped = 0
    for idx, value in df[SYNONYM_COLUMN].items():
        line = idx + 2  # header + 1-based
        text = str(value).strip()
        if text == "":
            skipped += 1
            log.info("✗ row %d SKIP (empty %s)", line, SYNONYM_COLUMN)
            continue
        norm = normalize(text)
        if DEDUPE and norm in seen:
            skipped += 1
            log.info("✗ row %d SKIP (duplicate): %s", line, text)
            continue
        seen.add(norm)
        rows.append((text, norm, LANGUAGE))
        log.info("✓ row %d INSERT: synonym_text=%r normalized_text=%r language=%r",
                 line, text, norm, LANGUAGE)

    log.info("Prepared %d synonyms, skipped %d", len(rows), skipped)

    if args.dry_run:
        log.info("Dry run: %d synonyms ready, nothing written.", len(rows))
        return

    if not rows:
        log.info("Nothing to insert.")
        return

    log.info("Connecting to database")
    conn = psycopg2.connect(db_url)
    try:
        with conn.cursor() as cur:
            source_id = resolve_source(cur, args.source_id)
            log.info("source_id=%s (%s)", source_id, SOURCE_NAME)
            rows = [r + (source_id,) for r in rows]

            if args.truncate:
                log.info("Truncating %s", TARGET_TABLE)
                cur.execute(sql.SQL("TRUNCATE TABLE {} RESTART IDENTITY CASCADE")
                            .format(sql.Identifier(TARGET_TABLE)))

            stmt = sql.SQL(
                "INSERT INTO {} (synonym_text, normalized_text, language, source_id) "
                "VALUES %s"
            ).format(sql.Identifier(TARGET_TABLE))
            execute_values(cur, stmt, rows)
            conn.commit()
            log.info("Done: inserted %d synonyms into %s", len(rows), TARGET_TABLE)
    except Exception:
        log.exception("Import failed - rolling back")
        conn.rollback()
        raise
    finally:
        conn.close()
        log.info("Connection closed")


if __name__ == "__main__":
    main()
