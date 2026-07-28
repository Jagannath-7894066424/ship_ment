#!/usr/bin/env python3
"""
Read the file whose path is configured in the .env file and return it as JSON.

The path is taken from the DEFAULT_FILE variable in the .env file located in
this directory (you can override the variable name with --env-var). Both CSV
and Excel (.xlsx/.xls) files are supported.
"""

import argparse
import datetime as dt
import json
import logging
import os
import re
import sys
from contextlib import contextmanager
from pathlib import Path

import pandas as pd
import psycopg2
from psycopg2 import sql
from psycopg2.extras import RealDictCursor
from dotenv import dotenv_values, load_dotenv

logger = logging.getLogger(__name__)

# --- cargo_property_values insert ------------------------------------------
# Target tables.
CARGO_TABLE = "cargo_chemical"
FIELD_TABLE = "field_definitions"
VALUE_TABLE = "cargo_property_values"
SOURCE_TABLE = "source"
SYNONYM_TABLE = "synonyms"
CARGO_SYNONYM_TABLE = "cargo_synonym"

# Settable columns of cargo_property_values (id + created/updated are managed
# by the DB). Schema doc calls the PK `value_id`; the real column is `id`.
VALUE_COLUMNS = (
    "cargo_id", "field_name", "value", "source_id", "source_synonym_id",
    "source_page_ref", "as_of_date", "entered_date", "entered_by",
    "entry_type", "is_winning", "conflict_flag", "notes",
)

# Maps a CSV/record column -> field_definitions.field_name. Only the confident
# matches are pre-filled; extend this with the rest once you confirm them.
# Any column not listed here is skipped on insert.
COLUMN_TO_FIELD = {
    "Boiling point": "boiling_point_c",
    "Melting point": "melting_point_c",
    "Flash point": "flash_point_c",
    # UnNr is no longer a cargo_chemical column; UN numbers now live in the
    # dedicated cargo_un_number table (populated by master_loader).
    "Solubility": "water_solubility",
}

# Column positions in the CGOSPEC file.
# 0 = chemical (parent) name, 1 = commodity (child) name, 19 = notes.
# Everything between is a shared property, present on both parent and child.
PARENT_NAME_COL = 0
CHILD_NAME_COL = 1
NOTES_COL = 19
PROP_COLS = {
    2: "sp_gr",
    3: "temp",
    4: "correction_factor",
    5: "ship_type",
    6: "tank_type",
    7: "pollution_cat",
    8: "compliance",
    9: "uscg_compat",
    10: "boiling_point",
    11: "melting_point",
    12: "flash_point",
    13: "heat_adjacent",
    14: "heat_req_v",
    15: "heat_req_d",
    16: "colour",
    17: "solubility",
    18: "unnr",
}


def _clean(value) -> str | None:
    """Normalise a raw cell: blanks/NaN -> None, otherwise a stripped string."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _properties(row: tuple) -> dict:
    """Pull the shared property fields (+ notes) out of one row tuple."""
    props = {key: _clean(row[idx]) for idx, key in PROP_COLS.items()}
    props["notes"] = _clean(row[NOTES_COL])
    return props


def _merge_continuation(target: dict, row: tuple) -> None:
    """Fold a wrapped continuation row into the record it continues.

    The notes text is appended; any stray property cell fills a value the
    target is still missing.
    """
    note = _clean(row[NOTES_COL])
    if note:
        target["notes"] = f"{target['notes']} {note}" if target.get("notes") else note
    for idx, key in PROP_COLS.items():
        value = _clean(row[idx])
        if value and not target.get(key):
            target[key] = value


def build_nested(df: pd.DataFrame) -> list[dict]:
    logger.info("Calling build_nested for the data.")

    records: list[dict] = []
    current_parent: dict | None = None
    last_target: dict | None = None

    for row in df.itertuples(index=False, name=None):
        parent_name = _clean(row[PARENT_NAME_COL])
        child_name = _clean(row[CHILD_NAME_COL])

        if parent_name:
            current_parent = {
                "chemical_name": parent_name,
                **_properties(row),
                "commodities": [],
            }
            records.append(current_parent)
            last_target = current_parent

        elif child_name:
            child = {
                "commodity_name": child_name,
                **_properties(row),
            }
            # A commodity is a synonym/grade of its parent chemical, so inherit
            # the parent's specs wherever the commodity's own cell is blank.
            # Notes stay commodity-specific and are never inherited.
            if current_parent is not None:
                for key in PROP_COLS.values():
                    if child.get(key) is None:
                        child[key] = current_parent.get(key)

            if current_parent is None:
                current_parent = {
                    "chemical_name": child_name,
                    **_properties(row),
                    "commodities": [],
                }
                records.append(current_parent)
                last_target = current_parent
            else:
                current_parent["commodities"].append(child)
                last_target = child

        elif last_target is not None:
            _merge_continuation(last_target, row)

    logger.info(f"build_nested completed. Total parent chemicals: {len(records)}")

    return records


def load_path_from_env(env_var: str) -> Path:
    """Return the file path stored under `env_var` in the local .env file."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        sys.exit(f"Error: no .env file found at {env_path}")

    values = dotenv_values(env_path)
    raw = values.get(env_var)
    if not raw:
        sys.exit(f"Error: '{env_var}' is not set in {env_path}")

    file_path = Path(raw.strip().strip('"').strip("'"))
    if not file_path.exists():
        sys.exit(f"Error: file referenced by '{env_var}' does not exist: {file_path}")
    return file_path


