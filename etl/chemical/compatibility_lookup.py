#!/usr/bin/env python3
"""
Compatibility resolution for two cargoes (46 CFR Part 150).

Each source has its own cargo_chemical row, so both cargoes are first expanded to
every row of the same chemical with the DB function cargo_identity(id):
  strict = same normalized name, loose = one non-ambiguous synonym hop.

Then, most specific rule first:

    1. Chemical-pair exception between the two identities (either stored order).
    2. Cargo->group exception: one cargo against a reactive group of the other.
    3. Reactive-group matrix (compatibility) for the cargoes' groups.

Within a step the most restrictive result wins. Loose (synonym) matches may only
make a result incompatible; a "compatible" exception must match strictly.
Group 0 ("Unassigned Cargoes") has no matrix entries and is never "same group =>
compatible": an unassigned cargo is decided only by an exception, else unknown.

Usage (manual test):
    python compatibility_lookup.py <cargo_a_id> <cargo_b_id>

Reads DATABASE_URL from the .env file in this directory.
"""

import os
import sys
from pathlib import Path
from typing import Dict, Optional, Set, Tuple

import psycopg2
from dotenv import load_dotenv

UNASSIGNED_CODE = 0
EXC_COLS = "id, cargo_a_id, cargo_b_id, group_b_id, compatible, exception_type"


def _canonical(a: int, b: int) -> Tuple[int, int]:
    return (a, b) if a <= b else (b, a)


def _identity(cur, cargo_id: int) -> Tuple[Set[int], Set[int]]:
    """(strict ids, all ids) of the chemical cargo_id belongs to."""
    cur.execute("SELECT cargo_id, strict FROM cargo_identity(%s)", (cargo_id,))
    rows = cur.fetchall()
    return {i for i, st in rows if st}, {i for i, _ in rows}


def _reactive_groups(cur, cargo_ids: Set[int]) -> Dict[int, int]:
    """{reactive_group_id: group_code} over the given cargo rows."""
    cur.execute(
        "SELECT DISTINCT reactive_group_id, group_code FROM cargo_reactive_group WHERE cargo_id = ANY(%s)",
        (list(cargo_ids),),
    )
    return dict(cur.fetchall())


def _pick(rows, is_strict) -> Optional[dict]:
    """Incompatible (strict or loose) beats compatible; compatible must be strict."""
    for want in (False, True):
        hits = [r for r in rows if r[4] is want and (want is False or is_strict(r))]
        if hits:
            r = next((h for h in hits if is_strict(h)), hits[0])
            return {
                "compatible": r[4],
                "source": "exception",
                "detail": {"exception_id": r[0], "match": "exact" if is_strict(r) else "synonym",
                           "exception_type": r[5], "group_id": r[3]},
            }
    return None


def resolve_compatibility(cur, cargo_a_id: int, cargo_b_id: int) -> dict:
    """Resolve whether two cargoes are compatible.

    Returns {"compatible": bool | None, "source": "exception" | "matrix" | "unknown",
             "detail": {...}}; None => undetermined.
    """
    strict_a, all_a = _identity(cur, cargo_a_id)
    strict_b, all_b = _identity(cur, cargo_b_id)

    # 1) Chemical-pair exception between the identities, either order.
    cur.execute(
        f"SELECT {EXC_COLS} FROM compatibility_exception "
        "WHERE (cargo_a_id = ANY(%s) AND cargo_b_id = ANY(%s)) "
        "   OR (cargo_a_id = ANY(%s) AND cargo_b_id = ANY(%s))",
        (list(all_a), list(all_b), list(all_b), list(all_a)),
    )
    hit = _pick(cur.fetchall(), lambda r: (r[1] in strict_a and r[2] in strict_b)
                or (r[1] in strict_b and r[2] in strict_a))
    if hit:
        hit["detail"]["kind"] = "pair"
        return hit

    groups_a = _reactive_groups(cur, strict_a)
    groups_b = _reactive_groups(cur, strict_b)

    # 2) Cargo->group exception: A against B's groups, or B against A's groups.
    cur.execute(
        f"SELECT {EXC_COLS} FROM compatibility_exception WHERE cargo_b_id IS NULL AND ("
        "(cargo_a_id = ANY(%s) AND group_b_id = ANY(%s)) OR (cargo_a_id = ANY(%s) AND group_b_id = ANY(%s)))",
        (list(all_a), list(groups_b), list(all_b), list(groups_a)),
    )
    hit = _pick(cur.fetchall(), lambda r: (r[1] in strict_a and r[3] in groups_b)
                or (r[1] in strict_b and r[3] in groups_a))
    if hit:
        hit["detail"]["kind"] = "cargo_group"
        return hit

    if not groups_a or not groups_b:
        return {"compatible": None, "source": "unknown",
                "detail": {"reason": "one or both cargoes have no reactive group"}}

    # 3) Matrix. Unassigned (group 0) cargoes have no chart entry.
    assigned_a = {g for g, code in groups_a.items() if code != UNASSIGNED_CODE}
    assigned_b = {g for g, code in groups_b.items() if code != UNASSIGNED_CODE}
    if not assigned_a or not assigned_b:
        return {"compatible": None, "source": "unknown",
                "detail": {"reason": "unassigned cargo (group 0): no chart entry and no "
                                     "Appendix I exception; decide case by case"}}

    incompatible_hit = None
    matched = False
    for ga in assigned_a:
        for gb in assigned_b:
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
            if m[0] is False:
                incompatible_hit = (x, y, m[1])

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

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
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
