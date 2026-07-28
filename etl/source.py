import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import psycopg2
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("source_sync")

DEFAULT_JSON = Path(__file__).parent / "data" / "source.json"

# JSON key -> source table column
RANK_KEYS = (
    "rank_regulatory",
    "rank_cleaning",
    "rank_compatibility",
    "rank_physical",
    "rank_health",
)

# Placeholder cells that should be stored as SQL NULL rather than literal text.
_PLACEHOLDERS = {"", "-", "—", "–", "n/a", "na", "none", "?"}


def clean(value: Any) -> Optional[str]:
    """Trim a cell; treat placeholders/empties as None (SQL NULL)."""
    if value is None:
        return None
    s = str(value).strip()
    if s.lower() in _PLACEHOLDERS:
        return None
    return s


def to_int(value: Any) -> Optional[int]:
    """Coerce a rank cell to int, or None when absent/non-numeric."""
    s = clean(value)
    if s is None:
        return None
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return None


def normalize_name(s: str) -> str:
    """Lowercase and collapse whitespace so trivial differences don't duplicate."""
    return re.sub(r"\s+", " ", s.strip().lower())


def load_records(path: Path) -> list:
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        sys.exit(f"Error: expected a JSON array in {path}, got {type(data).__name__}.")
    return data


def load_existing_names(cur) -> set:
    """Return the set of normalized names already in the source table."""
    cur.execute("SELECT name FROM source")
    return {normalize_name(row[0]) for row in cur.fetchall() if row[0]}


def create_source(cur, record: Dict[str, Any]) -> int:
    """Insert a new source row from a json record and return its id."""
    cols: Dict[str, Any] = {
        "name": clean(record.get("Source")),
        "edition": clean(record.get("Edition")),
        "source_type": clean(record.get("Type")),
        "notes": "Auto-created from source.json",
    }
    for key in RANK_KEYS:
        cols[key] = to_int(record.get(key))

    col_names = list(cols.keys())
    placeholders = ", ".join(["%s"] * len(col_names))
    cur.execute(
        f"INSERT INTO source ({', '.join(col_names)}, date_ingested, created_at, updated_at) "
        f"VALUES ({placeholders}, now(), now(), now()) RETURNING id",
        [cols[c] for c in col_names],
    )
    return cur.fetchone()[0]


def main():
    parser = argparse.ArgumentParser(description="Sync source.json into the source table.")
    parser.add_argument("--file", default=str(DEFAULT_JSON), help="path to the JSON file")
    parser.add_argument("--dry-run", action="store_true",
                        help="report decisions, write nothing")
    args = parser.parse_args()

    path = Path(args.file)
    if not path.is_file():
        sys.exit(f"Error: file not found: {path}")

    load_dotenv(Path(__file__).parent.parent / ".env")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        sys.exit("Error: DATABASE_URL not set (checked .env).")

    records = load_records(path)
    log.info("Loaded %d records from %s", len(records), path.name)

    log.info("Connecting to database")
    conn = psycopg2.connect(db_url)
    n_created = n_skipped = n_invalid = 0
    try:
        with conn.cursor() as cur:
            existing = load_existing_names(cur)
            log.info("Loaded %d existing source names", len(existing))

            for i, record in enumerate(records, 1):
                name = clean(record.get("Source"))
                if not name:
                    log.warning("[%d] record has no 'Source' name -> skipping", i)
                    n_invalid += 1
                    continue

                # Verify by name: exists -> skip, else create.
                if normalize_name(name) in existing:
                    log.info("[%d] EXISTS -> skip: %r", i, name)
                    n_skipped += 1
                    continue

                if args.dry_run:
                    log.info("[%d] NEW -> would create: %r", i, name)
                else:
                    new_id = create_source(cur, record)
                    log.info("[%d] CREATED id=%s: %r", i, new_id, name)
                existing.add(normalize_name(name))  # avoid dup within this run
                n_created += 1

            log.info("=" * 60)
            log.info("SUMMARY (%s)", "DRY-RUN" if args.dry_run else "COMMIT")
            log.info("  created : %d", n_created)
            log.info("  skipped : %d (already exist)", n_skipped)
            log.info("  invalid : %d (no name)", n_invalid)
            log.info("=" * 60)

            if args.dry_run:
                log.info("Dry run: nothing written.")
                conn.rollback()
                return
            conn.commit()
            log.info("✓ Committed.")
    except Exception:
        log.exception("Sync failed - rolling back")
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