def read_file(file_path: Path, sheet: str | None) -> pd.DataFrame:
    """Read a CSV or Excel file into a DataFrame."""
    suffix = file_path.suffix.lower()
    if suffix in (".xlsx", ".xls"):
        return pd.read_excel(file_path, sheet_name=sheet or 0)
    if suffix == ".csv":
        return pd.read_csv(file_path)
    sys.exit(f"Error: unsupported file type '{suffix}' (expected .csv, .xlsx or .xls)")


@contextmanager
def connect_db():
    """Yield a RealDictCursor connected via DATABASE_URL; commit on clean exit."""
    load_dotenv(Path(__file__).parent.parent / ".env")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        sys.exit("Error: DATABASE_URL not set (checked .env).")
    conn = psycopg2.connect(db_url, cursor_factory=RealDictCursor)
    try:
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_cargo_by_name(cur, chemical_name: str) -> dict | None:
    """Return the cargo_chemical row whose canonical_name matches (case/space-insensitive)."""
    cur.execute(
        sql.SQL("SELECT * FROM {} WHERE lower(btrim(canonical_name)) = lower(btrim(%s)) LIMIT 1")
        .format(sql.Identifier(CARGO_TABLE)),
        (chemical_name,),
    )
    return cur.fetchone()


def get_field_definitions(cur) -> list[dict]:
    """Return every field_definitions row, ordered by display_order then name."""
    cur.execute(
        sql.SQL("SELECT * FROM {} ORDER BY display_order NULLS LAST, field_name")
        .format(sql.Identifier(FIELD_TABLE))
    )
    return cur.fetchall()


def normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace (matches import_synonyms)."""
    s = text.lower()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def get_cargo_columns(cur) -> set[str]:
    """Return the set of column names on the cargo_chemical table."""
    cur.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        """,
        (CARGO_TABLE,),
    )
    return {r["column_name"] for r in cur.fetchall()}


