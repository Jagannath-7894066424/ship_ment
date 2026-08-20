#!/usr/bin/env python3
"""
Build cargo_family_group for the Dr Verwey Tank Cleaning Guide (PDF Book) and
link every loaded cargo_chemical to its family.

The 431 family names come from the FROM->TO matrix (column 1), because that
sheet names all 431 numbers. The Cargo Details sheet only prints a heading for
the 25 numbers that hold more than one member; for the other 406 the family and
the single cargo share a name.

Chemicals are linked through the `verwey_cargo_number` property that
verwey_pdf_book.py already wrote, so this script does not re-read the details
sheet.

Run AFTER verwey_pdf_book.py.

Usage:
    python3 etl/verwey_pdf_book_families.py
    python3 etl/verwey_pdf_book_families.py --dry-run
    python3 etl/verwey_pdf_book_families.py path/to/matrix.csv
"""

import argparse
import csv
import logging
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

from _paths import input_file
from cargo_chemicals import get_source_id

SOURCE_NAME = "Dr Verweys Tank Cleaning Guide Pdf Book"
DEFAULT_FILE = input_file("Dr Verweys Tank Cleaning Guide Pdf Book  _from-to procedure.csv")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("verwey_pdf_book_families")

# The matrix labels its rows inconsistently: some carry the bracket prefix
# ("[153] FATTY ACID METHYL ESTERS"), most do not. Strip it so the stored name
# is just the family name.
BRACKET_PREFIX_RE = re.compile(r"^\s*\[\s*\d+\s*\]\s*")


def parse_families(path: Path) -> List[Tuple[str, str]]:
    """Return [(family_code, family_name), ...] for all 431 Verwey numbers."""
    with open(path, encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))

    families: List[Tuple[str, str]] = []
    seen: set = set()
    for row in rows[1:]:
        if len(row) < 2:
            continue
        code = row[0].strip()
        name = BRACKET_PREFIX_RE.sub("", row[1].strip()).strip()
        if not code.isdigit() or not name or code in seen:
            continue
        seen.add(code)
        families.append((code, name))
    return families


def resolve_source(cur) -> int:
    sid = get_source_id(cur, SOURCE_NAME)
    if sid is None:
        log.error("Source %r not found - run verwey_pdf_book.py first", SOURCE_NAME)
        sys.exit(1)
    log.info("Using source id=%s (%r)", sid, SOURCE_NAME)
    return sid


def load_families(cur, families: List[Tuple[str, str]], source_id: int) -> Dict[str, int]:
    execute_values(
        cur,
        """
        INSERT INTO cargo_family_group
            (source_id, family_code, family_name, created_at, updated_at)
        VALUES %s
        ON CONFLICT (source_id, family_code) DO UPDATE SET
            family_name = EXCLUDED.family_name,
            updated_at  = now()
        """,
        [(source_id, code, name) for code, name in families],
        template="(%s,%s,%s,now(),now())",
        page_size=500,
    )
    cur.execute(
        "SELECT family_code, id FROM cargo_family_group WHERE source_id = %s", (source_id,)
    )
    return dict(cur.fetchall())


def link_chemicals(cur, ids: Dict[str, int], source_id: int) -> Tuple[int, int]:
    """Point each cargo_chemical at its family via the verwey_cargo_number property."""
    cur.execute(
        """
        SELECT cargo_id, value
          FROM cargo_property_values
         WHERE source_id = %s AND field_name = 'verwey_cargo_number'
        """,
        (source_id,),
    )
    rows = cur.fetchall()

    updates: List[Tuple[int, int]] = []
    unmatched: List[Tuple[int, str]] = []
    for cargo_id, value in rows:
        # Three chemicals appear under two Verwey numbers (the FAME entries at
        # 53/153, 307/153 and 417/153); the loader stored both as "53,153".
        # cargo_family_group_id holds one family, so the first number wins and
        # the full list stays visible on the conflict-flagged property row.
        codes = [c.strip() for c in (value or "").split(",") if c.strip()]
        family_id = next((ids[c] for c in codes if c in ids), None)
        if family_id is None:
            unmatched.append((cargo_id, value))
            continue
        if len(codes) > 1:
            log.warning("cargo_id=%s belongs to Verwey families %s - linked to %s "
                        "(single FK column; full list kept on the property row)",
                        cargo_id, ", ".join(codes), codes[0])
        updates.append((cargo_id, family_id))

    if unmatched:
        log.warning("%d chemical(s) had no matching family: %s",
                    len(unmatched), unmatched[:5])

    if updates:
        execute_values(
            cur,
            """
            UPDATE cargo_chemical c SET cargo_family_group_id = v.family_id,
                                        updated_at = now()
              FROM (VALUES %s) AS v(cargo_id, family_id)
             WHERE c.id = v.cargo_id
            """,
            updates,
            template="(%s::int,%s::int)",
            page_size=1000,
        )
    return len(updates), len(unmatched)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", default=str(DEFAULT_FILE))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    path = Path(args.file).expanduser()
    if not path.exists():
        log.error("Input file not found: %s", path)
        sys.exit(1)

    families = parse_families(path)
    log.info("Parsed %d families from %s", len(families), path.name)
    if not families:
        sys.exit(1)

    load_dotenv(".env")
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        log.error("DATABASE_URL not set (run from the repo root)")
        sys.exit(1)

    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            source_id = resolve_source(cur)
            ids = load_families(cur, families, source_id)
            linked, unmatched = link_chemicals(cur, ids, source_id)

            cur.execute(
                """
                SELECT count(*) FILTER (WHERE n = 1), count(*) FILTER (WHERE n > 1), max(n)
                  FROM (SELECT cargo_family_group_id, count(*) n
                          FROM cargo_chemical
                         WHERE source_id = %s AND cargo_family_group_id IS NOT NULL
                         GROUP BY 1) t
                """,
                (source_id,),
            )
            singles, multi, largest = cur.fetchone()

        if args.dry_run:
            conn.rollback()
            log.info("DRY RUN - rolled back")
        else:
            conn.commit()
        log.info("Families: %d | chemicals linked: %d | unmatched: %d",
                 len(ids), linked, unmatched)
        log.info("Families holding 1 chemical: %s | holding >1: %s | largest: %s",
                 singles, multi, largest)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
