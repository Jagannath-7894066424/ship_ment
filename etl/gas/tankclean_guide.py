#!/usr/bin/env python3
"""
Load the TankClean Guide change-of-grade matrix for liquefied gases.

Source: "TankClean Guide" (source.json, category 'gas').

WHAT THIS FILE IS
-----------------
One sheet, "Tank Clean Guide", holding a 10 x 10 change-of-grade matrix: rows
are the LAST cargo, columns the NEXT cargo, and every off-diagonal cell says
what must be done between them. All 90 off-diagonal cells are filled and the
diagonal is empty throughout - a cargo followed by itself is not a change of
grade - which is checked on load rather than assumed.

    ETHYLENE   PROPYLENE   BUTADIENE   BUTYLENES   C4 RAFFINATE
    VINYL CHLORIDE MONOMER   PROPYLENE OXIDE   PROPANE DRY
    LPG C3/C4 PRO/BUT ETC    AMMONIA

A SECOND SHEET, "Sheet3", IS EMPTY and is ignored.

A CELL IS A RECIPE, NOT A CODE
-------------------------------
Unlike the Shell matrix, whose cells hold a regime number, a cell here holds the
procedure itself, printed over up to five spreadsheet rows:

    N2/IG/CH4        <- purge medium
    No vis. Insp     <- whether the tank must be entered and looked at
    Stand R + C      <- the condition the tank must be left in
    Gas < 5 %        <- the threshold that proves it

The lines are NOT at fixed positions. "No purging Required" spills its second
word onto the next row, "Standard" and "Condition" are printed on two rows, and
"N2 no vis. Insp" carries the medium and the inspection on one. So each line is
classified by what it says, not by where it sits, and a line matching no rule
is a hard error naming it - all 90 cells classify with nothing left over.

EVERY PAIR CARRIES ITS OWN STEPS
---------------------------------
There are no procedure_templates rows for this source, by instruction. Each of
the 90 pairs is one cleaning_process row, and the lines of its cell become that
row's own cleaning_process_step rows, in the order the sheet prints them.

The same recipe does recur - "N2 / Visual insp / Stand R + C" covers 12 pairs -
so the instructions are stored more than once. That is the deliberate trade:
a reader asking "what do I do between these two cargoes" gets the answer from
one row and its steps, with no second table to resolve first.

THE SHEET'S ABBREVIATIONS ARE EXPANDED, EVERY TIME
----------------------------------------------------
A cell is written in shorthand that only the legend column explains:

    No vis. Insp   no visual inspection is required. VISUAL INSP. is defined in
                   the legend: oxygen content 21% by volume, remaining vapour
                   below T.L.V. values.
    Stand R + C    BOTH Standard Requirement AND Standard Condition, each its
                   own legend entry - R is not a grade, it is "Requirement".
    Gas < 1000 ppm the threshold that proves the tank is ready.

So PROPYLENE -> ETHYLENE reads back as four steps:

    1  PURGING       Purge with N2
    2  INSPECTION    No visual inspection required
    3  PRECONDITION  Standard Requirement + Standard Condition, both spelled out
    4  CONDITION     Confirm Gas<1000ppm

Each step carries the legend's own words in `remarks`, taken from the sheet
rather than typed in here, so the shorthand never has to be decoded by whoever
reads the row later.

"Not Compatible" IS A VERDICT, NOT A PROCEDURE
-----------------------------------------------
Two cells - PROPYLENE OXIDE -> AMMONIA and AMMONIA -> PROPYLENE OXIDE - say
only "Not Compatible". They get:

  * a cleaning_process row like any other pair, so the matrix stays complete,
    whose single step is a DECISION saying the change must not be carried out,
  * ONE cargo_gas_compatibility row. That table stores an unordered pair once in
    canonical order, and the two cells agree, so one row states it.

No compatibility row is written for any other pair. The matrix says how to
change between them, which is not the same as declaring them compatible, and
inventing 43 "compatible" verdicts the source never printed would put words in
its mouth.

THE ELEVENTH COLUMN IS A LEGEND, NOT A CARGO
----------------------------------------------
It defines the terms the cells use - what a visual inspection requires, what
"standard condition" and "standard requirement" mean, the dew points. It is
loaded as procedure_note rows (the source's definitions, kept once) AND quoted
into the `remarks` of the steps that use each term, so a step is readable on
its own.

IDEMPOTENCY
-----------
Upsert throughout, on (gas_name, source_id), the cleaning_process pair index,
(cleaning_process_id, step_order) and (gas_a_id, gas_b_id, source_id). Step
slots no longer printed are deleted, so shortening a cell shortens it in the
database. The whole sheet is validated before anything is written; one
transaction.

Usage:
    python3 etl/gas/tankclean_guide.py
    python3 etl/gas/tankclean_guide.py --dry-run
    python3 etl/gas/tankclean_guide.py <file>
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

_ETL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ETL_ROOT / "common"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import input_file  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("tankclean_guide")

SOURCE_NAME = "TankClean Guide"
DEFAULT_FILE = input_file("TankClean Guide.xls")
SHEET = "Tank Clean Guide"
CARGO_TYPE = "GAS"

# Column index -> the NEXT cargo it heads. The sheet prints these names over two
# header rows ("VINYL/CHLRD" + "MONOMER"), so they are spelled out here once.
NEXT_CARGOES: Dict[int, str] = {
    1: "ETHYLENE", 2: "PROPYLENE", 3: "BUTADIENE", 4: "BUTYLENES",
    5: "C4 RAFFINATE", 6: "VINYL CHLORIDE MONOMER", 7: "PROPYLENE OXIDE",
    8: "PROPANE DRY", 9: "LPG C3/C4 PRO/BUT ETC", 10: "AMMONIA",
}

# (last cargo, first row, last row). Blocks are four rows except LPG, which the
# sheet prints over five. Read off the sheet and verified by the diagonal check.
BLOCKS: List[Tuple[str, int, int]] = [
    ("ETHYLENE", 2, 5), ("PROPYLENE", 6, 9), ("BUTADIENE", 10, 13),
    ("BUTYLENES", 14, 17), ("C4 RAFFINATE", 18, 21),
    ("VINYL CHLORIDE MONOMER", 22, 25), ("PROPYLENE OXIDE", 26, 29),
    ("PROPANE DRY", 30, 33), ("LPG C3/C4 PRO/BUT ETC", 34, 38),
    ("AMMONIA", 39, 42),
]

LEGEND_COLUMN = 11

# Line classification. Matched against the whole line; a line matching none of
# them stops the load, because a wording nobody has read is not something a
# loader should file under a guessed heading.
RE_MEDIUM = re.compile(r"^(N2|H2O|No purging|Not Compatible)", re.I)
RE_INSPECT = re.compile(r"insp", re.I)
RE_CONDITION = re.compile(r"^(stand|stad)", re.I)
RE_THRESHOLD = re.compile(r"^(gas|oxygen|o2)\b", re.I)
# "N2 no vis. Insp" prints the medium and the inspection in one cell.
RE_MEDIUM_WITH_INSPECT = re.compile(r"^(N2)\s+(no vis\.?\s*insp.*)$", re.I)

NOT_COMPATIBLE = "Not Compatible"
NO_PURGING = re.compile(r"^no purging", re.I)

# Lines the sheet breaks across two rows. The second is joined back onto the
# first rather than being classified on its own, where it would mean nothing.
CONTINUATIONS = {
    "No purging": {"requisted", "Required"},
    "Not": {"Compatible"},
    "H2O+N2+IG": {"CH4"},
}

# The legend column, as (note_code, title, the rows that hold its body).
LEGEND: List[Tuple[str, str, range]] = [
    ("TCG_VISUAL_INSP", "Visual inspection", range(1, 12)),
    ("TCG_STANDARD_CONDITION", "Standard condition", range(14, 22)),
    ("TCG_STANDARD_REQUIREMENT", "Standard requirement", range(26, 33)),
    ("TCG_DEW_POINT", "Dew point", range(34, 37)),
]
# Which legend note applies to a template, decided from what the template says
# rather than from a hand-kept list.
LEGEND_APPLIES = {
    "TCG_VISUAL_INSP": lambda r: bool(r["inspection"]) and "no" not in r["inspection"].lower(),
    "TCG_STANDARD_CONDITION": lambda r: bool(r["condition"]),
    "TCG_STANDARD_REQUIREMENT": lambda r: any("oxygen" in t.lower() or "o2" in t.lower()
                                              for t in r["thresholds"]),
    "TCG_DEW_POINT": lambda r: bool(r["medium"]) and not NO_PURGING.match(r["medium"])
                               and r["medium"] != NOT_COMPATIBLE,
}


def clean(value) -> str:
    if value is None:
        return ""
    s = re.sub(r"\s+", " ", str(value).replace(" ", " ")).strip()
    return "" if s.lower() == "nan" else s


def canonical(text: Optional[str]) -> str:
    """Fold the spacing and punctuation the sheet is inconsistent about.

    Used ONLY to decide whether two cells print the same procedure. The printed
    text itself is never replaced by this.
    """
    if not text:
        return ""
    s = re.sub(r"\s*([<>])\s*", r" \1", text)
    s = re.sub(r"(\d)\s*(ppm|%)", r"\1\2", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip().rstrip(",.").lower()
    s = re.sub(r"\bno\s+vis(?:ual)?\.?\s*insp\b.*", "no visual inspection", s)
    s = re.sub(r"\bvisual\s+insp\b.*", "visual inspection", s)
    return s


def classify(lines: List[str], where: str, errors: List[str]) -> Optional[dict]:
    """Turn a cell's printed lines into {medium, inspection, condition, thresholds}."""
    joined: List[str] = []
    for line in lines:
        if joined and line in CONTINUATIONS.get(joined[-1], set()):
            joined[-1] = f"{joined[-1]} {line}"
        elif joined and line.lower() == "condition" and RE_CONDITION.match(joined[-1]):
            joined[-1] = f"{joined[-1]} {line}"
        else:
            joined.append(line)

    rec = {"medium": None, "inspection": None, "condition": None,
           "thresholds": [], "printed": " | ".join(joined)}
    for line in joined:
        if RE_MEDIUM.match(line) and rec["medium"] is None:
            pair = RE_MEDIUM_WITH_INSPECT.match(line)
            if pair:
                rec["medium"], rec["inspection"] = pair.group(1), pair.group(2)
            else:
                rec["medium"] = line
        elif RE_INSPECT.search(line) and rec["inspection"] is None:
            rec["inspection"] = line
        elif RE_CONDITION.match(line) and rec["condition"] is None:
            rec["condition"] = line
        elif RE_THRESHOLD.match(line):
            rec["thresholds"].append(line)
        else:
            errors.append(f"{where}: cannot classify the line {line!r}. Add a "
                          f"rule to etl/gas/tankclean_guide.py.")
            return None
    if rec["medium"] is None:
        errors.append(f"{where}: no purge medium in {joined!r}")
        return None
    return rec


