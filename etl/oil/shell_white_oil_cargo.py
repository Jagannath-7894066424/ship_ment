#!/usr/bin/env python3
"""
Load the Shell White Oil Tank Cleaning Guide cargo list into crude_oil, with the
guide's trade names in synonyms + crude_oil_synonym.

SOURCE
------
"Shell White Oil Tank Cleaning Guide" (source.json, category 'oil'). Six
spreadsheets are extracts of that ONE document, so every loader for it RESOLVES
the existing source row and none of them creates one:

    this loader                        cargo list  -> crude_oil, synonyms
    shell_white_oil_procedure_templates.py  codes  -> procedure_templates, steps,
                                                      requirements
    shell_white_oil_matrix.py         the 16x16 grid -> cleaning_process,
                                                      crude_oil_compatibility

This loader must run FIRST: the matrix resolves its from/to columns against the
rows created here.

A SEPARATE SOURCE FROM "Shell Tank Cleaning Guide 2016.pdf"
-----------------------------------------------------------
Shell's white-oil guide is its own document with its own 1..9 numbered codes,
and those numbers mean something different from the 30 numbered regimes of the
2016 pre-cargo matrix. procedure_templates is keyed on (source_id,
procedure_code), so sharing a source row would have one guide's code 2
overwrite the other's. The sheet's own `source_name` column says
"Shell White Oil Tank Cleaning Guide"; that is the source row used.

INPUT
-----
"... - cargo names.xlsx": 16 rows of

    id | cargo_name | cargo_type | trade_name

The sheet's `id` is its own 1..16 numbering, not a database id. It is NOT
stored - cargo_name is the identity, and crude_oil's key is (oil_name,
source_id). shell_white_oil_matrix.py joins on the name for the same reason.

WHY crude_oil
-------------
cargo_type is OIL on all 16 rows, and crude_oil is the oil-cargo master that
shell_cargo_master.py, hm50_cargo_matrix.py and bp_cargo.py already use for
refined products. Rows are per-source, so this guide's "Kerosine" sits alongside
HM 50's "Kerosene (un-dyed)" without either being merged into the other.

TRADE NAMES
-----------
`trade_name` is filled on 4 of the 16 rows and holds comma-separated aliases
("JP4, MC77, ATG"). Those are names, so they go to synonyms + crude_oil_synonym
with relationship_type 'trade_name' - the same treatment shell_cargo_master.py
gives its "Grade Names" column, which uses 'grade_name'. The synonyms row is
keyed on normalized_text and REUSED when it already exists, so a name both
guides publish is stored once and linked twice.

IDEMPOTENCY
-----------
Upsert on (oil_name, source_id) for the master and (crude_oil_id, synonym_id)
for a name link. One transaction; any error rolls the whole import back.

Usage:
    python3 etl/oil/shell_white_oil_cargo.py
    python3 etl/oil/shell_white_oil_cargo.py --dry-run
    python3 etl/oil/shell_white_oil_cargo.py "/path/to/cargo names.xlsx"
"""

import argparse
import logging
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import openpyxl
import psycopg2
from dotenv import load_dotenv

# Loaders are run as scripts (python3 etl/<branch>/<file>.py), so only their
# own directory is on sys.path. Add the shared helpers.
_ETL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ETL_ROOT / "common"))

from _paths import input_file  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("shell_white_oil_cargo")

SOURCE_NAME = "Shell White Oil Tank Cleaning Guide"
CARGO_TYPE = "OIL"
RELATIONSHIP_TYPE = "trade_name"
DEFAULT_FILE = input_file("Shell White Oil Tank Cleaning Guide - cargo names.xlsx")

COLS = ["cargo_name", "cargo_type", "trade_name"]

# Progress markers the extract carries in its own cells ("done" typed in the
# row below the data). They name no cargo, so they are skipped and reported
# rather than becoming a crude_oil row.
MARKERS = {"done", "ok", "completed", "-"}


def clean(value) -> Optional[str]:
    """Trim a cell; blank -> None (SQL NULL)."""
    if value is None:
        return None
    s = re.sub(r"\s+", " ", str(value).replace("\n", " ")).strip()
    return s or None


def normalize_synonym(text: str) -> str:
    """Match master_loader.normalize_synonym so both branches key names alike."""
    s = text.lower()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def split_trade_names(raw) -> List[str]:
    """Split the trade_name cell. The source separates with ',' and ';'."""
    s = clean(raw)
    if s is None:
        return []
    seen, out = set(), []
    for part in re.split(r"[;,]", s):
        p = part.strip()
        if not p:
            continue
        key = normalize_synonym(p)
        if key and key not in seen:
            seen.add(key)
            out.append(p)
    return out


def resolve_source(cur, name: str) -> int:
    cur.execute("SELECT id FROM source WHERE name = %s", (name,))
    row = cur.fetchone()
    if row is None:
        sys.exit(
            f"Error: source {name!r} not found.\n"
            f"  It is declared in etl/data/source.json - register it with:\n"
            f"      python3 etl/common/source.py"
        )
    return row[0]


