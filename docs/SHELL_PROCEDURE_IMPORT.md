# Shell Procedure Import

Structured, lossless storage for cleaning procedure templates, and the Excel
import that fills it.

Status: implemented and imported. All 11 Shell procedures are in
`localhost:5433/ship_db` under source 24, and the 9-test suite passes.

**Short answer to "which file do I run":**

```bash
python3 etl/shell_procedure_templates.py
```

Full runbook in §7.

---

## 1. What a procedure is made of

Four tables, because the parts answer four different questions. Collapsing any
two of them loses information.

```
                          source
                            |
                            v
                   procedure_templates            what does this code mean?
                            |                     WD, CW, NC ...
        +-------------------+-------------------+
        |                   |                   |
        v                   v                   v
 procedure_template_  procedure_template_  procedure_template_
        steps            requirement           instruction
                            |
  ordered actions      rules & limits      the source's own words
  sequenced 1..n       unsequenced         unsequenced

                   procedure_templates
                            ^
                            | procedure_template_id
                            |
                     cleaning_process               which procedure applies
                                                    between two cargoes?
```

`cleaning_process` **names** a procedure and never restates its steps. Correct a
step once and every transition citing it is corrected.

---

## 2. Schema changes, and why each one

Five changes. Nothing was removed, renamed, or reordered.

### `procedure_templates.source_definition` (TEXT, nullable)

The source's own sentence, kept beside the structured rows rather than replaced
by them. Normalising prose into steps and requirements is lossy by nature; this
is what makes the loss auditable and lets rows be re-derived if the parsing
rules change later. Required by rule 3 (do not lose the original source text).

### `procedure_templates.loading_allowed` (BOOLEAN, default `true`)

False only for decision codes that forbid the transition — `NC`. A
`cleaning_process` pointing at such a template means *do not load*, and its empty
step list is correct rather than missing. The default backfills all 338
pre-existing Verwey/Drew templates correctly: they are all real procedures.

### `procedure_template_steps.step_type` (`CleaningStepType`, nullable)

Lets a reader act on a step without parsing `step_name`. Nullable because the
1,437 rows already loaded carry no classification, and guessing one for them
would fabricate data.

### `CleaningStepType` — five values added

`PRECONDITION`, `VENTILATING`, `PURGING`, `GAS_FREEING`, `MOPPING`.

All seven existing values are kept exactly where they were. `PRECONDITION` is
inserted *before* `PRECLEANING` because it describes a state the tank must
already be in rather than an action performed; the rest are appended.

### `procedure_template_requirement` — new table

Rules and limits that constrain a procedure but hold no position in the step
sequence. Three things live here that a step cannot express:

| kind | example |
|---|---|
| measured limit | `ROB_LIMIT <= 0.05 %` |
| precondition | `PRECONDITION = WD` |
| **disjunction** | `ATMOSPHERE_TREATMENT = VENTILATE_OR_PURGE` |

The disjunction is the reason the table exists. The source says *ventilate **or**
purge*. Recording that as `ventilation_required = true` **and**
`inert_gas_required = true` would assert that both are mandatory — which the
source does not say. One requirement row keeps the OR intact.

`requirement_type` is `TEXT`, not an enum, on purpose: every new source document
brings its own vocabulary, and a migration per new rule type would make adding a
source harder than it needs to be.

### Not changed

`cleaning_process`, `cleaning_process_step`, `cargo_chemical`, `crude_oil`,
`gas`, and every unrelated model. The `(source_id, procedure_code)` uniqueness on
`procedure_templates` is preserved exactly.

---

## 3. Indexes and uniqueness

| table | key | purpose |
|---|---|---|
| `procedure_templates` | `(source_id, procedure_code)` | pre-existing, preserved |
| `procedure_template_steps` | `(procedure_templates_id, step_order)` | **new** — the step idempotency key |
| `procedure_template_requirement` | `(procedure_template_id, display_order)` | **new** |
| `procedure_template_instruction` | `(procedure_templates_id, display_order)` | pre-existing |

The steps unique index did not exist before. Without it a second import appended
a second copy of every procedure. Verified free of collisions before adding:
1,437 rows, 0 duplicate `(template, step_order)` pairs.