def signature(rec: dict) -> str:
    """A cell's procedure, with the sheet's inconsistent spacing folded away.

    Used only for the run report - to say how many DISTINCT procedures the 90
    cells print - never to merge rows. Every pair keeps its own steps.
    """
    return "|".join([canonical(rec["medium"]), canonical(rec["inspection"]),
                     canonical(rec["condition"]),
                     ",".join(sorted(canonical(t) for t in rec["thresholds"]))])


# "Stand R + C" is not one term but two: Standard Requirement AND Standard
# Condition, each defined separately in the legend column. Anything else naming
# a condition is the Standard Condition alone.
RE_REQ_AND_COND = re.compile(r"\br\s*\+\s*c\b", re.I)
RE_COND_ONLY = re.compile(r"cond", re.I)


def expand_condition(text: str, legend: Dict[str, str]) -> List[Tuple[str, str]]:
    """The legend entries a condition cell stands for, in the order printed."""
    if RE_REQ_AND_COND.search(text):
        return [("Standard Requirement", legend["TCG_STANDARD_REQUIREMENT"]),
                ("Standard Condition", legend["TCG_STANDARD_CONDITION"])]
    if RE_COND_ONLY.search(text):
        return [("Standard Condition", legend["TCG_STANDARD_CONDITION"])]
    return []


