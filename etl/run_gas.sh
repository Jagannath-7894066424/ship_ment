#!/usr/bin/env bash
#
# Load the cargo_gas branch: the gas master and its property values.
#
#   bash etl/run_gas.sh          # load the gas branch
#   bash etl/run_gas.sh -k       # keep going after a failing step
#   bash etl/run_gas.sh --fresh  # DESTRUCTIVE: wipe the gas tables first
#
# Assumes the foundation steps (etl/common/field_definition.py, etl/common/source.py)
# have already run - run_all.sh does them first. Run this alone only against a DB
# that already has field_definitions and source populated.
#
# cargo_gas is a SEPARATE entity from cargo_chemical and crude_oil, with its own
# master and property tables and no FK to either. The three branches meet only at
# `source` (category 'gas' vs 'oil' vs 'chemical'), so nothing here affects the
# other two and this script can be run on its own.

set -uo pipefail
cd "$(dirname "$0")/.."   # repo root
source etl/_run_lib.sh
parse_run_args "$@"

# Gas-branch tables only. cleaning_process is SHARED with the other two branches,
# so it is not truncated here - no loader writes GAS rows to it yet, and when one
# does it must delete its own rows the way run_oil.sh does, not truncate.
if [[ $FRESH -eq 1 ]]; then
  truncate_tables cargo_gas_property_values cargo_gas
fi

printf '%s=== ETL: cargo_gas branch ===%s\n' "$Y" "$N"

# 1) Relative vapour densities: 31 gases, one property each. The first gas source
#    in the project - cargo_gas and cargo_gas_property_values existed but were
#    empty until this loader.
run "gas vapour_density          -> cargo_gas, properties" python3 etl/gas/vapour_density.py

# Only summarise when run directly; run_all.sh prints one summary for all branches.
[[ "${BASH_SOURCE[0]}" == "${0}" ]] && finish
