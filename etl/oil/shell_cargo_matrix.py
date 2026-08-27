#!/usr/bin/env python3
"""
Load the Shell cargo-to-cargo regime matrix into cleaning_process (cargo_type OIL).

Source: "Shell Tank Cleaning Guide 2016.pdf" (source.json, category 'oil') - the
same source row as shell_cargo_master.py and shell_procedure_templates.py, which
load the other two extracts of that one document.

FILE LAYOUT
-----------
    discharged_id | discharged_un | discharged_name
    to_load_id    | to_load_un    | to_load_name
    regime_code

784 rows = a complete 28 x 28 matrix, including the 28 same-cargo cells (loading
a product back onto itself still calls for a regime).

Mapping:

    discharged_name -> crude_oil.aggregated_name -> cleaning_process.from_cargo_id
    to_load_name    -> crude_oil.aggregated_name -> cleaning_process.to_cargo_id
    regime_code     -> cleaning_process.procedure_code ("1".."30")
                       and, resolved after insert, procedure_template_id

cargo_type is OIL for every row, so from_cargo_id / to_cargo_id are crude_oil ids.
Those columns carry NO foreign key - see the cleaning_process docstring in
schema.prisma - so the ids here are validated by the cleaning_process_cargo_refs
trigger on the way in, not by a constraint.

*_un and *_id are read for reporting only. The ids are the guide's own row
numbers, not database ids, and are deliberately not stored: nothing else in the
schema keys on them and keeping them would invite a join that means nothing.

JOINING ON aggregated_name
--------------------------
The matrix names products by their AGGREGATED name ("Jet A1, AVTUR, Aviation
Fuel"), not by the Cargo Master's Matrix Title ("Jet A1"), which is why
crude_oil.aggregated_name exists and why shell_cargo_master.py must run first.
25 of the 28 matrix cargoes join on that column exactly. The other three need a
decision, and all three are recorded here rather than resolved by guesswork:

  1. FAN-OUT. 'PyGas, Pygas-tail, Pyrolysis Gasoline' is one matrix row but two
     Cargo Master products (Pygas, Pygas-tail) sharing that aggregated name. The
     guide states one regime for the pair, so it applies to each: the row is
     expanded to every combination and each expansion says so in `notes`. This
     is the only reason the loader writes more rows than the file has.

  2. MISSING MASTERS. 'Toluene' and the black-oil catch-all appear in the matrix
     but in no Cargo Master row, so there is nothing to join to. They are created
     in crude_oil (see MISSING_CARGOES) with no property values, because the
     Cargo Master sheet publishes none for them.

     Toluene is NOT folded into the sheet's combined 'Xylenes-C8 & Toluene' row.
     The matrix distinguishes them - Xylene->Toluene is regime 2 while either to
     itself is regime 1 - so merging would both lose that and collide on the
     (from, to, source, condition) unique key.

IDEMPOTENCY
-----------
Re-running upserts on the partial unique index
cleaning_process_pair_key (from_cargo_id, to_cargo_id, source_id,
COALESCE(condition,'')). One transaction; any error rolls the whole import back.

Usage:
    python3 etl/oil/shell_cargo_matrix.py
    python3 etl/oil/shell_cargo_matrix.py --dry-run
    python3 etl/oil/shell_cargo_matrix.py "/path/to/regime code.csv"
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
from psycopg2.extras import execute_values
from dotenv import load_dotenv

# Loaders are run as scripts (python3 etl/<branch>/<file>.py), so only their
# own directory is on sys.path. Add the shared helpers.
_ETL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ETL_ROOT / "common"))

from _crude_oil import clean_text  # noqa: E402
from _paths import input_file  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("shell_cargo_matrix")

SOURCE_NAME = "Shell Tank Cleaning Guide 2016.pdf"
DEFAULT_FILE = str(input_file("Shell Tank Cleaning Guide 2016 cargo to cargo regime code.csv"))
CARGO_TYPE = "OIL"
BATCH = 5000

COL_FROM = "discharged_name"
COL_TO = "to_load_name"
COL_CODE = "regime_code"
REQUIRED = [COL_FROM, COL_TO, COL_CODE]

# Matrix cargoes with no Cargo Master row, created here so the matrix can be
# loaded whole. Listed explicitly rather than derived: creating master rows is
# not something a loader should do silently, and this is the audit trail.
#
#   aggregated_name (verbatim from the matrix) -> oil_name to store
MISSING_CARGOES: Dict[str, str] = {
    "Toluene": "Toluene",
    "Black Oil's inc: Crude Oil, Fuel Oil, Dirty Condensate, Waxy Dist',": "Black Oils",
}


def norm(text: Optional[str]) -> Optional[str]:
    """Collapse whitespace so a stray double space cannot break the join."""
    s = clean_text(text)
    if s is None:
        return None
    return re.sub(r"\s+", " ", s.replace("\n", " ")).strip()


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


def load_aggregated_index(cur, source_id: int) -> Dict[str, List[int]]:
    """aggregated_name -> [crude_oil.id, ...] for this source.

    A list, not a single id: one aggregated name can legitimately cover several
    products (Pygas / Pygas-tail), and collapsing that to one id would silently
    drop the other product's cleaning rules.
    """
    cur.execute(
        "SELECT id, aggregated_name FROM crude_oil "
        " WHERE source_id = %s AND aggregated_name IS NOT NULL ORDER BY id",
        (source_id,),
    )
    index: Dict[str, List[int]] = {}
    for oil_id, name in cur.fetchall():
        index.setdefault(norm(name), []).append(oil_id)
    return index


def ensure_missing_cargoes(cur, source_id: int, needed: set) -> int:
    """Create the crude_oil rows for matrix cargoes the Cargo Master omits.

    Only for names actually referenced by this file, so a trimmed matrix does
    not create products it never mentions.
    """
    created = 0
    for aggregated, oil_name in MISSING_CARGOES.items():
        if norm(aggregated) not in needed:
            continue
        cur.execute(
            "SELECT id FROM crude_oil WHERE oil_name = %s AND source_id = %s",
            (oil_name, source_id),
        )
        if cur.fetchone():
            continue
        cur.execute(
            "INSERT INTO crude_oil (oil_name, source_id, country_of_origin, "
            "aggregated_name, created_at, updated_at) "
            "VALUES (%s, %s, NULL, %s, now(), now()) RETURNING id",
            (oil_name, source_id, aggregated),
        )
        log.warning("created crude_oil %r (id=%s) - in the matrix, not in the "
                    "Cargo Master sheet", oil_name, cur.fetchone()[0])
        created += 1
    return created


def read_matrix(path: Path) -> Tuple[pd.DataFrame, List[str]]:
    """Read the CSV and report anything in it this loader does not map."""
    df = pd.read_csv(path)

    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        sys.exit(f"Error: file is missing column(s): {', '.join(missing)}")

    # The export carries two trailing empty columns. One of them holds a single
    # stray value; it is reported, never guessed at.
    strays: List[str] = []
    for col in [c for c in df.columns if str(c).startswith("Unnamed")]:
        for i, val in df[col].items():
            if pd.notna(val):
                where = f"{df.at[i, COL_FROM]!r} -> {df.at[i, COL_TO]!r}"
                strays.append(f"row {i + 2}: {where} carries an unmapped value "
                              f"{val!r} in column {col!r}")
    return df, strays


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", default=DEFAULT_FILE)
    ap.add_argument("--dry-run", action="store_true", help="parse and report, write nothing")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.exists():
        sys.exit(f"Error: file not found: {path}")

    df, strays = read_matrix(path)
    log.info("%s: %d rows", path.name, len(df))
    for s in strays:
        log.warning("%s - kept in remarks, regime code unchanged", s)

    load_dotenv()
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            source_id = resolve_source(cur, SOURCE_NAME)
            log.info("Source id=%s (%r)", source_id, SOURCE_NAME)

            referenced = {norm(v) for v in df[COL_FROM]} | {norm(v) for v in df[COL_TO]}
            created_oils = ensure_missing_cargoes(cur, source_id, referenced)

            index = load_aggregated_index(cur, source_id)
            log.info("crude_oil: %d aggregated name(s) available%s", len(index),
                     f", {created_oils} created for this matrix" if created_oils else "")

            # Which regime codes this source actually defines. A cell naming a
            # code with no template still loads - procedure_code keeps the
            # printed value verbatim - but it is worth saying out loud.
            cur.execute("SELECT procedure_code FROM procedure_templates WHERE source_id = %s",
                        (source_id,))
            known_codes = {r[0] for r in cur.fetchall()}

            rows: List[tuple] = []
            skipped: Dict[str, int] = {}
            fanned = 0
            unknown_code = set()

            for i, r in df.iterrows():
                line = i + 2
                from_name, to_name = norm(r[COL_FROM]), norm(r[COL_TO])
                code = norm(r[COL_CODE])
                if code is None:
                    skipped["no regime code"] = skipped.get("no regime code", 0) + 1
                    log.warning("row %d: no %s - skipped", line, COL_CODE)
                    continue
                # "1" not "1.0": pandas reads an all-integer column as float.
                if re.fullmatch(r"\d+\.0", code):
                    code = code[:-2]
                if code not in known_codes:
                    unknown_code.add(code)

                from_ids, to_ids = index.get(from_name), index.get(to_name)
                for label, name, ids in ((COL_FROM, from_name, from_ids),
                                         (COL_TO, to_name, to_ids)):
                    if not ids:
                        key = f"{label} not in crude_oil.aggregated_name"
                        skipped[key] = skipped.get(key, 0) + 1
                        log.warning("row %d: %s %r has no crude_oil row - skipped",
                                    line, label, name)
                if not from_ids or not to_ids:
                    continue

                expanded = len(from_ids) * len(to_ids)
                if expanded > 1:
                    fanned += expanded - 1
                for fid in from_ids:
                    for tid in to_ids:
                        note = None
                        if expanded > 1:
                            note = (f"Matrix names one cargo where the Cargo Master has "
                                    f"several sharing that aggregated name; this is 1 of "
                                    f"{expanded} rows expanded from line {line}.")
                        remark = next((s for s in strays if s.startswith(f"row {line}:")), None)
                        rows.append((CARGO_TYPE, fid, tid, code, source_id, note, remark))

            log.info("Resolved %d matrix row(s) into %d cleaning_process row(s)"
                     "%s", len(df) - sum(skipped.values()), len(rows),
                     f" ({fanned} added by fan-out)" if fanned else "")
            if unknown_code:
                log.warning("regime code(s) with no procedure_templates row in this "
                            "source: %s", ", ".join(sorted(unknown_code)))
            for reason, n in sorted(skipped.items()):
                log.warning("skipped %d row(s): %s", n, reason)

            if args.dry_run:
                for t in rows[:5]:
                    log.info("  from=%-5s to=%-5s regime=%-3s", t[1], t[2], t[3])
                log.info("--dry-run: nothing written, rolling back.")
                conn.rollback()
                return 0

            for start in range(0, len(rows), BATCH):
                execute_values(
                    cur,
                    """
                    INSERT INTO cleaning_process
                        (cargo_type, from_cargo_id, to_cargo_id, procedure_code,
                         source_id, notes, remarks, created_at, updated_at)
                    VALUES %s
                    ON CONFLICT (from_cargo_id, to_cargo_id, source_id,
                                 COALESCE(condition, ''))
                        WHERE from_cargo_id IS NOT NULL AND to_cargo_id IS NOT NULL
                    DO UPDATE SET cargo_type     = EXCLUDED.cargo_type,
                                  procedure_code = EXCLUDED.procedure_code,
                                  notes          = EXCLUDED.notes,
                                  remarks        = EXCLUDED.remarks,
                                  updated_at     = now()
                    """,
                    rows[start:start + BATCH],
                    template="(%s::\"CargoType\",%s,%s,%s,%s,%s,%s,now(),now())",
                    page_size=BATCH,
                )
                log.info("  upserted %d / %d", min(start + BATCH, len(rows)), len(rows))

            # Link each row to its template. procedure_code is kept verbatim
            # either way, so a code the legend never defines simply stays
            # unlinked rather than blocking the import.
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
                 "%d linked to a template this run, %d of %d carry one | "
                 "crude_oil created: %d",
                 source_id, total, linked, with_template, total, created_oils)
        return 0
    except Exception:
        conn.rollback()
        log.exception("Import failed - rolled back, nothing written.")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