def steps_for(rec: dict, legend: Dict[str, str]) -> List[dict]:
    """One step per line the cell prints, in the sheet's own order.

    `description` says what to do and what the shorthand means; `remarks` quotes
    the legend entry the shorthand refers to, so the step needs nothing else to
    be understood.
    """
    steps: List[dict] = []
    medium = rec["medium"]

    if medium == NOT_COMPATIBLE:
        return [{"type": "DECISION", "method": NOT_COMPATIBLE, "medium": None,
                 "description": "The guide marks this change of grade 'Not "
                                "Compatible': it must not be carried out.",
                 "remarks": None}]

    if NO_PURGING.match(medium):
        steps.append({"type": "PRECONDITION", "method": "No purging", "medium": None,
                      "description": f"The guide prints {medium!r}: no purge is "
                                     f"called for between these two cargoes.",
                      "remarks": None})
    else:
        steps.append({"type": "PURGING", "method": "Purge", "medium": medium,
                      "description": f"Purge the tank with {medium}.",
                      "remarks": legend["TCG_DEW_POINT"]})

    if rec["inspection"]:
        required = "no" not in rec["inspection"].lower()
        steps.append({
            "type": "INSPECTION",
            "method": "Visual inspection" if required else "No visual inspection",
            "medium": None,
            "description": (f"The guide prints {rec['inspection']!r}: a visual "
                            f"inspection is "
                            f"{'required' if required else 'NOT required'}."),
            "remarks": legend["TCG_VISUAL_INSP"]})

    if rec["condition"]:
        parts = expand_condition(rec["condition"], legend)
        if parts:
            names = " and ".join(name for name, _ in parts)
            steps.append({
                "type": "PRECONDITION", "method": names, "medium": None,
                "description": (f"The guide prints {rec['condition']!r}, which "
                                f"means {names}. Leave the tank meeting both."
                                if len(parts) > 1 else
                                f"The guide prints {rec['condition']!r}, which "
                                f"means {names}. Leave the tank meeting it."),
                "remarks": " / ".join(f"{name}: {body}" for name, body in parts)})
        else:
            steps.append({"type": "PRECONDITION", "method": rec["condition"],
                          "medium": None,
                          "description": f"Leave the tank in the condition the "
                                         f"guide prints as {rec['condition']!r}.",
                          "remarks": None})

    for threshold in rec["thresholds"]:
        steps.append({"type": "CONDITION", "method": "Verify threshold",
                      "medium": None,
                      "description": f"Confirm {threshold} before loading.",
                      "remarks": None})
    return steps


