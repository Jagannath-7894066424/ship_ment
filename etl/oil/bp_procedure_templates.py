#!/usr/bin/env python3
"""
Load the BP Tank Cleaning Guide cleaning codes into procedure_templates +
procedure_template_steps + procedure_template_requirement.

SOURCE
------
"BP Tank Cleaning Guide" (source.json, category 'oil'). Four spreadsheets are
extracts of that ONE document, so every BP loader RESOLVES the existing source
row and none of them creates one:

    this loader          templates + steps  -> procedure_templates, steps, requirements
    bp_cargo.py          cargo list         -> crude_oil, crude_oil_property_values
    bp_cargo_matrix.py   from->to matrix    -> cleaning_process

INPUTS
------
1. "... - procedure templates.xlsx"      14 rows, one per code
2. "... - procedure template steps.xlsx" 27 rows, the ordered steps of each

Both sheets are already database-shaped - they carry an `id`, a
`procedure_templates_id` and column names close to the schema's. Those
spreadsheet ids are NOT stored: they are the extract's own numbering, and
procedure_code is the real key. The step sheet is joined to the template sheet
on procedure_code, not on procedure_templates_id, so a renumbered extract still
loads.

WHAT GOES WHERE
---------------
    procedure_code    -> procedure_templates.procedure_code   (the key)
    template_name     -> procedure_templates.template_name
    description       -> procedure_templates.description AND .source_definition
    water_type        -> procedure_templates.water_type
    loading_allowed   -> procedure_templates.loading_allowed  (false for BLACK)
    colour_name +
    source_symbol     -> procedure_templates.notes            (see COLOURS)
    gas_free_required -> procedure_template_requirement       (see FLAGS)
    every step row    -> procedure_template_steps

cargo_type is OIL on every row: BP publishes a petroleum-cargo guide, and the
sheet says OIL on all 14 rows.

COLOURS
-------
BP keys its matrix on a COLOUR plus an optional symbol - a grey cell, a cyan
cell with "PM" on it. The extract flattens that into procedure_code (GREY,
CYAN_PM), which is what everything joins on, so colour_name and source_symbol
carry no information the code does not already hold. They are kept verbatim in
`notes` as the legend's own rendering rather than given columns of their own:
procedure_templates is shared by every guide, and a colour is BP's device.

FLAGS
-----
The extract also has gas_free_required / wall_wash_required /
inert_gas_required / default_temperature_c / default_duration_hours /
default_chemicals. procedure_templates had exactly these columns once and
20260820120000_procedure_templates_drop_unused_columns dropped them in favour of
procedure_template_requirement, which can also express limits and disjunctions.
So:

  * gas_free_required is true on the six RED (hot wash) templates -> one
    requirement row each, requirement_type GAS_FREE.
  * wall_wash_required and inert_gas_required are false on all 14 rows, and the
    three default_* columns are empty on all 14. They state nothing, so nothing
    is written; a later extract that fills them will produce requirement rows
    the same way.

IDEMPOTENCY
-----------
Re-running upserts. Keys: (source_id, procedure_code) for templates,
(procedure_templates_id, step_order) for steps,
(procedure_template_id, display_order) for requirements. Rows past the end of
the sheet are deleted, so shortening a procedure shortens it here. Both files
are validated in full before anything is written; one transaction.

Usage:
    python3 etl/oil/bp_procedure_templates.py
    python3 etl/oil/bp_procedure_templates.py --dry-run
"""

import argparse
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
log = logging.getLogger("bp_templates")

SOURCE_NAME = "BP Tank Cleaning Guide"
CARGO_TYPE = "OIL"

DEFAULT_TEMPLATES = input_file("BP Tank Cleaning Guide - procedure templates.xlsx")
DEFAULT_STEPS = input_file("BP Tank Cleaning Guide - procedure template steps.xlsx")

TEMPLATE_COLS = ["procedure_code", "template_name", "cargo_type", "description",
                 "colour_name", "source_symbol", "water_type", "gas_free_required",
                 "wall_wash_required", "inert_gas_required", "source_page_ref",
                 "notes", "loading_allowed"]
STEP_COLS = ["procedure_code", "step_order", "step_name", "step_type",
             "step_description", "medium", "temperature", "duration", "cleaner",
             "mandatory", "condition"]

