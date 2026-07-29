#!/usr/bin/env bash
#
# Run every ETL loader in the correct order, one by one, printing progress and
# which file just finished — similar to `yarn prisma:push`.
#
#   bash etl/run_all.sh          # run everything (idempotent loaders; DB may be populated)
#   yarn etl                     # same, via package.json
#   bash etl/run_all.sh -k       # keep going after a failing step (default: stop)
#   bash etl/run_all.sh --fresh  # DESTRUCTIVE: wipe all ETL tables, then rebuild.
#                                # For bootstrapping a NEW/EMPTY db — NOT a live one.
#
# Reads DATABASE_URL + input files exactly like the individual loaders (via .env
# and etl/data/inputs). Run from anywhere — it cd's to the repo root itself.

set -uo pipefail
cd "$(dirname "$0")/.."   # repo root

KEEP_GOING=0
FRESH=0
for a in "$@"; do
  case "$a" in
    -k|--keep-going) KEEP_GOING=1 ;;
    --fresh)         FRESH=1 ;;
    *) printf 'unknown option: %s\n' "$a" >&2; exit 2 ;;
  esac
done

# Colours (fall back to empty if not a TTY)
if [[ -t 1 ]]; then B=$'\033[1;34m'; G=$'\033[1;32m'; R=$'\033[1;31m'; Y=$'\033[1;33m'; N=$'\033[0m'
else B=; G=; R=; Y=; N=; fi

INPUTS="etl/data/inputs"
STEP=0
FAILED=0
START=$(date +%s)

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

# --fresh: wipe every ETL-managed table (CASCADE) so the reload starts clean.
# `source` and `dot_hazmat_symbol` are intentionally kept (source.py is
# skip-if-exists; the symbol legend is a static migration seed).
if [[ $FRESH -eq 1 ]]; then
  printf '%s!!! --fresh: TRUNCATING all ETL tables before reload (DESTRUCTIVE) !!!%s\n' "$R" "$N"
  python3 - <<'PY' || { printf '%s     ✗ truncate failed — aborting%s\n' "$R" "$N"; exit 1; }
import os, time, psycopg2
from dotenv import load_dotenv
load_dotenv(".env")
TABLES = [
    "cargo_property_values", "cargo_un_number", "cargo_synonym", "synonyms",
    "master_cargo_chemical_group_details", "cargo_hazard_data", "cargo_dot_hazad",
    "cargo_reactive_group", "compatibility", "compatibility_exception",
    "reactive_groups", "cargo_operational_requirement", "operational_requirement",
    "procedure_template_steps", "procedure_templates",
    "cleaning_process_step", "cleaning_process",
    "cargo_property_values", "cargo_chemical", "field_definitions",
]
seen = list(dict.fromkeys(TABLES))
url = os.environ["DATABASE_URL"]
last = None
for attempt in range(6):
    try:
        c = psycopg2.connect(url, connect_timeout=8); c.autocommit = True; cur = c.cursor()
        # keep only tables that actually exist (schema drifts between DBs)
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
fi

printf '%s=== ETL: loading all sources in order ===%s\n' "$Y" "$N"

# 1) foundation (vocabulary + source authorities) — must be first
run "field_definition            -> field_definitions"          python3 etl/field_definition.py
run "source                      -> source"                     python3 etl/source.py

# 2) core cargo via master_loader (upserts; handles the LARS/CHEM/USCG formats,
#    including duplicate canonical-names that break the plain-INSERT loaders).
#    master_loader (lars) also loads the synonyms + properties, so the separate
#    cargo_synonym / import_synonyms scripts are not needed here.
run "master_loader (LARS)        -> cargo_chemical, synonyms, props" python3 etl/master_loader.py "$INPUTS/Lars Stole Birkeland - Chemical Cargo specifications - 2002.xlsx - CGOSPEC.csv"
run "master_loader (CHEM)        -> cargo, properties"          python3 etl/master_loader.py "$INPUTS/Unknown - Products CHEM - 1996.XLS"
run "master_loader (USCG)        -> cargo, hazard, properties"  python3 etl/master_loader.py "$INPUTS/USCG Chemical Data Guide For Bulk Shipment By Water [7th Edition 1990]_reviewed.csv"
run "master_loader (IBC Code)    -> cargo (identity/carriage)"  python3 etl/master_loader.py "$INPUTS/IBC Code.xlsx"
run "master_loader (Miracle)     -> cargo, cleaning_process"    python3 etl/master_loader.py

# 3) reactive groups + compatibility. reactive_group first (plain insert into an
#    empty table), then cargo_compatibility (ON CONFLICT tolerates overlap).
#    group_details FKs to reactive_groups.group_code, so it must run AFTER them.
run "reactive_group              -> reactive_groups"            python3 etl/reactive_group.py
run "cargo_compatibility         -> compatibility"              python3 etl/cargo_compatibility.py
run "compatibility_exceptions    -> compatibility_exception"    python3 etl/compatibility_exception_loader.py
run "link_cargo_reactive_groups  -> cargo_reactive_group"       python3 etl/link_cargo_reactive_groups.py
run "group_details               -> master_cargo_chemical_group_details" python3 etl/master_cargo_chemical_group_details.py

# 5) operational requirements (IBC)
run "operational_requirements    -> operational_requirement"    python3 etl/cargo_operational_requirement.py

# 6) cleaning guides
run "procedure_templates         -> procedure_templates"        python3 etl/proceduretemplate.py
run "verwey_cleaning             -> cleaning_process (matrix)"  python3 etl/verwey_cleaning.py
run "drew_ameroid                -> procedure_templates + pairs" python3 etl/drew_ameroid.py

# 7) DOT Hazardous Materials Table
run "dot_hmt_extract             -> JSON (no DB)"               python3 etl/dot_hmt_extract.py
run "dot_hazmat_symbol           -> cargo_hazard_data.dot_symbol" python3 etl/dot_hazmat_symbol_loader.py
run "cargo_dot_hazad             -> cargo_dot_hazad"            python3 etl/cargo_dot_hazad_loader.py

END=$(date +%s)
printf '\n%s=== finished: %d steps, %d failed, %ds total ===%s\n' \
  "$([[ $FAILED -eq 0 ]] && echo "$G" || echo "$R")" "$STEP" "$FAILED" "$((END - START))" "$N"
exit $(( FAILED > 0 ? 1 : 0 ))
