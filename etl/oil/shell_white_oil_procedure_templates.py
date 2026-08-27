#!/usr/bin/env python3
"""
Load the Shell White Oil Tank Cleaning Guide cleaning codes into
procedure_templates + procedure_template_steps + procedure_template_requirement.

SOURCE
------
"Shell White Oil Tank Cleaning Guide" (source.json, category 'oil') - the same
source row as shell_white_oil_cargo.py and shell_white_oil_matrix.py, and a
DIFFERENT one from "Shell Tank Cleaning Guide 2016.pdf": both guides number
their codes 1, 2, 3 ... and the numbers mean different things, so sharing a
source row would have one overwrite the other on (source_id, procedure_code).

Must run BEFORE shell_white_oil_matrix.py, which links its pairs to the
templates defined here.

INPUTS
------
1. "... - procedure templates.xlsx"      9 rows, one per code
2. "... - procedure template steps.xlsx" 40 rows, the ordered steps of each

Both sheets are already database-shaped - they carry an `id` and column names
close to the schema's. Those spreadsheet ids are NOT stored: they are the
extract's own numbering, and procedure_code is the real key. The step sheet is
joined to the template sheet on procedure_code, so a renumbered extract still
loads.

WHAT GOES WHERE
---------------
    procedure_code         -> procedure_templates.procedure_code   (the key)
    template_name          -> procedure_templates.template_name
    description            -> procedure_templates.description AND
                              .source_definition
    water_type             -> procedure_templates.water_type
    source_page_ref        -> procedure_templates.source_page_ref  (see DATES)
    loading_allowed        -> procedure_templates.loading_allowed
    notes + target_purpose -> procedure_templates.notes            (see PURPOSE)
    the flag / default_*
      columns              -> procedure_template_requirement        (see FLAGS)
    every step row         -> procedure_template_steps

cargo_type is OIL on every row: this is a petroleum-cargo guide, and the sheet
says OIL on all 9 rows. `source_name` is checked against the source this loader
writes and then dropped - it identifies the document, it is not data about a
code.

PURPOSE
-------
`target_purpose` is "Cargo transition cleaning" on all 9 rows. It describes the
guide, not the code, and procedure_templates has no column for it, so it is
appended to `notes` verbatim rather than given one. A later extract that varies
it per code will still read back correctly.

FLAGS
-----
procedure_templates had gas_free_required / wall_wash_required /
inert_gas_required / default_* columns once;
20260820120000_procedure_templates_drop_unused_columns dropped them in favour of
procedure_template_requirement, which can also carry values, operators and
units. So each true flag and each filled default_* cell becomes one requirement
row:

  * ventilation_required  -> VENTILATION  (true on code 3)
  * gas_free_required     -> GAS_FREE     (true on code 3)
  * wall_wash_required    -> WALL_WASH    (false on all 9 - nothing written)
  * inert_gas_required    -> INERT_GAS    (false on all 9 - nothing written)
  * default_temperature_c -> TEMPERATURE, value verbatim ("Cold", "Hot")
  * default_duration_hours-> DURATION,    value verbatim
  * default_chemicals     -> CHEMICAL     (empty on all 9 - nothing written)

default_temperature_c holds "Cold" / "Hot", not a number, so the value is stored
as published and no unit is asserted. A false flag and an empty cell state
nothing and produce no row; a later extract that fills them produces requirement
rows the same way.

DATES
-----
`source_page_ref` reaches this loader as a datetime: the extract was typed in a
spreadsheet, which read "2/3" as a date and stored 2026-02-03. The same thing
happened to one procedure code in the matrix sheet. Excel cannot be asked what
was typed, so the two known cells are restored from an explicit table below,
confirmed by the source owner, and anything else that arrives as a date is a
hard error rather than a guess.

IDEMPOTENCY
-----------
Re-running upserts. Keys: (source_id, procedure_code) for templates,
(procedure_templates_id, step_order) for steps,
(procedure_template_id, display_order) for requirements. Rows past the end of
the sheet are deleted, so shortening a procedure shortens it here. Both files
are validated in full before anything is written; one transaction.

Usage:
    python3 etl/oil/shell_white_oil_procedure_templates.py
    python3 etl/oil/shell_white_oil_procedure_templates.py --dry-run
"""

