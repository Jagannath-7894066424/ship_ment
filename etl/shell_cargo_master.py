#!/usr/bin/env python3
"""
Load the 'Cargo Master' sheet of "Shell Tank Cleanning Data.xlsx" into
crude_oil + crude_oil_property_values + synonyms.

Source: "Shell Tank Cleanning Data" (source.json, category 'oil').

SHEET LAYOUT
------------
    UN No | Matrix Title | Generic Product | Grade Names
          | Flash Point °C | Density kg/m3 | Sulphur ppm | Main Characteristics

Mapping:

    Matrix Title          -> crude_oil.oil_name          (the identity)
    Grade Names           -> synonyms + crude_oil_synonym
    UN No                 -> property UN_NUMBER
    Generic Product       -> property GENERIC_PRODUCT
    Flash Point °C        -> property FLASH_POINT           (°C)
    Density kg/m3         -> property DENSITY               (kg/m3)
    Sulphur ppm           -> property SULFUR                (ppm)
    Main Characteristics  -> property MAIN_CHARACTERISTICS

Nothing but the oil name lands as a column on crude_oil; every measured or
descriptive quantity goes to crude_oil_property_values, which is what that table
is for. country_of_origin stays NULL - the sheet does not give one, and guessing
one from a grade name would be fabrication.

A NOTE ON WHAT THESE ARE
------------------------
These are refined products (ULSD, Jet A1, AVGAS, MTBE) and petrochemicals, not
crude assays, so crude_oil is being used as a general oil-cargo master here. That
is a deliberate instruction, not an oversight; recording it because a reader
comparing this source against the two crude-assay sources will notice that
'API' and 'Pour Point' are absent and 'Flash Point' is not.

QUALIFIED VALUES
----------------
This source almost never gives a plain number. It gives bounds and ranges:

    "above 56"      -> normalized_min = 56
    "below -20"     -> normalized_max = -20
    "38 to 60"      -> normalized_min = 38,  normalized_max = 60
    "2,000 max"     -> normalized_max = 2000
    "Below 10"      -> normalized_max = 10
    "1 or 10 max"   -> stored as text, NOT normalized - the source is genuinely
                       ambiguous and picking one would invent a figure

The printed wording is always kept verbatim in `value`; the bounds are a reading
of it, never a replacement for it.

IDEMPOTENCY
-----------
Re-running upserts. Keys: (oil_name, source_id) for the oil,
(crude_oil_id, source_id, field_name) for a property, (crude_oil_id, synonym_id)
for a name link. One transaction; any error rolls the whole import back.

Usage:
    python3 etl/shell_cargo_master.py
    python3 etl/shell_cargo_master.py --dry-run
    python3 etl/shell_cargo_master.py "/path/to/Shell Tank Cleanning Data.xlsx"
"""

import argparse
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import psycopg2
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _crude_oil import (  # noqa: E402
    Parsed,
    clean_text,
    ensure_field_definitions,
    fmt_num,
    upsert_crude_oil,
    upsert_property,
)
from _paths import input_file  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("shell_cargo_master")

SOURCE_NAME = "Shell Tank Cleanning Data"
ENTERED_BY = "shell_cargo_master.py"
DEFAULT_FILE = str(input_file("Shell Tank Cleanning Data.xlsx"))
SHEET = "Cargo Master"

COL_NAME = "Matrix Title"
COL_GRADES = "Grade Names"

# sheet column -> (field_name, unit, is_free_text)
PROPERTIES: List[Tuple[str, str, Optional[str], bool]] = [
    ("UN No",                "UN_NUMBER",            None,     True),
    ("Generic Product",      "GENERIC_PRODUCT",      None,     True),
    ("Flash Point °C",       "FLASH_POINT",          "°C",     False),
    ("Density kg/m3",        "DENSITY",              "kg/m3",  False),
    ("Sulphur ppm",          "SULFUR",               "ppm",    False),
    ("Main Characteristics", "MAIN_CHARACTERISTICS", None,     True),
]
FIELDS = [p[1] for p in PROPERTIES]