Requirements are keyed on `display_order`, **not** on `requirement_type`: a
source may legitimately state two requirements of the same type (two
preconditions, say), and that must stay representable.

Supporting indexes: `procedure_template_steps(step_type)`,
`procedure_template_requirement(procedure_template_id)`,
`procedure_template_requirement(requirement_type)`.

---

## 4. The source file, and what was missing from it

The supplied workbook

```
/home/lap044/Documents/ship_documents/Oil_document/Shell Tank Cleaning Procedure.xlsx
```

carries **one** sheet, `Cleaning Codes`, holding all 11 procedure definitions
with the columns the spec describes. It carries **no** steps, requirements or
instructions sheets — that data was supplied as written specification, not as
spreadsheet rows.

Rule 11 forbids hardcoding procedure data into the importer, so the gap is
closed in Excel instead:

```
build_shell_procedure_workbook.py   spec  -> Excel    (data entry, run once)
shell_procedure_templates.py        Excel -> database (the importer)
```

`etl/build_shell_procedure_workbook.py` writes
`etl/data/inputs/Shell Tank Cleaning Procedure.xlsx` with five sheets. The
`procedure_templates` sheet is copied **verbatim** from the supplied file; the
other three are materialised from the specification. **The importer holds no
procedure data at all** — correcting a step, a limit or a wording is an Excel
edit, never a code change.

### Two things the source file says that you should look at

1. **`cargo_type` is inconsistent.** Nine procedures say `OIL`; `HW` and `NC`
   say `CHEMICAL`. This is a Shell *oil* matrix, so that looks like a slip in the
   sheet — but it is valid data, so it is imported **verbatim** and reported
   loudly on every run. Fix it in the workbook's `cargo_type` column and re-run
   if it is wrong; nothing is corrected in code.

2. **`source_page_ref` is empty** for all 11 rows. The column exists and is
   imported; the source file simply has no values in it. Page 2 is where these
   definitions live in the Shell PDF, if you want to fill it in.

---

## 5. Import flow

```
  workbook
     |
     v
  read all sheets                 procedure_templates / _steps /
     |                            _requirements / _instructions
     v
  VALIDATE THE WHOLE WORKBOOK     nothing is written yet
     |                            - required columns present
     |                            - procedure_code unique on the template sheet
     |                            - every child row's procedure_code is defined
     |                            - cargo_type, step_type, instruction_type
     |                              valid against the Prisma enums
     |                            - booleans strictly parsed
     |                            - step_order / display_order integers >= 1,
     |                              no duplicate slots within a procedure
     |                            - loading_allowed=false => no steps
     |
     +--- any error --> report EVERY error with sheet + row, exit 1, write nothing
     |
     v
  resolve source_id               from --source-id, or by name via the shared
     |                            helpers. Never from the spreadsheet.
     v
  BEGIN                           one transaction
     |
     +-- upsert procedure_templates             on (source_id, procedure_code)
     +-- upsert procedure_template_steps        on (template, step_order)
     +-- upsert procedure_template_requirement  on (template, display_order)
     +-- upsert procedure_template_instruction  on (template, display_order)
     +-- delete child rows whose slot the workbook no longer supplies
     |
     v
  COMMIT                          any exception -> ROLLBACK, nothing partial
```

**Validation never skips a row silently.** Every problem is collected and
reported together with its sheet name and spreadsheet row number, and any single
problem aborts the entire import.

**Idempotency** is by slot. The delete step is what makes a *shortened* procedure
actually shorten — without it, removing step 5 in Excel would leave the old step 5
in the database forever.

**`source_id` comes from the import context, never the spreadsheet.** A procedure
code means nothing without the document it was read from: Verwey's `A` and
Shell's `CW` share one table. The importer integrates with the project's existing
source mechanism (`etl/data/source.json` → `etl/source.py`), which registered
*Shell Tank Cleaning Procedure* as source **24**, category `oil`.

---

## 6. What was imported

