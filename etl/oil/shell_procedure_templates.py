#!/usr/bin/env python3
"""
Load the Shell tank-cleaning procedure codes into
procedure_templates + procedure_template_steps.

Default input: etl/data/inputs/Shell Tank Cleaning Procedure.xlsx, sheet
"Cleaning Codes" - 11 rows, one per procedure code.

SOURCE
------
"Shell Tank Cleaning Guide 2016.pdf" (source.json, category 'oil'). This
spreadsheet is an extract of that document, not a source of its own, so the
loader RESOLVES the existing source row and never creates one. The same source
also owns the Shell Cargo Master (etl/oil/shell_cargo_master.py).

WHAT GOES WHERE
---------------
    procedure_code       -> procedure_templates.procedure_code   (the key)
    template_name        -> procedure_templates.template_name
    water_type           -> procedure_templates.water_type
    loading_allowed      -> procedure_templates.loading_allowed
    description          -> procedure_templates.description
    Concrete steps       -> procedure_template_steps             (one row per line)

    cargo_type is forced to OIL for every row. The sheet says CHEMICAL on HW and
    NC, which is an error in an oil tank-cleaning document; loading it verbatim
    would file two of the eleven codes under the wrong cargo master. This is a
    deliberate, instructed correction - the only value in this loader that does
    not come from the sheet.

procedure_template_requirement and procedure_template_instruction are NOT
written. This sheet expresses everything as steps; nothing is split off into
rules or source-level statements.

THE STEP CELL
-------------
"Concrete steps" holds a numbered list as free text:

    1. Drain cargo tank using ship's stripping system.
    2. Minimise ROB (Remaining On Board).
    ...

Each numbered line becomes one step row. The line's own number is step_order,
and the sentence is kept VERBATIM in step_description.

step_name is NOT NULL and the sheet gives no short label, so one is derived from
the sentence by the rule table below. step_type and medium come from the same
rules. The rules are matched IN ORDER and a sentence matching none of them is a
hard error naming the sentence: a new wording in the sheet must be classified by
a person, never guessed at by falling through to a default.

Two codes - BF and BF-VP - have an EMPTY step cell. They are loaded as templates
with zero steps. The sheet says nothing about their steps, so nothing is
invented; filling the cell and re-running is all it takes to give them steps.

NC is a decision, not a procedure: loading_allowed is false and it gets no steps.
Its cell holds a sentence rather than a numbered list, and that sentence is
already the description.

IDEMPOTENCY
-----------
Re-running upserts rather than duplicating. Keys:

    templates  (source_id, procedure_code)
    steps      (procedure_templates_id, step_order)

Steps whose slot is no longer in the sheet are deleted, so shortening a
procedure in Excel shortens it in the database instead of leaving a tail behind.
The ENTIRE sheet is validated before a single row is written; any error aborts
the whole import.

Usage:
    python3 etl/oil/shell_procedure_templates.py
    python3 etl/oil/shell_procedure_templates.py --dry-run
    python3 etl/oil/shell_procedure_templates.py path/to/workbook.xlsx
"""

import argparse
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import psycopg2
from dotenv import load_dotenv

# Loaders are run as scripts (python3 etl/<branch>/<file>.py), so only their
# own directory is on sys.path. Add the shared helpers.
_ETL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ETL_ROOT / "common"))

from _paths import input_file  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("shell_procedures")

SOURCE_NAME = "Shell Tank Cleaning Guide 2016.pdf"
DEFAULT_FILE = input_file("Shell Tank Cleaning Procedure.xlsx")
DEFAULT_REGIME_FILE = input_file("Shell Tank cleaning Procedure From Excel.csv")
SHEET = "Cleaning Codes"

# Words that carry no identity when matching a filename to a source name.
_STOPWORDS = {"the", "of", "for", "from", "and", "a", "an", "pdf", "csv", "xls",
              "xlsx", "excel", "copy", "final", "data", "v1", "v2"}
MIN_NAME_TOKEN_OVERLAP = 3

# Instructed: the sheet's own cargo_type is not trusted. See module docstring.
CARGO_TYPE = "OIL"

REQUIRED_COLUMNS = ["procedure_code", "template_name", "water_type",
                    "loading_allowed", "description", "Concrete steps"]

# A numbered line in the step cell: "6. Cold-water wash cargo tank(s) and lines."
STEP_LINE_RE = re.compile(r"^\s*(\d+)\s*[.)]\s*(.+?)\s*$")


