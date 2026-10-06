#!/usr/bin/env python3
"""
Load the Cargo Library 3 chemical-pair / cargo-vs-group exceptions into
compatibility_exception, under source_id=17 ("Cargo Library 3 TABLE OF
CHEMICAL CARGO").

cargo_compatibility.py already loads Cargo Library 3's GROUP-vs-GROUP chart
(reactive_groups + compatibility, 38 groups under source_id=17) from the
colour-coded matrix workbook. These two extra workbooks hold the finer,
CHEMICAL-specific exceptions to that group-level chart:

  --compatible-file   "Compatible Cargoes" sheet: specific chemical pairs that
                       ARE compatible despite their groups not being (exception
                       type "Compatible").

  --incompatible-file two sheets:
                       "Not Compatible by Group": a cargo vs. an entire
                       not-compatible GROUP (cargo_b left blank), OR — for its
                       last few rows where a "Not Compatibillity Chemical Name"
                       is given — a specific cargo-vs-cargo pair.
                       ("Specific Pairs" duplicates those same tail rows in a
                       cleaner layout and "Matrix" is a derived quick-check
                       grid; both are redundant with "Not Compatible by Group"
                       and are not read.)
                       All rows here are exception type "Incompatible".

Every cargo_a_id/cargo_b_id stored in a source_id=17 exception row must itself
be a cargo_chemical row with source_id=17 — this source's exceptions never
point at another source's chemical row. So chemical names are resolved only
against cargo_chemical rows already under source_id=17 (canonical name or
synonym); a name not found there is created as a new cargo_chemical row under
source_id=17, even if an identically-named chemical already exists under a
different source (same "own the whole chain" convention as
compatibility_exception_loader.py --all-under-source). Group numbers are
mapped through reactive_groups.group_code for source_id=17 (the same taxonomy
cargo_compatibility.py already populated there).

Since the cargo ids a name resolves to depend on which source owns them, a
re-run first deletes this source's own compatibility_exception rows and
rebuilds them from the two workbooks (cargo_chemical rows already created
under source_id=17 are reused, never duplicated, via their own
(source_id, canonical_name) unique constraint). Cargo-vs-group rows
(cargo_b_id IS NULL, so the compatibility_exception unique constraint can't
dedupe them) are additionally deduped in Python within a single run.

Two housekeeping passes run every time, over EVERY source_id=17 chemical (not
just ones this run touched), so a chemical created before either pass existed
still gets backfilled:
  - backfill_group_links: cargo_reactive_group link for a chemical that has
    none.
  - backfill_master_group_details: a (group_code, group_name, cargo_name) row
    in master_cargo_chemical_group_details for a chemical not yet named there
    — it's the global name->group reference link_cargo_reactive_groups.py
    falls back to, so these chemicals need to exist in it too.

Usage:
    python cargo_library_3_exceptions.py
    python cargo_library_3_exceptions.py --show-unmatched --dry-run
    python cargo_library_3_exceptions.py --source-id 17

Reads DATABASE_URL from the .env file at the repo root.
"""

import argparse
import logging
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import pandas as pd
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

_ETL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ETL_ROOT / "common"))

from _paths import input_file
from cargo_compatibility import CargoResolver, _text

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("cargo_library_3_exceptions")

DEFAULT_COMPATIBLE_FILE = input_file(
    "Cargo Library 3 TABLE OF CHEMICAL CARGO Comaptibillity Exception .xlsx")
DEFAULT_INCOMPATIBLE_FILE = input_file(
    "Cargo Library 3 TABLE OF CHEMICAL CARGO Not Comaptibillity Exception .xlsx")
DEFAULT_SOURCE_ID = 17

# A handful of "Not Compatible by Group" cargo-name cells carry an inline
# annotation instead of a clean name (e.g. "Fish oil - no with Sulphuric acid
# (2)"), restating what columns 3/5 already say. Strip it so the chemical
# resolves to its real name rather than creating a near-duplicate.
_NAME_ANNOTATION_RE = re.compile(r"\s*-\s*(?:no|not)?\s*with\b.*$", re.IGNORECASE)

# Not an actual chemical - a bare category label with no reactive_groups match.
# Left unmatched (logged, skipped) rather than created as a fake cargo_chemical.
_NON_CHEMICAL_LABELS = {"strong oxidizing agents iii"}


def clean_name(name: str) -> str:
    return _NAME_ANNOTATION_RE.sub("", name).strip()


def as_code(value) -> Optional[str]:
    """Interpret a group-number cell ('0', '20', '20.0') -> '0'/'20', else None.

    reactive_groups includes code "0" ("Unassigned Cargoes"), so unlike
    cargo_compatibility._as_code (restricted to 1..44) this accepts 0 too.
    """
    s = _text(value)
    if not s:
        return None
    try:
        return str(int(float(s)))
    except (TypeError, ValueError):
        return None


