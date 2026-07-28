#!/usr/bin/env python3
"""
Load the Dr. Verwey Tank Cleaning Guide — Table 2 (Cleaning Procedures List)
into procedure_templates + procedure_template_steps.

Source file (2 columns): column 0 = procedure code (A, AA, B, ...), column 1 =
the full procedure text whose numbered lines ("1.", "2.", ...) are the steps.

Mapping:
  * one procedure_templates row per code
      - procedure_code : the official code (A, B, C, D, EE, LL, ...)
      - description     : the FULL procedure text, verbatim (nothing is lost)
      - notes          : any NOTE: block
  * one procedure_template_steps row per numbered instruction
      - step_description : the instruction verbatim
      - step_name / medium / temperature / duration / cleaner : best-effort
        extraction of the free-text values as published (e.g. "Cold", "80°C",
        "About 2½ hours", "Teepol 0.05%")

Idempotent: procedure_code is UNIQUE, so re-running upserts the template and
replaces its steps.

Usage:
    python3 etl/proceduretemplate.py                 # default file, upsert
    python3 etl/proceduretemplate.py path/to.csv
    python3 etl/proceduretemplate.py --dry-run
    python3 etl/proceduretemplate.py --source-id 7
"""

import argparse
import csv
import logging
import os
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

from _paths import input_file
from cargo_chemicals import get_source_id, create_source

DEFAULT_FILE = input_file("Dr. Verwey’s Tank Cleaning Table 4.xlsx - CLEANING PROCEDURES (T-2).csv")
SOURCE_NAME = "Dr Verweys Tank Cleaning Guide"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("proceduretemplate")

# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------
CODE_RE = re.compile(r"^[A-Z]{1,2}$")            # A, AA, B, ... in column 0
STEP_RE = re.compile(r"^\s*(\d+)\.\s*(.*)$")      # "1. Butterworthing ..."
NOTE_RE = re.compile(r"^\s*NOTE\b", re.I)
LEAD_CODE_RE = re.compile(r"^\s*[A-Z]{1,2}\s*[-–]\s*")  # strip a "LL - " prefix

# Cleaning media -> canonical label. Order matters (specific before generic).
MEDIA = [
    ("dichloromethane", "Dichloromethane"),
    ("toluene", "Toluene"),
    ("methanol", "Methanol"),
    ("ethanol", "Ethanol"),
    ("gasoil", "Dry Gas Oil"), ("gas oil", "Dry Gas Oil"),
    ("nitrogen", "Nitrogen"),
    ("seawater", "Sea Water"), ("sea water", "Sea Water"),
    ("freshwater", "Fresh Water"), ("fresh water", "Fresh Water"),
    ("livestream", "Steam"), ("steam", "Steam"),
    ("air", "Air"),
    ("water", "Water"),   # generic fallback, last
]

# Leading action words that name a step.
ACTIONS = [
    "butterworthing", "spraying", "flushing", "steaming", "draining", "drying",
    "stripping", "bottomwashing", "washing", "rinsing", "ventilating",
    "gasfreeing", "gas-freeing", "filling", "emptying", "boiling", "circulating",
    "neutralizing", "neutralising", "recirculating", "prewash", "pre-wash",
    "cleaning", "mopping", "blowing",
]

TEMP_WORDS = [
    ("luke warm", "Luke warm"), ("lukewarm", "Luke warm"),
    ("cold", "Cold"), ("hot", "Hot"), ("warm", "Warm"),
    ("ambient", "Ambient"), ("boiling", "Boiling"),
]
DEG_RE = re.compile(r"(\d+(?:\s*[-–]\s*\d+)?)\s*°\s*[cC]")


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def extract_temperature(step: str) -> Optional[str]:
    """Explicit "NN °C" (range aware) wins; otherwise a qualitative word."""
    m = DEG_RE.search(step)
    if m:
        val = re.sub(r"\s*[-–]\s*", "-", m.group(1).strip())
        return f"{val}°C"
    low = step.lower()
    for word, canon in TEMP_WORDS:
        if word in low:
            return canon
    return None


def extract_medium(step: str) -> Optional[str]:
    low = step.lower()
    for term, canon in MEDIA:
        if term in low:
            return canon
    return None


def extract_duration(step: str) -> Optional[str]:
    low = step.lower()
    m = re.search(r"for\s+(about\s+)?([^;:,.\n]*?hours?)", low)
    if m:
        phrase = ((m.group(1) or "") + m.group(2))
        return _cap(re.sub(r"\s+", " ", phrase).strip())
    m = re.search(r"until\s+[^;:,.\n]+", low)
    if m:
        return _cap(m.group(0).strip())
    if "without interruption" in low:
        return "Without interruption"
    m = re.search(r"(?:\d+\s*)?[½¼¾]?\s*hours?", low)  # standalone "½ hour"
    if m and re.search(r"[½¼¾\d]", m.group(0)):
        return _cap(re.sub(r"\s+", " ", m.group(0)).strip())
    return None


def extract_cleaner(step: str) -> Optional[str]:
    """"0.05% liquid detergent (Teepol)" -> "Teepol 0.05%"."""
    m = re.search(r"([\d.]+)\s*%\s*(?:liquid\s+)?detergent(?:\s*\(([^)]+)\))?", step, re.I)
    if not m:
        return None
    pct = f"{m.group(1)}%"
    name = (m.group(2) or "").strip()
    return f"{name} {pct}" if name else f"{pct} detergent"