import argparse
import datetime as dt
import logging
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import openpyxl
import psycopg2
from dotenv import load_dotenv

# Loaders are run as scripts (python3 etl/<branch>/<file>.py), so only their
# own directory is on sys.path. Add the shared helpers.
_ETL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ETL_ROOT / "common"))

from _paths import input_file  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("shell_white_oil_templates")

SOURCE_NAME = "Shell White Oil Tank Cleaning Guide"
CARGO_TYPE = "OIL"

DEFAULT_TEMPLATES = input_file(
    "Shell White Oil Tank Cleaning Guide - procedure templates.xlsx")
DEFAULT_STEPS = input_file(
    "Shell White Oil Tank Cleaning Guide - procedure template steps.xlsx")

TEMPLATE_COLS = ["procedure_code", "template_name", "cargo_type", "description",
                 "target_purpose", "water_type", "default_temperature_c",
                 "default_duration_hours", "default_chemicals",
                 "ventilation_required", "gas_free_required", "wall_wash_required",
                 "inert_gas_required", "source_name", "source_page_ref", "notes",
                 "loading_allowed"]
STEP_COLS = ["procedure_code", "step_order", "step_name", "step_type",
             "step_description", "medium", "temperature", "duration", "cleaner",
             "mandatory", "condition"]

# CleaningStepType members this sheet uses. PREPARATION, ASSESSMENT, INSPECTION,
# SPECIAL_CLEANING, TEMPERATURE and NITROGEN are this guide's own vocabulary,
# added by 20260827200000_cleaning_step_type_shell_white_oil_values.
STEP_TYPES = {
    "PRECONDITION", "PRECLEANING", "CLEANING", "RINSING", "FLUSHING", "STEAMING",
    "DRAINING", "DRYING", "VENTILATING", "PURGING", "GAS_FREEING", "MOPPING",
    "MAIN", "REFERENCE", "CONDITIONAL", "DECISION", "WARNING",
    "RESTRICTION", "CONDITION",
    "PREPARATION", "ASSESSMENT", "INSPECTION", "SPECIAL_CLEANING", "TEMPERATURE",
    "NITROGEN",
}

# Cells a spreadsheet turned into dates, and the text that was typed. Restored
# on instruction, never inferred: 2026-02-03 is "2/3" under one locale's
# month/day reading and "3/2" under the other's, and only the person who typed
# it knows which. A date NOT listed here stops the import.
DATE_TEXT: Dict[dt.date, str] = {
    dt.date(2026, 2, 3): "2/3",   # source_page_ref, all 9 template rows
}

# Boolean flag column -> (requirement_type, description). A false flag writes
# nothing: "not required" is the default, not a requirement.
FLAG_REQUIREMENTS: Dict[str, Tuple[str, str]] = {
    "ventilation_required": ("VENTILATION", "Tank must be ventilated."),
    "gas_free_required":    ("GAS_FREE",    "Tank must be gas free."),
    "wall_wash_required":   ("WALL_WASH",   "Tank must pass a wall wash."),
    "inert_gas_required":   ("INERT_GAS",   "Tank must be inerted."),
}

# Valued column -> (requirement_type, description). The value is stored exactly
# as the guide words it ("Cold", "about 4 hrs (2 hrs on vessels with painted
# tanks)"), so no unit is asserted and nothing is parsed into a number.
VALUE_REQUIREMENTS: Dict[str, Tuple[str, str]] = {
    "default_temperature_c":  ("TEMPERATURE", "Wash temperature as published."),
    "default_duration_hours": ("DURATION",    "Wash duration as published."),
    "default_chemicals":      ("CHEMICAL",    "Cleaning chemical as published."),
}


def clean(value) -> Optional[str]:
    """Trim a cell; blank -> None (SQL NULL)."""
    if value is None:
        return None
    s = re.sub(r"\s+", " ", str(value).replace("\n", " ")).strip()
    return s or None


