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
# so it is not truncated here - only the GAS rows are deleted, and they go
# FIRST. Their from_cargo_id / to_cargo_id have no FK to cargo_gas (the columns
# are polymorphic, shared with the chemical and oil branches), and
# truncate_tables restarts identities, so wiping cargo_gas while leaving them
# behind would leave 144 rows pointing at ids that get handed to entirely
# different gases on the reload - and nothing would complain.
#
# cargo_gas_synonym is listed explicitly even though TRUNCATE ... CASCADE would
# take it anyway: naming it says the link rows are meant to go. The `synonyms`
# rows they point at are NOT truncated - that table is shared with the chemical
# and oil branches, and dropping a name this branch happens to reuse would
# delete it out from under them.
if [[ $FRESH -eq 1 ]]; then
  printf '%s!!! --fresh: deleting GAS rows from cleaning_process (DESTRUCTIVE) !!!%s\n' "$R" "$N"
  python3 - <<'PYDEL' || { printf '%s     x delete failed - aborting%s\n' "$R" "$N"; exit 1; }
import os, psycopg2
from dotenv import load_dotenv
load_dotenv(".env")
conn = psycopg2.connect(os.environ["DATABASE_URL"])
with conn, conn.cursor() as cur:
    cur.execute("DELETE FROM cleaning_process WHERE cargo_type = 'GAS'")
    print(f"     deleted {cur.rowcount} GAS cleaning_process row(s)")
conn.close()
PYDEL
  truncate_tables cargo_gas_synonym cargo_gas_compatibility cargo_gas_property_values cargo_gas
fi

printf '%s=== ETL: cargo_gas branch ===%s\n' "$Y" "$N"

# 1) Relative vapour densities: 31 gases, one property each. The first gas source
#    in the project - cargo_gas and cargo_gas_property_values existed but were
#    empty until this loader.
run "gas vapour_density          -> cargo_gas, properties" python3 etl/gas/vapour_density.py

# 2) Products_info: 38 products with nine properties each (formula, UN number,
#    molecular weight, boiling/flash point, specific gravity, flammable limits,
#    TLV, odour threshold). Overlaps VAPDENS by name on purpose - cargo_gas is
#    keyed (gas_name, source_id), so each source keeps its own rows.
run "gas products_info           -> cargo_gas, properties" python3 etl/gas/products_info.py

# 3) Properties of Gases Doc: 15 commercial gases with eleven properties each,
#    including the critical point. The only gas source that cites where its
#    figures come from (GPSA, Chemiekaarten, LGI), which is why it outranks the
#    other two for physical properties.
run "gas properties_of_gases     -> cargo_gas, properties" python3 etl/gas/properties_of_gases.py
run "gas properties_of_gases_data -> cargo_gas, properties" python3 etl/gas/properties_of_gases_data.py

# 4) Cargo Data: 19 cargoes with formula, molecular weight, UN number + IMO
#    class (one column in the source, two fields here) and liquid density.
run "gas cargo_data              -> cargo_gas, properties" python3 etl/gas/cargo_data.py

# 5) Thermodynamic data book, one sheet per gas: seven of the eight are loaded
#    below, each with --sheet. ETHYLENE is the one left out - it states no
#    enthalpy datum, so its 276 enthalpy rows are still an open question; the
#    loader reads the sheet and everything else about it is ready.
#    One run per sheet, so each gas's
#    load is attributable on its own and a sheet the loader cannot read yet does
#    not hold back the ones it can. This is the only gas source whose data is
#    state-dependent, so it also writes cargo_gas_thermodynamic_property.
run "gas thermodynamic (butadiene) -> cargo_gas, properties, thermodynamics" \
    python3 etl/gas/thermodynamic_data.py --sheet "BUTADIENE 1_3" --gas-name "Butadiene 1-3"
run "gas thermodynamic (ammonia)   -> cargo_gas, properties, thermodynamics" \
    python3 etl/gas/thermodynamic_data.py --sheet "AMMONIA" --gas-name "Ammonia"
