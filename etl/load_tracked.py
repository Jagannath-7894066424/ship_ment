#!/usr/bin/env python3
"""
Run every ETL loader one at a time and record exactly what each one wrote.

run_all.sh runs the same loaders in the same order but only reports pass/fail.
This driver snapshots every table's row count before and after each step, so the
output answers "which file put how many rows into which table" - per loader,
per table, with a total at the end.

The per-step deltas are NET (rows after - rows before). A loader that upserts
existing rows shows 0 for a table it rewrote in place; the point of the report
is what the load ADDED, not how many statements ran.

Writes a machine-readable manifest to etl/data/load_manifest.json alongside the
console report.

Usage:
    python3 etl/load_tracked.py                 # run everything, stop on failure
    python3 etl/load_tracked.py -k              # keep going after a failure
    python3 etl/load_tracked.py --dry-run       # list the plan, run nothing
    python3 etl/load_tracked.py --only verwey   # only steps whose label matches
    python3 etl/load_tracked.py --from 18       # resume at step 18
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import psycopg2
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
INPUTS = "etl/data/inputs"
VERWEY_PDF = f"{INPUTS}/Dr Verweys Tank Cleaning Guide Pdf Book"
MANIFEST = REPO_ROOT / "etl" / "data" / "load_manifest.json"

# (label, argv) - same order as run_all.sh. Keep the two in step.
LOADERS: List[Tuple[str, List[str]]] = [
    # 1) foundation
    ("field_definition",            ["python3", "etl/field_definition.py"]),
    ("source",                      ["python3", "etl/source.py"]),
    # 2) core cargo
    ("master_loader (LARS)",        ["python3", "etl/master_loader.py", f"{INPUTS}/Lars Stole Birkeland - Chemical Cargo specifications - 2002.xlsx - CGOSPEC.csv"]),
    ("master_loader (CHEM)",        ["python3", "etl/master_loader.py", f"{INPUTS}/Unknown - Products CHEM - 1996.XLS"]),
    ("master_loader (USCG)",        ["python3", "etl/master_loader.py", f"{INPUTS}/USCG Chemical Data Guide For Bulk Shipment By Water [7th Edition 1990]_reviewed.csv"]),
    ("master_loader (IBC Code)",    ["python3", "etl/master_loader.py", f"{INPUTS}/IBC Code.xlsx"]),
    ("master_loader (Miracle)",     ["python3", "etl/master_loader.py"]),
    # 2b) Sittig
    ("sittig_handbook",             ["python3", "etl/sittig_handbook.py", f"{INPUTS}/Sittigs Handbook of Toxic & Hazardous Chemicals.csv"]),
    # 3) reactive groups + compatibility
    ("reactive_group",              ["python3", "etl/reactive_group.py"]),
    ("cargo_compatibility",         ["python3", "etl/cargo_compatibility.py"]),
    ("compatibility_exceptions",    ["python3", "etl/compatibility_exception_loader.py"]),
    ("link_cargo_reactive_groups",  ["python3", "etl/link_cargo_reactive_groups.py"]),
    ("group_details",               ["python3", "etl/master_cargo_chemical_group_details.py"]),
    # 5) operational requirements
    ("operational_requirements",    ["python3", "etl/cargo_operational_requirement.py"]),
    # 6) cleaning guides
    ("procedure_templates",         ["python3", "etl/proceduretemplate.py"]),
    ("verwey_cleaning",             ["python3", "etl/verwey_cleaning.py"]),
    ("drew_ameroid",                ["python3", "etl/drew_ameroid.py"]),
    # 6b) Verwey PDF Book
    ("verwey_pdf_book",             ["python3", "etl/verwey_pdf_book.py", f"{VERWEY_PDF} - Cargo details.csv"]),
    ("verwey_pdf_book_procedures",  ["python3", "etl/verwey_pdf_book_procedures.py", f"{VERWEY_PDF} Procdure Template.csv"]),
    ("verwey_pdf_book_families",    ["python3", "etl/verwey_pdf_book_families.py", f"{VERWEY_PDF}  _from-to procedure.csv"]),
    ("verwey_pdf_book_matrix",      ["python3", "etl/verwey_pdf_book_matrix.py", f"{VERWEY_PDF}  _from-to procedure.csv"]),
    # 7) DOT
    ("dot_hmt_extract",             ["python3", "etl/dot_hmt_extract.py"]),
    ("dot_hazmat_symbol",           ["python3", "etl/dot_hazmat_symbol_loader.py"]),
    ("cargo_dot_hazad",             ["python3", "etl/cargo_dot_hazad_loader.py"]),
]

BOLD, GREEN, RED, YELLOW, DIM, RESET = (
    ("\033[1m", "\033[1;32m", "\033[1;31m", "\033[1;33m", "\033[2m", "\033[0m")
    if sys.stdout.isatty() else ("", "", "", "", "", "")
)


def input_of(argv: List[str]) -> str:
    """The data file a step reads, for the report. '(loader default)' if implicit."""
    for arg in argv[2:]:
        if not arg.startswith("-"):
            return Path(arg).name
    return "(loader default)"


def connect():
    load_dotenv(REPO_ROOT / ".env")
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        sys.exit("DATABASE_URL not set (check .env)")
    return psycopg2.connect(dsn)


def table_names(cur) -> List[str]:
    cur.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public' AND table_type='BASE TABLE' "
        "AND table_name <> '_prisma_migrations' ORDER BY table_name"
    )
    return [r[0] for r in cur.fetchall()]


def snapshot(cur, tables: List[str]) -> Dict[str, int]:
    """Count every tracked table.

    The connection runs in autocommit mode on purpose. A snapshot taken inside
    an open transaction holds AccessShareLock on all 30 tables until commit,
    and several loaders open with TRUNCATE ... RESTART IDENTITY CASCADE, which
    needs AccessExclusiveLock. Holding the snapshot txn across the subprocess
    call therefore deadlocks the driver against its own child, and every later
    statement queues behind it. Keep this autocommit.
    """
    counts = {}
    for t in tables:
        cur.execute(f'SELECT count(*) FROM "{t}"')
        counts[t] = cur.fetchone()[0]
    return counts


def run_step(index: int, total: int, label: str, argv: List[str],
             cur, tables: List[str]) -> dict:
    src = input_of(argv)
    print(f"\n{BOLD}[{index:2d}/{total}] {label}{RESET}  {DIM}<- {src}{RESET}")

    before = snapshot(cur, tables)     # autocommit: locks released per statement
    t0 = time.time()
    proc = subprocess.run(argv, cwd=REPO_ROOT, capture_output=True, text=True)
    elapsed = time.time() - t0
    after = snapshot(cur, tables)

    delta = {t: after[t] - before[t] for t in tables if after[t] != before[t]}
    ok = proc.returncode == 0

    if ok:
        total_rows = sum(v for v in delta.values() if v > 0)
        print(f"{GREEN}     ✓ {elapsed:5.1f}s  +{total_rows:,} rows{RESET}")
    else:
        print(f"{RED}     ✗ FAILED (exit {proc.returncode}) after {elapsed:.1f}s{RESET}")
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-4:]
        for line in tail:
            print(f"{RED}       {line}{RESET}")

    for t, n in sorted(delta.items(), key=lambda kv: -abs(kv[1])):
        sign = "+" if n > 0 else ""
        colour = YELLOW if n < 0 else ""
        print(f"       {colour}{t:<40} {sign}{n:>10,}{RESET}")
    if ok and not delta:
        print(f"{DIM}       (no row changes - idempotent re-run or no DB writes){RESET}")

    return {
        "step": index,
        "label": label,
        "input_file": src,
        "command": " ".join(argv),
        "ok": ok,
        "exit_code": proc.returncode,
        "seconds": round(elapsed, 2),
        "rows_by_table": delta,
        "rows_total": sum(v for v in delta.values() if v > 0),
    }


def report(results: List[dict], tables: List[str], final: Dict[str, int]) -> None:
    print(f"\n{BOLD}{'=' * 100}{RESET}")
    print(f"{BOLD}PER-FILE INSERT REPORT{RESET}")
    print(f"{BOLD}{'=' * 100}{RESET}")
    print(f"{'#':>3}  {'loader':<28} {'input file':<46} {'rows':>10}  ok")
    print("-" * 100)
    for r in results:
        mark = f"{GREEN}✓{RESET}" if r["ok"] else f"{RED}✗{RESET}"
        name = r["input_file"]
        if len(name) > 45:
            name = name[:42] + "..."
        print(f'{r["step"]:>3}  {r["label"]:<28} {name:<46} {r["rows_total"]:>10,}  {mark}')

    # table x loader attribution
    print(f"\n{BOLD}ROWS BY TABLE (which loader filled it){RESET}")
    print("-" * 100)
    by_table: Dict[str, List[Tuple[str, int]]] = {}
    for r in results:
        for t, n in r["rows_by_table"].items():
            if n > 0:
                by_table.setdefault(t, []).append((r["label"], n))
    for t in sorted(by_table, key=lambda x: -final.get(x, 0)):
        contributors = ", ".join(f"{lbl} {n:,}" for lbl, n in by_table[t])
        print(f"  {t:<38} {final.get(t, 0):>10,}   {DIM}{contributors}{RESET}")

    empty = [t for t in tables if final.get(t, 0) == 0]
    if empty:
        print(f"\n{DIM}  still empty: {', '.join(empty)}{RESET}")

    total = sum(final.values())
    failed = [r for r in results if not r["ok"]]
    colour = GREEN if not failed else RED
    print(f"\n{colour}{BOLD}TOTAL: {total:,} rows across {len([t for t in tables if final.get(t,0)])} tables"
          f" | {len(results) - len(failed)}/{len(results)} steps ok{RESET}")
    if failed:
        print(f"{RED}  failed: {', '.join(r['label'] for r in failed)}{RESET}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-k", "--keep-going", action="store_true",
                    help="continue after a failing step")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, run nothing")
    ap.add_argument("--only", help="only steps whose label contains this text")
    ap.add_argument("--from", dest="start", type=int, default=1, help="resume at step N")
    args = ap.parse_args()

    steps = [(i, lbl, argv) for i, (lbl, argv) in enumerate(LOADERS, start=1)
             if i >= args.start and (not args.only or args.only.lower() in lbl.lower())]

    if args.dry_run:
        print(f"{BOLD}Plan ({len(steps)} steps):{RESET}")
        for i, lbl, argv in steps:
            print(f"  {i:2d}. {lbl:<28} <- {input_of(argv)}")
        return

    conn = connect()
    conn.autocommit = True             # never hold a txn across a loader subprocess
    results: List[dict] = []
    try:
        with conn.cursor() as cur:
            tables = table_names(cur)
            print(f"{BOLD}Tracking {len(tables)} tables across {len(steps)} loaders{RESET}")
            start_counts = snapshot(cur, tables)
            print(f"{DIM}starting rows: {sum(start_counts.values()):,}{RESET}")

            for i, lbl, argv in steps:
                res = run_step(i, len(LOADERS), lbl, argv, cur, tables)
                results.append(res)
                if not res["ok"] and not args.keep_going:
                    print(f"\n{RED}Stopping at step {i}. Fix it, or re-run with -k, "
                          f"or resume with --from {i}.{RESET}")
                    break

            final = snapshot(cur, tables)
            report(results, tables, final)

        MANIFEST.write_text(json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "steps": results,
            "final_row_counts": {t: n for t, n in sorted(final.items()) if n},
            "total_rows": sum(final.values()),
        }, indent=2))
        print(f"\n{DIM}manifest -> {MANIFEST.relative_to(REPO_ROOT)}{RESET}")
    finally:
        conn.close()

    sys.exit(1 if any(not r["ok"] for r in results) else 0)


if __name__ == "__main__":
    main()