# ---------------------------------------------------------------------------
# Regime CSV (Shell white-oil cleaning regimes)
# ---------------------------------------------------------------------------
REGIME_COLUMNS = ["regime_code", "instruction_1", "instruction_2", "steps"]

# "Perform WD" appears in 25 of the 30 regimes. It is stored ONCE, here, and
# referenced - never copied into a regime. This is its own procedure_code so it
# does not collide with the letter-coded WD that the Excel sheet defines; the
# two are different wordings of the procedure and were instructed to stay apart.
WD_REGIME_CODE = "WD-REGIME"
WD_REGIME_NAME = "Well Drained (regime reference)"
WD_REGIME_STEPS = [
    "Use the ship's stripping system to drain the cargo tank.",
    "Minimise ROB (Remaining On Board).",
    "Any remaining ROB should only be in the pump well where applicable.",
    "Clear and drain pump columns, pipelines and drops.",
    "Remove all free-standing product and water.",
    "ROB must not exceed 0.05% of the individual tank capacity.",
]

# (pattern, step_name, step_type) matched IN ORDER against the lower-cased
# sentence. Order is load-bearing: the cold-fresh-water wash must be tried
# before the generic "wash cargo tank(s)" rule or it would lose its medium.
#
# The sentence itself is stored VERBATIM as step_description; these rules only
# supply the short step_name (NOT NULL, absent from the CSV) and the role.
REGIME_STEP_RULES = [
    (r"perform wd",                                  "Perform WD",                  "REFERENCE"),
    (r"do not load",                                 "Do Not Load",                 "DECISION"),
    (r"chemical-grade-specific",                     "Chemical Grade Requirement",  "CONDITIONAL"),
    (r"for mogas blending",                          "Mogas Blending Exception",    "CONDITIONAL"),
    (r"bottom flush with seawater may replace",      "Bottom Flush Alternative",    "CONDITIONAL"),
    (r"follow the cleaning instruction for regime",  "Follow Regime Instruction",   "MAIN"),
    (r"wash cargo tank\(s\) using cold fresh water", "Cold Fresh Water Wash",       "MAIN"),
    (r"wash cargo tank\(s\) as specified",           "Wash Cargo Tanks",            "MAIN"),
    (r"water flush the lines",                       "Water Flush Lines",           "MAIN"),
    (r"drain tank and lines well",                   "Drain Tank And Lines",        "MAIN"),
    (r"remove all free-standing",                    "Remove Free-Standing Water",  "MAIN"),
    (r"prepare tanks for entry",                     "Prepare Tanks For Entry",     "MAIN"),
    (r"meticulously drain pumps and lines",          "Drain Pumps And Mop Dry",     "MAIN"),
    (r"purge tank atmosphere",                       "Purge Tank Atmosphere",       "MAIN"),
    (r"no additional cleaning or draining",          "No Cleaning Required",        "MAIN"),
    (r"previous and next cargo are identical",       "Identical Cargo",             "MAIN"),
]
_REGIME_COMPILED = [(re.compile(p), n, r) for p, n, r in REGIME_STEP_RULES]

# Only these roles quote the source's condition wording; MAIN and REFERENCE
# steps are unconditional and leave `condition` NULL.
CONDITION_BEARING = {"CONDITIONAL", "DECISION"}

# Reusable notes, stored once and linked to the procedures they apply to.
# `applies_when` is a regex tested against the regime's whole text (both
# instructions plus its steps): the link is derived from what the source says,
# never from a hand-kept list of regime numbers.
NOTES = [
    ("ROB", "ROB",
     "ROB refers to the quantity Remaining On Board the vessel after discharge "
     "of the carried cargo.",
     r"\brob\b"),
    ("PURGE_VENT_GASFREE", "Purging, Ventilating or Gas-Free",
     "This applies to procedures that require purge, ventilation or gas-free "
     "operations.",
     r"purge|purging|ventilat|gas[ -]?free"),
    ("WASH_NON_INERTED", "Tank Washing on Non-Inerted Vessels",
     "This applies to washing procedures on vessels without inert gas systems.",
     r"\bwash"),
]
_NOTES_COMPILED = [(c, t_, b, re.compile(p, re.I)) for c, t_, b, p in NOTES]


