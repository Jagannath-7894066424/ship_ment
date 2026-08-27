#!/usr/bin/env python3
"""
Load the Energy Institute HM 50 cleaning codes into
procedure_templates + procedure_template_steps.

SOURCE
------
"HM Tank Cleaning Guide By Energy Institute" (source.json, category 'oil').
Four spreadsheets are extracts of that ONE document, so every HM 50 loader
RESOLVES the existing source row and none of them creates one:

    this loader            legend + steps        -> procedure_templates, steps
    hm50_cargo_matrix.py   from->to matrix       -> crude_oil, cleaning_process
    hm50_cargo_guidance.py per-cargo narrative   -> crude_oil_property_values

INPUTS
------
1. "... - procedure templates.xlsx"      Code | HM 50 definition | Operational interpretation
2. "... - procedure template steps.xlsx" Procedure Code | Step Order | Step Name | Step Type |
                                         Step Description | Medium | Temperature | Duration |
                                         Cleaner | Mandatory | Condition | Reference Template Code

WHAT GOES WHERE
---------------
    Code                        -> procedure_templates.procedure_code
    HM 50 definition            -> procedure_templates.source_definition  (VERBATIM)
    Operational interpretation  -> procedure_templates.description
    (derived, see TEMPLATE_NAMES)-> procedure_templates.template_name
    (derived, see WATER_TYPES)  -> procedure_templates.water_type
    every step row              -> procedure_template_steps

cargo_type is OIL on every row: HM 50 is a petroleum-cargo guide, so its codes
are written against the oil master.

X and X* ARE NOT PROCEDURES. They are decisions - "not to be loaded without
special cleaning instructions" - so they get loading_allowed = false and no
steps. The steps sheet correctly has no rows for them; that is not a gap.

COMPOSITE CODES
---------------
The matrix uses four codes the legend never lists as entries of their own:
2M, 2P, 3M and 3P (143 of its 443 cells). They are not an extraction error -
the legend's own text refers to them, "use code 2M" under code 1 and "Otherwise
apply code 3M" under LU - so HM 50 treats them as a base wash plus a modifier.

They are built here, from COMPOSITE_CODES, as templates whose steps are
REFERENCE steps pointing at their parts. Nothing is copied or paraphrased:
source_definition stays NULL because the legend prints none, and each row says
in `notes` that the loader composed it. Change one part and every composite
that references it follows.

TEMPLATE NAMES
--------------
The legend publishes no short name and template_name is NOT NULL, so the names
come from TEMPLATE_NAMES below. A code that is not in that table is a HARD
ERROR naming the code: a new code has to be named by a person, never by a
default that quietly files it as "Procedure X".

IDEMPOTENCY
-----------
Re-running upserts. Keys: (source_id, procedure_code) for templates,
(procedure_templates_id, step_order) for steps. Step slots past the end of the
sheet are deleted, so shortening a procedure in Excel shortens it here. Both
files are validated in full before anything is written; one transaction.

Usage:
    python3 etl/oil/hm50_procedure_templates.py
    python3 etl/oil/hm50_procedure_templates.py --dry-run
    python3 etl/oil/hm50_procedure_templates.py --templates-file X --steps-file Y
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
from dotenv import load_dotenv

# Loaders are run as scripts (python3 etl/<branch>/<file>.py), so only their
# own directory is on sys.path. Add the shared helpers.
_ETL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ETL_ROOT / "common"))

from _paths import input_file  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("hm50_templates")

SOURCE_NAME = "HM Tank Cleaning Guide By Energy Institute"
CARGO_TYPE = "OIL"

DEFAULT_TEMPLATES = input_file(
    "HM Tank Cleaning Guide By Energy Institute - procedure templates.xlsx")
DEFAULT_STEPS = input_file(
    "HM Tank Cleaning Guide By Energy Institute - procedure template steps.xlsx")

COL_CODE = "Code"
COL_DEFINITION = "HM 50 definition"
COL_INTERPRETATION = "Operational interpretation"
TEMPLATE_COLS = [COL_CODE, COL_DEFINITION, COL_INTERPRETATION]

S_CODE = "Procedure Code"
S_ORDER = "Step Order"
S_NAME = "Step Name"
S_TYPE = "Step Type"
S_DESC = "Step Description"
S_MEDIUM = "Medium"
S_TEMP = "Temperature"
S_DURATION = "Duration"
S_CLEANER = "Cleaner"
S_MANDATORY = "Mandatory"
S_CONDITION = "Condition"
S_REFERENCE = "Reference Template Code"
STEP_COLS = [S_CODE, S_ORDER, S_NAME, S_TYPE, S_DESC, S_MEDIUM, S_TEMP,
             S_DURATION, S_CLEANER, S_MANDATORY, S_CONDITION, S_REFERENCE]

# Short names for the legend codes. Not in the document - see TEMPLATE NAMES
# above. A code missing from here aborts the import.
TEMPLATE_NAMES: Dict[str, str] = {
    "X":   "Special Cleaning Instructions Required",
    "X*":  "Special Cleaning Plus Clean Intermediate Cargoes",
    "1":   "Drain Tanks, Lines and Pumps",
    "2":   "Cold Water Wash and Drain",
    "3":   "Hot Water Wash and Drain",
    "3M*": "Stringent Hot Water Wash, Drain and Mop",
    "P":   "Purge to Below 2% Hydrocarbon",
    "M":   "Gas Free, Lift Scale and Mop",
    "#":   "Fresh Water Rinse After Salt Water Wash",
    "LU":  "Reduced Cleaning for Lubricating Oils",
}

# The wash medium each code is defined around; absent = the code involves no
# water (drain, purge, mop) or leaves the medium to the special instructions.
WATER_TYPES: Dict[str, str] = {
    "2":   "Cold Water",
    "3":   "Hot Water",
    "3M*": "Hot Water",
    "#":   "Fresh Water",
}

# Codes that forbid the transition outright rather than describing a wash.
DECISION_CODES = {"X", "X*"}

# Matrix-only codes, composed from legend codes. code -> (parts, name).
COMPOSITE_CODES: Dict[str, Tuple[Tuple[str, ...], str]] = {
    "2M": (("2", "M"), "Cold Water Wash and Drain, then Gas Free, Lift Scale and Mop"),
    "2P": (("2", "P"), "Cold Water Wash and Drain, then Purge"),
    "3M": (("3", "M"), "Hot Water Wash and Drain, then Gas Free, Lift Scale and Mop"),
    "3P": (("3", "P"), "Hot Water Wash and Drain, then Purge"),
}

# CleaningStepType members the steps sheet is allowed to use. Kept explicit so a
# typo in the sheet fails here with a readable message instead of as a Postgres
# enum cast error halfway through the import.
STEP_TYPES = {
    "PRECONDITION", "PRECLEANING", "CLEANING", "RINSING", "FLUSHING", "STEAMING",
    "DRAINING", "DRYING", "VENTILATING", "PURGING", "GAS_FREEING", "MOPPING",
    "MAIN", "REFERENCE", "CONDITIONAL", "DECISION", "WARNING",
}


def clean(value) -> Optional[str]:
    """Trim a cell; blank / NaN -> None (SQL NULL)."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    s = re.sub(r"\s+", " ", str(value).replace("\n", " ")).strip()
    return s or None


