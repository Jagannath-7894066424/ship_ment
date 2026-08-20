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
    "procedure_template_steps", "procedure_template_instruction", "procedure_templates",
    "cleaning_process_step", "cleaning_process",
    "crude_oil_property_values", "crude_oil",
    "cargo_property_values", "cargo_family_group", "cargo_chemical", "field_definitions",
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

# 2b) Sittig's Handbook — health/toxicity reference. Its own loader rather than
#     master_loader because the CSV is badly quoted (unquoted commas push rows
#     past the 73 header columns); sittig_handbook.py repairs the two
#     recoverable cases and flags the rest in cargo_chemical.notes instead of
#     dropping them. Seeds its own field_definitions.
run "sittig_handbook             -> cargo, properties, synonyms" python3 etl/sittig_handbook.py "$INPUTS/Sittigs Handbook of Toxic & Hazardous Chemicals.csv"

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

# 6b) Dr Verwey PDF Book edition — a SEPARATE source from the two steps above
#     (431 cargoes / 279 procedure codes vs 390 / 37; the code systems differ,
#     e.g. AB/AC/BA here vs AA/BB/CC there). Kept apart by source, so nothing
#     above is affected.
#
#     Order is load-bearing:
#       chemicals  -> writes the verwey_cargo_number property the families step reads
#       procedures -> cleaning_process FKs (source_id, procedure_code) to these
#       families   -> the matrix keys on family ids
#       matrix     -> needs both families and procedures to exist
VERWEY_PDF="$INPUTS/Dr Verweys Tank Cleaning Guide Pdf Book"
run "verwey_pdf_book             -> cargo_chemical, properties"  python3 etl/verwey_pdf_book.py "$VERWEY_PDF - Cargo details.csv"
run "verwey_pdf_book_procedures  -> templates, steps, instructions" python3 etl/verwey_pdf_book_procedures.py "$VERWEY_PDF Procdure Template.csv"
run "verwey_pdf_book_families    -> cargo_family_group"          python3 etl/verwey_pdf_book_families.py "$VERWEY_PDF  _from-to procedure.csv"
run "verwey_pdf_book_matrix      -> cleaning_process (family)"   python3 etl/verwey_pdf_book_matrix.py "$VERWEY_PDF  _from-to procedure.csv"

# 7) DOT Hazardous Materials Table
run "dot_hmt_extract             -> JSON (no DB)"               python3 etl/dot_hmt_extract.py
run "dot_hazmat_symbol           -> cargo_hazard_data.dot_symbol" python3 etl/dot_hazmat_symbol_loader.py
run "cargo_dot_hazad             -> cargo_dot_hazad"            python3 etl/cargo_dot_hazad_loader.py

# 8) crude oil — a SEPARATE entity from cargo_chemical, with its own master and
#    property tables. There is no FK between the two branches; they meet only at
#    source (category 'oil' vs 'chemical'). Nothing above is affected.
#
#    The same crude appears in both sources under one name but with different
#    figures, and each source keeps its own row: identity is (oil_name,
#    source_id). The match report reconciles them without merging anything.
run "crude_oil_basic             -> crude_oil, properties"      python3 etl/crude_oil_basic.py "$INPUTS/Crude Oils-Prop.xls"
run "crude_oil_assay             -> crude_oil, properties"      python3 etl/crude_oil_assay.py "$INPUTS/Crudeoildata.XLS"
run "crude_oil_match_report      -> CSV (read-only)"            python3 etl/crude_oil_match_report.py

# 9) Shell tank-cleaning procedure templates.
#
#    The supplied workbook carries only the procedure definitions; the ordered
#    steps, requirements and instructions were supplied as a written spec and
#    are materialised into Excel by the build step so the importer reads data
#    and holds none. Build first, import second.
#
#    source_id comes from the import context, not the spreadsheet: a procedure
#    code means nothing without the document it was read from. source.py above
#    registers "Shell Tank Cleaning Procedure" (category oil).
run "shell_procedure_workbook    -> Excel (no DB)"              python3 etl/build_shell_procedure_workbook.py
run "shell_procedure_templates   -> templates, steps, reqs, instr" python3 etl/shell_procedure_templates.py

#    Shell Cargo Master: the refined products the matrix is keyed on. Lands in
#    the crude-oil tables (used here as the general oil-cargo master) with the
#    "Grade Names" column normalised into synonyms via crude_oil_synonym.
run "shell_cargo_master          -> crude_oil, properties, synonyms" python3 etl/shell_cargo_master.py

END=$(date +%s)
printf '\n%s=== finished: %d steps, %d failed, %ds total ===%s\n' \
  "$([[ $FAILED -eq 0 ]] && echo "$G" || echo "$R")" "$STEP" "$FAILED" "$((END - START))" "$N"
exit $(( FAILED > 0 ? 1 : 0 ))