def read_compatible_rows(path: Path) -> List[dict]:
    """'Compatible Cargoes' sheet: S.No, cargo_a, group_a, cargo_b, group_b."""
    df = pd.read_excel(path, sheet_name="Compatible Cargoes", header=None, dtype=object)
    rows = []
    for _, r in df.iterrows():
        a_name = _text(r.get(1))
        if not a_name or a_name.lower() == "cargo chemical name":
            continue
        rows.append({
            "cargo_a_name": a_name,
            "group_a_code": as_code(r.get(2)),
            "cargo_b_name": _text(r.get(3)),
            "group_b_code": as_code(r.get(4)),
            "compatible": True,
            "exception_type": "Compatible",
        })
    return rows


def read_incompatible_rows(path: Path) -> List[dict]:
    """'Not Compatible by Group' sheet: cargo vs. a group, or (tail rows) vs. a
    specific cargo when a 'Not Compatibillity Chemical Name' is given."""
    df = pd.read_excel(path, sheet_name="Not Compatible by Group", header=None, dtype=object)
    rows = []
    for _, r in df.iterrows():
        a_name = clean_name(_text(r.get(1)))
        if not a_name or a_name.lower() == "cargo name":
            continue
        b_name = clean_name(_text(r.get(4)))
        rows.append({
            "cargo_a_name": a_name,
            "group_a_code": as_code(r.get(2)),
            "cargo_b_name": b_name,
            "group_b_code": as_code(r.get(5)),
            "compatible": False,
            "exception_type": "Incompatible",
        })
    return rows


def resolve_or_create(cur, resolver: CargoResolver, cache: dict, name: str,
                       source_id: int, dry_run: bool, group_code: Optional[str],
                       code_to_rg: Dict[str, int]) -> Tuple[Optional[int], bool, bool]:
    """Return (cargo_id, created?, group_linked?); creates a minimal cargo_chemical
    row under source_id when the name matches nothing in the global catalog, and
    links it to its reactive group (the sheet's "Cargo Group No" for this
    chemical) via cargo_reactive_group, same as link_cargo_reactive_groups.py."""
    cid = resolver.resolve(name)
    if cid is not None:
        return cid, False, False
    key = name.strip().lower()
    if not key:
        return None, False, False
    if key in cache:
        return cache[key], False, False
    if dry_run:
        cid = -(len(cache) + 1)  # placeholder so the would-create row is counted
        cache[key] = cid
        return cid, True, group_code in code_to_rg
    cur.execute(
        "INSERT INTO cargo_chemical (canonical_name, source_id, date_added, "
        "date_last_updated, created_at, updated_at) VALUES (%s, %s, now(), now(), now(), now()) "
        "ON CONFLICT (source_id, canonical_name) DO UPDATE SET updated_at = now() "
        "RETURNING id",
        (name.strip(), source_id),
    )
    cid = cur.fetchone()[0]
    cache[key] = cid
    rg_id = code_to_rg.get(group_code) if group_code else None
    if rg_id is not None:
        cur.execute(
            "INSERT INTO cargo_reactive_group "
            "(cargo_id, reactive_group_id, group_code, \"isPrimary\", source_id, notes, "
            "created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, now(), now()) "
            "ON CONFLICT DO NOTHING",
            (cid, rg_id, int(group_code), True, source_id,
             "Derived from Cargo Library 3 compatibility-exception sheet"),
        )
    return cid, True, rg_id is not None


def backfill_group_links(cur, rows: List[dict], source_id: int,
                          code_to_rg: Dict[str, int], dry_run: bool) -> int:
    """Link any cargo_chemical already under source_id that still has no
    cargo_reactive_group row (e.g. created by an earlier run of this script,
    before it linked groups on creation) to the group its name maps to here."""
    name_to_code: Dict[str, str] = {}
    for r in rows:
        for name_key, code_key in (("cargo_a_name", "group_a_code"),
                                    ("cargo_b_name", "group_b_code")):
            name, code = r[name_key], r[code_key]
            if name and code:
                name_to_code.setdefault(name.strip().lower(), code)

    cur.execute(
        "SELECT c.id, c.canonical_name FROM cargo_chemical c "
        "WHERE c.source_id = %s AND NOT EXISTS ("
        "  SELECT 1 FROM cargo_reactive_group g WHERE g.cargo_id = c.id)",
        (source_id,),
    )
    n_linked = 0
    for cargo_id, canonical_name in cur.fetchall():
        code = name_to_code.get((canonical_name or "").strip().lower())
        rg_id = code_to_rg.get(code) if code else None
        if rg_id is None:
            continue
        n_linked += 1
        if dry_run:
            continue
        cur.execute(
            "INSERT INTO cargo_reactive_group "
            "(cargo_id, reactive_group_id, group_code, \"isPrimary\", source_id, notes, "
            "created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, now(), now()) "
            "ON CONFLICT DO NOTHING",
            (cargo_id, rg_id, int(code), True, source_id,
             "Derived from Cargo Library 3 compatibility-exception sheet"),
        )
    return n_linked


