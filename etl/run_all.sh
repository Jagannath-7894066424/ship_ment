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
# Structure: the foundation runs here, then each branch runs from its own script.
#
#   etl/common/    shared: field_definitions vocabulary, the source registry
#   etl/chemical/  cargo_chemical branch  -> etl/run_chemical.sh
#   etl/oil/       crude_oil branch       -> etl/run_oil.sh
#
# Either branch can be run on its own once the foundation exists; this script is
# the full rebuild that guarantees the ordering between them.
#
# Reads DATABASE_URL + input files exactly like the individual loaders (via .env
# and etl/data/inputs). Run from anywhere — it cd's to the repo root itself.

set -uo pipefail
cd "$(dirname "$0")/.."   # repo root
source etl/_run_lib.sh
parse_run_args "$@"

# --fresh: wipe every ETL-managed table (CASCADE) so the reload starts clean.
# Done here rather than per branch because `synonyms` and `field_definitions` are
# shared - neither branch may truncate them alone. `source` and
# `dot_hazmat_symbol` are intentionally kept (common/source.py is
# skip-if-exists; the symbol legend is a static migration seed).
if [[ $FRESH -eq 1 ]]; then
  truncate_tables \
    cargo_property_values cargo_un_number cargo_synonym synonyms \
    master_cargo_chemical_group_details cargo_hazard_data cargo_dot_hazad \
    cargo_reactive_group compatibility compatibility_exception \
    reactive_groups cargo_operational_requirement operational_requirement \
    procedure_template_steps procedure_template_instruction procedure_templates \
    cleaning_process_step cleaning_process \
    crude_oil_property_values crude_oil_synonym crude_oil_compatibility crude_oil \
    cargo_gas_property_values cargo_gas \
    cargo_family_group cargo_chemical field_definitions
  # The branch scripts must not truncate again underneath us.
  FRESH=0
fi

printf '%s=== ETL: loading all sources in order ===%s\n' "$Y" "$N"

# 1) foundation (vocabulary + source authorities) — must be first, and is shared
#    by both branches: field_definitions is FK'd by cargo_property_values AND
#    crude_oil_property_values, and every loader resolves its document to a
#    source row before writing anything.
run "field_definition            -> field_definitions"          python3 etl/common/field_definition.py
run "source                      -> source"                     python3 etl/common/source.py

# 2) the two cargo branches. Chemical first: it is the larger branch and seeds
#    the shared `synonyms` vocabulary that shell_cargo_master then reuses for the
#    refined-product grade names. Neither branch FKs the other, so the order is
#    a convention rather than a hard requirement.
#
#    The branch scripts are sourced, not executed, so STEP/FAILED stay continuous
#    and one summary covers the whole run. argv is cleared first: they re-parse
#    it on entry, and would otherwise act on --fresh a second time and truncate
#    the tables this script has already rebuilt. KEEP_GOING/FRESH carry over as
#    variables, which is what the branch scripts actually read.
set --
source etl/run_chemical.sh
source etl/run_oil.sh
source etl/run_gas.sh

finish
