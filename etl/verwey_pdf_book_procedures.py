#!/usr/bin/env python3
"""
Load the Dr Verwey Tank Cleaning Guide (PDF Book edition) — Procedure Template
sheet into procedure_templates + procedure_template_steps +
procedure_template_instruction.

Source columns: Procedure Code, Procedure, Note.

The Procedure cell holds up to three different kinds of text:

    Iso-cyanates react with water to form polyurea and CO2. ...      <- preamble
    All equipment used must be dry.                                     (instruction)

    1. Recirculation in ambient temperature mec/per for 1 hour       <- numbered step
    2. Butterworth with ambient temperature sea water for 1,5 hours
       - Solvent cleaner (hydrocarbon free)                          <- cleaner bullet
       (Follow the manufacturer's usage instructions ...)            <- step remark

    6. Cargo to be loaded requires an ultra-high cleanliness standard <- instruction
                                                                        (see below)

Routing:
  * numbered lines that are tank actions -> procedure_template_steps
  * the preamble, the Note column, and the trailing "Cargo to be loaded
    requires ..." sentence -> procedure_template_instruction, classified by
    severity.

Why the trailing sentence is an instruction and not a step: it is the only
difference between `X`, `X+` and `X-`, and it states a requirement on the NEXT
cargo rather than an action to perform on this tank. Keeping it out of the step
list means `A` and `A+` share an identical, correct 2-step procedure.

15 codes have no numbered steps at all (HX, HY, TX, VB, VX, WX, X, YX, ZX and
their +/- variants) — they are pure advisory text. They still get a template
row, with every sentence stored as an instruction.

This is a DIFFERENT code system from source 8 (`Dr Verweys Tank Cleaning Guide`,
37 codes A/AA/B/BB/...). The two editions are kept apart by
UNIQUE(source_id, procedure_code); nothing here touches source 8.

Idempotent: templates keyed by (source_id, procedure_code); steps and
instructions are rebuilt per template (delete-then-insert) so a re-run cannot
accumulate duplicates.

Usage:
    python3 etl/verwey_pdf_book_procedures.py
    python3 etl/verwey_pdf_book_procedures.py --dry-run
    python3 etl/verwey_pdf_book_procedures.py path/to/file.csv
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
from cargo_chemicals import get_source_id, create_source

SOURCE_NAME = "Dr Verweys Tank Cleaning Guide Pdf Book"
DEFAULT_FILE = input_file("Dr Verweys Tank Cleaning Guide Pdf Book Procdure Template.csv")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("verwey_pdf_book_procedures")

# ---------------------------------------------------------------------------
# Line classification
# ---------------------------------------------------------------------------
STEP_RE = re.compile(r"^\s*(\d+)\s*\.\s*(.+)$")
BULLET_RE = re.compile(r"^\s*-\s*(.+)$")
PAREN_ONLY_RE = re.compile(r"^\s*\((.+)\)\s*$")

# The "+"/"-" tail sentence. Present as a numbered line in the source but it is
# a requirement on the next cargo, not a tank action - see module docstring.
TAIL_RE = re.compile(r"^\s*Cargo to be loaded requires\b", re.IGNORECASE)
# Same sentence, but printed mid-line after the last step (A+, A-).
INLINE_TAIL_RE = re.compile(r"\s+(\d+\s*\.\s*Cargo to be loaded requires\b)", re.IGNORECASE)

# Page furniture left behind by the PDF extraction (procedure OA only).
PDF_JUNK_RE = re.compile(r"^\s*(TOP|MENU|\d{1,4})\s*$")

# ---------------------------------------------------------------------------
# Step field extraction, tuned to this edition's phrasing
#   "Butterworth with sea water at 45 °C to 50 °C for 1,5 hours (remark)"
# ---------------------------------------------------------------------------
ACTIONS: List[Tuple[str, str, Optional[str]]] = [
    # match text (lowercase), canonical step_name, CleaningStepType
    ("optional recirculation", "Optional Recirculation", "CLEANING"),
    ("recirc./injection", "Recirc./Injection", "CLEANING"),
    ("recirculation", "Recirculation", "CLEANING"),
    ("butterworth", "Butterworth", "CLEANING"),
    ("rinse", "Rinse", "RINSING"),
    ("vent, mop and dry", "Vent, mop and dry", "DRYING"),
    ("gas free", "Gas free", "DRYING"),
    ("ventilation", "Ventilation", "DRYING"),
    ("drain", "Drain", "DRAINING"),
    ("purge", "Purge", "DRAINING"),
    ("fill", "Fill", "PRECLEANING"),
]

MEDIA = [
    ("sea water or fresh water", "Sea water or fresh water"),
    ("fresh water", "Fresh water"),
    ("sea water", "Sea water"),
    ("mec/per", "MEC/PER"),
    ("nitrogen", "Nitrogen"),
]

# "45 °C to 50 °C", "a maximum temperature of 50 °C", "40 °C"
TEMP_RANGE_RE = re.compile(r"(\d+)\s*°\s*C\s*to\s*(\d+)\s*°\s*C", re.IGNORECASE)
TEMP_MAX_RE = re.compile(r"maximum temperature of\s*(\d+)\s*°\s*C", re.IGNORECASE)
TEMP_ONE_RE = re.compile(r"(\d+)\s*°\s*C", re.IGNORECASE)
# "1,5 hours", "2,5 hours", "1 hour", "20 minutes"
DURATION_RE = re.compile(r"for\s+(\d+(?:[.,]\d+)?)\s*(hours?|minutes?)", re.IGNORECASE)
TRAILING_PAREN_RE = re.compile(r"\(([^()]*(?:\([^()]*\)[^()]*)*)\)\s*$")


def extract_temperature(text: str) -> Optional[str]:
    m = TEMP_RANGE_RE.search(text)
    if m:
        return f"{m.group(1)}-{m.group(2)}°C"
    m = TEMP_MAX_RE.search(text)
    if m:
        return f"max {m.group(1)}°C"
    m = TEMP_ONE_RE.search(text)
    if m:
        return f"{m.group(1)}°C"
    if re.search(r"ambient temperature", text, re.IGNORECASE):
        return "Ambient"
    return None


def extract_medium(text: str) -> Optional[str]:
    low = text.lower()
    for term, canon in MEDIA:
        if term in low:
            return canon
    return None


def extract_duration(text: str) -> Optional[str]:
    m = DURATION_RE.search(text)
    if not m:
        return None
    return f"{m.group(1).replace(',', '.')} {m.group(2).lower()}"


def extract_step_name(text: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (step_name, CleaningStepType) for a step line."""
    low = text.lower()
    best: Optional[Tuple[int, str, Optional[str]]] = None
    for term, canon, kind in ACTIONS:
        pos = low.find(term)
        if pos != -1 and (best is None or pos < best[0]):
            best = (pos, canon, kind)
    if best:
        return best[1], best[2]
    m = re.match(r"\s*([A-Za-z][\w\-/.]*)", text)
    return (m.group(1).capitalize() if m else None), None


