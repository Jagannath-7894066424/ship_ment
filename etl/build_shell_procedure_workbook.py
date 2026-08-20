#!/usr/bin/env python3
"""
Build the complete Shell tank-cleaning procedure workbook that
shell_procedure_templates.py imports.

WHY THIS SCRIPT EXISTS
----------------------
The supplied source file

    Shell Tank Cleaning Procedure.xlsx

carries ONE sheet ("Cleaning Codes") holding the 11 procedure definitions. The
ordered steps, the requirements and the source-level instructions for those
procedures were supplied as a written specification, not as spreadsheet rows, so
there was nothing for the importer to read.

This script closes that gap ONCE, by writing those three sheets into Excel. It
is deliberately NOT part of the importer: the importer must read its data from
the workbook and hold none of its own, so that correcting a step or a limit is
an edit in Excel and never a code change.

    build_shell_procedure_workbook.py   spec  -> Excel   (data entry, run once)
    shell_procedure_templates.py        Excel -> database (the importer)

The templates sheet is copied VERBATIM from the source workbook - every cell,
including the two rows whose cargo_type reads CHEMICAL rather than OIL. Source
data is not silently corrected here; the importer reports the inconsistency and
leaves the decision to a person.

Output goes to etl/data/inputs/ with the other ETL inputs. Re-running overwrites
it, so hand-edits to the OUTPUT are lost - edit and re-import, or fold the change
in here.

Usage:
    python3 etl/build_shell_procedure_workbook.py
    python3 etl/build_shell_procedure_workbook.py --source-workbook /path/to/file.xlsx
    python3 etl/build_shell_procedure_workbook.py --out /path/to/out.xlsx
"""

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

from _paths import input_file

DEFAULT_SOURCE = Path(
    "/home/lap044/Documents/ship_documents/Oil_document/Shell Tank Cleaning Procedure.xlsx"
)
DEFAULT_OUT = input_file("Shell Tank Cleaning Procedure.xlsx")

# The sheet the supplied workbook actually uses for the template definitions.
SOURCE_TEMPLATE_SHEET = "Cleaning Codes"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("build_shell_workbook")

