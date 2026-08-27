#!/usr/bin/env python3
"""
Load the Dr Verwey oil cleaning/compatibility matrix into cleaning_process AND
crude_oil_compatibility.

SOURCE
------
"Dr Verweys Tank Cleaning Guide - Oil" (source.json, category 'oil'). Runs LAST
of the three: cargo names resolve against verwey_oil_cargo.py's crude_oil rows,
procedure codes against verwey_oil_procedure_templates.py's templates.

INPUT
-----
"... Oil - cleaning and compatibility.xlsx": 64 rows = a complete 8 x 8 grid.

    id | cargo_oil_name_from | cargo_oil_name_to | procedure_code | Compattibbillity

ONE SHEET, TWO FACTS, TWO TABLES
--------------------------------
Each row states both "which procedure applies" and "may this be loaded at all",
and those live in different places:

    procedure_code   -> cleaning_process     (+ procedure_template_id)
    Compattibbillity -> crude_oil_compatibility.compatible

crude_oil_compatibility exists because neither `compatibility` (FKs
reactive_groups) nor `compatibility_exception` (FKs cargo_chemical) can
reference crude_oil. It is DIRECTIONAL: 13 of the 28 unordered pairs here
disagree by direction - Crude -> Gasoil is No while Gasoil -> Crude is Yes -
so a canonical-ordered pair would keep only one of the two answers.

The verdict tracks the code exactly in this extract (DD and OO are the only
codes marked No, on all 17 of their pairs), but it is stored as published rather
than derived from the code: they are two statements by the source, and a future
edition may separate them.

THE DIAGONAL
------------
The 8 same-cargo pairs carry no procedure_code and Compattibbillity = Yes -
loading a cargo onto itself needs no cleaning. They still get a
crude_oil_compatibility row, because "compatible with itself" is a real answer,
and a cleaning_process row with procedure_code NULL, because the absence of a
required procedure is itself the rule.

CASE FOLDING
------------
One row spells the code `j` where the other 12 spell it `J`. Folded to `J` on
instruction; the fold is logged and recorded in the row's notes, so it is never
a silent correction.

`U` is used by one pair but defined by no template. The pair loads with
procedure_code kept verbatim and procedure_template_id NULL, and is reported -
define U and re-run to link it.

IDEMPOTENCY
-----------
cleaning_process upserts on cleaning_process_pair_key (from_cargo_id,
to_cargo_id, source_id, COALESCE(condition,'')); crude_oil_compatibility on
(from_crude_oil_id, to_crude_oil_id, source_id). One transaction.

Usage:
    python3 etl/oil/verwey_oil_matrix.py
    python3 etl/oil/verwey_oil_matrix.py --dry-run
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
from psycopg2.extras import execute_values
from dotenv import load_dotenv

_ETL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ETL_ROOT / "common"))

from _paths import input_file  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("verwey_oil_matrix")

SOURCE_NAME = "Dr Verweys Tank Cleaning Guide - Oil"
CARGO_TYPE = "OIL"
DEFAULT_FILE = input_file("Dr Verweys Tank Cleaning Guide Oil - cleaning and compatibility.xlsx")
BATCH = 5000

COL_FROM = "cargo_oil_name_from"
COL_TO = "cargo_oil_name_to"
COL_CODE = "procedure_code"
COL_COMPAT = "Compattibbillity"   # the sheet's spelling, kept as the key it is
COLS = [COL_FROM, COL_TO, COL_CODE, COL_COMPAT]

# Codes whose only defect is capitalisation. Folded on instruction, never
# guessed: a code not listed here and not defined stays verbatim and unlinked.
CASE_FOLD = {"j": "J"}


def clean(value) -> Optional[str]:
    if value is None:
        return None
    s = re.sub(r"[ \t]+", " ", str(value).replace("\n", " ")).strip()
    return s or None


def parse_compatible(value) -> Optional[bool]:
    s = clean(value)
    if s is None:
        return None
    low = s.lower()
    if low in {"yes", "y", "true", "1", "1.0", "compatible"}:
        return True
    if low in {"no", "n", "false", "0", "0.0", "incompatible"}:
        return False
    return None


def resolve_source(cur, name: str) -> int:
    cur.execute("SELECT id FROM source WHERE name = %s", (name,))
    row = cur.fetchone()
    if row is None:
        sys.exit(f"Error: source {name!r} not found.\n"
                 f"  Register it with: python3 etl/common/source.py")
    return row[0]


def read_matrix(path: Path) -> Tuple[List[dict], List[str], List[str]]:
    ws = openpyxl.load_workbook(path, data_only=True).worksheets[0]
    grid = [list(r) for r in ws.iter_rows(values_only=True)]
    errors: List[str] = []
    folded: List[str] = []
    if not grid:
        return [], [f"{path.name} is empty"], []

    header = [clean(c) for c in grid[0]]
    missing = [c for c in COLS if c not in header]
    if missing:
        sys.exit(f"Error: {path.name} is missing column(s): {', '.join(missing)}")

    rows: List[dict] = []
    pairs: set = set()
    for i, raw in enumerate(grid[1:], start=2):
        r = dict(zip(header, raw))
        frm, to = clean(r[COL_FROM]), clean(r[COL_TO])
        if frm is None and to is None:
            continue
        if frm is None or to is None:
            errors.append(f"row {i}: pair is missing a cargo name")
            continue
        if (frm, to) in pairs:
            errors.append(f"row {i}: duplicate pair {frm!r} -> {to!r}")
            continue
        pairs.add((frm, to))

        code = clean(r[COL_CODE])
        note = None
        if code in CASE_FOLD:
            folded.append(f"row {i}: {frm!r} -> {to!r} spelled {code!r}")
            note = (f"The extract spells this code {code!r}; stored as "
                    f"{CASE_FOLD[code]!r}, which is how every other row spells it.")
            code = CASE_FOLD[code]

        compatible = parse_compatible(r[COL_COMPAT])
        if compatible is None:
            errors.append(f"row {i}: {frm!r} -> {to!r} has {COL_COMPAT} "
                          f"{r[COL_COMPAT]!r}, which is neither Yes nor No")
            continue

        rows.append({"from_name": frm, "to_name": to, "procedure_code": code,
                     "compatible": compatible, "notes": note, "line": i})
    return rows, errors, folded


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", default=str(DEFAULT_FILE))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.is_file():
        sys.exit(f"Error: file not found: {path}")

    rows, errors, folded = read_matrix(path)
    if errors:
        log.error("%d validation error(s) - nothing was imported:", len(errors))
        for e in errors[:20]:
            log.error("    %s", e)
        return 1

    froms = {r["from_name"] for r in rows}
    tos = {r["to_name"] for r in rows}
    log.info("%s: %d pair(s), %d x %d grid", path.name, len(rows), len(froms), len(tos))
    if len(rows) != len(froms) * len(tos):
        log.warning("%d combination(s) the sheet does not state",
                    len(froms) * len(tos) - len(rows))
    for f in folded:
        log.warning("case folded: %s", f)
    log.info("compatible: %d yes, %d no", sum(1 for r in rows if r["compatible"]),
             sum(1 for r in rows if not r["compatible"]))
    log.info("pairs with no procedure_code (same-cargo diagonal): %d",
             sum(1 for r in rows if r["procedure_code"] is None))

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

            cur.execute("SELECT oil_name, id FROM crude_oil WHERE source_id = %s", (source_id,))
            oil_ids = dict(cur.fetchall())
            if not oil_ids:
                sys.exit("Error: this source has no crude_oil rows.\n"
                         "  Run etl/oil/verwey_oil_cargo.py first.")

            cur.execute("SELECT procedure_code FROM procedure_templates WHERE source_id = %s",
                        (source_id,))
            known = {r[0] for r in cur.fetchall()}
            if not known:
                sys.exit("Error: this source defines no procedure_templates.\n"
                         "  Run etl/oil/verwey_oil_procedure_templates.py first.")

            unknown_cargo: Dict[str, int] = {}
            unknown_code: Dict[str, int] = {}
            cp_rows: List[tuple] = []
            compat_rows: List[tuple] = []

            for r in rows:
                fid, tid = oil_ids.get(r["from_name"]), oil_ids.get(r["to_name"])
                for name, oid in ((r["from_name"], fid), (r["to_name"], tid)):
                    if oid is None:
                        unknown_cargo[name] = unknown_cargo.get(name, 0) + 1
                if fid is None or tid is None:
                    continue
                code = r["procedure_code"]
                if code is not None and code not in known:
                    unknown_code[code] = unknown_code.get(code, 0) + 1

                cp_rows.append((CARGO_TYPE, fid, tid, code, source_id, r["notes"]))
                compat_rows.append((fid, tid, r["compatible"], source_id, code, r["notes"]))

            for name, n in sorted(unknown_cargo.items()):
                log.error("cargo %r has no crude_oil row under this source (%d pair(s))", name, n)
            for code, n in sorted(unknown_code.items()):
                log.warning("code %r is used by %d pair(s) but this source defines no "
                            "template - procedure_code kept verbatim, row left unlinked. "
                            "Define it and re-run to link.", code, n)
            if unknown_cargo:
                log.error("Refusing to import: the extracts disagree with each other.")
                conn.rollback()
                return 1

            if args.dry_run:
                log.info("--dry-run: %d cleaning_process + %d crude_oil_compatibility "
                         "row(s) prepared, nothing written.", len(cp_rows), len(compat_rows))
                conn.rollback()
                return 0

            for start in range(0, len(cp_rows), BATCH):
                execute_values(
                    cur,
                    """
                    INSERT INTO cleaning_process
                        (cargo_type, from_cargo_id, to_cargo_id, procedure_code,
                         source_id, notes, created_at, updated_at)
                    VALUES %s
                    ON CONFLICT (from_cargo_id, to_cargo_id, source_id,
                                 COALESCE(condition, ''))
                        WHERE from_cargo_id IS NOT NULL AND to_cargo_id IS NOT NULL
                    DO UPDATE SET cargo_type     = EXCLUDED.cargo_type,
                                  procedure_code = EXCLUDED.procedure_code,
                                  notes          = EXCLUDED.notes,
                                  updated_at     = now()
                    """,
                    cp_rows[start:start + BATCH],
                    template="(%s::\"CargoType\",%s,%s,%s,%s,%s,now(),now())",
                    page_size=BATCH,
                )

            for start in range(0, len(compat_rows), BATCH):
                execute_values(
                    cur,
                    """
                    INSERT INTO crude_oil_compatibility
                        (from_crude_oil_id, to_crude_oil_id, compatible, source_id,
                         procedure_code, notes, created_at, updated_at)
                    VALUES %s
                    ON CONFLICT (from_crude_oil_id, to_crude_oil_id, source_id)
                    DO UPDATE SET compatible     = EXCLUDED.compatible,
                                  procedure_code = EXCLUDED.procedure_code,
                                  notes          = EXCLUDED.notes,
                                  updated_at     = now()
                    """,
                    compat_rows[start:start + BATCH],
                    template="(%s,%s,%s,%s,%s,%s,now(),now())",
                    page_size=BATCH,
                )

            cur.execute(
                """
                UPDATE cleaning_process cp
                   SET procedure_template_id = pt.id, updated_at = now()
                  FROM procedure_templates pt
                 WHERE pt.source_id = cp.source_id
                   AND pt.procedure_code = cp.procedure_code
                   AND cp.source_id = %s
                   AND cp.cargo_type = %s::"CargoType"
                   AND cp.procedure_code IS NOT NULL
                   AND cp.procedure_template_id IS DISTINCT FROM pt.id
                """,
                (source_id, CARGO_TYPE),
            )
            linked = cur.rowcount

            cur.execute("""SELECT count(*), count(procedure_template_id)
                             FROM cleaning_process
                            WHERE source_id = %s AND cargo_type = %s::"CargoType" """,
                        (source_id, CARGO_TYPE))
            cp_total, cp_linked = cur.fetchone()
            cur.execute("""SELECT count(*), count(*) FILTER (WHERE compatible)
                             FROM crude_oil_compatibility WHERE source_id = %s""",
                        (source_id,))
            c_total, c_yes = cur.fetchone()

        conn.commit()
        log.info("✓ Committed. cleaning_process: %d row(s), %d linked this run, "
                 "%d of %d carry a template | crude_oil_compatibility: %d row(s) "
                 "(%d compatible, %d not)",
                 cp_total, linked, cp_linked, cp_total, c_total, c_yes, c_total - c_yes)
        return 0
    except Exception:
        conn.rollback()
        log.exception("Import failed - rolled back, nothing written.")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