def extract_step_name(step: str) -> Optional[str]:
    low = step.lower()
    best, best_pos = None, len(low) + 1
    for act in ACTIONS:
        pos = low.find(act)
        if pos != -1 and pos < best_pos:
            best, best_pos = act, pos
    if best:
        return _cap(best)
    m = re.search(r"[A-Za-z][A-Za-z-]+", step)   # fall back to first word
    return _cap(m.group(0)) if m else None


def parse_steps(cell: str) -> Tuple[List[str], Optional[str]]:
    """Split one procedure cell into (ordered step texts, NOTE text)."""
    text = LEAD_CODE_RE.sub("", cell.strip(), count=1)   # drop a "LL - " prefix
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
    notes = " ".join(note_lines).strip() or None
    return steps, notes


def parse_file(path: Path) -> List[Tuple[str, str, List[str], Optional[str]]]:
    """Return [(code, full_text, [step, ...], notes), ...]."""
    out = []
    with open(path, encoding="utf-8", errors="replace", newline="") as fh:
        for row in csv.reader(fh):
            if not row:
                continue
            code = (row[0] or "").strip()
            if not CODE_RE.match(code):
                continue
            cell = row[1] if len(row) > 1 else ""
            if not cell.strip():
                continue
            steps, notes = parse_steps(cell)
            if not steps:
                # Advisory-only procedures (e.g. DD, OO) have no numbered steps;
                # keep the template so every code is stored, text preserved in
                # `description`, and put the prose in `notes`.
                notes = notes or cell.strip()
                log.info("Procedure %s has no numbered steps (advisory only)", code)
            out.append((code, cell.strip(), steps, notes))
    return out


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------
def resolve_source(cur, forced: Optional[int], dry_run: bool) -> int:
    if forced is not None:
        cur.execute("SELECT id FROM source WHERE id=%s", (forced,))
        if cur.fetchone() is None:
            sys.exit(f"Error: --source-id {forced} not found in source table.")
        return forced
    sid = get_source_id(cur, SOURCE_NAME)
    if sid is None:
        cur.execute("SELECT id FROM source WHERE name ILIKE '%verwey%' ORDER BY id LIMIT 1")
        row = cur.fetchone()
        sid = row[0] if row else None
    if sid is None:
        if dry_run:
            return -1
        sid = create_source(cur, SOURCE_NAME)
        log.info("Created source '%s' id=%s", SOURCE_NAME, sid)
    return sid


def main():
    ap = argparse.ArgumentParser(description="Load Dr. Verwey Table 2 cleaning procedures.")
    ap.add_argument("file", nargs="?", default=str(DEFAULT_FILE), help="CSV path")
    ap.add_argument("--source-id", type=int, default=None, help="force a source id")
    ap.add_argument("--dry-run", action="store_true", help="parse + report, write nothing")
    args = ap.parse_args()

    load_dotenv(Path(__file__).parent.parent / ".env")

    path = Path(args.file)
    if not path.exists():
        sys.exit(f"File not found: {path}")

    procedures = parse_file(path)
    log.info("Parsed %d procedures from %s", len(procedures), path.name)

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            source_id = resolve_source(cur, args.source_id, args.dry_run)
            log.info("source_id=%s", source_id)

            n_tpl = n_steps = 0
            for code, full_text, steps, notes in procedures:
                if args.dry_run:
                    n_tpl += 1
                    n_steps += len(steps)
                    continue

                cur.execute(
                    """
                    INSERT INTO procedure_templates
                        (procedure_code, template_name, description, source_id, notes, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, now(), now())
                    ON CONFLICT (procedure_code) DO UPDATE SET
                        template_name = EXCLUDED.template_name,
                        description   = EXCLUDED.description,
                        notes         = EXCLUDED.notes,
                        source_id     = EXCLUDED.source_id,
                        updated_at    = now()
                    RETURNING id
                    """,
                    (code, f"Procedure {code}", full_text, source_id, notes),
                )
                tpl_id = cur.fetchone()[0]

                # replace steps (idempotent)
                cur.execute("DELETE FROM procedure_template_steps WHERE procedure_templates_id=%s", (tpl_id,))
                rows = []
                for i, step in enumerate(steps, start=1):
                    rows.append((
                        tpl_id, i,
                        extract_step_name(step),
                        step.rstrip(" ;:,."),
                        extract_medium(step),
                        extract_temperature(step),
                        extract_duration(step),
                        extract_cleaner(step),
                        True,          # mandatory
                        None,          # notes
                    ))
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

            log.info("=" * 60)
            log.info("SUMMARY (%s)", "DRY-RUN" if args.dry_run else "COMMIT")
            log.info("  procedure_templates     : %d", n_tpl)
            log.info("  procedure_template_steps: %d", n_steps)
            log.info("=" * 60)

            if args.dry_run:
                conn.rollback()
                log.info("Dry run: nothing written.")
                return
            conn.commit()
            log.info("✓ Committed.")
    except Exception:
        conn.rollback()
        log.exception("Load failed - rolled back")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