# ---------------------------------------------------------------------------
# Step classification
# ---------------------------------------------------------------------------
# (pattern, step_name, step_type, medium, mandatory)
#
# Matched IN ORDER against the lower-cased sentence, first match wins. Order is
# load-bearing: "Bulk washing may use cold seawater" also contains "wash" and
# "cold", so the bulk and final-wash rules must be tried before the generic wash
# rules or the allowance would be recorded as an ordinary mandatory wash.
#
# `medium` is only ever the wash water the sentence NAMES. temperature, duration
# and cleaner stay NULL - this source states none of them.
STEP_RULES: List[Tuple[str, str, str, Optional[str], bool]] = [
    # --- washing: most specific first -------------------------------------
    (r"bulk washing may use cold sea ?water",
     "Bulk Wash", "CLEANING", "Cold Sea Water", False),
    (r"bulk washing may use hot sea ?water",
     "Bulk Wash", "CLEANING", "Hot Sea Water", False),
    (r"final wash must be with cold fresh water",
     "Final Wash", "CLEANING", "Cold Fresh Water", True),
    (r"final wash must be with hot fresh water",
     "Final Wash", "CLEANING", "Hot Fresh Water", True),
    (r"wash tank\(s\) and lines with cold fresh water",
     "Cold Fresh Water Wash", "CLEANING", "Cold Fresh Water", True),
    (r"wash tank\(s\) and lines with hot fresh water",
     "Hot Fresh Water Wash", "CLEANING", "Hot Fresh Water", True),
    (r"cold-water wash",
     "Cold Water Wash", "CLEANING", "Cold Water", True),
    (r"hot-water wash",
     "Hot Water Wash", "CLEANING", "Hot Water", True),

    # --- the well-drained preconditions -----------------------------------
    (r"drain cargo tank using ship's stripping system",
     "Drain Cargo Tank", "DRAINING", None, True),
    (r"minimise rob",
     "Minimise ROB", "PRECONDITION", None, True),
    (r"rob should only be in the pump well",
     "ROB In Pump Well", "PRECONDITION", None, True),
    (r"clear and drain pump columns",
     "Clear Pump Columns And Lines", "DRAINING", None, True),
    (r"rob must not exceed",
     "ROB Limit", "PRECONDITION", None, True),

    # --- finishing --------------------------------------------------------
    (r"drain well",
     "Drain Well", "DRAINING", None, True),
    (r"remove (all )?free-standing",
     "Remove Free-Standing Water", "DRAINING", None, True),
    (r"ventilate or purge",
     "Ventilate Or Purge", "VENTILATING", None, True),
    (r"render tank gas-free",
     "Render Gas-Free", "GAS_FREEING", None, True),
    (r"mop tanks dry",
     "Mop Dry", "MOPPING", None, True),
]

_COMPILED = [(re.compile(p), name, stype, medium, mand)
             for p, name, stype, medium, mand in STEP_RULES]


def classify(sentence: str) -> Optional[Tuple[str, str, Optional[str], bool]]:
    """(step_name, step_type, medium, mandatory) for a step sentence, or None.

    None means no rule matched. The caller turns that into an error rather than
    a default: an unclassified wording is a sheet a person has not looked at.
    """
    low = sentence.lower()
    for rx, name, stype, medium, mand in _COMPILED:
        if rx.search(low):
            return name, stype, medium, mand
    return None


# ---------------------------------------------------------------------------
# Reading and validation
# ---------------------------------------------------------------------------
def text(value: Any) -> Optional[str]:
    """Trim a cell; blank and NaN become None."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    s = re.sub(r"[ \t]+", " ", str(value).strip())
    return s or None


def boolean(value: Any, where: str, errors: List[str]) -> Optional[bool]:
    """The sheet writes loading_allowed as 1/0; accept the usual spellings."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        errors.append(f"{where}: loading_allowed is empty")
        return None
    s = str(value).strip().lower()
    if s in {"1", "1.0", "true", "yes", "y"}:
        return True
    if s in {"0", "0.0", "false", "no", "n"}:
        return False
    errors.append(f"{where}: loading_allowed is {value!r}, expected 1/0")
    return None


