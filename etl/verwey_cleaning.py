#!/usr/bin/env python3
"""
Import the Dr. Verwey Tank Cleaning Guide into cleaning_process /
cleaning_process_step (no new tables).

Part 1 — Cleaning Procedures (Table 2)
    One template cleaning_process per procedure code (A, B, ..., EE, LL):
        cargo_id = from_cargo_id = to_cargo_id = NULL, procedure_code = <code>.
    One cleaning_process_step per numbered instruction, reusing the existing
    columns: method (action), description (full sentence), medium, temperature,
    duration, cleaner (+ mandatory). Procedure NOTE -> cleaning_process.notes.

Part 2 — FROM -> TO Cleaning Matrix (chemical_to_chemical)
    For every matrix cell "previous cargo -> next cargo = code":
        * resolve both cargoes by canonical_name (case/space-insensitive),
        * create a cleaning_process with from_cargo_id / to_cargo_id / code,
        * copy the template's steps into it (every pair gets its own steps).

Idempotent: templates keyed by (procedure_code, source_id); matrix rows keyed by
(from_cargo_id, to_cargo_id, source_id) via partial UNIQUE indexes. Re-running
updates in place and never duplicates. All work runs in one transaction.

Usage:
    python3 etl/verwey_cleaning.py                 # procedures + matrix
    python3 etl/verwey_cleaning.py --no-matrix     # Part 1 only
    python3 etl/verwey_cleaning.py --dry-run
    python3 etl/verwey_cleaning.py --limit 500     # cap matrix pairs (testing)
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
# Reuse the (already-tested) Table-2 parser and field extractors.
from proceduretemplate import (
    parse_file, resolve_source,
    extract_step_name, extract_medium, extract_temperature,
    extract_duration, extract_cleaner,
)

PROCEDURES_FILE = input_file("Dr. Verwey’s Tank Cleaning Table 4.xlsx - CLEANING PROCEDURES (T-2).csv")
MATRIX_FILE = input_file("Dr. Verwey’s Tank Cleaning Table 4.xlsx - CLEANING chemical_to_chemical.csv")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("verwey_cleaning")

CODE_RE = re.compile(r"^[A-Z]{1,2}$")


# ---------------------------------------------------------------------------
# Part 1: cleaning procedures -> template cleaning_process rows
# ---------------------------------------------------------------------------
def load_procedures(cur, path: Path, source_id: int, dry_run: bool) -> Tuple[Dict[str, int], int, int]:
    """Upsert one template cleaning_process per code and replace its steps.

    Returns (code -> template_id, n_processes, n_steps).
    """
    procedures = parse_file(path)
    log.info("Parsed %d procedures from %s", len(procedures), path.name)

    code_to_id: Dict[str, int] = {}
    n_proc = n_steps = 0
    for code, full_text, steps, notes in procedures:
        if dry_run:
            code_to_id[code] = -1
            n_proc += 1
            n_steps += len(steps)
            continue

        cur.execute(
            """
            INSERT INTO cleaning_process (procedure_code, title, source_id, notes, created_at, updated_at)
            VALUES (%s, %s, %s, %s, now(), now())
            ON CONFLICT (procedure_code, source_id)
                WHERE cargo_id IS NULL AND from_cargo_id IS NULL AND to_cargo_id IS NULL
                  AND from_cargo_family_group_id IS NULL
                  AND to_cargo_family_group_id IS NULL
            DO UPDATE SET title = EXCLUDED.title, notes = EXCLUDED.notes, updated_at = now()
            RETURNING id
            """,
            (code, f"Procedure {code}", source_id, notes),
        )
        tpl_id = cur.fetchone()[0]
        code_to_id[code] = tpl_id

        cur.execute("DELETE FROM cleaning_process_step WHERE cleaning_process_id=%s", (tpl_id,))
        rows = []
        for i, step in enumerate(steps, start=1):
            rows.append((
                tpl_id, i,
                extract_step_name(step),           # method  (step action)
                extract_medium(step),
                extract_temperature(step),
                extract_duration(step),
                extract_cleaner(step),
                step.rstrip(" ;:,."),              # description (full sentence)
                True,                              # mandatory
            ))
        if rows:
            execute_values(
                cur,
                """
                INSERT INTO cleaning_process_step
                    (cleaning_process_id, step_order, method, medium, temperature,
                     duration, cleaner, description, mandatory, created_at, updated_at)
                VALUES %s
                """,
                rows,
                template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),now())",
            )
        n_proc += 1
        n_steps += len(rows)
    return code_to_id, n_proc, n_steps


# ---------------------------------------------------------------------------
# Part 2: FROM -> TO cleaning matrix
# ---------------------------------------------------------------------------
def _norm(name: str) -> str:
    """Canonical match key: lowercase, drop everything but letters/digits.

    The matrix strips spaces from names (e.g. "ACETICANHYDRIDE"), so a plain
    whitespace-collapse won't match "Acetic Anhydride"; alphanumeric-only does.
    """
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _candidate_keys(name: str) -> List[str]:
    """Normalized keys to try for a matrix name, best first.

    The matrix writes isomer/locant markers as a trailing parenthetical
    (e.g. "AMYLACETATE(N-)", "AMINO2METHYL1PROPANOL(2-)", "BUTYLENEOXIDE(1,2)"),
    whereas canonical names carry them up front ("n-Amyl acetate",
    "2-Amino-2-methyl-1-propanol", "1,2-Butylene oxide"). So besides the literal
    form we also try the marker moved to the front, then the bare core.
    """
    keys = [_norm(name)]
    m = re.match(r"^(.*?)\(([^)]*)\)\s*$", name)
    if m:
        core = m.group(1)
        marker = m.group(2).strip().rstrip("-").strip()   # "N-" -> "N", "1,2" -> "1,2"
        if marker:
            keys.append(_norm(marker + core))             # -> "namylacetate"
        keys.append(_norm(core))                          # bare core, last resort
    seen, out = set(), []
    for k in keys:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


class CargoResolver:
    """Resolve a cargo name to a cargo_id via canonical_name then synonyms.

    Matching is case/space/punctuation-insensitive (alphanumeric only) and
    isomer-aware (see _candidate_keys). Canonical matches win over synonyms; the
    lowest cargo_id wins when a name maps to several source copies.
    """

    def __init__(self, cur):
        cur.execute("SELECT id, canonical_name FROM cargo_chemical")
        self.canonical: Dict[str, int] = {}
        for cid, name in cur.fetchall():
            self._add(self.canonical, _norm(name or ""), cid)

        cur.execute(
            "SELECT cs.cargo_id, s.synonym_text "
            "FROM cargo_synonym cs JOIN synonyms s ON s.id = cs.synonym_id"
        )
        self.synonyms: Dict[str, int] = {}
        for cid, text in cur.fetchall():
            self._add(self.synonyms, _norm(text or ""), cid)

    @staticmethod
    def _add(table: Dict[str, int], key: str, cid: int) -> None:
        if key and (key not in table or cid < table[key]):
            table[key] = cid

    def resolve(self, name: str) -> Optional[int]:
        keys = _candidate_keys(name)
        for k in keys:                       # prefer canonical names
            if k in self.canonical:
                return self.canonical[k]
        for k in keys:                       # then fall back to synonyms
            if k in self.synonyms:
                return self.synonyms[k]
        return None


def parse_matrix(path: Path) -> List[Tuple[str, str, str]]:
    """Return [(from_name, to_name, code), ...] for every coded cell."""
    with open(path, encoding="utf-8", errors="replace", newline="") as fh:
        rows = list(csv.reader(fh))
    if not rows:
        return []
    header = rows[0]
    # columns >= 3 hold a TO-cargo index (matches a row's index in column 0)
    to_cols = [(j, header[j].strip()) for j in range(3, len(header)) if header[j].strip().isdigit()]

    idx_name: Dict[str, str] = {}
    data_rows = []
    for r in rows[1:]:
        if len(r) < 2 or not r[0].strip().isdigit():
            continue
        idx, name = r[0].strip(), r[1].strip()
        if name:
            idx_name[idx] = name
        data_rows.append(r)

    cells: List[Tuple[str, str, str]] = []
    for r in data_rows:
        from_name = idx_name.get(r[0].strip())
        if not from_name:
            continue
        for j, to_idx in to_cols:
            if j >= len(r):
                continue
            code = r[j].strip()
            if not CODE_RE.match(code):
                continue
            to_name = idx_name.get(to_idx)
            if to_name:
                cells.append((from_name, to_name, code))
    return cells


def load_matrix(cur, path: Path, source_id: int, valid_codes: set,
                dry_run: bool, limit: Optional[int]) -> Tuple[int, int, int]:
    """Create one cleaning_process per resolved cargo pair and copy template steps.

    Returns (n_pairs, n_steps_copied, n_unmatched_names).
    """
    cells = parse_matrix(path)
    log.info("Matrix: %d coded cells", len(cells))
    resolver = CargoResolver(cur)

    # Dedupe by resolved (from_id, to_id): synonym/isomer matching can map several
    # distinct matrix names onto the same cargo pair, and a single INSERT ... ON
    # CONFLICT batch may not touch the same row twice. First cell wins.
    pair_map: Dict[Tuple[int, int], Tuple[str, str]] = {}
    unmatched: set = set()
    skipped_no_template = skipped_unmatched = skipped_self = skipped_dup = 0

    for from_name, to_name, code in cells:
        if code not in valid_codes:
            skipped_no_template += 1
            continue
        fid = resolver.resolve(from_name)
        tid = resolver.resolve(to_name)
        if fid is None:
            unmatched.add(from_name)
        if tid is None:
            unmatched.add(to_name)
        if fid is None or tid is None:
            skipped_unmatched += 1
            continue
        if fid == tid:
            skipped_self += 1
            continue
        key = (fid, tid)
        if key in pair_map:
            skipped_dup += 1
            continue
        pair_map[key] = (code, f"{from_name} → {to_name} (Procedure {code})")

    pairs: List[Tuple[int, int, str, str]] = [
        (fid, tid, code, title) for (fid, tid), (code, title) in pair_map.items()
    ]

    if unmatched:
        sample = sorted(unmatched)[:30]
        log.warning("Unmatched cargo names: %d (not in cargo_chemical). Sample: %s",
                    len(unmatched), sample)
    log.info("Matrix pairs: resolved=%d | skipped: no-template=%d, unmatched=%d, self=%d, dup-pair=%d",
             len(pairs), skipped_no_template, skipped_unmatched, skipped_self, skipped_dup)

    if limit is not None:
        pairs = pairs[:limit]
        log.info("Applying --limit: %d pairs", len(pairs))

    if dry_run or not pairs:
        return len(pairs), 0, len(unmatched)

    # 1) upsert the pair processes (idempotent via partial unique index)
    execute_values(
        cur,
        """
        INSERT INTO cleaning_process
            (from_cargo_id, to_cargo_id, procedure_code, source_id, title, created_at, updated_at)
        VALUES %s
        ON CONFLICT (from_cargo_id, to_cargo_id, source_id, (COALESCE(condition, '')))
            WHERE from_cargo_id IS NOT NULL AND to_cargo_id IS NOT NULL
        DO UPDATE SET procedure_code = EXCLUDED.procedure_code,
                      title = EXCLUDED.title, updated_at = now()
        """,
        [(fid, tid, code, source_id, title) for fid, tid, code, title in pairs],
        template="(%s,%s,%s,%s,%s,now(),now())",
        page_size=2000,
    )

    # 1b) point every row at its procedure_templates row. Until the CargoType
    # refactor this link was implicit in the (source_id, procedure_code)
    # composite FK; that FK is gone now that cleaning_process is polymorphic,
    # so the id has to be resolved explicitly. Idempotent.
    cur.execute(
        """
        UPDATE cleaning_process cp
           SET procedure_template_id = pt.id
          FROM procedure_templates pt
         WHERE pt.source_id = cp.source_id
           AND pt.procedure_code = cp.procedure_code
           AND cp.source_id = %s
           AND cp.procedure_code IS NOT NULL
           AND cp.procedure_template_id IS DISTINCT FROM pt.id
        """,
        (source_id,),
    )

    # 2) copy template steps into every matrix process (server-side, idempotent)
    cur.execute(
        """
        DELETE FROM cleaning_process_step
         WHERE cleaning_process_id IN (
            SELECT id FROM cleaning_process
             WHERE source_id = %s AND from_cargo_id IS NOT NULL
         )
        """,
        (source_id,),
    )
    cur.execute(
        """
        INSERT INTO cleaning_process_step
            (cleaning_process_id, step_order, method, medium, temperature, duration,
             cleaner, description, remarks, mandatory, created_at, updated_at)
        SELECT mp.id, ts.step_order, ts.method, ts.medium, ts.temperature, ts.duration,
               ts.cleaner, ts.description, ts.remarks, ts.mandatory, now(), now()
          FROM cleaning_process mp
          JOIN cleaning_process tpl
            ON tpl.procedure_code = mp.procedure_code
           AND tpl.source_id = mp.source_id
           AND tpl.cargo_id IS NULL AND tpl.from_cargo_id IS NULL AND tpl.to_cargo_id IS NULL
           AND tpl.from_cargo_family_group_id IS NULL
           AND tpl.to_cargo_family_group_id IS NULL
          JOIN cleaning_process_step ts ON ts.cleaning_process_id = tpl.id
         WHERE mp.source_id = %s AND mp.from_cargo_id IS NOT NULL
        """,
        (source_id,),
    )
    n_steps_copied = cur.rowcount
    return len(pairs), n_steps_copied, len(unmatched)


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Import Dr. Verwey cleaning procedures + matrix.")
    ap.add_argument("--procedures", default=str(PROCEDURES_FILE), help="Table 2 CSV")
    ap.add_argument("--matrix", default=str(MATRIX_FILE), help="chemical_to_chemical CSV")
    ap.add_argument("--source-id", type=int, default=None, help="force a source id")
    ap.add_argument("--no-matrix", action="store_true", help="import procedures only (Part 1)")
    ap.add_argument("--limit", type=int, default=None, help="cap matrix pairs (testing)")
    ap.add_argument("--dry-run", action="store_true", help="parse + report, write nothing")
    args = ap.parse_args()

    load_dotenv(Path(__file__).parent.parent / ".env")

    proc_path = Path(args.procedures)
    matrix_path = Path(args.matrix)
    if not proc_path.exists():
        sys.exit(f"Procedures file not found: {proc_path}")
    if not args.no_matrix and not matrix_path.exists():
        sys.exit(f"Matrix file not found: {matrix_path}")

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            source_id = resolve_source(cur, args.source_id, args.dry_run)
            log.info("source_id=%s", source_id)

            code_to_id, n_proc, n_steps = load_procedures(cur, proc_path, source_id, args.dry_run)
            valid_codes = set(code_to_id)

            n_pairs = n_matrix_steps = n_unmatched = 0
            if not args.no_matrix:
                n_pairs, n_matrix_steps, n_unmatched = load_matrix(
                    cur, matrix_path, source_id, valid_codes, args.dry_run, args.limit)

            log.info("=" * 64)
            log.info("SUMMARY (%s)", "DRY-RUN" if args.dry_run else "COMMIT")
            log.info("  template processes / steps : %d / %d", n_proc, n_steps)
            if not args.no_matrix:
                log.info("  matrix processes           : %d", n_pairs)
                log.info("  matrix steps copied        : %d", n_matrix_steps)
                log.info("  unmatched cargo names      : %d", n_unmatched)
            log.info("=" * 64)

            if args.dry_run:
                conn.rollback()
                log.info("Dry run: nothing written.")
                return
            conn.commit()
            log.info("✓ Committed.")
    except Exception:
        conn.rollback()
        log.exception("Import failed - rolled back")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
