"""Shared helpers for the crude-oil loaders.

Both crude-oil sources feed the same pair of tables (``crude_oil`` and
``crude_oil_property_values``), so the field-definition seed, the upserts and
the value parsing live here rather than being duplicated per loader.

Nothing in this module touches ``cargo_chemical`` or ``cargo_property_values``.
Crude oils are a separate entity; the two branches meet only at ``source``.
"""

import datetime as _dt
import re
from typing import Any, Dict, Optional, Tuple

import xlrd

# ---------------------------------------------------------------------------
# Field definitions
# ---------------------------------------------------------------------------
# field_name -> (display_name, data_type, canonical_unit, category, description)
#
# `unit` here is the CANONICAL unit for the property, not a promise about any
# given row: the two sources publish pour point in different units (°F in the
# basic source, °C in the assay source), so every value row carries its own
# `unit` and callers must read that, never this one.
FIELD_DEFS: Dict[str, Tuple[str, str, Optional[str], str, str]] = {
    # --- Shell Cargo Master (refined-product characteristics) -----------------
    # Added for shell_cargo_master.py. That source describes refined products
    # rather than crude assays, so it quotes a different set of properties; they
    # live in the same registry because they land in the same value table.
    "UN_NUMBER": (
        "UN Number", "text", None, "Regulatory",
        "UN transport number as the source prints it. Text, not a number: a row "
        "may cite several ('1223/1202', '2398 & 1149').",
    ),
    "GENERIC_PRODUCT": (
        "Generic Product", "text", None, "Identity",
        "The source's generic product description for the grade.",
    ),
    "FLASH_POINT": (
        "Flash Point", "number", "°C", "Physical",
        "Flash point. Usually quoted as a bound ('above 56', 'below -20') rather "
        "than a point value - read normalized_min / normalized_max.",
    ),
    "DENSITY": (
        "Density", "number", "kg/m3", "Physical",
        "Density. Sometimes a range ('840 to 875') - read normalized_min / max.",
    ),
    "MAIN_CHARACTERISTICS": (
        "Main Characteristics", "text", None, "Physical",
        "The source's free-text description of appearance and sensitivities.",
    ),

    "API": (
        "Gravity (API)", "number", "°API", "Physical",
        "API gravity. The assay source quotes a Min/Max range; the basic source a single value.",
    ),
    "SULFUR": (
        "Sulfur Content", "number", "wt%", "Physical",
        "Total sulfur. Mostly wt%, but some rows are published in ppm or g/kg - read the row's unit.",
    ),
    "POUR_POINT": (
        "Pour Point", "number", "°C", "Physical",
        "Pour point. UNIT VARIES BY SOURCE: °F in the basic source, °C in the assay source.",
    ),
    "CLOUD_POINT": (
        "Cloud Point (Calculated)", "number", "°C", "Physical",
        "Calculated cloud point, quoted as a Min/Max range.",
    ),
    "RVP": (
        "Reid Vapour Pressure", "number", "psi", "Physical",
        "Reid vapour pressure, quoted as a Min/Max range.",
    ),
    "WAX": (
        "Total Wax", "number", "wt%", "Physical",
        "Total wax content.",
    ),
    "GAS_GT_C4": (
        "Gas > C4", "number", "wt%", "Physical",
        "Fraction heavier than C4.",
    ),
    "VISCOSITY_T1": (
        "Viscosity Temperature 1", "number", "°C", "Physical",
        "Temperature of the first viscosity measurement; pairs with VISCOSITY_X1.",
    ),
    "VISCOSITY_X1": (
        "Viscosity at T1", "number", "cSt", "Physical",
        "Kinematic viscosity at VISCOSITY_T1.",
    ),
    "VISCOSITY_T2": (
        "Viscosity Temperature 2", "number", "°C", "Physical",
        "Temperature of the second viscosity measurement; pairs with VISCOSITY_X2.",
    ),
    "VISCOSITY_X2": (
        "Viscosity at T2", "number", "cSt", "Physical",
        "Kinematic viscosity at VISCOSITY_T2.",
    ),
    "MINIMUM_TEMPERATURE_LOAD": (
        "Minimum Temperature Required - Load", "text", "°C", "Carriage",
        "Minimum load temperature, or the literal 'No Heat' when none is required.",
    ),
    "MINIMUM_TEMPERATURE_CARRIAGE": (
        "Minimum Temperature Required - Carriage", "text", "°C", "Carriage",
        "Minimum carriage temperature, or the literal 'No Heat' when none is required.",
    ),
    "MINIMUM_TEMPERATURE_DISCHARGE": (
        "Minimum Temperature Required - Discharge", "text", "°C", "Carriage",
        "Minimum discharge temperature, or the literal 'No Heat' when none is required.",
    ),
    "COW_DBT": (
        "Crude Oil Washing Requirement (DBT)", "text", None, "Carriage",
        "COW requirement code for dirty-ballast tankers, e.g. A1, A2, A2/A1, None.",
    ),
    "COW_SBT": (
        "Crude Oil Washing Requirement (SBT)", "text", None, "Carriage",
        "COW requirement code for segregated-ballast tankers, e.g. B1, B2, B2/B1.",
    ),
    "H2S_OIL_PHASE_NORMAL": (
        "H2S in Oil Phase (Normal)", "number", "ppm", "Health",
        "Normal hydrogen sulfide concentration in the oil phase. Distinct from SULFUR.",
    ),
    "H2S_OIL_PHASE_MAX": (
        "H2S in Oil Phase (Max)", "number", "ppm", "Health",
        "Maximum hydrogen sulfide concentration in the oil phase. Distinct from SULFUR.",
    ),
    "BENZENE": (
        "Benzene Content", "number", "wt%", "Health",
        "Benzene content by weight.",
    ),
    "REMARKS": (
        "Remarks", "text", None, "Carriage",
        "Free-text carriage/washing guidance as printed by the source.",
    ),
    "ASSAY_DATE": (
        "Assay Date", "text", None, "Identity",
        "Assay date exactly as printed. The parsed form is on as_of_date; see notes for "
        "the convention used when the source gives only a month or a year.",
    ),
}