def read_cargoes(path: Path) -> Tuple[List[dict], List[str], List[str]]:
    """Read the sheet. Returns (cargoes, skipped markers, errors)."""
    ws = openpyxl.load_workbook(path, data_only=True).worksheets[0]
    grid = [list(r) for r in ws.iter_rows(values_only=True)]
    errors: List[str] = []
    skipped: List[str] = []
    if not grid:
        return [], [], [f"{path.name} is empty"]

    header = [clean(c) for c in grid[0]]
    missing = [c for c in COLS if c not in header]
    if missing:
        sys.exit(f"Error: {path.name} is missing column(s): {', '.join(missing)}")

    cargoes: List[dict] = []
    seen: set = set()
    for i, raw in enumerate(grid[1:], start=2):
        r = dict(zip(header, raw))
        name = clean(r["cargo_name"])
        if name is None:
            continue
        if name.lower() in MARKERS:
            skipped.append(f"row {i}: {name!r} is a progress marker, not a cargo")
            continue
        if name in seen:
            errors.append(f"row {i}: duplicate cargo_name {name!r}")
            continue
        seen.add(name)

        sheet_type = (clean(r["cargo_type"]) or CARGO_TYPE).upper()
        if sheet_type != CARGO_TYPE:
            errors.append(f"row {i}: {name!r} says cargo_type {sheet_type!r}; "
                          f"this loader writes {CARGO_TYPE}")
            continue

        cargoes.append({"cargo_name": name,
                        "trade_names": split_trade_names(r["trade_name"])})
    return cargoes, skipped, errors


def upsert_cargo(cur, source_id: int, name: str) -> Tuple[int, bool]:
    cur.execute("SELECT id FROM crude_oil WHERE oil_name = %s AND source_id = %s",
                (name, source_id))
    row = cur.fetchone()
    if row:
        return row[0], False
    cur.execute(
        "INSERT INTO crude_oil (oil_name, source_id, created_at, updated_at) "
        "VALUES (%s, %s, now(), now()) RETURNING id",
        (name, source_id),
    )
    return cur.fetchone()[0], True


def get_or_create_synonym(cur, cache: Dict[str, int], text_value: str,
                          source_id: int) -> int:
    """synonyms row for this text, reusing an existing one where it matches.

    Keyed on normalized_text so this guide and every other branch converge on
    one row for one name instead of storing it twice. source_id lands on the row
    only when this call creates it - a reused row keeps the source that first
    published the name.
    """
    normalized = normalize_synonym(text_value)
    if normalized in cache:
        return cache[normalized]

    cur.execute("SELECT id FROM synonyms WHERE normalized_text = %s ORDER BY id LIMIT 1",
                (normalized,))
    row = cur.fetchone()
    if row:
        cache[normalized] = row[0]
        return row[0]

    cur.execute(
        "INSERT INTO synonyms (synonym_text, normalized_text, source_id, "
        "date_added, created_at, updated_at) "
        "VALUES (%s, %s, %s, now(), now(), now()) RETURNING id",
        (text_value, normalized, source_id),
    )
    sid = cur.fetchone()[0]
    cache[normalized] = sid
    return sid


def link_synonym(cur, crude_oil_id: int, synonym_id: int, source_id: int) -> None:
    cur.execute(
        """
        INSERT INTO crude_oil_synonym
            (crude_oil_id, synonym_id, relationship_type, ambiguity_flag, source_id,
             created_at, updated_at)
        VALUES (%s, %s, %s, false, %s, now(), now())
        ON CONFLICT (crude_oil_id, synonym_id) DO UPDATE SET
            relationship_type = EXCLUDED.relationship_type,
            updated_at        = now()
        """,
        (crude_oil_id, synonym_id, RELATIONSHIP_TYPE, source_id),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", default=str(DEFAULT_FILE))
    ap.add_argument("--dry-run", action="store_true", help="parse and report, write nothing")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.is_file():
        sys.exit(f"Error: file not found: {path}")

    cargoes, skipped, errors = read_cargoes(path)
    if errors:
        log.error("%d validation error(s) - nothing was imported:", len(errors))
        for e in errors:
            log.error("    %s", e)
        return 1

    n_names = sum(len(c["trade_names"]) for c in cargoes)
    log.info("%s: %d cargo(es), %d trade name(s) on %d of them",
             path.name, len(cargoes), n_names,
             sum(1 for c in cargoes if c["trade_names"]))
    for s in skipped:
        log.warning("skipped %s", s)

    if args.dry_run:
        for c in cargoes:
            log.info("  %-24s %s", c["cargo_name"],
                     ", ".join(c["trade_names"]) or "-")
        log.info("--dry-run: input is valid, nothing written.")
        return 0

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        sys.exit("Error: DATABASE_URL not set (checked .env).")

    conn = psycopg2.connect(db_url)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            source_id = resolve_source(cur, SOURCE_NAME)
            log.info("Source id=%s (%r)", source_id, SOURCE_NAME)

            cache: Dict[str, int] = {}
            created = links = 0
            for c in cargoes:
                oil_id, made = upsert_cargo(cur, source_id, c["cargo_name"])
                created += int(made)
                for tn in c["trade_names"]:
                    link_synonym(cur, oil_id,
                                 get_or_create_synonym(cur, cache, tn, source_id),
                                 source_id)
                    links += 1

        conn.commit()
        log.info("✓ Committed. crude_oil: %d cargo(es), %d created this run | "
                 "crude_oil_synonym: %d link(s) | cargo_type=%s",
                 len(cargoes), created, links, CARGO_TYPE)
        return 0
    except Exception:
        conn.rollback()
        log.exception("Import failed - rolled back, nothing written.")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
