#!/usr/bin/env bash
#
# Shared machinery for the ETL runners (run_all.sh, run_chemical.sh, run_oil.sh).
# Sourced, never executed on its own.
#
# Provides:
#   parse_run_args "$@"   -> sets KEEP_GOING / FRESH, rejects unknown flags
#   run "<label>" <cmd…>  -> numbered step with timing, colour and failure policy
#   truncate_tables <t…>  -> TRUNCATE … RESTART IDENTITY CASCADE, with retries
#   finish                -> prints the summary and exits with the right code
#
# Step numbering and the failure count are shared state, so a parent script that
# sources a child runner keeps one continuous sequence rather than restarting
# at [1] for every branch.

# Colours (fall back to empty if not a TTY)
if [[ -t 1 ]]; then B=$'\033[1;34m'; G=$'\033[1;32m'; R=$'\033[1;31m'; Y=$'\033[1;33m'; N=$'\033[0m'
else B=; G=; R=; Y=; N=; fi

INPUTS="etl/data/inputs"
: "${STEP:=0}"
: "${FAILED:=0}"
: "${START:=$(date +%s)}"
: "${KEEP_GOING:=0}"
: "${FRESH:=0}"

parse_run_args() {
  for a in "$@"; do
    case "$a" in
      -k|--keep-going) KEEP_GOING=1 ;;
      --fresh)         FRESH=1 ;;
      *) printf 'unknown option: %s\n' "$a" >&2; exit 2 ;;
    esac
  done
}

run() {   # run "<label>" <command...>
  STEP=$((STEP + 1))
  local label="$1"; shift
  printf '\n%s[%2d] ▶ %s%s\n' "$B" "$STEP" "$label" "$N"
  local t0 t1
  t0=$(date +%s)
  if "$@"; then
    t1=$(date +%s)
    printf '%s     ✓ done: %s (%ds)%s\n' "$G" "$label" "$((t1 - t0))" "$N"
  else
    local code=$?
    printf '%s     ✗ FAILED: %s (exit %d)%s\n' "$R" "$label" "$code" "$N"
    FAILED=$((FAILED + 1))
    if [[ $KEEP_GOING -eq 0 ]]; then
      printf '\n%sAborting at step %d. Fix it, or re-run with -k to keep going.%s\n' "$R" "$STEP" "$N"
      exit 1
    fi
  fi
}

# TRUNCATE the named tables (CASCADE) so a reload starts clean. Tables that do
# not exist are skipped - the schema drifts between developer databases.
truncate_tables() {
  printf '%s!!! --fresh: TRUNCATING %d tables before reload (DESTRUCTIVE) !!!%s\n' "$R" "$#" "$N"
  TRUNCATE_TABLES="$*" python3 - <<'PY' || { printf '%s     ✗ truncate failed — aborting%s\n' "$R" "$N"; exit 1; }
import os, time, psycopg2
from dotenv import load_dotenv
load_dotenv(".env")
seen = list(dict.fromkeys(os.environ["TRUNCATE_TABLES"].split()))
url = os.environ["DATABASE_URL"]
last = None
for attempt in range(6):
    try:
        c = psycopg2.connect(url, connect_timeout=8); c.autocommit = True; cur = c.cursor()
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
        existing = {r[0] for r in cur.fetchall()}
        present = [t for t in seen if t in existing]
        missing = [t for t in seen if t not in existing]
        stmt = "TRUNCATE TABLE " + ", ".join(f'"{t}"' for t in present) + " RESTART IDENTITY CASCADE"
        cur.execute(stmt)
        print(f"  truncated {len(present)} tables" + (f" (skipped missing: {', '.join(missing)})" if missing else ""))
        cur.close(); c.close()
        break
    except Exception as e:
        last = e; print(f"  truncate attempt {attempt+1} failed: {e}"); time.sleep(3)
else:
    raise SystemExit(f"truncate failed: {last}")
PY
}

finish() {
  local end; end=$(date +%s)
  printf '\n%s=== finished: %d steps, %d failed, %ds total ===%s\n' \
    "$([[ $FAILED -eq 0 ]] && echo "$G" || echo "$R")" "$STEP" "$FAILED" "$((end - START))" "$N"
  exit $(( FAILED > 0 ? 1 : 0 ))
}