# CleaningStepType members this sheet may use. RESTRICTION and CONDITION are
# BP's own vocabulary, added by 20260826000000_cleaning_step_type_bp_values.
STEP_TYPES = {
    "PRECONDITION", "PRECLEANING", "CLEANING", "RINSING", "FLUSHING", "STEAMING",
    "DRAINING", "DRYING", "VENTILATING", "PURGING", "GAS_FREEING", "MOPPING",
    "MAIN", "REFERENCE", "CONDITIONAL", "DECISION", "WARNING",
    "RESTRICTION", "CONDITION",
}

# Extract flag column -> the requirement it becomes when true. The three
# default_* columns are absent here deliberately: they are empty on every row,
# and a requirement with no value states nothing.
FLAG_REQUIREMENTS: Dict[str, Tuple[str, str]] = {
    "gas_free_required":   ("GAS_FREE",  "Tank must be gas free."),
    "wall_wash_required":  ("WALL_WASH", "Tank must pass a wall wash."),
    "inert_gas_required":  ("INERT_GAS", "Tank must be inerted."),
}


def clean(value) -> Optional[str]:
    """Trim a cell; blank -> None (SQL NULL)."""
    if value is None:
        return None
    s = re.sub(r"\s+", " ", str(value).replace("\n", " ")).strip()
    return s or None


def to_int(value) -> Optional[int]:
    """Excel hands back whole numbers as floats, so 1 arrives as '1.0'."""
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
        code = clean(r["procedure_code"])
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

        # colour_name and source_symbol have no column - see COLOURS. Appended
        # to the sheet's own note rather than replacing it.
        colour, symbol = clean(r["colour_name"]), clean(r["source_symbol"])
        legend = "BP legend: colour " + (colour or "unspecified")
        if symbol:
            legend += f", symbol {symbol}"
        legend += "."
        note = clean(r["notes"])
        note = f"{note} {legend}" if note else legend

        description = clean(r["description"])
        requirements = [
            (FLAG_REQUIREMENTS[col][0], FLAG_REQUIREMENTS[col][1])
            for col in FLAG_REQUIREMENTS if to_bool(r.get(col), False)
        ]

        templates.append({
            "procedure_code": code,
            "template_name": name,
            "description": description,
            # The sheet's description IS the guide's wording for this code, so
            # it is both the readable description and the verbatim definition.
            "source_definition": description,
            "water_type": clean(r["water_type"]),
            "loading_allowed": to_bool(r["loading_allowed"], True),
            "source_page_ref": str(to_int(r["source_page_ref"]))
                               if to_int(r["source_page_ref"]) is not None else None,
            "notes": note,
            "requirements": requirements,
        })
    return templates, errors


def build_steps(rows: List[dict], known: set) -> Tuple[Dict[str, List[dict]], List[str]]:
    by_code: Dict[str, List[dict]] = {}
    errors: List[str] = []

    for i, r in enumerate(rows, start=2):
        code = clean(r["procedure_code"])
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


def sync_requirements(cur, template_id: int, reqs: List[Tuple[str, str]]) -> int:
    for order, (rtype, description) in enumerate(reqs, start=1):
        cur.execute(
            """
            INSERT INTO procedure_template_requirement
                (procedure_template_id, requirement_type, requirement_value,
                 operator, unit, mandatory, description, display_order,
                 created_at, updated_at)
            VALUES (%s, %s, NULL, NULL, NULL, true, %s, %s, now(), now())
            ON CONFLICT (procedure_template_id, display_order) DO UPDATE SET
                requirement_type  = EXCLUDED.requirement_type,
                requirement_value = EXCLUDED.requirement_value,
                description       = EXCLUDED.description,
                mandatory         = EXCLUDED.mandatory,
                updated_at        = now()
            """,
            (template_id, rtype, description, order),
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
    log.info("requirements from the extract's flag columns: %d", n_reqs)
    stepless = sorted(c for c in known if not steps.get(c))
    if stepless:
        log.warning("code(s) with no steps in the sheet: %s", ", ".join(stepless))
    blocked = sorted(t["procedure_code"] for t in templates if not t["loading_allowed"])
    if blocked:
        log.info("loading_allowed = false: %s", ", ".join(blocked))

    if args.dry_run:
        for t in templates:
            log.info("  %-9s %-26s steps=%d reqs=%d allowed=%s",
                     t["procedure_code"], t["template_name"],
                     len(steps.get(t["procedure_code"], [])),
                     len(t["requirements"]), t["loading_allowed"])
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
