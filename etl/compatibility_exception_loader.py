#!/usr/bin/env python3
"""
Load 46 CFR Part 150 Appendix I chemical-pair exceptions into compatibility_exception.

Input CSV columns (appendix_I_b_cargo_exceptions.csv):

    id, cargo_a_name, group__a_name, group_a_id, cargo_b_name, group_b_name,
    group_b_id, compatible, exception_type, appendix, section

Mapping into the table:
    cargo_a_name  -> cargo_a_id   (resolved via cargo_chemical: canonical + synonyms)
    cargo_b_name  -> cargo_b_id
    group_a_id    -> group_a_id   (the file value is a group CODE -> reactive_groups.id)
    group_b_id    -> group_b_id
    compatible    -> compatible   ("✅ TRUE"/"TRUE" -> true, "FALSE" -> false)
    exception_type-> exception_type (enum: Compatible | Incompatible)
    appendix, section -> as-is

Rows keep the file's order (cargo_a_id = cargo_a_name, cargo_b_id = cargo_b_name).
Chemicals that cannot be matched are logged for manual review and skipped (never
fatal). Re-runs are idempotent (ON CONFLICT (cargo_a_id, cargo_b_id) DO NOTHING).

Usage:
    python compatibility_exception_loader.py
    python compatibility_exception_loader.py --file /path/to.csv
    python compatibility_exception_loader.py --source-id 24
    python compatibility_exception_loader.py --show-unmatched --dry-run

Reads DATABASE_URL from the .env file in this directory.
"""

import argparse
import logging
import os
import re
import sys
from pathlib import Path

from _paths import input_file
from typing import Dict, Optional, Set, Tuple

import pandas as pd
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

# Reuse the chemical-name resolver + source resolution already built there.
from cargo_compatibility import CargoResolver, _text, get_or_create_source, derive_source_name

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("compatibility_exception_loader")

DEFAULT_FILE = input_file("USCG CHRIS Chemical Data Guides_chemical_exceptions.csv")
VALID_EXCEPTION_TYPES = {"Compatible", "Incompatible"}


def parse_bool(value) -> Optional[bool]:
    """Interpret a compatible cell (e.g. '✅ TRUE', 'FALSE') -> True/False/None."""
    s = re.sub(r"[^a-z]", "", _text(value).lower()
               )   # strip emoji/spaces/punctuation
    if s in ("true", "t", "yes", "y", "compatible"):
        return True
    if s in ("false", "f", "no", "n", "incompatible"):
        return False
    return None


def as_group_code(value) -> Optional[str]:
    """Interpret a group-code cell ('18', '18.0') -> '18', else None."""
    s = _text(value)
    if not s:
        return None
    try:
        return str(int(float(s)))
    except (TypeError, ValueError):
        return None


def resolve_or_create(cur, resolver, cache: dict, name: str, source_id: int,
                      create: bool) -> Tuple[Optional[int], bool]:
    """Return (cargo_id, created?). Resolve via cargo_chemical; optionally create a
    minimal row (canonical_name + source) when the chemical is not found."""
    cid = resolver.resolve(name)
    if cid is not None:
        return cid, False
    key = name.strip().lower()
    if key in cache:
        return cache[key], False
    if not create or not key:
        return None, False
    cur.execute(
        "INSERT INTO cargo_chemical (canonical_name, source_id, date_added, "
        "date_last_updated, created_at, updated_at) VALUES (%s, %s, now(), now(), now(), now()) "
        "ON CONFLICT (source_id, canonical_name) DO UPDATE SET updated_at = now() RETURNING id",
        (name.strip(), source_id),
    )
    cid = cur.fetchone()[0]
    cache[key] = cid
    return cid, True