def norm_code(value) -> Optional[str]:
    """Normalise a procedure code cell.

    Excel stores the numeric codes as numbers, so 1 / 2 / 3 arrive as "1.0".
    Every HM 50 file has to agree on the spelling or the matrix will not join to
    the templates, so all three of them normalise through this one function.
    """
    s = clean(value)
    if s is None:
        return None
    return s[:-2] if re.fullmatch(r"\d+\.0", s) else s


def to_bool(value, default: bool = True) -> bool:
    """Read the sheet's 1.0 / 0.0 mandatory flag."""
    s = clean(value)
    if s is None:
        return default
    return s.lower() in {"1", "1.0", "true", "yes", "y"}


def to_order(value) -> Optional[int]:
    s = clean(value)
    if s is None:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


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


def require_columns(df: pd.DataFrame, wanted: List[str], path: Path) -> None:
    missing = [c for c in wanted if c not in df.columns]
    if missing:
        sys.exit(f"Error: {path.name} is missing column(s): {', '.join(missing)}")


def read_templates(path: Path) -> Tuple[List[dict], List[str]]:
    """Legend sheet -> one dict per code, plus validation errors."""
    df = pd.read_excel(path, dtype=str)
    require_columns(df, TEMPLATE_COLS, path)

    rows: List[dict] = []
    errors: List[str] = []
    seen: set = set()

    for i, r in df.iterrows():
        line = i + 2
        code = norm_code(r[COL_CODE])
        if code is None:
            continue
        if code in seen:
            errors.append(f"row {line}: duplicate code {code!r}")
            continue
        seen.add(code)
        if code not in TEMPLATE_NAMES:
            errors.append(
                f"row {line}: code {code!r} has no entry in TEMPLATE_NAMES - "
                f"name it in etl/oil/hm50_procedure_templates.py")
            continue
        rows.append({
            "procedure_code": code,
            "template_name": TEMPLATE_NAMES[code],
            "description": clean(r[COL_INTERPRETATION]),
            "source_definition": clean(r[COL_DEFINITION]),
            "water_type": WATER_TYPES.get(code),
            "loading_allowed": code not in DECISION_CODES,
            "notes": None,
        })
    return rows, errors


