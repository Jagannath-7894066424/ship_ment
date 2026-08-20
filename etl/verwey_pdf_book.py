#!/usr/bin/env python3
"""
Load the Dr Verwey Tank Cleaning Guide (PDF Book edition) — Cargo Details sheet
into cargo_chemical + cargo_property_values.

Source file has two row shapes:

    [10] ALCOHOL ETHOXYLATES,,,,,,,,,,,,,,,          <- family heading, col 0
    ,10,ALCOHOL (C12-C15) POLY (20+) ETHOXYLATE,...  <- member, col 1 = Verwey no.

The heading carries NO properties — it is the family label printed above its
members, and it is also the name the FROM->TO matrix uses for that Verwey
number. Headings therefore do NOT become cargo_chemical rows; the number and
the family name are recorded as properties on each member instead, so a
cargo_family_group table can be built from them later without re-reading the
file.

583 data rows = 25 headings + 558 members over 431 Verwey numbers
(406 numbers hold exactly one member, 25 hold 2-19).

Every source column is written to cargo_property_values (nothing is dropped);
the subset that has a dedicated cargo_chemical column is ALSO written there so
existing queries keep working. Viscosity is deliberately property-only: the
source quotes it at @25..@90 C and cargo_chemical.viscosity_cp_20c would assert
a temperature the data does not have.

Idempotent: chemicals keyed by (source_id, canonical_name), properties by
(cargo_id, source_id, field_name); re-running upserts in place.

Usage:
    python3 etl/verwey_pdf_book.py                  # default file, upsert
    python3 etl/verwey_pdf_book.py --dry-run
    python3 etl/verwey_pdf_book.py --limit 20
    python3 etl/verwey_pdf_book.py path/to/file.csv
"""

import argparse
import csv
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

from _paths import input_file
from cargo_chemicals import get_source_id, create_source

SOURCE_NAME = "Dr Verweys Tank Cleaning Guide Pdf Book"
DEFAULT_FILE = input_file("Dr Verweys Tank Cleaning Guide Pdf Book - Cargo details.csv")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("verwey_pdf_book")

# ---------------------------------------------------------------------------
# Column layout of the Cargo Details sheet
# ---------------------------------------------------------------------------
COL_HEADING, COL_ID, COL_NAME, COL_CAS = 0, 1, 2, 3
COL_MARPOL, COL_FOSFA, COL_NIOP, COL_EU = 4, 5, 6, 7
COL_DENSITY, COL_SOLUBILITY = 8, 9
COL_BOILING, COL_MELTING, COL_VISCOSITY, COL_FLASH = 10, 11, 12, 13
COL_STATIC = 14

# "[10] ALCOHOL ETHOXYLATES" -> ("10", "ALCOHOL ETHOXYLATES")
HEADING_RE = re.compile(r"^\s*\[(\d+)\]\s*(.+?)\s*$")

# The sheet uses several spellings of "no value". '-' and the en/em dashes are
# the transcriber's blanks; 'Unknown' is the book's own wording.
NULL_TOKENS = {"", "-", "–", "—", "unknown", "none", "n.a.", "n/a"}

# Excel re-formatted a subset of the CAS numbers as dates: 107-05-1 was stored
# as '0107-05-01' and 8002-05-9 as '8002-05-09'. Real CAS is 2-7 digits, then
# exactly 2, then exactly 1.
CAS_MANGLED_RE = re.compile(r"^0*(\d{1,7})-(\d{1,2})-0?(\d{1,2})$")
CAS_CLEAN_RE = re.compile(r"^\d{2,7}-\d{2}-\d$")

# Leading number of a free-text measurement: "0.958 (@50 C)", "1.5 (MIN)",
# "0.91 (0.79-0.91)", "-46", "3000 (MAX)".
LEADING_NUM_RE = re.compile(r"^\s*(-?\d+(?:[.,]\d+)?)")
# Reference temperature carried inside the same cell: "(@50 C)", "@ 37.8 C".
REF_TEMP_RE = re.compile(r"@\s*(-?\d+(?:[.,]\d+)?)\s*[°]?\s*C", re.IGNORECASE)
# Qualifier word in parentheses: "(MIN)", "(Approx.)", "(Lower Limit)".
QUALIFIER_RE = re.compile(r"\(([^()@\d][^()]*)\)")