def read_matrix(path: Path) -> Tuple[List[dict], Dict[str, str], List[str]]:
    errors: List[str] = []
    sheets = pd.ExcelFile(path).sheet_names
    if SHEET not in sheets:
        return [], {}, [f"no sheet named {SHEET!r}; the workbook has {sheets!r}"]
    grid = pd.read_excel(path, sheet_name=SHEET, header=None).values.tolist()

    def cell(r: int, c: int) -> str:
        if r >= len(grid) or c >= len(grid[r]):
            return ""
        return clean(grid[r][c])

    pairs: List[dict] = []
    for last, r0, r1 in BLOCKS:
        for ci, nxt in NEXT_CARGOES.items():
            lines = [cell(r, ci) for r in range(r0, r1 + 1)]
            lines = [l for l in lines if l]
            where = f"{last!r} -> {nxt!r}"
            if last == nxt:
                # The diagonal must stay empty: a cargo followed by itself is
                # not a change of grade, and a value there would mean the block
                # boundaries are wrong.
                if lines:
                    errors.append(f"{where}: the diagonal should be empty but "
                                  f"holds {lines!r}")
                continue
            if not lines:
                errors.append(f"{where}: empty cell; the matrix should be complete")
                continue
            rec = classify(lines, where, errors)
            if rec is None:
                continue
            rec.update({"from": last, "to": nxt})
            pairs.append(rec)

    legend: Dict[str, str] = {}
    for code, _title, rows in LEGEND:
        body = " ".join(x for x in (cell(r, LEGEND_COLUMN) for r in rows) if x)
        if not body:
            errors.append(f"legend {code}: no text in column {LEGEND_COLUMN}")
        legend[code] = body
    return pairs, legend, errors


