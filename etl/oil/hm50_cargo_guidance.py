#!/usr/bin/env python3
"""
Load the HM 50 per-cargo guidance into crude_oil_property_values
(field_name = HM50_CARGO_GUIDANCE).

SOURCE
------
"HM Tank Cleaning Guide By Energy Institute" (source.json, category 'oil') - the
same source row as hm50_procedure_templates.py and hm50_cargo_matrix.py, which
MUST run first: this loader attaches text to the crude_oil rows the matrix
loader creates.

INPUT
-----
"... - cargo guidance.xlsx": 19 rows of
    Cargo / cargo family | HM 50 individual-cargo guidance (structured summary)

WHY A PROPERTY VALUE
--------------------
crude_oil has no notes column, and this is per-cargo text that belongs to ONE
source - exactly what the property-value table is for. Storing it there also
means a second guide's opinion of the same cargo lands beside this one instead
of overwriting it (source_id is part of the key).

    value       the guidance, verbatim
    value_type  'text'
    notes       says whether the guide wrote it for this cargo alone or for a
                family the matrix splits into several cargoes

THE NAMES DO NOT MATCH
----------------------
The guidance sheet names 19 cargo FAMILIES; the matrix names 23 cargoes, and
they are not the same vocabulary - "Premium and regular kerosenes" against
"Kerosene (un-dyed)" and "Kerosene (dyed)". FAMILY_TO_CARGOES below is that
mapping, written out by hand. It is deliberately conservative:

  - a family maps to SEVERAL cargoes only where its own name is plural and the
    matrix splits it (kerosenes -> dyed + un-dyed; FAME -> B5 / B15 / >B15);
  - a family whose subject the matrix never names maps to [] and gets a
    crude_oil row of its own, so its guidance is stored rather than dropped;
  - a family missing from the table is a HARD ERROR. Fuzzy-matching cargo names
    is how "Aviation gasoline" guidance ends up filed under "Aviation turbine
    gasoline", which is a different product.

Matrix cargoes with no guidance are reported, never invented.

IDEMPOTENCY
-----------
Upsert on (crude_oil_id, source_id, field_name). One transaction.

Usage:
    python3 etl/oil/hm50_cargo_guidance.py
    python3 etl/oil/hm50_cargo_guidance.py --dry-run
    python3 etl/oil/hm50_cargo_guidance.py "/path/to/cargo guidance.xlsx"
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
from dotenv import load_dotenv

# Loaders are run as scripts (python3 etl/<branch>/<file>.py), so only their
# own directory is on sys.path. Add the shared helpers.
_ETL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ETL_ROOT / "common"))

from _crude_oil import Parsed, ensure_field_definitions, upsert_property  # noqa: E402
from _paths import input_file  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("hm50_guidance")

SOURCE_NAME = "HM Tank Cleaning Guide By Energy Institute"
FIELD_NAME = "HM50_CARGO_GUIDANCE"
ENTERED_BY = "hm50_cargo_guidance.py"
DEFAULT_FILE = input_file(
    "HM Tank Cleaning Guide By Energy Institute - cargo guidance.xlsx")

COL_FAMILY = "Cargo / cargo family"
COL_TEXT = "HM 50 individual-cargo guidance (structured summary)"

# guidance family (verbatim) -> matrix cargo name(s) (verbatim).
# [] means the matrix names no such cargo; the family becomes a crude_oil row
# under its own name so the text is kept.
FAMILY_TO_CARGOES: Dict[str, List[str]] = {
    "Naphthas and light distillate feedstocks": ["Naphtha (lead free)"],
    "Aviation gasoline":                        ["Aviation gasoline"],
    "Leaded motor gasoline":                    ["Motor gasoline (leaded)"],
    "Unleaded motor gasoline":                  ["Motor gasoline (unleaded)"],
    "Ultra low sulfur gasolines":               ["Ultra low sulfur motor gasoline (unleaded)"],
    "Solvents / white spirit":                  ["Solvents / White spirit"],
    "Aviation jet fuel":                        ["Aviation jet fuel and components"],
    "Premium and regular kerosenes":            ["Kerosene (un-dyed)", "Kerosene (dyed)"],
    "Gas oil and automotive diesel fuel":       ["Gas oil (un-dyed)", "Gas oil (dyed)"],
    "Ultra low sulfur automotive diesel fuel":  ["Ultra low sulfur gas oil/diesel"],
    "Crude oil and condensate":                 ["Crude oil and condensate"],
    "Base lubricating oils":                    ["Base lubricating oil"],
    "Vacuum gas oil":                           ["Vacuum gas oil"],
    "Medium and heavy fuel oils":               ["Fuel oil (sulfur >1%)"],
    "Low sulfur fuel oil":                      ["Low sulfur fuel oil (sulfur <1%)"],
    "FAME and blended biodiesel": [
        "Diesel blended with up to 5% FAME (B5 or lower)",
        "Diesel blended with 5% to 15% FAME (B15 or lower)",
        "FAME or diesel/gas oil blended >15% FAME (B15 or higher)",
    ],
    # Named by the guidance sheet, by no matrix column.
    "Light fuel oil":       [],
    "GTL products":         [],
    "Light cycle oil (LCO)": [],
}


def clean(value) -> Optional[str]:
    if value is None:
        return None
    s = re.sub(r"[ \t]+", " ", str(value).replace("\n", " ")).strip()
    return s or None


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


def read_guidance(path: Path) -> Tuple[List[Tuple[str, str]], List[str]]:
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.worksheets[0]
    grid = [list(r) for r in ws.iter_rows(values_only=True)]
    errors: List[str] = []

    if not grid:
        return [], [f"{path.name} is empty"]

    header = [clean(c) for c in grid[0][:2]]
    if header[0] != COL_FAMILY or header[1] != COL_TEXT:
        errors.append(f"row 1 is {header!r}, expected [{COL_FAMILY!r}, {COL_TEXT!r}]")

    rows: List[Tuple[str, str]] = []
    seen: set = set()
    for i, raw in enumerate(grid[1:], start=2):
        family, text = clean(raw[0]), clean(raw[1] if len(raw) > 1 else None)
        if family is None and text is None:
            continue
        if family is None:
            errors.append(f"row {i}: guidance text with no cargo family")
            continue
        if text is None:
            errors.append(f"row {i}: family {family!r} has no guidance text")
            continue
        if family in seen:
            errors.append(f"row {i}: duplicate family {family!r}")
            continue
        seen.add(family)
        if family not in FAMILY_TO_CARGOES:
            errors.append(
                f"row {i}: family {family!r} is not in FAMILY_TO_CARGOES - map it "
                f"to its matrix cargo name(s) in etl/oil/hm50_cargo_guidance.py")
            continue
        rows.append((family, text))
    return rows, errors


def cargo_ids(cur, source_id: int) -> Dict[str, int]:
    cur.execute("SELECT oil_name, id FROM crude_oil WHERE source_id = %s", (source_id,))
    return dict(cur.fetchall())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", default=str(DEFAULT_FILE))
    ap.add_argument("--dry-run", action="store_true", help="parse and report, write nothing")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.is_file():
        sys.exit(f"Error: file not found: {path}")

    rows, errors = read_guidance(path)
    if errors:
        log.error("%d validation error(s) - nothing was imported:", len(errors))
        for e in errors:
            log.error("    %s", e)
        return 1
    log.info("%s: %d cargo family/families", path.name, len(rows))

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

            added = ensure_field_definitions(cur, only=[FIELD_NAME])
            log.info("field_definitions: %s", f"{FIELD_NAME} created" if added
                     else f"{FIELD_NAME} already present")

            ids = cargo_ids(cur, source_id)
            if not ids:
                sys.exit("Error: this source has no crude_oil rows.\n"
                         "  Run etl/oil/hm50_cargo_matrix.py first.")

            written = 0
            created_cargoes: List[str] = []
            targeted: set = set()

            for family, text in rows:
                # ONE property row per line of the sheet. Where the matrix splits
                # a family into several cargoes the text is stored against the
                # FIRST of them only, not copied onto each: the sheet states the
                # guidance once, so the database holds it once. The family's own
                # wording and the cargoes it covers are recorded in the row's
                # notes, so nothing the sheet says is lost.
                all_targets = FAMILY_TO_CARGOES[family] or [family]
                targets = all_targets[:1]
                own_row = not FAMILY_TO_CARGOES[family]

                for name in targets:
                    oil_id = ids.get(name)
                    if oil_id is None:
                        if not own_row:
                            # The mapping names a matrix cargo that is not in the
                            # database: the two files disagree, so stop.
                            raise SystemExit(
                                f"Error: family {family!r} maps to cargo {name!r}, "
                                f"which has no crude_oil row under this source. "
                                f"Fix FAMILY_TO_CARGOES or re-run the matrix loader.")
                        cur.execute(
                            "INSERT INTO crude_oil (oil_name, source_id, created_at, "
                            "updated_at) VALUES (%s, %s, now(), now()) RETURNING id",
                            (name, source_id),
                        )
                        oil_id = cur.fetchone()[0]
                        ids[name] = oil_id
                        created_cargoes.append(name)

                    if own_row:
                        note = ("HM 50 states this guidance for a cargo the "
                                "cargo-to-cargo matrix does not list, so this row "
                                "carries the guidance but no cleaning transitions.")
                    elif len(all_targets) > 1:
                        note = (f"HM 50 states this guidance for the family "
                                f"{family!r}, which the matrix splits into "
                                f"{len(all_targets)} cargoes: "
                                f"{', '.join(repr(t) for t in all_targets)}. The "
                                f"sheet states it once, so it is stored once, "
                                f"here on the first of them; it applies to all.")
                    else:
                        note = f"HM 50 states this guidance for {family!r}."

                    if not args.dry_run:
                        upsert_property(
                            cur, oil_id, source_id, FIELD_NAME,
                            Parsed(value=text, normalized_value=None,
                                   normalized_min=None, normalized_max=None,
                                   unit=None, value_type="text", notes=note),
                            entered_by=ENTERED_BY,
                        )
                    written += 1
                    targeted.add(name)

            uncovered = sorted(set(ids) - targeted)
            if uncovered:
                log.warning("%d cargo(es) have no guidance text in this sheet: %s",
                            len(uncovered), "; ".join(uncovered))
            if created_cargoes:
                log.warning("created %d crude_oil row(s) for families the matrix "
                            "does not name: %s", len(created_cargoes),
                            "; ".join(created_cargoes))

            if args.dry_run:
                log.info("--dry-run: %d value(s) prepared, nothing written, rolling back.",
                         written)
                conn.rollback()
                return 0

        conn.commit()
        log.info("✓ Committed. crude_oil_property_values (%s, source %s): %d row(s) "
                 "from %d family/families", FIELD_NAME, source_id, written, len(rows))
        return 0
    except SystemExit:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        log.exception("Import failed - rolled back, nothing written.")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