TRUE_TOKENS = {"yes", "y", "true", "1"}
FALSE_TOKENS = {"no", "n", "false", "0"}

# ---------------------------------------------------------------------------
# Source column -> field_definitions.field_name (every column is mapped; none
# is dropped). Entries flagged create=True are added to the catalog on demand.
# ---------------------------------------------------------------------------
FieldSpec = Tuple[str, str, Optional[str], str, str]  # name, display, unit, type, category

PROPERTY_FIELDS: Dict[int, FieldSpec] = {
    COL_ID:         ("verwey_cargo_number", "Verwey Cargo Number", None, "text", "Cleaning"),
    COL_CAS:        ("cas_number", "CAS Number", None, "text", "Identity"),
    COL_MARPOL:     ("marpol_category", "MARPOL Category", None, "text", "Regulatory"),
    COL_FOSFA:      ("fosfa_grade", "FOSFA Acceptability", None, "text", "Regulatory"),
    COL_NIOP:       ("niop_grade", "NIOP Acceptability", None, "text", "Regulatory"),
    COL_EU:         ("eu_grade", "EU Acceptability", None, "text", "Regulatory"),
    COL_DENSITY:    ("density", "Relative Density", "kg/l", "number", "Physical"),
    COL_SOLUBILITY: ("water_solubility", "Water Solubility", "g/g", "text", "Physical"),
    COL_BOILING:    ("boiling_point", "Boiling Point", "°C", "number", "Physical"),
    COL_MELTING:    ("melting_point", "Melting Point", "°C", "number", "Physical"),
    COL_VISCOSITY:  ("viscosity", "Viscosity", "mPa·s", "number", "Physical"),
    COL_FLASH:      ("flash_point", "Flash Point", "°C", "number", "Physical"),
    COL_STATIC:     ("static_accumulator", "Static Accumulator", None, "boolean", "Physical"),
}
# Family label is not a source column; it comes from the heading row above.
FAMILY_FIELD: FieldSpec = ("verwey_family_name", "Verwey Family Name", None, "text", "Cleaning")

# Properties that also have a dedicated cargo_chemical column. Viscosity is
# absent on purpose (see module docstring).
WIDE_COLUMNS = {
    COL_CAS: "cas_number",
    COL_MARPOL: "marpol_category",
    COL_DENSITY: "density_g_cm3",
    COL_SOLUBILITY: "water_solubility",
    COL_BOILING: "boiling_point_c",
    COL_MELTING: "melting_point_c",
    COL_FLASH: "flash_point_c",
}


# ---------------------------------------------------------------------------
# Cell parsing
# ---------------------------------------------------------------------------
def clean(cell: Optional[str]) -> Optional[str]:
    """Strip a cell and collapse the sheet's several 'no value' spellings to None."""
    if cell is None:
        return None
    text = cell.strip()
    return None if text.lower() in NULL_TOKENS else text


def cas_checksum_ok(cas: str) -> bool:
    """Verify the CAS check digit (last digit = weighted sum of the rest mod 11)."""
    digits = cas.replace("-", "")
    body, check = digits[:-1], int(digits[-1])
    total = sum(int(d) * (len(body) - i) for i, d in enumerate(body))
    return total % 11 == check


