# ETL input files

Source spreadsheets / CSVs that the `etl/*.py` loaders import. This is the
default location: `etl/_paths.py` looks here whenever `SHIP_DATA_DIR` is unset
in the repo-root `.env` (which is the normal setup now — the files live in the
repo, so every developer has them on clone).

To use a different local data folder for a run, set `SHIP_DATA_DIR` in `.env`,
or pass an explicit path to a loader (e.g. `python3 etl/master_loader.py <path>`).

## Files and the loader that reads each

| File | Loader |
|------|--------|
| `Lars Stole Birkeland - Chemical Cargo specifications - 2002.xlsx - CGOSPEC.csv` | `cargo_chemicals.py`, `cargo_synonym.py`, `import_synonyms.py` |
| `cargo_chemicals_groupData.csv` | `master_cargo_chemical_group_details.py` |
| `Miracle Tank Cleaning Guide.xlsx` | `master_loader.py` (Miracle), `miracle_2007.py` |
| `Unknown - Products CHEM - 1996.XLS` | `master_loader.py` (CHEM) |
| `USCG Chemical Data Guide For Bulk Shipment By Water [7th Edition 1990]_reviewed.csv` | `master_loader.py` (USCG) |
| `USCG CHRIS Chemical Data Guides_chemical_exceptions.csv` | `compatibility_exception_loader.py` |
| `Cargo Library 3 TABLE OF CHEMICAL CARGO.xlsx` | `cargo_compatibility.py` |
| `Cargo Library 4 Odfjell - Compatibility Chart and Notes Reactive Cargoes - 1999 .xlsx` | compatibility / reactive-group loaders |
| `IBC Code.xlsx` | `cargo_operational_requirement.py` |
| `Dr. Verwey's Tank Cleaning Table 4.xlsx - CLEANING PROCEDURES (T-2).csv` | `proceduretemplate.py`, `verwey_cleaning.py` |
| `Dr. Verwey's Tank Cleaning Table 4.xlsx - CLEANING chemical_to_chemical.csv` | `verwey_cleaning.py` |
| `Cargo Library 2 DOT_hazardous_materials.xlsx - HM Table.csv` | `dot_hmt_extract.py`, `cargo_dot_hazad_loader.py` |
| `DMM_Chemical_DB_Schema.xlsx - 20 - Source Authority Matrix.csv` | source authority reference |
| `Operational_References_Master.csv` | operational reference |
| `washing_requirement - Washing Requirement.csv` | washing-requirement reference |

## Not committed
The large source PDFs (`USCG Chemical Data guide Book.pdf`, `Miracle Tank
Cleaning guide 2008.pdf`, ~57 MB) are the original references but are **not**
read by any loader, so they are git-ignored (see the repo `.gitignore`). Keep
them elsewhere if you need the originals.