# Cells that mean "no data" rather than a value.
MISSING = {"", "-", "--", "---", "n/a", "na", "none", "?", "nil"}

# entered_by is set per loader; these match the cargo_property_values convention.
ENTRY_TYPE = "import"
IS_WINNING = True
CONFLICT_FLAG = False


def ensure_field_definitions(cur, only: Optional[list] = None) -> int:
    """Create any missing crude-oil field_definitions. Returns the number added.

    field_name is the FK target for crude_oil_property_values, so a definition
    must exist before any value referencing it is inserted.
    """
    names = only if only is not None else list(FIELD_DEFS)
    added = 0
    for name in names:
        display, dtype, unit, category, description = FIELD_DEFS[name]
        cur.execute(
            """
            INSERT INTO field_definitions
                (field_name, display_name, data_type, unit, category, description,
                 typical_source, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, now(), now())
            ON CONFLICT (field_name) DO NOTHING
            """,
            (name, display, dtype, unit, category, description, "Crude oil assay"),
        )
        added += cur.rowcount
    return added


# ---------------------------------------------------------------------------
# Name handling
# ---------------------------------------------------------------------------
def normalize_oil_name(name: str) -> str:
    """Fold case and collapse whitespace - nothing else.

    Deliberately conservative. 'Arabian Light' and 'Arabian Light - Berri' are
    different crudes, so no token is ever dropped; this only removes formatting
    noise so the same printed name matches itself across files.
    """
    return re.sub(r"\s+", " ", str(name).strip()).upper()


def clean_text(value: Any) -> Optional[str]:
    """Trim a cell, mapping the source's missing-data markers to None."""
    if value is None:
        return None
    s = re.sub(r"\s+", " ", str(value).strip())
    if s.lower() in MISSING:
        return None
    return s


def fmt_num(x: float) -> str:
    """Render a number for the `value` column.

    Integral floats lose the '.0' Excel shows; the exact value is preserved in
    the normalized_* columns, which is what any comparison should read.
    """
    if x == int(x):
        return str(int(x))
    return repr(round(x, 10)).rstrip("0").rstrip(".")


# ---------------------------------------------------------------------------
# Value parsing
# ---------------------------------------------------------------------------
_NUM_RE = re.compile(r"^[+-]?\d*\.?\d+$")
_LT_RE = re.compile(r"^<\s*([+-]?\d*\.?\d+)$")          # "<-60", "< -36"
_GT_RE = re.compile(r"^([+-]?\d*\.?\d+)\s*\+$")          # "3.96+"
_NUM_UNIT_RE = re.compile(r"^([+-]?\d*\.?\d+)\s*(.+)$")  # "160 ppm", "2.01 g/ kg"


class Parsed(dict):
    """A parsed cell: value / normalized_* / value_type / unit / notes."""


