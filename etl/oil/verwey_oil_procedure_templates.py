#!/usr/bin/env python3
"""
Load the Dr Verwey oil cleaning codes into procedure_templates +
procedure_template_steps.

SOURCE
------
"Dr Verweys Tank Cleaning Guide - Oil" (source.json, category 'oil') - a source
row of its own, NOT one of the two chemical Verwey sources. See
verwey_oil_cargo.py for why: the oil code letters collide with chemical ones
that mean something else, and procedure_templates is keyed on
(source_id, procedure_code).

INPUTS
------
1. "... Oil - procedure templates.xlsx"      id | procedure_code | template_name |
                                             cargo_type | description
2. "... Oil - procedure template steps.xlsx" procedure_code | source_method_id |
                                             step_name | step_type | source_value |
                                             medium | temperature | duration |
                                             cleaner | mandatory | notes

The step sheet carries no step_order; `source_method_id` is the guide's own
method number within a code and is used as the order. `source_value` is the raw
matrix cell the step was read from ("Butterworth Cold - source value: SW"), kept
in the input sheet for traceability but not stored as step_description: it only
ever restates medium/temperature/duration/mandatory, and for most rows it's bare
"y" ("this step applies", already captured by the step existing + mandatory=true).

TWO KNOWN DEFECTS IN THE EXTRACT, NEITHER PAPERED OVER
------------------------------------------------------
1. STALE LABELS. Codes I and II were renamed J and JJ, but at first only in
   procedure_code - template_name and description still read "Cleaning Code I"
   and "Source cleaning code I." (now corrected in the sheet; the check stays
   for any future code whose label disagrees). The sheet's text is stored VERBATIM anyway,
   because rewriting a source's own wording to match what it probably meant is
   how a transcription error becomes indistinguishable from the source. The
   mismatch is recorded in `notes` and warned about, so it is visible and
   fixable by re-running once the sheet is corrected.

2. UNDEFINED CODE. The steps and the matrix both use `U`, which the template
   sheet at first did not define (now added to the sheet). procedure_template_steps.procedure_templates_id is a
   NOT NULL foreign key, so U's steps CANNOT be stored without inventing a
   template for it. They are skipped and reported by name; define U in the
   template sheet and re-run and they load, since every write here is an upsert.

`OO(dd)` is defined but used by neither the steps nor the matrix. That is fine -
a legend entry the matrix happens not to reach - so it loads as a template with
no steps, exactly like DD, which the matrix does use but gives no steps to.

IDEMPOTENCY
-----------
Keys: (source_id, procedure_code) and (procedure_templates_id, step_order).
Steps past the end of the sheet are deleted. Everything is validated before a
single row is written; one transaction.

Usage:
    python3 etl/oil/verwey_oil_procedure_templates.py
    python3 etl/oil/verwey_oil_procedure_templates.py --dry-run
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
log = logging.getLogger("verwey_oil_templates")

SOURCE_NAME = "Dr Verweys Tank Cleaning Guide - Oil"
CARGO_TYPE = "OIL"
DEFAULT_TEMPLATES = input_file("Dr Verweys Tank Cleaning Guide Oil - procedure templates.xlsx")
DEFAULT_STEPS = input_file("Dr Verweys Tank Cleaning Guide Oil - procedure template steps.xlsx")

TEMPLATE_COLS = ["procedure_code", "template_name", "cargo_type", "description"]
STEP_COLS = ["procedure_code", "source_method_id", "step_name", "step_type",
             "source_value", "medium", "temperature", "duration", "cleaner",
             "mandatory", "notes"]

STEP_TYPES = {
    "PRECONDITION", "PRECLEANING", "CLEANING", "RINSING", "FLUSHING", "STEAMING",
    "DRAINING", "DRYING", "VENTILATING", "PURGING", "GAS_FREEING", "MOPPING",
    "MAIN", "REFERENCE", "CONDITIONAL", "DECISION", "WARNING",
    "RESTRICTION", "CONDITION",
}


def clean(value) -> Optional[str]:
    if value is None:
        return None
    s = re.sub(r"\s+", " ", str(value).replace("\n", " ")).strip()
    return s or None


def to_int(value) -> Optional[int]:
    s = clean(value)
    if s is None:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def to_bool(value, default: bool = True) -> bool:
    s = clean(value)
    if s is None:
        return default
    return s.lower() in {"1", "1.0", "true", "yes", "y"}


def read_sheet(path: Path, wanted: List[str]) -> List[dict]:
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


def build_templates(rows: List[dict]) -> Tuple[List[dict], List[str], List[str]]:
    templates: List[dict] = []
    errors: List[str] = []
    stale: List[str] = []
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

        description = clean(r["description"])
        note = None
        # See STALE LABELS: the code was renamed but its own label was not.
        if code not in name:
            stale.append(f"{code!r} is labelled {name!r}")
            note = (f"The extract's procedure_code is {code} but template_name and "
                    f"description still name a different code. Both are stored as "
                    f"the sheet has them; procedure_code is what everything joins "
                    f"on. Correct the sheet and re-run to clear this.")

        templates.append({
            "procedure_code": code,
            "template_name": name,
            "description": description,
            "source_definition": description,
            "notes": note,
        })
    return templates, errors, stale


def build_steps(rows: List[dict], known: set) -> Tuple[Dict[str, List[dict]], List[str], Dict[str, int]]:
    by_code: Dict[str, List[dict]] = {}
    errors: List[str] = []
    undefined: Dict[str, int] = {}

    for i, r in enumerate(rows, start=2):
        code = clean(r["procedure_code"])
        if code is None:
            continue
        if code not in known:
            # Cannot be stored: the FK to procedure_templates is NOT NULL.
            undefined[code] = undefined.get(code, 0) + 1
            continue

        order = to_int(r["source_method_id"])
        if order is None:
            errors.append(f"steps row {i}: {code!r} has no usable source_method_id")
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
            "method_id": order,
            "step_name": name,
            "step_type": step_type,
            # source_value is the raw matrix cell ("Butterworth Cold — source
            # value: SW"), but it only ever restates what medium/temperature/
            # duration/mandatory already capture structured — for the common
            # case it's bare "y" ("this step applies", already mandatory=true)
            # with no information of its own. Not stored.
            "step_description": None,
            "medium": clean(r["medium"]),
            "temperature": clean(r["temperature"]),
            "duration": clean(r["duration"]),
            "cleaner": clean(r["cleaner"]),
            "mandatory": to_bool(r["mandatory"]),
            "notes": clean(r["notes"]),
        })

    # source_method_id is the slot in the guide's printed cleaning-method matrix
    # (1 = Butterworth Cold, 5 = Flush, 6 = Steaming, 7 = Draining, 8 = Dry), not
    # a per-code counter: a code that skips an operation skips its number, so D
    # is [1, 5, 6, 7, 8]. Sorting by it gives the operational order in every
    # code, which is what step_order means, so step_order is the RANK of the
    # method number and the number itself is preserved in notes - dropping it
    # would lose which matrix row a step came from, and step_order must stay
    # dense for the delete-the-tail rule below to work.
    for code, steps in by_code.items():
        steps.sort(key=lambda s: s["method_id"])
        ids = [s["method_id"] for s in steps]
        if len(set(ids)) != len(ids):
            errors.append(f"{code!r}: duplicate source_method_id value(s) {ids}")
            continue
        for order, s in enumerate(steps, start=1):
            s["step_order"] = order
            origin = f"Source cleaning-method row {s['method_id']}."
            s["notes"] = f"{s['notes']} {origin}" if s["notes"] else origin
    return by_code, errors, undefined


def upsert_template(cur, source_id: int, t: dict) -> int:
    cur.execute(
        """
        INSERT INTO procedure_templates
            (procedure_code, template_name, cargo_type, description, source_id,
             source_definition, notes, created_at, updated_at)
        VALUES (%s, %s, %s::"CargoType", %s, %s, %s, %s, now(), now())
        ON CONFLICT (source_id, procedure_code) DO UPDATE SET
            template_name     = EXCLUDED.template_name,
            cargo_type        = EXCLUDED.cargo_type,
            description       = EXCLUDED.description,
            source_definition = EXCLUDED.source_definition,
            notes             = EXCLUDED.notes,
            updated_at        = now()
        RETURNING id
        """,
        (t["procedure_code"], t["template_name"], CARGO_TYPE, t["description"],
         source_id, t["source_definition"], t["notes"]),
    )
    return cur.fetchone()[0]


def sync_steps(cur, template_id: int, steps: List[dict]) -> None:
    for s in steps:
        cur.execute(
            """
            INSERT INTO procedure_template_steps
                (procedure_templates_id, step_order, step_name, step_type,
                 step_description, medium, temperature, duration, cleaner,
                 mandatory, notes, created_at, updated_at)
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
                notes            = EXCLUDED.notes,
                updated_at       = now()
            """,
            (template_id, s["step_order"], s["step_name"], s["step_type"],
             s["step_description"], s["medium"], s["temperature"], s["duration"],
             s["cleaner"], s["mandatory"], s["notes"]),
        )
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
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    tpath, spath = Path(args.templates_file), Path(args.steps_file)
    for p in (tpath, spath):
        if not p.is_file():
            sys.exit(f"Error: file not found: {p}")

    templates, errors, stale = build_templates(read_sheet(tpath, TEMPLATE_COLS))
    known = {t["procedure_code"] for t in templates}
    steps, errs, undefined = build_steps(read_sheet(spath, STEP_COLS), known)
    errors += errs

    if errors:
        log.error("%d validation error(s) - nothing was imported:", len(errors))
        for e in errors:
            log.error("    %s", e)
        return 1

    n_steps = sum(len(v) for v in steps.values())
    log.info("%s: %d code(s)", tpath.name, len(templates))
    log.info("%s: %d step(s) across %d code(s)", spath.name, n_steps, len(steps))
    for s in stale:
        log.warning("stale label: %s - stored verbatim, recorded in notes", s)
    for code, n in sorted(undefined.items()):
        log.warning("code %r has %d step(s) but no template row - SKIPPED. "
                    "procedure_template_steps requires a template; define %r in "
                    "%s and re-run to load them.", code, n, code, tpath.name)
    stepless = sorted(c for c in known if not steps.get(c))
    if stepless:
        log.info("template(s) with no steps in the sheet: %s", ", ".join(stepless))

    if args.dry_run:
        for t in templates:
            log.info("  %-8s %-22s steps=%d", t["procedure_code"],
                     t["template_name"], len(steps.get(t["procedure_code"], [])))
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
            for t in templates:
                tid = upsert_template(cur, source_id, t)
                sync_steps(cur, tid, steps.get(t["procedure_code"], []))
        conn.commit()
        log.info("✓ Committed. procedure_templates: %d | procedure_template_steps: %d "
                 "| skipped (undefined code): %d | cargo_type=%s",
                 len(templates), n_steps, sum(undefined.values()), CARGO_TYPE)
        return 0
    except Exception:
        conn.rollback()
        log.exception("Import failed - rolled back, nothing written.")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
