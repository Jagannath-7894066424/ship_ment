#!/usr/bin/env python3
"""
Link cargoes to their 46 CFR reactive groups (populates cargo_reactive_group).

For each cargo_chemical the reactive-group code is resolved in two ways:

  1. PRIMARY  — cargo_chemical.uscg_compatibility_group, e.g. "20",
                "20 ALCOHOLS AND GLYCOLS", "07 ALIPHATIC AMINES"; the leading
                number is the reactive-group code.

  2. FALLBACK — when uscg_compatibility_group is NULL/empty, match the cargo's
                canonical_name against master_cargo_chemical_group_details.cargo_name
                and take that row's group_code.

The code maps to a `reactive_groups` row (from the compatibility-chart source);
its id becomes reactive_group_id and its numeric code is stored in group_code.

Codes with no matching reactive group (e.g. 0 "Unassigned", 90/91/92) are logged
and skipped. Re-runs are idempotent (existing links are skipped).

Usage:
    python link_cargo_reactive_groups.py
    python link_cargo_reactive_groups.py --rg-source-id 24
    python link_cargo_reactive_groups.py --dry-run

Reads DATABASE_URL from the .env file in this directory.
"""

import argparse
import logging
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, Optional, Set, Tuple

import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("link_cargo_reactive_groups")

NOTE_USCG = "Derived from cargo_chemical.uscg_compatibility_group"
NOTE_NAME = "Derived from master_cargo_chemical_group_details (name match)"


def leading_code(value: str) -> Optional[str]:
    """Extract the leading group code, e.g. '07 ALIPHATIC AMINES' -> '7'."""
    m = re.match(r"\s*(\d+)", value or "")
    return str(int(m.group(1))) if m else None


def pick_rg_source(cur, forced: Optional[int]) -> int:
    """Choose which source's reactive_groups to link against."""
    if forced is not None:
        cur.execute("SELECT 1 FROM reactive_groups WHERE source_id=%s LIMIT 1", (forced,))
        if cur.fetchone() is None:
            sys.exit(f"Error: no reactive_groups for source_id={forced}.")
        return forced
    # Default: the source with the most reactive-group rows (the full taxonomy).
    cur.execute(
        "SELECT source_id, count(*) c FROM reactive_groups GROUP BY source_id ORDER BY c DESC LIMIT 1"
    )
    row = cur.fetchone()
    if row is None:
        sys.exit("Error: reactive_groups is empty — import a compatibility chart first.")
    return row[0]


