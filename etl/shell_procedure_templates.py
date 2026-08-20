#!/usr/bin/env python3
"""
Import cleaning procedure templates from Excel into
procedure_templates + procedure_template_steps + procedure_template_requirement
+ procedure_template_instruction.

Default input: etl/data/inputs/Shell Tank Cleaning Procedure.xlsx
(built by etl/build_shell_procedure_workbook.py from the supplied Shell file).

WHAT GOES WHERE
---------------
    procedure_templates              what the code means            WD, CW, NC
      |- procedure_template_steps         ordered actions           sequenced
      |- procedure_template_requirement   rules and limits          unsequenced
      '- procedure_template_instruction   source-level statements   unsequenced

cleaning_process is NOT touched. It names a procedure via procedure_template_id
and must never restate its steps.

THIS SCRIPT HOLDS NO PROCEDURE DATA. Every value comes from the workbook, so a
correction to a step, a limit or a wording is an Excel edit, never a code change.

SOURCE
------
The workbook keys its sheets on procedure_code, but procedure_templates is keyed
on (source_id, procedure_code) - a code means nothing without the document it
came from. source_id is therefore supplied by the import context, not by the
spreadsheet:

    --source-id 26                 use exactly this source
    --source-name "Shell ..."      resolve by name via the shared helpers,
                                   creating the row only with --create-source

VALIDATION
----------
The ENTIRE workbook is validated before a single row is written, and any error
aborts the whole import - no partial procedure data, no silently skipped rows.

IDEMPOTENCY
-----------
Re-running upserts rather than duplicating. Keys:

    templates     (source_id, procedure_code)
    steps         (procedure_templates_id, step_order)
    requirements  (procedure_template_id, display_order)
    instructions  (procedure_templates_id, display_order)

Child rows whose slot is no longer in the workbook are deleted, so shortening a
procedure in Excel shortens it in the database instead of leaving a tail behind.

Usage:
    python3 etl/shell_procedure_templates.py --source-id 26
    python3 etl/shell_procedure_templates.py --source-name "Shell Tank Cleaning Procedure" --create-source
    python3 etl/shell_procedure_templates.py --dry-run
    python3 etl/shell_procedure_templates.py path/to/workbook.xlsx --source-id 26
"""

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import psycopg2
from dotenv import load_dotenv

from _paths import input_file
from cargo_chemicals import get_source_id, get_source_id_partial, create_source

DEFAULT_FILE = input_file("Shell Tank Cleaning Procedure.xlsx")
DEFAULT_SOURCE_NAME = "Shell Tank Cleaning Procedure"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("shell_procedures")

# Sheet names. The supplied workbook calls the template sheet "Cleaning Codes",
# so both spellings are accepted rather than forcing a rename of the source file.
SHEET_TEMPLATES = ("procedure_templates", "Cleaning Codes")
SHEET_STEPS = ("procedure_template_steps",)
SHEET_REQUIREMENTS = ("procedure_template_requirements", "procedure_template_requirement")
SHEET_INSTRUCTIONS = ("procedure_template_instructions", "procedure_template_instruction")

# Enum values, mirroring prisma/schema.prisma. Validated here so a bad cell is
# reported with its sheet and row instead of surfacing as a Postgres cast error.
CARGO_TYPES = {"CHEMICAL", "OIL", "GAS"}
STEP_TYPES = {"PRECONDITION", "PRECLEANING", "CLEANING", "RINSING", "FLUSHING",
              "STEAMING", "DRAINING", "DRYING", "VENTILATING", "PURGING",
              "GAS_FREEING", "MOPPING"}
INSTRUCTION_TYPES = {"DANGER", "WARNING", "CAUTION", "IMPORTANT", "INFO"}

TRUE_VALUES = {"true", "t", "yes", "y", "1", "1.0"}
FALSE_VALUES = {"false", "f", "no", "n", "0", "0.0"}


class ValidationErrors(Exception):
    """Collected workbook problems, reported together rather than one per run."""

    def __init__(self, errors: List[str]):
        self.errors = errors
        super().__init__(f"{len(errors)} validation error(s)")


