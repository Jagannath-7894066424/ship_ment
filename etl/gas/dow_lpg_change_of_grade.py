#!/usr/bin/env python3
"""
Load the DOW LPG change-of-grade guidelines into cargo_gas, cleaning_process
and cleaning_process_step.

SOURCE
------
"DOW LPG Change of Grade Guidelines" (source.json, category 'gas'). A gas
carrier's change-of-grade matrix: what must be done to a cargo tank that last
held one LPG grade before it may load the next. It is the FIRST cleaning source
in the gas branch - cleaning_process and cleaning_process_step have held only
CHEMICAL and OIL rows until now - and it is ranked for rank_cleaning alone,
because it measures nothing.

THIS WORKBOOK IS A TRANSCRIPTION, NOT THE APPROVED PROCEDURE
--------------------------------------------------------------
Its own README says so: "This workbook is a structured transcription/
interpretation of the supplied source. Operational use must be checked against
the approved DOW/shipboard procedure." That sentence is carried onto every row
this loader writes. A cleaning rule read out of a database and acted on is a
safety decision, and the database must not present a second-hand
interpretation as if it were the approved document.

WHAT ONE ROW STATES
-------------------
144 DIRECTED pairs - 13 previous cargoes x 12 next cargoes, less the 12
same-cargo cells. Ammonia is previous-only: the matrix never has it as a
destination, so it contributes 12 rows and takes none.

Direction matters and the matrix is not symmetric. Butane -> butadiene requires
a purge to below 2%; butadiene -> butane requires only a liquid-free tank.

THE PREVIOUS CARGO IS A VAPOUR, THE NEXT IS A CARGO
-----------------------------------------------------
The matrix names its rows "AMMONIA vapour", "VCM vapour", "PROPYLENE poly
vapour" and its columns "BUTANE", "VCM", "PROPYLENE polymer". The row is the
residue the last cargo left behind, which is what the tank actually contains
and what the procedure has to remove.

The workbook's own 'Cargo Reference' sheet maps each printed name to a
normalised one, and that map is used rather than stripping " vapour" here: the
sheet is the source's own statement of which row and which column are the same
cargo, and deriving it independently would be this loader deciding that
"PROPYLENE poly vapour" and "PROPYLENE polymer" are one thing.

THE CODES DECOMPOSE, AND THE DECOMPOSITION IS PROVED RATHER THAN ASSUMED
--------------------------------------------------------------------------
14 requirement codes are built from 5 atomic keys defined on the workbook's
'Keys Definitions' sheet - W, V, N2, N2/I, L.F - optionally carrying a
threshold ("N2/I<5%"). Each becomes ordered cleaning_process_step rows.

The split is not trusted on sight. Every code is reassembled from its parts
into the sentence the workbook prints in its own 'Detailed Instruction' column,
and a code whose reassembly does not match that sentence EXACTLY stops the
load. So "W,V,N2/I" is only split into three steps because doing so rebuilds
"Water wash. Visual inspection. Purge with nitrogen or inert gas." - the
workbook's own words - and not because commas usually mean steps.

"THE SPECIFIED VALUE" IS NOT SPECIFIED
--------------------------------------
A threshold reads "until the specified value is below 5%", and the workbook
never says WHAT is below 5% - presumably the previous cargo's vapour
concentration, but it does not say so. The limit is stored as printed, on the
step it qualifies, and its note records that the quantity is unnamed in the
source. Naming it here would be this loader adding a measurement the guideline
never made.

O2 CONTENT BELONGS TO THE DESTINATION
-------------------------------------
The O2 limit is a property of the cargo about to be loaded, not of the
transition: the workbook lists it once per next cargo, and its README says so -
"O2 content is listed by destination/Next Cargo exactly as supplied". Twelve
figures, repeated across the matrix.

It is written to cleaning_process.condition, because it is the acceptance
criterion this transition has to meet before loading and nothing else in these
two tables holds it. It is the same figure for every row sharing a destination,
which is the source's own doing and not a fanning-out by this loader.

NOT PERMITTED
-------------
One pair is prohibited outright: ammonia vapour -> propylene oxide. It gets its
cleaning_process row like any other - a rule that a transition may not be made
is still a rule - carrying procedure_code 'not permitted' and a single
RESTRICTION step, so a reader walking the steps of a process meets the
prohibition instead of an empty list it might read as "nothing to do".

THREE SHEETS ARE THE SAME DATA
------------------------------
'Change of Grade Instructions' holds all 144 pairs in long form. 'Original
Matrix' holds the same cells in wide form and 'AMMONIA Vapour' repeats the 12
ammonia rows. Only the long sheet is loaded, and the other two are checked
cell-for-cell against it first: a disagreement between two copies of one matrix
is not something to resolve by preferring a sheet.

IDEMPOTENCY
-----------
This source's cleaning_process rows are DELETED and rewritten on every run,
which cascades to their steps. That is the pattern run_gas.sh calls for - a
loader removes its own rows and never truncates a table three branches share.
cargo_gas is upserted on (gas_name, source_id). Everything is validated before
anything is written; one transaction.

NO PROPERTIES ARE WRITTEN
-------------------------
cargo_gas rows are created for the 13 cargo names and nothing else. This source
states no property of any cargo - it states what to do between two of them - so
it writes no cargo_gas_property_values at all.

Usage:
    python3 etl/gas/dow_lpg_change_of_grade.py
    python3 etl/gas/dow_lpg_change_of_grade.py --dry-run
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
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _gas import upsert_gas  # noqa: E402
from _paths import input_file  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("gas_dow_lpg")

SOURCE_NAME = "DOW LPG Change of Grade Guidelines"
DEFAULT_FILE = input_file("DOW_LPG_Change_of_Grade_Guidelines_COMPLETE.xlsx")

SHEET_MAIN = "Change of Grade Instructions"
SHEET_WIDE = "Original Matrix"
SHEET_AMMONIA = "AMMONIA Vapour"
SHEET_KEYS = "Keys Definitions"
SHEET_CARGO = "Cargo Reference"
SHEET_O2 = "O2 Content"
SHEET_README = "README"

MAIN_HEADER = ["Previous Cargo", "Next Cargo", "Requirement",
               "Detailed Instruction", "Next Cargo O2 Requirement", "Status"]

NOT_PERMITTED = "not permitted"

# One atomic key -> how it becomes a cleaning_process_step, and the sentence the
# workbook writes for it. The sentence is what proves the decomposition: a code
# is only split into these parts if reassembling their sentences reproduces the
# workbook's own 'Detailed Instruction' text exactly.
KEYS = {
    "W":    {"sentence": "Water wash",
             "step_type": "CLEANING", "method": "Water wash", "medium": "Water"},
    "V":    {"sentence": "Visual inspection",
             "step_type": "INSPECTION", "method": "Visual inspection", "medium": None},
    "N2":   {"sentence": "Purge with nitrogen only",
             "step_type": "PURGING", "method": "Purge", "medium": "Nitrogen"},
    "N2/I": {"sentence": "Purge with nitrogen or inert gas",
             "step_type": "PURGING", "method": "Purge",
             "medium": "Nitrogen or inert gas"},
    "L.F":  {"sentence": "Ensure liquid-free condition",
             "step_type": "PRECONDITION", "method": "Ensure liquid-free condition",
             "medium": None},
}

# How the workbook words a threshold, and the sentence a prohibited pair gets.
THRESHOLD_SENTENCE = " until the specified value is below {limit}"
NOT_PERMITTED_SENTENCE = ("This cargo change is not permitted according to the "
                          "supplied guideline")

THRESHOLD_NOTE = (
    "The guideline gives this limit as 'the specified value', and nowhere says "
    "WHAT quantity is being held below it - presumably the previous cargo's "
    "vapour concentration, but the workbook does not state that. The limit is "
    "stored exactly as printed and the quantity is deliberately left unnamed.")

TRANSCRIPTION_NOTE = ""     # filled from the README at run time


def cell(value) -> str:
    """Trim a cell and collapse its whitespace; NaN becomes ''."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return re.sub(r"\s+", " ", str(value).replace("\n", " ")).strip()


