#!/usr/bin/env python3
"""
Load the Shell cleaning matrix into crude_oil_compatibility + cleaning_process.

Source: "Shell Cleaning Matrix" (source.json, category 'oil').

WHAT THIS FILE IS
-----------------
    discharged_un | discharged_name | to_load_un | to_load_name
                  | regime_code     | Compatibillity

784 rows - a complete 28 x 28 directed matrix over the products loaded by
shell_cleaning_matrix_products.py, with no pair appearing twice. Each row says:
after discharging X, to load Y, follow regime N - and whether the pair is
compatible at all.

WHY THE SAME ROW GOES TO TWO TABLES
------------------------------------
A cell states two different kinds of fact and the schema keeps them apart:

    crude_oil_compatibility   MAY these two cargoes follow each other
    cleaning_process          WHAT TO DO between them

"Not compatible" is a verdict; "machine wash with cold sea water, then flush
lines" is a procedure. Writing the verdict only into cleaning_process would
bury it in a table about operations, and writing the procedure only into
crude_oil_compatibility would have nowhere to put the steps. So the verdict
goes to the first, the regime link to the second, and the regime's steps are
not copied into either - they live once on the procedure_templates row that
shell_cleaning_matrix_regimes.py loads, which cleaning_process points at.

DIRECTIONAL, AND STORED AS SUCH
--------------------------------
crude_oil_compatibility is directional by design - which cargo left the residue
decides the answer - and this file is directional too: all 784 ordered pairs are
present, so both (X, Y) and (Y, X) have their own row and neither is inferred
from the other. Nothing is canonicalised.

82 ROWS HAVE NO VERDICT, AND GET NO COMPATIBILITY ROW
-------------------------------------------------------
`Compatibillity` is blank on exactly 82 rows, and every one of them is regime
21 - "No cleaning regime available for this combination here, refer to STASCO
OTS/321". The regime file marks that regime's own compatibility 'N/A' for the
same reason. This is the source declining to answer, not a missing cell.

crude_oil_compatibility.compatible is NOT NULL, so there is no way to record
"no verdict" there and no honest value to invent: writing false would forbid
784 - 82 pairs the source never forbade, and writing true would permit them.
Those 82 pairs therefore get NO compatibility row. They still get their
cleaning_process row, because "refer to OTS/321" IS what to do.

The verdicts line up with the regimes exactly, which is checked on load:

    False  <-> regime 24 (45 rows)   "NOT compatible - do not load"
    blank  <-> regime 21 (82 rows)   "refer to OTS/321"
    True   <-> every other regime

A row whose verdict and regime disagree is a hard error, not a warning: the two
columns would then be telling different stories about the same pair.

CARGOES ARE RESOLVED BY NAME
-----------------------------
Through crude_oil.aggregated_name within this source, the same join
shell_cargo_matrix.py uses. Under this source the 28 names are distinct, so
each matrix row resolves to exactly one pair and nothing fans out. A name that
does not resolve is a hard error - it would mean this file and the products
file have drifted apart, and half a matrix is worse than none.

The un-number columns are NOT re-read. They are already loaded, from the
products file, against the product they belong to; reading them again here
would create a second place for them to be wrong. They are checked against it
instead, and a disagreement is reported.

IDEMPOTENCY
-----------
Upsert on (from_crude_oil_id, to_crude_oil_id, source_id) for the verdicts, and
on the partial unique index over (from_cargo_id, to_cargo_id, source_id,
COALESCE(condition,'')) for the processes. The whole file is validated before
anything is written; one transaction.

Usage:
    python3 etl/oil/shell_cleaning_matrix_compatibility.py
    python3 etl/oil/shell_cleaning_matrix_compatibility.py --dry-run
    python3 etl/oil/shell_cleaning_matrix_compatibility.py <file>
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
from psycopg2.extras import execute_values

# Loaders are run as scripts, so only their own directory is on sys.path.
_ETL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ETL_ROOT / "common"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _crude_oil import get_source_id  # noqa: E402
from _paths import input_file  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("shell_cleaning_matrix_compatibility")

SOURCE_NAME = "Shell Cleaning Matrix"
DEFAULT_FILE = input_file("Shell_cleaning_matrix_compatibility.xlsx")
SHEET = "Compatibility"
CARGO_TYPE = "OIL"
BATCH = 1000

COL_FROM = "discharged_name"
COL_TO = "to_load_name"
COL_FROM_UN = "discharged_un"
COL_TO_UN = "to_load_un"
COL_CODE = "regime_code"
COL_COMPAT = "Compatibillity"        # the sheet's own spelling
REQUIRED = [COL_FROM_UN, COL_FROM, COL_TO_UN, COL_TO, COL_CODE, COL_COMPAT]

# The verdict each of these regimes must carry, from the regime file. Any other
# regime must be compatible. Checked on every row - see the header.
VERDICT_BY_REGIME = {"24": False, "21": None}

NO_VERDICT_NOTE = (
    "The matrix leaves this pair's compatibility blank and sends the reader to "
    "STASCO OTS/321 (regime 21). That is the source declining to answer, not a "
    "missing cell, so the pair has NO crude_oil_compatibility row - "
    "compatible is NOT NULL there and both booleans would be inventions. The "
    "cleaning_process row below is what the source does say.")
NOT_COMPATIBLE_NOTE = (
    "The matrix marks this pair NOT compatible (regime 24): the last cargo is "
    "not compatible with the product to be loaded.")


def norm(value) -> str:
    """Collapse whitespace so a stray double space cannot break the join."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return re.sub(r"\s+", " ", str(value).replace(" ", " ")).strip()