# ---------------------------------------------------------------------------
# Cell coercion
# ---------------------------------------------------------------------------
def text(value: Any) -> Optional[str]:
    """Trimmed cell text, or None for blank / NaN."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    s = str(value).strip()
    return s or None


def boolean(value: Any, where: str, errors: List[str],
            default: Optional[bool] = None) -> Optional[bool]:
    """Strict boolean. An unrecognised value is an error, never a silent False."""
    s = text(value)
    if s is None:
        return default
    low = s.lower()
    if low in TRUE_VALUES:
        return True
    if low in FALSE_VALUES:
        return False
    errors.append(f"{where}: {s!r} is not a boolean "
                  f"(accepted: true/false, yes/no, 1/0)")
    return default


def integer(value: Any, where: str, errors: List[str]) -> Optional[int]:
    s = text(value)
    if s is None:
        errors.append(f"{where}: required, but blank")
        return None
    try:
        n = int(float(s))
    except ValueError:
        errors.append(f"{where}: {s!r} is not an integer")
        return None
    if n < 1:
        errors.append(f"{where}: {n} must be 1 or greater")
        return None
    return n


def enum_value(value: Any, allowed: set, where: str, errors: List[str],
               required: bool = True) -> Optional[str]:
    s = text(value)
    if s is None:
        if required:
            errors.append(f"{where}: required, but blank")
        return None
    up = s.upper().replace(" ", "_").replace("-", "_")
    if up not in allowed:
        errors.append(f"{where}: {s!r} is not valid (allowed: {', '.join(sorted(allowed))})")
        return None
    return up


# ---------------------------------------------------------------------------
# Workbook reading
# ---------------------------------------------------------------------------
def read_sheet(xl: pd.ExcelFile, names: Tuple[str, ...]) -> Optional[pd.DataFrame]:
    """First sheet matching any of `names` (case-insensitive), or None."""
    lookup = {s.lower(): s for s in xl.sheet_names}
    for want in names:
        actual = lookup.get(want.lower())
        if actual is not None:
            return xl.parse(actual)
    return None


def require_columns(df: pd.DataFrame, columns: List[str], sheet: str,
                    errors: List[str]) -> None:
    missing = [c for c in columns if c not in df.columns]
    if missing:
        errors.append(f"[{sheet}] missing required column(s): {', '.join(missing)}")


def excel_row(index: int) -> int:
    """Spreadsheet row number for a 0-based DataFrame index (1 header row)."""
    return index + 2


# ---------------------------------------------------------------------------
# Validation - the whole workbook, before any write
# ---------------------------------------------------------------------------
def validate(path: Path) -> Dict[str, List[dict]]:
    errors: List[str] = []
    xl = pd.ExcelFile(path)
    log.info("Workbook %s -> sheets %s", path.name, xl.sheet_names)

    templates_df = read_sheet(xl, SHEET_TEMPLATES)
    if templates_df is None:
        raise ValidationErrors(
            [f"no template sheet found; expected one of {list(SHEET_TEMPLATES)}, "
             f"workbook has {xl.sheet_names}"]
        )

    require_columns(templates_df, ["procedure_code", "template_name", "cargo_type"],
                    "procedure_templates", errors)
    if errors:
        raise ValidationErrors(errors)

    # --- templates -------------------------------------------------------
    templates: List[dict] = []
    seen_codes: Dict[str, int] = {}
    for i, row in templates_df.iterrows():
        where = f"[procedure_templates] row {excel_row(i)}"
        code = text(row.get("procedure_code"))
        if code is None:
            errors.append(f"{where}: procedure_code is required")
            continue
        if code in seen_codes:
            errors.append(f"{where}: procedure_code {code!r} already used on row "
                          f"{seen_codes[code]} - codes must be unique within a source")
            continue
        seen_codes[code] = excel_row(i)

        name = text(row.get("template_name"))
        if name is None:
            errors.append(f"{where}: template_name is required for {code!r}")

        templates.append({
            "procedure_code": code,
            "template_name": name,
            "cargo_type": enum_value(row.get("cargo_type"), CARGO_TYPES,
                                     f"{where}: cargo_type", errors),
            "water_type": text(row.get("water_type")),
            "loading_allowed": boolean(row.get("loading_allowed"),
                                       f"{where}: loading_allowed", errors, default=True),
            "description": text(row.get("description")),
            "source_definition": text(row.get("source_definition")),
            "source_page_ref": text(row.get("source_page_ref")),
        })

    known = set(seen_codes)

    def check_parent(code: Optional[str], where: str) -> None:
        if code is None:
            errors.append(f"{where}: procedure_code is required")
        elif code not in known:
            errors.append(f"{where}: procedure_code {code!r} is not defined on the "
                          f"template sheet (known: {', '.join(sorted(known))})")

    # --- steps -----------------------------------------------------------
    steps: List[dict] = []
    steps_df = read_sheet(xl, SHEET_STEPS)
    if steps_df is None:
        log.warning("no steps sheet - importing templates without steps")
    else:
        require_columns(steps_df, ["procedure_code", "step_order", "step_name"],
                        "procedure_template_steps", errors)
        slots: Dict[Tuple[str, int], int] = {}
        for i, row in steps_df.iterrows():
            where = f"[procedure_template_steps] row {excel_row(i)}"
            code = text(row.get("procedure_code"))
            check_parent(code, where)
            order = integer(row.get("step_order"), f"{where}: step_order", errors)
            name = text(row.get("step_name"))
            if name is None:
                errors.append(f"{where}: step_name is required")
            if code and order is not None:
                if (code, order) in slots:
                    errors.append(f"{where}: {code} already has step_order {order} on row "
                                  f"{slots[(code, order)]}")
                    continue
                slots[(code, order)] = excel_row(i)
            steps.append({
                "procedure_code": code,
                "step_order": order,
                "step_name": name,
                "step_type": enum_value(row.get("step_type"), STEP_TYPES,
                                        f"{where}: step_type", errors, required=False),
                "medium": text(row.get("medium")),
                "temperature": text(row.get("temperature")),
                "duration": text(row.get("duration")),
                "cleaner": text(row.get("cleaner")),
                "mandatory": boolean(row.get("mandatory"), f"{where}: mandatory",
                                     errors, default=True),
                "step_description": text(row.get("step_description")),
                "notes": text(row.get("notes")),
            })

    # --- requirements ----------------------------------------------------
    requirements: List[dict] = []
    reqs_df = read_sheet(xl, SHEET_REQUIREMENTS)
    if reqs_df is None:
        log.warning("no requirements sheet - importing without requirements")
    else:
        require_columns(reqs_df, ["procedure_code", "requirement_type", "display_order"],
                        "procedure_template_requirements", errors)
        slots = {}
        for i, row in reqs_df.iterrows():
            where = f"[procedure_template_requirements] row {excel_row(i)}"
            code = text(row.get("procedure_code"))
            check_parent(code, where)
            order = integer(row.get("display_order"), f"{where}: display_order", errors)
            rtype = text(row.get("requirement_type"))
            if rtype is None:
                errors.append(f"{where}: requirement_type is required")
            if code and order is not None:
                if (code, order) in slots:
                    errors.append(f"{where}: {code} already has display_order {order} "
                                  f"on row {slots[(code, order)]}")
                    continue
                slots[(code, order)] = excel_row(i)
            requirements.append({
                "procedure_code": code,
                "requirement_type": rtype,
                "requirement_value": text(row.get("requirement_value")),
                "operator": text(row.get("operator")),
                "unit": text(row.get("unit")),
                "mandatory": boolean(row.get("mandatory"), f"{where}: mandatory",
                                     errors, default=True),
                "description": text(row.get("description")),
                "display_order": order,
            })

    # --- instructions ----------------------------------------------------
    instructions: List[dict] = []
    instr_df = read_sheet(xl, SHEET_INSTRUCTIONS)
    if instr_df is None:
        log.warning("no instructions sheet - importing without instructions")
    else:
        require_columns(instr_df, ["procedure_code", "instruction_type", "message",
                                   "display_order"],
                        "procedure_template_instructions", errors)
        slots = {}
        for i, row in instr_df.iterrows():
            where = f"[procedure_template_instructions] row {excel_row(i)}"
            code = text(row.get("procedure_code"))
            check_parent(code, where)
            order = integer(row.get("display_order"), f"{where}: display_order", errors)
            message = text(row.get("message"))
            if message is None:
                errors.append(f"{where}: message is required")
            if code and order is not None:
                if (code, order) in slots:
                    errors.append(f"{where}: {code} already has display_order {order} "
                                  f"on row {slots[(code, order)]}")
                    continue
                slots[(code, order)] = excel_row(i)
            instructions.append({
                "procedure_code": code,
                "instruction_type": enum_value(row.get("instruction_type"),
                                               INSTRUCTION_TYPES,
                                               f"{where}: instruction_type", errors),
                "message": message,
                "display_order": order,
                "mandatory": boolean(row.get("mandatory"), f"{where}: mandatory",
                                     errors, default=True),
                "notes": text(row.get("notes")),
            })

    # A template that forbids loading must not carry steps: "not compatible" is a
    # decision, and steps against it would assert an operation the source forbids.
    no_load = {t["procedure_code"] for t in templates if t["loading_allowed"] is False}
    for code in sorted(no_load & {s["procedure_code"] for s in steps}):
        errors.append(f"[procedure_template_steps] {code} has loading_allowed=false but "
                      f"defines steps; a procedure that forbids loading has none")

    if errors:
        raise ValidationErrors(errors)

    return {"templates": templates, "steps": steps,
            "requirements": requirements, "instructions": instructions}


# ---------------------------------------------------------------------------
# Import - one transaction
# ---------------------------------------------------------------------------
def upsert_template(cur, source_id: int, t: dict) -> int:
    cur.execute(
        """
        INSERT INTO procedure_templates
            (procedure_code, template_name, cargo_type, water_type, loading_allowed,
             description, source_definition, source_page_ref, source_id,
             created_at, updated_at)
        VALUES (%s, %s, %s::"CargoType", %s, %s, %s, %s, %s, %s, now(), now())
        ON CONFLICT (source_id, procedure_code) DO UPDATE SET
            template_name     = EXCLUDED.template_name,
            cargo_type        = EXCLUDED.cargo_type,
            water_type        = EXCLUDED.water_type,
            loading_allowed   = EXCLUDED.loading_allowed,
            description       = EXCLUDED.description,
            source_definition = EXCLUDED.source_definition,
            source_page_ref   = EXCLUDED.source_page_ref,
            updated_at        = now()
        RETURNING id
        """,
        (t["procedure_code"], t["template_name"], t["cargo_type"], t["water_type"],
         t["loading_allowed"], t["description"], t["source_definition"],
         t["source_page_ref"], source_id),
    )
    return cur.fetchone()[0]


def sync_children(cur, table: str, parent_column: str, order_column: str,
                  template_id: int, rows: List[tuple], columns: List[str],
                  updates: List[str]) -> Tuple[int, int]:
    """Upsert child rows on their slot key, then drop slots no longer supplied.

    The delete is what makes a shortened procedure actually shorten: without it,
    removing step 5 in Excel would leave the old step 5 in the database forever.
    """
    removed = 0
    if rows:
        placeholders = ", ".join(["%s"] * len(columns))
        set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in updates)
        for row in rows:
            cur.execute(
                f"""
                INSERT INTO {table} ({", ".join(columns)}, created_at, updated_at)
                VALUES ({placeholders}, now(), now())
                ON CONFLICT ({parent_column}, {order_column})
                DO UPDATE SET {set_clause}, updated_at = now()
                """,
                row,
            )
        kept = [r[columns.index(order_column)] for r in rows]
        cur.execute(
            f"DELETE FROM {table} WHERE {parent_column} = %s AND NOT ({order_column} = ANY(%s))",
            (template_id, kept),
        )
        removed = cur.rowcount
    else:
        cur.execute(f"DELETE FROM {table} WHERE {parent_column} = %s", (template_id,))
        removed = cur.rowcount
    return len(rows), removed


def run_import(cur, source_id: int, data: Dict[str, List[dict]]) -> Dict[str, int]:
    by_code: Dict[str, int] = {}
    for t in data["templates"]:
        by_code[t["procedure_code"]] = upsert_template(cur, source_id, t)
    log.info("procedure_templates            : %d upserted", len(by_code))

    def group(rows: List[dict]) -> Dict[str, List[dict]]:
        out: Dict[str, List[dict]] = {code: [] for code in by_code}
        for r in rows:
            out.setdefault(r["procedure_code"], []).append(r)
        return out

    counts = {"templates": len(by_code), "steps": 0, "requirements": 0,
              "instructions": 0, "removed": 0}

    steps_by_code = group(data["steps"])
    for code, tid in by_code.items():
        rows = [(tid, s["step_order"], s["step_name"],
                 s["step_type"], s["step_description"], s["medium"], s["temperature"],
                 s["duration"], s["cleaner"], s["mandatory"], s["notes"])
                for s in sorted(steps_by_code.get(code, []), key=lambda x: x["step_order"])]
        n, removed = sync_children(
            cur, "procedure_template_steps", "procedure_templates_id", "step_order", tid, rows,
            ["procedure_templates_id", "step_order", "step_name", "step_type",
             "step_description", "medium", "temperature", "duration", "cleaner",
             "mandatory", "notes"],
            ["step_name", "step_type", "step_description", "medium", "temperature",
             "duration", "cleaner", "mandatory", "notes"],
        )
        counts["steps"] += n
        counts["removed"] += removed
    log.info("procedure_template_steps       : %d upserted", counts["steps"])

    reqs_by_code = group(data["requirements"])
    for code, tid in by_code.items():
        rows = [(tid, r["display_order"], r["requirement_type"], r["requirement_value"],
                 r["operator"], r["unit"], r["mandatory"], r["description"])
                for r in sorted(reqs_by_code.get(code, []), key=lambda x: x["display_order"])]
        n, removed = sync_children(
            cur, "procedure_template_requirement", "procedure_template_id", "display_order",
            tid, rows,
            ["procedure_template_id", "display_order", "requirement_type",
             "requirement_value", "operator", "unit", "mandatory", "description"],
            ["requirement_type", "requirement_value", "operator", "unit",
             "mandatory", "description"],
        )
        counts["requirements"] += n
        counts["removed"] += removed
    log.info("procedure_template_requirement : %d upserted", counts["requirements"])

    instr_by_code = group(data["instructions"])
    for code, tid in by_code.items():
        rows = [(tid, i["display_order"], i["instruction_type"], i["message"],
                 i["mandatory"], i["notes"])
                for i in sorted(instr_by_code.get(code, []), key=lambda x: x["display_order"])]
        n, removed = sync_children(
            cur, "procedure_template_instruction", "procedure_templates_id",
            "display_order", tid, rows,
            ["procedure_templates_id", "display_order", "instruction_type", "message",
             "mandatory", "notes"],
            ["instruction_type", "message", "mandatory", "notes"],
        )
        counts["instructions"] += n
        counts["removed"] += removed
    log.info("procedure_template_instruction : %d upserted", counts["instructions"])

    return counts


def report_source_anomalies(data: Dict[str, List[dict]]) -> None:
    """Flag source data that imports cleanly but looks wrong. Never auto-corrects."""
    types: Dict[str, List[str]] = {}
    for t in data["templates"]:
        types.setdefault(t["cargo_type"], []).append(t["procedure_code"])
    if len(types) > 1:
        majority = max(types, key=lambda k: len(types[k]))
        log.warning("=" * 72)
        log.warning("cargo_type is not consistent across this source's procedures:")
        for ct, codes in sorted(types.items(), key=lambda kv: -len(kv[1])):
            log.warning("    %-8s %2d procedure(s): %s", ct, len(codes), ", ".join(sorted(codes)))
        odd = [c for ct, codes in types.items() if ct != majority for c in codes]
        log.warning("Imported VERBATIM as the workbook states it. If %s should be %s,",
                    ", ".join(sorted(odd)), majority)
        log.warning("fix the cargo_type column in the workbook and re-run - not in code.")
        log.warning("=" * 72)


def resolve_source(cur, source_id: Optional[int], source_name: str,
                   create: bool) -> int:
    """source_id comes from the import context, never from the spreadsheet."""
    if source_id is not None:
        cur.execute("SELECT id, name FROM source WHERE id = %s", (source_id,))
        row = cur.fetchone()
        if row is None:
            sys.exit(f"Error: --source-id {source_id} does not exist in source.")
        log.info("Using source id=%s (%r)", row[0], row[1])
        return row[0]

    sid = get_source_id(cur, source_name) or get_source_id_partial(cur, source_name)
    if sid is not None:
        return sid
    if not create:
        sys.exit(
            f"Error: no source matches {source_name!r}.\n"
            f"  Register it with the shared loader:  python3 etl/source.py\n"
            f"  or pass --source-id <id>, or re-run with --create-source."
        )
    return create_source(cur, source_name)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", default=str(DEFAULT_FILE))
    ap.add_argument("--source-id", type=int, help="source.id to import against")
    ap.add_argument("--source-name", default=DEFAULT_SOURCE_NAME)
    ap.add_argument("--create-source", action="store_true",
                    help="create the source row if --source-name matches nothing")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate the workbook and report; write nothing")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.exists():
        sys.exit(f"Error: workbook not found: {path}\n"
                 f"Build it with: python3 etl/build_shell_procedure_workbook.py")

    try:
        data = validate(path)
    except ValidationErrors as exc:
        log.error("Workbook is invalid - nothing was written:")
        for e in exc.errors:
            log.error("  %s", e)
        sys.exit(1)

    log.info("Validated: %d templates, %d steps, %d requirements, %d instructions",
             len(data["templates"]), len(data["steps"]),
             len(data["requirements"]), len(data["instructions"]))
    report_source_anomalies(data)

    if args.dry_run:
        log.info("--dry-run: workbook is valid, no database changes made.")
        return

    load_dotenv()
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = False          # one transaction for the whole import
    try:
        with conn.cursor() as cur:
            source_id = resolve_source(cur, args.source_id, args.source_name,
                                       args.create_source)
            counts = run_import(cur, source_id, data)
        conn.commit()
        log.info("✓ Committed. templates=%d steps=%d requirements=%d instructions=%d "
                 "stale-rows-removed=%d",
                 counts["templates"], counts["steps"], counts["requirements"],
                 counts["instructions"], counts["removed"])
    except Exception:
        conn.rollback()
        log.exception("Import failed - rolled back, no partial procedure data written.")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