def grid_of(path: Path, sheet: str) -> List[List[str]]:
    return [[cell(v) for v in row] for row in
            pd.read_excel(path, sheet_name=sheet, header=None).values.tolist()]


def sentence(text: str) -> str:
    """A detailed-instruction sentence without its full stop, for comparison."""
    return text.strip().rstrip(".").strip()


def split_code(code: str, errors: List[str],
               where: str) -> Optional[List[Tuple[str, Optional[str]]]]:
    """A requirement code as ordered (key, threshold) pairs.

    "W,V,N2/I"  -> [(W, None), (V, None), (N2/I, None)]
    "N2/I<5%"   -> [(N2/I, '5%')]

    The key is matched against the LONGEST known key that the token starts with,
    so "N2/I<5%" is the N2/I key and not the N2 key with a stray "/I".
    """
    out: List[Tuple[str, Optional[str]]] = []
    for token in (t.strip() for t in code.split(",")):
        if not token:
            errors.append(f"{where}: requirement {code!r} has an empty part")
            return None
        match = None
        for key in sorted(KEYS, key=len, reverse=True):
            if token == key or token.startswith(key + "<"):
                match = key
                break
        if match is None:
            errors.append(f"{where}: {token!r} in requirement {code!r} is not "
                          f"one of the keys the workbook defines "
                          f"({', '.join(sorted(KEYS))})")
            return None
        rest = token[len(match):]
        limit = None
        if rest:
            m = re.fullmatch(r"<\s*(.+)", rest)
            if not m:
                errors.append(f"{where}: {token!r} carries {rest!r} after the "
                              f"key {match!r}, which is not a '<limit' threshold")
                return None
            limit = m.group(1).strip()
        out.append((match, limit))
    return out