| code | cargo_type | loading_allowed | steps | requirements | instructions |
|---|---|---|---|---|---|
| WD | OIL | true | 1 | 2 | 1 |
| BF | OIL | true | 3 | 1 | 0 |
| BF-VP | OIL | true | 4 | 2 | 0 |
| CW | OIL | true | 3 | 1 | 0 |
| CW-VP | OIL | true | 4 | 2 | 0 |
| CWM | OIL | true | 5 | 3 | 0 |
| CFW | OIL | true | 4 | 4 | 1 |
| HW | *CHEMICAL* | true | 4 | 2 | 0 |
| HWM | OIL | true | 5 | 3 | 0 |
| HFW | OIL | true | 4 | 4 | 1 |
| NC | *CHEMICAL* | **false** | **0** | 1 | 1 |

11 templates, 37 steps, 25 requirements, 4 instructions. Every row has
`source_definition` populated. `NC` has no steps, by design.

---

## 7. Runbook

All commands from the repo root, `/home/lap044/projects/ship_project`.

### Run the migration

`prisma migrate` is not used in this project — the schema was built with
`prisma db push` and migrations are applied by hand:

```bash
python3 etl/apply_migration.py prisma/migrations/20260819000000_procedure_template_requirements/migration.sql
```

Already applied. The file is idempotent, so re-running is safe and is the fastest
way to confirm a database is up to date. `--list` shows every migration;
`--dry-run` prints the SQL without touching anything.

Then regenerate the client so `procedure_template_requirement` and the new enum
values exist in TypeScript:

```bash
npx prisma generate
```

### Import the Excel file  ← the file you run

```bash
# 1. build the workbook (once, or after editing the definitions)
python3 etl/build_shell_procedure_workbook.py

# 2. import it   <-- THIS IS THE ONE THAT WRITES TO THE DATABASE
python3 etl/shell_procedure_templates.py
```

`etl/shell_procedure_templates.py` is the importer. With no arguments it reads
`etl/data/inputs/Shell Tank Cleaning Procedure.xlsx` and resolves the source by
name. Useful variants:

```bash
# validate only - reports every problem, writes nothing
python3 etl/shell_procedure_templates.py --dry-run

# pin the source explicitly (the import-context form)
python3 etl/shell_procedure_templates.py --source-id 24

# a different workbook
python3 etl/shell_procedure_templates.py "/path/to/other.xlsx" --source-id 24
```

Re-running is safe: it upserts, never duplicates.

It also runs as part of the full pipeline (`npm run etl`), wired in as step 9 of
`etl/run_all.sh` after the crude-oil loaders.

### Run the tests

```bash
python3 etl/tests/test_shell_procedure_import.py
```

Nine tests, no framework needed. Test 2 re-runs the importer to prove
idempotency; test 9 writes a `cleaning_process` row inside a savepoint it rolls
back, so the suite leaves the database exactly as it found it.

```
  PASS  Test 1  - all 11 procedures imported, with their child rows
  PASS  Test 2  - second import creates no duplicates
  PASS  Test 3  - WD: ROB <= 0.05 %, pump well only
  PASS  Test 4  - CFW: bulk = Cold Sea Water, final = Cold Fresh Water (mandatory)
  PASS  Test 5  - HFW: bulk = Hot Sea Water, final = Hot Fresh Water (mandatory)
  PASS  Test 6  - CW-VP: WD, Cold Water Wash, Drain, Ventilate OR Purge (one requirement)
  PASS  Test 7  - CWM / HWM: wash, drain, gas-free, mop dry
  PASS  Test 8  - NC: loading_allowed = false, no steps
  PASS  Test 9  - cleaning_process -> procedure_template_id -> template -> steps

  9/9 passed
```

---

## 8. Reading a procedure back

`src/procedure.ts`:

```ts
import { getCompleteProcedure, getProcedureForTransition } from "./procedure.js";

const cfw = await getCompleteProcedure(prisma, 24, "CFW");
```

Ordering is fixed: steps by `step_order`, requirements and instructions by
`display_order`.

`getProcedureForTransition(prisma, cleaningProcessId)` resolves the other
direction — from a cargo transition to the procedure it calls for — and returns
the transition's own `condition` and `remarks` beside it, because a matrix cell
is often conditional and acting on the procedure without reading the condition
applies a rule out of its context.

### Actual response for CFW

