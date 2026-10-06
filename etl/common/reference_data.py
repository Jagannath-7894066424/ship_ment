#!/usr/bin/env python3
"""
Load a source's numbered references (clauses, tables, paragraphs) into
reference_data.

WHY THIS LOADER IS A REGISTRY AND NOT ONE SCRIPT PER FILE
---------------------------------------------------------
Every other loader in this project is written for one file, because each file
has a different shape and the shape is most of the work. This one is not: a
reference extract is always the same four things - a number, a sub-number, the
text beside it, and a cross-reference - whatever document it came from. What
differs between files is only WHICH source published it and WHICH category it
belongs to, so those two facts are data, in DATASETS below, and adding a second
extract is adding an entry rather than copying 300 lines.

Each dataset names a file, a source and a category. Add one there; no code
changes.

THE TWO NUMBER COLUMNS
----------------------
    Reference number       the parent clause    "4.23"
    subreference number    the full clause      "4.23.1.1"

The second always begins with the first - checked on every row, for all 895
rows of the IGC extract - so they are a parent/child pair and not two unrelated
numbering schemes. reference_no keeps the parent so "everything under 4.23" is
one indexed lookup; sub_reference_no keeps the precise clause.

"sort key" IS DROPPED, AND VERIFIED BEFORE IT IS
------------------------------------------------
The file carries a fifth column, a zero-padded key ("004023001001") that exists
so a spreadsheet sorts 4.23 after 4.9 instead of before it. It holds nothing
the sub-reference does not: every one of its values is exactly the
sub-reference with each dotted group padded to three digits, which this loader
recomputes and compares row by row rather than assuming. A row where the two
disagree stops the load, because that would mean the column had come to mean
something else and dropping it would then be losing data.

Ordering in the database is done by recomputing the same key in the query, so
storing it would be storing a derived value that could drift from the number it
is derived from.

"Additional info" IS A CROSS-REFERENCE COLUMN
----------------------------------------------
228 of the 895 rows fill it, and almost all are IGC paragraph numbers the
clause points at - "1.2.43", "4.21 to 4.26", "ch 10", "table 6.5". It is the
source pointing at itself, which is what additional_reference_no is for, and it
is stored verbatim: "4.3.4.3/4.15" is two references in the source's own
notation and splitting them would be a decision about syntax this file does not
justify.

Values with no digits at all cannot be references. There are two, and neither
is dropped - a value that is not a reference is still what the source's working
sheet said, and deleting it would hide a spreadsheet error rather than surface
it. They are listed on every run so they stay visible.

DUPLICATE CLAUSE NUMBERS
------------------------
The IGC extract is a working spreadsheet and repeats some clause numbers. Two
kinds, handled differently:

  * 31 rows are identical to another row in all four columns. They are the same
    statement entered twice and only one is kept.
  * 27 clause numbers carry rows that DIFFER - usually one has a cross-
    reference the other lacks, sometimes one keeps a heading the other dropped
    ("Plastic deformation-For type C independent tanks..."). Both are kept and
    both are listed on every run. Picking one would be choosing between two
    readings of the source on no evidence; reference_data has no unique key
    precisely so that this table can hold what the source actually contains.

IDEMPOTENCY
-----------
reference_data has no natural unique key (see the migration for why), so there
is nothing to upsert on. A run therefore DELETES the rows for its own
(source_id, category) and reloads them. The delete is scoped to both, not to
the source alone, so two datasets that share a source but differ in category do
not erase each other - which is the whole reason the registry pairs them.

The file is fully validated before anything is written, and the delete and the
insert are one transaction.

Usage:
    python3 etl/common/reference_data.py                    # default dataset
    python3 etl/common/reference_data.py --dataset igc-oil
    python3 etl/common/reference_data.py --list
    python3 etl/common/reference_data.py --dry-run
    python3 etl/common/reference_data.py <file>             # override the path
"""

import argparse
import csv
import logging
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import psycopg2
from dotenv import load_dotenv

# Loaders are run as scripts, so only their own directory is on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import input_file  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("reference_data")

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Dataset:
    """One reference extract: which file, whose document, which category.

    `source` is matched against source.name, so it must be a name that
    etl/data/source.json already registers - the loader does not invent
    sources. `category` is this project's own grouping for the extract and is
    stored on every row; together the two scope the reload.
    """
    file: str
    source: str
    category: str
    description: str


# Add an extract by adding an entry. Nothing else needs to change.
DATASETS: Dict[str, Dataset] = {
    "igc-oil": Dataset(
        file="IGC Code 2016 edition-working - reference_data_with_no.csv",
        # The IGC Code, 2016 edition. source.json registers the document under
        # the name its first extract was loaded for; it is the same document.
        source="IGC Code 2016 Cargo Gas",
        category="oil",
        description="IGC Code 2016 clause text, chapters 1-19",
    ),
}
DEFAULT_DATASET = "igc-oil"