def rebuild(parts: List[Tuple[str, Optional[str]]]) -> str:
    """The workbook's own instruction text, rebuilt from the split parts.

    Compared against the sheet's 'Detailed Instruction' column; a mismatch means
    the code was not decomposed the way the source reads it, and stops the load.
    """
    said = []
    for key, limit in parts:
        text = KEYS[key]["sentence"]
        if limit:
            text += THRESHOLD_SENTENCE.format(limit=limit)
        said.append(text + ".")
    return " ".join(said)


def read_reference(path: Path, errors: List[str]) -> Dict[str, str]:
    """Printed cargo name -> the workbook's own normalised name."""
    grid = grid_of(path, SHEET_CARGO)
    if not grid or grid[0][:2] != ["Source Row Name", "Normalized Cargo Name"]:
        errors.append(f"{SHEET_CARGO!r} does not start with the expected "
                      f"'Source Row Name' / 'Normalized Cargo Name' header; it "
                      f"reads {grid[0] if grid else []!r}")
        return {}
    out: Dict[str, str] = {}
    for row in grid[1:]:
        printed, normalised = (row + ["", ""])[:2]
        if not printed:
            continue
        if printed in out and out[printed] != normalised:
            errors.append(f"{SHEET_CARGO!r} maps {printed!r} to both "
                          f"{out[printed]!r} and {normalised!r}")
            continue
        out[printed] = normalised
    return out


def read_keys(path: Path, errors: List[str]) -> Dict[str, str]:
    """Key -> the workbook's detailed explanation of it."""
    grid = grid_of(path, SHEET_KEYS)
    out: Dict[str, str] = {}
    for row in grid[1:]:
        key = (row + [""])[0]
        if key:
            out[key] = (row + ["", "", ""])[2]
    unknown = set(out) - set(KEYS)
    missing = set(KEYS) - set(out)
    if unknown or missing:
        errors.append(f"{SHEET_KEYS!r} defines {sorted(out)}, but this loader "
                      f"knows {sorted(KEYS)}. Unknown: {sorted(unknown) or 'none'}. "
                      f"Undefined: {sorted(missing) or 'none'}")
    return out


def read_o2(path: Path, errors: List[str]) -> Dict[str, str]:
    """Next cargo -> its O2 content requirement, as printed."""
    grid = grid_of(path, SHEET_O2)
    out: Dict[str, str] = {}
    for row in grid[1:]:
        cargo, limit = (row + ["", ""])[:2]
        if cargo:
            out[cargo] = limit
    if not out:
        errors.append(f"{SHEET_O2!r} carries no cargo/limit rows")
    return out


def read_readme(path: Path) -> str:
    for row in grid_of(path, SHEET_README):
        if row and row[0].lower().startswith("important"):
            return (row + [""])[1]
    return ""


