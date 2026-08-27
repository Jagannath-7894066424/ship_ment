"""Shared helpers for the gas loaders.

Every gas source feeds the same pair of tables (``cargo_gas`` and
``cargo_gas_property_values``), so the field-definition seed, the master upsert
and the property upsert live here rather than being duplicated per loader. The
oil branch has the same arrangement in ``etl/oil/_crude_oil.py``, and this
mirrors it deliberately - the two branches are the same shape.

Nothing here touches ``cargo_chemical`` or ``crude_oil``. A gas is its own
master; the three branches meet only at ``source``.
"""

import re
from typing import Any, Dict, Optional, Tuple

# ---------------------------------------------------------------------------
# Field definitions
# ---------------------------------------------------------------------------
# field_name -> (display_name, data_type, canonical_unit, category, description)
#
# field_definitions is shared by all three branches (cargo_property_values,
# crude_oil_property_values and cargo_gas_property_values all FK it), so a name
# added here is visible everywhere and must stay specific enough to be
# unambiguous.
FIELD_DEFS: Dict[str, Tuple[str, str, Optional[str], str, str]] = {
    "RELATIVE_VAPOUR_DENSITY": (
        "Relative Vapour Density", "number", None, "Physical",
        "Vapour density relative to air, which the source lists as 1.00. "
        "DIMENSIONLESS - it is a ratio, so `unit` is deliberately NULL. The "
        "source's 'KG / CUB.M' legend describes the densities the ratio is "
        "computed from, not the ratio itself. The measurement conditions vary "
        "per row and are recorded on the value, not here.",
    ),
}

# Cells that mean "no data" rather than a value.
MISSING = {"", "-", "--", "---", "n/a", "na", "none", "?", "nil"}

# entered_by is set per loader; these match the other two branches' convention.
ENTRY_TYPE = "import"
IS_WINNING = True
CONFLICT_FLAG = False


def clean_text(value: Any) -> Optional[str]:
    """Trim a cell and collapse whitespace; placeholders become None."""
    if value is None:
        return None
    s = re.sub(r"\s+", " ", str(value).replace("\n", " ")).strip()
    return None if s.lower() in MISSING else s


def ensure_field_definitions(cur, only: Optional[list] = None) -> int:
    """Create any missing gas field_definitions. Returns the number added.

    field_name is the FK target for cargo_gas_property_values, so a definition
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
            (name, display, dtype, unit, category, description, "Gas cargo data"),
        )
        added += cur.rowcount
    return added


def upsert_gas(cur, source_id: int, gas_name: str,
               country_of_origin: Optional[str] = None) -> Tuple[int, bool]:
    """Insert or find one cargo_gas row. Returns (id, created).

    Identity is (gas_name, source_id), the same per-source rule crude_oil uses:
    two sources naming the same gas keep their own rows and their own figures.
    """
    cur.execute("SELECT id FROM cargo_gas WHERE gas_name = %s AND source_id = %s",
                (gas_name, source_id))
    row = cur.fetchone()
    if row:
        return row[0], False
    cur.execute(
        "INSERT INTO cargo_gas (gas_name, source_id, country_of_origin, "
        "created_at, updated_at) VALUES (%s, %s, %s, now(), now()) RETURNING id",
        (gas_name, source_id, country_of_origin),
    )
    return cur.fetchone()[0], True


def upsert_property(cur, cargo_gas_id: int, source_id: int, field_name: str,
                    value: Optional[str], normalized_value: Optional[float] = None,
                    normalized_min: Optional[float] = None,
                    normalized_max: Optional[float] = None,
                    unit: Optional[str] = None, value_type: str = "number",
                    entered_by: str = "gas_loader",
                    source_page_ref: Optional[str] = None,
                    notes: Optional[str] = None) -> None:
    """Insert or refresh one (gas, source, field) value.

    ON CONFLICT updates rather than skipping, so a re-run picks up corrections
    to the source file. Values from other sources are untouched - source_id is
    part of the key, so one source can never overwrite another's figure.
    """
    cur.execute(
        """
        INSERT INTO cargo_gas_property_values
            (cargo_gas_id, source_id, field_name, value, normalized_value,
             normalized_min, normalized_max, unit, value_type, source_synonym_id,
             source_page_ref, as_of_date, entered_date, entered_by, entry_type,
             is_winning, conflict_flag, notes, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, %s, NULL,
                now(), %s, %s, %s, %s, %s, now(), now())
        ON CONFLICT (cargo_gas_id, source_id, field_name) DO UPDATE SET
            value            = EXCLUDED.value,
            normalized_value = EXCLUDED.normalized_value,
            normalized_min   = EXCLUDED.normalized_min,
            normalized_max   = EXCLUDED.normalized_max,
            unit             = EXCLUDED.unit,
            value_type       = EXCLUDED.value_type,
            source_page_ref  = EXCLUDED.source_page_ref,
            notes            = EXCLUDED.notes,
            updated_at       = now()
        """,
        (cargo_gas_id, source_id, field_name, value, normalized_value,
         normalized_min, normalized_max, unit, value_type, source_page_ref,
         entered_by, ENTRY_TYPE, IS_WINNING, CONFLICT_FLAG, notes),
    )
