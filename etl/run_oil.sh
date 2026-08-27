#!/usr/bin/env bash
#
# Load the crude_oil branch: the oil master and its assay properties, then one
# block per tank-cleaning guide - Shell (refined-product master + regime codes +
# matrix), Energy Institute HM 50, BP, and Dr Verwey's oil edition. Each guide is
# its own source row with its own codes and its own cargo names; they share the
# crude_oil / procedure_templates / cleaning_process tables and nothing else.
#
#   bash etl/run_oil.sh          # load the oil branch
#   yarn etl:oil                 # same, via package.json
#   bash etl/run_oil.sh -k       # keep going after a failing step
#   bash etl/run_oil.sh --fresh  # DESTRUCTIVE: wipe the oil tables first
#
# Assumes the foundation steps (etl/common/field_definition.py, etl/common/source.py)
# have already run - run_all.sh does them first. Run this alone only against a DB
# that already has field_definitions and source populated.
#
# crude_oil is a SEPARATE entity from cargo_chemical, with its own master and
# property tables. There is no FK between the two branches; they meet only at
# source (category 'oil' vs 'chemical'), so nothing here affects the chemical
# branch and this script can be run on its own.

set -uo pipefail
cd "$(dirname "$0")/.."   # repo root
source etl/_run_lib.sh
parse_run_args "$@"

# Oil-branch tables only. `synonyms` is deliberately NOT in this list - it is the
# shared vocabulary the chemical branch also links to, and crude_oil_synonym
# (the link that carries the oil-side source) is truncated instead.
if [[ $FRESH -eq 1 ]]; then
  # cleaning_process is SHARED with the chemical branch, so it must not be
  # truncated - only the OIL rows are deleted, and they go first. Their
  # from_cargo_id / to_cargo_id have no FK to crude_oil (the columns are
  # polymorphic), so wiping crude_oil while leaving them behind would leave rows
  # pointing at ids that no longer exist and nothing would complain.
  printf '%s!!! --fresh: deleting OIL rows from cleaning_process (DESTRUCTIVE) !!!%s\n' "$R" "$N"
  python3 - <<'PYDEL' || { printf '%s     x delete failed - aborting%s\n' "$R" "$N"; exit 1; }
import os, psycopg2
from dotenv import load_dotenv
load_dotenv(".env")
conn = psycopg2.connect(os.environ["DATABASE_URL"])
with conn, conn.cursor() as cur:
    cur.execute("DELETE FROM cleaning_process WHERE cargo_type = 'OIL'")
    print(f"     deleted {cur.rowcount} OIL cleaning_process row(s)")
conn.close()
PYDEL
  truncate_tables crude_oil_property_values crude_oil_synonym crude_oil
fi

printf '%s=== ETL: crude_oil branch ===%s\n' "$Y" "$N"

# 1) crude oil masters.
#
#    The same crude appears in both sources under one name but with different
#    figures, and each source keeps its own row: identity is (oil_name,
#    source_id). The match report reconciles them without merging anything.
#    One loader reads both sheets (--source picks which); they stay two steps
#    here so the row counts stay attributable to a source.
run "crude_oil basic             -> crude_oil, properties"      python3 etl/oil/crude_oil.py --source basic "$INPUTS/Crude Oils-Prop.xls"
run "crude_oil assay             -> crude_oil, properties"      python3 etl/oil/crude_oil.py --source assay "$INPUTS/Crudeoildata.XLS"
run "crude_oil_match_report      -> CSV (read-only)"            python3 etl/oil/crude_oil_match_report.py

# 2) Shell tank-cleaning procedures, from two extracts of the same guide:
#      - the 11 letter codes (WD, CW, NC ...) the pre-cargo matrix is keyed on
#      - the 30 numbered cleaning regimes, whose steps reference WD rather than
#        restating it, and whose conditional instructions stay conditional
#    Both extracts belong to the 2016 guide, named with --source: matching it
#    from the filename stopped working once the White Oil guide below joined the
#    source table, because "Shell Tank cleaning Procedure From Excel.csv" shares
#    exactly three identity words with each of them and the loader treats a tie
#    as an error rather than a coin toss.
run "shell_procedure_templates   -> templates, steps, instructions, notes" python3 etl/oil/shell_procedure_templates.py \
    --source "Shell Tank Cleaning Guide 2016.pdf"

