#!/usr/bin/env python3
"""
Load the Shell cleaning matrix's 30 regimes into procedure_templates +
procedure_template_steps.

Source: "Shell Cleaning Matrix" (source.json, category 'oil').

WHAT THIS FILE IS
-----------------
    regime_code | simple_statement | compatibility | compatibility_note
                | procedure_steps  | water_type    | original_instruction_1
                | original_instruction_2

30 rows, codes 1-30, no gaps and no duplicates. Each is one cleaning regime the
cargo-to-cargo matrix points at by code, so this file is the matrix's legend:
without it every one of those 784 cells is a number with no meaning.

WHERE THE COLUMNS GO
--------------------
    regime_code             -> procedure_templates.procedure_code
    "Cleaning Regime <n>"   -> procedure_templates.template_name
    simple_statement        -> procedure_templates.description
    water_type              -> procedure_templates.water_type
    compatibility           -> procedure_templates.loading_allowed
    compatibility_note      -> procedure_templates.notes
    original_instruction_1  -> procedure_templates.source_definition
    original_instruction_2  -> appended to source_definition
    procedure_steps         -> procedure_template_steps, one row per line

cargo_type is OIL on every row.

BOTH ORIGINAL INSTRUCTIONS ARE KEPT, JOINED
--------------------------------------------
16 regimes carry a second instruction paragraph. source_definition exists to
hold "the procedure exactly as the source document words it" against the
normalised steps, so dropping the second paragraph would defeat the column's
whole purpose. The two are joined with a blank line, in the file's order, and
the join is the only edit made to either.

THE STEP CELL
-------------
A numbered list with real newlines inside one quoted CSV field:

    1. Well drain tank, pump columns and pipe lines.
    2. Machine wash the tank with cold sea water.

98 lines across the 30 regimes, every one numbered, and every regime numbered
1..n with no gaps - checked on load, not assumed. The line's own number is
step_order and the sentence is stored VERBATIM in step_description.

step_name is NOT NULL and the file gives no short label, so one is derived by
the rule table below, which also supplies step_type. The rules are matched IN
ORDER, first match wins, and a sentence matching NONE of them is a hard error
naming the sentence and its regime. A new wording must be read by a person; a
loader that guessed would file an instruction under the wrong kind silently.
All 55 distinct sentences in this file are covered, and every rule is used.

WHY ROLE TYPES AND NOT PHYSICAL ONES
-------------------------------------
CleaningStepType offers both physical operations (CLEANING, DRAINING, MOPPING)
and role values (MAIN, CONDITIONAL, DECISION, REFERENCE). This file gets role
values, for the reason the enum records them: most of its sentences are not one
operation. "Machine wash with cold sea water; flush lines; drain." is three,
and picking one of the three as the row's type would misfile the other two.
What IS unambiguous per sentence is the part it plays in its regime, which is
what a role value states.

The wash water a sentence names is still captured, in `medium`, which is a
separate column and does not have to compete with the step's role.

CONDITIONS ARE QUOTED, NOT INTERPRETED
---------------------------------------
A CONDITIONAL or DECISION step keeps its leading clause in `condition`, cut at
the first colon and otherwise verbatim: "If chemical grade", "If tank coatings
are in good condition". A sentence with no colon keeps the whole sentence.
Narrowing "If discharged gasoline had no oxygenates (MTBE/ETBE)" to a label
like "no_oxygenates" is how a specific condition becomes a wrong one.

FOUR REGIMES ARE NOT CLEANING PROCEDURES
-----------------------------------------
    7   refer to the black oil matrix / OTS/321
    21  no regime available; refer to OTS/321
    24  NOT compatible - do not load
    29  input error; re-check the grade selection

They are loaded as templates because the matrix points at them by code and a
code with no row would leave those cells dangling. What distinguishes them:

  * `compatibility` is FALSE on 24 and 'N/A' on 7, 21 and 29. loading_allowed
    is false for FALSE and NULL for 'N/A' - NULL because "refer elsewhere" and
    "you mis-selected" are not permissions to load OR refusals, and recording
    either as a boolean would be inventing an answer the source withholds.
  * `water_type` is 'N/A' on exactly these four rows and nowhere else, which is
    the file saying there is no wash medium because there is no wash. Stored as
    NULL, with a note. Every other value is kept verbatim, including "None" and
    "None (drain only)" - those describe a real procedure that uses no water,
    which is a different statement from "this is not a procedure".

IDEMPOTENCY
-----------
Upsert on (source_id, procedure_code) and (procedure_templates_id, step_order).
Step slots no longer in the file are deleted, so shortening a regime shortens
it in the database instead of leaving a tail behind. The whole file is
validated before anything is written; one transaction.

Usage:
    python3 etl/oil/shell_cleaning_matrix_regimes.py
    python3 etl/oil/shell_cleaning_matrix_regimes.py --dry-run
    python3 etl/oil/shell_cleaning_matrix_regimes.py <file>
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
from dotenv import load_dotenv

# Loaders are run as scripts, so only their own directory is on sys.path.
_ETL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ETL_ROOT / "common"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _crude_oil import get_source_id  # noqa: E402
from _paths import input_file  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("shell_cleaning_matrix_regimes")

SOURCE_NAME = "Shell Cleaning Matrix"
DEFAULT_FILE = input_file("Shell_cleaning_matrix_regimes_updated.csv")
CARGO_TYPE = "OIL"

COL_CODE = "regime_code"
COL_SIMPLE = "simple_statement"
COL_COMPAT = "compatibility"
COL_COMPAT_NOTE = "compatibility_note"
COL_STEPS = "procedure_steps"
COL_WATER = "water_type"
COL_INSTR1 = "original_instruction_1"
COL_INSTR2 = "original_instruction_2"
EXPECTED_HEADER = [COL_CODE, COL_SIMPLE, COL_COMPAT, COL_COMPAT_NOTE,
                   COL_STEPS, COL_WATER, COL_INSTR1, COL_INSTR2]

# A numbered line in the step cell: "2. Machine wash the tank with cold sea water."
STEP_LINE = re.compile(r"^\s*(\d+)\s*[.)]\s*(.+?)\s*$")

# The file's own marker for "this question does not apply", used on exactly the
# four rows that are not cleaning procedures. See the header.
NOT_APPLICABLE = "n/a"

NOT_A_PROCEDURE_NOTE = (
    "The file prints 'N/A' as this regime's water type. That is not a missing "
    "value: these are the rows that are not cleaning procedures at all, so "
    "there is no wash medium because there is no wash. Stored as NULL.")
NO_VERDICT_NOTE = (
    "The file prints 'N/A' for this regime's compatibility. It neither permits "
    "nor refuses the transition - it sends the reader elsewhere, or reports a "
    "selection error - so loading_allowed is NULL rather than a boolean the "
    "source never stated.")
NOT_COMPATIBLE_NOTE = (
    "The file marks this regime NOT compatible: it is a refusal to load, not a "
    "cleaning procedure. loading_allowed is false.")

# (pattern, step_name, step_type), matched IN ORDER against the lower-cased
# sentence, first match wins. Order is load-bearing: the conditional forms all
# begin "If ..." and must be tried before the plain operations they contain, or
# "If chemical grade: machine wash ..." would be recorded as an ordinary
# mandatory wash and its condition lost.
#
# The sentence is stored VERBATIM as step_description; these rules supply only
# the short step_name (NOT NULL, absent from the file) and the role.
STEP_RULES: List[Tuple[str, str, str]] = [
    # --- decisions: a load / do-not-load determination, not an action ------
    (r"^do not load\b",                         "Do Not Load",                  "DECISION"),
    (r"^for any other use: do not load",        "Do Not Load (Other Use)",      "DECISION"),
    (r"^not a cleaning regime",                 "Selection Error",              "DECISION"),
    (r"^confirm the grade to load.*do not load", "Confirm Grade / Do Not Load", "DECISION"),
    (r"^confirm intended use",                  "Confirm Intended Use",         "DECISION"),
    (r"^check condensate colour",               "Check Condensate Colour",      "DECISION"),
    # --- references: defer to another document ----------------------------
    (r"^refer to ",                             "Refer To Other Guidance",      "REFERENCE"),
    # --- conditionals: apply only when the leading clause holds ------------
    (r"^if ig fitted",                          "Purge Or Gas Free",            "CONDITIONAL"),
    (r"^if .*bottom flush may be used",         "Bottom Flush Alternative",     "CONDITIONAL"),
    (r"^if .*mopping not required",             "Mopping Not Required",         "CONDITIONAL"),
    (r"^if .*wash water should be",             "Wash Water Requirement",       "CONDITIONAL"),
    (r"^if .*use hot fresh wash water",         "Chemical Grade Wash And Dry",  "CONDITIONAL"),
    (r"^if .*machine wash",                     "Conditional Machine Wash",     "CONDITIONAL"),
    (r"^if .*well drain",                       "Conditional Well Drain",       "CONDITIONAL"),
    (r"^if .*prepare for entry",                "Conditional Prepare And Dry",  "CONDITIONAL"),
    (r"^in that case",                          "Conditional Prepare And Dry",  "CONDITIONAL"),
    # --- main sequence ------------------------------------------------------
    (r"^no action required",                    "No Action Required",           "MAIN"),
    (r"^well drain",                            "Well Drain",                   "MAIN"),
    (r"^keep any rob",                          "ROB In Pump Well",             "MAIN"),
    (r"^ensure rob does not exceed",            "ROB Limit",                    "MAIN"),
    (r"^clear and drain pump columns",          "Clear And Drain Lines",        "MAIN"),
    (r"^bottom flush or machine wash",          "Bottom Flush Or Machine Wash", "MAIN"),
    (r"^(cold |hot )?machine wash",             "Machine Wash",                 "MAIN"),
    (r"^water flush",                           "Water Flush Lines",            "MAIN"),
    (r"^drain tank and lines",                  "Drain Tank And Lines",         "MAIN"),
    (r"^prepare tanks? for entry",              "Prepare Tanks For Entry",      "MAIN"),
    (r"^prepare for entry",                     "Prepare And Dry",              "MAIN"),
    (r"^meticulously drain",                    "Drain Pumps And Mop Dry",      "MAIN"),
]
_COMPILED = [(re.compile(p), n, t) for p, n, t in STEP_RULES]

# Only these roles quote the source's condition wording. A MAIN or REFERENCE
# step is unconditional and leaves `condition` NULL.
CONDITION_BEARING = {"CONDITIONAL", "DECISION"}

# The wash water a sentence names, most specific first: "cold sea water" must
# be tried before "sea water" or the temperature would be dropped.
MEDIA = [
    (r"cold fresh (?:wash )?water", "Cold Fresh Water"),
    (r"hot fresh (?:wash )?water",  "Hot Fresh Water"),
    (r"cold sea[- ]?(?:wash )?water", "Cold Sea Water"),
    (r"hot sea[- ]?(?:wash )?water",  "Hot Sea Water"),
    (r"sea[- ]?water",              "Sea Water"),
    (r"fresh water",                "Fresh Water"),
]
_MEDIA = [(re.compile(p), m) for p, m in MEDIA]


def clean(text: Optional[str]) -> str:
    """Trim and collapse runs of spaces, keeping newlines - the step cell needs
    them to split into lines."""
    return re.sub(r"[ \t]+", " ", (text or "").replace(" ", " ")).strip()


def classify(sentence: str) -> Optional[Tuple[str, str]]:
    low = sentence.lower()
    for rx, name, step_type in _COMPILED:
        if rx.search(low):
            return name, step_type
    return None


def medium_of(sentence: str) -> Optional[str]:
    low = sentence.lower()
    for rx, medium in _MEDIA:
        if rx.search(low):
            return medium
    return None


def condition_of(sentence: str) -> str:
    """The leading clause, cut at the first colon. Verbatim otherwise."""
    head, sep, _ = sentence.partition(":")
    return head.strip() if sep else sentence.strip()


def loading_allowed(raw: str) -> Tuple[Optional[bool], Optional[str]]:
    """(loading_allowed, note) from the compatibility cell. See the header."""
    value = raw.strip().upper()
    if value == "TRUE":
        return True, None
    if value == "FALSE":
        return False, NOT_COMPATIBLE_NOTE
    return None, NO_VERDICT_NOTE


def read_file(path: Path) -> Tuple[List[dict], List[str]]:
    errors: List[str] = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        raw = list(csv.reader(fh))
    if not raw:
        return [], [f"{path.name} is empty"]

    header = [clean(c) for c in raw[0]]
    if header != EXPECTED_HEADER:
        return [], [f"unexpected header.\n        found    {header!r}\n"
                    f"        expected {EXPECTED_HEADER!r}"]
    index = {name: i for i, name in enumerate(header)}

    regimes: List[dict] = []
    seen: Dict[str, int] = {}
    for line_no, row in enumerate(raw[1:], start=2):
        if not any(clean(c) for c in row):
            continue
        if len(row) != len(header):
            errors.append(f"line {line_no}: {len(row)} field(s), expected {len(header)}")
            continue

        get = lambda name: clean(row[index[name]])  # noqa: E731
        code = get(COL_CODE)
        if not code:
            errors.append(f"line {line_no}: no {COL_CODE!r}")
            continue
        if code in seen:
            errors.append(f"line {line_no}: regime {code!r} already appeared on "
                          f"line {seen[code]}; (source_id, procedure_code) must "
                          f"identify one template")
            continue
        seen[code] = line_no

        water_raw = get(COL_WATER)
        water = None if water_raw.lower() == NOT_APPLICABLE else (water_raw or None)

        allowed, allowed_note = loading_allowed(get(COL_COMPAT))

        notes = [n for n in (get(COL_COMPAT_NOTE) or None, allowed_note) if n]
        if water_raw.lower() == NOT_APPLICABLE:
            notes.append(NOT_A_PROCEDURE_NOTE)

        definition = "\n\n".join(p for p in (get(COL_INSTR1), get(COL_INSTR2)) if p)

        steps: List[dict] = []
        for raw_line in get(COL_STEPS).split("\n"):
            if not raw_line.strip():
                continue
            m = STEP_LINE.match(raw_line)
            if not m:
                errors.append(f"line {line_no} (regime {code}): step line is not "
                              f"numbered: {raw_line.strip()!r}")
                continue
            sentence = m.group(2).strip()
            hit = classify(sentence)
            if hit is None:
                # A new wording must be classified by a person. See the header.
                errors.append(f"line {line_no} (regime {code}): no rule matches "
                              f"the step {sentence!r}. Add a rule to STEP_RULES.")
                continue
            name, step_type = hit
            steps.append({
                "order": int(m.group(1)),
                "name": name,
                "type": step_type,
                "description": sentence,
                "medium": medium_of(sentence),
                "condition": (condition_of(sentence)
                              if step_type in CONDITION_BEARING else None),
                # Only a CONDITIONAL step is optional: it applies when its
                # condition holds. Everything else is part of the procedure.
                "mandatory": step_type != "CONDITIONAL",
            })

        orders = [s["order"] for s in steps]
        if orders and orders != list(range(1, len(orders) + 1)):
            errors.append(f"line {line_no} (regime {code}): step numbers are "
                          f"{orders}, expected 1..{len(orders)}")
        if not steps:
            errors.append(f"line {line_no} (regime {code}): no steps")

        regimes.append({"line": line_no, "code": code,
                        "name": f"Cleaning Regime {code}",
                        "description": get(COL_SIMPLE) or None,
                        "water_type": water, "loading_allowed": allowed,
                        "notes": " ".join(notes) or None,
                        "definition": definition or None, "steps": steps})

    if not regimes and not errors:
        errors.append(f"{path.name} has a header but no data rows")
    return regimes, errors


def report(regimes: List[dict]) -> None:
    total_steps = sum(len(r["steps"]) for r in regimes)
    log.info("%d regime(s), %d step(s)", len(regimes), total_steps)

    by_type: Dict[str, int] = {}
    for r in regimes:
        for s in r["steps"]:
            by_type[s["type"]] = by_type.get(s["type"], 0) + 1
    log.info("    step types: %s", ", ".join(
        f"{t}={n}" for t, n in sorted(by_type.items(), key=lambda kv: -kv[1])))
    log.info("    %d step(s) name a wash medium",
             sum(1 for r in regimes for s in r["steps"] if s["medium"]))

    special = [r for r in regimes if r["loading_allowed"] is not True]
    log.info("    %d regime(s) are not ordinary cleaning procedures:", len(special))
    for r in special:
        log.info("        regime %-3s loading_allowed=%-5s %s", r["code"],
                 r["loading_allowed"], (r["description"] or "")[:64])
    log.info("    %d regime(s) carry a second original instruction",
             sum(1 for r in regimes if r["definition"] and "\n\n" in r["definition"]))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", default=str(DEFAULT_FILE))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.is_file():
        sys.exit(f"Error: file not found: {path}")
    log.info("file: %s", path)

    regimes, errors = read_file(path)
    if errors:
        log.error("%d validation error(s) - nothing was imported:", len(errors))
        for e in errors:
            log.error("    %s", e)
        return 1

    report(regimes)
    if args.dry_run:
        log.info("--dry-run: nothing written.")
        return 0

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        sys.exit("Error: DATABASE_URL not set (checked .env).")

    conn = psycopg2.connect(db_url)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            source_id = get_source_id(cur, SOURCE_NAME)
            log.info("Source id=%s (%r)", source_id, SOURCE_NAME)

            page_ref = path.name
            templates = steps_written = pruned = 0
            for r in regimes:
                cur.execute(
                    """
                    INSERT INTO procedure_templates
                        (procedure_code, template_name, cargo_type, description,
                         water_type, source_id, source_page_ref,
                         source_definition, loading_allowed, notes,
                         created_at, updated_at)
                    VALUES (%s, %s, %s::"CargoType", %s, %s, %s, %s, %s, %s, %s,
                            now(), now())
                    ON CONFLICT (source_id, procedure_code) DO UPDATE SET
                        template_name     = EXCLUDED.template_name,
                        cargo_type        = EXCLUDED.cargo_type,
                        description       = EXCLUDED.description,
                        water_type        = EXCLUDED.water_type,
                        source_page_ref   = EXCLUDED.source_page_ref,
                        source_definition = EXCLUDED.source_definition,
                        loading_allowed   = EXCLUDED.loading_allowed,
                        notes             = EXCLUDED.notes,
                        updated_at        = now()
                    RETURNING id
                    """,
                    (r["code"], r["name"], CARGO_TYPE, r["description"],
                     r["water_type"], source_id, page_ref, r["definition"],
                     r["loading_allowed"], r["notes"]),
                )
                template_id = cur.fetchone()[0]
                templates += 1

                for s in r["steps"]:
                    cur.execute(
                        """
                        INSERT INTO procedure_template_steps
                            (procedure_templates_id, step_order, step_name,
                             step_type, step_description, medium, mandatory,
                             condition, created_at, updated_at)
                        VALUES (%s, %s, %s, %s::"CleaningStepType", %s, %s, %s,
                                %s, now(), now())
                        ON CONFLICT (procedure_templates_id, step_order) DO UPDATE SET
                            step_name        = EXCLUDED.step_name,
                            step_type        = EXCLUDED.step_type,
                            step_description = EXCLUDED.step_description,
                            medium           = EXCLUDED.medium,
                            mandatory        = EXCLUDED.mandatory,
                            condition        = EXCLUDED.condition,
                            updated_at       = now()
                        """,
                        (template_id, s["order"], s["name"], s["type"],
                         s["description"], s["medium"], s["mandatory"],
                         s["condition"]),
                    )
                    steps_written += 1

                # A regime that lost steps must lose them here too.
                cur.execute(
                    "DELETE FROM procedure_template_steps "
                    " WHERE procedure_templates_id = %s AND step_order > %s",
                    (template_id, len(r["steps"])),
                )
                pruned += cur.rowcount

        conn.commit()
        log.info("✓ Committed. procedure_templates: %d | "
                 "procedure_template_steps: %d written, %d stale slot(s) removed",
                 templates, steps_written, pruned)
        return 0
    except Exception:
        conn.rollback()
        log.exception("Import failed - rolled back, nothing written.")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