# ---------------------------------------------------------------------------
# Steps - the ordered physical actions.
# ---------------------------------------------------------------------------
# (code, step_order, step_name, step_type, medium, temperature, duration,
#  cleaner, mandatory, step_description)
#
# NC is absent on purpose: "Not Compatible" is a decision, not a procedure, and
# inventing steps for it would assert a cleaning operation the source forbids.
#
# The "Ventilate OR Purge" steps carry step_type VENTILATING because
# CleaningStepType holds one value per row and cannot express a disjunction.
# The OR itself is preserved as an ATMOSPHERE_TREATMENT requirement; the step
# description repeats it so a reader of the step list alone is not misled.
STEPS = [
    ("WD",    1, "Well Drained",    "PRECONDITION", None,               None, None, None, True,
     "Cargo tank, pump columns and pipelines well drained using the ship's stripping system to minimise ROB."),

    ("BF",    1, "Well Drained",    "PRECONDITION", None,               None, None, None, True, "First perform WD."),
    ("BF",    2, "Bottom Flush",    "CLEANING",     "Water",            None, None, None, True, "Bottom flush each cargo tank with water."),
    ("BF",    3, "Drain",           "DRAINING",     None,               None, None, None, True, "Drain well to remove all free-standing water/product."),

    ("BF-VP", 1, "Well Drained",    "PRECONDITION", None,               None, None, None, True, "First perform WD."),
    ("BF-VP", 2, "Bottom Flush",    "CLEANING",     "Water",            None, None, None, True, "Bottom flush each cargo tank with water."),
    ("BF-VP", 3, "Drain",           "DRAINING",     None,               None, None, None, True, "Drain well to remove all free-standing water/product."),
    ("BF-VP", 4, "Ventilate OR Purge", "VENTILATING", None,             None, None, None, True,
     "Ventilate OR purge the tank atmosphere - either satisfies the source; both are not required."),

    ("CW",    1, "Well Drained",    "PRECONDITION", None,               None, None, None, True, "First perform WD."),
    ("CW",    2, "Cold Water Wash", "CLEANING",     "Cold Water",       None, None, None, True, "Cold-water wash cargo tank(s) and lines."),
    ("CW",    3, "Drain",           "DRAINING",     None,               None, None, None, True, "Drain well to remove all free-standing water/product."),

    ("CW-VP", 1, "Well Drained",    "PRECONDITION", None,               None, None, None, True, "First perform WD."),
    ("CW-VP", 2, "Cold Water Wash", "CLEANING",     "Cold Water",       None, None, None, True, "Cold-water wash cargo tanks and lines."),
    ("CW-VP", 3, "Drain",           "DRAINING",     None,               None, None, None, True, "Drain well to remove all free-standing water/product."),
    ("CW-VP", 4, "Ventilate OR Purge", "VENTILATING", None,             None, None, None, True,
     "Ventilate OR purge the tank atmospheres - either satisfies the source; both are not required."),

    ("CWM",   1, "Well Drained",    "PRECONDITION", None,               None, None, None, True, "First perform WD."),
    ("CWM",   2, "Cold Water Wash", "CLEANING",     "Cold Water",       None, None, None, True, "Cold-water wash cargo tanks and lines."),
    ("CWM",   3, "Drain",           "DRAINING",     None,               None, None, None, True, "Drain well to remove all free-standing water/product."),
    ("CWM",   4, "Render Gas-Free", "GAS_FREEING",  None,               None, None, None, True, "Render the tank gas-free."),
    ("CWM",   5, "Mop Dry",         "MOPPING",      None,               None, None, None, True, "Mop the tank dry."),

    ("CFW",   1, "Well Drained",    "PRECONDITION", None,               None, None, None, True, "First perform WD."),
    ("CFW",   2, "Bulk Wash",       "CLEANING",     "Cold Sea Water",   None, None, None, False,
     "Bulk washing MAY be conducted with cold sea water. Optional - the bulk stage may equally use cold fresh water."),
    ("CFW",   3, "Final Wash",      "CLEANING",     "Cold Fresh Water", None, None, None, True,
     "The final wash MUST be cold fresh water. This is what makes a cold-sea-water bulk wash acceptable."),
    ("CFW",   4, "Drain",           "DRAINING",     None,               None, None, None, True, "After washing, drain the tank well to remove all free-standing water/product."),

    ("HW",    1, "Well Drained",    "PRECONDITION", None,               None, None, None, True, "First perform WD."),
    ("HW",    2, "Hot Water Wash",  "CLEANING",     "Hot Water",        None, None, None, True, "Hot-water wash cargo tank(s) and lines."),
    ("HW",    3, "Drain",           "DRAINING",     None,               None, None, None, True, "Drain well to remove all free-standing water/product."),
    ("HW",    4, "Ventilate OR Purge", "VENTILATING", None,             None, None, None, True,
     "Ventilate OR purge the tank atmospheres - either satisfies the source; both are not required."),

    ("HWM",   1, "Well Drained",    "PRECONDITION", None,               None, None, None, True, "First perform WD."),
    ("HWM",   2, "Hot Water Wash",  "CLEANING",     "Hot Water",        None, None, None, True, "Hot-water wash cargo tanks and lines."),
    ("HWM",   3, "Drain",           "DRAINING",     None,               None, None, None, True, "Drain well to remove all free-standing water/product."),
    ("HWM",   4, "Render Gas-Free", "GAS_FREEING",  None,               None, None, None, True, "Render the tank gas-free."),
    ("HWM",   5, "Mop Dry",         "MOPPING",      None,               None, None, None, True, "Mop the tank dry."),

    ("HFW",   1, "Well Drained",    "PRECONDITION", None,               None, None, None, True, "First perform WD."),
    ("HFW",   2, "Bulk Wash",       "CLEANING",     "Hot Sea Water",    None, None, None, False,
     "Bulk washing MAY be conducted with hot sea water. Optional - the bulk stage may equally use hot fresh water."),
    ("HFW",   3, "Final Wash",      "CLEANING",     "Hot Fresh Water",  None, None, None, True,
     "The final wash MUST be hot fresh water. This is what makes a hot-sea-water bulk wash acceptable."),
    ("HFW",   4, "Drain",           "DRAINING",     None,               None, None, None, True, "After washing, drain the tank well to remove all free-standing water/product."),
]