# 3) Shell Cargo Master: the refined products the matrix is keyed on. Lands in
#    the crude-oil tables (used here as the general oil-cargo master) with the
#    "Grade Names" column normalised into synonyms via crude_oil_synonym.
run "shell_cargo_master          -> crude_oil, properties, synonyms" python3 etl/oil/shell_cargo_master.py

# 4) Shell cargo-to-cargo regime matrix: which of the 30 numbered regimes applies
#    between a discharged cargo and the next one loaded. Joins to crude_oil on
#    aggregated_name, so it MUST run after shell_cargo_master (which populates
#    that column) and after shell_procedure_templates (which defines the codes it
#    links to). Writes cleaning_process with cargo_type = OIL.
run "shell_cargo_matrix          -> cleaning_process (OIL)" python3 etl/oil/shell_cargo_matrix.py

# 5) Energy Institute HM 50, from four extracts of one guide. Independent of the
#    Shell steps above - a separate source with its own codes and its own cargo
#    names - but internally ordered: the matrix needs the codes to link to, and
#    the guidance attaches to the crude_oil rows the matrix creates.
run "hm50_procedure_templates    -> templates, steps (OIL)"  python3 etl/oil/hm50_procedure_templates.py
run "hm50_cargo_matrix           -> crude_oil, cleaning_process (OIL)" python3 etl/oil/hm50_cargo_matrix.py
run "hm50_cargo_guidance         -> crude_oil_property_values" python3 etl/oil/hm50_cargo_guidance.py

# 6) Shell White Oil Tank Cleaning Guide, from six extracts of one document. A
#    SEPARATE source from the 2016 pre-cargo matrix above: both guides number
#    their codes 1, 2, 3 ... and the numbers mean different things, so one source
#    row would have one guide's code 2 overwrite the other's. Internally ordered:
#    the matrix resolves cargo names against the rows the cargo loader creates
#    and codes against the templates defined before it. Writes
#    crude_oil_compatibility as well as cleaning_process - the grid states both
#    which code to run and whether the transition is allowed at all.
run "shell_white_oil_cargo      -> crude_oil, synonyms" python3 etl/oil/shell_white_oil_cargo.py
run "shell_white_oil_templates  -> templates, steps, requirements (OIL)" python3 etl/oil/shell_white_oil_procedure_templates.py
run "shell_white_oil_matrix     -> cleaning_process, crude_oil_compatibility" python3 etl/oil/shell_white_oil_matrix.py

# 7) BP Tank Cleaning Guide, from four extracts of one guide. Its own source and
#    its own colour-coded codes (GREY/CYAN/RED/BLACK), independent of Shell and
#    HM 50. Internally ordered: the matrix resolves cargo names against the rows
#    bp_cargo.py creates and codes against the templates defined before it.
run "bp_procedure_templates      -> templates, steps, requirements (OIL)" python3 etl/oil/bp_procedure_templates.py
run "bp_cargo                    -> crude_oil, crude_oil_property_values" python3 etl/oil/bp_cargo.py
run "bp_cargo_matrix             -> cleaning_process (OIL)" python3 etl/oil/bp_cargo_matrix.py

# 8) Dr Verwey, oil edition. Its OWN source row, not one of the two chemical
#    Verwey sources: procedure_templates is keyed on (source_id, procedure_code)
#    and the oil code letters collide with chemical ones that mean something
#    else, so sharing a source would overwrite seven published definitions.
#    Writes crude_oil_compatibility as well as cleaning_process - see the
#    loader docstring for why oil compatibility needs its own table.
run "verwey_oil_cargo            -> crude_oil, crude_oil_property_values" python3 etl/oil/verwey_oil_cargo.py
run "verwey_oil_templates        -> templates, steps (OIL)" python3 etl/oil/verwey_oil_procedure_templates.py
run "verwey_oil_matrix           -> cleaning_process, crude_oil_compatibility" python3 etl/oil/verwey_oil_matrix.py

# Only summarise when run directly; run_all.sh prints one summary for both branches.
[[ "${BASH_SOURCE[0]}" == "${0}" ]] && finish
