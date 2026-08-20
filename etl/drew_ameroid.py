#!/usr/bin/env python3
"""
Import the Drew Ameroid Tank Cleaning Guide.

Corrected, normalized design (steps stored ONCE):
  1. "Cleaning Procedures" sheet -> procedure_templates  (one per code, source-scoped)
       and                        -> procedure_template_steps (its numbered steps)
  2. "Cross-Reference Matrix"    -> cleaning_process        (one per FROM->TO pair,
       only the RELATION: from_cargo_id / to_cargo_id / procedure_code + source_id).
       NO steps are copied into cleaning_process_step — the steps live in
       procedure_template_steps and are reached via the (source_id, procedure_code)
       link to procedure_templates.

  "Product Index" sheet maps the matrix's numeric indices to cargo names. The "GF"
  (gas-free certification) row is a state, not a product, so it is skipped. Names
  resolve to cargo_chemical by canonical_name then synonyms (CargoResolver).

Idempotent: procedure_templates keyed by (source_id, procedure_code); matrix rows
keyed by (from_cargo_id, to_cargo_id, source_id) via the existing partial UNIQUE
index. Re-running upserts in place.

Usage:
    python3 etl/drew_ameroid.py                 # procedures + matrix
    python3 etl/drew_ameroid.py --no-matrix     # procedures only
    python3 etl/drew_ameroid.py --dry-run
    python3 etl/drew_ameroid.py --file "/path/Drew Ameroid Tank Cleaning Guide.xlsx"
"""

import argparse
import logging
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

from _paths import input_file
# Reuse the tested field extractors + the cargo resolver.
from proceduretemplate import (
    NOTE_RE, extract_step_name, extract_medium, extract_temperature,
    extract_duration, extract_cleaner,
)
from verwey_cleaning import CargoResolver
from cargo_chemicals import get_source_id, get_source_id_partial, create_source

DEFAULT_FILE = str(input_file("Drew Ameroid Tank Cleaning Guide.xlsx"))
SOURCE_NAME = "Drew Ameroid Tank Cleaning Guide (TCG)"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("drew_ameroid")

CODE_RE = re.compile(r"^[A-Z]{1,2}$")
# Drew numbers steps as "1/." (also tolerate "1." / "1)").
STEP_RE = re.compile(r"^\s*(\d+)\s*(?:/\.|\.|\))\s*(.*)$")


# ---------------------------------------------------------------------------
# Parsing (the three sheets)
# ---------------------------------------------------------------------------
def _cell(v) -> str:
    return "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v)


def parse_steps(text: str) -> Tuple[List[str], Optional[str]]:
    """Split one procedure cell into (ordered step texts, NOTE text)."""
    steps: List[str] = []
    note_lines: List[str] = []
    in_note = False
    current: Optional[str] = None
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if NOTE_RE.match(line):
            in_note = True
            rest = re.sub(r"^\s*NOTE\s*:?\s*", "", line, flags=re.I).strip()
            if rest:
                note_lines.append(rest)
            continue
        if in_note:
            note_lines.append(line)
            continue
        m = STEP_RE.match(line)
        if m:
            if current is not None:
                steps.append(current)
            current = m.group(2).strip()
        elif current is not None:
            current = f"{current} {line}".strip()   # wrapped continuation line
    if current is not None:
        steps.append(current)
    steps = [s for s in (st.strip() for st in steps) if s]
    return steps, (" ".join(note_lines).strip() or None)


def parse_procedures(df: pd.DataFrame) -> List[Tuple[str, str, List[str], Optional[str]]]:
    """Sheet 'Cleaning Procedures' -> [(code, full_text, [step,...], notes), ...]."""
    out = []
    for _, row in df.iterrows():
        code = _cell(row[0]).strip()
        if not CODE_RE.match(code):
            continue
        cell = _cell(row[1]) if len(row) > 1 else ""
        if not cell.strip():
            continue
        steps, notes = parse_steps(cell)
        if not steps:                       # advisory-only: keep text in notes
            notes = notes or cell.strip()
            log.info("Procedure %s has no numbered steps (advisory only)", code)
        out.append((code, cell.strip(), steps, notes))
    return out