def cell_text(value, where: str, errors: List[str]) -> Optional[str]:
    """clean(), plus the date restoration described under DATES."""
    if isinstance(value, (dt.datetime, dt.date)):
        key = value.date() if isinstance(value, dt.datetime) else value
        text = DATE_TEXT.get(key)
        if text is None:
            errors.append(f"{where}: holds the date {key.isoformat()}, which a "
                          f"spreadsheet made out of typed text. Add the original "
                          f"text to DATE_TEXT in this loader and re-run.")
            return None
        return text
    return clean(value)


def norm_code(value) -> Optional[str]:
    """Codes are whole numbers, so Excel hands 1 back as '1.0'."""
    s = clean(value)
    if s is None:
        return None
    return s[:-2] if re.fullmatch(r"\d+\.0", s) else s


def to_int(value) -> Optional[int]:
    s = clean(value)
    if s is None:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def to_bool(value, default: Optional[bool] = None) -> Optional[bool]:
    """Read the sheet's 1.0 / 0.0 / Yes / No flags."""
    s = clean(value)
    if s is None:
        return default
    return s.lower() in {"1", "1.0", "true", "yes", "y"}


def read_sheet(path: Path, wanted: List[str]) -> List[dict]:
    """Read the first worksheet into dicts keyed by header, or exit on a missing column."""
    ws = openpyxl.load_workbook(path, data_only=True).worksheets[0]
    grid = [list(r) for r in ws.iter_rows(values_only=True)]
    if not grid:
        sys.exit(f"Error: {path.name} is empty")
    header = [clean(c) for c in grid[0]]
    missing = [c for c in wanted if c not in header]
    if missing:
        sys.exit(f"Error: {path.name} is missing column(s): {', '.join(missing)}")
    return [dict(zip(header, row)) for row in grid[1:] if any(c is not None for c in row)]


def resolve_source(cur, name: str) -> int:
    cur.execute("SELECT id FROM source WHERE name = %s", (name,))
    row = cur.fetchone()
    if row is None:
        sys.exit(
            f"Error: source {name!r} not found.\n"
            f"  It is declared in etl/data/source.json - register it with:\n"
            f"      python3 etl/common/source.py"
        )
    return row[0]


def build_templates(rows: List[dict]) -> Tuple[List[dict], List[str]]:
    templates: List[dict] = []
    errors: List[str] = []
    seen: set = set()

    for i, r in enumerate(rows, start=2):
        code = norm_code(r["procedure_code"])
        if code is None:
            continue
        if code in seen:
            errors.append(f"templates row {i}: duplicate procedure_code {code!r}")
            continue
        seen.add(code)

        name = clean(r["template_name"])
        if name is None:
            errors.append(f"templates row {i}: {code!r} has no template_name "
                          f"(the column is NOT NULL)")
            continue

        sheet_type = (clean(r["cargo_type"]) or CARGO_TYPE).upper()
        if sheet_type != CARGO_TYPE:
            errors.append(f"templates row {i}: {code!r} says cargo_type "
                          f"{sheet_type!r}; this loader writes {CARGO_TYPE}")
            continue

        # The sheet names the document it came from. Checked, not stored: the
        # source row is what records it, and a mismatch means this file belongs
        # to another guide.
        sheet_source = clean(r["source_name"])
        if sheet_source is not None and sheet_source != SOURCE_NAME:
            errors.append(f"templates row {i}: source_name is {sheet_source!r}, "
                          f"but this loader writes {SOURCE_NAME!r}")
            continue

        # target_purpose has no column - see PURPOSE. Appended to the sheet's
        # own note rather than replacing it.
        note = clean(r["notes"])
        purpose = clean(r["target_purpose"])
        if purpose:
            note = f"{note} Purpose: {purpose}." if note else f"Purpose: {purpose}."

        requirements: List[Tuple[str, Optional[str], str]] = []
        for col, (rtype, description) in FLAG_REQUIREMENTS.items():
            if to_bool(r.get(col), False):
                requirements.append((rtype, None, description))
        for col, (rtype, description) in VALUE_REQUIREMENTS.items():
            value = clean(r.get(col))
            if value:
                requirements.append((rtype, value, description))

        description = clean(r["description"])
        templates.append({
            "procedure_code": code,
            "template_name": name,
            "description": description,
            # The sheet's description IS the guide's wording for this code, so
            # it is both the readable description and the verbatim definition.
            "source_definition": description,
            "water_type": clean(r["water_type"]),
            "loading_allowed": to_bool(r["loading_allowed"], True),
            "source_page_ref": cell_text(r["source_page_ref"],
                                         f"templates row {i} source_page_ref",
                                         errors),
            "notes": note,
            "requirements": requirements,
        })
    return templates, errors


