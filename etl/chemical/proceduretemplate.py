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
    python3 etl/chemical/proceduretemplate.py                 # default file, upsert
    python3 etl/chemical/proceduretemplate.py path/to.csv
    python3 etl/chemical/proceduretemplate.py --dry-run
    python3 etl/chemical/proceduretemplate.py --source-id 7
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

# Loaders are run as scripts (python3 etl/<branch>/<file>.py), so only their
# own directory is on sys.path. Add the shared helpers.
_ETL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ETL_ROOT / "common"))

from _paths import input_file
from sources import get_source_id, create_source

DEFAULT_FILE = input_file("Dr. Verwey’s Tank Cleaning Table 4.xlsx - CLEANING PROCEDURES (T-2).csv")
SOURCE_NAME = "Dr Verweys Tank Cleaning Guide"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("proceduretemplate")

# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------
CODE_RE = re.compile(r"^[A-Z]{1,2}$")            # A, AA, B, ... in column 0
# "1. Butterworthing ...", and also "1 . " / "1) " - the guide is re-exported by
# hand and the separator is not always tight against the number. Requiring
# "\d+\." dropped any step written "1 . ", and because a dropped first line has
# no step to attach to it vanished entirely and every later step shifted up one.
# Also "3 Butterworthing ..." - a step whose separator is missing entirely.
# Guarded by a CAPITAL letter, because a wrapped continuation line can begin
# with a number too: procedure BB carries "...for about\n3 hours;", and taking
# that as step 3 would invent a step and truncate the real one. A step starts a
# sentence; a continuation carries on in lower case.
STEP_RE = re.compile(r"^\s*(\d+)(?:\s*[.)]\s*|\s+(?=[A-Z]))(.*)$")
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
# A temperature the source prints as a figure. Accepts BOTH notations: the
# degree sign, and the spelled-out "deg C" / "degrees C" that the Drew Ameroid
# guide uses throughout. Requiring the sign alone silently threw away every
# figure in that guide and left only the qualitative word beside it - "Warm"
# where the source said "warm (40-55 deg C)".
#
# The [cC] is guarded so a Fahrenheit figure printed beside it
# ("(104-131 deg F)") is never taken, and the number is guarded against a
# preceding letter or digit: one source carries the OCR typo "(B0 °C)" for
# "(80 °C)", and reading the surviving "0" out of it asserted 0°C for a HOT
# seawater wash - a contradiction the source never printed. A figure welded to
# a letter is not a figure, so it falls through to the qualitative word.
_DEG_UNIT = r"(?:°|deg(?:rees)?\.?)\s*\.?\s*[cC](?![a-zA-Z])"
# "(50 C)" - a bare C with the degree sign lost in the export. Accepted ONLY
# inside parentheses: there a figure followed by a lone C can be nothing but a
# temperature, whereas a bare "50 C" in running text could be anything. Checked
# against every step description in the database - it matches this one form and
# nothing else.
# Also "(80 -C)" / "(80 *C)" - the degree sign itself mangled by the export.
# Still parenthesised and still guarded against a letter-corrupted number, so
# "(B0 *C)" stays rejected: there it is the FIGURE that is unreadable, and a
# wrong temperature is worse than none.
DEG_BARE_PAREN_RE = re.compile(
    r"\((?<![A-Za-z])\s*(\d+(?:[.,]\d+)?)\s*[-*·°]?\s*[cC]\s*\)")
# Parenthesised asides, for keeping a temperature word out of them.
PAREN_RE = re.compile(r"\([^()]*\)")
_DEG_NUM = r"\d+(?:[.,]\d+)?"
# "70 °C to 80 °C" - both ends carry the unit.
DEG_RANGE_RE = re.compile(
    rf"(?<![A-Za-z0-9])({_DEG_NUM})\s*{_DEG_UNIT}\s*(?:to|[-–])\s*({_DEG_NUM})\s*{_DEG_UNIT}")
# "40-55 deg C" - one unit for both ends - and the plain "80 °C".
DEG_RE = re.compile(
    rf"(?<![A-Za-z0-9])({_DEG_NUM}(?:\s*[-–]\s*{_DEG_NUM})?)\s*{_DEG_UNIT}")

# A duration. Numbers may carry a EUROPEAN DECIMAL COMMA ("1,5 - 2,5 hours"),
# a vulgar fraction ("2½ hours") or a written one ("1/2 - 1 hour"), and the
# range may be joined by a dash, "to" or "till". The old pattern excluded
# commas and full stops from the phrase it captured, so a comma-decimal range
# never matched and a fallback grabbed the tail of it instead: every
# "1,5 - 2,5 hours" in the Drew Ameroid guide was stored as "5 hours", which is
# not an imprecise reading of the source but a wrong one.
_DUR_NUM = r"\d+(?:[.,]\d+)?|\d+\s*/\s*\d+"
DURATION_RE = re.compile(
    rf"(?P<qual>minimum|min\.?|at\s+least|maximum|max\.?|about|approx(?:imately)?\.?)?\s*"
    rf"(?P<lo>{_DUR_NUM})\s*(?:(?:-|–|—|to|till)\s*(?P<hi>{_DUR_NUM}))?\s*"
    rf"(?P<unit>hours?|hrs?|minutes?|mins?)\b", re.I)

FRACTIONS = {"½": ".5", "¼": ".25", "¾": ".75"}