def parse_product_index(df: pd.DataFrame) -> Dict[str, str]:
    """Sheet 'Product Index' -> {index: cargo_name}."""
    idx_name: Dict[str, str] = {}
    for _, row in df.iterrows():
        num = _cell(row[0]).strip()
        name = _cell(row[1]).strip() if len(row) > 1 else ""
        if num.isdigit() and name:
            idx_name[num] = name
    return idx_name


def parse_matrix(df: pd.DataFrame, idx_name: Dict[str, str]) -> List[Tuple[str, str, str]]:
    """Sheet 'Cross-Reference Matrix' -> [(from_name, to_name, code), ...].

    Row with 'FROM \\ TO' in col 1 is the header; its columns >=2 carry a TO index
    (matches Product Index). Each later row: col0 = FROM index ('GF' row skipped),
    col1 = FROM name, cols >=2 = the procedure code for that TO cargo.
    """
    rows = [[_cell(v) for v in df.iloc[i].tolist()] for i in range(len(df))]
    hdr = next((i for i, r in enumerate(rows)
                if len(r) > 1 and r[1].strip().upper().startswith("FROM")), None)
    if hdr is None:
        return []
    header = rows[hdr]
    to_cols = [(j, header[j].strip()) for j in range(2, len(header)) if header[j].strip().isdigit()]

    cells: List[Tuple[str, str, str]] = []
    for r in rows[hdr + 1:]:
        if len(r) < 2 or not r[0].strip().isdigit():   # skip 'GF' and blanks
            continue
        from_name = r[1].strip() or idx_name.get(r[0].strip(), "")
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


# ---------------------------------------------------------------------------
# Load — step 1: procedure_templates (+ steps)
# ---------------------------------------------------------------------------
def load_procedure_templates(cur, procedures, source_id: int, dry_run: bool
                             ) -> Tuple[set, int, int]:
    """Upsert one procedure_templates row per code; replace its steps.

    Returns (codes, n_templates, n_steps).
    """
    codes = set()
    n_tpl = n_steps = 0
    for code, full_text, steps, notes in procedures:
        codes.add(code)
        if dry_run:
            n_tpl += 1
            n_steps += len(steps)
            continue
        cur.execute(
            """
            INSERT INTO procedure_templates
                (procedure_code, template_name, description, source_id, notes, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, now(), now())
            ON CONFLICT (source_id, procedure_code) DO UPDATE SET
                template_name = EXCLUDED.template_name,
                description   = EXCLUDED.description,
                notes         = EXCLUDED.notes,
                updated_at    = now()
            RETURNING id
            """,
            (code, f"Procedure {code}", full_text, source_id, notes),
        )
        tpl_id = cur.fetchone()[0]
        cur.execute("DELETE FROM procedure_template_steps WHERE procedure_templates_id=%s", (tpl_id,))
        rows = [
            (tpl_id, i, extract_step_name(s), s.rstrip(" ;:,."),
             extract_medium(s), extract_temperature(s), extract_duration(s),
             extract_cleaner(s), True, None)
            for i, s in enumerate(steps, start=1)
        ]
        if rows:
            execute_values(
                cur,
                """
                INSERT INTO procedure_template_steps
                    (procedure_templates_id, step_order, step_name, step_description,
                     medium, temperature, duration, cleaner, mandatory, notes,
                     created_at, updated_at)
                VALUES %s
                """,
                rows,
                template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),now())",
            )
        n_tpl += 1
        n_steps += len(rows)
    return codes, n_tpl, n_steps


