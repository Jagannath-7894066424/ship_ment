#!/usr/bin/env python3
"""
Merge one source into another, then delete the old source.

    python3 etl/common/merge_source.py --from 20 --into 4 --dry-run
    python3 etl/common/merge_source.py --from 20 --into 4

Steps (one transaction):
  1. cargo_chemical rows of --from whose canonical_name equals (case/space-insensitive)
     a row of --into are duplicates: every FK to them is repointed to the --into row,
     then the duplicate is deleted.
  2. Every column with an FK to source.id is changed from --from to --into.
  3. The --from source row is deleted.

Rows that would break a unique constraint after the repoint are dropped (the row that
already belongs to --into wins). That is only allowed in tables nothing else references;
a conflict in any other table aborts the run, because deleting there would cascade.
"""

import argparse
import os
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

CHEM = "cargo_chemical"


def fk_columns(cur, target: str):
    """(table, column, has_incoming_fks) for single-column FKs referencing target(id)."""
    cur.execute(
        """
        SELECT c.conrelid::regclass::text, a.attname
        FROM pg_constraint c
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
        WHERE c.contype = 'f' AND c.confrelid = %s::regclass AND array_length(c.conkey, 1) = 1
        """,
        (target,),
    )
    return cur.fetchall()


def referenced_tables(cur):
    cur.execute("SELECT DISTINCT confrelid::regclass::text FROM pg_constraint WHERE contype = 'f'")
    return {r[0] for r in cur.fetchall()}


def unique_indexes(cur, table: str):
    """Column lists of the unique indexes on table (expression / partial indexes skipped)."""
    cur.execute(
        """
        SELECT array_agg(a.attname ORDER BY k.ord)
        FROM pg_index i
        CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY k(attnum, ord)
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
        WHERE i.indrelid = %s::regclass AND i.indisunique AND i.indpred IS NULL
          AND NOT (0 = ANY(i.indkey::int2[]))
        GROUP BY i.indexrelid
        """,
        (table,),
    )
    return [r[0] for r in cur.fetchall()]


class Remap:
    """SQL fragments for one table's changing columns ({col: 'src' | 'chem'})."""

    def __init__(self, cols, src_from, src_into):
        self.cols = cols
        self.f, self.t = int(src_from), int(src_into)

    def new(self, alias, col):
        ref = f'{alias}."{col}"'
        if self.cols.get(col) == "src":
            return f"(CASE WHEN {ref} = {self.f} THEN {self.t} ELSE {ref} END)"
        if self.cols.get(col) == "chem":
            return f"COALESCE((SELECT m.new_id FROM _merge_map m WHERE m.old_id = {ref}), {ref})"
        return ref

    def changing(self, alias):
        parts = []
        for col, kind in self.cols.items():
            ref = f'{alias}."{col}"'
            parts.append(f"{ref} = {self.f}" if kind == "src"
                         else f"{ref} IN (SELECT old_id FROM _merge_map)")
        return "(" + " OR ".join(parts) + ")"


def conflict_where(table, remap, ucols):
    eq = " AND ".join(f"{remap.new('o', c)} = {remap.new('t', c)}" for c in ucols)
    return (f"{remap.changing('t')} AND EXISTS (SELECT 1 FROM {table} o WHERE o.ctid <> t.ctid "
            f"AND {eq} AND (NOT {remap.changing('o')} OR o.ctid < t.ctid))")