def verdict_of(raw) -> Optional[bool]:
    """The Compatibillity cell as a boolean, or None when the sheet is blank."""
    text = norm(raw).lower()
    if text in ("", "nan", "none"):
        return None
    if text in ("true", "yes", "y", "1"):
        return True
    if text in ("false", "no", "n", "0"):
        return False
    raise ValueError(text)


def load_name_index(cur, source_id: int) -> Dict[str, List[int]]:
    """aggregated_name -> [crude_oil.id, ...] for this source."""
    cur.execute(
        "SELECT id, aggregated_name FROM crude_oil "
        " WHERE source_id = %s AND aggregated_name IS NOT NULL ORDER BY id",
        (source_id,))
    index: Dict[str, List[int]] = {}
    for oil_id, name in cur.fetchall():
        index.setdefault(norm(name), []).append(oil_id)
    return index


def load_un_index(cur, source_id: int) -> Dict[str, str]:
    """aggregated_name -> UN_NUMBER as the products loader stored it."""
    cur.execute(
        """SELECT co.aggregated_name, v.value
             FROM crude_oil co
             JOIN crude_oil_property_values v
               ON v.crude_oil_id = co.id AND v.field_name = 'UN_NUMBER'
            WHERE co.source_id = %s""",
        (source_id,))
    return {norm(name): norm(value) for name, value in cur.fetchall()}