def get_source_by_filename(cur, file_name: str) -> dict | None:
    """Find the source whose name/file_path best matches the file name.

    Matching is done on normalized text (lowercased, punctuation stripped) so
    that em-dash vs hyphen and extra suffixes like '.xlsx - CGOSPEC' don't break
    it. A source matches when its normalized name is contained in (or contains)
    the normalized file name; otherwise we fall back to token overlap.
    """
    target = normalize(Path(file_name).stem)
    target_tokens = set(target.split())

    cur.execute(sql.SQL("SELECT * FROM {} ORDER BY id").format(sql.Identifier(SOURCE_TABLE)))
    best, best_score = None, 0
    for src in cur.fetchall():
        candidates = [src.get("name") or ""]
        if src.get("file_path"):
            candidates.append(Path(src["file_path"]).stem)
        for cand in candidates:
            cnorm = normalize(cand)
            if not cnorm:
                continue
            if cnorm in target or target in cnorm:
                score = len(cnorm)               # strong: full containment
            else:
                overlap = target_tokens & set(cnorm.split())
                # require a meaningful overlap to avoid spurious matches
                score = len(overlap) if len(overlap) >= max(2, len(cnorm.split()) // 2) else 0
            if score > best_score:
                best, best_score = src, score
    return best


def get_synonym_id(cur, cargo_id: int, text: str) -> int | None:
    """Return the synonyms.id for `text`, preferring one linked to this cargo."""
    norm = normalize(text)
    # Prefer a synonym actually linked to this cargo via cargo_synonym.
    cur.execute(
        sql.SQL(
            """
            SELECT s.id FROM {syn} s
            JOIN {cs} cs ON cs.synonym_id = s.id
            WHERE cs.cargo_id = %s
              AND (s.normalized_text = %s OR lower(btrim(s.synonym_text)) = lower(btrim(%s)))
            LIMIT 1
            """
        ).format(syn=sql.Identifier(SYNONYM_TABLE), cs=sql.Identifier(CARGO_SYNONYM_TABLE)),
        (cargo_id, norm, text),
    )
    row = cur.fetchone()
    if row:
        return row["id"]
    # Fallback: any synonym matching the text.
    cur.execute(
        sql.SQL(
            "SELECT id FROM {} WHERE normalized_text = %s "
            "OR lower(btrim(synonym_text)) = lower(btrim(%s)) LIMIT 1"
        ).format(sql.Identifier(SYNONYM_TABLE)),
        (norm, text),
    )
    row = cur.fetchone()
    return row["id"] if row else None


def values_match(observed, stored) -> bool:
    """True if the observed CSV value equals the value mirrored in cargo_chemical."""
    if stored is None:
        return False
    if observed is None:
        return False
    try:  # numeric comparison first ("12.0" == 12)
        return float(str(observed)) == float(str(stored))
    except (ValueError, TypeError):
        return str(observed).strip().lower() == str(stored).strip().lower()


def _fmt_value(value) -> str:
    """Stringify a CSV value; render integer-valued floats without the .0."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def insert_property_value(cur, *, cargo_id, field_name, value, source_id,
                          source_synonym_id=None, source_page_ref=None,
                          as_of_date=None, entered_date=None, entered_by="system",
                          entry_type="ingested", is_winning=True,
                          conflict_flag=False, notes=None) -> int:
    """INSERT one cargo_property_values row and return its new id."""
    row = {
        "cargo_id": cargo_id, "field_name": field_name, "value": value,
        "source_id": source_id, "source_synonym_id": source_synonym_id,
        "source_page_ref": source_page_ref, "as_of_date": as_of_date,
        "entered_date": entered_date or dt.date.today(), "entered_by": entered_by,
        "entry_type": entry_type, "is_winning": is_winning,
        "conflict_flag": conflict_flag, "notes": notes,
    }
    cur.execute(
        sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING id").format(
            sql.Identifier(VALUE_TABLE),
            sql.SQL(", ").join(sql.Identifier(c) for c in VALUE_COLUMNS),
            sql.SQL(", ").join(sql.Placeholder() for _ in VALUE_COLUMNS),
        ),
        [row[c] for c in VALUE_COLUMNS],
    )
    return cur.fetchone()["id"]


def insert_records(records: list[dict], file_name: str,
                   source_id: int | None = None, dry_run: bool = False) -> None:
    """Ingest the records into cargo_property_values.

    Per the agreed rules:
      * field_name comes from field_definitions (and must be a cargo_chemical
        column); the record's value for that field is mapped via COLUMN_TO_FIELD.
      * cargo_id + the cargo's stored data come from cargo_chemical, looked up
        by the chemical name (Unnamed: 0 / chemical_name).
      * source_id is resolved from the source table by the file name (unless
        passed explicitly).
      * if the record is a commodity (COMMODITIES set), source_synonym_id is the
        matching synonyms.id; otherwise NULL (canonical name).
      * is_winning = the observed value equals what's mirrored in cargo_chemical.

    A (cargo_id, field_name, source_synonym_id) already inserted this run is
    skipped so forward-filled rows don't duplicate.
    """
    with connect_db() as cur:
        # 5. Resolve the source by file name (unless caller forced one).
        if source_id is None:
            src = get_source_by_filename(cur, file_name)
            if not src:
                sys.exit(f"Error: no source matched file name {file_name!r}; pass --source-id")
            source_id = src["id"]
            logger.info("Resolved source_id=%s (%s) from file name", source_id, src.get("name"))

        # 2. field_definitions field_names, restricted to real cargo_chemical columns.
        field_names = {f["field_name"] for f in get_field_definitions(cur)}
        cargo_cols = get_cargo_columns(cur)
        active: dict[str, str] = {}
        for column, field_name in COLUMN_TO_FIELD.items():
            if field_name not in field_names:
                logger.warning("'%s' not in %s -> skip", field_name, FIELD_TABLE)
            elif field_name not in cargo_cols:
                logger.warning("'%s' is not a %s column -> skip", field_name, CARGO_TABLE)
            else:
                active[column] = field_name
        logger.info("Active field mappings: %s", active)

        inserted = winning = 0
        seen: set[tuple[int, str, int | None]] = set()
        missing_cargo: set[str] = set()

        for rec in records:
            # 3. cargo id + data by chemical name.
            name = rec.get("chemical_name") or rec.get("Unnamed: 0")
            if not name:
                continue
            cargo = get_cargo_by_name(cur, name)
            if not cargo:
                missing_cargo.add(name)
                continue
            cargo_id = cargo["id"]

            # 5b. commodity -> synonym id (canonical name -> NULL).
            commodity = rec.get("commodity_name") or rec.get("COMMODITIES")
            synonym_id = get_synonym_id(cur, cargo_id, commodity) if commodity else None

            # 4. one value row per mapped field that holds a value.
            for column, field_name in active.items():
                value = rec.get(column)
                if value in (None, ""):
                    continue
                key = (cargo_id, field_name, synonym_id)
                if key in seen:
                    continue
                seen.add(key)

                # 6. is_winning = matches the value mirrored in cargo_chemical.
                is_winning = values_match(value, cargo.get(field_name))
                winning += int(is_winning)
                if not dry_run:
                    insert_property_value(
                        cur, cargo_id=cargo_id, field_name=field_name,
                        value=_fmt_value(value), source_id=source_id,
                        source_synonym_id=synonym_id, is_winning=is_winning,
                    )
                inserted += 1

        verb = "Would insert" if dry_run else "Inserted"
        logger.info("%s %d value rows into %s (%d marked is_winning)",
                    verb, inserted, VALUE_TABLE, winning)
        if missing_cargo:
            logger.warning("%d chemical name(s) had no cargo_chemical match, e.g. %s",
                           len(missing_cargo), sorted(missing_cargo)[:5])


def main() -> None:

    print("Starting main()")
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-var", default="DEFAULT_FILE",
                        help="Name of the .env variable holding the file path (default: DEFAULT_FILE)")
    parser.add_argument("--sheet", default=None,
                        help="Sheet name to read for Excel files (default: first sheet)")
    parser.add_argument("--output", default=None,
                        help="Write JSON to this file instead of stdout")
    parser.add_argument("--indent", type=int, default=2,
                        help="JSON indentation (default: 2)")
    parser.add_argument("--log", action="store_true",
                        help="Log every record (and a summary) instead of printing JSON")
    parser.add_argument("--nested", action="store_true",
                        help="Group rows into parent chemicals with a nested commodities list")
    parser.add_argument("--limit", type=int, default=None,
                        help="Only output the first N records (e.g. --limit 2)")
    parser.add_argument("--insert", action="store_true",
                        help="Insert the records' mapped properties into cargo_property_values")
    parser.add_argument("--source-id", type=int, default=None,
                        help="source_id to stamp on rows (default: resolved from the file name)")
    parser.add_argument("--db-dry-run", action="store_true",
                        help="With --insert: report what would be inserted, write nothing")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    file_path = load_path_from_env(args.env_var)
    print(file_path,"file path")
    print(args,"file path")
    
    if args.nested:
        # Read everything as text so values like ".920" or "1.720" are kept
        # verbatim and blanks become real empties rather than NaN.
        df = pd.read_csv(file_path, dtype=str, keep_default_na=False, header=0)
        records = build_nested(df)
        
        logger.info(len(records),"====================records")
    else:
        df = read_file(file_path, args.sheet)
        # Only the parent row of each chemical carries the name and the specs;
        # the commodity rows beneath it are blank. Group by chemical (a new
        # group starts wherever the name column is filled), then forward-fill so
        # every commodity row inherits its chemical's name and properties.
        # COMMODITIES (col 1) and the notes column (last) stay per-row.
        group = df.iloc[:, 0].notna().cumsum()
        df.iloc[:, 0] = df.iloc[:, 0].ffill()
        prop_cols = df.columns[2:-1]  # everything between COMMODITIES and notes
        df[prop_cols] = df.groupby(group)[prop_cols].ffill()
        # NaN -> null so the output is valid JSON. Cast to object first, otherwise
        # float columns coerce None back to NaN.
        records = df.astype(object).where(pd.notnull(df), None).to_dict(orient="records")
        print(len(records),"=====================>")
        
    if args.limit is not None:
        records = records[:args.limit]
            # logger.info(len(records),"====================records else")

    if args.insert:
        insert_records(records, file_name=file_path.name,
                       source_id=args.source_id, dry_run=args.db_dry_run)
        return


    # if args.log:
        # logger.info("Loaded %d records from %s", len(records), file_path)
        # for i, record in enumerate(records, start=1):
            # logger.info("Record %d/%d: %s", i, len(records),
                        # json.dumps(record, ensure_ascii=False, default=str))
        # logger.info("Done logging %d records", len(records))
        # return

    payload = json.dumps(records)

    if args.output:
        Path(args.output).write_text(payload, encoding="utf-8")
        # print(f"Wrote {len(records)} records to {args.output} ====================>")
    else:
         print(json.dumps(records[185], indent=2, ensure_ascii=False))
         print(f"\nTotal records: {len(records)}")



if __name__ == "__main__":
    main()