RELATIONSHIP_TYPE = "grade_name"

# ---------------------------------------------------------------------------
# Value parsing - the qualified forms this source actually uses
# ---------------------------------------------------------------------------
_NUM = r"[+-]?[\d,]*\.?\d+"
_ABOVE_RE = re.compile(rf"^(?:above|over|greater than|min(?:imum)?)\s*({_NUM})$", re.I)
_BELOW_RE = re.compile(rf"^(?:below|under|less than|up to)\s*({_NUM})$", re.I)
_MAX_RE = re.compile(rf"^({_NUM})\s*max(?:imum)?$", re.I)
_MIN_RE = re.compile(rf"^({_NUM})\s*min(?:imum)?$", re.I)
_RANGE_RE = re.compile(rf"^({_NUM})\s*(?:to|-|–|—)\s*({_NUM})$", re.I)
_APPROX_RE = re.compile(rf"^(?:circa|approx(?:\.|imately)?|about|~)\s*({_NUM})$", re.I)
_PLAIN_RE = re.compile(rf"^({_NUM})$")


def _num(s: str) -> float:
    """'2,000' -> 2000.0. The sheet uses thousands separators."""
    return float(s.replace(",", ""))


def parse_identifier(raw: Any) -> Optional[Parsed]:
    """Store a cell as text, without ever reading a number out of it.

    For identifiers and prose. A UN number looks numeric ("1202") but is a label,
    not a quantity: normalising it would invite "un_number > 1200", and half the
    rows cite several at once ("1223/1202", "2398 & 1149") anyway.
    """
    s = clean_text(raw)
    if s is None:
        return None
    return Parsed(value=re.sub(r"\s+", " ", s.replace("\n", " ")).strip(),
                  normalized_value=None, normalized_min=None, normalized_max=None,
                  unit=None, value_type="text", notes=None)


def parse_qualified(raw: Any, unit: Optional[str]) -> Optional[Parsed]:
    """Parse one Shell cell into value-column form, or None when empty.

    Anything not recognised is kept as TEXT with its wording intact rather than
    coerced. A figure this loader cannot read is a figure it must not invent.
    """
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        if pd.isna(raw):
            return None
        return Parsed(value=fmt_num(float(raw)), normalized_value=float(raw),
                      normalized_min=None, normalized_max=None, unit=unit,
                      value_type="number", notes=None)

    s = clean_text(raw)
    if s is None:
        return None
    s = s.replace("\n", " ").strip()
    # "below zero" is a bound like any other; the source just spells the number.
    s = re.sub(r"\bzero\b", "0", s, flags=re.I)

    m = _PLAIN_RE.match(s)
    if m:
        return Parsed(value=s, normalized_value=_num(m.group(1)), normalized_min=None,
                      normalized_max=None, unit=unit, value_type="number", notes=None)

    m = _RANGE_RE.match(s)
    if m:
        return Parsed(value=s, normalized_value=None, normalized_min=_num(m.group(1)),
                      normalized_max=_num(m.group(2)), unit=unit, value_type="range",
                      notes=None)

    m = _ABOVE_RE.match(s) or _MIN_RE.match(s)
    if m:
        return Parsed(value=s, normalized_value=None, normalized_min=_num(m.group(1)),
                      normalized_max=None, unit=unit, value_type="range",
                      notes="Source gives a lower bound only; normalized_max is unbounded.")

    m = _BELOW_RE.match(s) or _MAX_RE.match(s)
    if m:
        return Parsed(value=s, normalized_value=None, normalized_min=None,
                      normalized_max=_num(m.group(1)), unit=unit, value_type="range",
                      notes="Source gives an upper bound only; normalized_min is unbounded.")

    # "circa 880" - one figure, hedged. Normalised so it is comparable, with the
    # hedge recorded so nobody mistakes it for a measured value.
    m = _APPROX_RE.match(s)
    if m:
        return Parsed(value=s, normalized_value=_num(m.group(1)), normalized_min=None,
                      normalized_max=None, unit=unit, value_type="number",
                      notes="Source qualifies this as approximate.")

    # e.g. "1 or 10 max" - two candidate limits, and the sheet does not say which
    # applies. Kept verbatim with no bounds; picking one would invent a figure.
    return Parsed(value=s, normalized_value=None, normalized_min=None,
                  normalized_max=None, unit=None, value_type="text",
                  notes="Not reduced to a bound: the source wording is not a single "
                        "comparable figure." if unit else None)


