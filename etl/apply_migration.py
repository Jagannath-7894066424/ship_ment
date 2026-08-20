#!/usr/bin/env python3
"""
Apply a hand-written migration to the database.

This project does NOT use `prisma migrate` - the schema was built with
`prisma db push` and _prisma_migrations is empty, so the migration files under
prisma/migrations/ are applied by hand. This script is that hand.

Every migration in this repo is written to be idempotent (IF NOT EXISTS,
DO $$ ... guards), so re-running one is safe.

TRANSACTIONS
------------
Runs inside a transaction and rolls back on error, EXCEPT for migrations
containing ALTER TYPE ... ADD VALUE: PostgreSQL 12 cannot use an enum value that
was added in the still-open transaction, so those are detected and run with
autocommit instead. Their statements are individually idempotent, so a re-run
after a partial failure completes the job.

Usage:
    python3 etl/apply_migration.py prisma/migrations/<name>/migration.sql
    python3 etl/apply_migration.py --list
    python3 etl/apply_migration.py <path> --dry-run
"""

import argparse
import logging
import sys
from pathlib import Path

import psycopg2
from dotenv import dotenv_values

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = REPO_ROOT / "prisma" / "migrations"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("apply_migration")


def database_url() -> str:
    """Read .env directly rather than the environment.

    A stale DATABASE_URL exported in a shell would otherwise silently point a
    migration at a different database than the one .env names.
    """
    values = dotenv_values(REPO_ROOT / ".env")
    url = values.get("DATABASE_URL")
    if not url:
        sys.exit("Error: DATABASE_URL is not set in .env")
    return url


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", help="migration.sql to apply")
    ap.add_argument("--list", action="store_true", help="list migrations, newest last")
    ap.add_argument("--dry-run", action="store_true", help="print the SQL, change nothing")
    args = ap.parse_args()

    if args.list:
        for d in sorted(MIGRATIONS.iterdir()):
            if (d / "migration.sql").exists():
                print(d.name)
        return

    if not args.path:
        ap.error("give a migration.sql path, or --list")

    path = Path(args.path)
    if not path.is_absolute():
        path = REPO_ROOT / path
    if not path.exists():
        sys.exit(f"Error: {path} not found")

    sql = path.read_text()
    if args.dry_run:
        print(sql)
        return

    url = database_url()
    # An enum value added inside a transaction cannot be USED until commit
    # (PostgreSQL 12), so such a migration must not be wrapped in one.
    needs_autocommit = "ADD VALUE" in sql.upper()

    log.info("Applying %s", path.relative_to(REPO_ROOT))
    log.info("  target      : %s", url.split("@")[-1])
    log.info("  transaction : %s", "no (ALTER TYPE ADD VALUE present)"
             if needs_autocommit else "yes (rolls back on error)")

    conn = psycopg2.connect(url)
    conn.autocommit = needs_autocommit
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        if not needs_autocommit:
            conn.commit()
        log.info("✓ Applied.")
    except Exception:
        if not needs_autocommit:
            conn.rollback()
            log.exception("Failed - rolled back, nothing changed.")
        else:
            log.exception("Failed mid-way. The file is idempotent: re-run it.")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