def parse_scalar(raw: Any, unit: Optional[str] = None,
                 allow_unit_override: bool = False) -> Optional[Parsed]:
    """Parse a single cell into value-column form, or None when there is no data.

    Handles the four shapes these two sources actually use:
      12.3      -> number
      "<-60"    -> range open below, normalized_max = -60
      "3.96+"   -> range open above, normalized_min = 3.96
      "No Heat" -> text
    """
    if isinstance(raw, float):
        return Parsed(value=fmt_num(raw), normalized_value=raw, normalized_min=None,
                      normalized_max=None, unit=unit, value_type="number", notes=None)

    s = clean_text(raw)
    if s is None:
        return None

    if _NUM_RE.match(s):
        # Covers the basic source's "+75.0" / "-30.0" pour points, stored as text.
        return Parsed(value=s, normalized_value=float(s), normalized_min=None,
                      normalized_max=None, unit=unit, value_type="number", notes=None)

    m = _LT_RE.match(s)
    if m:
        return Parsed(value=s, normalized_value=None, normalized_min=None,
                      normalized_max=float(m.group(1)), unit=unit, value_type="range",
                      notes="Source gives an upper bound only ('less than'); "
                            "normalized_min is unbounded.")

    m = _GT_RE.match(s)
    if m:
        return Parsed(value=s, normalized_value=None, normalized_min=float(m.group(1)),
                      normalized_max=None, unit=unit, value_type="range",
                      notes="Source gives a lower bound only (trailing '+'); "
                            "normalized_max is unbounded.")

    if allow_unit_override:
        # The basic source publishes a few sulfur figures in ppm or g/kg
        # instead of wt%. Keep the number, keep the source's own unit, and do
        # NOT silently convert it into the column's default unit.
        m = _NUM_UNIT_RE.match(s)
        if m:
            row_unit = re.sub(r"\s+", "", m.group(2))
            return Parsed(value=s, normalized_value=float(m.group(1)),
                          normalized_min=None, normalized_max=None, unit=row_unit,
                          value_type="number",
                          notes=f"Source published this row in {row_unit}, not the "
                                f"column's usual unit; not converted.")

    # A text value ("No Heat", "A2/A1") carries no unit, whatever the column's
    # default is - storing "°C" next to "No Heat" would be a lie.
    return Parsed(value=s, normalized_value=None, normalized_min=None,
                  normalized_max=None, unit=None, value_type="text", notes=None)


def parse_range(raw_min: Any, raw_max: Any, unit: Optional[str] = None) -> Optional[Parsed]:
    """Parse a Min/Max column pair.

    Both present -> range. Only one present -> that single value, unchanged.
    A range is never collapsed to a midpoint: the source published two numbers
    and both are kept.
    """
    lo = parse_scalar(raw_min, unit)
    hi = parse_scalar(raw_max, unit)

    if lo is None and hi is None:
        return None
    if hi is None:
        return lo
    if lo is None:
        return hi

    lo_n = lo["normalized_value"] if lo["normalized_value"] is not None else lo["normalized_min"]
    hi_n = hi["normalized_value"] if hi["normalized_value"] is not None else hi["normalized_max"]

    if lo_n is None or hi_n is None:
        # e.g. Min is "<-60" and Max is a number: keep both printed forms and
        # whatever bounds we can actually justify.
        return Parsed(value=f"{lo['value']} - {hi['value']}", normalized_value=None,
                      normalized_min=lo_n, normalized_max=hi_n, unit=unit,
                      value_type="range",
                      notes="One bound is an inequality in the source; "
                            "the corresponding normalized bound is unbounded.")

    return Parsed(value=f"{lo['value']} - {hi['value']}", normalized_value=None,
                  normalized_min=lo_n, normalized_max=hi_n, unit=unit,
                  value_type="range", notes=None)


# ---------------------------------------------------------------------------
# Assay date
# ---------------------------------------------------------------------------
# Incomplete-date convention, applied consistently and recorded in the notes of
# every row it affects:
#   full date      -> used as-is
#   month + year   -> first day of that month
#   year only      -> 1 January of that year
# The printed form is never discarded: it is stored verbatim as the ASSAY_DATE
# property value.
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

_MONYEAR_RE = re.compile(r"^([A-Za-z]{3})[_\-/ ](\d{2}|\d{4})$")


