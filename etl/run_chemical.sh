#!/usr/bin/env bash
#
# Load the cargo_chemical branch: the chemical cargo master, its properties,
# synonyms, hazard/DOT data, reactive groups, compatibility and the tank-cleaning
# guides keyed on chemicals.
#
#   bash etl/run_chemical.sh          # load the chemical branch
#   yarn etl:chemical                 # same, via package.json
#   bash etl/run_chemical.sh -k       # keep going after a failing step
#   bash etl/run_chemical.sh --fresh  # DESTRUCTIVE: wipe the chemical tables first
#
# Assumes the foundation steps (etl/common/field_definition.py, etl/common/source.py)
# have already run - run_all.sh does them first. Run this alone only against a DB
# that already has field_definitions and source populated.
#
# Nothing here touches the oil branch: the two meet only at `source`.

set -uo pipefail
cd "$(dirname "$0")/.."   # repo root
source etl/_run_lib.sh
parse_run_args "$@"

# Chemical-branch tables only. `synonyms` is deliberately NOT in this list: it is
# the shared name vocabulary that the oil branch links to as well, and the
# loaders dedupe on normalized_text, so keeping it costs nothing and truncating
# it would strand crude_oil_synonym.
if [[ $FRESH -eq 1 ]]; then
  truncate_tables \
    cargo_property_values cargo_un_number cargo_synonym \
    master_cargo_chemical_group_details cargo_hazard_data cargo_dot_hazad \
    cargo_reactive_group compatibility compatibility_exception \
    reactive_groups cargo_operational_requirement operational_requirement \
    procedure_template_steps procedure_template_instruction procedure_templates \
    cleaning_process_step cleaning_process \
    cargo_family_group cargo_chemical
fi

printf '%s=== ETL: cargo_chemical branch ===%s\n' "$Y" "$N"

# 1) core cargo via master_loader (upserts; handles the LARS/CHEM/USCG formats,
#    including duplicate canonical-names that break the plain-INSERT loaders).
#    master_loader (lars) also loads the synonyms + properties, so the separate
#    cargo_synonym / import_synonyms scripts are not needed here.
run "master_loader (LARS)        -> cargo_chemical, synonyms, props" python3 etl/chemical/master_loader.py "$INPUTS/Lars Stole Birkeland - Chemical Cargo specifications - 2002.xlsx - CGOSPEC.csv"
run "master_loader (CHEM)        -> cargo, properties"          python3 etl/chemical/master_loader.py "$INPUTS/Unknown - Products CHEM - 1996.XLS"
run "master_loader (USCG)        -> cargo, hazard, properties"  python3 etl/chemical/master_loader.py "$INPUTS/USCG Chemical Data Guide For Bulk Shipment By Water [7th Edition 1990]_reviewed.csv" --source-name "USCG CHRIS Chemical Data Guide"
run "master_loader (IBC Code)    -> cargo (identity/carriage)"  python3 etl/chemical/master_loader.py "$INPUTS/IBC Code.xlsx"
run "master_loader (Miracle)     -> cargo, cleaning_process"    python3 etl/chemical/master_loader.py

# 1b) Sittig's Handbook — health/toxicity reference. Its own loader rather than
#     master_loader because the CSV is badly quoted (unquoted commas push rows
#     past the 73 header columns); sittig_handbook.py repairs the two
#     recoverable cases and flags the rest in cargo_chemical.notes instead of
#     dropping them. Seeds its own field_definitions.
run "sittig_handbook             -> cargo, properties, synonyms" python3 etl/chemical/sittig_handbook.py "$INPUTS/Sittigs Handbook of Toxic & Hazardous Chemicals.csv"

# 2) reactive groups + compatibility. reactive_group first (plain insert into an
#    empty table), then cargo_compatibility (ON CONFLICT tolerates overlap).
#    group_details FKs to reactive_groups.group_code, so it must run AFTER them.
run "reactive_group              -> reactive_groups"            python3 etl/chemical/reactive_group.py
run "cargo_compatibility         -> compatibility"              python3 etl/chemical/cargo_compatibility.py
run "compatibility_exceptions    -> compatibility_exception"    python3 etl/chemical/compatibility_exception_loader.py --all-under-source --source-name "USCG CHRIS Chemical Data Guide"
run "link_cargo_reactive_groups  -> cargo_reactive_group"       python3 etl/chemical/link_cargo_reactive_groups.py
run "group_details               -> master_cargo_chemical_group_details" python3 etl/chemical/master_cargo_chemical_group_details.py

# 3) operational requirements (IBC)
run "operational_requirements    -> operational_requirement"    python3 etl/chemical/cargo_operational_requirement.py

# 4) cleaning guides
run "procedure_templates         -> procedure_templates"        python3 etl/chemical/proceduretemplate.py
run "verwey_cleaning             -> cleaning_process (matrix)"  python3 etl/chemical/verwey_cleaning.py
run "drew_ameroid                -> procedure_templates + pairs" python3 etl/chemical/drew_ameroid.py

# 4b) Dr Verwey PDF Book edition — a SEPARATE source from the two steps above
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
run "verwey_pdf_book             -> cargo_chemical, properties"  python3 etl/chemical/verwey_pdf_book.py "$VERWEY_PDF - Cargo details.csv"
run "verwey_pdf_book_procedures  -> templates, steps, instructions" python3 etl/chemical/verwey_pdf_book_procedures.py "$VERWEY_PDF Procdure Template.csv"
run "verwey_pdf_book_families    -> cargo_family_group"          python3 etl/chemical/verwey_pdf_book_families.py "$VERWEY_PDF  _from-to procedure.csv"
run "verwey_pdf_book_matrix      -> cleaning_process (family)"   python3 etl/chemical/verwey_pdf_book_matrix.py "$VERWEY_PDF  _from-to procedure.csv"

# 5) DOT Hazardous Materials Table
run "dot_hmt_extract             -> JSON (no DB)"               python3 etl/chemical/dot_hmt_extract.py
run "dot_hazmat_symbol           -> cargo_hazard_data.dot_symbol" python3 etl/chemical/dot_hazmat_symbol_loader.py
run "cargo_dot_hazad             -> cargo_dot_hazad"            python3 etl/chemical/cargo_dot_hazad_loader.py

# Only summarise when run directly; run_all.sh prints one summary for both branches.
[[ "${BASH_SOURCE[0]}" == "${0}" ]] && finish
