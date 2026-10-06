#!/usr/bin/env python3
"""
Load the Shell White Oil Tank Cleaning Guide cargo-to-cargo grid into
cleaning_process, crude_oil_compatibility AND crude_oil_compatibility_exception.

SOURCE
------
"Shell White Oil Tank Cleaning Guide" (source.json, category 'oil'). Runs LAST
of the three: cargo names resolve against shell_white_oil_cargo.py's crude_oil
rows, procedure codes against shell_white_oil_procedure_templates.py's
templates.

INPUTS - THE SAME 256 PAIRS, TWICE, PLUS AN OVERLAY
---------------------------------------------------
1. "... - cleaning process.xlsx"  256 rows = a complete 16 x 16 grid

       id | grade_to_be_loaded | grade_discharged_previous_cargo |
       procedure_code | compatibility_status

   The OPERATIONAL answer: which code to run. Where the guide prints "X/2" this
   sheet carries the workable half, 2, and where it prints a bare "X" it carries
   nothing at all.

2. "... - compatibility.xlsx"     256 rows, the same grid

       id | previous_cargo | cargo_to_be_loaded | procedure_code |
       compatibility_status | is_compatible | compatibility_reason

   The COMPATIBILITY answer: may this be loaded at all. Its procedure_code is
   the guide's verbatim printing - "X", "X/2", "X/4" survive here.

3. "... - compatibility exceptions.xlsx"  9 rows

       id | cargo_oil_name_from | cargo_oil_name_to | exception

   Flags 9 of the 256 pairs as exceptions - see EXCEPTIONS.

Note the column order differs between (1) and (2): sheet 1 names the cargo being
LOADED first, sheet 2 names the PREVIOUS cargo first. Both describe the same
directed pair, and this loader reads each by its own column names, so the two
agree. The direction stored is always previous -> next:

    grade_discharged_previous_cargo = previous_cargo = from_cargo_id
    grade_to_be_loaded              = cargo_to_be_loaded = to_cargo_id

The two sheets are cross-checked pair by pair before anything is written; if
they state different pair sets or disagree on a status, nothing is imported.

ONE GRID, THREE FACTS, THREE TABLES
------------------------------------
    cleaning process procedure_code -> cleaning_process (+ procedure_template_id)
    compatibility  is_compatible    -> crude_oil_compatibility.compatible
    compatibility  procedure_code   -> crude_oil_compatibility.procedure_code,
                                       verbatim, X and all
    compatibility_status            -> cleaning_process.notes and the
                                       compatibility row's notes
    exceptions sheet                -> crude_oil_compatibility_exception,
                                       one row per flagged pair (see EXCEPTIONS)

crude_oil_compatibility and crude_oil_compatibility_exception exist because
neither `compatibility` (FKs reactive_groups) nor `compatibility_exception`
(FKs cargo_chemical) can reference crude_oil. crude_oil_compatibility is
DIRECTIONAL, which this grid needs: Avgas -> SBP is X while SBP -> Avgas is
code 1.

THE THREE STATUSES
------------------
    COMPATIBLE   239 pairs  is_compatible 1     a code is given
    INCOMPATIBLE   8 pairs  is_compatible 0     "X", not to be loaded; the
                                                cleaning sheet leaves the code
                                                empty and so does this loader
    CONDITIONAL    9 pairs  is_compatible EMPTY the aviation exception below

crude_oil_compatibility.compatible is NOT NULL, so the 9 CONDITIONAL pairs need
an answer the sheet does not spell. They are stored as compatible = true on
instruction: the guide gives each of them a workable code (2 or 4) once the
stated mercaptan / H2S limits are met, so loading is allowed, conditionally. The
condition is not lost - compatibility_reason is kept verbatim in notes, and the
verbatim "X/2" / "X/4" stays in procedure_code, so a reader never sees a bare
"yes".

EXCEPTIONS
----------
The exception sheet's 9 rows are exactly the 9 CONDITIONAL pairs (Naphtha,
Naphtha (Lead Free), Natural Gasoline -> Avgas, Avtag, Avtur). compatibility_
exception can't hold them (it FKs cargo_chemical, not crude_oil), so each gets
its own row in crude_oil_compatibility_exception instead - same pair, same
compatible/procedure_code/reason as its crude_oil_compatibility row, since the
exception IS that pair, not a separate verdict. A row the exception sheet names
that is NOT conditional in the compatibility sheet is a hard error, because that
would mean the two sheets disagree about which pairs are exceptional.

DATES
-----
One procedure code reaches this loader as a datetime: the extract was typed in a
spreadsheet, which read "1/2" (run code 1 and code 2) as a date and stored
2026-02-01. It is restored from an explicit table below, confirmed by the source
owner; any other date is a hard error rather than a guess. "1/2" is kept
verbatim as the code - it names two procedures, and picking one would be a
decision this project has no standing to make - so that pair loads with
procedure_template_id NULL and is reported.

IDEMPOTENCY
-----------
cleaning_process upserts on cleaning_process_pair_key (from_cargo_id,
to_cargo_id, source_id, COALESCE(condition,'')); crude_oil_compatibility and
crude_oil_compatibility_exception both on (from_crude_oil_id, to_crude_oil_id,
source_id). One transaction; any error rolls the whole import back.

Usage:
    python3 etl/oil/shell_white_oil_matrix.py
    python3 etl/oil/shell_white_oil_matrix.py --dry-run
"""