def main() -> None:
    parser = argparse.ArgumentParser(description="Link cargoes to reactive groups.")
    parser.add_argument("--rg-source-id", type=int, default=None,
                        help="source_id whose reactive_groups to link to (default: auto)")
    parser.add_argument("--show-unmatched", action="store_true",
                        help="print the names of chemicals that couldn't be linked")
    parser.add_argument("--show-fallback", action="store_true",
                        help="print chemicals with no USCG group that were resolved via the master table")
    parser.add_argument("--dry-run", action="store_true", help="report, write nothing")
    args = parser.parse_args()

    load_dotenv(Path(__file__).parent.parent / ".env")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        sys.exit("Error: DATABASE_URL not set (checked .env).")

    conn = psycopg2.connect(db_url)
    try:
        with conn.cursor() as cur:
            rg_source = pick_rg_source(cur, args.rg_source_id)
            cur.execute("SELECT group_code, id FROM reactive_groups WHERE source_id=%s",(rg_source,))

            reactive_groups = cur.fetchall()

            print("\n========== Reactive Groups ==========")
            for group_code, rg_id in reactive_groups:
                print(f"group_code={group_code}, reactive_group_id={rg_id}")

            code_to_rg = {str(group_code): rg_id for group_code, rg_id in reactive_groups}
            log.info("Linking against source_id=%s (%d reactive groups)", rg_source, len(code_to_rg))

            # Fallback lookup: chemical name -> group_code (each name maps to one group).
            cur.execute("SELECT cargo_name, group_code FROM master_cargo_chemical_group_details")
            master_rows = cur.fetchall()

            print("\n========== Master Cargo Mapping ==========")
            for cargo_name, group_code in master_rows:
                print(f"{cargo_name} -> {group_code}")

            name_to_code: Dict[str, str] = {}
            for cargo_name, gcode in master_rows:
                if cargo_name:
                    name_to_code.setdefault(cargo_name.strip().lower(), str(gcode))
            log.info("Loaded %d name->group_code fallbacks", len(name_to_code))

            # Existing links -> idempotent skip.
            cur.execute("SELECT cargo_id, reactive_group_id FROM cargo_reactive_group")
            existing: Set[Tuple[int, int]] = {(a, b) for a, b in cur.fetchall()}

            cur.execute("SELECT id, canonical_name, uscg_compatibility_group FROM cargo_chemical")
            rows = cur.fetchall()

            to_insert = []
            unmatched = Counter()
            unmatched_names = []
            fallback_details = []          # (name, code) for no-USCG -> master-table links
            skipped_existing = 0
            n_via_uscg = n_via_name = 0
            for cargo_id, canonical_name, uscg in rows:
                # 1) PRIMARY: the USCG compatibility group.
                code = leading_code(uscg) if uscg and uscg.strip() else None
                note = NOTE_USCG
                via_name = False
                # 2) FALLBACK: name match in master_cargo_chemical_group_details.
                if code is None and canonical_name:
                    code = name_to_code.get(canonical_name.strip().lower())
                    note, via_name = NOTE_NAME, True

                rg_id = code_to_rg.get(code) if code else None
                if rg_id is None:
                    if code is not None:                # had a code but no reactive group (e.g. "0")
                        unmatched[code] += 1
                    unmatched_names.append(canonical_name or f"(cargo id {cargo_id})")
                    continue

                # Record HOW the group was resolved (for every matched cargo, whether
                # or not it is a new link) so the report is meaningful on re-runs too.
                if via_name:
                    fallback_details.append((canonical_name, code))

                if (cargo_id, rg_id) in existing:
                    skipped_existing += 1
                    continue
                existing.add((cargo_id, rg_id))
                to_insert.append((cargo_id, rg_id, int(code), True, rg_source, note))
                if via_name:
                    n_via_name += 1
                else:
                    n_via_uscg += 1

            log.info("cargo_chemical scanned: %d | new links: %d (uscg %d, name %d) | already linked: %d",
                     len(rows), len(to_insert), n_via_uscg, n_via_name, skipped_existing)
            if unmatched:
                log.warning("Codes with no reactive group (skipped): %s", dict(unmatched))

            # No USCG group in cargo_chemical -> group resolved from the master table.
            log.info("No USCG group -> resolved via master table: %d", len(fallback_details))
            if args.show_fallback:
                for nm, code in sorted(fallback_details, key=lambda x: str(x[0]).lower()):
                    log.info("  FROM MASTER: %s (group %s)", nm, code)

            # Chemicals with no reactive-group link (no USCG group and no name match).
            log.info("Unlinked chemicals (no data found): %d", len(unmatched_names))
            if args.show_unmatched:
                for nm in sorted(unmatched_names, key=str.lower):
                    log.info("  UNLINKED: %s", nm)

            if args.dry_run:
                log.info("Dry run: rolling back, nothing written.")
                conn.rollback()
                return

            if to_insert:
                execute_values(
                    cur,
                    "INSERT INTO cargo_reactive_group "
                    "(cargo_id, reactive_group_id, group_code, \"isPrimary\", source_id, notes, "
                    "created_at, updated_at) VALUES %s",
                    to_insert,
                    template="(%s, %s, %s, %s, %s, %s, now(), now())",
                )
            conn.commit()
            log.info("Linked %d cargoes to reactive groups.", len(to_insert))
    except Exception:
        conn.rollback()
        log.exception("Linking failed - rolled back.")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
