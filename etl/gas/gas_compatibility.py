#!/usr/bin/env python3
"""
Load the liquefied-gas compatibility workbook into cargo_gas,
cargo_gas_compatibility, cleaning_process and cleaning_process_step.

SOURCE
------
"Chemical Compatibilities of Liquefied Gases" (source.json, category 'gas').
One document holding two DIFFERENT kinds of statement, which is why one source
writes to two unrelated pairs of tables:

  sheet 1  CHEMICAL COMPATIBILITIES OF LIQ   may these two share a tank?
           -> cargo_gas_compatibility        136 unordered pairs

  sheet 2  PREV. CARGO COMP. OF LIQUIFIED    what must be done between them?
           -> cleaning_process (+ steps)     120 directed transitions

They are not the same question and must not be collapsed. "Ammonia and ethylene
oxide react" is a fact about a pair; "wash, inspect and purge before loading
butadiene" is a procedure with an order. The first has no direction, the second
does.

A third sheet, 'astm table', is deliberately NOT loaded - see the end of this
docstring.

SHEET 1: A SYMMETRIC MATRIX, STORED ONCE PER PAIR
---------------------------------------------------
17 x 17, every cell 'Y' or 'X'. The symmetry is verified rather than assumed:
all 289 cells must equal their transpose and the whole diagonal must read 'Y',
or the load stops. On that evidence each unordered pair is written ONCE, in
canonical order gas_a_id <= gas_b_id - the arrangement `compatibility` uses for
the reactive-group chart, and one the database enforces with a CHECK constraint
(see 20260910000000_cargo_gas_compatibility).

The 17 diagonal cells are not stored. "Butane is compatible with butane" is a
consequence of the matrix being about pairs of different things, not a finding;
storing it would put 17 rows in the table that no reader would ever ask for.
136 off-diagonal pairs remain, of which 21 are incompatible.

THREE OF THE 17 ARE NOT CARGOES
-------------------------------
Water vapour, Oxygen or Air, and Carbon dioxide are tank ATMOSPHERES and
contaminants, not things a ship loads. They are kept, because they carry a
large share of what the matrix is for - butadiene with oxygen, ammonia with
carbon dioxide, chlorine with water vapour are all incompatible, and dropping
them would delete 21 of the 42 'X' cells. Each gets a cargo_gas row like any
other name in the source, and every compatibility row involving one records in
its notes that the counterparty is an atmosphere rather than a cargo.

SHEET 2: THE SAME SHAPE AS THE DOW MATRIX, A DIFFERENT GUIDE
--------------------------------------------------------------
13 last cargoes x 11 next cargoes, 120 coded cells. Same tables and same
treatment as etl/gas/dow_lpg_change_of_grade.py, under a different source_id so
the two guides' answers stay separable.

Its legend defines six keys - W, V, N2, N2I, ET, S - and the cells spell them
in ways that need care:

  * SEPARATORS DIFFER FOR ONE INSTRUCTION. "V.N2" (33 cells) and "V,N2" (6) are
    the same thing written with a dot and with a comma.

  * A COMMA CAN FALL INSIDE A KEY. "W,V,N2,I" is W + V + N2I, not W + V + N2 +
    a key called "I". Splitting on commas alone invents a seventh key and drops
    the distinction between nitrogen-only and nitrogen-or-inert-gas - which is
    the distinction the guide draws most often. Keys are therefore matched
    longest-first against the remaining text, so "N2,I" and "N2I" both resolve
    to N2I.

  * "Heat" IS NOT IN THE LEGEND. "S Heat" and "ET Heat" appear 3 times and the
    legend defines no such key. A footnote says the tank bottom should be
    heated to about 0 C before inerting, which is probably what it means, but
    the guide does not say so. It is kept verbatim as a modifier on the step
    and flagged in `notes` as undefined, never expanded into an instruction
    this loader invented.

TWO REQUIREMENTS PER DESTINATION, ONE `condition` COLUMN
----------------------------------------------------------
The sheet heads each next-cargo column with BOTH an O2 content and a dew point
(<-50 C for ethylene, <-10 C for butane). Both are properties of the cargo
about to be loaded, both must be met before loading, and cleaning_process has
one `condition` column - so both are written into it, joined and labelled:

    "O2 <0.3%; Dew-point <-50°C"

rather than picking one and losing the other, or splitting the row in two and
asserting two transitions where the guide states one.

AMMONIA STATES NO PROCEDURE
---------------------------
The ammonia row carries no codes at all, only the sentence "Loading cargoes
after ammonia is often subject to specific terminal requirements". That is a
real statement about 11 transitions, so 11 rows are written carrying it as
`remarks` and NO steps: an empty step list here means the guide declines to
prescribe one, which is why the sentence is on the row to say so. Inventing
steps from a sentence that says the terminal decides them would be the worst
possible reading of it.

WHAT IS NOT LOADED
------------------
'astm table' holds ASTM weight-in-vacuo / weight-in-air conversion factors by
density band, plus two prose passages on LNG quantification. It is reference
data keyed by a density RANGE and not by any cargo, so nothing in this schema
can hold it without inventing a table for it, and it is left out rather than
forced into a cargo-shaped row. Two rows of that sheet are document prose, not
data at all.

IDEMPOTENCY
-----------
cargo_gas upserts on (gas_name, source_id). cargo_gas_compatibility upserts on
the pair/source unique index. This source's cleaning_process rows are deleted
and rewritten, cascading to their steps - the pattern run_gas.sh calls for,
because cleaning_process is shared with the chemical and oil branches and must
never be truncated. Everything is validated before anything is written; one
transaction.

Usage:
    python3 etl/gas/gas_compatibility.py
    python3 etl/gas/gas_compatibility.py --dry-run
"""