```json
{
  "procedure_code": "CFW",
  "template_name": "Cold Fresh Water",
  "cargo_type": "OIL",
  "water_type": "Cold Fresh Water",
  "loading_allowed": true,
  "source_definition": "First perform WD, then cargo tanks and lines need to be washed with Cold Fresh Water. Bulk washing may be conducted with Cold Sea Water so long as a final wash with Cold Fresh Water is conducted. After washing the tank must be drained well.",
  "source": { "id": 24, "name": "Shell Tank Cleaning Procedure", "category": "oil" },

  "steps": [
    { "step_order": 1, "step_name": "Well Drained", "step_type": "PRECONDITION", "medium": null,               "mandatory": true  },
    { "step_order": 2, "step_name": "Bulk Wash",    "step_type": "CLEANING",     "medium": "Cold Sea Water",   "mandatory": false },
    { "step_order": 3, "step_name": "Final Wash",   "step_type": "CLEANING",     "medium": "Cold Fresh Water", "mandatory": true  },
    { "step_order": 4, "step_name": "Drain",        "step_type": "DRAINING",     "medium": null,               "mandatory": true  }
  ],

  "requirements": [
    { "requirement_type": "PRECONDITION",      "requirement_value": "WD",               "mandatory": true,  "display_order": 1 },
    { "requirement_type": "BULK_WASH_MEDIUM",  "requirement_value": "Cold Sea Water",   "mandatory": false, "display_order": 2 },
    { "requirement_type": "FINAL_WASH_MEDIUM", "requirement_value": "Cold Fresh Water", "mandatory": true,  "display_order": 3 },
    { "requirement_type": "DRAIN_REQUIRED",    "requirement_value": "true",             "mandatory": true,  "display_order": 4 }
  ],

  "instructions": [
    { "instruction_type": "IMPORTANT", "message": "Bulk washing may be conducted with Cold Sea Water so long as a final wash with Cold Fresh Water is conducted.", "mandatory": true, "display_order": 1 }
  ]
}
```

Note `mandatory: false` on the bulk wash and `true` on the final wash. That pair
is the whole point of not flattening CFW to `water_type = "Cold Fresh Water"`:
the sea-water bulk wash is an **allowance**, and the fresh-water final wash is
the **condition** it depends on. HFW carries the same shape with hot media.

### Optional HTTP surface

`src/procedure-import-api.ts` exposes the importer and the queries over Express
(already a dependency). It shells out to the same Python importer rather than
reimplementing validation, transactions and idempotency where the two could
drift apart. There is no multipart handler — accepting a browser upload needs a
file-upload middleware this project does not have; the route takes a server-side
path. Run it with `npx tsx src/procedure-import-api.ts`.

---

## 9. Files

| file | role |
|---|---|
| `prisma/schema.prisma` | 5 schema changes (§2) |
| `prisma/migrations/20260819000000_procedure_template_requirements/migration.sql` | the migration |
| `etl/apply_migration.py` | applies a hand-written migration |
| `etl/build_shell_procedure_workbook.py` | spec → Excel, one-time data entry |
| `etl/data/inputs/Shell Tank Cleaning Procedure.xlsx` | **the import source** |
| `etl/shell_procedure_templates.py` | **the importer** |
| `etl/tests/test_shell_procedure_import.py` | 9 tests |
| `src/procedure.ts` | complete-procedure queries |
| `src/procedure-import-api.ts` | optional Express routes |
| `etl/run_all.sh` | wired in as step 9 |
| `etl/data/source.json` | Shell source registered, category `oil` |

---

## 10. Known gaps

- **The cargo transition matrix is still not loaded.** These are the *procedure
  definitions* (Shell PDF page 2). The page-3 cargo-to-cargo matrix that cites
  them — `Diesel → Gasoline: CW` — needs the matrix data and a separate loader
  writing `cleaning_process`. See [Cargo Type Architecture](CARGO_TYPE_ARCHITECTURE.md) §11.
- **`HW` and `NC` say `cargo_type = CHEMICAL`** in the source workbook. Imported
  verbatim, reported on every run. See §4.
- **`source_page_ref` is empty** for all 11 rows — the source file has no values.
- **The steps, requirements and instructions were transcribed from the written
  specification**, not from the supplied spreadsheet, which contains none. They
  now live in Excel and are edited there.