def parse_steps(cell: Any, code: str, errors: List[str]) -> List[dict]:
    """Split the step cell into rows. Returns [] when the cell is empty."""
    raw = text(cell)
    if raw is None:
        return []

    lines = [ln for ln in (l.strip() for l in raw.splitlines()) if ln]
    steps: List[dict] = []
    for ln in lines:
        m = STEP_LINE_RE.match(ln)
        if not m:
            # An unnumbered line is prose, not a step. NC is the only row that
            # has one and it is handled before this is ever called, so reaching
            # here means the sheet changed and a person should look.
            errors.append(f"[{code}] step line is not numbered: {ln[:70]!r}")
            continue

        order, sentence = int(m.group(1)), m.group(2)
        rule = classify(sentence)
        if rule is None:
            errors.append(
                f"[{code}] step {order}: no classification rule matches "
                f"{sentence!r}. Add a rule to STEP_RULES or fix the wording.")
            continue

        name, stype, medium, mandatory = rule
        steps.append({
            "step_order": order,
            "step_name": name,
            "step_description": sentence,   # verbatim
            "step_type": stype,
            "medium": medium,
            "mandatory": mandatory,
        })

    orders = [s["step_order"] for s in steps]
    if orders and orders != list(range(1, len(orders) + 1)):
        errors.append(f"[{code}] step numbers are not 1..n in order: {orders}")
    return steps


def read_sheet(path: Path) -> Tuple[List[dict], List[str]]:
    """Validate the whole sheet up front. Returns (rows, errors)."""
    errors: List[str] = []
    xl = pd.ExcelFile(path)
    if SHEET not in xl.sheet_names:
        return [], [f"workbook has no sheet {SHEET!r} (found {xl.sheet_names})"]

    df = pd.read_excel(path, sheet_name=SHEET)
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        return [], [f"[{SHEET}] missing column(s): {', '.join(missing)}"]

    rows: List[dict] = []
    seen: Dict[str, int] = {}
    for i, r in df.iterrows():
        excel_row = i + 2                      # header is row 1
        where = f"[{SHEET}] row {excel_row}"

        code = text(r["procedure_code"])
        if not code:
            errors.append(f"{where}: procedure_code is empty")
            continue
        if code in seen:
            errors.append(f"{where}: procedure_code {code!r} already used on row {seen[code]}")
            continue
        seen[code] = excel_row

        name = text(r["template_name"])
        if not name:
            errors.append(f"{where}: template_name is empty")

        allowed = boolean(r["loading_allowed"], where, errors)

        # A code that forbids loading is a decision, not a procedure: it gets no
        # steps, and its cell is prose rather than a numbered list.
        steps = [] if allowed is False else parse_steps(r["Concrete steps"], code, errors)

        rows.append({
            "procedure_code": code,
            "template_name": name,
            "water_type": text(r["water_type"]),
            "loading_allowed": allowed,
            "description": text(r["description"]),
            "steps": steps,
        })

    return rows, errors


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------
def _tokens(name: str) -> set:
    """Identity-bearing words of a name, for matching a file to a source."""
    words = re.findall(r"[a-z0-9]+", name.lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 1}


def source_by_name(cur, name: str) -> int:
    """Resolve a source by its exact name, for --source.

    The filename heuristic below cannot separate two guides published by the
    same company: "Shell Tank cleaning Procedure From Excel.csv" shares exactly
    three identity words with BOTH "Shell Tank Cleaning Guide 2016.pdf" and
    "Shell White Oil Tank Cleaning Guide", and a tie is a hard error there by
    design. Naming the source settles it without weakening that check.
    """
    cur.execute("SELECT id FROM source WHERE name = %s", (name,))
    row = cur.fetchone()
    if row is None:
        sys.exit(
            f"Error: source {name!r} not found.\n"
            f"  It is declared in etl/data/source.json - register it with:\n"
            f"      python3 etl/common/source.py"
        )
    return row[0]