def normalize_synonym(text: str) -> str:
    """Match master_loader.normalize_synonym so both branches key names alike."""
    s = text.lower()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def split_grade_names(raw: Any) -> List[str]:
    """Split the Grade Names cell. The source separates with both ',' and ';'."""
    s = clean_text(raw)
    if s is None:
        return []
    parts = [p.strip() for p in re.split(r"[;,]", s.replace("\n", " "))]
    seen, out = set(), []
    for p in parts:
        p = re.sub(r"\s+", " ", p).strip()
        if not p:
            continue
        key = normalize_synonym(p)
        if key and key not in seen:
            seen.add(key)
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def get_or_create_synonym(cur, cache: Dict[str, int], text_value: str) -> int:
    """synonyms row for this text, reusing an existing one where it matches.

    Keyed on normalized_text so the chemical branch and this one converge on the
    same row for the same name instead of storing it twice.
    """
    normalized = normalize_synonym(text_value)
    if normalized in cache:
        return cache[normalized]

    cur.execute("SELECT id FROM synonyms WHERE normalized_text = %s ORDER BY id LIMIT 1",
                (normalized,))
    row = cur.fetchone()
    if row:
        cache[normalized] = row[0]
        return row[0]

    cur.execute(
        "INSERT INTO synonyms (synonym_text, normalized_text, date_added, created_at, updated_at) "
        "VALUES (%s, %s, now(), now(), now()) RETURNING id",
        (text_value, normalized),
    )
    sid = cur.fetchone()[0]
    cache[normalized] = sid
    return sid


def link_synonym(cur, crude_oil_id: int, synonym_id: int, source_id: int) -> None:
    cur.execute(
        """
        INSERT INTO crude_oil_synonym
            (crude_oil_id, synonym_id, relationship_type, ambiguity_flag, source_id,
             created_at, updated_at)
        VALUES (%s, %s, %s, false, %s, now(), now())
        ON CONFLICT (crude_oil_id, synonym_id) DO UPDATE SET
            relationship_type = EXCLUDED.relationship_type,
            updated_at        = now()
        """,
        (crude_oil_id, synonym_id, RELATIONSHIP_TYPE, source_id),
    )


def flag_ambiguous(cur, source_id: int) -> int:
    """Mark names that resolve to more than one product within this source.

    'V-Power Diesel' is listed against both ULSD grades and is a product in its
    own right, so a search on it cannot pick one oil. The source really is
    ambiguous there; the flag records that rather than resolving it by guesswork.
    """
    cur.execute(
        """
        UPDATE crude_oil_synonym cs
           SET ambiguity_flag = (dup.n > 1), updated_at = now()
          FROM (SELECT synonym_id, count(DISTINCT crude_oil_id) AS n
                  FROM crude_oil_synonym
                 WHERE source_id = %s
                 GROUP BY synonym_id) dup
         WHERE cs.synonym_id = dup.synonym_id
           AND cs.source_id = %s
           AND cs.ambiguity_flag IS DISTINCT FROM (dup.n > 1)
        """,
        (source_id, source_id),
    )
    return cur.rowcount