# Ethane states three of the seven general properties and prints the other four
# with nothing after them - no row is written for those, which is the sheet
# declining to state them rather than a parse failure.
run "gas thermodynamic (ethane)    -> cargo_gas, properties, thermodynamics" \
    python3 etl/gas/thermodynamic_data.py --sheet "ETHANE" --gas-name "Ethane"
# Ethylene states no enthalpy datum; the loader flags its enthalpies as such.
run "gas thermodynamic (ethylene)  -> cargo_gas, properties, thermodynamics" \
    python3 etl/gas/thermodynamic_data.py --sheet "ETHYLENE" --gas-name "Ethylene"
# n-Butane is the only sheet so far that states a LIQUID viscosity, and it
# misspells the gas-phase label as "gasseous" - a spelling the loader now knows,
# because a property it cannot name is one it drops without saying so.
run "gas thermodynamic (n-butane)  -> cargo_gas, properties, thermodynamics" \
    python3 etl/gas/thermodynamic_data.py --sheet "n_BUTANE" --gas-name "n-Butane"
# Propane spells its superheated heading "SUPERHEADTED" - the fourth spelling of
# that heading in one workbook.
run "gas thermodynamic (propane)   -> cargo_gas, properties, thermodynamics" \
    python3 etl/gas/thermodynamic_data.py --sheet "PROPANE" --gas-name "Propane"
# VCM is the odd one out: it misspells its saturated heading, heads its
# SUPERHEATED grid "PROPERTIES OF SATURATED VAPOUR" (every row of that grid
# records the mislabelling), calls the inflammability limits "Explosive Limits",
# and is the only sheet that states a flash point. Loaded under the name its own
# title uses; other sources spell it VCM, Vinylchloride and Vinyl Chloride.
run "gas thermodynamic (vcm)       -> cargo_gas, properties, thermodynamics" \
    python3 etl/gas/thermodynamic_data.py --sheet "VCM" --gas-name "Vinyl Chloride Monomer"
# Propylene shares ETHYLENE's layout - the data pasted into a cargo cool-down
# worksheet, properties in column 9, the tables from column 14 - which is why
# ethylene's worksheet says "liquid Propylene". Unlike ethylene it states its
# enthalpy datum, so it loads whole.
run "gas thermodynamic (propylene) -> cargo_gas, properties, thermodynamics" \
    python3 etl/gas/thermodynamic_data.py --sheet "PROPYLENE" --gas-name "Propylene"

# 6) SGS density tables: the SECOND source of saturated data for cargo_gas, and
#    a surveyor's book rather than a design handbook - vapour pressure and the
#    two phase densities only, but at 0.5 C steps (0.1 C for ethylene) over the
#    carriage range, which is what a custody-transfer calculation needs. Kept
#    apart from the workbook by source_id; six gases in one run, so it takes no
#    --sheet.
run "gas sgs_density            -> cargo_gas, thermodynamics" python3 etl/gas/sgs_density.py

# 6) IMO cargo numbers: 16 cargoes, and the first gas source that is regulatory
#    rather than physical - IMO hazard class, UN number and MFAG table number,
#    no measurement anywhere. Every property value is text with no normalized
#    value: 1005 is a name for ammonia, not a quantity. Its fourth column, the
#    source's own three-letter codes, is not a property at all and goes to the
#    shared `synonyms` table through cargo_gas_synonym - the only gas loader
#    that writes either.
run "gas imo_cargo_numbers      -> cargo_gas, properties, synonyms" python3 etl/gas/imo_cargo_numbers.py

# 7) Products.doc: 16 products with twelve columns each plus a names list, and
#    two C4 streams given only as compositions - the broadest gas source, and
#    the only one that carries identity, regulatory status, physical properties,
#    a health limit and alternative names in one table. Its liquid-density
#    column is a thousand times out from the unit it prints; those six values
#    are loaded as printed with is_winning=false, never converted.
run "gas products_doc           -> cargo_gas, properties, synonyms" python3 etl/gas/products_doc.py