def resolve_source(cur, filename: str) -> int:
    """Find the source this input belongs to by PARTIAL name match, else create.

    The inputs are extracts whose filenames never equal the document's name
    ("Shell Tank cleaning Procedure From Excel.csv" vs "Shell Tank Cleaning
    Guide 2016.pdf"), so an exact match would create a duplicate source for
    every extract. Matching is on shared identity-bearing words.

    A near-tie is an ERROR, not a coin toss: several tank-cleaning guides share
    the words "tank" and "cleaning", and silently attaching Shell's procedures
    to Dr Verwey's source would be worse than stopping.
    """
    want = _tokens(Path(filename).stem)
    cur.execute("SELECT id, name FROM source ORDER BY id")
    scored = sorted(((len(want & _tokens(n)), sid, n) for sid, n in cur.fetchall()),
                    reverse=True)

    best, runner = scored[0], (scored[1] if len(scored) > 1 else (0, None, None))
    if best[0] >= MIN_NAME_TOKEN_OVERLAP:
        if best[0] == runner[0]:
            sys.exit(f"Error: {filename!r} matches {best[2]!r} and {runner[2]!r} "
                     f"equally well ({best[0]} words each). Rename the file or "
                     f"pass the source explicitly.")
        log.info("source matched on name: %r -> %r (id=%s, %d shared words; "
                 "next best %r with %d)",
                 Path(filename).name, best[2], best[1], best[0], runner[2], runner[0])
        return best[1]

    # Nothing close enough. Create the document this extract belongs to.
    cur.execute(
        """
        INSERT INTO source (name, category, notes, date_ingested, created_at, updated_at)
        VALUES (%s, 'oil', %s, now(), now(), now())
        RETURNING id
        """,
        (SOURCE_NAME, f"Created from {Path(filename).name}: no existing source "
                      f"shared {MIN_NAME_TOKEN_OVERLAP}+ words with the filename."),
    )
    sid = cur.fetchone()[0]
    log.warning("no source matched %r - created %r (id=%s)",
                Path(filename).name, SOURCE_NAME, sid)
    return sid


def upsert_template(cur, source_id: int, row: dict) -> int:
    cur.execute(
        """
        INSERT INTO procedure_templates
            (procedure_code, template_name, cargo_type, water_type,
             loading_allowed, description, source_id, created_at, updated_at)
        VALUES (%s, %s, %s::"CargoType", %s, %s, %s, %s, now(), now())
        ON CONFLICT (source_id, procedure_code) DO UPDATE SET
            template_name   = EXCLUDED.template_name,
            cargo_type      = EXCLUDED.cargo_type,
            water_type      = EXCLUDED.water_type,
            loading_allowed = EXCLUDED.loading_allowed,
            description     = EXCLUDED.description,
            updated_at      = now()
        RETURNING id
        """,
        (row["procedure_code"], row["template_name"], CARGO_TYPE, row["water_type"],
         row["loading_allowed"], row["description"], source_id),
    )
    return cur.fetchone()[0]


def sync_steps(cur, template_id: int, steps: List[dict]) -> None:
    """Upsert this template's steps and drop any slot the sheet no longer has."""
    for s in steps:
        cur.execute(
            """
            INSERT INTO procedure_template_steps
                (procedure_templates_id, step_order, step_name, step_description,
                 step_type, medium, mandatory, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s::"CleaningStepType", %s, %s, now(), now())
            ON CONFLICT (procedure_templates_id, step_order) DO UPDATE SET
                step_name        = EXCLUDED.step_name,
                step_description = EXCLUDED.step_description,
                step_type        = EXCLUDED.step_type,
                medium           = EXCLUDED.medium,
                mandatory        = EXCLUDED.mandatory,
                updated_at       = now()
            """,
            (template_id, s["step_order"], s["step_name"], s["step_description"],
             s["step_type"], s["medium"], s["mandatory"]),
        )

    # Deleting by "beyond the end" rather than wiping and re-inserting keeps the
    # surviving rows' ids stable, so anything referencing a step still does.
    cur.execute(
        "DELETE FROM procedure_template_steps "
        "WHERE procedure_templates_id = %s AND step_order > %s",
        (template_id, len(steps)),
    )


def run_import(cur, source_id: int, rows: List[dict]) -> Tuple[int, int]:
    n_steps = 0
    for row in rows:
        tid = upsert_template(cur, source_id, row)
        sync_steps(cur, tid, row["steps"])
        n_steps += len(row["steps"])
        log.info("  %-6s %-34s steps=%d", row["procedure_code"],
                 row["template_name"] or "", len(row["steps"]))
    return len(rows), n_steps


# ---------------------------------------------------------------------------
# Regime CSV: read, validate, write
# ---------------------------------------------------------------------------
def classify_regime_step(sentence: str) -> Optional[Tuple[str, str]]:
    low = sentence.lower()
    for rx, name, role in _REGIME_COMPILED:
        if rx.search(low):
            return name, role
    return None