def parse_assay_date(raw: Any, cell_type: int, datemode: int
                     ) -> Tuple[Optional[_dt.datetime], Optional[str], Optional[str]]:
    """Return (parsed_date, printed_form, convention_note)."""
    if cell_type == xlrd.XL_CELL_DATE:
        # Serials below ~3000 are not dates at all - they are bare years typed
        # into a date-formatted cell (1973, 1990). Excel would render those as
        # 1905, which would be wrong by 70 years.
        if raw < 3000:
            year = int(raw)
            return (_dt.datetime(year, 1, 1), str(year),
                    "Source gives the year only; dated to 1 January by project convention.")
        y, mo, d, hh, mm, ss = xlrd.xldate_as_tuple(raw, datemode)
        return _dt.datetime(y, mo, d, hh, mm, ss), f"{y:04d}-{mo:02d}-{d:02d}", None

    s = clean_text(raw)
    if s is None:
        return None, None, None

    m = _MONYEAR_RE.match(s)
    if m:
        mon = _MONTHS.get(m.group(1).lower())
        if mon:
            yr = int(m.group(2))
            if yr < 100:
                yr += 1900 if yr >= 30 else 2000
            return (_dt.datetime(yr, mon, 1), s,
                    "Source gives month and year only; dated to the first of the "
                    "month by project convention.")

    if re.fullmatch(r"\d{4}", s):
        return (_dt.datetime(int(s), 1, 1), s,
                "Source gives the year only; dated to 1 January by project convention.")

    return None, s, "Assay date could not be parsed; printed form preserved."


# ---------------------------------------------------------------------------
# Upserts
# ---------------------------------------------------------------------------
def get_source_id(cur, name: str) -> int:
    cur.execute("SELECT id FROM source WHERE name = %s", (name,))
    row = cur.fetchone()
    if not row:
        raise SystemExit(
            f"Error: source {name!r} is not registered. Add it to etl/data/source.json "
            f"and run `python3 etl/source.py` first."
        )
    return row[0]


def upsert_crude_oil(cur, oil_name: str, source_id: int,
                     country: Optional[str]) -> Tuple[int, bool]:
    """Find or create the (oil_name, source_id) row. Returns (id, created)."""
    cur.execute(
        "SELECT id FROM crude_oil WHERE oil_name = %s AND source_id = %s",
        (oil_name, source_id),
    )
    row = cur.fetchone()
    if row:
        if country is not None:
            cur.execute(
                "UPDATE crude_oil SET country_of_origin = COALESCE(%s, country_of_origin), "
                "updated_at = now() WHERE id = %s",
                (country, row[0]),
            )
        return row[0], False

    cur.execute(
        "INSERT INTO crude_oil (oil_name, source_id, country_of_origin, created_at, updated_at) "
        "VALUES (%s, %s, %s, now(), now()) RETURNING id",
        (oil_name, source_id, country),
    )
    return cur.fetchone()[0], True


def upsert_property(cur, crude_oil_id: int, source_id: int, field_name: str,
                    parsed: Parsed, entered_by: str,
                    as_of_date=None, extra_note: Optional[str] = None) -> None:
    """Insert or refresh one (oil, source, field) value.

    ON CONFLICT updates rather than skipping so a re-run picks up corrections
    to the spreadsheet. Values from other sources are untouched - source_id is
    part of the key, so one source can never overwrite another's figure.
    """
    notes = parsed.get("notes")
    if extra_note:
        notes = f"{notes} {extra_note}" if notes else extra_note

    cur.execute(
        """
        INSERT INTO crude_oil_property_values
            (crude_oil_id, source_id, field_name, value, normalized_value,
             normalized_min, normalized_max, unit, value_type, source_synonym_id,
             source_page_ref, as_of_date, entered_date, entered_by, entry_type,
             is_winning, conflict_flag, notes, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, NULL, %s,
                now(), %s, %s, %s, %s, %s, now(), now())
        ON CONFLICT (crude_oil_id, source_id, field_name) DO UPDATE SET
            value            = EXCLUDED.value,
            normalized_value = EXCLUDED.normalized_value,
            normalized_min   = EXCLUDED.normalized_min,
            normalized_max   = EXCLUDED.normalized_max,
            unit             = EXCLUDED.unit,
            value_type       = EXCLUDED.value_type,
            as_of_date       = EXCLUDED.as_of_date,
            notes            = EXCLUDED.notes,
            updated_at       = now()
        """,
        (crude_oil_id, source_id, field_name, parsed["value"],
         parsed["normalized_value"], parsed["normalized_min"], parsed["normalized_max"],
         parsed["unit"], parsed["value_type"], as_of_date,
         entered_by, ENTRY_TYPE, IS_WINNING, CONFLICT_FLAG, notes),
    )