def build_steps(rows: List[dict], known: set) -> Tuple[Dict[str, List[dict]], List[str]]:
    by_code: Dict[str, List[dict]] = {}
    errors: List[str] = []

    for i, r in enumerate(rows, start=2):
        code = norm_code(r["procedure_code"])
        if code is None:
            continue
        if code not in known:
            errors.append(f"steps row {i}: steps for {code!r}, which the template "
                          f"sheet does not define")
            continue

        order = to_int(r["step_order"])
        if order is None:
            errors.append(f"steps row {i}: {code!r} has no usable step_order")
            continue

        name = clean(r["step_name"])
        if name is None:
            errors.append(f"steps row {i}: {code!r} step {order} has no step_name "
                          f"(the column is NOT NULL)")
            continue

        step_type = clean(r["step_type"])
        if step_type is not None:
            step_type = step_type.upper()
            if step_type not in STEP_TYPES:
                errors.append(f"steps row {i}: {code!r} step {order} has step_type "
                              f"{step_type!r}, which is not a CleaningStepType value")
                continue

        by_code.setdefault(code, []).append({
            "step_order": order,
            "step_name": name,
            "step_type": step_type,
            "step_description": clean(r["step_description"]),
            "medium": clean(r["medium"]),
            "temperature": clean(r["temperature"]),
            "duration": clean(r["duration"]),
            "cleaner": clean(r["cleaner"]),
            "mandatory": to_bool(r["mandatory"], True),
            "condition": clean(r["condition"]),
        })

    for code, steps in by_code.items():
        steps.sort(key=lambda s: s["step_order"])
        orders = [s["step_order"] for s in steps]
        if len(set(orders)) != len(orders):
            errors.append(f"{code!r}: duplicate step_order value(s) {orders}")
        elif orders != list(range(1, len(orders) + 1)):
            errors.append(f"{code!r}: step_order is {orders}, expected a 1-based "
                          f"run with no gaps")
    return by_code, errors