def repair_cas(raw: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Return (cas, note). Undoes the Excel date mangling; never invents a value.

    The note is non-None when the value was rewritten or looks wrong, so the
    change is visible in cargo_property_values.notes rather than silent.
    """
    if raw is None:
        return None, None
    text = raw.strip()
    if CAS_CLEAN_RE.match(text):
        return text, None if cas_checksum_ok(text) else "cas checksum mismatch"

    m = CAS_MANGLED_RE.match(text)
    if m:
        fixed = f"{int(m.group(1))}-{int(m.group(2)):02d}-{int(m.group(3))}"
        if CAS_CLEAN_RE.match(fixed) and cas_checksum_ok(fixed):
            return fixed, f"repaired from Excel date format {text!r}"
        return fixed, f"repaired from {text!r}; checksum not verified"
    return text, "unrecognised CAS format"


def parse_measurement(raw: Optional[str]) -> Tuple[Optional[float], Optional[str]]:
    """Split a free-text measurement into (numeric value, provenance note).

    The sheet stores value + reference temperature + qualifier in one cell
    ("0.958 (@50 C)", "3000 (MAX)", "LOW"). The raw string is always kept in
    cargo_property_values.value; this pulls out what can be compared
    numerically and records the rest, because cargo_property_values has no
    reference-temperature column.
    """
    if raw is None:
        return None, None

    notes: List[str] = []
    temp = REF_TEMP_RE.search(raw)
    if temp:
        notes.append(f"ref_temp_c={temp.group(1).replace(',', '.')}")
    qual = QUALIFIER_RE.search(raw)
    if qual:
        notes.append(f"qualifier={qual.group(1).strip()}")

    num = LEADING_NUM_RE.match(raw)
    if not num:
        # "LOW", "SOLID", "COMPLETE", "Insoluble" - real values, just not numeric.
        notes.append("non-numeric value")
        return None, "; ".join(notes)
    return float(num.group(1).replace(",", ".")), "; ".join(notes) or None


def parse_boolean(raw: Optional[str]) -> Optional[bool]:
    if raw is None:
        return None
    text = raw.strip().lower()
    if text in TRUE_TOKENS:
        return True
    if text in FALSE_TOKENS:
        return False
    return None


def split_embedded_synonyms(name: str) -> Tuple[str, List[str]]:
    """Pull ';'-separated alternative names out of a NAME cell.

    Row 216 is 'LATEX: Carboxylated Styrene-Butadiene Copolymer;
    Styrene-Butadiene Rubber' - one cargo written with two alternative names.
    The verbatim string stays the canonical name (the book's own wording is
    never rewritten); the parts are additionally registered as synonyms.
    """
    if ":" not in name or ";" not in name:
        return name, []
    prefix, rest = name.split(":", 1)
    parts = [p.strip() for p in rest.split(";") if p.strip()]
    if len(parts) < 2:
        return name, []
    return name, [prefix.strip()] + parts


# ---------------------------------------------------------------------------
# File parsing
# ---------------------------------------------------------------------------
class Member:
    """One member row: a real cargo with its own properties."""

    __slots__ = ("verwey_number", "family_name", "name", "cells", "row_no", "synonyms")

    def __init__(self, verwey_number: str, family_name: Optional[str],
                 name: str, cells: List[Optional[str]], row_no: int):
        self.verwey_number = verwey_number
        self.family_name = family_name
        self.name = name
        self.cells = cells
        self.row_no = row_no
        _, self.synonyms = split_embedded_synonyms(name)


def parse_file(path: Path) -> Tuple[List[Member], Dict[str, str]]:
    """Return (members, families).

    families maps Verwey number -> family heading, for the 25 numbers that
    print one. The other 406 numbers have no heading; their family name is the
    single member's own name (filled in by the caller).
    """
    with open(path, encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    if not rows:
        return [], {}

    members: List[Member] = []
    families: Dict[str, str] = {}
    current_family: Optional[Tuple[str, str]] = None  # (number, name)

    for row_no, row in enumerate(rows[1:], start=2):
        cells = [clean(c) for c in row] + [None] * (COL_STATIC + 1 - len(row))

        heading = cells[COL_HEADING]
        if heading:
            m = HEADING_RE.match(heading)
            if not m:
                log.warning("Row %d: unparseable family heading %r - skipped", row_no, heading)
                continue
            number, family_name = m.group(1), m.group(2)
            families[number] = family_name
            current_family = (number, family_name)
            continue

        number = cells[COL_ID]
        name = cells[COL_NAME]
        if not number or not number.isdigit() or not name:
            continue

        # A member only belongs to the heading directly above it. The ID column
        # is the authority: if it disagrees with the heading, the heading is not
        # this row's family (the sheet has one such case, Piperylene Concentrate
        # carrying id 264 inside [264] NESSOLS - which the ID column endorses).
        family_name = None
        if current_family and current_family[0] == number:
            family_name = current_family[1]
        members.append(Member(number, family_name, name, cells, row_no))

    return members, families


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def resolve_source(cur) -> int:
    """Reuse the existing source row if the name is already there, else create it."""
    sid = get_source_id(cur, SOURCE_NAME)
    if sid is not None:
        log.info("Using existing source id=%s (%r)", sid, SOURCE_NAME)
        return sid
    return create_source(cur, SOURCE_NAME)


def ensure_field_definitions(cur, dry_run: bool) -> None:
    """Add any catalog entries this sheet needs that are not already defined.

    cargo_property_values.field_name is a FK to field_definitions, so an
    unknown field would fail the insert rather than be silently dropped.
    """
    specs = list(PROPERTY_FIELDS.values()) + [FAMILY_FIELD]
    cur.execute("SELECT field_name FROM field_definitions")
    known = {r[0] for r in cur.fetchall()}
    missing = [s for s in specs if s[0] not in known]
    if not missing:
        log.info("field_definitions: all %d fields already defined", len(specs))
        return
    log.info("field_definitions: adding %d new field(s): %s",
             len(missing), ", ".join(s[0] for s in missing))
    if dry_run:
        return
    execute_values(
        cur,
        "INSERT INTO field_definitions "
        "(field_name, display_name, unit, data_type, category, typical_source, "
        " description, created_at, updated_at) VALUES %s "
        "ON CONFLICT (field_name) DO NOTHING",
        [(name, display, unit, dtype, category, SOURCE_NAME,
          f"{display} as published in the Verwey cargo details sheet")
         for name, display, unit, dtype, category in missing],
        template="(%s,%s,%s,%s,%s,%s,%s,now(),now())",
    )


def build_chemical_row(m: Member) -> Dict[str, Any]:
    """Map a member onto the cargo_chemical wide columns."""
    row: Dict[str, Any] = {"canonical_name": m.name}

    cas, _ = repair_cas(m.cells[COL_CAS])
    row["cas_number"] = cas
    row["marpol_category"] = m.cells[COL_MARPOL]

    for col, column_name in (
        (COL_DENSITY, "density_g_cm3"),
        (COL_BOILING, "boiling_point_c"),
        (COL_MELTING, "melting_point_c"),
        (COL_FLASH, "flash_point_c"),
    ):
        value, _ = parse_measurement(m.cells[col])
        row[column_name] = value

    solubility = m.cells[COL_SOLUBILITY]
    row["water_solubility"] = solubility
    # 'REACTION' in the solubility column is the book saying the cargo reacts
    # with water - the hazard behind procedure S ("Iso-cyanates react with water
    # to form polyurea and CO2"). Promote it out of the text field so safety
    # logic can read it.
    row["water_reactive"] = True if solubility and solubility.upper() == "REACTION" else None
    return row


def load_chemicals(cur, members: List[Member], source_id: int,
                   dry_run: bool) -> Dict[int, int]:
    """Upsert cargo_chemical rows. Returns row_no -> cargo_id."""
    by_name: Dict[str, List[Member]] = {}
    for m in members:
        by_name.setdefault(m.name.strip().lower(), []).append(m)

    duplicates = {k: v for k, v in by_name.items() if len(v) > 1}
    for key, group in duplicates.items():
        log.warning("Duplicate name %r appears under Verwey numbers %s "
                    "-> one chemical, numbers merged on the property row",
                    group[0].name, ", ".join(m.verwey_number for m in group))

    # One cargo_chemical per distinct name; (source_id, canonical_name) is UNIQUE
    # so the duplicates above must collapse rather than fight over the row.
    first: List[Member] = [g[0] for g in by_name.values()]
    rows = [build_chemical_row(m) for m in first]
    log.info("cargo_chemical: %d member rows -> %d distinct chemicals (%d duplicate names)",
             len(members), len(rows), len(duplicates))
    if dry_run:
        # Synthetic ids so the later stages can still be counted and reported.
        synthetic = {key: -(i + 1) for i, key in enumerate(by_name)}
        return {m.row_no: synthetic[m.name.strip().lower()] for m in members}

    execute_values(
        cur,
        """
        INSERT INTO cargo_chemical
            (canonical_name, source_id, cas_number, marpol_category, density_g_cm3,
             boiling_point_c, melting_point_c, flash_point_c, water_solubility,
             water_reactive, date_added, date_last_updated, created_at, updated_at)
        VALUES %s
        ON CONFLICT (source_id, canonical_name) DO UPDATE SET
            cas_number       = EXCLUDED.cas_number,
            marpol_category  = EXCLUDED.marpol_category,
            density_g_cm3    = EXCLUDED.density_g_cm3,
            boiling_point_c  = EXCLUDED.boiling_point_c,
            melting_point_c  = EXCLUDED.melting_point_c,
            flash_point_c    = EXCLUDED.flash_point_c,
            water_solubility = EXCLUDED.water_solubility,
            water_reactive   = EXCLUDED.water_reactive,
            date_last_updated = now(),
            updated_at        = now()
        """,
        [(r["canonical_name"], source_id, r["cas_number"], r["marpol_category"],
          r["density_g_cm3"], r["boiling_point_c"], r["melting_point_c"],
          r["flash_point_c"], r["water_solubility"], r["water_reactive"])
         for r in rows],
        template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),now(),now(),now())",
        page_size=500,
    )

    cur.execute(
        "SELECT id, canonical_name FROM cargo_chemical WHERE source_id = %s", (source_id,)
    )
    name_to_id = {n.strip().lower(): i for i, n in cur.fetchall()}
    return {m.row_no: name_to_id[m.name.strip().lower()]
            for m in members if m.name.strip().lower() in name_to_id}


def load_properties(cur, members: List[Member], families: Dict[str, str],
                    ids: Dict[int, int], source_id: int, dry_run: bool) -> int:
    """Write every source column of every member to cargo_property_values."""
    # Members that share a name share a cargo_id; their Verwey numbers are
    # merged into one value so neither is lost to the unique key.
    numbers_by_cargo: Dict[int, List[str]] = {}
    for m in members:
        cargo_id = ids.get(m.row_no)
        if cargo_id is not None and m.verwey_number not in numbers_by_cargo.setdefault(cargo_id, []):
            numbers_by_cargo[cargo_id].append(m.verwey_number)

    seen: set = set()
    values: List[tuple] = []

    def add(cargo_id: int, field: str, raw: Optional[str],
            normalized: Optional[float], unit: Optional[str],
            value_type: str, note: Optional[str], conflict: bool = False) -> None:
        if raw is None or (cargo_id, field) in seen:
            return
        seen.add((cargo_id, field))
        values.append((cargo_id, source_id, field, raw, normalized, unit, value_type,
                       note, "verwey_pdf_book", "import", True, conflict))

    for m in members:
        cargo_id = ids.get(m.row_no)
        if cargo_id is None:
            continue

        numbers = numbers_by_cargo.get(cargo_id, [m.verwey_number])
        family = m.family_name or families.get(m.verwey_number) or m.name

        for col, (field, _display, unit, dtype, _cat) in PROPERTY_FIELDS.items():
            if col == COL_ID:
                add(cargo_id, field, ",".join(numbers), None, None, "text",
                    "shared across several Verwey numbers" if len(numbers) > 1 else None,
                    conflict=len(numbers) > 1)
                continue

            raw = m.cells[col]
            if raw is None:
                continue

            if col == COL_CAS:
                fixed, note = repair_cas(raw)
                add(cargo_id, field, fixed, None, None, "text", note)
            elif col == COL_STATIC:
                flag = parse_boolean(raw)
                add(cargo_id, field, raw, None, None, "boolean",
                    None if flag is not None else "unrecognised boolean")
            elif dtype == "number":
                normalized, note = parse_measurement(raw)
                add(cargo_id, field, raw, normalized, unit, "number", note)
            else:
                note = ("water-reactive: 'REACTION' recorded as solubility"
                        if col == COL_SOLUBILITY and raw.upper() == "REACTION" else None)
                add(cargo_id, field, raw, None, unit, "text", note)

        add(cargo_id, FAMILY_FIELD[0], family, None, None, "text",
            None if m.family_name else "no printed heading; family is the cargo itself")

    log.info("cargo_property_values: %d rows", len(values))
    if dry_run or not values:
        return len(values)

    execute_values(
        cur,
        """
        INSERT INTO cargo_property_values
            (cargo_id, source_id, field_name, value, normalized_value, unit,
             value_type, notes, entered_by, entry_type, is_winning, conflict_flag,
             entered_date, created_at, updated_at)
        VALUES %s
        ON CONFLICT (cargo_id, source_id, field_name) DO UPDATE SET
            value            = EXCLUDED.value,
            normalized_value = EXCLUDED.normalized_value,
            unit             = EXCLUDED.unit,
            value_type       = EXCLUDED.value_type,
            notes            = EXCLUDED.notes,
            conflict_flag    = EXCLUDED.conflict_flag,
            updated_at       = now()
        """,
        values,
        template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),now(),now())",
        page_size=1000,
    )
    return len(values)


def load_synonyms(cur, members: List[Member], ids: Dict[int, int],
                  source_id: int, dry_run: bool) -> int:
    """Register alternative names that the NAME cell packs into one string."""
    pairs = [(ids[m.row_no], s) for m in members if m.row_no in ids for s in m.synonyms]
    if not pairs:
        log.info("cargo_synonym: no embedded alternative names found")
        return 0
    log.info("cargo_synonym: %d alternative name(s) from %d cargo row(s)",
             len(pairs), len({p[0] for p in pairs}))
    if dry_run:
        return len(pairs)

    for cargo_id, text in pairs:
        normalized = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
        cur.execute(
            "INSERT INTO synonyms (synonym_text, normalized_text, date_added, "
            "created_at, updated_at) VALUES (%s,%s,now(),now(),now()) RETURNING id",
            (text, normalized),
        )
        synonym_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO cargo_synonym (cargo_id, synonym_id, relationship_type, "
            "ambiguity_flag, source_id, created_at, updated_at) "
            "VALUES (%s,%s,%s,%s,%s,now(),now()) ON CONFLICT (cargo_id, synonym_id) DO NOTHING",
            (cargo_id, synonym_id, "alternative_name", False, source_id),
        )
    return len(pairs)


# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", default=str(DEFAULT_FILE),
                    help="Cargo Details CSV (defaults to the file in SHIP_DATA_DIR)")
    ap.add_argument("--dry-run", action="store_true",
                    help="parse and report, write nothing")
    ap.add_argument("--limit", type=int, help="only load the first N member rows")
    args = ap.parse_args()

    path = Path(args.file).expanduser()
    if not path.exists():
        log.error("Input file not found: %s", path)
        sys.exit(1)

    members, families = parse_file(path)
    if args.limit:
        members = members[: args.limit]
    log.info("Parsed %s: %d members, %d family headings, %d Verwey numbers",
             path.name, len(members), len(families),
             len({m.verwey_number for m in members}))
    if not members:
        log.error("No member rows parsed - check the column layout")
        sys.exit(1)

    load_dotenv(".env")
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        log.error("DATABASE_URL not set (run from the repo root so .env is found)")
        sys.exit(1)

    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            source_id = resolve_source(cur)
            ensure_field_definitions(cur, args.dry_run)
            ids = load_chemicals(cur, members, source_id, args.dry_run)
            n_props = load_properties(cur, members, families, ids, source_id, args.dry_run)
            n_syn = load_synonyms(cur, members, ids, source_id, args.dry_run)

        if args.dry_run:
            conn.rollback()
            log.info("DRY RUN - rolled back")
        else:
            conn.commit()
            log.info("✓ Committed: %d chemicals, %d property values, %d synonyms (source_id=%s)",
                     len(set(ids.values())), n_props, n_syn, source_id)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