def process_table(cur, table, remap, is_referenced, dry_run, report):
    """Drop unique-conflict rows, then repoint. Returns False if the run must abort."""
    ok = True
    for ucols in unique_indexes(cur, table):
        if not set(ucols) & set(remap.cols):
            continue
        where = conflict_where(table, remap, ucols)
        cur.execute(f"SELECT count(*) FROM {table} t WHERE {where}")
        n = cur.fetchone()[0]
        if not n:
            continue
        if is_referenced:
            report.append(f"  ABORT  {table}: {n} rows collide on unique({', '.join(ucols)}) "
                          f"and the table is referenced by other tables (delete would cascade)")
            ok = False
            continue
        report.append(f"  drop   {table}: {n} duplicate rows on unique({', '.join(ucols)})")
        if not dry_run:
            cur.execute(f"DELETE FROM {table} t WHERE {where}")

    cur.execute(f"SELECT count(*) FROM {table} t WHERE {remap.changing('t')}")
    n = cur.fetchone()[0]
    if n:
        report.append(f"  update {table}: {n} rows ({', '.join(f'{c}' for c in remap.cols)})")
        if not dry_run and ok:
            sets = ", ".join(f'"{c}" = {remap.new("t", c)}' for c in remap.cols)
            cur.execute(f"UPDATE {table} t SET {sets} WHERE {remap.changing('t')}")
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="src_from", type=int, required=True)
    ap.add_argument("--into", dest="src_into", type=int, required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.src_from == args.src_into:
        sys.exit("--from and --into must differ")

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("SET lock_timeout = '10s'")
    host = conn.get_dsn_parameters()
    print(f"DB {host.get('host')}:{host.get('port')}/{host.get('dbname')}  "
          f"{'DRY RUN' if args.dry_run else 'WRITE'}")

    for sid in (args.src_from, args.src_into):
        cur.execute("SELECT name FROM source WHERE id = %s", (sid,))
        row = cur.fetchone()
        if row is None:
            sys.exit(f"source id {sid} not found")
        print(f"  source {sid}: {row[0]!r}")

    # Temp table only; ON COMMIT DROP, never touches shared tables.
    cur.execute(
        f"""
        CREATE TEMP TABLE _merge_map ON COMMIT DROP AS
        SELECT d.id AS old_id,
               (SELECT min(k.id) FROM {CHEM} k
                 WHERE k.source_id = %(into)s
                   AND lower(btrim(k.canonical_name)) = lower(btrim(d.canonical_name))) AS new_id
        FROM {CHEM} d WHERE d.source_id = %(from)s
        """,
        {"from": args.src_from, "into": args.src_into},
    )
    cur.execute("DELETE FROM _merge_map WHERE new_id IS NULL")
    cur.execute(f"SELECT count(*) FROM {CHEM} WHERE source_id = %s", (args.src_from,))
    n_from = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM _merge_map")
    n_dup = cur.fetchone()[0]
    print(f"  {CHEM} under {args.src_from}: {n_from}  (merge into existing {args.src_into} row: "
          f"{n_dup}, move as-is: {n_from - n_dup})")

    per_table = {}
    for t, c in fk_columns(cur, "source"):
        per_table.setdefault(t, {})[c] = "src"
    for t, c in fk_columns(cur, CHEM):
        per_table.setdefault(t, {})[c] = "chem"
    referenced = referenced_tables(cur)

    report, ok = [], True
    # Children first, cargo_chemical last (its duplicates must lose all references first).
    for table in sorted(t for t in per_table if t != CHEM):
        ok &= process_table(cur, table, Remap(per_table[table], args.src_from, args.src_into),
                            table in referenced, args.dry_run, report)

    chem_self = {c: k for c, k in per_table.get(CHEM, {}).items() if k == "chem"}
    if chem_self:
        ok &= process_table(cur, CHEM, Remap(chem_self, args.src_from, args.src_into),
                            True, args.dry_run, report)
    report.append(f"  delete {CHEM}: {n_dup} merged duplicates")
    report.append(f"  update {CHEM}: {n_from - n_dup} rows (source_id)")
    report.append(f"  delete source: id {args.src_from}")
    print("\n".join(report))

    if not ok:
        conn.rollback()
        sys.exit("Aborted: resolve the conflicts above first. Nothing written.")
    if args.dry_run:
        conn.rollback()
        print("Dry run: nothing written.")
        return

    cur.execute(f"DELETE FROM {CHEM} WHERE id IN (SELECT old_id FROM _merge_map)")
    cur.execute(f"UPDATE {CHEM} SET source_id = %s WHERE source_id = %s",
                (args.src_into, args.src_from))
    for t, c in fk_columns(cur, "source"):
        cur.execute(f'SELECT count(*) FROM {t} WHERE "{c}" = %s', (args.src_from,))
        left = cur.fetchone()[0]
        if left:
            conn.rollback()
            sys.exit(f"Aborted: {t}.{c} still has {left} rows on source {args.src_from}. Nothing written.")
    cur.execute("DELETE FROM source WHERE id = %s", (args.src_from,))
    conn.commit()
    print(f"Done: source {args.src_from} merged into {args.src_into} and deleted.")


if __name__ == "__main__":
    main()