# ---------------------------------------------------------------------------
# Load — step 2: cleaning_process (matrix pairs = RELATION ONLY, no steps)
# ---------------------------------------------------------------------------
def load_matrix(cur, cells, source_id: int, valid_codes: set, dry_run: bool
                ) -> Tuple[int, int]:
    """Create one cleaning_process per resolved FROM->TO pair. No steps are copied;
    the pair links to procedure_templates via (source_id, procedure_code).

    Returns (n_pairs, n_unmatched_names).
    """
    resolver = CargoResolver(cur)
    pair_map: Dict[Tuple[int, int], Tuple[str, str]] = {}
    unmatched: set = set()
    skipped_no_template = skipped_unmatched = skipped_self = skipped_dup = 0

    for from_name, to_name, code in cells:
        if code not in valid_codes:
            skipped_no_template += 1
            continue
        fid, tid = resolver.resolve(from_name), resolver.resolve(to_name)
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

    pairs = [(fid, tid, code, title) for (fid, tid), (code, title) in pair_map.items()]

    if unmatched:
        log.warning("Unmatched cargo names: %d. Sample: %s",
                    len(unmatched), sorted(unmatched)[:30])
    log.info("Matrix pairs: resolved=%d | skipped: no-template=%d, unmatched=%d, self=%d, dup=%d",
             len(pairs), skipped_no_template, skipped_unmatched, skipped_self, skipped_dup)

    if dry_run or not pairs:
        return len(pairs), len(unmatched)

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
    return len(pairs), len(unmatched)


def resolve_source(cur, forced: Optional[int], dry_run: bool) -> int:
    if forced is not None:
        cur.execute("SELECT id FROM source WHERE id=%s", (forced,))
        if cur.fetchone() is None:
            sys.exit(f"Error: --source-id {forced} not found.")
        return forced
    sid = get_source_id(cur, SOURCE_NAME) or get_source_id_partial(cur, SOURCE_NAME)
    if sid is None:
        cur.execute("SELECT id FROM source WHERE name ILIKE '%drew%ameroid%' ORDER BY id LIMIT 1")
        row = cur.fetchone()
        sid = row[0] if row else None
    if sid is None:
        if dry_run:
            return -1
        sid = create_source(cur, SOURCE_NAME)
        log.info("Created source '%s' id=%s", SOURCE_NAME, sid)
    return sid


def main():
    ap = argparse.ArgumentParser(description="Import Drew Ameroid Tank Cleaning Guide.")
    ap.add_argument("--file", default=DEFAULT_FILE, help="the .xlsx path")
    ap.add_argument("--source-id", type=int, default=None, help="force a source id")
    ap.add_argument("--no-matrix", action="store_true", help="procedures only")
    ap.add_argument("--dry-run", action="store_true", help="parse + report, write nothing")
    args = ap.parse_args()

    load_dotenv(Path(__file__).parent.parent / ".env")
    path = Path(args.file)
    if not path.exists():
        sys.exit(f"File not found: {path}")

    df_proc = pd.read_excel(path, "Cleaning Procedures", header=None, dtype=str, keep_default_na=False)
    df_idx = pd.read_excel(path, "Product Index", header=None, dtype=str, keep_default_na=False)
    df_mat = pd.read_excel(path, "Cross-Reference Matrix", header=None, dtype=str, keep_default_na=False)

    procedures = parse_procedures(df_proc)
    idx_name = parse_product_index(df_idx)
    cells = parse_matrix(df_mat, idx_name) if not args.no_matrix else []
    log.info("Parsed: %d procedures, %d products, %d matrix cells",
             len(procedures), len(idx_name), len(cells))

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            source_id = resolve_source(cur, args.source_id, args.dry_run)
            log.info("source_id=%s (%s)", source_id, SOURCE_NAME)

            # step 1: procedure_templates (+ steps)
            codes, n_tpl, n_steps = load_procedure_templates(cur, procedures, source_id, args.dry_run)

            # step 2: cleaning_process matrix pairs (relation only, no steps)
            n_pairs = n_unmatched = 0
            if not args.no_matrix:
                n_pairs, n_unmatched = load_matrix(cur, cells, source_id, codes, args.dry_run)

            log.info("=" * 64)
            log.info("SUMMARY (%s)", "DRY-RUN" if args.dry_run else "COMMIT")
            log.info("  procedure_templates        : %d", n_tpl)
            log.info("  procedure_template_steps   : %d", n_steps)
            if not args.no_matrix:
                log.info("  cleaning_process (pairs)   : %d", n_pairs)
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
