#!/usr/bin/env python3
"""
Populate `operational_requirement` and its join table `cargo_operational_requirement`
from an IBC Code operational-requirements spreadsheet.

Input file columns (see IBC Code Chemical.xlsx):

    Chemical name | Code | Title | Explanation

Two tables are loaded from that one file:

1. operational_requirement  (one row per UNIQUE code)
   The file repeats every code once per chemical, so codes are DEDUPLICATED by
   `code` (first occurrence wins for title/explanation). We write:

       code        <- Code                       (unique)
       section     <- Code minus its last dotted segment  (15.11.2 -> 15.11)
       title       <- Title
       description <- ""                          (not in file; column is NOT NULL)
       explanation <- Explanation
       source_id   <- resolved source (see below)

2. cargo_operational_requirement  (cargo <-> requirement links)
   For each file row we resolve:

       cargo_chemical_id          <- cargo_chemical.id matched by
                                     lower(canonical_name) = lower(Chemical name)
                                     AND source_id = resolved source_id
       operational_requirement_id <- operational_requirement.id for that Code

   The same cargo name lives under more than one source, so filtering the cargo
   lookup by source_id is what picks the right row. Duplicate (cargo, requirement)
   pairs are collapsed (@@unique([cargo_chemical_id, operational_requirement_id])).

source_id resolution:
   Derived from the FILE NAME by partial-matching it against source.name, e.g.
   'IBC Code Chemical.xlsx' -> source 'IBC Code'. The longest source name that is
   a substring of the normalized file name wins. This same id is used for
   operational_requirement.source_id and to filter the cargo lookup.

Each real run TRUNCATEs both tables (CASCADE) and reloads them from scratch.

PREREQUISITE: cargo_chemical must already be populated (run cargo_chemicals.py).

Usage:
    python3 cargo_operational_requirement.py                 # wipe + reload (DEFAULT_FILE)
    python3 cargo_operational_requirement.py path/to/file.xlsx
    python3 cargo_operational_requirement.py --dry-run       # resolve + log, no writes
    python3 cargo_operational_requirement.py --source-id 2   # force source_id (skip name match)

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
DEFAULT_FILE = input_file("IBC Code.xlsx")
REQ_TABLE = "operational_requirement"
LINK_TABLE = "cargo_operational_requirement"

CHEMICAL_COLUMN = "Chemical name"
CODE_COLUMN = "Code"
TITLE_COLUMN = "Title"
EXPLANATION_COLUMN = "Explanation"
DESCRIPTION_DEFAULT = ""          # column is NOT NULL but not present in the file
# ----------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("cargo_operational_requirement")


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


def normalize_match(text: str) -> str:
    """lowercase, non-alphanumeric -> space, collapse whitespace."""
    s = text.lower()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def section_of(code: str) -> str:
    """Section = code with its last dotted segment removed (15.11.2 -> 15.11).

    A code with no dot (rare) is its own section.
    """
    code = code.strip()
    return code.rsplit(".", 1)[0] if "." in code else code


def get_source_id_from_filename(cur, path: Path):
    """Resolve source.id by partial-matching the file name against source.name.

    The normalized file stem ('ibc code chemical') is searched for each source's
    normalized name as a substring; the LONGEST matching source name wins so a
    short generic name can't shadow a more specific one. Returns None if nothing
    matches.
    """
    stem = normalize_match(path.stem)
    log.info("Resolving source from file name: %r (normalized %r)", path.name, stem)
    cur.execute("SELECT id, name FROM source")
    best = None  # (len, id, name)
    for sid, sname in cur.fetchall():
        norm = normalize_match(sname)
        if norm and norm in stem:
            if best is None or len(norm) > best[0]:
                best = (len(norm), sid, sname)
    if best is None:
        log.warning("No source name partially matches file '%s'", path.name)
        return None
    log.info("Matched source id=%s (%r) for file '%s'", best[1], best[2], path.name)
    return best[1]


def load_cargo_lookup(cur, source_id):
    """Map lower(canonical_name) -> cargo_chemical.id, filtered by source_id.

    The same canonical_name exists under multiple sources, so the source filter
    is required to pick the intended cargo row.
    """
    cur.execute(
        "SELECT lower(canonical_name), id FROM cargo_chemical WHERE source_id = %s",
        (source_id,),
    )
    lookup = {}
    for name, cid in cur.fetchall():
        lookup.setdefault(name, cid)   # first id wins on any name collision
    log.info("Loaded %d cargo names under source_id=%s", len(lookup), source_id)
    return lookup


def main():
    parser = argparse.ArgumentParser(
        description="Populate operational_requirement + cargo_operational_requirement."
    )
    parser.add_argument("file", nargs="?", default=DEFAULT_FILE, help="CSV/XLSX path")
    parser.add_argument("--dry-run", action="store_true", help="Resolve + log, no writes")
    parser.add_argument("--source-id", type=int, default=None,
                        help="Force source_id instead of matching the file name")
    args = parser.parse_args()

    path = Path(args.file)
    if not path.is_file():
        sys.exit(f"Error: file not found: {path}")

    load_dotenv(Path(__file__).parent.parent / ".env")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        sys.exit("Error: DATABASE_URL not set (checked .env).")

    df = read_file(path)
    for col in (CHEMICAL_COLUMN, CODE_COLUMN, TITLE_COLUMN, EXPLANATION_COLUMN):
        if col not in df.columns:
            sys.exit(f"Error: column '{col}' not found in file.")

    log.info("Connecting to database")
    conn = psycopg2.connect(db_url)
    try:
        with conn.cursor() as cur:
            source_id = args.source_id
            if source_id is None:
                source_id = get_source_id_from_filename(cur, path)
                if source_id is None:
                    sys.exit("Error: could not resolve source_id from file name; "
                             "pass --source-id explicitly.")
            else:
                log.info("Using forced source_id=%s", source_id)

            cargo_by_name = load_cargo_lookup(cur, source_id)

            # --- Pass 1: dedupe operational_requirement rows by code ---------
            req_by_code = {}   # code -> (section, title, description, explanation)
            for idx, row in df.iterrows():
                code = str(row[CODE_COLUMN]).strip()
                if not code:
                    continue
                if code in req_by_code:
                    continue   # first occurrence wins
                req_by_code[code] = (
                    section_of(code),
                    str(row[TITLE_COLUMN]).strip(),
                    DESCRIPTION_DEFAULT,
                    str(row[EXPLANATION_COLUMN]).strip() or None,
                )
            log.info("Prepared %d unique operational_requirement rows from %d file rows",
                     len(req_by_code), len(df))

            # --- Pass 2: build cargo<->requirement links ---------------------
            link_pairs = set()        # (cargo_id, code) - unique pairs
            missing_cargo = set()
            for idx, row in df.iterrows():
                line = idx + 2
                code = str(row[CODE_COLUMN]).strip()
                chem = str(row[CHEMICAL_COLUMN]).strip()
                if not code or not chem:
                    continue
                cargo_id = cargo_by_name.get(chem.lower())
                if cargo_id is None:
                    if chem.lower() not in missing_cargo:
                        missing_cargo.add(chem.lower())
                        log.info("✗ row %d cargo not found under source_id=%s: %r",
                                 line, source_id, chem)
                    continue
                link_pairs.add((cargo_id, code))
            log.info("Prepared %d unique cargo/requirement links; %d cargo names unmatched",
                     len(link_pairs), len(missing_cargo))

            if args.dry_run:
                log.info("Dry run: %d requirements, %d links ready, nothing written.",
                         len(req_by_code), len(link_pairs))
                return

            # --- Wipe + reload both tables (child cascades from parent) ------
            log.info("Truncating %s and %s before reload", LINK_TABLE, REQ_TABLE)
            cur.execute(sql.SQL("TRUNCATE TABLE {} RESTART IDENTITY CASCADE")
                        .format(sql.Identifier(REQ_TABLE)))

            if not req_by_code:
                log.info("No requirements to insert; tables left empty.")
                conn.commit()
                return

            req_rows = [
                (code, section, title, description, explanation, source_id)
                for code, (section, title, description, explanation) in req_by_code.items()
            ]
            stmt = sql.SQL(
                "INSERT INTO {} (code, section, title, description, explanation, source_id) "
                "VALUES %s"
            ).format(sql.Identifier(REQ_TABLE))
            execute_values(cur, stmt, req_rows)
            log.info("Inserted %d rows into %s", len(req_rows), REQ_TABLE)

            # Map code -> new operational_requirement.id for the join rows.
            cur.execute(sql.SQL("SELECT code, id FROM {}").format(sql.Identifier(REQ_TABLE)))
            req_id_by_code = {code: rid for code, rid in cur.fetchall()}

            link_rows = []
            for cargo_id, code in link_pairs:
                req_id = req_id_by_code.get(code)
                if req_id is None:
                    continue
                link_rows.append((cargo_id, req_id, None))

            if link_rows:
                stmt = sql.SQL(
                    "INSERT INTO {} (cargo_chemical_id, operational_requirement_id, notes) "
                    "VALUES %s"
                ).format(sql.Identifier(LINK_TABLE))
                execute_values(cur, stmt, link_rows)
            conn.commit()
            log.info("Done: %d requirements, %d links inserted.",
                     len(req_rows), len(link_rows))
    except Exception:
        log.exception("Import failed - rolling back")
        conn.rollback()
        raise
    finally:
        conn.close()
        log.info("Connection closed")


if __name__ == "__main__":
    main()