def read_steps(path: Path, known_codes: set) -> Tuple[Dict[str, List[dict]], List[str]]:
    """Steps sheet -> procedure_code -> ordered step dicts, plus errors."""
    df = pd.read_excel(path, dtype=str)
    require_columns(df, STEP_COLS, path)

    by_code: Dict[str, List[dict]] = {}
    errors: List[str] = []

    for i, r in df.iterrows():
        line = i + 2
        code = norm_code(r[S_CODE])
        if code is None:
            continue
        if code not in known_codes:
            errors.append(f"row {line}: steps for code {code!r}, which the legend "
                          f"sheet does not define")
            continue

        order = to_order(r[S_ORDER])
        if order is None:
            errors.append(f"row {line}: code {code!r} has no usable {S_ORDER!r}")
            continue

        name = clean(r[S_NAME])
        if name is None:
            errors.append(f"row {line}: code {code!r} step {order} has no {S_NAME!r} "
                          f"(the column is NOT NULL)")
            continue

        step_type = clean(r[S_TYPE])
        if step_type is not None:
            step_type = step_type.upper()
            if step_type not in STEP_TYPES:
                errors.append(f"row {line}: code {code!r} step {order} has "
                              f"{S_TYPE} {step_type!r}, which is not a "
                              f"CleaningStepType value")
                continue

        ref = norm_code(r[S_REFERENCE])
        if ref is not None and ref not in known_codes:
            errors.append(f"row {line}: code {code!r} step {order} references "
                          f"{ref!r}, which the legend sheet does not define")
            continue

        by_code.setdefault(code, []).append({
            "step_order": order,
            "step_name": name,
            "step_type": step_type,
            "step_description": clean(r[S_DESC]),
            "medium": clean(r[S_MEDIUM]),
            "temperature": clean(r[S_TEMP]),
            "duration": clean(r[S_DURATION]),
            "cleaner": clean(r[S_CLEANER]),
            "mandatory": to_bool(r[S_MANDATORY]),
            "condition": clean(r[S_CONDITION]),
            "reference_code": ref,
        })

    for code, steps in by_code.items():
        steps.sort(key=lambda s: s["step_order"])
        orders = [s["step_order"] for s in steps]
        if len(set(orders)) != len(orders):
            errors.append(f"code {code!r}: duplicate {S_ORDER} value(s) {orders}")
        if orders and orders != list(range(1, len(orders) + 1)):
            errors.append(f"code {code!r}: {S_ORDER} is {orders}, expected a "
                          f"1-based run with no gaps")
    return by_code, errors


def build_composites(known_codes: set) -> Tuple[List[dict], Dict[str, List[dict]], List[str]]:
    """Templates + REFERENCE steps for the matrix-only composite codes."""
    templates: List[dict] = []
    steps: Dict[str, List[dict]] = {}
    errors: List[str] = []

    for code, (parts, name) in COMPOSITE_CODES.items():
        unknown = [p for p in parts if p not in known_codes]
        if unknown:
            errors.append(f"composite {code!r} is built from {', '.join(unknown)}, "
                          f"which the legend sheet does not define")
            continue
        composed = " then ".join(f"code {p}" for p in parts)
        note = (f"Composite code: the HM 50 matrix uses {code} but the legend "
                f"prints no entry for it. Composed by the loader as {composed}; "
                f"the legend's own text refers to codes 2M and 3M this way. Its "
                f"steps are references to those codes, not copies of them.")
        templates.append({
            "procedure_code": code,
            "template_name": name,
            "description": f"Apply {composed}.",
            "source_definition": None,   # the legend prints none
            "water_type": WATER_TYPES.get(parts[0]),
            "loading_allowed": True,
            "notes": note,
        })
        steps[code] = [
            {"step_order": n, "step_name": f"Apply code {p}", "step_type": "REFERENCE",
             "step_description": f"Perform code {p} as defined by this source.",
             "medium": None, "temperature": None, "duration": None, "cleaner": None,
             "mandatory": True, "condition": None, "reference_code": p}
            for n, p in enumerate(parts, start=1)
        ]
    return templates, steps, errors