def report(pairs, legend):
    distinct={signature(p) for p in pairs}
    log.info("%d directed pair(s) over %d cargo(es); %d distinct procedure(s) "
             "(each pair keeps its own steps)", len(pairs),
             len({p["from"] for p in pairs}|{p["to"] for p in pairs}), len(distinct))
    log.info("    purge media: %s", ", ".join(sorted({p["medium"] for p in pairs})))
    bad=[p for p in pairs if p["medium"]==NOT_COMPATIBLE]
    log.info("    %d pair(s) marked %r:", len(bad), NOT_COMPATIBLE)
    for p in bad: log.info("        %s -> %s", p["from"], p["to"])
    log.info("    %d step(s) in total", sum(len(steps_for(p,legend)) for p in pairs))
    log.info("    %d cell(s) print 'R + C' -> BOTH Standard Requirement and "
             "Standard Condition",
             sum(1 for p in pairs if p["condition"]
                 and len(expand_condition(p["condition"],legend))>1))
    for code,title,_ in LEGEND:
        log.info("    legend %-24s %s", title, legend[code][:58])



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

    pairs, legend, errors = read_matrix(path)
    if errors:
        log.error("%d validation error(s) - nothing was imported:", len(errors))
        for e in errors[:25]:
            log.error("    %s", e)
        return 1

    report(pairs, legend)
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
            cur.execute("SELECT id FROM source WHERE name = %s", (SOURCE_NAME,))
            row = cur.fetchone()
            if row is None:
                sys.exit(f"Error: source {SOURCE_NAME!r} not found.\n"
                         f"  It is declared in etl/data/source.json - register it with:\n"
                         f"      python3 etl/common/source.py")
            source_id = row[0]
            log.info("Source id=%s (%r)", source_id, SOURCE_NAME)
            page_ref = f"{path.name} [{SHEET}]"

            # --- cargoes ---------------------------------------------------
            gas_ids: Dict[str, int] = {}
            for name in sorted({p["from"] for p in pairs} | {p["to"] for p in pairs}):
                cur.execute(
                    "INSERT INTO cargo_gas (gas_name, source_id, created_at, updated_at) "
                    "VALUES (%s, %s, now(), now()) "
                    "ON CONFLICT (gas_name, source_id) DO UPDATE SET updated_at = now() "
                    "RETURNING id", (name, source_id))
                gas_ids[name] = cur.fetchone()[0]
            log.info("cargo_gas: %d", len(gas_ids))

            # --- legend notes ----------------------------------------------
            note_ids: Dict[str, int] = {}
            for code, title, _ in LEGEND:
                cur.execute(
                    "INSERT INTO procedure_note (note_code, title, body, source_id, "
                    "created_at, updated_at) VALUES (%s, %s, %s, %s, now(), now()) "
                    "ON CONFLICT (note_code) DO UPDATE SET title = EXCLUDED.title, "
                    "body = EXCLUDED.body, source_id = EXCLUDED.source_id, "
                    "updated_at = now() RETURNING id",
                    (code, title, legend[code], source_id))
                note_ids[code] = cur.fetchone()[0]
            log.info("procedure_note: %d", len(note_ids))

            # --- the 90 pairs, each carrying its own steps ------------------
            n_steps = pruned = 0
            for rec in pairs:
                cur.execute(
                    """
                    INSERT INTO cleaning_process
                        (cargo_type, from_cargo_id, to_cargo_id, source_id,
                         title, condition, source_page_ref, created_at, updated_at)
                    VALUES (%s::"CargoType", %s, %s, %s, %s, %s, %s, now(), now())
                    ON CONFLICT (from_cargo_id, to_cargo_id, source_id,
                                 COALESCE(condition, ''))
                        WHERE from_cargo_id IS NOT NULL AND to_cargo_id IS NOT NULL
                    DO UPDATE SET cargo_type      = EXCLUDED.cargo_type,
                                  title           = EXCLUDED.title,
                                  source_page_ref = EXCLUDED.source_page_ref,
                                  updated_at      = now()
                    RETURNING id
                    """,
                    (CARGO_TYPE, gas_ids[rec["from"]], gas_ids[rec["to"]], source_id,
                     f"{rec['from']} -> {rec['to']}", rec["printed"], page_ref))
                process_id = cur.fetchone()[0]

                steps = steps_for(rec, legend)
                for order, step in enumerate(steps, start=1):
                    cur.execute(
                        """
                        INSERT INTO cleaning_process_step
                            (cleaning_process_id, step_order, step_type, method,
                             medium, description, remarks, mandatory,
                             created_at, updated_at)
                        VALUES (%s, %s, %s::"CleaningStepType", %s, %s, %s, %s,
                                TRUE, now(), now())
                        ON CONFLICT (cleaning_process_id, step_order) DO UPDATE SET
                            step_type   = EXCLUDED.step_type,
                            method      = EXCLUDED.method,
                            medium      = EXCLUDED.medium,
                            description = EXCLUDED.description,
                            remarks     = EXCLUDED.remarks,
                            mandatory   = EXCLUDED.mandatory,
                            updated_at  = now()
                        """,
                        (process_id, order, step["type"], step["method"],
                         step["medium"], step["description"], step["remarks"]))
                    n_steps += 1
                cur.execute("DELETE FROM cleaning_process_step "
                            " WHERE cleaning_process_id = %s AND step_order > %s",
                            (process_id, len(steps)))
                pruned += cur.rowcount
            log.info("cleaning_process: %d | cleaning_process_step: %d "
                     "(%d stale slot(s) removed)", len(pairs), n_steps, pruned)


            # --- the one incompatibility -----------------------------------
            incompatible = {tuple(sorted((p["from"], p["to"])))
                            for p in pairs if p["medium"] == NOT_COMPATIBLE}
            for a, b in sorted(incompatible):
                a_id, b_id = gas_ids[a], gas_ids[b]
                if a_id > b_id:
                    a_id, b_id = b_id, a_id
                cur.execute(
                    """
                    INSERT INTO cargo_gas_compatibility
                        (gas_a_id, gas_b_id, compatible, source_id, raw_value,
                         source_page_ref, notes, created_at, updated_at)
                    VALUES (%s, %s, FALSE, %s, %s, %s, %s, now(), now())
                    ON CONFLICT (gas_a_id, gas_b_id, source_id) DO UPDATE SET
                        compatible = EXCLUDED.compatible,
                        raw_value  = EXCLUDED.raw_value,
                        notes      = EXCLUDED.notes,
                        updated_at = now()
                    """,
                    (a_id, b_id, source_id, NOT_COMPATIBLE, page_ref,
                     "Both cells of this unordered pair print 'Not Compatible', "
                     "so the pair is stored once in canonical order. The matrix "
                     "asserts nothing about any other pair's compatibility, so no "
                     "other pair has a row here."))
            log.info("cargo_gas_compatibility: %d", len(incompatible))

        conn.commit()
        log.info("✓ Committed.")
        return 0
    except Exception:
        conn.rollback()
        log.exception("Import failed - rolled back, nothing written.")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