STEP_COLUMNS = ["procedure_code", "step_order", "step_name", "step_type", "medium",
                "temperature", "duration", "cleaner", "mandatory", "step_description"]

# ---------------------------------------------------------------------------
# Requirements - rules and limits that hold no place in the step sequence.
# ---------------------------------------------------------------------------
# (code, display_order, requirement_type, requirement_value, operator, unit,
#  mandatory, description)
REQUIREMENTS = [
    ("WD",    1, "ROB_LIMIT",    "0.05",            "<=", "%", True,
     "An ROB volume greater than 0.05% of the individual tank capacity does not meet the well-drained criteria."),
    ("WD",    2, "ROB_LOCATION", "Pump Well Only",  None, None, True,
     "Any ROB that remains should only ever be in the pump well."),

    ("BF",    1, "PRECONDITION", "WD", None, None, True, "WD must be completed before the bottom flush."),

    ("BF-VP", 1, "PRECONDITION", "WD", None, None, True, "WD must be completed first."),
    ("BF-VP", 2, "ATMOSPHERE_TREATMENT", "VENTILATE_OR_PURGE", None, None, True,
     "Ventilate OR purge. Either satisfies the requirement; the source does not require both."),

    ("CW",    1, "PRECONDITION", "WD", None, None, True, "WD must be completed before the cold water wash."),

    ("CW-VP", 1, "PRECONDITION", "WD", None, None, True, "WD must be completed first."),
    ("CW-VP", 2, "ATMOSPHERE_TREATMENT", "VENTILATE_OR_PURGE", None, None, True,
     "Ventilate OR purge. Either satisfies the requirement; the source does not require both."),

    ("CWM",   1, "PRECONDITION", "WD",       None, None, True, "WD must be completed first."),
    ("CWM",   2, "GAS_FREE",     "REQUIRED", None, None, True, "The tank must be rendered gas-free."),
    ("CWM",   3, "MOP_DRY",      "REQUIRED", None, None, True, "The tank must be mopped dry."),

    ("CFW",   1, "PRECONDITION",      "WD",               None, None, True,  "WD must be completed first."),
    ("CFW",   2, "BULK_WASH_MEDIUM",  "Cold Sea Water",   None, None, False,
     "Bulk washing MAY be conducted with cold sea water. An allowance, not an obligation - hence mandatory=false."),
    ("CFW",   3, "FINAL_WASH_MEDIUM", "Cold Fresh Water", None, None, True,
     "The final wash MUST be cold fresh water. This is the condition on which the sea-water bulk wash depends."),
    ("CFW",   4, "DRAIN_REQUIRED",    "true",             None, None, True,
     "After washing, the tank must be drained well."),

    ("HW",    1, "PRECONDITION", "WD", None, None, True, "WD must be completed first."),
    ("HW",    2, "ATMOSPHERE_TREATMENT", "VENTILATE_OR_PURGE", None, None, True,
     "Ventilate OR purge. Either satisfies the requirement; the source does not require both."),

    ("HWM",   1, "PRECONDITION", "WD",       None, None, True, "WD must be completed first."),
    ("HWM",   2, "GAS_FREE",     "REQUIRED", None, None, True, "The tank must be rendered gas-free."),
    ("HWM",   3, "MOP_DRY",      "REQUIRED", None, None, True, "The tank must be mopped dry."),

    ("HFW",   1, "PRECONDITION",      "WD",              None, None, True,  "WD must be completed first."),
    ("HFW",   2, "BULK_WASH_MEDIUM",  "Hot Sea Water",   None, None, False,
     "Bulk washing MAY be conducted with hot sea water. An allowance, not an obligation - hence mandatory=false."),
    ("HFW",   3, "FINAL_WASH_MEDIUM", "Hot Fresh Water", None, None, True,
     "The final wash MUST be hot fresh water. This is the condition on which the sea-water bulk wash depends."),
    ("HFW",   4, "DRAIN_REQUIRED",    "true",            None, None, True,
     "After washing, the tank must be drained well."),

    ("NC",    1, "LOADING_ALLOWED", "false", None, None, True,
     "The product must not be loaded into the tank(s). There is no cleaning procedure to execute."),
]

REQUIREMENT_COLUMNS = ["procedure_code", "requirement_type", "requirement_value", "operator",
                       "unit", "mandatory", "description", "display_order"]

