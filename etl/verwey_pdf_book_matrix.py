#!/usr/bin/env python3
"""
Load the Dr Verwey Tank Cleaning Guide (PDF Book) FROM->TO cleaning matrix into
cleaning_process at FAMILY grain.

The sheet is 431 x 431. Row 0 lists the TO family numbers; each data row starts
with its FROM family number and name, then one procedure code per TO family:

              1     2     3    ...
    1  ACETIC ACID          X+    FA+   FA
    2  ACETIC ACID ANHYD.   BA+   X+    BA
    3  ACETONE              AB+   AB+   X

The book keys these cells on the family number and never on a member chemical,
so each cell becomes ONE cleaning_process row against
from_cargo_family_group_id / to_cargo_family_group_id. The cargo-grain columns
(from_cargo_id / to_cargo_id) are left NULL; they carry sources 8 and 9, which
this script does not touch. Readers pick the column pair by source_id.

Fanning a cell out onto the member chemicals instead would turn 1 assertion by
Verwey into up to 19 (family [296] has 19 members) and make the fabricated rows
indistinguishable from the book's own after the next revision.

NO steps are copied into cleaning_process_step. The steps live once in
procedure_template_steps and are reached through
(source_id, procedure_code) -> procedure_templates.

Idempotent: the partial UNIQUE index on
(source_id, from_cargo_family_group_id, to_cargo_family_group_id) makes a
re-run an upsert.

Run AFTER verwey_pdf_book_procedures.py and verwey_pdf_book_families.py.

Usage:
    python3 etl/verwey_pdf_book_matrix.py
    python3 etl/verwey_pdf_book_matrix.py --dry-run
    python3 etl/verwey_pdf_book_matrix.py --limit 1000
"""

import argparse
import csv
import logging
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

from _paths import input_file
from cargo_chemicals import get_source_id

SOURCE_NAME = "Dr Verweys Tank Cleaning Guide Pdf Book"
DEFAULT_FILE = input_file("Dr Verweys Tank Cleaning Guide Pdf Book  _from-to procedure.csv")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("verwey_pdf_book_matrix")

# A cell holds a procedure code: 1-2 letters, optionally suffixed + or -.
CODE_RE = re.compile(r"^[A-Za-z]{1,2}[+-]?$")

BATCH = 5000


def normalise_code(code: str, templates: set) -> Tuple[Optional[str], Optional[str]]:
    """Map a raw cell to a loadable procedure_code.

    Returns (code, note). A None code means the cell cannot be loaded.

    Two source defects are handled:
      * 'c+' - lowercase typo for 'C+' (one cell, row 235 Methyl tert-Butyl
        Ether). Case is corrected.
      * 'TX+', 'WX+', 'YX+', 'ZX+' - the '+' variants are not in the book; only
        the bare TX/WX/YX/ZX are. Those four codes all read "It is advisable NOT
        to load and carry this product after this cargo", so a stricter
        next-cargo standard adds nothing to the verdict and the base code is
        used. The substitution is recorded on the row.
    """
    if code in templates:
        return code, None

    upper = code.upper()
    if upper in templates:
        return upper, f"case corrected from {code!r}"

    base = upper.rstrip("+-")
    if base != upper and base in templates:
        return base, f"{code!r} has no template in this edition; used base code {base!r}"

    return None, f"no template for {code!r}"