def read_regimes(path: Path) -> Tuple[List[dict], List[str]]:
    """Validate the whole CSV up front. Returns (rows, errors)."""
    import csv

    errors: List[str] = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in REGIME_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            return [], [f"CSV missing column(s): {', '.join(missing)}"]
        raw = list(reader)

    rows: List[dict] = []
    seen: Dict[str, int] = {}
    for i, r in enumerate(raw, start=2):          # header is line 1
        where = f"[csv line {i}]"
        code = (r.get("regime_code") or "").strip()
        if not code:
            errors.append(f"{where}: regime_code is empty")
            continue
        if code in seen:
            errors.append(f"{where}: regime_code {code!r} already used on line {seen[code]}")
            continue
        seen[code] = i

        instr1 = (r.get("instruction_1") or "").strip() or None
        instr2 = (r.get("instruction_2") or "").strip() or None
        if not instr1:
            errors.append(f"{where}: instruction_1 is empty")

        # The condition wording is quoted from whichever instruction states it:
        # instruction_2 when the source supplies one, otherwise instruction_1,
        # which is where regimes like 10 and 28 put their "may" clause.
        condition_text = instr2 or instr1

        steps: List[dict] = []
        for line in (r.get("steps") or "").splitlines():
            line = line.strip()
            if not line:
                continue
            m = STEP_LINE_RE.match(line)
            if not m:
                errors.append(f"{where} regime {code}: step line is not numbered: {line[:60]!r}")
                continue
            order, sentence = int(m.group(1)), m.group(2)
            rule = classify_regime_step(sentence)
            if rule is None:
                errors.append(
                    f"{where} regime {code} step {order}: no rule matches {sentence!r}. "
                    f"Add a rule to REGIME_STEP_RULES or fix the wording.")
                continue
            name, role = rule
            steps.append({
                "step_order": order,
                "step_name": name,
                "step_description": sentence,          # verbatim
                "step_type": role,
                "condition": condition_text if role in CONDITION_BEARING else None,
                "is_reference": role == "REFERENCE",
            })

        orders = [s["step_order"] for s in steps]
        if orders and orders != list(range(1, len(orders) + 1)):
            errors.append(f"{where} regime {code}: step numbers are not 1..n: {orders}")

        # A regime whose only step is the refusal forbids loading outright.
        # Regimes that refuse only under a condition (18, 22, 23, 25, 26) keep
        # loading_allowed true - the decision is on the step, not the regime.
        only_decision = bool(steps) and all(s["step_type"] == "DECISION" for s in steps)

        rows.append({
            "regime_code": code,
            "instruction_1": instr1,
            "instruction_2": instr2,
            "steps": steps,
            "loading_allowed": not only_decision,
            "note_haystack": " ".join(filter(None, [
                instr1, instr2, *(s["step_description"] for s in steps)])),
        })

    return rows, errors


def upsert_regime_template(cur, source_id: int, code: str, name: str,
                           loading_allowed: bool) -> int:
    cur.execute(
        """
        INSERT INTO procedure_templates
            (procedure_code, template_name, cargo_type, loading_allowed,
             source_id, created_at, updated_at)
        VALUES (%s, %s, %s::"CargoType", %s, %s, now(), now())
        ON CONFLICT (source_id, procedure_code) DO UPDATE SET
            template_name   = EXCLUDED.template_name,
            cargo_type      = EXCLUDED.cargo_type,
            loading_allowed = EXCLUDED.loading_allowed,
            updated_at      = now()
        RETURNING id
        """,
        (code, name, CARGO_TYPE, loading_allowed, source_id),
    )
    return cur.fetchone()[0]


def sync_regime_steps(cur, template_id: int, steps: List[dict],
                      wd_id: Optional[int]) -> None:
    for s in steps:
        cur.execute(
            """
            INSERT INTO procedure_template_steps
                (procedure_templates_id, step_order, step_name, step_description,
                 step_type, condition, reference_template_id, mandatory,
                 created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s::"CleaningStepType", %s, %s, true, now(), now())
            ON CONFLICT (procedure_templates_id, step_order) DO UPDATE SET
                step_name             = EXCLUDED.step_name,
                step_description      = EXCLUDED.step_description,
                step_type             = EXCLUDED.step_type,
                condition             = EXCLUDED.condition,
                reference_template_id = EXCLUDED.reference_template_id,
                updated_at            = now()
            """,
            (template_id, s["step_order"], s["step_name"], s["step_description"],
             s["step_type"], s["condition"],
             wd_id if s["is_reference"] else None),
        )
    cur.execute(
        "DELETE FROM procedure_template_steps "
        "WHERE procedure_templates_id = %s AND step_order > %s",
        (template_id, len(steps)),
    )