# "warm sea or fresh water" names TWO media, but "sea water" never appears as a
# contiguous string - the guide elides the first "water". Expanded before the
# MEDIA scan so both are seen.
ELIDED_WATER_RE = re.compile(r"\b(sea|fresh)\s+or\s+(sea|fresh)\s+water\b", re.I)


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def _defraction(text: str) -> str:
    """"2½ hours" -> "2.5 hours"; a standalone "½ hour" -> "0.5 hour".

    Converted only where the glyph joins a digit or stands on its own. One
    source carries OCR damage ("1%½", "l½") where the leading figure is lost;
    those are left untouched so the loose fallback below keeps the raw phrase
    rather than this inventing a number out of the surviving half.
    """
    out = []
    for i, ch in enumerate(text):
        if ch in FRACTIONS:
            prev = text[i - 1] if i else ""
            if prev.isdigit():
                out.append(FRACTIONS[ch])
                continue
            if not prev.isalnum() and prev != "%":
                out.append("0" + FRACTIONS[ch])
                continue
        out.append(ch)
    return "".join(out)


def _dur_num(token: str) -> str:
    """One duration figure as a plain number: "1,5" -> 1.5, "1/2" -> 0.5."""
    token = token.strip().replace(",", ".")
    if "/" in token:
        num, den = [x.strip() for x in token.split("/")]
        return f"{float(num) / float(den):g}"
    return f"{float(token):g}"


def extract_temperature(step: str) -> Optional[str]:
    """The figure the source prints, and the word it prints beside it.

    Both are kept ("Warm (40-55°C)") rather than one replacing the other: the
    word is how the guide's own matrix refers to the step, and the range is
    what a reader needs to actually run it.
    """
    rng = None
    m = DEG_RANGE_RE.search(step)
    if m:
        rng = f"{m.group(1).replace(',', '.')}-{m.group(2).replace(',', '.')}"
    else:
        m = DEG_RE.search(step)
        if m:
            rng = re.sub(r"\s*[-–]\s*", "-", m.group(1).strip()).replace(",", ".")
        else:
            m = DEG_BARE_PAREN_RE.search(step)
            if m:
                rng = m.group(1).replace(",", ".")
    # A qualitative word inside parentheses is usually an ASIDE about something
    # else, not the wash temperature: "at 55 °C to 60 °C ... (Moderate
    # temperature to avoid high boiling residues)" is not a boiling wash, and
    # "(Ventilate tank after hot wash and ...)" describes the previous step.
    # So the word is taken from outside the parentheses, and the bracketed text
    # is consulted only when the step states no figure at all - there the
    # parenthetical is the instruction ("Fill (Fill slowly 5 m3 cold fresh
    # water ...)") and its word is all there is.
    outside = PAREN_RE.sub(" ", step).lower()
    word = next((canon for term, canon in TEMP_WORDS if term in outside), None)
    if word is None and rng is None:
        low = step.lower()
        word = next((canon for term, canon in TEMP_WORDS if term in low), None)
    if rng and word:
        return f"{word} ({rng}°C)"
    if rng:
        return f"{rng}°C"
    return word


def extract_medium(step: str) -> Optional[str]:
    """Every medium the step names, joined the way the source joins them.

    Returning only the first match dropped the alternative a step offers -
    "cold sea or fresh water" became just "Fresh Water", which reads as an
    instruction the source never gave. The connector is taken from the text
    between the two mentions, so "sea or fresh" stays an alternative and
    "fresh water and Steam" stays a sequence.
    """
    low = ELIDED_WATER_RE.sub(lambda m: f"{m.group(1)} water or {m.group(2)} water",
                              step.lower())
    hits: list = []
    seen = set()
    for term, canon in MEDIA:
        i = low.find(term)
        if i >= 0 and canon not in seen:
            seen.add(canon)
            hits.append((i, canon))
    if not hits:
        return None
    if len(hits) > 1:                      # "Water" is the generic fallback
        hits = [h for h in hits if h[1] != "Water"] or hits
    hits.sort()
    if len(hits) == 1:
        return hits[0][1]
    out = hits[0][1]
    for (pos_a, _), (pos_b, name_b) in zip(hits, hits[1:]):
        gap = low[pos_a:pos_b]
        joiner = " or " if re.search(r"\bor\b", gap) else (
                 " and " if re.search(r"\band\b", gap) else ", ")
        out += joiner + name_b
    return out


def extract_duration(step: str) -> Optional[str]:
    """The period the step runs for, as a number and a unit."""
    step = _defraction(step)
    m = DURATION_RE.search(step)
    if m:
        lo, hi = _dur_num(m.group("lo")), m.group("hi")
        unit = "hours" if m.group("unit").lower().startswith(("hour", "hr")) else "minutes"
        value = f"{lo}-{_dur_num(hi)} {unit}" if hi else f"{lo} {unit}"
        if not hi and lo == "1":
            value = f"1 {unit[:-1]}"
        qual = (m.group("qual") or "").strip().lower().rstrip(".")
        if qual in ("minimum", "min", "at least"):
            value = f"Minimum {value}"
        elif qual in ("maximum", "max"):
            value = f"Maximum {value}"
        elif qual:
            value = f"About {value}"
        return _cap(value)
    low = step.lower()
    m = re.search(r"until+\s+[^;:,.\n]+", low)     # "untill" is in one source
    if m:
        return _cap(m.group(0).strip())
    if "without interruption" in low:
        return "Without interruption"
    # Nothing parsed as a figure. Keep whatever phrase the source put after
    # "for", so a step that had a duration - even a malformed one - never
    # loses it to this rewrite.
    m = re.search(r"for\s+(about\s+)?([^;:\n]*?hours?)", low)
    if m:
        return _cap(re.sub(r"\s+", " ", (m.group(1) or "") + m.group(2)).strip())
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

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")

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
                    ON CONFLICT (source_id, procedure_code) DO UPDATE SET
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