def backfill_master_group_details(cur, source_id: int, dry_run: bool) -> int:
    """Add a (group_code, group_name, cargo_name) row to
    master_cargo_chemical_group_details for every source_id chemical that has
    no row there yet (by name) — it's the global name->group reference
    link_cargo_reactive_groups.py falls back to, so a chemical this script
    introduced should be findable there too. Existing rows for a name are left
    alone; only chemicals missing entirely are added."""
    cur.execute(
        "SELECT c.canonical_name, rg.group_code, rg.group_name "
        "FROM cargo_chemical c "
        "JOIN cargo_reactive_group g ON g.cargo_id = c.id "
        "JOIN reactive_groups rg ON rg.id = g.reactive_group_id "
        "WHERE c.source_id = %s",
        (source_id,),
    )
    candidates = cur.fetchall()

    cur.execute("SELECT DISTINCT lower(cargo_name) FROM master_cargo_chemical_group_details")
    existing_names = {r[0] for r in cur.fetchall()}

    to_insert = []
    seen = set()
    for cargo_name, group_code, group_name in candidates:
        key = cargo_name.strip().lower()
        if key in existing_names or key in seen:
            continue
        seen.add(key)
        to_insert.append((group_code, group_name, cargo_name))

    if not dry_run and to_insert:
        execute_values(
            cur,
            "INSERT INTO master_cargo_chemical_group_details "
            "(group_code, group_name, cargo_name, created_at, updated_at) VALUES %s",
            to_insert,
            template="(%s, %s, %s, now(), now())",
        )
    return len(to_insert)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Load Cargo Library 3 chemical-level compatibility exceptions.")
    parser.add_argument("--compatible-file", default=DEFAULT_COMPATIBLE_FILE,
                         help="path to the 'Compatible Cargoes' workbook")
    parser.add_argument("--incompatible-file", default=DEFAULT_INCOMPATIBLE_FILE,
                         help="path to the 'Not Compatible' workbook")
    parser.add_argument("--source-id", type=int, default=DEFAULT_SOURCE_ID,
                         help="source_id for both the exception rows and the reactive_groups "
                              "taxonomy the file's group numbers map against (default: 17)")
    parser.add_argument("--show-unmatched", action="store_true",
                         help="print the rows whose chemicals couldn't be resolved/created")
    parser.add_argument("--dry-run", action="store_true", help="report, write nothing")
    args = parser.parse_args()

    compat_path = Path(args.compatible_file)
    incompat_path = Path(args.incompatible_file)
    for p in (compat_path, incompat_path):
        if not p.is_file():
            sys.exit(f"Error: file not found: {p}")

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        sys.exit("Error: DATABASE_URL not set (checked .env).")

    rows = read_compatible_rows(compat_path) + read_incompatible_rows(incompat_path)
    log.info("Read %d exception rows (%d compatible, %d incompatible)", len(rows),
              sum(1 for r in rows if r["compatible"]),
              sum(1 for r in rows if not r["compatible"]))

    conn = psycopg2.connect(db_url)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name FROM source WHERE id=%s", (args.source_id,))
            src = cur.fetchone()
            if src is None:
                sys.exit(f"Error: source_id={args.source_id} not found.")
            log.info("Exception source: id=%s (%r)", src[0], src[1])

            cur.execute(
                "SELECT group_code, id FROM reactive_groups WHERE source_id=%s", (args.source_id,))
            code_to_rg: Dict[str, int] = {code: rid for code, rid in cur.fetchall()}
            if not code_to_rg:
                sys.exit(f"Error: no reactive_groups for source_id={args.source_id}. "
                         "Load the group chart first (cargo_compatibility.py).")
            log.info("group codes mapped via reactive_groups source_id=%s (%d groups)",
                     args.source_id, len(code_to_rg))

            # Chemical names resolve only within source_id's own cargo_chemical rows
            # (never borrow another source's id), so this source's cargo ids depend
            # entirely on what's loaded here — rebuild its exception rows from
            # scratch each run rather than trying to patch stale cross-source ones.
            if args.dry_run:
                cur.execute(
                    "SELECT count(*) FROM compatibility_exception WHERE source_id=%s",
                    (args.source_id,))
                log.info("Dry run: would delete %d existing source_id=%s exception rows "
                         "before rebuilding", cur.fetchone()[0], args.source_id)
            else:
                cur.execute(
                    "DELETE FROM compatibility_exception WHERE source_id=%s", (args.source_id,))
                log.info("Deleted %d existing source_id=%s exception rows (rebuilding)",
                         cur.rowcount, args.source_id)

            resolver = CargoResolver(cur, args.source_id)

            # Other sources' exception rows: still used to dedupe cargo-vs-group rows
            # (cargo_b_id IS NULL, so the table's unique constraint can't dedupe them)
            # in case another source already recorded the identical rule against the
            # SAME source_id=17 cargo id — e.g. a chemical reused across both runs.
            cur.execute(
                "SELECT cargo_a_id, cargo_b_id, group_b_id FROM compatibility_exception "
                "WHERE source_id != %s", (args.source_id,))
            existing: Set[tuple] = {
                ("pair", a, b) if b is not None else ("grp", a, gb)
                for a, b, gb in cur.fetchall()
            }

            to_insert = []
            seen: Set[tuple] = set()
            unmatched: List[str] = []
            created_cache: dict = {}
            n_created = 0
            n_group_linked = 0

            for r in rows:
                a_name, b_name = r["cargo_a_name"], r["cargo_b_name"]
                a_id, made, linked = resolve_or_create(cur, resolver, created_cache, a_name,
                                                        args.source_id, args.dry_run,
                                                        r["group_a_code"], code_to_rg)
                n_created += made
                n_group_linked += linked
                if a_id is None:
                    unmatched.append(f"{a_name!r} <> {b_name or '(group)'}  [blank cargo_a]")
                    continue

                ga = code_to_rg.get(r["group_a_code"])

                if b_name and b_name.lower() in _NON_CHEMICAL_LABELS:
                    unmatched.append(f"{a_name!r} <> {b_name!r}  [not a chemical, no group match]")
                    continue

                if b_name:
                    b_id, made, linked = resolve_or_create(cur, resolver, created_cache, b_name,
                                                            args.source_id, args.dry_run,
                                                            r["group_b_code"], code_to_rg)
                    n_created += made
                    n_group_linked += linked
                    if b_id is None:
                        unmatched.append(f"{a_name!r} <> {b_name!r}  [blank cargo_b]")
                        continue
                    if b_id == a_id:
                        unmatched.append(f"{a_name!r} <> {b_name!r}  [same chemical]")
                        continue
                    gb = code_to_rg.get(r["group_b_code"])
                    key = ("pair", a_id, b_id)
                else:
                    gb = code_to_rg.get(r["group_b_code"])
                    if gb is None:
                        unmatched.append(
                            f"{a_name!r} <> (group {r['group_b_code']!r})  [group not in reactive_groups]")
                        continue
                    key = ("grp", a_id, gb)

                if key in existing or key in seen:
                    continue
                seen.add(key)
                to_insert.append((a_id, b_id if b_name else None, ga, gb,
                                   r["compatible"], r["exception_type"],
                                   args.source_id))

            n_pair = sum(1 for row in to_insert if row[1] is not None)
            n_grp = len(to_insert) - n_pair
            log.info("Matched: %d (chemical-pair %d, cargo->group %d) | created chemicals: %d "
                      "(linked to a reactive group: %d) | skipped: %d",
                      len(to_insert), n_pair, n_grp, n_created, n_group_linked, len(unmatched))
            if args.show_unmatched:
                for u in unmatched:
                    log.warning("  UNMATCHED: %s", u)

            n_backfilled = backfill_group_links(cur, rows, args.source_id, code_to_rg, args.dry_run)
            log.info("Reactive-group backfill (chemicals under source_id=%s with no group link "
                      "yet): %d", args.source_id, n_backfilled)

            n_master_added = backfill_master_group_details(cur, args.source_id, args.dry_run)
            log.info("master_cargo_chemical_group_details backfill (chemicals with no row there "
                      "yet, by name): %d", n_master_added)

            if args.dry_run:
                log.info("Dry run: rolling back, nothing written.")
                conn.rollback()
                return

            n_ins = 0
            for row in to_insert:
                cur.execute(
                    "INSERT INTO compatibility_exception "
                    "(cargo_a_id, cargo_b_id, group_a_id, group_b_id, compatible, exception_type, "
                    "source_id, created_at, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, now(), now()) "
                    "ON CONFLICT (cargo_a_id, cargo_b_id) DO NOTHING",
                    row,
                )
                n_ins += cur.rowcount
            conn.commit()
            log.info("Inserted %d exception rows (created %d chemicals).", n_ins, n_created)
    except Exception:
        conn.rollback()
        log.exception("Load failed - rolled back.")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