def pick_rg_source(cur, forced: Optional[int]) -> int:
    """Source whose reactive_groups the file's group CODES map against (id lookup)."""
    if forced is not None:
        cur.execute(
            "SELECT 1 FROM reactive_groups WHERE source_id=%s LIMIT 1", (forced,))
        if cur.fetchone() is None:
            sys.exit(f"Error: no reactive_groups for source_id={forced}.")
        return forced
    cur.execute(
        "SELECT source_id, count(*) c FROM reactive_groups GROUP BY source_id ORDER BY c DESC LIMIT 1"
    )
    row = cur.fetchone()
    if row is None:
        sys.exit(
            "Error: reactive_groups is empty — import a compatibility chart first.")
    return row[0]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Load Appendix I exceptions into compatibility_exception.")
    parser.add_argument("file", nargs="?", default=DEFAULT_FILE,
                        help="path to the exceptions CSV")
    parser.add_argument("--source-id", type=int, default=None,
                        help="force the exception rows' source_id (default: match by file name)")
    parser.add_argument("--rg-source-id", type=int, default=None,
                        help="source whose reactive_groups map the group codes (default: auto)")
    parser.add_argument("--wipe", action="store_true",
                        help="delete existing exceptions first")
    parser.add_argument("--create-missing", action="store_true",
                        help="create a cargo_chemical row for names not found, then insert the exception")
    parser.add_argument("--show-unmatched", action="store_true",
                        help="print the pairs whose chemicals couldn't be matched")
    parser.add_argument("--dry-run", action="store_true",
                        help="report, write nothing")
    args = parser.parse_args()

    path = Path(args.file)
    if not path.is_file():
        sys.exit(f"Error: file not found: {path}")

    load_dotenv(Path(__file__).parent.parent / ".env")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        sys.exit("Error: DATABASE_URL not set (checked .env).")

    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df.columns = [str(c).strip() for c in df.columns]
    log.info("Read %d exception rows from %s", len(df), path.name)

    conn = psycopg2.connect(db_url)
    try:
        with conn.cursor() as cur:
            # Exception rows' source_id: forced, else matched from the FILE NAME
            # (partial match against the source table, e.g.
            # "USCG CHRIS Chemical Data Guides_chemical_exceptions.csv"
            #  -> "USCG CHRIS Chemical Data Guide").
            if args.source_id is not None:
                cur.execute(
                    "SELECT id, name FROM source WHERE id=%s", (args.source_id,))
                row = cur.fetchone()
                if row is None:
                    sys.exit(f"Error: source_id={args.source_id} not found.")
                source_id, source_name = row
                log.info("Exception source (forced): id=%s (%r)",
                         source_id, source_name)
            else:
                # find_source partial match -> reuse
                source_id = get_or_create_source(cur, path)
                log.info("Exception source matched from file name %r -> id=%s",
                         derive_source_name(path), source_id)

            # Group CODES in the file map to reactive_groups ids of the taxonomy source.
            rg_source = pick_rg_source(cur, args.rg_source_id)
            cur.execute(
                "SELECT group_code, id FROM reactive_groups WHERE source_id=%s", (rg_source,))
            code_to_rg: Dict[str, int] = {
                code: rid for code, rid in cur.fetchall()}
            resolver = CargoResolver(cur)
            log.info("group codes mapped via reactive_groups source_id=%s (%d groups)",
                     rg_source, len(code_to_rg))

            if args.wipe and not args.dry_run:
                cur.execute("DELETE FROM compatibility_exception")
                log.info("Deleted %d existing exception rows", cur.rowcount)

            # Existing rows -> idempotent skip. Chemical-pair rows key on (a,b);
            # cargo->group rows (cargo_b_id NULL) key on (a, group_a_id, group_b_id).
            existing: Set[tuple] = set()
            if not args.wipe:
                cur.execute(
                    "SELECT cargo_a_id, cargo_b_id, group_a_id, group_b_id FROM compatibility_exception"
                )
                for a, b, ga_, gb_ in cur.fetchall():
                    existing.add(("pair", a, b) if b is not None else (
                        "grp", a, ga_, gb_))

            to_insert = []
            seen: Set[tuple] = set()
            unmatched = []
            created_cache: dict = {}
            n_created = 0
            for _, r in df.iterrows():
                a_name, b_name = _text(r.get("cargo_a_name")), _text(
                    r.get("cargo_b_name"))

                # cargo_a is required (NOT NULL) -> resolve, or create it (--create-missing).
                a_id, made = resolve_or_create(cur, resolver, created_cache, a_name,
                                               source_id, args.create_missing and not args.dry_run)
                n_created += made
                if a_id is None:
                    unmatched.append(
                        f"{a_name!r} <> {b_name or '(group)'}  [no match: cargo_a]")
                    continue

                # cargo_b: blank -> a cargo->GROUP exception (cargo_b_id NULL, use group_b_id);
                #          named but not in DB -> resolve or create it.
                if b_name:
                    b_id, made = resolve_or_create(cur, resolver, created_cache, b_name,
                                                   source_id, args.create_missing and not args.dry_run)
                    n_created += made
                    if b_id is None:
                        unmatched.append(
                            f"{a_name!r} <> {b_name!r}  [no match: cargo_b]")
                        continue
                    if b_id == a_id:
                        unmatched.append(
                            f"{a_name!r} <> {b_name!r}  [same chemical]")
                        continue
                else:
                    b_id = None

                compatible = parse_bool(r.get("compatible"))
                if compatible is None:
                    unmatched.append(
                        f"{a_name!r} <> {b_name or '(group)'}  [unclear 'compatible']")
                    continue

                etype = _text(r.get("exception_type"))
                if etype not in VALID_EXCEPTION_TYPES:
                    etype = "Compatible" if compatible else "Incompatible"

                ga = code_to_rg.get(as_group_code(r.get("group_a_id")))
                gb = code_to_rg.get(as_group_code(r.get("group_b_id")))
                appendix = _text(r.get("appendix")) or None
                section = _text(r.get("section")) or None

                if b_id is None:
                    # cargo->group exception: need a group on the b side to be meaningful.
                    if gb is None:
                        unmatched.append(
                            f"{a_name!r} <> (group {as_group_code(r.get('group_b_id'))})  [group_b not in reactive_groups]")
                        continue
                    key = ("grp", a_id, ga, gb)
                else:
                    # Keep the file's order: cargo_a_id = cargo_a_name, cargo_b_id = cargo_b_name.
                    key = ("pair", a_id, b_id)

                if key in existing or key in seen:
                    continue
                seen.add(key)
                to_insert.append((a_id, b_id, ga, gb, compatible, etype, appendix, section,
                                  None, None, source_id))

            n_pair = sum(1 for row in to_insert if row[1] is not None)
            n_grp = len(to_insert) - n_pair
            log.info("Matched: %d (chemical-pair %d, cargo->group %d) | created chemicals: %d | skipped: %d",
                     len(to_insert), n_pair, n_grp, n_created, len(unmatched))
            if args.show_unmatched:
                for u in unmatched:
                    log.warning("  UNMATCHED: %s", u)

            if args.dry_run:
                log.info("Dry run: rolling back, nothing written.")
                conn.rollback()
                return

            # Insert one row at a time.
            n_ins = 0
            for row in to_insert:
                cur.execute(
                    "INSERT INTO compatibility_exception "
                    "(cargo_a_id, cargo_b_id, group_a_id, group_b_id, compatible, exception_type, "
                    "appendix, section, reason, notes, source_id, created_at, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now()) "
                    "ON CONFLICT (cargo_a_id, cargo_b_id) DO NOTHING",
                    row,
                )
                n_ins += cur.rowcount
            conn.commit()
            log.info(
                "✓ Inserted %d exception rows (created %d chemicals).", n_ins, n_created)
    except Exception:
        conn.rollback()
        log.exception("Load failed - rolled back.")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