# ---------------------------------------------------------------------------
# Instructions - source-level statements, in the source's own wording.
# ---------------------------------------------------------------------------
# (code, display_order, instruction_type, message, mandatory)
INSTRUCTIONS = [
    ("WD",  1, "IMPORTANT",
     "An ROB volume in excess of 0.05% of the individual tank capacity does not meet the well drained criteria.", True),
    ("CFW", 1, "IMPORTANT",
     "Bulk washing may be conducted with Cold Sea Water so long as a final wash with Cold Fresh Water is conducted.", True),
    ("HFW", 1, "IMPORTANT",
     "Bulk washing may be conducted with Hot Sea Water so long as a final wash with Hot Fresh Water is conducted.", True),
    ("NC",  1, "WARNING",
     "The last cargo is Not Compatible with the product to be loaded and therefore the product should not be loaded into the tank(s).", True),
]

INSTRUCTION_COLUMNS = ["procedure_code", "instruction_type", "message", "display_order", "mandatory"]

README_ROWS = [
    ("Workbook", "Shell tank-cleaning procedure templates, steps, requirements and instructions."),
    ("Imported by", "python3 etl/shell_procedure_templates.py --source-id <id>"),
    ("Cross-sheet key", "procedure_code"),
    ("procedure_templates", "Copied VERBATIM from the supplied 'Cleaning Codes' sheet. 11 codes."),
    ("procedure_template_steps", "Ordered actions. NC has none by design - it is a decision, not a procedure."),
    ("procedure_template_requirements", "Rules and limits: ROB_LIMIT, PRECONDITION, ATMOSPHERE_TREATMENT, ..."),
    ("procedure_template_instructions", "Source-level statements in the source's own wording."),
    ("Ventilate OR Purge", "Stored as ATMOSPHERE_TREATMENT = VENTILATE_OR_PURGE, one requirement, so the OR survives. Never as ventilation_required AND inert_gas_required."),
    ("CFW / HFW", "Bulk wash medium is mandatory=false (an allowance); final wash medium is mandatory=true (the condition)."),
    ("Editing", "Edit this workbook and re-run the importer. It is idempotent - rows are upserted, never duplicated."),
]


def build(source_workbook: Path, out: Path) -> None:
    if not source_workbook.exists():
        sys.exit(f"Error: source workbook not found: {source_workbook}")

    log.info("Reading templates verbatim from %s [%s]", source_workbook.name, SOURCE_TEMPLATE_SHEET)
    templates = pd.read_excel(source_workbook, sheet_name=SOURCE_TEMPLATE_SHEET)
    log.info("  %d template rows, columns: %s", len(templates), list(templates.columns))

    steps = pd.DataFrame(STEPS, columns=STEP_COLUMNS)
    reqs = pd.DataFrame([(c, t, v, o, u, m, d, n)
                         for (c, n, t, v, o, u, m, d) in REQUIREMENTS],
                        columns=REQUIREMENT_COLUMNS)
    instrs = pd.DataFrame([(c, t, m, n, mand)
                           for (c, n, t, m, mand) in INSTRUCTIONS],
                          columns=INSTRUCTION_COLUMNS)
    readme = pd.DataFrame(README_ROWS, columns=["item", "detail"])

    out.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        templates.to_excel(writer, sheet_name="procedure_templates", index=False)
        steps.to_excel(writer, sheet_name="procedure_template_steps", index=False)
        reqs.to_excel(writer, sheet_name="procedure_template_requirements", index=False)
        instrs.to_excel(writer, sheet_name="procedure_template_instructions", index=False)
        readme.to_excel(writer, sheet_name="README", index=False)

    log.info("✓ Wrote %s", out)
    log.info("  procedure_templates              %3d rows", len(templates))
    log.info("  procedure_template_steps         %3d rows (%d procedures; NC has none)",
             len(steps), steps["procedure_code"].nunique())
    log.info("  procedure_template_requirements  %3d rows", len(reqs))
    log.info("  procedure_template_instructions  %3d rows", len(instrs))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source-workbook", default=str(DEFAULT_SOURCE),
                    help="workbook holding the supplied 'Cleaning Codes' sheet")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="workbook to write")
    args = ap.parse_args()
    build(Path(args.source_workbook), Path(args.out))


if __name__ == "__main__":
    main()