def parse_matrix(path: Path) -> Tuple[List[Tuple[str, str, str]], Dict[str, int]]:
    """Return ([(from_code, to_code, raw_cell), ...], stats)."""
    with open(path, encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    if not rows:
        return [], {}

    header = rows[0]
    # Columns 2.. hold the TO family number; column 0 is FROM, column 1 its name.
    to_codes = [(i, header[i].strip()) for i in range(2, len(header))
                if header[i].strip().isdigit()]

    cells: List[Tuple[str, str, str]] = []
    empty_rows: List[str] = []
    malformed = Counter()

    for row in rows[1:]:
        if len(row) < 2 or not row[0].strip().isdigit():
            continue
        from_code = row[0].strip()
        found = False
        for index, to_code in to_codes:
            if index >= len(row):
                continue
            cell = row[index].strip()
            if not cell:
                continue
            if not CODE_RE.match(cell):
                malformed[cell] += 1
                continue
            cells.append((from_code, to_code, cell))
            found = True
        if not found:
            empty_rows.append(from_code)

    stats = {"empty_rows": empty_rows, "malformed": malformed}
    return cells, stats


def resolve_source(cur) -> int:
    sid = get_source_id(cur, SOURCE_NAME)
    if sid is None:
        log.error("Source %r not found - run verwey_pdf_book.py first", SOURCE_NAME)
        sys.exit(1)
    log.info("Using source id=%s (%r)", sid, SOURCE_NAME)
    return sid


def load(cur, cells: List[Tuple[str, str, str]], source_id: int,
         dry_run: bool) -> Tuple[int, Counter, Counter]:
    cur.execute(
        "SELECT family_code, id FROM cargo_family_group WHERE source_id = %s", (source_id,)
    )
    families = dict(cur.fetchall())
    log.info("Families available: %d", len(families))
    if not families:
        log.error("No cargo_family_group rows - run verwey_pdf_book_families.py first")
        sys.exit(1)

    cur.execute(
        "SELECT procedure_code FROM procedure_templates WHERE source_id = %s", (source_id,)
    )
    templates = {r[0] for r in cur.fetchall()}
    log.info("Procedure templates available: %d", len(templates))
    if not templates:
        log.error("No procedure_templates - run verwey_pdf_book_procedures.py first")
        sys.exit(1)

    rows: List[tuple] = []
    substituted = Counter()
    dropped = Counter()
    unknown_family = Counter()

    for from_code, to_code, raw in cells:
        from_id = families.get(from_code)
        to_id = families.get(to_code)
        if from_id is None or to_id is None:
            unknown_family[from_code if from_id is None else to_code] += 1
            continue

        code, note = normalise_code(raw, templates)
        if code is None:
            dropped[raw] += 1
            continue
        if note:
            substituted[raw] += 1
        rows.append((from_id, to_id, code, source_id, note))

    log.info("Matrix cells -> %d loadable rows", len(rows))
    if substituted:
        for raw, n in substituted.most_common():
            log.warning("Code %r substituted on %d cell(s)", raw, n)
    if dropped:
        for raw, n in dropped.most_common():
            log.warning("Code %r has no template - %d cell(s) SKIPPED", raw, n)
    if unknown_family:
        log.warning("Cells referencing an unknown family: %s", dict(unknown_family))

    if dry_run or not rows:
        return len(rows), substituted, dropped

    for start in range(0, len(rows), BATCH):
        chunk = rows[start:start + BATCH]
        execute_values(
            cur,
            """
            INSERT INTO cleaning_process
                (from_cargo_family_group_id, to_cargo_family_group_id,
                 procedure_code, source_id, notes, created_at, updated_at)
            VALUES %s
            ON CONFLICT (source_id, from_cargo_family_group_id, to_cargo_family_group_id)
                WHERE from_cargo_family_group_id IS NOT NULL
                  AND to_cargo_family_group_id IS NOT NULL
            DO UPDATE SET procedure_code = EXCLUDED.procedure_code,
                          notes          = EXCLUDED.notes,
                          updated_at     = now()
            """,
            chunk,
            template="(%s,%s,%s,%s,%s,now(),now())",
            page_size=BATCH,
        )
        log.info("  inserted %d / %d", min(start + BATCH, len(rows)), len(rows))

    # Point every row at its procedure_templates row. Until the CargoType
    # refactor this link was implicit in the (source_id, procedure_code)
    # composite FK; that FK is gone now that cleaning_process is polymorphic,
    # so the id has to be resolved explicitly. Idempotent.
    cur.execute(
        """
        UPDATE cleaning_process cp
           SET procedure_template_id = pt.id
          FROM procedure_templates pt
         WHERE pt.source_id = cp.source_id
           AND pt.procedure_code = cp.procedure_code
           AND cp.source_id = %s
           AND cp.procedure_code IS NOT NULL
           AND cp.procedure_template_id IS DISTINCT FROM pt.id
        """,
        (source_id,),
    )
    log.info("  linked %d row(s) to procedure_templates", cur.rowcount)

    return len(rows), substituted, dropped


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", default=str(DEFAULT_FILE))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, help="only load the first N cells")
    args = ap.parse_args()

    path = Path(args.file).expanduser()
    if not path.exists():
        log.error("Input file not found: %s", path)
        sys.exit(1)

    cells, stats = parse_matrix(path)
    log.info("Parsed %s: %d non-empty cells", path.name, len(cells))
    if stats.get("empty_rows"):
        log.warning("FROM families with a completely empty row (no data recorded, "
                    "NOT 'no cleaning required'): %s", stats["empty_rows"])
    if stats.get("malformed"):
        log.warning("Cells that do not look like a procedure code: %s",
                    dict(stats["malformed"]))
    if args.limit:
        cells = cells[: args.limit]
    if not cells:
        sys.exit(1)

    load_dotenv(".env")
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        log.error("DATABASE_URL not set (run from the repo root)")
        sys.exit(1)

    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            source_id = resolve_source(cur)
            loaded, substituted, dropped = load(cur, cells, source_id, args.dry_run)
        if args.dry_run:
            conn.rollback()
            log.info("DRY RUN - rolled back (%d rows)", loaded)
        else:
            conn.commit()
            log.info("✓ Committed %d family-grain cleaning_process rows "
                     "(%d substituted, %d skipped) for source_id=%s",
                     loaded, sum(substituted.values()), sum(dropped.values()), source_id)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