import argparse
import datetime as dt
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
log = logging.getLogger("shell_white_oil_matrix")

SOURCE_NAME = "Shell White Oil Tank Cleaning Guide"
CARGO_TYPE = "OIL"
BATCH = 5000

DEFAULT_CLEANING = input_file(
    "Shell White Oil Tank Cleaning Guide - cleaning process.xlsx")
DEFAULT_COMPAT = input_file(
    "Shell White Oil Tank Cleaning Guide - compatibility.xlsx")
DEFAULT_EXCEPTIONS = input_file(
    "Shell White Oil Tank Cleaning Guide - compatibility exceptions.xlsx")

CLEANING_COLS = ["grade_to_be_loaded", "grade_discharged_previous_cargo",
                 "procedure_code", "compatibility_status"]
COMPAT_COLS = ["previous_cargo", "cargo_to_be_loaded", "procedure_code",
               "compatibility_status", "is_compatible", "compatibility_reason"]
EXCEPTION_COLS = ["cargo_oil_name_from", "cargo_oil_name_to", "exception"]

STATUSES = {"COMPATIBLE", "INCOMPATIBLE", "CONDITIONAL"}

# is_compatible is empty on every CONDITIONAL row and the column it lands in is
# NOT NULL. See THE THREE STATUSES: allowed once the stated limits are met.
CONDITIONAL_COMPATIBLE = True

# Cells a spreadsheet turned into dates, and the text that was typed. Restored
# on instruction, never inferred - only the person who typed it knows whether
# 2026-02-01 was "1/2" or "2/1". A date NOT listed here stops the import.
DATE_TEXT: Dict[dt.date, str] = {
    dt.date(2026, 2, 1): "1/2",   # procedure_code, the Kerosine -> Gasoil pair
}

# Progress markers the extracts carry in their own cells. They name no cargo, so
# a row carrying one is skipped and reported rather than resolved.
MARKERS = {"done", "ok", "completed", "-"}


def clean(value) -> Optional[str]:
    if value is None:
        return None
    s = re.sub(r"\s+", " ", str(value).replace("\n", " ")).strip()
    return s or None


def cell_text(value, where: str, errors: List[str]) -> Optional[str]:
    """clean(), plus the date restoration described under DATES."""
    if isinstance(value, (dt.datetime, dt.date)):
        key = value.date() if isinstance(value, dt.datetime) else value
        text = DATE_TEXT.get(key)
        if text is None:
            errors.append(f"{where}: holds the date {key.isoformat()}, which a "
                          f"spreadsheet made out of typed text. Add the original "
                          f"text to DATE_TEXT in this loader and re-run.")
            return None
        return text
    s = clean(value)
    if s is None:
        return None
    # Whole-number codes arrive from Excel as "1.0".
    return s[:-2] if re.fullmatch(r"\d+\.0", s) else s


def to_bool(value) -> Optional[bool]:
    """Read the sheet's 1.0 / 0.0 flags. Empty stays None - see the statuses."""
    s = clean(value)
    if s is None:
        return None
    return s.lower() in {"1", "1.0", "true", "yes", "y"}


def read_sheet(path: Path, wanted: List[str]) -> List[dict]:
    ws = openpyxl.load_workbook(path, data_only=True).worksheets[0]
    grid = [list(r) for r in ws.iter_rows(values_only=True)]
    if not grid:
        sys.exit(f"Error: {path.name} is empty")
    header = [clean(c) for c in grid[0]]
    missing = [c for c in wanted if c not in header]
    if missing:
        sys.exit(f"Error: {path.name} is missing column(s): {', '.join(missing)}")
    return [dict(zip(header, row)) for row in grid[1:] if any(c is not None for c in row)]


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