def duration_to_minutes(duration: Optional[str]) -> Optional[int]:
    """'1.5 hours' -> 90, '20 minutes' -> 20. Feeds default_duration_minutes."""
    if not duration:
        return None
    m = re.match(r"([\d.]+)\s*(hour|minute)", duration)
    if not m:
        return None
    value = float(m.group(1))
    return int(round(value * 60)) if m.group(2) == "hour" else int(round(value))


def temperature_to_c(temperature: Optional[str]) -> Optional[int]:
    """'45-50°C' -> 47 (midpoint), '80°C' -> 80, 'Ambient' -> None.

    Feeds default_temperature_c. 'Ambient' stays NULL because the book gives no
    number for it - inventing one would be asserting something it never said.
    """
    if not temperature:
        return None
    m = re.match(r"(\d+)-(\d+)°C", temperature)
    if m:
        return int(round((int(m.group(1)) + int(m.group(2))) / 2))
    m = re.search(r"(\d+)°C", temperature)
    return int(m.group(1)) if m else None


def split_remark(text: str) -> Tuple[str, Optional[str]]:
    """Peel a trailing parenthetical off a step line."""
    m = TRAILING_PAREN_RE.search(text)
    if not m:
        return text.strip(), None
    return text[: m.start()].strip(), m.group(1).strip()


# ---------------------------------------------------------------------------
# Instruction severity
# ---------------------------------------------------------------------------
SEVERITY_RULES: List[Tuple[str, re.Pattern]] = [
    ("DANGER", re.compile(
        r"violent|explosion|vacuum-collapse|react(s)? with water|"
        r"strongly exothermic|eliminate all ignition|don.?t use|!!!",
        re.IGNORECASE)),
    ("WARNING", re.compile(
        r"^caution|must be dry|immediately after|flush the tank immediately|"
        r"no ballast water|avoid any warming|should be avoided|"
        r"never start|liable to polymeri|!!",
        re.IGNORECASE)),
    ("CAUTION", re.compile(
        r"could result in the formation|might trigger|to avoid|"
        r"deactivate|white deposits|sticky|turn to water glass",
        re.IGNORECASE)),
    ("IMPORTANT", re.compile(
        r"not to be loaded|advisable not to load|check loading requirements|"
        r"should be verified|cargo to be loaded requires|recommended|"
        r"additional cleaning|wall wash standard",
        re.IGNORECASE)),
]