def check_copies(path: Path, pairs: Dict[Tuple[str, str], str],
                 errors: List[str]) -> Tuple[int, int]:
    """Verify the wide matrix and the ammonia sheet against the long sheet."""
    wide = grid_of(path, SHEET_WIDE)
    checked = 0
    if wide:
        columns = {i: c for i, c in enumerate(wide[0]) if i and c}
        for row in wide[2:]:                    # row 1 is the O2 header line
            previous = (row + [""])[0]
            if not previous:
                continue
            for i, nxt in columns.items():
                value = (row + [""] * (i + 1))[i]
                if not value:
                    continue                    # the diagonal
                got = pairs.get((previous, nxt))
                if got is None:
                    errors.append(f"{SHEET_WIDE!r} has {previous!r} -> {nxt!r}, "
                                  f"which {SHEET_MAIN!r} does not")
                elif got != value:
                    errors.append(f"{previous!r} -> {nxt!r} reads {value!r} on "
                                  f"{SHEET_WIDE!r} but {got!r} on {SHEET_MAIN!r}; "
                                  f"two copies of one matrix disagree")
                checked += 1

    ammonia = grid_of(path, SHEET_AMMONIA)
    checked_ammonia = 0
    for row in ammonia[1:]:
        previous, nxt, requirement = (row + ["", "", ""])[:3]
        if not previous:
            continue
        got = pairs.get((previous, nxt))
        if got is None:
            errors.append(f"{SHEET_AMMONIA!r} has {previous!r} -> {nxt!r}, which "
                          f"{SHEET_MAIN!r} does not")
        elif got != requirement:
            errors.append(f"{previous!r} -> {nxt!r} reads {requirement!r} on "
                          f"{SHEET_AMMONIA!r} but {got!r} on {SHEET_MAIN!r}")
        checked_ammonia += 1
    return checked, checked_ammonia


def read_file(path: Path) -> Tuple[dict, List[str]]:
    """Parse the workbook. Returns (parsed, errors); nothing is written unless
    errors is empty."""
    errors: List[str] = []
    names = pd.ExcelFile(path).sheet_names
    for sheet in (SHEET_MAIN, SHEET_WIDE, SHEET_AMMONIA, SHEET_KEYS,
                  SHEET_CARGO, SHEET_O2):
        if sheet not in names:
            errors.append(f"the workbook has no {sheet!r} sheet; it holds {names!r}")
    if errors:
        return {}, errors

    reference = read_reference(path, errors)
    keys = read_keys(path, errors)
    o2 = read_o2(path, errors)
    readme = read_readme(path)
    if not readme:
        errors.append(f"{SHEET_README!r} no longer carries its 'Important' line. "
                      f"That line is what records this workbook as a "
                      f"transcription rather than the approved procedure, and "
                      f"every row written from it has to carry that")

    grid = grid_of(path, SHEET_MAIN)
    if not grid or grid[0][:len(MAIN_HEADER)] != MAIN_HEADER:
        errors.append(f"{SHEET_MAIN!r} header reads {grid[0] if grid else []!r}, "
                      f"expected {MAIN_HEADER!r}")
        return {}, errors

    rows: List[dict] = []
    printed_pairs: Dict[Tuple[str, str], str] = {}
    for line, raw in enumerate(grid[1:], start=2):
        previous, nxt, requirement, detailed, o2_printed, status = \
            (raw + [""] * 6)[:6]
        if not previous and not nxt:
            continue
        where = f"{SHEET_MAIN!r} line {line}"
        if not previous or not nxt:
            errors.append(f"{where}: a row with only one cargo named")
            continue
        if (previous, nxt) in printed_pairs:
            errors.append(f"{where}: {previous!r} -> {nxt!r} appears twice")
            continue
        printed_pairs[(previous, nxt)] = requirement

        from_name = reference.get(previous)
        to_name = reference.get(nxt)
        for printed, resolved in ((previous, from_name), (nxt, to_name)):
            if resolved is None:
                errors.append(f"{where}: {printed!r} is not in {SHEET_CARGO!r}, "
                              f"so this loader has no normalised name for it")
        if from_name is None or to_name is None:
            continue
        if from_name == to_name:
            errors.append(f"{where}: {previous!r} -> {nxt!r} both normalise to "
                          f"{from_name!r}; a cargo cannot follow itself")
            continue

        prohibited = requirement.strip().lower() == NOT_PERMITTED
        parts: List[Tuple[str, Optional[str]]] = []
        if prohibited:
            if sentence(detailed) != NOT_PERMITTED_SENTENCE:
                errors.append(f"{where}: a 'not permitted' row reads "
                              f"{detailed!r}, not the wording the workbook uses "
                              f"elsewhere ({NOT_PERMITTED_SENTENCE!r})")
                continue
        else:
            split = split_code(requirement, errors, where)
            if split is None:
                continue
            parts = split
            if rebuild(parts) != detailed.strip():
                errors.append(
                    f"{where}: requirement {requirement!r} splits into "
                    f"{[k for k, _ in parts]}, which rebuilds as "
                    f"{rebuild(parts)!r}, but the workbook's own instruction "
                    f"reads {detailed!r}. The split does not match the source")
                continue

        expected_o2 = o2.get(nxt)
        if expected_o2 is None:
            errors.append(f"{where}: {nxt!r} has no entry on {SHEET_O2!r}")
        elif expected_o2 != o2_printed:
            errors.append(f"{where}: the O2 requirement for {nxt!r} reads "
                          f"{o2_printed!r} here but {expected_o2!r} on "
                          f"{SHEET_O2!r}")

        rows.append({"line": line, "from_printed": previous, "to_printed": nxt,
                     "from_name": from_name, "to_name": to_name,
                     "code": requirement, "detailed": detailed,
                     "o2": o2_printed, "status": status,
                     "prohibited": prohibited, "parts": parts})

    if not rows:
        errors.append(f"{SHEET_MAIN!r} has a header but no data rows")
        return {}, errors

    wide_checked, ammonia_checked = check_copies(path, printed_pairs, errors)

    cargoes = sorted({r["from_name"] for r in rows} | {r["to_name"] for r in rows})
    return ({"rows": rows, "cargoes": cargoes, "keys": keys, "o2": o2,
             "readme": readme, "wide_checked": wide_checked,
             "ammonia_checked": ammonia_checked}, errors)


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