def read_status(value, where: str, errors: List[str]) -> Optional[str]:
    status = clean(value)
    if status is None:
        errors.append(f"{where}: no compatibility_status")
        return None
    status = status.upper()
    if status not in STATUSES:
        errors.append(f"{where}: compatibility_status {status!r} is not one of "
                      f"{sorted(STATUSES)}")
        return None
    return status


def read_cleaning(path: Path) -> Tuple[Dict[Tuple[str, str], dict], List[str], List[str]]:
    """Sheet 1 -> {(from, to): row}. Returns (pairs, skipped, errors)."""
    pairs: Dict[Tuple[str, str], dict] = {}
    skipped: List[str] = []
    errors: List[str] = []

    for i, r in enumerate(read_sheet(path, CLEANING_COLS), start=2):
        frm = clean(r["grade_discharged_previous_cargo"])
        to = clean(r["grade_to_be_loaded"])
        if frm is None and to is None:
            continue
        for name in (frm, to):
            if name is not None and name.lower() in MARKERS:
                skipped.append(f"cleaning row {i}: {name!r} is a progress marker")
                frm = None
                break
        if frm is None or to is None:
            if not any(s.startswith(f"cleaning row {i}:") for s in skipped):
                errors.append(f"cleaning row {i}: one side of the pair is blank")
            continue
        if (frm, to) in pairs:
            errors.append(f"cleaning row {i}: duplicate pair {frm!r} -> {to!r}")
            continue

        status = read_status(r["compatibility_status"], f"cleaning row {i}", errors)
        pairs[(frm, to)] = {
            "procedure_code": cell_text(r["procedure_code"],
                                        f"cleaning row {i} procedure_code", errors),
            "status": status,
        }
    return pairs, skipped, errors


def read_compat(path: Path) -> Tuple[Dict[Tuple[str, str], dict], List[str], List[str]]:
    """Sheet 2 -> {(from, to): row}. Returns (pairs, skipped, errors)."""
    pairs: Dict[Tuple[str, str], dict] = {}
    skipped: List[str] = []
    errors: List[str] = []

    for i, r in enumerate(read_sheet(path, COMPAT_COLS), start=2):
        frm = clean(r["previous_cargo"])
        to = clean(r["cargo_to_be_loaded"])
        if frm is None and to is None:
            continue
        for name in (frm, to):
            if name is not None and name.lower() in MARKERS:
                skipped.append(f"compatibility row {i}: {name!r} is a progress marker")
                frm = None
                break
        if frm is None or to is None:
            if not any(s.startswith(f"compatibility row {i}:") for s in skipped):
                errors.append(f"compatibility row {i}: one side of the pair is blank")
            continue
        if (frm, to) in pairs:
            errors.append(f"compatibility row {i}: duplicate pair {frm!r} -> {to!r}")
            continue

        status = read_status(r["compatibility_status"], f"compatibility row {i}", errors)
        flag = to_bool(r["is_compatible"])
        if flag is None:
            if status == "CONDITIONAL":
                flag = CONDITIONAL_COMPATIBLE
            elif status is not None:
                errors.append(f"compatibility row {i}: {status} but is_compatible is "
                              f"empty; only CONDITIONAL rows may leave it blank")
                continue
        elif status == "INCOMPATIBLE" and flag:
            errors.append(f"compatibility row {i}: INCOMPATIBLE but is_compatible is true")
            continue
        elif status == "COMPATIBLE" and not flag:
            errors.append(f"compatibility row {i}: COMPATIBLE but is_compatible is false")
            continue

        pairs[(frm, to)] = {
            "procedure_code": cell_text(r["procedure_code"],
                                        f"compatibility row {i} procedure_code", errors),
            "status": status,
            "compatible": flag,
            "reason": clean(r["compatibility_reason"]),
        }
    return pairs, skipped, errors


def read_exceptions(path: Path) -> Tuple[Dict[Tuple[str, str], str], List[str], List[str]]:
    """Sheet 3 -> {(from, to): flag text}. Returns (exceptions, skipped, errors)."""
    exceptions: Dict[Tuple[str, str], str] = {}
    skipped: List[str] = []
    errors: List[str] = []

    for i, r in enumerate(read_sheet(path, EXCEPTION_COLS), start=2):
        frm = clean(r["cargo_oil_name_from"])
        to = clean(r["cargo_oil_name_to"])
        if frm is None and to is None:
            continue
        for name in (frm, to):
            if name is not None and name.lower() in MARKERS:
                skipped.append(f"exceptions row {i}: {name!r} is a progress marker")
                frm = None
                break
        if frm is None or to is None:
            if not any(s.startswith(f"exceptions row {i}:") for s in skipped):
                errors.append(f"exceptions row {i}: one side of the pair is blank")
            continue

        flag = clean(r["exception"])
        if flag is None:
            errors.append(f"exceptions row {i}: {frm!r} -> {to!r} has no exception value")
            continue
        exceptions[(frm, to)] = flag
    return exceptions, skipped, errors