def read_matrix(path: Path) -> Tuple[List[dict], List[str]]:
    errors: List[str] = []
    sheets = pd.ExcelFile(path).sheet_names
    if SHEET not in sheets:
        return [], [f"no sheet named {SHEET!r}; the workbook has {sheets!r}"]
    df = pd.read_excel(path, sheet_name=SHEET)

    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        return [], [f"missing column(s) {missing!r}; the sheet has "
                    f"{list(df.columns)!r}"]

    rows: List[dict] = []
    seen: Dict[Tuple[str, str], int] = {}
    for i, r in df.iterrows():
        line = i + 2
        from_name, to_name = norm(r[COL_FROM]), norm(r[COL_TO])
        code = norm(r[COL_CODE])
        # "1" not "1.0": pandas reads an all-integer column as float.
        if re.fullmatch(r"\d+\.0", code):
            code = code[:-2]

        if not from_name or not to_name:
            errors.append(f"row {line}: missing a cargo name "
                          f"({from_name!r} -> {to_name!r})")
            continue
        if not code:
            errors.append(f"row {line}: no {COL_CODE!r} for "
                          f"{from_name!r} -> {to_name!r}")
            continue
        if (from_name, to_name) in seen:
            errors.append(f"row {line}: pair {from_name!r} -> {to_name!r} "
                          f"already appeared on row {seen[(from_name, to_name)]}")
            continue
        seen[(from_name, to_name)] = line

        try:
            compatible = verdict_of(r[COL_COMPAT])
        except ValueError as exc:
            errors.append(f"row {line}: {COL_COMPAT!r} is {exc}, which is "
                          f"neither a verdict nor blank")
            continue

        # The two columns must tell the same story about the pair.
        expected = VERDICT_BY_REGIME.get(code, True)
        if compatible != expected:
            errors.append(
                f"row {line}: regime {code} implies compatible={expected!r} but "
                f"the sheet says {compatible!r} for {from_name!r} -> {to_name!r}")
            continue

        rows.append({"line": line, "from": from_name, "to": to_name,
                     "code": code, "compatible": compatible,
                     "from_un": norm(r[COL_FROM_UN]), "to_un": norm(r[COL_TO_UN])})

    if not rows and not errors:
        errors.append(f"{path.name} has no data rows")
    return rows, errors


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

    rows, errors = read_matrix(path)
    if errors:
        log.error("%d validation error(s) - nothing was imported:", len(errors))
        for e in errors[:25]:
            log.error("    %s", e)
        return 1

    names = {r["from"] for r in rows} | {r["to"] for r in rows}
    verdicts = [r for r in rows if r["compatible"] is not None]
    log.info("%d pair(s) over %d cargo(es); %d carry a verdict, %d do not",
             len(rows), len(names), len(verdicts), len(rows) - len(verdicts))
    log.info("    %d compatible, %d NOT compatible",
             sum(1 for r in verdicts if r["compatible"]),
             sum(1 for r in verdicts if not r["compatible"]))

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        sys.exit("Error: DATABASE_URL not set (checked .env).")

    conn = psycopg2.connect(db_url)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            source_id = get_source_id(cur, SOURCE_NAME)
            log.info("Source id=%s (%r)", source_id, SOURCE_NAME)

            index = load_name_index(cur, source_id)
            unresolved = sorted(n for n in names if n not in index)
            if unresolved:
                log.error("%d cargo name(s) have no crude_oil row under this "
                          "source. Run shell_cleaning_matrix_products.py first.",
                          len(unresolved))
                for n in unresolved[:15]:
                    log.error("    %s", n)
                conn.rollback()
                return 1
            fanned = sorted(n for n in names if len(index[n]) > 1)
            if fanned:
                log.error("%d name(s) resolve to more than one crude_oil row, "
                          "so a pair would not be one pair: %s",
                          len(fanned), ", ".join(fanned[:5]))
                conn.rollback()
                return 1
            log.info("resolved all %d cargo name(s), 1:1", len(names))

            # The un columns are already loaded from the products file; this
            # only reports drift between the two. See the header.
            un_index = load_un_index(cur, source_id)
            strip = lambda s: re.sub(r"\bUN\s*(?=\d)", "", s, flags=re.I).strip()  # noqa: E731
            drift = set()
            for r in rows:
                for name, printed in ((r["from"], r["from_un"]),
                                      (r["to"], r["to_un"])):
                    stored = un_index.get(name)
                    if stored and printed and strip(printed) != stored:
                        drift.add((name, printed, stored))
            if drift:
                log.warning("%d cargo(es) print a UN number here that differs "
                            "from the products file:", len(drift))
                for name, printed, stored in sorted(drift)[:10]:
                    log.warning("    %-46s sheet=%-16s stored=%s",
                                name[:46], printed, stored)
            else:
                log.info("UN numbers agree with the products file on every row")

            cur.execute("SELECT procedure_code, id FROM procedure_templates "
                        " WHERE source_id = %s", (source_id,))
            templates = dict(cur.fetchall())
            unknown = sorted({r["code"] for r in rows} - set(templates))
            if unknown:
                log.warning("regime code(s) with no procedure_templates row: %s",
                            ", ".join(unknown))

            page_ref = path.name
            compat_rows = []
            process_rows = []
            for r in rows:
                from_id, to_id = index[r["from"]][0], index[r["to"]][0]
                if r["compatible"] is not None:
                    compat_rows.append((
                        from_id, to_id, r["compatible"], source_id, r["code"],
                        NOT_COMPATIBLE_NOTE if not r["compatible"] else None))
                process_rows.append((
                    CARGO_TYPE, from_id, to_id, source_id, r["code"],
                    templates.get(r["code"]),
                    f"{r['from']} -> {r['to']}", page_ref,
                    NO_VERDICT_NOTE if r["compatible"] is None else None))

            if args.dry_run:
                log.info("would write %d crude_oil_compatibility row(s) and "
                         "%d cleaning_process row(s)",
                         len(compat_rows), len(process_rows))
                log.info("--dry-run: nothing written, rolling back.")
                conn.rollback()
                return 0

            for start in range(0, len(compat_rows), BATCH):
                execute_values(
                    cur,
                    """
                    INSERT INTO crude_oil_compatibility
                        (from_crude_oil_id, to_crude_oil_id, compatible,
                         source_id, procedure_code, notes, created_at, updated_at)
                    VALUES %s
                    ON CONFLICT (from_crude_oil_id, to_crude_oil_id, source_id)
                    DO UPDATE SET compatible     = EXCLUDED.compatible,
                                  procedure_code = EXCLUDED.procedure_code,
                                  notes          = EXCLUDED.notes,
                                  updated_at     = now()
                    """,
                    compat_rows[start:start + BATCH],
                    template="(%s,%s,%s,%s,%s,%s,now(),now())", page_size=BATCH)
            log.info("crude_oil_compatibility: %d row(s)", len(compat_rows))

            for start in range(0, len(process_rows), BATCH):
                execute_values(
                    cur,
                    """
                    INSERT INTO cleaning_process
                        (cargo_type, from_cargo_id, to_cargo_id, source_id,
                         procedure_code, procedure_template_id, title,
                         source_page_ref, notes, created_at, updated_at)
                    VALUES %s
                    ON CONFLICT (from_cargo_id, to_cargo_id, source_id,
                                 COALESCE(condition, ''))
                        WHERE from_cargo_id IS NOT NULL AND to_cargo_id IS NOT NULL
                    DO UPDATE SET cargo_type            = EXCLUDED.cargo_type,
                                  procedure_code        = EXCLUDED.procedure_code,
                                  procedure_template_id = EXCLUDED.procedure_template_id,
                                  title                 = EXCLUDED.title,
                                  source_page_ref       = EXCLUDED.source_page_ref,
                                  notes                 = EXCLUDED.notes,
                                  updated_at            = now()
                    """,
                    process_rows[start:start + BATCH],
                    template="(%s::\"CargoType\",%s,%s,%s,%s,%s,%s,%s,%s,now(),now())",
                    page_size=BATCH)
            log.info("cleaning_process: %d row(s)", len(process_rows))

        conn.commit()
        log.info("✓ Committed. crude_oil_compatibility: %d | cleaning_process: %d",
                 len(compat_rows), len(process_rows))
        return 0
    except Exception:
        conn.rollback()
        log.exception("Import failed - rolled back, nothing written.")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