def upsert_template(cur, source_id: int, t: dict) -> int:
    cur.execute(
        """
        INSERT INTO procedure_templates
            (procedure_code, template_name, cargo_type, description, water_type,
             source_id, source_definition, loading_allowed, notes,
             created_at, updated_at)
        VALUES (%s, %s, %s::"CargoType", %s, %s, %s, %s, %s, %s, now(), now())
        ON CONFLICT (source_id, procedure_code) DO UPDATE SET
            template_name     = EXCLUDED.template_name,
            cargo_type        = EXCLUDED.cargo_type,
            description       = EXCLUDED.description,
            water_type        = EXCLUDED.water_type,
            source_definition = EXCLUDED.source_definition,
            loading_allowed   = EXCLUDED.loading_allowed,
            notes             = EXCLUDED.notes,
            updated_at        = now()
        RETURNING id
        """,
        (t["procedure_code"], t["template_name"], CARGO_TYPE, t["description"],
         t["water_type"], source_id, t["source_definition"], t["loading_allowed"],
         t["notes"]),
    )
    return cur.fetchone()[0]


def sync_steps(cur, template_id: int, steps: List[dict], ids: Dict[str, int]) -> None:
    for s in steps:
        cur.execute(
            """
            INSERT INTO procedure_template_steps
                (procedure_templates_id, step_order, step_name, step_type,
                 step_description, medium, temperature, duration, cleaner,
                 mandatory, condition, reference_template_id, created_at, updated_at)
            VALUES (%s, %s, %s, %s::"CleaningStepType", %s, %s, %s, %s, %s, %s, %s,
                    %s, now(), now())
            ON CONFLICT (procedure_templates_id, step_order) DO UPDATE SET
                step_name             = EXCLUDED.step_name,
                step_type             = EXCLUDED.step_type,
                step_description      = EXCLUDED.step_description,
                medium                = EXCLUDED.medium,
                temperature           = EXCLUDED.temperature,
                duration              = EXCLUDED.duration,
                cleaner               = EXCLUDED.cleaner,
                mandatory             = EXCLUDED.mandatory,
                condition             = EXCLUDED.condition,
                reference_template_id = EXCLUDED.reference_template_id,
                updated_at            = now()
            """,
            (template_id, s["step_order"], s["step_name"], s["step_type"],
             s["step_description"], s["medium"], s["temperature"], s["duration"],
             s["cleaner"], s["mandatory"], s["condition"],
             ids.get(s["reference_code"]) if s["reference_code"] else None),
        )
    # Slots the sheet no longer fills, so a shortened procedure shortens here.
    cur.execute(
        "DELETE FROM procedure_template_steps "
        "WHERE procedure_templates_id = %s AND step_order > %s",
        (template_id, len(steps)),
    )


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
    templates, errors = read_templates(tpath)
    known = {t["procedure_code"] for t in templates}
    steps, errs = read_steps(spath, known)
    errors += errs
    composites, composite_steps, errs = build_composites(known)
    errors += errs

    if errors:
        log.error("%d validation error(s) - nothing was imported:", len(errors))
        for e in errors:
            log.error("    %s", e)
        return 1

    all_templates = templates + composites
    all_steps = {**steps, **composite_steps}
    n_steps = sum(len(v) for v in all_steps.values())

    log.info("%s: %d legend code(s)", tpath.name, len(templates))
    log.info("%s: %d step(s) across %d code(s)", spath.name,
             sum(len(v) for v in steps.values()), len(steps))
    log.info("composites: %d code(s), %d reference step(s)",
             len(composites), sum(len(v) for v in composite_steps.values()))
    stepless = sorted(t["procedure_code"] for t in all_templates
                      if not all_steps.get(t["procedure_code"]))
    if stepless:
        log.info("no steps (decisions, or the sheet lists none): %s", ", ".join(stepless))

    if args.dry_run:
        for t in all_templates:
            log.info("  %-4s %-58s steps=%d allowed=%s", t["procedure_code"],
                     t["template_name"], len(all_steps.get(t["procedure_code"], [])),
                     t["loading_allowed"])
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

            # Every template first: a step's reference_template_id needs the id
            # of a code that may be defined further down the sheet.
            ids: Dict[str, int] = {}
            for t in all_templates:
                ids[t["procedure_code"]] = upsert_template(cur, source_id, t)

            for code, rows in all_steps.items():
                sync_steps(cur, ids[code], rows, ids)
            # Templates the sheet gives no steps at all keep none.
            for t in all_templates:
                if t["procedure_code"] not in all_steps:
                    sync_steps(cur, ids[t["procedure_code"]], [], ids)

        conn.commit()
        log.info("✓ Committed. procedure_templates: %d (%d from the legend, "
                 "%d composite) | procedure_template_steps: %d | cargo_type=%s",
                 len(all_templates), len(templates), len(composites), n_steps,
                 CARGO_TYPE)
        return 0
    except Exception:
        conn.rollback()
        log.exception("Import failed - rolled back, nothing written.")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