def cross_check(cleaning: Dict[Tuple[str, str], dict],
                compat: Dict[Tuple[str, str], dict],
                exceptions: Dict[Tuple[str, str], str]) -> List[str]:
    """The three sheets are extracts of one table and must agree."""
    errors: List[str] = []

    only_cleaning = sorted(set(cleaning) - set(compat))
    only_compat = sorted(set(compat) - set(cleaning))
    for frm, to in only_cleaning[:10]:
        errors.append(f"{frm!r} -> {to!r} is in the cleaning sheet but not the "
                      f"compatibility sheet")
    for frm, to in only_compat[:10]:
        errors.append(f"{frm!r} -> {to!r} is in the compatibility sheet but not the "
                      f"cleaning sheet")
    if len(only_cleaning) > 10 or len(only_compat) > 10:
        errors.append(f"... {len(only_cleaning) + len(only_compat)} pair(s) differ in total")

    for key in sorted(set(cleaning) & set(compat)):
        a, b = cleaning[key]["status"], compat[key]["status"]
        if a != b:
            errors.append(f"{key[0]!r} -> {key[1]!r}: cleaning sheet says {a}, "
                          f"compatibility sheet says {b}")

    for key, flag in sorted(exceptions.items()):
        row = compat.get(key)
        if row is None:
            errors.append(f"exception {key[0]!r} -> {key[1]!r} names a pair the "
                          f"compatibility sheet does not state")
        elif row["status"] != "CONDITIONAL":
            errors.append(f"exception {key[0]!r} -> {key[1]!r} is {row['status']} in "
                          f"the compatibility sheet, not CONDITIONAL - the sheets "
                          f"disagree about which pairs are exceptional")
    return errors