# The header exactly as the file prints it -> the column it becomes. Matched on
# the header text, never on position, so a re-export that reorders the columns
# stops the loader instead of filing clause text as a cross-reference.
REF_NO = "Reference number"
SUB_REF_NO = "subreference number"
DESCRIPTION = "description"
ADDITIONAL = "Additional info"
SORT_KEY = "sort key"
EXPECTED_HEADER = [REF_NO, SUB_REF_NO, DESCRIPTION, ADDITIONAL, SORT_KEY]

DOTTED = re.compile(r"^\d+(?:\.\d+)*$")


def clean(text: Optional[str]) -> str:
    """Trim and collapse whitespace, including the non-breaking spaces the
    export leaves in pasted clause text."""
    return re.sub(r"\s+", " ", (text or "").replace(" ", " ")).strip()


def sort_key_for(sub_ref: str) -> Optional[str]:
    """The zero-padded key the file's 5th column holds, recomputed.

    None when the sub-reference is not purely dotted digits, which is the only
    case where the file's own key could legitimately differ.
    """
    if not DOTTED.match(sub_ref):
        return None
    return "".join(f"{int(part):03d}" for part in sub_ref.split("."))


def read_file(path: Path) -> Tuple[List[dict], List[str], List[str]]:
    """Parse and validate the CSV.

    Returns (rows, errors, warnings). Nothing is written unless errors is
    empty; warnings are things a human should see but that do not make the
    file unloadable.
    """
    errors: List[str] = []
    warnings: List[str] = []

    with path.open(newline="", encoding="utf-8-sig") as fh:
        raw = list(csv.reader(fh))
    if not raw:
        return [], [f"{path.name} is empty"], []

    header = [clean(c) for c in raw[0]]
    # The export pads every row with empty columns; they carry nothing.
    while header and not header[-1]:
        header.pop()
    if header != EXPECTED_HEADER:
        errors.append(f"unexpected header.\n        found    {header!r}\n"
                      f"        expected {EXPECTED_HEADER!r}")
        return [], errors, warnings

    rows: List[dict] = []
    key_stats = {"exact": 0, "zeros lost": 0, "absent": 0, "unusable": 0}
    for line_no, raw_row in enumerate(raw[1:], start=2):
        if not any(clean(c) for c in raw_row):
            continue
        cells = list(raw_row)
        ref, sub_ref = clean(cells[0]), clean(cells[1] if len(cells) > 1 else "")
        desc = clean(cells[2] if len(cells) > 2 else "")

        # Clause text containing commas was split into extra cells by the
        # spreadsheet; each fragment keeps the space that followed its comma.
        # A real cross-reference has no space at either edge, so edge
        # whitespace marks a fragment: it is rejoined with the comma it was split on.
        i = 3
        while i < len(cells) and clean(cells[i]) and cells[i] != cells[i].strip():
            desc += ("," if cells[i][:1].isspace() else ", ") + cells[i]
            i += 1
        desc = clean(desc)
        if i > 3:
            warnings.append(f"line {line_no}: clause {sub_ref} text was split on "
                            f"commas across {i - 2} cells; rejoined")

        # What remains is the cross-reference and the sort key, in either
        # order and at either column (rows were shifted by a stray cell). The
        # key is all digits, so it is told apart from a reference by that.
        additional, file_key = "", ""
        for c in cells[i:]:
            c = clean(c)
            if not c:
                continue
            if c == DESCRIPTION:
                warnings.append(f"line {line_no}: clause {sub_ref} has the stray "
                                f"label {c!r} in a data column; ignored")
            elif re.fullmatch(r"\d{5,}|[\d.]+E\+\d+", c):
                file_key = c
            elif not additional:
                additional = c
            else:
                warnings.append(f"line {line_no}: clause {sub_ref} has extra cell "
                                f"{c!r}; ignored")

        if not ref:
            errors.append(f"line {line_no}: no {REF_NO!r}")
            continue
        if not sub_ref:
            errors.append(f"line {line_no}: no {SUB_REF_NO!r} (ref {ref})")
            continue
        # The parent/child claim this loader's column mapping rests on. If it
        # ever fails, the two columns are not what the header says they are.
        if not sub_ref.startswith(ref):
            errors.append(f"line {line_no}: sub-reference {sub_ref!r} does not "
                          f"begin with its reference {ref!r}")
            continue
        # The file's sort key is not stored (ordering recomputes it), and the
        # export no longer keeps it reliable: leading zeros are lost, large keys
        # were turned into floats, and the newest rows have none. It is counted,
        # not trusted - the key is derived from the sub-reference itself.
        computed = sort_key_for(sub_ref)
        if not file_key:
            key_stats["absent"] += 1
        elif file_key == computed:
            key_stats["exact"] += 1
        elif computed is not None and file_key.isdigit() \
                and file_key.zfill(len(computed)) == computed:
            key_stats["zeros lost"] += 1
        else:
            key_stats["unusable"] += 1
        if not desc:
            warnings.append(f"line {line_no}: clause {sub_ref} has no text")

        if additional and not re.search(r"\d", additional):
            warnings.append(f"line {line_no}: clause {sub_ref} gives "
                            f"{additional!r} as a cross-reference, which holds "
                            f"no number; stored as printed")

        rows.append({"line": line_no, "reference_no": ref,
                     "sub_reference_no": sub_ref,
                     "description": desc or None,
                     "additional_reference_no": additional or None})

    if not rows and not errors:
        errors.append(f"{path.name} has a header but no data rows")
    warnings.append(f"sort key column (ignored, recomputed): {key_stats}")
    return rows, errors, warnings