# 8) IGC Code 2016 chapter 19: 37 cargoes and the summary of minimum
#    requirements. The only gas source that describes the SHIP rather than the
#    cargo - ship type, tank type, vapour space control, vapour detection and
#    gauging - so every value is regulatory text with no unit and no normalized
#    value. Two of its seven columns hold IGC paragraph numbers, which are
#    pointers into chapters 13, 14 and 17 rather than requirements in themselves.
run "gas igc_code               -> cargo_gas, properties" python3 etl/gas/igc_code.py

# 9) DOW LPG change-of-grade guidelines: 144 directed pairs over 13 LPG grades,
#    and the FIRST gas source to write cleaning_process / cleaning_process_step -
#    both tables held only CHEMICAL and OIL rows before it. It states no
#    property of any cargo, so it creates cargo_gas rows by name alone and
#    writes no cargo_gas_property_values.
#
#    It removes its OWN rows and re-inserts them rather than upserting, which is
#    why --fresh must never truncate cleaning_process: that table is shared with
#    the chemical and oil branches.
run "gas dow_lpg_change_of_grade -> cargo_gas, cleaning_process, steps" \
    python3 etl/gas/dow_lpg_change_of_grade.py

# 10) Liquefied-gas compatibility workbook: one document holding TWO different
#     kinds of statement, so one source writes to two unrelated pairs of tables.
#     Its 17x17 matrix answers "may these two share a tank" and goes to
#     cargo_gas_compatibility - a new table, the gas twin of
#     crude_oil_compatibility (20260910000000_cargo_gas_compatibility). Its
#     change-of-grade matrix answers "what must be done between them" and goes
#     to cleaning_process, a second guide alongside DOW.
#
#     The compatibility matrix is symmetric and stored once per pair; the
#     change-of-grade matrix is directional. Removes its own GAS
#     cleaning_process rows, same as the DOW loader.
run "gas gas_compatibility      -> cargo_gas, compatibility, cleaning_process" \
    python3 etl/gas/gas_compatibility.py

# 11) Tanker Safety Guide (Liquefied Gas): 38 cargoes and forty-nine columns
#     each - the broadest gas source, and the first that is a SAFETY guide
#     rather than a data book. Half its columns are instructions (what to do in
#     a fire, what to do for a casualty who has inhaled the vapour) rather than
#     measurements, so it brings fifteen health and emergency fields no gas
#     source had before, and is the first gas source with a health rank at all.
#
#     Five of its columns republish IGC Code chapter 19 and are loaded into the
#     same igc_* fields etl/gas/igc_code.py writes, so the two sources' answers
#     to one regulatory question sit side by side.
#
#     Nine of its values are ones the file contradicts itself about - ammonia's
#     molecular weight and relative vapour density are printed in each other's
#     columns, and seven CAS numbers are malformed or given to more than one
#     cargo. All nine load as printed with is_winning=false; none is corrected.
run "gas tanker_safety_guide    -> cargo_gas, properties, synonyms" \
    python3 etl/gas/tanker_safety_guide.py

# 12) The same guide's CARRIAGE CONDITIONS table - 11 cargoes, three columns -
#     loaded under the SAME source row as step 11, because it is a second table
#     in one publication rather than a second publication. It must run AFTER
#     step 11: it reads back the boiling point that step loaded so a
#     disagreement between the book's two tables is recorded on the value
#     (ethylene boils at -103.7 in one table and -104 in the other).
#
#     All three of its columns load under their OWN field names -
#     boiling_point_atmospheric_c, vapour_pressure_45c and
#     practical_carriage_conditions - because sharing a source_id means the
#     same field name would have one table silently overwrite the other's
#     answer, and the coarser table would win.
run "gas tanker_safety_guide (carriage) -> cargo_gas, properties" \
    python3 etl/gas/tanker_safety_guide_carriage.py

# Only summarise when run directly; run_all.sh prints one summary for all branches.
[[ "${BASH_SOURCE[0]}" == "${0}" ]] && finish