def build_notes(status: Optional[str], reason: Optional[str]) -> Optional[str]:
    """One readable line, in the source's own words."""
    parts = []
    if status:
        parts.append(f"Shell White Oil status: {status}.")
    if reason:
        parts.append(reason if reason.endswith(".") else reason + ".")
    return " ".join(parts) or None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cleaning-file", default=str(DEFAULT_CLEANING))
    ap.add_argument("--compatibility-file", default=str(DEFAULT_COMPAT))
    ap.add_argument("--exceptions-file", default=str(DEFAULT_EXCEPTIONS))
    ap.add_argument("--dry-run", action="store_true",
                    help="validate all three sheets and report; write nothing")
    args = ap.parse_args()

    paths = [Path(args.cleaning_file), Path(args.compatibility_file),
             Path(args.exceptions_file)]
    for p in paths:
        if not p.is_file():
            sys.exit(f"Error: file not found: {p}")
    cpath, mpath, xpath = paths

    cleaning, skipped_a, errors = read_cleaning(cpath)
    compat, skipped_b, errs = read_compat(mpath)
    errors += errs
    exceptions, skipped_c, errs = read_exceptions(xpath)
    errors += errs
    errors += cross_check(cleaning, compat, exceptions)

    if errors:
        log.error("%d validation error(s) - nothing was imported:", len(errors))
        for e in errors[:25]:
            log.error("    %s", e)
        return 1

    froms = {f for f, _ in compat}
    tos = {t for _, t in compat}
    log.info("%s / %s: %d pair(s), %d x %d grid", cpath.name, mpath.name,
             len(compat), len(froms), len(tos))
    if len(compat) != len(froms) * len(tos):
        log.warning("%d combination(s) the sheets do not state",
                    len(froms) * len(tos) - len(compat))
    for s in skipped_a + skipped_b + skipped_c:
        log.warning("skipped %s", s)
    for status in sorted(STATUSES):
        log.info("  %-12s %d pair(s)", status,
                 sum(1 for r in compat.values() if r["status"] == status))
    log.info("%s: %d exception overlay(s)", xpath.name, len(exceptions))

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
                         "  Run etl/oil/shell_white_oil_cargo.py first.")

            cur.execute("SELECT procedure_code FROM procedure_templates WHERE source_id = %s",
                        (source_id,))
            known = {r[0] for r in cur.fetchall()}
            if not known:
                sys.exit("Error: this source defines no procedure_templates.\n"
                         "  Run etl/oil/shell_white_oil_procedure_templates.py first.")

            unknown_cargo: Dict[str, int] = {}
            unknown_code: Dict[str, int] = {}
            cp_rows: List[tuple] = []
            compat_rows: List[tuple] = []
            exception_rows: List[tuple] = []

            for key in sorted(compat):
                frm, to = key
                fid, tid = oil_ids.get(frm), oil_ids.get(to)
                for name, oid in ((frm, fid), (to, tid)):
                    if oid is None:
                        unknown_cargo[name] = unknown_cargo.get(name, 0) + 1
                if fid is None or tid is None:
                    continue

                c_row = compat[key]
                notes = build_notes(c_row["status"], c_row["reason"])

                # The operational code comes from the cleaning sheet; the
                # compatibility sheet's is the guide's verbatim printing.
                code = cleaning[key]["procedure_code"]
                if code is not None and code not in known:
                    unknown_code[code] = unknown_code.get(code, 0) + 1

                cp_rows.append((CARGO_TYPE, fid, tid, code, source_id, notes))
                compat_rows.append((fid, tid, c_row["compatible"], source_id,
                                    c_row["procedure_code"], notes))

                # Flagged by the exceptions sheet: its own row, same pair, same
                # compatible/procedure_code/reason as crude_oil_compatibility
                # above - see EXCEPTIONS.
                if key in exceptions:
                    exception_rows.append((fid, tid, c_row["compatible"], source_id,
                                           c_row["procedure_code"], notes))

            for name, n in sorted(unknown_cargo.items()):
                log.error("cargo %r has no crude_oil row under this source (%d pair(s)). "
                          "Run etl/oil/shell_white_oil_cargo.py first.", name, n)
            if unknown_cargo:
                log.error("Refusing to import: the extracts name cargoes the master "
                          "does not have.")
                conn.rollback()
                return 1

            for code, n in sorted(unknown_code.items()):
                log.warning("code %r is used by %d pair(s) but this source defines no "
                            "template - procedure_code kept verbatim, row left "
                            "unlinked", code, n)
            log.info("pairs with no procedure_code (the guide prints X, not to be "
                     "loaded): %d", sum(1 for r in cp_rows if r[3] is None))

            if args.dry_run:
                log.info("--dry-run: %d cleaning_process + %d crude_oil_compatibility + "
                         "%d crude_oil_compatibility_exception row(s) prepared, nothing "
                         "written.", len(cp_rows), len(compat_rows), len(exception_rows))
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

            if exception_rows:
                for start in range(0, len(exception_rows), BATCH):
                    execute_values(
                        cur,
                        """
                        INSERT INTO crude_oil_compatibility_exception
                            (from_crude_oil_id, to_crude_oil_id, compatible, source_id,
                             procedure_code, notes, created_at, updated_at)
                        VALUES %s
                        ON CONFLICT (from_crude_oil_id, to_crude_oil_id, source_id)
                        DO UPDATE SET compatible     = EXCLUDED.compatible,
                                      procedure_code = EXCLUDED.procedure_code,
                                      notes          = EXCLUDED.notes,
                                      updated_at     = now()
                        """,
                        exception_rows[start:start + BATCH],
                        template="(%s,%s,%s,%s,%s,%s,now(),now())",
                        page_size=BATCH,
                    )
            # A pair the exceptions sheet no longer names is no longer an exception.
            current_pairs = {(fid, tid) for fid, tid, *_ in exception_rows}
            cur.execute(
                "SELECT id, from_crude_oil_id, to_crude_oil_id "
                "FROM crude_oil_compatibility_exception WHERE source_id = %s",
                (source_id,),
            )
            stale_ids = [rid for rid, fid, tid in cur.fetchall()
                        if (fid, tid) not in current_pairs]
            if stale_ids:
                cur.execute(
                    "DELETE FROM crude_oil_compatibility_exception WHERE id = ANY(%s)",
                    (stale_ids,),
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
            cur.execute("SELECT count(*) FROM crude_oil_compatibility_exception "
                       "WHERE source_id = %s", (source_id,))
            exc_total = cur.fetchone()[0]

        conn.commit()
        log.info("✓ Committed. cleaning_process: %d row(s), %d linked this run, "
                 "%d of %d carry a template | crude_oil_compatibility: %d row(s) "
                 "(%d compatible, %d not) | crude_oil_compatibility_exception: %d row(s)",
                 cp_total, linked, cp_linked, cp_total, c_total, c_yes, c_total - c_yes,
                 exc_total)
        return 0
    except Exception:
        conn.rollback()
        log.exception("Import failed - rolled back, nothing written.")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