def upsert_template(cur, source_id: int, t: dict) -> int:
    cur.execute(
        """
        INSERT INTO procedure_templates
            (procedure_code, template_name, cargo_type, description, water_type,
             source_id, source_page_ref, source_definition, loading_allowed,
             notes, created_at, updated_at)
        VALUES (%s, %s, %s::"CargoType", %s, %s, %s, %s, %s, %s, %s, now(), now())
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
        (t["procedure_code"], t["template_name"], CARGO_TYPE, t["description"],
         t["water_type"], source_id, t["source_page_ref"], t["source_definition"],
         t["loading_allowed"], t["notes"]),
    )
    return cur.fetchone()[0]


def sync_steps(cur, template_id: int, steps: List[dict]) -> None:
    for s in steps:
        cur.execute(
            """
            INSERT INTO procedure_template_steps
                (procedure_templates_id, step_order, step_name, step_type,
                 step_description, medium, temperature, duration, cleaner,
                 mandatory, condition, created_at, updated_at)
            VALUES (%s, %s, %s, %s::"CleaningStepType", %s, %s, %s, %s, %s, %s, %s,
                    now(), now())
            ON CONFLICT (procedure_templates_id, step_order) DO UPDATE SET
                step_name        = EXCLUDED.step_name,
                step_type        = EXCLUDED.step_type,
                step_description = EXCLUDED.step_description,
                medium           = EXCLUDED.medium,
                temperature      = EXCLUDED.temperature,
                duration         = EXCLUDED.duration,
                cleaner          = EXCLUDED.cleaner,
                mandatory        = EXCLUDED.mandatory,
                condition        = EXCLUDED.condition,
                updated_at       = now()
            """,
            (template_id, s["step_order"], s["step_name"], s["step_type"],
             s["step_description"], s["medium"], s["temperature"], s["duration"],
             s["cleaner"], s["mandatory"], s["condition"]),
        )
    cur.execute(
        "DELETE FROM procedure_template_steps "
        "WHERE procedure_templates_id = %s AND step_order > %s",
        (template_id, len(steps)),
    )


def sync_requirements(cur, template_id: int,
                      reqs: List[Tuple[str, Optional[str], str]]) -> int:
    for order, (rtype, value, description) in enumerate(reqs, start=1):
        cur.execute(
            """
            INSERT INTO procedure_template_requirement
                (procedure_template_id, requirement_type, requirement_value,
                 operator, unit, mandatory, description, display_order,
                 created_at, updated_at)
            VALUES (%s, %s, %s, NULL, NULL, true, %s, %s, now(), now())
            ON CONFLICT (procedure_template_id, display_order) DO UPDATE SET
                requirement_type  = EXCLUDED.requirement_type,
                requirement_value = EXCLUDED.requirement_value,
                description       = EXCLUDED.description,
                mandatory         = EXCLUDED.mandatory,
                updated_at        = now()
            """,
            (template_id, rtype, value, description, order),
        )
    cur.execute(
        "DELETE FROM procedure_template_requirement "
        "WHERE procedure_template_id = %s AND display_order > %s",
        (template_id, len(reqs)),
    )
    return len(reqs)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--templates-file", default=str(DEFAULT_TEMPLATES))
    ap.add_argument("--steps-file", default=str(DEFAULT_STEPS))
    ap.add_argument("--dry-run", action="store_true",
                    help="validate both sheets and report; write nothing")
    args = ap.parse_args()

    tpath, spath = Path(args.templates_file), Path(args.steps_file)
    for p in (tpath, spath):
        if not p.is_file():
            sys.exit(f"Error: file not found: {p}")

    # Validate EVERYTHING first: a half-imported pair of sheets is harder to
    # reason about than none at all.
    templates, errors = build_templates(read_sheet(tpath, TEMPLATE_COLS))
    known = {t["procedure_code"] for t in templates}
    steps, errs = build_steps(read_sheet(spath, STEP_COLS), known)
    errors += errs

    if errors:
        log.error("%d validation error(s) - nothing was imported:", len(errors))
        for e in errors:
            log.error("    %s", e)
        return 1

    n_steps = sum(len(v) for v in steps.values())
    n_reqs = sum(len(t["requirements"]) for t in templates)
    log.info("%s: %d code(s)", tpath.name, len(templates))
    log.info("%s: %d step(s) across %d code(s)", spath.name, n_steps, len(steps))
    log.info("requirements from the flag / default_* columns: %d", n_reqs)
    stepless = sorted(c for c in known if not steps.get(c))
    if stepless:
        log.warning("code(s) with no steps in the sheet: %s", ", ".join(stepless))
    blocked = sorted(t["procedure_code"] for t in templates if not t["loading_allowed"])
    if blocked:
        log.info("loading_allowed = false: %s", ", ".join(blocked))

    if args.dry_run:
        for t in templates:
            log.info("  %-4s %-44s steps=%2d reqs=%d allowed=%s page=%s",
                     t["procedure_code"], t["template_name"][:44],
                     len(steps.get(t["procedure_code"], [])),
                     len(t["requirements"]), t["loading_allowed"],
                     t["source_page_ref"])
        log.info("--dry-run: inputs are valid, nothing written.")
        return 0

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        sys.exit("Error: DATABASE_URL not set (checked .env).")

    conn = psycopg2.connect(db_url)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            source_id = resolve_source(cur, SOURCE_NAME)
            log.info("Source id=%s (%r)", source_id, SOURCE_NAME)

            written_reqs = 0
            for t in templates:
                tid = upsert_template(cur, source_id, t)
                sync_steps(cur, tid, steps.get(t["procedure_code"], []))
                written_reqs += sync_requirements(cur, tid, t["requirements"])

        conn.commit()
        log.info("✓ Committed. procedure_templates: %d | procedure_template_steps: %d "
                 "| procedure_template_requirement: %d | cargo_type=%s",
                 len(templates), n_steps, written_reqs, CARGO_TYPE)
        return 0
    except Exception:
        conn.rollback()
        log.exception("Import failed - rolled back, nothing written.")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
