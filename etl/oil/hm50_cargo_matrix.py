#!/usr/bin/env python3
"""
Load the HM 50 cargo-to-cargo cleaning matrix into crude_oil + cleaning_process
(cargo_type OIL).

SOURCE
------
"HM Tank Cleaning Guide By Energy Institute" (source.json, category 'oil') - the
same source row as hm50_procedure_templates.py, which defines the codes this
matrix is keyed on and therefore MUST run first.

FILE LAYOUT
-----------
A grid, not a row-per-pair list:

    A1  "Grade discharged / Previous cargo"   B1..X1  the 23 cargoes to load
    A2..A20  the 19 cargoes discharged        B2..X20 the code for that pair

    row = previous cargo (discharged)   ->  cleaning_process.from_cargo_id
    column = next cargo (to load)       ->  cleaning_process.to_cargo_id
    cell = HM 50 code                   ->  cleaning_process.procedure_code
                                            + procedure_template_id, resolved
                                              after insert

THE MASTER
----------
crude_oil is the oil-cargo master (the same use shell_cargo_master.py makes of
it), so the 23 headings become crude_oil rows under this source. Identity is
(oil_name, source_id), so these sit alongside the Shell products without
colliding with them; nothing is merged across sources by this loader.

cargo_type is OIL on every row, so from_cargo_id / to_cargo_id are crude_oil
ids. Those columns carry NO foreign key - see the cleaning_process docstring in
schema.prisma - so they are validated by the cleaning_process_cargo_refs
trigger on the way in, not by a constraint.

TWO THINGS THE SHEET GETS WRONG, NEITHER OF THEM GUESSED AT
-----------------------------------------------------------
1. IT IS NOT SQUARE. 19 rows against 23 columns: 'Vacuum gas oil',
   'Fuel oil (sulfur >1%)', 'Low sulfur fuel oil (sulfur <1%)' and 'Crude oil
   and condensate' can be loaded but never discharged. Their crude_oil rows are
   still created (they are real cargoes, named by the source) and the ~92
   missing transitions are reported, not invented.

2. SIX ROWS HAVE ONE CELL TOO MANY. Every row physically spans A..Y, X1 is the
   last heading and Y1 is EMPTY, so six rows carry a value in a 24th column that
   names no cargo. Assigning it to a cargo would be a guess, so it is kept
   verbatim in `remarks` on the rows from that line and reported. Give column Y
   a heading in the sheet and re-run to place it properly.

IDEMPOTENCY
-----------
Re-running upserts on the partial unique index cleaning_process_pair_key
(from_cargo_id, to_cargo_id, source_id, COALESCE(condition,'')). One
transaction; any error rolls the whole import back.

Usage:
    python3 etl/oil/hm50_cargo_matrix.py
    python3 etl/oil/hm50_cargo_matrix.py --dry-run
    python3 etl/oil/hm50_cargo_matrix.py "/path/to/matrix.xlsx"
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
log = logging.getLogger("hm50_matrix")

SOURCE_NAME = "HM Tank Cleaning Guide By Energy Institute"
CARGO_TYPE = "OIL"
DEFAULT_FILE = input_file(
    "HM Tank Cleaning Guide By Energy Institute - cargo to cargo matrix.xlsx")
BATCH = 5000

# A1: the corner cell, a label rather than a cargo.
CORNER = "Grade discharged / Previous cargo"


def clean(value) -> Optional[str]:
    """Trim a cell; blank -> None."""
    if value is None:
        return None
    s = re.sub(r"\s+", " ", str(value).replace("\n", " ")).strip()
    return s or None


def norm_code(value) -> Optional[str]:
    """Same code spelling as hm50_procedure_templates.norm_code.

    Excel stores 1 / 2 / 3 as numbers, so they arrive as "1.0". The matrix must
    spell a code exactly as the legend loader stored it or the template link
    silently finds nothing.
    """
    s = clean(value)
    if s is None:
        return None
    return s[:-2] if re.fullmatch(r"\d+\.0", s) else s


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


def read_matrix(path: Path) -> Tuple[List[str], List[str], Dict[Tuple[str, str], str],
                                     Dict[str, str], List[str]]:
    """Read the grid.

    Returns (column cargoes, row cargoes, {(from, to): code}, {from: stray},
             errors).
    """
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.worksheets[0]
    grid = [list(r) for r in ws.iter_rows(values_only=True)]
    errors: List[str] = []

    if not grid:
        return [], [], {}, {}, [f"{path.name} is empty"]

    header = grid[0]
    corner = clean(header[0])
    if corner != CORNER:
        errors.append(f"A1 is {corner!r}, expected {CORNER!r} - is this the "
                      f"matrix sheet?")

    # Headings run from B until the first empty cell. Anything past that is the
    # unheaded 24th column, handled as a stray below.
    columns: List[str] = []
    for cell in header[1:]:
        name = clean(cell)
        if name is None:
            break
        columns.append(name)
    if len(set(columns)) != len(columns):
        errors.append("duplicate cargo heading(s) in row 1")

    n_cols = len(columns)
    rows: List[str] = []
    cells: Dict[Tuple[str, str], str] = {}
    strays: Dict[str, str] = {}

    for i, raw in enumerate(grid[1:], start=2):
        from_name = clean(raw[0])
        if from_name is None:
            continue                      # trailing blank rows / the merged footnote
        if from_name in rows:
            errors.append(f"row {i}: duplicate discharged cargo {from_name!r}")
            continue
        rows.append(from_name)

        for j, to_name in enumerate(columns):
            code = norm_code(raw[1 + j] if 1 + j < len(raw) else None)
            if code is None:
                continue                  # an empty cell states no rule
            cells[(from_name, to_name)] = code

        # Column Y and beyond: values that belong to no heading.
        extra = [clean(v) for v in raw[1 + n_cols:]]
        extra = [v for v in extra if v is not None]
        if extra:
            strays[from_name] = (
                f"Source row {i} carries {len(extra)} value(s) - "
                f"{', '.join(repr(v) for v in extra)} - in a column with no cargo "
                f"heading; kept here because assigning them to a cargo would be a "
                f"guess.")
    return columns, rows, cells, strays, errors


def ensure_cargoes(cur, source_id: int, names: List[str]) -> Dict[str, int]:
    """Upsert one crude_oil row per matrix cargo; return name -> id."""
    ids: Dict[str, int] = {}
    created = 0
    for name in names:
        cur.execute(
            "SELECT id FROM crude_oil WHERE oil_name = %s AND source_id = %s",
            (name, source_id),
        )
        row = cur.fetchone()
        if row:
            ids[name] = row[0]
            continue
        cur.execute(
            "INSERT INTO crude_oil (oil_name, source_id, created_at, updated_at) "
            "VALUES (%s, %s, now(), now()) RETURNING id",
            (name, source_id),
        )
        ids[name] = cur.fetchone()[0]
        created += 1
    log.info("crude_oil: %d cargo(es), %d created this run", len(ids), created)
    return ids


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", default=str(DEFAULT_FILE))
    ap.add_argument("--dry-run", action="store_true", help="parse and report, write nothing")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.is_file():
        sys.exit(f"Error: file not found: {path}")

    columns, rows, cells, strays, errors = read_matrix(path)
    if errors:
        log.error("%d validation error(s) - nothing was imported:", len(errors))
        for e in errors:
            log.error("    %s", e)
        return 1

    log.info("%s: %d column cargo(es) x %d row cargo(es), %d filled cell(s)",
             path.name, len(columns), len(rows), len(cells))

    load_only = [c for c in columns if c not in rows]
    if load_only:
        log.warning("%d cargo(es) appear as a column but never as a row, so the "
                    "sheet states no rule for discharging them (%d transitions "
                    "absent): %s", len(load_only), len(load_only) * len(columns),
                    "; ".join(load_only))
    row_only = [r for r in rows if r not in columns]
    if row_only:
        log.warning("%d cargo(es) appear as a row but never as a column: %s",
                    len(row_only), "; ".join(row_only))
    for from_name, msg in strays.items():
        log.warning("%s -> %s", from_name, msg)

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

            # Every cargo the sheet names, whether it can be discharged or only
            # loaded - a column-only cargo is still a cargo this guide covers.
            all_names = columns + [r for r in rows if r not in columns]
            ids = ensure_cargoes(cur, source_id, all_names)

            cur.execute("SELECT procedure_code FROM procedure_templates WHERE source_id = %s",
                        (source_id,))
            known_codes = {r[0] for r in cur.fetchall()}
            if not known_codes:
                sys.exit("Error: this source defines no procedure_templates.\n"
                         "  Run etl/oil/hm50_procedure_templates.py first.")

            unknown: Dict[str, int] = {}
            payload: List[tuple] = []
            for (from_name, to_name), code in cells.items():
                if code not in known_codes:
                    unknown[code] = unknown.get(code, 0) + 1
                payload.append((CARGO_TYPE, ids[from_name], ids[to_name], code,
                                source_id, strays.get(from_name)))

            for code, n in sorted(unknown.items(), key=lambda kv: -kv[1]):
                log.warning("code %r is used in %d cell(s) but this source defines "
                            "no template for it - procedure_code is kept verbatim "
                            "and the row stays unlinked", code, n)

            if args.dry_run:
                for t in payload[:5]:
                    log.info("  from=%-5s to=%-5s code=%-3s", t[1], t[2], t[3])
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
                         source_id, remarks, created_at, updated_at)
                    VALUES %s
                    ON CONFLICT (from_cargo_id, to_cargo_id, source_id,
                                 COALESCE(condition, ''))
                        WHERE from_cargo_id IS NOT NULL AND to_cargo_id IS NOT NULL
                    DO UPDATE SET cargo_type     = EXCLUDED.cargo_type,
                                  procedure_code = EXCLUDED.procedure_code,
                                  remarks        = EXCLUDED.remarks,
                                  updated_at     = now()
                    """,
                    payload[start:start + BATCH],
                    template="(%s::\"CargoType\",%s,%s,%s,%s,%s,now(),now())",
                    page_size=BATCH,
                )
                log.info("  upserted %d / %d", min(start + BATCH, len(payload)), len(payload))

            # Link each pair to its template. procedure_code is kept verbatim
            # either way, so a code the legend never defines stays unlinked
            # rather than blocking the import.
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
