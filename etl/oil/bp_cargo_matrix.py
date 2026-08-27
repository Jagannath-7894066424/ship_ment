#!/usr/bin/env python3
"""
Load the BP Tank Cleaning Guide cargo-to-cargo matrix into cleaning_process
(cargo_type OIL).

SOURCE
------
"BP Tank Cleaning Guide" (source.json, category 'oil'). Runs LAST of the three
BP loaders: it resolves cargo names against the crude_oil rows bp_cargo.py
creates, and procedure codes against the templates bp_procedure_templates.py
defines.

INPUT
-----
"... - cargo to cargo matrix.xlsx": one row per pair, already flattened -

    id | from_cargo_id | to_cargo_id | previous_cargo | cargo_to_be_loaded |
    procedure_code | colour_name | source_symbol | loading_allowed |
    source_name | source_page_ref | notes

484 rows = a complete 22 x 22 grid, the 22 same-cargo pairs included (loading a
product back onto itself still calls for a code).

Mapping:

    previous_cargo     -> crude_oil.oil_name -> cleaning_process.from_cargo_id
    cargo_to_be_loaded -> crude_oil.oil_name -> cleaning_process.to_cargo_id
    procedure_code     -> cleaning_process.procedure_code, and
                          procedure_template_id resolved after insert

JOINING ON THE NAME, NOT THE ID
-------------------------------
from_cargo_id / to_cargo_id in the sheet are the cargo sheet's own 1..22
numbering, not database ids, so they are NOT stored. The join goes through the
name, which is the identity crude_oil actually keys on. The loader still CHECKS
the sheet's id against the name it is paired with, so a renumbered or misaligned
extract fails loudly instead of silently loading the wrong pair.

cargo_type is OIL on every row, so from_cargo_id / to_cargo_id hold crude_oil
ids. Those columns carry NO foreign key - see the cleaning_process docstring in
schema.prisma - so they are validated by the cleaning_process_cargo_refs trigger
on the way in, not by a constraint.

WHAT IS NOT STORED
------------------
  * colour_name / source_symbol. procedure_code already encodes both (GREY,
    CYAN_PM) and the template carries the legend; repeating them per pair would
    be the same fact 484 times.
  * loading_allowed. It is a property of the CODE, not of the pair - every BLACK
    row is false and every other row true - and procedure_templates.loading_allowed
    already holds it. The loader VERIFIES the sheet agrees with the template
    rather than trusting either blindly.

IDEMPOTENCY
-----------
Upsert on the partial unique index cleaning_process_pair_key (from_cargo_id,
to_cargo_id, source_id, COALESCE(condition,'')). One transaction.

Usage:
    python3 etl/oil/bp_cargo_matrix.py
    python3 etl/oil/bp_cargo_matrix.py --dry-run
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

# Loaders are run as scripts (python3 etl/<branch>/<file>.py), so only their
# own directory is on sys.path. Add the shared helpers.
_ETL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ETL_ROOT / "common"))

from _paths import input_file  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("bp_matrix")

SOURCE_NAME = "BP Tank Cleaning Guide"
CARGO_TYPE = "OIL"
DEFAULT_FILE = input_file("BP Tank Cleaning Guide - cargo to cargo matrix.xlsx")
BATCH = 5000

COLS = ["from_cargo_id", "to_cargo_id", "previous_cargo", "cargo_to_be_loaded",
        "procedure_code", "loading_allowed", "source_page_ref", "notes"]


def clean(value) -> Optional[str]:
    if value is None:
        return None
    s = re.sub(r"[ \t]+", " ", str(value).replace("\n", " ")).strip()
    return s or None


def to_int(value) -> Optional[int]:
    s = clean(value)
    if s is None:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def to_bool(value, default: Optional[bool] = None) -> Optional[bool]:
    s = clean(value)
    if s is None:
        return default
    return s.lower() in {"1", "1.0", "true", "yes", "y"}


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


def read_matrix(path: Path) -> Tuple[List[dict], List[str]]:
    ws = openpyxl.load_workbook(path, data_only=True).worksheets[0]
    grid = [list(r) for r in ws.iter_rows(values_only=True)]
    errors: List[str] = []
    if not grid:
        return [], [f"{path.name} is empty"]

    header = [clean(c) for c in grid[0]]
    missing = [c for c in COLS if c not in header]
    if missing:
        sys.exit(f"Error: {path.name} is missing column(s): {', '.join(missing)}")

    rows: List[dict] = []
    pairs: set = set()
    # The sheet's own id -> name, learned as we go. Checking it costs nothing
    # and catches a misaligned extract that would otherwise load silently.
    id_to_name: Dict[int, str] = {}

    for i, raw in enumerate(grid[1:], start=2):
        r = dict(zip(header, raw))
        frm, to = clean(r["previous_cargo"]), clean(r["cargo_to_be_loaded"])
        if frm is None and to is None:
            continue
        if frm is None or to is None:
            errors.append(f"row {i}: pair is missing a cargo name")
            continue

        code = clean(r["procedure_code"])
        if code is None:
            errors.append(f"row {i}: {frm!r} -> {to!r} has no procedure_code")
            continue

        for label, sheet_id, name in (("from_cargo_id", to_int(r["from_cargo_id"]), frm),
                                      ("to_cargo_id", to_int(r["to_cargo_id"]), to)):
            if sheet_id is None:
                continue
            known = id_to_name.setdefault(sheet_id, name)
            if known != name:
                errors.append(f"row {i}: {label}={sheet_id} is {name!r} here but "
                              f"{known!r} elsewhere - the extract's numbering and "
                              f"its names disagree")

        if (frm, to) in pairs:
            errors.append(f"row {i}: duplicate pair {frm!r} -> {to!r}")
            continue
        pairs.add((frm, to))

        page = to_int(r["source_page_ref"])
        rows.append({
            "from_name": frm,
            "to_name": to,
            "procedure_code": code,
            "loading_allowed": to_bool(r["loading_allowed"]),
            "source_page_ref": str(page) if page is not None else None,
            "notes": clean(r["notes"]),
            "line": i,
        })
    return rows, errors


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", default=str(DEFAULT_FILE))
    ap.add_argument("--dry-run", action="store_true", help="parse and report, write nothing")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.is_file():
        sys.exit(f"Error: file not found: {path}")

    rows, errors = read_matrix(path)
    if errors:
        log.error("%d validation error(s) - nothing was imported:", len(errors))
        for e in errors[:20]:
            log.error("    %s", e)
        return 1

    names = {r["from_name"] for r in rows} | {r["to_name"] for r in rows}
    log.info("%s: %d pair(s) over %d cargo name(s)", path.name, len(rows), len(names))
    expected = len({r["from_name"] for r in rows}) * len({r["to_name"] for r in rows})
    if len(rows) != expected:
        log.warning("%d pair(s) for a %d x %d grid - %d combination(s) the sheet "
                    "does not state", len(rows), len({r["from_name"] for r in rows}),
                    len({r["to_name"] for r in rows}), expected - len(rows))

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

            cur.execute("SELECT oil_name, id FROM crude_oil WHERE source_id = %s",
                        (source_id,))
            oil_ids = dict(cur.fetchall())
            if not oil_ids:
                sys.exit("Error: this source has no crude_oil rows.\n"
                         "  Run etl/oil/bp_cargo.py first.")

            cur.execute("SELECT procedure_code, loading_allowed FROM procedure_templates "
                        "WHERE source_id = %s", (source_id,))
            templates = dict(cur.fetchall())
            if not templates:
                sys.exit("Error: this source defines no procedure_templates.\n"
                         "  Run etl/oil/bp_procedure_templates.py first.")

            unknown_cargo: Dict[str, int] = {}
            unknown_code: Dict[str, int] = {}
            disagreements: List[str] = []
            payload: List[tuple] = []

            for r in rows:
                fid, tid = oil_ids.get(r["from_name"]), oil_ids.get(r["to_name"])
                for name, oid in ((r["from_name"], fid), (r["to_name"], tid)):
                    if oid is None:
                        unknown_cargo[name] = unknown_cargo.get(name, 0) + 1
                if fid is None or tid is None:
                    continue

                code = r["procedure_code"]
                if code not in templates:
                    unknown_code[code] = unknown_code.get(code, 0) + 1
                # The pair sheet repeats loading_allowed; the template owns it.
                # A disagreement means one of the two extracts is wrong, and
                # picking a winner silently is how that stops being noticed.
                elif r["loading_allowed"] is not None \
                        and templates[code] is not None \
                        and r["loading_allowed"] != templates[code]:
                    disagreements.append(
                        f"row {r['line']}: {r['from_name']!r} -> {r['to_name']!r} "
                        f"says loading_allowed={r['loading_allowed']} but template "
                        f"{code!r} says {templates[code]}")

                payload.append((CARGO_TYPE, fid, tid, code, source_id,
                                r["source_page_ref"], r["notes"]))

            for name, n in sorted(unknown_cargo.items()):
                log.error("cargo %r has no crude_oil row under this source (%d pair(s))",
                          name, n)
            for code, n in sorted(unknown_code.items()):
                log.warning("code %r is used in %d pair(s) but this source defines no "
                            "template for it - procedure_code is kept verbatim and the "
                            "row stays unlinked", code, n)
            for d in disagreements[:20]:
                log.error("%s", d)
            if unknown_cargo or disagreements:
                log.error("Refusing to import: the extracts disagree with each other.")
                conn.rollback()
                return 1

            if args.dry_run:
                for t in payload[:5]:
                    log.info("  from=%-5s to=%-5s code=%s", t[1], t[2], t[3])
                log.info("--dry-run: %d row(s) prepared, nothing written, rolling back.",
                         len(payload))
                conn.rollback()
                return 0

            for start in range(0, len(payload), BATCH):
                execute_values(
                    cur,
                    """
                    INSERT INTO cleaning_process
                        (cargo_type, from_cargo_id, to_cargo_id, procedure_code,
                         source_id, source_page_ref, notes, created_at, updated_at)
                    VALUES %s
                    ON CONFLICT (from_cargo_id, to_cargo_id, source_id,
                                 COALESCE(condition, ''))
                        WHERE from_cargo_id IS NOT NULL AND to_cargo_id IS NOT NULL
                    DO UPDATE SET cargo_type      = EXCLUDED.cargo_type,
                                  procedure_code  = EXCLUDED.procedure_code,
                                  source_page_ref = EXCLUDED.source_page_ref,
                                  notes           = EXCLUDED.notes,
                                  updated_at      = now()
                    """,
                    payload[start:start + BATCH],
                    template="(%s::\"CargoType\",%s,%s,%s,%s,%s,%s,now(),now())",
                    page_size=BATCH,
                )
                log.info("  upserted %d / %d", min(start + BATCH, len(payload)), len(payload))

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

            cur.execute(
                """SELECT count(*), count(procedure_template_id)
                     FROM cleaning_process
                    WHERE source_id = %s AND cargo_type = %s::"CargoType"
                      AND from_cargo_id IS NOT NULL""",
                (source_id, CARGO_TYPE),
            )
            total, with_template = cur.fetchone()

        conn.commit()
        log.info("✓ Committed. cleaning_process (OIL, source %s): %d pair row(s), "
                 "%d linked this run, %d of %d carry a template",
                 source_id, total, linked, with_template, total)
        return 0
    except Exception:
        conn.rollback()
        log.exception("Import failed - rolled back, nothing written.")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