def report(parsed: dict) -> None:
    rows = parsed["rows"]
    log.info("%d directed pair(s) over %d cargo(es)", len(rows),
             len(parsed["cargoes"]))
    log.info("    cargoes: %s", ", ".join(parsed["cargoes"]))
    log.info("    %r verified against %r: %d cell(s); %r: %d row(s)",
             SHEET_WIDE, SHEET_MAIN, parsed["wide_checked"],
             SHEET_AMMONIA, parsed["ammonia_checked"])

    from collections import Counter
    codes = Counter(r["code"] for r in rows)
    log.info("    %d distinct requirement code(s), each split into steps and "
             "checked against the workbook's own instruction text:", len(codes))
    for code, n in sorted(codes.items()):
        row = next(r for r in rows if r["code"] == code)
        steps = ("PROHIBITED" if row["prohibited"]
                 else " -> ".join(f"{k}{'<' + l if l else ''}" for k, l in row["parts"]))
        log.info("        %-14s x%-3d %s", code, n, steps)

    total_steps = sum(1 if r["prohibited"] else len(r["parts"]) for r in rows)
    log.info("    cleaning_process rows: %d | cleaning_process_step rows: %d",
             len(rows), total_steps)

    o2 = parsed["o2"]
    log.info("    O2 requirement by destination (stored as cleaning_process."
             "condition): %s", ", ".join(f"{k}={v}" for k, v in sorted(o2.items())))

    prohibited = [f"{r['from_name']} -> {r['to_name']}" for r in rows if r["prohibited"]]
    if prohibited:
        log.warning("    %d pair(s) the guideline does NOT permit: %s",
                    len(prohibited), "; ".join(prohibited))
    log.info("    every row carries the workbook's own caveat: %s", parsed["readme"])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", default=str(DEFAULT_FILE))
    ap.add_argument("--dry-run", action="store_true",
                    help="parse and report, write nothing")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.is_file():
        sys.exit(f"Error: file not found: {path}")

    parsed, errors = read_file(path)
    if errors:
        log.error("%d validation error(s) - nothing was imported:", len(errors))
        for e in errors:
            log.error("    %s", e)
        return 1

    report(parsed)
    if args.dry_run:
        log.info("--dry-run: nothing written.")
        return 0

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        sys.exit("Error: DATABASE_URL not set (checked .env).")

    page_ref = f"{path.name} [{SHEET_MAIN}]"
    caveat = parsed["readme"]
    conn = psycopg2.connect(db_url)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            source_id = resolve_source(cur, SOURCE_NAME)
            log.info("Source id=%s (%r)", source_id, SOURCE_NAME)

            gas_ids: Dict[str, int] = {}
            created = 0
            for name in parsed["cargoes"]:
                gas_id, is_new = upsert_gas(cur, source_id, name)
                gas_ids[name] = gas_id
                created += is_new
            log.info("cargo_gas: %d (%d created), no properties written - this "
                     "source states none", len(gas_ids), created)

            # This source's own rows only, never a TRUNCATE: cleaning_process is
            # shared with the chemical and oil branches. Steps go with them,
            # cleaning_process_step being ON DELETE CASCADE.
            cur.execute("DELETE FROM cleaning_process WHERE source_id = %s "
                        "AND cargo_type = 'GAS'", (source_id,))
            log.info("removed %d cleaning_process row(s) from a previous run of "
                     "this source", cur.rowcount)

            processes = steps_written = 0
            for r in parsed["rows"]:
                notes = [f"Change of grade: tank last held {r['from_printed']}, "
                         f"next cargo {r['to_printed']}.",
                         f"The guideline's O2 requirement for the next cargo "
                         f"({r['to_name']}) is {r['o2']}, held in `condition`. "
                         f"It belongs to the destination cargo rather than to "
                         f"this transition, and is the same for every pair "
                         f"loading {r['to_name']}."]
                if r["prohibited"]:
                    notes.append("THE GUIDELINE DOES NOT PERMIT THIS CHANGE OF "
                                 "GRADE. The row exists because a rule that a "
                                 "transition may not be made is still a rule; "
                                 "its single step records the prohibition.")
                if caveat:
                    notes.append(f"Source caveat: {caveat}")

                cur.execute(
                    """
                    INSERT INTO cleaning_process
                        (cargo_type, from_cargo_id, to_cargo_id, source_id,
                         procedure_code, title, condition, remarks,
                         source_page_ref, notes, created_at, updated_at)
                    VALUES ('GAS', %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            now(), now())
                    RETURNING id
                    """,
                    (gas_ids[r["from_name"]], gas_ids[r["to_name"]], source_id,
                     r["code"], f"{r['from_name']} -> {r['to_name']}", r["o2"],
                     r["detailed"], page_ref, " ".join(notes)),
                )
                process_id = cur.fetchone()[0]
                processes += 1

                if r["prohibited"]:
                    cur.execute(
                        """
                        INSERT INTO cleaning_process_step
                            (cleaning_process_id, step_order, step_type, method,
                             description, mandatory, created_at, updated_at)
                        VALUES (%s, 1, 'RESTRICTION', %s, %s, TRUE, now(), now())
                        """,
                        (process_id, NOT_PERMITTED,
                         r["detailed"] or NOT_PERMITTED_SENTENCE + "."),
                    )
                    steps_written += 1
                    continue

                for order, (key, limit) in enumerate(r["parts"], start=1):
                    spec = KEYS[key]
                    remarks = None
                    if limit:
                        remarks = (f"Continue until the specified value is below "
                                   f"{limit}. {THRESHOLD_NOTE}")
                    cur.execute(
                        """
                        INSERT INTO cleaning_process_step
                            (cleaning_process_id, step_order, step_type, method,
                             medium, description, remarks, mandatory,
                             created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, now(), now())
                        """,
                        (process_id, order, spec["step_type"], spec["method"],
                         spec["medium"],
                         parsed["keys"].get(key) or spec["sentence"] + ".",
                         remarks),
                    )
                    steps_written += 1

        conn.commit()
        log.info("✓ Committed. cargo_gas: %d | cleaning_process: %d | "
                 "cleaning_process_step: %d", len(gas_ids), processes,
                 steps_written)
        return 0
    except Exception:
        conn.rollback()
        log.exception("Import failed - rolled back, nothing written.")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