import argparse
import logging
import os
import re
import sys
from collections import Counter
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
log = logging.getLogger("gas_compatibility")

SOURCE_NAME = "Chemical Compatibilities of Liquefied Gases"
DEFAULT_FILE = input_file("chemical Compatiblities.pdf.xlsx")

SHEET_COMPAT = "CHEMICAL COMPATIBILITIES OF LIQ"
SHEET_GRADE = "PREV. CARGO COMP. OF LIQUIFIED "
SHEET_ASTM = "astm table"

COMPATIBLE_MARK = "Y"
INCOMPATIBLE_MARK = "X"

# Names in the compatibility matrix that are not cargoes. They stay - most of
# the matrix's incompatibilities involve one - but every row that mentions one
# says so, because "compatible with oxygen" is a statement about a tank
# atmosphere and "compatible with butane" is one about a cargo.
NOT_CARGOES = {
    "Watervapour": "water vapour in the tank atmosphere",
    "OxygenorAir": "oxygen, i.e. an un-inerted tank atmosphere",
    "Carbondioxide": "carbon dioxide, used as an inerting medium",
}
NOT_CARGO_NOTE = (
    "{name} is not a cargo: the matrix uses it for {what}. The verdict is about "
    "whether the cargo may be exposed to it, not about carrying the two "
    "together.")

# ---------------------------------------------------------------------------
# Sheet 2: the change-of-grade keys, from the workbook's own legend column
# ---------------------------------------------------------------------------
KEYS = {
    "W":   {"step_type": "CLEANING",     "method": "Water wash",  "medium": "Water"},
    "V":   {"step_type": "INSPECTION",   "method": "Visual inspection", "medium": None},
    "N2I": {"step_type": "PURGING",      "method": "Inert",
            "medium": "Nitrogen or inert gas"},
    "N2":  {"step_type": "PURGING",      "method": "Inert",       "medium": "Nitrogen"},
    "ET":  {"step_type": "PRECONDITION", "method": "Empty tank",  "medium": None},
    "S":   {"step_type": "CONDITION",    "method": "Standard requirements",
            "medium": None},
}
# Longest first, so "N2I" is never read as "N2" with a stray "I" left over.
KEY_ORDER = sorted(KEYS, key=len, reverse=True)

