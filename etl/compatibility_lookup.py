#!/usr/bin/env python3
"""
Compatibility resolution for two cargoes (46 CFR Part 150).

The lookup ALWAYS follows this order — an Appendix I exception overrides the
reactive-group matrix:

    1. Search compatibility_exception using (cargo_a_id, cargo_b_id).
    2. If an exception exists, return that result immediately.
    3. Otherwise, determine the reactive groups of both cargoes.
    4. Query the compatibility matrix for those groups.
    5. Return the matrix result.

Both `compatibility` and `compatibility_exception` store each pair once, in
canonical order (smaller id first), so every lookup canonicalises the pair.

Usage (manual test):
    python compatibility_lookup.py <cargo_a_id> <cargo_b_id>

Reads DATABASE_URL from the .env file in this directory.
"""

import os
import sys
from pathlib import Path
from typing import Optional, Set, Tuple

import psycopg2
from dotenv import load_dotenv


def _canonical(a: int, b: int) -> Tuple[int, int]:
    """Return the pair in canonical order (smaller id first)."""
    return (a, b) if a <= b else (b, a)


def _reactive_group_ids(cur, cargo_id: int) -> Set[int]:
    """Reactive-group ids a cargo belongs to (via cargo_reactive_group)."""
    cur.execute(
        "SELECT reactive_group_id FROM cargo_reactive_group WHERE cargo_id = %s",
        (cargo_id,),
    )
    return {r[0] for r in cur.fetchall()}


def resolve_compatibility(cur, cargo_a_id: int, cargo_b_id: int) -> dict:
    """Resolve whether two cargoes are compatible, exceptions first.

    Returns a dict:
        {
          "compatible": bool | None,     # None => undetermined (no data)
          "source": "exception" | "matrix" | "unknown",
          "detail": ...                  # extra context per source
        }
    """
    a, b = _canonical(cargo_a_id, cargo_b_id)

    # 1–2) Exception overrides everything.
    cur.execute(
        "SELECT compatible, exception_type, appendix, section, reason "
        "FROM compatibility_exception WHERE cargo_a_id = %s AND cargo_b_id = %s",
        (a, b),
    )
    row = cur.fetchone()
    if row:
        return {
            "compatible": row[0],
            "source": "exception",
            "detail": {
                "exception_type": row[1],
                "appendix": row[2],
                "section": row[3],
                "reason": row[4],
            },
        }

    # 3) Reactive groups of both cargoes.
    groups_a = _reactive_group_ids(cur, cargo_a_id)
    groups_b = _reactive_group_ids(cur, cargo_b_id)
    if not groups_a or not groups_b:
        return {"compatible": None, "source": "unknown",
                "detail": {"reason": "one or both cargoes have no reactive group"}}

    # 4) Query the matrix for every group combination (canonical order). The most
    #    restrictive result wins: if ANY group pair is incompatible, so is the pair.
    incompatible_hit = None
    matched = False
    for ga in groups_a:
        for gb in groups_b:
            if ga == gb:                      # same group => compatible with itself
                matched = True
                continue
            x, y = _canonical(ga, gb)
            cur.execute(
                "SELECT compatible, reaction_description "
                "FROM compatibility WHERE group_a_id = %s AND group_b_id = %s",
                (x, y),
            )
            m = cur.fetchone()
            if m is None:
                continue
            matched = True
            if m[0] is False:                 # incompatible found -> most restrictive
                incompatible_hit = (x, y, m[1])

    # 5) Return the matrix result.
    if incompatible_hit:
        return {
            "compatible": False,
            "source": "matrix",
            "detail": {"group_a_id": incompatible_hit[0],
                       "group_b_id": incompatible_hit[1],
                       "reaction_description": incompatible_hit[2]},
        }
    if matched:
        return {"compatible": True, "source": "matrix", "detail": {}}
    return {"compatible": None, "source": "unknown",
            "detail": {"reason": "no matrix entry for the cargoes' reactive groups"}}


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit("Usage: python compatibility_lookup.py <cargo_a_id> <cargo_b_id>")
    cargo_a_id, cargo_b_id = int(sys.argv[1]), int(sys.argv[2])

    load_dotenv(Path(__file__).parent.parent / ".env")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        sys.exit("Error: DATABASE_URL not set (checked .env).")

    conn = psycopg2.connect(db_url)
    try:
        with conn.cursor() as cur:
            result = resolve_compatibility(cur, cargo_a_id, cargo_b_id)
        print(result)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
