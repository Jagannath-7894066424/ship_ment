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
    (DB default now())     -> date_added, created_at, updated_at
    (DB serial)            -> id

Usage:
    python3 import_synonyms.py                 # uses DEFAULT_FILE
    python3 import_synonyms.py path/to/file.csv
    python3 import_synonyms.py --dry-run       # parse + show rows, no writes
    python3 import_synonyms.py --truncate      # empty the table first

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
TARGET_TABLE = "synonyms"
SYNONYM_COLUMN = "COMMODITIES"   # file column that holds synonym names
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


def main():
    parser = argparse.ArgumentParser(description="Import synonyms into the synonyms table.")
    parser.add_argument("file", nargs="?", default=DEFAULT_FILE, help="CSV/XLSX path")
    parser.add_argument("--truncate", action="store_true", help="Empty the table first")
    parser.add_argument("--dry-run", action="store_true", help="Parse + log, no DB writes")
    args = parser.parse_args()

    path = Path(args.file)
    if not path.is_file():
        sys.exit(f"Error: file not found: {path}")

    load_dotenv(Path(__file__).parent.parent / ".env")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        sys.exit("Error: DATABASE_URL not set (checked .env).")

    df = read_file(path)
    if SYNONYM_COLUMN not in df.columns:
        sys.exit(f"Error: column '{SYNONYM_COLUMN}' not found in file.")

    # Build (synonym_text, normalized_text, language) rows from non-empty cells.
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
            if args.truncate:
                log.info("Truncating %s", TARGET_TABLE)
                cur.execute(sql.SQL("TRUNCATE TABLE {} RESTART IDENTITY CASCADE")
                            .format(sql.Identifier(TARGET_TABLE)))

            stmt = sql.SQL(
                "INSERT INTO {} (synonym_text, normalized_text, language) VALUES %s"
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