# A modifier the cells use and the legend never defines.
HEAT = "Heat"
HEAT_NOTE = (
    "The cell qualifies this step with 'Heat', which the guide's own legend "
    "does NOT define. A footnote on the sheet says the tank bottom should be "
    "heated to about 0°C before inerting starts, which is probably what is "
    "meant, but the guide does not say so. Kept exactly as printed and not "
    "expanded into an instruction this loader invented.")

AMMONIA_NO_STEPS_NOTE = (
    "The guide prescribes NO procedure for this transition. Its ammonia row "
    "carries only the sentence in `remarks`, so the empty step list is the "
    "guide declining to specify one rather than a parse failure. Do not read "
    "the absence of steps as 'nothing to do'.")


def cell(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return re.sub(r"\s+", " ", str(value).replace("\n", " ")).strip()


def grid_of(path: Path, sheet: str) -> List[List[str]]:
    return [[cell(v) for v in row] for row in
            pd.read_excel(path, sheet_name=sheet, header=None).values.tolist()]


def at(row: List[str], i: int) -> str:
    return row[i] if i < len(row) else ""


# ---------------------------------------------------------------------------
# Sheet 1
# ---------------------------------------------------------------------------
def read_compatibility(path: Path, errors: List[str]) -> dict:
    grid = grid_of(path, SHEET_COMPAT)
    if not grid:
        errors.append(f"{SHEET_COMPAT!r} is empty")
        return {}

    names = [c for c in grid[0][1:] if c]
    rows = [at(r, 0) for r in grid[1:1 + len(names)]]
    if rows != names:
        errors.append(f"{SHEET_COMPAT!r} is not square: its columns are "
                      f"{names!r} but its rows are {rows!r}. A compatibility "
                      f"matrix whose axes differ cannot be read as pairs")
        return {}

    matrix: Dict[Tuple[str, str], str] = {}
    for i, row_name in enumerate(names):
        row = grid[1 + i]
        for j, col_name in enumerate(names):
            value = at(row, 1 + j)
            if value not in (COMPATIBLE_MARK, INCOMPATIBLE_MARK):
                errors.append(
                    f"{SHEET_COMPAT!r}: {row_name!r} x {col_name!r} reads "
                    f"{value!r}, which is neither {COMPATIBLE_MARK!r} nor "
                    f"{INCOMPATIBLE_MARK!r}. The legend defines no third mark")
                continue
            matrix[(row_name, col_name)] = value
    if errors:
        return {}

    # Symmetry and the diagonal are the evidence for storing a pair once. If
    # either fails the matrix is directional or damaged, and collapsing it would
    # silently discard one of two different answers.
    asymmetric = [(a, b, matrix[(a, b)], matrix[(b, a)])
                  for a in names for b in names if matrix[(a, b)] != matrix[(b, a)]]
    if asymmetric:
        errors.append(
            f"{SHEET_COMPAT!r} is NOT symmetric - {len(asymmetric)} cell(s) "
            f"disagree with their transpose, e.g. {asymmetric[0]}. This loader "
            f"stores a pair once and would have to discard one of the two "
            f"answers; the matrix needs a directional table instead")
    self_incompatible = [a for a in names if matrix[(a, a)] != COMPATIBLE_MARK]
    if self_incompatible:
        errors.append(f"{SHEET_COMPAT!r} marks {self_incompatible!r} "
                      f"incompatible with itself, which the rest of the matrix "
                      f"gives no way to interpret")
    if errors:
        return {}

    pairs = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:              # off-diagonal, each pair once
            mark = matrix[(a, b)]
            pairs.append({"a": a, "b": b, "mark": mark,
                          "compatible": mark == COMPATIBLE_MARK})
    return {"names": names, "pairs": pairs}


# ---------------------------------------------------------------------------
# Sheet 2
# ---------------------------------------------------------------------------
def split_code(code: str, errors: List[str],
               where: str) -> Optional[Tuple[List[str], Optional[str], bool]]:
    """A change-of-grade code as (keys, threshold, heat).

    Handles the three spellings the sheet mixes: '.' and ',' as separators, and
    a comma falling INSIDE a key ("W,V,N2,I" is W + V + N2I).
    """
    text = code.strip()
    heat = False
    if text.lower().endswith(HEAT.lower()):
        heat = True
        text = text[:-len(HEAT)].strip()

    threshold = None
    m = re.search(r"<\s*([^<]+)$", text)
    if m:
        threshold = m.group(1).strip()
        text = text[:m.start()].strip()

    # Keys are read left to right, longest match first, with separators
    # discarded. That is what makes "N2,I" and "N2I" the same key.
    keys: List[str] = []
    rest = text
    while rest:
        rest = rest.lstrip(" .,")
        if not rest:
            break
        for key in KEY_ORDER:
            candidate = rest[:len(key)]
            if candidate.upper() == key.upper():
                keys.append(key)
                rest = rest[len(key):]
                break
        else:
            errors.append(f"{where}: {code!r} contains {rest!r}, which starts "
                          f"with no key the legend defines "
                          f"({', '.join(sorted(KEYS))})")
            return None
        # "N2" immediately followed by ",I" or "I" is the N2I key.
        if keys and keys[-1] == "N2":
            tail = rest.lstrip(" .,")
            if tail[:1].upper() == "I" and not tail[1:2].isalnum():
                keys[-1] = "N2I"
                rest = tail[1:]
    if not keys:
        errors.append(f"{where}: {code!r} names no key at all")
        return None
    return keys, threshold, heat


def read_change_of_grade(path: Path, errors: List[str]) -> dict:
    grid = grid_of(path, SHEET_GRADE)
    if len(grid) < 6:
        errors.append(f"{SHEET_GRADE!r} is too short to hold a matrix")
        return {}

    header = grid[1]
    if at(header, 0) != "LAST CARGO":
        errors.append(f"{SHEET_GRADE!r} row 2 col 1 reads {at(header, 0)!r}, "
                      f"expected 'LAST CARGO'")
        return {}
    # The next-cargo columns run until the blank that precedes the legend.
    next_cargoes: List[Tuple[int, str]] = []
    for i in range(1, len(header)):
        name = at(header, i)
        if not name:
            break
        next_cargoes.append((i, name))

    o2_row, dew_row = grid[2], grid[3]
    if at(o2_row, 0) != "O2 Content" or at(dew_row, 0) != "Dew-point":
        errors.append(f"{SHEET_GRADE!r} rows 3 and 4 are headed "
                      f"{at(o2_row, 0)!r} / {at(dew_row, 0)!r}, expected "
                      f"'O2 Content' / 'Dew-point'")
        return {}
    limits: Dict[str, str] = {}
    for i, name in next_cargoes:
        o2, dew = at(o2_row, i), at(dew_row, i)
        if not o2 or not dew:
            errors.append(f"{SHEET_GRADE!r}: {name!r} is missing its "
                          f"{'O2 content' if not o2 else 'dew point'}")
            continue
        limits[name] = f"O2 {o2}; Dew-point {dew}"

    transitions: List[dict] = []
    last_cargoes: List[str] = []
    for r in range(4, len(grid)):
        row = grid[r]
        last = at(row, 0)
        if not last:
            continue
        if last.lower().startswith(("note:", "these cargoes")):
            break
        last_cargoes.append(last)

        # A row whose only entry is prose states no code for any destination.
        coded = [(i, at(row, i)) for i, _ in next_cargoes if at(row, i)]
        prose = [v for _, v in coded if len(v) > 40]
        if prose:
            if len(coded) != 1:
                errors.append(f"{SHEET_GRADE!r} row {r + 1} ({last!r}) mixes a "
                              f"sentence with codes: {coded!r}")
                continue
            for i, name in next_cargoes:
                if name == last:
                    continue
                transitions.append({"row": r + 1, "from": last, "to": name,
                                    "code": None, "keys": [], "threshold": None,
                                    "heat": False, "remarks": prose[0],
                                    "condition": limits.get(name)})
            continue

        for i, name in next_cargoes:
            value = at(row, i)
            if not value:
                continue                       # diagonal, or the guide is silent
            where = f"{SHEET_GRADE!r} row {r + 1} ({last!r} -> {name!r})"
            split = split_code(value, errors, where)
            if split is None:
                continue
            keys, threshold, heat = split
            transitions.append({"row": r + 1, "from": last, "to": name,
                                "code": value, "keys": keys,
                                "threshold": threshold, "heat": heat,
                                "remarks": None, "condition": limits.get(name)})

    missing = [t for t in transitions if not t["condition"]]
    for t in missing:
        errors.append(f"{SHEET_GRADE!r} row {t['row']}: {t['to']!r} has no O2 / "
                      f"dew-point entry, so the transition has no acceptance "
                      f"criterion")
    if not transitions:
        errors.append(f"{SHEET_GRADE!r} yielded no transitions")
    return {"transitions": transitions, "limits": limits,
            "next_cargoes": [n for _, n in next_cargoes],
            "last_cargoes": last_cargoes}


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


def report(compat: dict, grade: dict) -> None:
    pairs = compat["pairs"]
    bad = [p for p in pairs if not p["compatible"]]
    log.info("sheet 1 - compatibility matrix: %d name(s), %d unordered pair(s) "
             "(diagonal not stored), %d incompatible",
             len(compat["names"]), len(pairs), len(bad))
    log.info("    verified symmetric across all %d cells, diagonal all %r",
             len(compat["names"]) ** 2, COMPATIBLE_MARK)
    atmospheres = sorted(set(compat["names"]) & set(NOT_CARGOES))
    log.info("    name(s) that are NOT cargoes, kept and flagged: %s",
             ", ".join(atmospheres) or "none")
    log.info("    incompatible pairs:")
    for p in bad:
        log.info("        %-16s x %s", p["a"], p["b"])

    t = grade["transitions"]
    coded = [x for x in t if x["code"]]
    prose = [x for x in t if not x["code"]]
    log.info("sheet 2 - change of grade: %d transition(s) over %d last x %d next "
             "cargo(es)", len(t), len(grade["last_cargoes"]),
             len(grade["next_cargoes"]))
    codes = Counter(x["code"] for x in coded)
    log.info("    %d distinct code(s):", len(codes))
    for code, n in sorted(codes.items()):
        x = next(y for y in coded if y["code"] == code)
        shown = " -> ".join(x["keys"])
        if x["threshold"]:
            shown += f" (until <{x['threshold']})"
        if x["heat"]:
            shown += " +Heat"
        log.info("        %-14s x%-3d %s", code, n, shown)
    heat = [x for x in coded if x["heat"]]
    if heat:
        log.warning("    %d cell(s) carry the undefined 'Heat' modifier: %s",
                    len(heat), ", ".join(f"{x['from']}->{x['to']}" for x in heat))
    if prose:
        log.info("    %d transition(s) the guide gives no procedure for (prose "
                 "only, no steps written): %s", len(prose),
                 ", ".join(sorted({x["from"] for x in prose})))
    log.info("    acceptance criteria written to cleaning_process.condition:")
    for name, limit in grade["limits"].items():
        log.info("        %-24s %s", name, limit)
    log.info("    cleaning_process rows: %d | cleaning_process_step rows: %d",
             len(t), sum(len(x["keys"]) for x in t))
    log.info("sheet %r is NOT loaded: ASTM conversion factors keyed by density "
             "band, which is not cargo data", SHEET_ASTM)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", default=str(DEFAULT_FILE))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.is_file():
        sys.exit(f"Error: file not found: {path}")

    errors: List[str] = []
    compat = read_compatibility(path, errors)
    grade = read_change_of_grade(path, errors) if not errors else {}
    if errors:
        log.error("%d validation error(s) - nothing was imported:", len(errors))
        for e in errors:
            log.error("    %s", e)
        return 1

    report(compat, grade)
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
            source_id = resolve_source(cur, SOURCE_NAME)
            log.info("Source id=%s (%r)", source_id, SOURCE_NAME)

            names = sorted(set(compat["names"])
                           | {t["from"] for t in grade["transitions"]}
                           | {t["to"] for t in grade["transitions"]})
            gas_ids: Dict[str, int] = {}
            created = 0
            for name in names:
                gas_id, is_new = upsert_gas(cur, source_id, name)
                gas_ids[name] = gas_id
                created += is_new
            log.info("cargo_gas: %d (%d created)", len(gas_ids), created)

            # --- sheet 1 -------------------------------------------------
            written = 0
            for p in compat["pairs"]:
                a_id, b_id = gas_ids[p["a"]], gas_ids[p["b"]]
                a_name, b_name = p["a"], p["b"]
                if a_id > b_id:                       # canonical order
                    a_id, b_id = b_id, a_id
                    a_name, b_name = b_name, a_name
                notes = [f"The matrix marks {p['a']} x {p['b']} as "
                         f"{p['mark']!r}."]
                for name in (p["a"], p["b"]):
                    if name in NOT_CARGOES:
                        notes.append(NOT_CARGO_NOTE.format(
                            name=name, what=NOT_CARGOES[name]))
                notes.append("The matrix is symmetric and this pair is stored "
                             "once, in canonical order; there is no row for the "
                             "reverse pair.")
                cur.execute(
                    """
                    INSERT INTO cargo_gas_compatibility
                        (gas_a_id, gas_b_id, compatible, source_id, raw_value,
                         source_page_ref, notes, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, now(), now())
                    ON CONFLICT (gas_a_id, gas_b_id, source_id) DO UPDATE SET
                        compatible      = EXCLUDED.compatible,
                        raw_value       = EXCLUDED.raw_value,
                        source_page_ref = EXCLUDED.source_page_ref,
                        notes           = EXCLUDED.notes,
                        updated_at      = now()
                    """,
                    (a_id, b_id, p["compatible"], source_id, p["mark"],
                     f"{path.name} [{SHEET_COMPAT}]", " ".join(notes)),
                )
                written += 1
            log.info("cargo_gas_compatibility: %d pair(s)", written)

            # --- sheet 2 -------------------------------------------------
            cur.execute("DELETE FROM cleaning_process WHERE source_id = %s "
                        "AND cargo_type = 'GAS'", (source_id,))
            log.info("removed %d cleaning_process row(s) from a previous run",
                     cur.rowcount)

            page_ref = f"{path.name} [{SHEET_GRADE}]"
            processes = steps = 0
            for t in grade["transitions"]:
                notes = [f"Change of grade: tank last held {t['from']}, next "
                         f"cargo {t['to']}.",
                         f"`condition` holds BOTH acceptance criteria the guide "
                         f"heads the {t['to']} column with; they belong to the "
                         f"destination cargo and are the same for every "
                         f"transition loading it."]
                if t["heat"]:
                    notes.append(HEAT_NOTE)
                if not t["code"]:
                    notes.append(AMMONIA_NO_STEPS_NOTE)
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
                    (gas_ids[t["from"]], gas_ids[t["to"]], source_id, t["code"],
                     f"{t['from']} -> {t['to']}", t["condition"], t["remarks"],
                     page_ref, " ".join(notes)),
                )
                process_id = cur.fetchone()[0]
                processes += 1

                for order, key in enumerate(t["keys"], start=1):
                    spec = KEYS[key]
                    remarks = []
                    if t["threshold"] and order == len(t["keys"]):
                        remarks.append(f"Continue until below {t['threshold']}, "
                                       f"as the cell prints it.")
                    if t["heat"]:
                        remarks.append(HEAT_NOTE)
                    cur.execute(
                        """
                        INSERT INTO cleaning_process_step
                            (cleaning_process_id, step_order, step_type, method,
                             medium, description, remarks, mandatory,
                             created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, now(), now())
                        """,
                        (process_id, order, spec["step_type"], spec["method"],
                         spec["medium"], f"{spec['method']} ({key}).",
                         " ".join(remarks) or None),
                    )
                    steps += 1

        conn.commit()
        log.info("✓ Committed. cargo_gas: %d | cargo_gas_compatibility: %d | "
                 "cleaning_process: %d | cleaning_process_step: %d",
                 len(gas_ids), written, processes, steps)
        return 0
    except Exception:
        conn.rollback()
        log.exception("Import failed - rolled back, nothing written.")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