def dedupe(rows: List[dict]) -> Tuple[List[dict], int, Dict[str, List[dict]]]:
    """Drop rows identical to an earlier one; keep rows that merely share a
    clause number.

    Returns (rows, identical_dropped, conflicting) - see the module header for
    why the second kind is kept.
    """
    seen = set()
    kept: List[dict] = []
    dropped = 0
    for row in rows:
        fingerprint = (row["reference_no"], row["sub_reference_no"],
                       row["description"], row["additional_reference_no"])
        if fingerprint in seen:
            dropped += 1
            continue
        seen.add(fingerprint)
        kept.append(row)

    by_sub: Dict[str, List[dict]] = defaultdict(list)
    for row in kept:
        by_sub[row["sub_reference_no"]].append(row)
    conflicting = {k: v for k, v in by_sub.items() if len(v) > 1}
    return kept, dropped, conflicting


def report(rows: List[dict], dropped: int, conflicting: Dict[str, List[dict]],
           warnings: List[str]) -> None:
    log.info("%d row(s) to load (%d identical duplicate(s) dropped)",
             len(rows), dropped)

    chapters = Counter(r["reference_no"].split(".")[0] for r in rows)
    log.info("    chapters: %s", ", ".join(
        f"{ch}({n})" for ch, n in sorted(
            chapters.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 999)))
    log.info("    %d row(s) carry a cross-reference",
             sum(1 for r in rows if r["additional_reference_no"]))
    log.info("    %d distinct parent reference(s)",
             len({r["reference_no"] for r in rows}))

    if conflicting:
        log.info("    %d clause number(s) keep MORE THAN ONE row, because the "
                 "rows differ. Both are loaded:", len(conflicting))
        for sub_ref in sorted(conflicting)[:10]:
            log.info("        %s", sub_ref)
            for row in conflicting[sub_ref]:
                log.info("            line %-5s xref=%-22s %s", row["line"],
                         row["additional_reference_no"] or "-",
                         (row["description"] or "")[:60])
        if len(conflicting) > 10:
            log.info("        ... and %d more", len(conflicting) - 10)

    for warning in warnings:
        log.warning("    %s", warning)


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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?",
                    help="override the dataset's own file path")
    ap.add_argument("--dataset", default=DEFAULT_DATASET, choices=sorted(DATASETS),
                    help=f"which extract to load (default: {DEFAULT_DATASET})")
    ap.add_argument("--list", action="store_true",
                    help="list the registered datasets and exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="parse and report, write nothing")
    args = ap.parse_args()

    if args.list:
        for key, ds in sorted(DATASETS.items()):
            print(f"{key}\n    file     : {ds.file}\n    source   : {ds.source}"
                  f"\n    category : {ds.category}\n    {ds.description}")
        return 0

    dataset = DATASETS[args.dataset]
    path = Path(args.file) if args.file else input_file(dataset.file)
    if not path.is_file():
        sys.exit(f"Error: file not found: {path}")

    log.info("dataset %r - source %r, category %r",
             args.dataset, dataset.source, dataset.category)
    log.info("  file: %s", path)

    rows, errors, warnings = read_file(path)
    if errors:
        log.error("%d validation error(s) - nothing was imported:", len(errors))
        for e in errors:
            log.error("    %s", e)
        return 1

    rows, dropped, conflicting = dedupe(rows)
    report(rows, dropped, conflicting, warnings)

    if args.dry_run:
        log.info("--dry-run: nothing written.")
        return 0

    load_dotenv(REPO_ROOT / ".env")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        sys.exit("Error: DATABASE_URL not set (checked .env).")

    conn = psycopg2.connect(db_url)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            source_id = resolve_source(cur, dataset.source)
            log.info("Source id=%s (%r)", source_id, dataset.source)

            # No unique key to upsert on, so a rerun replaces its own scope.
            # Scoped to category as well as source: another dataset may share
            # this source and must not be erased by this one.
            cur.execute("DELETE FROM reference_data "
                        "WHERE source_id = %s AND category = %s",
                        (source_id, dataset.category))
            log.info("removed %d row(s) from a previous run", cur.rowcount)

            cur.executemany(
                """
                INSERT INTO reference_data
                    (reference_no, sub_reference_no, description,
                     additional_reference_no, source_id, category,
                     created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, now(), now())
                """,
                [(r["reference_no"], r["sub_reference_no"], r["description"],
                  r["additional_reference_no"], source_id, dataset.category)
                 for r in rows],
            )

        conn.commit()
        log.info("✓ Committed. reference_data: %d row(s) for source %s "
                 "category %r", len(rows), source_id, dataset.category)
        return 0
    except Exception:
        conn.rollback()
        log.exception("Import failed - rolled back, nothing written.")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