def resolve_source(cur, name: str) -> int:
    cur.execute("SELECT id FROM source WHERE name = %s", (name,))
    row = cur.fetchone()
    if row is None:
        sys.exit(
            f"Error: source {name!r} not found.\n"
            f"  It is declared in etl/data/source.json - register it with:\n"
            f"      python3 etl/source.py"
        )
    return row[0]


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", default=DEFAULT_FILE)
    ap.add_argument("--dry-run", action="store_true", help="parse and report, write nothing")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.exists():
        sys.exit(f"Error: workbook not found: {path}")

    df = pd.read_excel(path, sheet_name=SHEET)
    log.info("%s [%s]: %d rows, columns %s", path.name, SHEET, len(df), list(df.columns))

    missing = [c for c, _, _, _ in PROPERTIES if c not in df.columns]
    if COL_NAME not in df.columns:
        missing.append(COL_NAME)
    if missing:
        sys.exit(f"Error: sheet is missing column(s): {', '.join(missing)}")

    # --- parse everything before touching the database ----------------------
    parsed_rows: List[dict] = []
    skipped_unnamed = 0
    for i, row in df.iterrows():
        oil_name = clean_text(row.get(COL_NAME))
        if oil_name is None:
            skipped_unnamed += 1
            log.warning("row %d: no %s - skipped", i + 2, COL_NAME)
            continue
        oil_name = re.sub(r"\s+", " ", oil_name)

        props = {}
        for column, field, unit, is_text in PROPERTIES:
            cell = row.get(column)
            parsed = parse_identifier(cell) if is_text else parse_qualified(cell, unit)
            if parsed is not None:
                props[field] = parsed

        parsed_rows.append({
            "oil_name": oil_name,
            "grades": split_grade_names(row.get(COL_GRADES)),
            "properties": props,
        })

    total_props = sum(len(r["properties"]) for r in parsed_rows)
    total_grades = sum(len(r["grades"]) for r in parsed_rows)
    log.info("Parsed %d product(s), %d property value(s), %d grade name(s)%s",
             len(parsed_rows), total_props, total_grades,
             f", {skipped_unnamed} row(s) skipped for having no name" if skipped_unnamed else "")

    unnormalised = [(r["oil_name"], f, p["value"])
                    for r in parsed_rows for f, p in r["properties"].items()
                    if p["value_type"] == "text" and f not in ("UN_NUMBER", "GENERIC_PRODUCT",
                                                               "MAIN_CHARACTERISTICS")]
    for oil, field, value in unnormalised:
        log.warning("kept as text, no bound read: %-24s %-14s %r", oil[:24], field, value)

    if args.dry_run:
        for r in parsed_rows[:5]:
            log.info("  %-26s grades=%-2d props=%s", r["oil_name"], len(r["grades"]),
                     ", ".join(sorted(r["properties"])))
        log.info("--dry-run: nothing written.")
        return 0

    load_dotenv()
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            source_id = resolve_source(cur, SOURCE_NAME)
            log.info("Source id=%s (%r)", source_id, SOURCE_NAME)

            added = ensure_field_definitions(cur, only=FIELDS)
            log.info("field_definitions: %d added, %d already present", added, len(FIELDS) - added)

            cache: Dict[str, int] = {}
            created = updated = n_props = n_links = 0
            for r in parsed_rows:
                oil_id, was_created = upsert_crude_oil(cur, r["oil_name"], source_id, None)
                created += was_created
                updated += (not was_created)

                for field, parsed in r["properties"].items():
                    upsert_property(cur, oil_id, source_id, field, parsed, ENTERED_BY)
                    n_props += 1

                for grade in r["grades"]:
                    link_synonym(cur, oil_id, get_or_create_synonym(cur, cache, grade), source_id)
                    n_links += 1

            flagged = flag_ambiguous(cur, source_id)

            cur.execute("""SELECT count(DISTINCT s.synonym_text)
                             FROM crude_oil_synonym cs
                             JOIN synonyms s ON s.id = cs.synonym_id
                            WHERE cs.source_id = %s AND cs.ambiguity_flag""", (source_id,))
            ambiguous = cur.fetchone()[0]

        conn.commit()
        log.info("✓ Committed. crude_oil: %d created, %d updated | properties: %d "
                 "| grade names linked: %d (%d ambiguous, %d flag(s) changed)",
                 created, updated, n_props, n_links, ambiguous, flagged)
        return 0
    except Exception:
        conn.rollback()
        log.exception("Import failed - rolled back, nothing written.")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