def classify(sentence: str) -> str:
    for level, pattern in SEVERITY_RULES:
        if pattern.search(sentence):
            return level
    return "INFO"


SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")


def split_sentences(text: str) -> List[str]:
    parts = [s.strip() for s in SENTENCE_SPLIT_RE.split(text.strip()) if s.strip()]
    return [p for p in parts if len(p) > 3]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
class Procedure:
    __slots__ = ("code", "full_text", "steps", "instructions")

    def __init__(self, code: str, full_text: str,
                 steps: List[dict], instructions: List[Tuple[str, str]]):
        self.code = code
        self.full_text = full_text
        self.steps = steps
        self.instructions = instructions


def parse_procedure(code: str, cell: str, note: str) -> Procedure:
    # In A+ / A- the tail sentence is printed on the same line as the last step
    # ("2. Vent, mop and dry 3. Cargo to be loaded requires ..."). Put it on its
    # own line so it is not swallowed into that step's description.
    cell = INLINE_TAIL_RE.sub(r"\n\1", cell)
    lines = [ln.rstrip() for ln in cell.split("\n")]

    preamble: List[str] = []
    tail: List[str] = []
    raw_steps: List[Tuple[str, str]] = []  # (source number, text)
    current: Optional[List[str]] = None
    current_no: Optional[str] = None
    seen_step = False

    def flush() -> None:
        nonlocal current, current_no
        if current is not None and current_no is not None:
            raw_steps.append((current_no, " ".join(current).strip()))
        current, current_no = None, None

    for raw in lines:
        line = raw.strip()
        if not line or PDF_JUNK_RE.match(line):
            continue

        m = STEP_RE.match(line)
        if m:
            number, body = m.group(1), m.group(2).strip()
            if TAIL_RE.match(body):
                # Numbered in the source but it is an instruction - see docstring.
                flush()
                tail.append(body)
                seen_step = True
                continue
            flush()
            current_no, current = number, [body]
            seen_step = True
            continue

        if TAIL_RE.match(line):
            flush()
            tail.append(line)
            continue

        if not seen_step:
            preamble.append(line)
            continue

        # Continuation of the step in progress: cleaner bullet, parenthetical
        # remark, or a wrapped line.
        if current is not None:
            current.append(line)

    flush()

    # ---- steps -----------------------------------------------------------
    steps: List[dict] = []
    for position, (source_no, text) in enumerate(raw_steps, start=1):
        # Cleaner bullets and remarks were folded into `text` when the wrapped
        # lines were joined; pull the bullets back out.
        bullets = re.findall(r"-\s*([A-Z][^-()]*?)(?=\s+-\s|\s*\(|$)", text)
        body, remark = split_remark(text)
        name, kind = extract_step_name(body)
        temperature = extract_temperature(body)
        duration = extract_duration(body)
        note_bits = []
        if kind:
            note_bits.append(f"type={kind}")
        if source_no != str(position):
            # HM prints steps 1,3,5,6,7 - keep the book's own numbering visible.
            note_bits.append(f"source step number {source_no}")
        if remark:
            note_bits.append(remark)
        steps.append({
            "step_order": position,
            "step_name": name,
            "step_description": text,
            "medium": extract_medium(body),
            "temperature": temperature,
            "duration": duration,
            "duration_minutes": duration_to_minutes(duration),
            "temperature_c": temperature_to_c(temperature),
            "cleaner": "; ".join(b.strip() for b in bullets) or None,
            "notes": "; ".join(note_bits) or None,
        })

    # ---- instructions ----------------------------------------------------
    sentences: List[str] = []
    for block in (" ".join(preamble), note.strip(), " ".join(tail)):
        for sentence in split_sentences(block):
            if sentence not in sentences:      # Note often repeats the preamble
                sentences.append(sentence)

    if not raw_steps and not sentences:
        sentences = split_sentences(cell)

    instructions = [(classify(s), s) for s in sentences]
    return Procedure(code, cell.strip(), steps, instructions)


def parse_file(path: Path) -> List[Procedure]:
    out: List[Procedure] = []
    with open(path, encoding="utf-8", newline="") as fh:
        for row in csv.reader(fh):
            if not row or not row[0].strip() or row[0].strip() == "Procedure Code":
                continue
            code = row[0].strip()
            cell = row[1] if len(row) > 1 else ""
            note = row[2] if len(row) > 2 else ""
            if not cell.strip():
                log.warning("Procedure %s has an empty Procedure cell - skipped", code)
                continue
            out.append(parse_procedure(code, cell, note))
    return out


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------
def resolve_source(cur) -> int:
    sid = get_source_id(cur, SOURCE_NAME)
    if sid is not None:
        log.info("Using existing source id=%s (%r)", sid, SOURCE_NAME)
        return sid
    return create_source(cur, SOURCE_NAME)