def sync_instructions(cur, template_id: int, instr1: Optional[str],
                      instr2: Optional[str]) -> int:
    """Preserve both instructions verbatim, in order.

    display_order 1 is the main procedure as the source words it; 2 is the
    conditional/additional instruction. instruction_2 is typed WARNING when it
    states an incompatibility, IMPORTANT otherwise - it is never a further step.
    """
    written = 0
    for order, message in ((1, instr1), (2, instr2)):
        if not message:
            continue
        itype = "WARNING" if (order == 2 and "not compatible" in message.lower()) else "IMPORTANT"
        cur.execute(
            """
            INSERT INTO procedure_template_instruction
                (procedure_templates_id, instruction_type, message, display_order,
                 mandatory, created_at, updated_at)
            VALUES (%s, %s::"InstructionType", %s, %s, true, now(), now())
            ON CONFLICT (procedure_templates_id, display_order) DO UPDATE SET
                instruction_type = EXCLUDED.instruction_type,
                message          = EXCLUDED.message,
                updated_at       = now()
            """,
            (template_id, itype, message, order),
        )
        written += 1
    cur.execute(
        "DELETE FROM procedure_template_instruction "
        "WHERE procedure_templates_id = %s AND display_order > %s",
        (template_id, written),
    )
    return written


def ensure_notes(cur, source_id: int) -> Dict[str, int]:
    """Create the reusable notes once. Returns note_code -> id."""
    ids: Dict[str, int] = {}
    for code, title, body, _ in _NOTES_COMPILED:
        cur.execute(
            """
            INSERT INTO procedure_note (note_code, title, body, source_id,
                                        created_at, updated_at)
            VALUES (%s, %s, %s, %s, now(), now())
            ON CONFLICT (note_code) DO UPDATE SET
                title = EXCLUDED.title, body = EXCLUDED.body, updated_at = now()
            RETURNING id
            """,
            (code, title, body, source_id),
        )
        ids[code] = cur.fetchone()[0]
    return ids


def link_notes(cur, template_id: int, haystack: str, note_ids: Dict[str, int]) -> int:
    """Link the notes whose subject this procedure actually mentions."""
    linked = 0
    wanted = []
    for code, _, _, rx in _NOTES_COMPILED:
        if rx.search(haystack):
            wanted.append(note_ids[code])
            cur.execute(
                """
                INSERT INTO procedure_template_note
                    (procedure_templates_id, procedure_note_id, created_at)
                VALUES (%s, %s, now())
                ON CONFLICT (procedure_templates_id, procedure_note_id) DO NOTHING
                """,
                (template_id, note_ids[code]),
            )
            linked += 1
    # Drop links the text no longer justifies, so an edit narrows them too.
    cur.execute(
        "DELETE FROM procedure_template_note WHERE procedure_templates_id = %s "
        "AND NOT (procedure_note_id = ANY(%s))",
        (template_id, wanted or [-1]),
    )
    return linked


def run_regime_import(cur, source_id: int, rows: List[dict]) -> Dict[str, int]:
    # WD first: the regimes reference it, so it must have an id before they do.
    wd_id = upsert_regime_template(cur, source_id, WD_REGIME_CODE, WD_REGIME_NAME, True)
    sync_regime_steps(cur, wd_id, [
        {"step_order": i, "step_name": f"WD Step {i}", "step_description": s,
         "step_type": "MAIN", "condition": None, "is_reference": False}
        for i, s in enumerate(WD_REGIME_STEPS, start=1)
    ], None)

    counts = {"templates": 1, "steps": len(WD_REGIME_STEPS), "conditional": 0,
              "reference": 0, "decision": 0, "instructions": 0, "note_links": 0}
    note_ids = ensure_notes(cur, source_id)

    for row in rows:
        tid = upsert_regime_template(cur, source_id, row["regime_code"],
                                     f"Cleaning Regime {row['regime_code']}",
                                     row["loading_allowed"])
        sync_regime_steps(cur, tid, row["steps"], wd_id)
        counts["instructions"] += sync_instructions(cur, tid, row["instruction_1"],
                                                    row["instruction_2"])
        counts["note_links"] += link_notes(cur, tid, row["note_haystack"], note_ids)
        counts["templates"] += 1
        counts["steps"] += len(row["steps"])
        for s in row["steps"]:
            if s["step_type"] == "CONDITIONAL":
                counts["conditional"] += 1
            elif s["step_type"] == "REFERENCE":
                counts["reference"] += 1
            elif s["step_type"] == "DECISION":
                counts["decision"] += 1

    counts["notes"] = len(note_ids)
    return counts


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--codes-file", default=str(DEFAULT_FILE),
                    help="workbook of letter-coded procedures (WD, CW, NC, ...)")
    ap.add_argument("--regimes-file", default=str(DEFAULT_REGIME_FILE),
                    help="CSV of numbered cleaning regimes (1..30)")
    ap.add_argument("--only", choices=["codes", "regimes"],
                    help="import just one of the two inputs (default: both)")
    ap.add_argument("--source",
                    help="name the source row exactly, instead of matching it "
                         "from the input filename (which cannot separate two "
                         "guides by the same publisher)")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate the inputs and report; write nothing")
    args = ap.parse_args()

    do_codes = args.only in (None, "codes")
    do_regimes = args.only in (None, "regimes")

    errors: List[str] = []
    rows: List[dict] = []
    regimes: List[dict] = []
    codes_path = Path(args.codes_file)
    regimes_path = Path(args.regimes_file)

    # Validate EVERYTHING before opening a connection: a half-imported pair of
    # inputs is harder to reason about than none at all.
    if do_codes:
        if not codes_path.is_file():
            sys.exit(f"Error: file not found: {codes_path}")
        rows, errs = read_sheet(codes_path)
        errors += errs
    if do_regimes:
        if not regimes_path.is_file():
            sys.exit(f"Error: file not found: {regimes_path}")
        regimes, errs = read_regimes(regimes_path)
        errors += errs

    if errors:
        log.error("%d validation error(s) - nothing was imported:", len(errors))
        for e in errors:
            log.error("    %s", e)
        return 1

    if do_codes:
        log.info("Validated %s [%s]: %d procedures, %d steps",
                 codes_path.name, SHEET, len(rows), sum(len(r["steps"]) for r in rows))
        stepless = [r["procedure_code"] for r in rows if not r["steps"]]
        if stepless:
            log.info("No steps (as the sheet has them): %s", ", ".join(stepless))
    if do_regimes:
        log.info("Validated %s: %d regimes, %d steps (%d conditional, %d reference, %d decision)",
                 regimes_path.name, len(regimes),
                 sum(len(r["steps"]) for r in regimes),
                 sum(1 for r in regimes for s in r["steps"] if s["step_type"] == "CONDITIONAL"),
                 sum(1 for r in regimes for s in r["steps"] if s["step_type"] == "REFERENCE"),
                 sum(1 for r in regimes for s in r["steps"] if s["step_type"] == "DECISION"))

    if args.dry_run:
        for r in rows:
            log.info("  %-6s %-34s steps=%d", r["procedure_code"],
                     r["template_name"] or "", len(r["steps"]))
        for r in regimes:
            log.info("  regime %-4s steps=%-3d instr2=%s", r["regime_code"],
                     len(r["steps"]), "yes" if r["instruction_2"] else "no")
        log.info("--dry-run: inputs are valid, no database changes made.")
        return 0

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        sys.exit("Error: DATABASE_URL not set (checked .env).")

    conn = psycopg2.connect(db_url)
    try:
        with conn.cursor() as cur:
            # Both inputs are extracts of the same document, so the source is
            # resolved from whichever one is being imported - unless --source
            # names it, which run_oil.sh does because the filenames no longer
            # pick one Shell guide over the other.
            source_id = (source_by_name(cur, args.source) if args.source
                         else resolve_source(cur, str(regimes_path if do_regimes
                                                      else codes_path)))

            n_tpl = n_steps = 0
            if do_codes:
                n_tpl, n_steps = run_import(cur, source_id, rows)

            counts = {}
            if do_regimes:
                counts = run_regime_import(cur, source_id, regimes)

            log.info("=" * 62)
            if do_codes:
                log.info("letter codes   templates %3d   steps %3d", n_tpl, n_steps)
            if do_regimes:
                log.info("regimes        templates %3d   steps %3d", counts["templates"], counts["steps"])
                log.info("               reference %3d   conditional %3d   decision %3d",
                         counts["reference"], counts["conditional"], counts["decision"])
                log.info("               instructions %3d  notes %d  note links %d",
                         counts["instructions"], counts["notes"], counts["note_links"])
            log.info("cargo_type     : %s on every row", CARGO_TYPE)
            log.info("=" * 62)
            conn.commit()
            log.info("✓ Committed.")
    except Exception:
        conn.rollback()
        log.exception("Import failed - rolled back")
        raise
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