def load(cur, procedures: List[Procedure], source_id: int) -> Tuple[int, int, int]:
    codes = [p.code for p in procedures]
    execute_values(
        cur,
        """
        INSERT INTO procedure_templates
            (procedure_code, template_name, description, source_id, created_at, updated_at)
        VALUES %s
        ON CONFLICT (source_id, procedure_code) DO UPDATE SET
            template_name = EXCLUDED.template_name,
            description   = EXCLUDED.description,
            updated_at    = now()
        """,
        [(p.code, f"Verwey procedure {p.code}", p.full_text, source_id) for p in procedures],
        template="(%s,%s,%s,%s,now(),now())",
        page_size=300,
    )

    cur.execute(
        "SELECT procedure_code, id FROM procedure_templates "
        "WHERE source_id = %s AND procedure_code = ANY(%s)",
        (source_id, codes),
    )
    ids = dict(cur.fetchall())

    # Rebuild children so a re-run replaces rather than accumulates.
    template_ids = list(ids.values())
    cur.execute("DELETE FROM procedure_template_steps WHERE procedure_templates_id = ANY(%s)",
                (template_ids,))
    cur.execute("DELETE FROM procedure_template_instruction WHERE procedure_templates_id = ANY(%s)",
                (template_ids,))

    step_rows = [
        (ids[p.code], s["step_order"], s["step_name"], s["step_description"],
         s["medium"], s["temperature"], s["duration"], s["cleaner"],
         True, s["notes"], s["duration_minutes"], s["temperature_c"])
        for p in procedures for s in p.steps
    ]
    if step_rows:
        execute_values(
            cur,
            """
            INSERT INTO procedure_template_steps
                (procedure_templates_id, step_order, step_name, step_description,
                 medium, temperature, duration, cleaner, mandatory, notes,
                 default_duration_minutes, default_temperature_c,
                 created_at, updated_at)
            VALUES %s
            """,
            step_rows,
            template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),now())",
            page_size=1000,
        )

    instruction_rows = [
        (ids[p.code], level, message, order,
         # "is advisable" / "could be" / "might" is guidance, not a requirement.
         not re.search(r"\bis advisable\b|\bcould be\b|\bmight\b|\bwise to\b",
                       message, re.IGNORECASE))
        for p in procedures
        for order, (level, message) in enumerate(p.instructions, start=1)
    ]
    if instruction_rows:
        execute_values(
            cur,
            """
            INSERT INTO procedure_template_instruction
                (procedure_templates_id, instruction_type, message, display_order,
                 mandatory, created_at, updated_at)
            VALUES %s
            """,
            instruction_rows,
            template="(%s,%s::\"InstructionType\",%s,%s,%s,now(),now())",
            page_size=1000,
        )

    return len(procedures), len(step_rows), len(instruction_rows)


def report(procedures: List[Procedure]) -> None:
    from collections import Counter
    no_steps = [p.code for p in procedures if not p.steps]
    log.info("Templates: %d | steps: %d | instructions: %d",
             len(procedures), sum(len(p.steps) for p in procedures),
             sum(len(p.instructions) for p in procedures))
    log.info("Advisory-only templates (no numbered steps): %d %s",
             len(no_steps), no_steps)
    severities = Counter(level for p in procedures for level, _ in p.instructions)
    log.info("Instruction severities: %s", dict(severities))
    names = Counter(s["step_name"] for p in procedures for s in p.steps)
    log.info("Step actions: %s", dict(names.most_common()))
    log.info("Steps with a parsed duration: %d | with a parsed temperature: %d",
             sum(1 for p in procedures for s in p.steps if s["duration_minutes"]),
             sum(1 for p in procedures for s in p.steps if s["temperature_c"]))


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

    procedures = parse_file(path)
    log.info("Parsed %s: %d procedure codes", path.name, len(procedures))
    report(procedures)
    if not procedures:
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
            n_tpl, n_steps, n_inst = load(cur, procedures, source_id)
        if args.dry_run:
            conn.rollback()
            log.info("DRY RUN - rolled back (%d templates, %d steps, %d instructions)",
                     n_tpl, n_steps, n_inst)
        else:
            conn.commit()
            log.info("✓ Committed: %d templates, %d steps, %d instructions (source_id=%s)",
                     n_tpl, n_steps, n_inst, source_id)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
