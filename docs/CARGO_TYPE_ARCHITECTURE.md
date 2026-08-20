# Cargo Type Architecture

How chemical, crude-oil and gas cargoes share one set of cleaning tables while
staying three separate master entities.

Status: implemented. Schema, migrations and query layer are in the repo and both
migrations are applied to `localhost:5433/ship_db`.

---

## 1. Architecture

Master data is separate. Operational data is common.

```
                              source
                                |
        +-----------------------+-----------------------+
        |                       |                       |
        v                       v                       v
  cargo_chemical            crude_oil                  gas
        |                       |                       |
        v                       v                       v
 cargo_property_values  crude_oil_property_    gas_property_values
                              values
        |                       |                       |
        +-----------------------+-----------------------+
                                |
                        COMMON OPERATIONS
                                |
                 +--------------+--------------+
                 |                             |
                 v                             v
        procedure_templates             cleaning_process
         (cargo_type)                    (cargo_type)
                 |                             |
                 v                             v
     procedure_template_steps        cleaning_process_step
```

The three masters have no foreign key between them, and none was added. They meet
only at `source` above and at the two operational tables below.

`cargo_chemical` keeps every relation it already had (synonyms, hazard data, UN
numbers, coatings, reactive groups, DOT HMT, operational requirements, family
groups). None of that moved.

---

## 2. What changed

| | Before | After |
|---|---|---|
| `gas` | did not exist | new master, sibling of `crude_oil` |
| `gas_property_values` | did not exist | new, mirrors `crude_oil_property_values` |
| `procedure_templates` | chemical-only, implicitly | `cargo_type`, `source_page_ref` |
| `cleaning_process` | chemical-only, FK to `cargo_chemical` | `cargo_type`, `condition`, `remarks`, `procedure_template_id`; cargo columns polymorphic |
| `CargoType` | did not exist | `CHEMICAL \| OIL \| GAS` |

Nothing was merged, renamed or dropped. Existing table names are preserved,
including the plural `procedure_templates` / `procedure_template_steps`.

**Reused rather than duplicated.** `cleaning_process` already had
`from_cargo_id` / `to_cargo_id` / `source_id` / `source_page_ref` / `notes`, and
`cleaning_process_step` already had `step_order` / `method` / `duration` /
`temperature` / `medium` / `cleaner` / `description` / `remarks` / `mandatory` /
`step_type`. `procedure_template_steps` already had the equivalent set. Those
columns cover the requested shape as-is; only the four genuinely missing ones
were added.

---

## 3. Enum

```prisma
enum CargoType {
  CHEMICAL // -> cargo_chemical
  OIL      // -> crude_oil
  GAS      // -> gas
}
```

Used **only** on `procedure_templates` and `cleaning_process`. Master tables
never carry it — they *are* the type.

It is deliberately separate from the existing `SourceCategory`
(`chemical | oil | gas`). `SourceCategory` describes a *document*; `CargoType`
describes a *row of operational data*. They will usually agree, but a single
source could publish procedures for more than one cargo type, and collapsing the
two would make that unrepresentable.

---

## 4. Models

### procedure_templates — reusable definition

Answers *"what does this code mean?"* — `CW` = Cold Water Wash.

```prisma
model procedure_templates {
  id              Int       @id @default(autoincrement())
  procedure_code  String
  template_name   String
  cargo_type      CargoType @default(CHEMICAL)   // NEW
  description     String?
  source_id       Int
  source_page_ref String?                        // NEW
  notes           String?
  // ... existing operational defaults kept unchanged:
  // target_purpose, water_type, default_temperature_c, default_duration_hours,
  // default_chemicals, ventilation_required, gas_free_required,
  // wall_wash_required, inert_gas_required
  created_at      DateTime  @default(now())
  updated_at      DateTime  @updatedAt

  @@unique([source_id, procedure_code])
  @@index([cargo_type])
  @@index([source_id])
  @@index([procedure_code])
  @@index([cargo_type, source_id])
}
```

`@@unique([source_id, procedure_code])` was already there and is kept: a code is
unique **within its source**. Verwey's `A` and Shell's `CW` coexist without
being forced to agree.

### cleaning_process — specific cargo transition

Answers *"which procedure applies between these two cargoes?"*

```prisma
model cleaning_process {
  id                    Int       @id @default(autoincrement())
  cargo_type            CargoType @default(CHEMICAL)   // NEW

  cargo_id              Int?   // single-cargo procedure (Miracle)
  from_cargo_id         Int?   // previous cargo
  to_cargo_id           Int?   // next cargo

  from_cargo_family_group_id Int?   // Verwey PDF Book, family grain — CHEMICAL only
  to_cargo_family_group_id   Int?

  procedure_template_id Int?      // NEW — canonical link
  procedure_code        String?   // kept: verbatim source wording

  source_id             Int
  cleaning_stage        CleaningStage?  // stage-based sources only
  method_number         Int?

  title                 String?
  condition             String?   // NEW
  remarks               String?   // NEW
  recipe_for            String?
  source_page_ref       String?
  notes                 String?
  created_at            DateTime @default(now())
  updated_at            DateTime @default(now())
}
```

`cleaning_process_step` and `procedure_template_steps` were **not changed** —
they already carried every column the design calls for.

---

## 5. The two tables are not interchangeable

This distinction is the point of the design and must survive every future change.

| | `procedure_templates` | `cleaning_process` |
|---|---|---|
| Question | what does `CW` mean? | what do I run between X and Y? |
| Cardinality | one row per code per source | one row per cargo pair (per condition) |
| Example | `CW` = Cold Water Wash | Diesel → Gasoline: run `CW` |
| Steps | `procedure_template_steps` — the canonical recipe | `cleaning_process_step` — steps as they apply to this transition |

A cleaning process **points at** a template; it does not restate it. `NC`
("Not Compatible") is just another template code — a decision outcome, not a
reason for a separate compatibility table. A `cleaning_process` row whose
template is `NC` means the source's matrix forbids that transition.

---

## 6. Relationships

**Real foreign keys** (unchanged or added):

- `cleaning_process.source_id` → `source.id`, cascade
- `cleaning_process.procedure_template_id` → `procedure_templates.id`, `SET NULL`
- `cleaning_process.from_cargo_family_group_id` / `to_cargo_family_group_id` → `cargo_family_group.id`, cascade — family groups hang off `cargo_chemical` and are chemical-only, so they are *not* polymorphic and keep their FK
- `procedure_templates.source_id` → `source.id`, cascade
- `gas.source_id`, `gas_property_values.{gas_id, source_id, field_name, source_synonym_id}`

**Replaced.** `cleaning_process` used to reach its template through a composite
FK on `(source_id, procedure_code)`. That is now `procedure_template_id`. The
composite key forced a process and its template into the same source row, which
does not survive the move to a shared table — an operator matrix may cite a
procedure defined in a different document. `procedure_code` is **kept**: it is
the verbatim source wording and the idempotency key of the ETL template rows.
All 305,464 rows that had a resolvable code were backfilled; zero were left
unresolved.

**No foreign key — polymorphic.** `cargo_id`, `from_cargo_id`, `to_cargo_id`.
`cargo_type` selects the master table:

```
cargo_type = CHEMICAL   ->  cargo_chemical.id
cargo_type = OIL        ->  crude_oil.id
cargo_type = GAS        ->  gas.id
```

PostgreSQL cannot retarget a foreign key per row, so the three FKs to
`cargo_chemical` were dropped and Prisma models no relation for these columns.
This is the one real cost of the design, and it is compensated rather than
accepted — see §7.

Because Prisma can no longer join these columns, `cargo_chemical` lost its
`cleaningProcesses` / `cleaningProcessesFrom` / `cleaningProcessesTo` relation
fields, and `src/cleaning.ts` was rewritten to resolve ids first and filter on
`cargo_type` explicitly. Its exported functions keep their previous shapes, so
callers in `src/index.ts` and `src/run-cleaning.ts` are unaffected.

---

## 7. Validation for cargo_type + from_cargo_id + to_cargo_id

Three layers, because the write path is not only the TypeScript app — the Python
ETL loaders and psql write to these tables too.

### Layer 1 — check constraints (shape)

```sql
cleaning_process_pair_complete     -- (from IS NULL) = (to IS NULL)
cleaning_process_cargo_xor_pair    -- cargo_id XOR a from/to pair
cleaning_process_family_is_chemical-- family columns only when cargo_type='CHEMICAL'
```

### Layer 2 — database trigger (existence)

`cleaning_process_cargo_refs`, `BEFORE INSERT OR UPDATE OF` the four columns,
does what a dynamic FK would: resolves each non-null id in the master
`cargo_type` names and raises `foreign_key_violation` if it is not there.

A second pair of triggers replaces the `ON DELETE CASCADE` the dropped FKs used
to provide — deleting a `cargo_chemical`, `crude_oil` or `gas` row still removes
its `cleaning_process` rows, scoped by `cargo_type` so a chemical delete never
touches an oil row that shares an id.

### Layer 3 — service layer (`src/cargo-type.ts`)

```ts
await assertCargoRefs(prisma, {
  cargo_type: CargoType.OIL,
  from_cargo_id: dieselId,
  to_cargo_id: gasolineId,
});
```

Throws a typed `CargoRefError` before the write reaches the database, so callers
get a useful message instead of a raw trigger exception. Also exported:
`cargoExists`, `cargoName`, `findCargoIdsByName`, `createCleaningProcess`
(validate-then-create), and `getTransition(prisma, cargoType, from, to)`.

### The limit you must design around

**Neither the trigger nor `assertCargoRefs` can catch a *wrong but valid*
`cargo_type`.** The id spaces overlap: today 564 ids exist in both
`cargo_chemical` and `crude_oil`. Writing a chemical id with
`cargo_type = OIL` produces a row that passes every check and silently means the
wrong cargo.

Only the writer knows which master an id came from. So: **never carry a bare
cargo id through application code — carry the `(cargo_type, id)` pair**, and set
`cargo_type` from the same place the id was read, never inferred later. A real
foreign key would have caught this; the polymorphic design cannot, and that is
the trade being made.

---

## 8. Indexes and constraints

**Unique / idempotency**

| Key | Covers |
|---|---|
| `procedure_templates_source_id_procedure_code_key` | one code per source (existing) |
| `cleaning_process_pair_key` — partial, `(from_cargo_id, to_cargo_id, source_id, COALESCE(condition,''))` | **changed**: cargo-grain transitions |
| `cleaning_process_template_key` — partial | template rows (existing) |
| `cleaning_process_family_pair_source_key` — partial | Verwey family-grain rows (existing) |
| `cleaning_process_cargo_id_source_id_cleaning_stage_method_n_key` | stage-based rows (existing) |
| `gas_gas_name_source_id_key`, `gas_property_values_gas_id_source_id_field_name_key` | new masters |

`condition` was added to the pair key because one matrix cell can carry several
conditional outcomes for the *same* pair ("CWM if Chemical Grade, otherwise NC").
The old key collapsed them into one row. `COALESCE` is required — `NULL <> NULL`
in a unique index, so an unconditional row would never conflict with itself.

`cargo_type` is deliberately *not* in that key: `source.category` is
single-valued, so every row of one source shares a `cargo_type` and it would add
nothing.

**Indexes added**

```
procedure_templates : cargo_type, source_id, procedure_code, (cargo_type, source_id)
cleaning_process    : cargo_type, procedure_template_id,
                      (cargo_type, source_id), (cargo_type, from_cargo_id),
                      (cargo_type, to_cargo_id)
gas                 : gas_name, source_id
gas_property_values : gas_id, source_id, field_name, (gas_id, source_id), (gas_id, field_name)
```

Existing `cleaning_process` indexes on `cargo_id`, `source_id`, `from_cargo_id`,
`to_cargo_id`, `procedure_code` are unchanged. The `(cargo_type, …)` composites
matter because *every* correct query now filters on `cargo_type` — an id alone
is ambiguous.

---

## 9. Migrations

Two, hand-applied via psycopg2 as this project requires (the database is not
`prisma migrate` tracked). Both are idempotent and re-runnable.

**`20260818000000_gas_master_and_properties`** — creates `gas` and
`gas_property_values` with their FKs and indexes. Purely additive.

**`20260818100000_cargo_type_common_operations`** — creates the `CargoType`
type; adds `cargo_type` + `source_page_ref` to `procedure_templates`; adds
`cargo_type`, `condition`, `remarks`, `procedure_template_id` to
`cleaning_process`; backfills `procedure_template_id` from
`(source_id, procedure_code)` and drops that composite FK; drops the three
`cargo_chemical` FKs; adds the check constraints, the validation trigger and the
three cascade triggers; rebuilds `cleaning_process_pair_key` with `condition`.

**Applied result** on `localhost:5433/ship_db`:

```
cleaning_process       306,712 rows -> unchanged, all backfilled cargo_type=CHEMICAL
cleaning_process_step  600,867 rows -> unchanged
procedure_templates        338 rows -> unchanged, all backfilled cargo_type=CHEMICAL
procedure_template_id linked                     305,464 rows
procedure_code set but template unresolved             0 rows
```

`DEFAULT 'CHEMICAL'` backfills correctly with no data change because every
existing row comes from a chemical tank-cleaning guide (Verwey, Verwey PDF Book,
Drew Ameroid, Miracle). `npx prisma migrate diff` against the live database
reports an empty diff — schema and database agree exactly.

### ETL loaders updated

- `etl/verwey_cleaning.py`, `etl/drew_ameroid.py` — `ON CONFLICT` target extended with `COALESCE(condition, '')` to match the rebuilt pair key.
- `etl/verwey_cleaning.py`, `etl/drew_ameroid.py`, `etl/verwey_pdf_book_matrix.py` — resolve `procedure_template_id` explicitly, since the composite FK that used to imply it is gone.

No loader sets `cargo_type`: they all load chemical sources and the column
default is correct. A future oil or gas loader must set it explicitly.

---

## 10. Example records

### CHEMICAL — real rows from the database

`procedure_templates` id 30 (Dr Verwey's Tank Cleaning Guide, source 7):

| id | procedure_code | template_name | cargo_type | source_id |
|---|---|---|---|---|
| 30 | `R` | Procedure R | `CHEMICAL` | 7 |

its `procedure_template_steps`:

| step_order | step_name | medium | temperature | duration | mandatory |
|---|---|---|---|---|---|
| 1 | Butterworthing | Sea Water | 20-30°C | About 2 hours | true |
| 2 | Flushing | Fresh Water | | | true |
| 3 | Steaming | Steam | | | true |
| 4 | Draining | | | | true |
| 5 | Drying | | | | true |

`cleaning_process` id 188735:

| cargo_type | from_cargo_id | to_cargo_id | procedure_template_id | procedure_code | source_id | condition |
|---|---|---|---|---|---|---|
| `CHEMICAL` | 5 (Acetic acid) | 1061 (n-Amyl alcohol) | 30 | `R` | 7 | `NULL` |

Read as: after Acetic acid, to load n-Amyl alcohol, run procedure R.

### OIL — shape, with real `crude_oil` rows

`crude_oil` 704 `ABOOZAR (ARDESHIR)` and 705 `ABU AL BU KHOOSH` exist (source 22,
`Crude Oil Basic Properties`), and the Shell `CW` / `CWM` / `NC` templates now
exist under source 24. **No oil cleaning matrix has been loaded**, so no
`cleaning_process` row with `cargo_type = OIL` exists yet. The shape a loader
would write:

| cargo_type | from_cargo_id | to_cargo_id | procedure_template_id | procedure_code | source_id | condition | source_page_ref |
|---|---|---|---|---|---|---|---|
| `OIL` | *crude_oil.id* | *crude_oil.id* | *→ CW template* | `CW` | *Shell source* | `NULL` | `3` |

### GAS — shape only

`gas` and `gas_property_values` are empty; no gas source exists. The tables exist
so the operational tables have a real master to point at. A row would carry
`cargo_type = GAS` with `from_cargo_id` / `to_cargo_id` drawn from `gas.id`.

No oil or gas cargo-transition data is invented here — none is available.

---

## 11. Shell Ship Pre-Cargo Matrix

**Page 2 is now loaded; page 3 is not.** The Shell procedure definitions were
imported on 2026-08-19 as source 24 — see
[Shell Procedure Import](SHELL_PROCEDURE_IMPORT.md). The cargo-to-cargo matrix on
page 3 still has no data in the repository, so the rest of this section remains a
mapping rather than an implementation, using only the codes and conditions stated
in the requirement.

### Page 2 — the legend → `procedure_templates` — **DONE**

One row per code, `source_page_ref = "2"` (the source file leaves that column
blank, so it is currently NULL). All 11 loaded, each with its ordered steps, its
requirements and the source's own wording in `source_definition`:

| procedure_code | template_name |
|---|---|
| `WD` | Well Drained |
| `BF` | Bottom Flush |
| `BF-VP` | Bottom Flush + Ventilate/Purge |
| `CW` | Cold Water Wash |
| `CW-VP` | Cold Water Wash + Ventilate/Purge |
| `CWM` | Cold Water Wash + Gas-Free + Mop |
| `CFW` | Cold Fresh Water |
| `HW` | Hot Water Wash |
| `HWM` | Hot Water Wash + Gas-Free + Mop |
| `HFW` | Hot Fresh Water |
| `NC` | Not Compatible |

The operations behind each code are stored as `procedure_template_steps`, the
rules and limits as `procedure_template_requirement` (including the
"ventilate OR purge" disjunction, which a step cannot express), and the source's
statements as `procedure_template_instruction`.

`NC` is a template like the others — `loading_allowed = false` and no steps. No
separate compatibility table.

### Page 3 — the matrix → `cleaning_process`

One row per cell, `cargo_type = OIL`, `source_page_ref = "3"`, `from_cargo_id`
and `to_cargo_id` resolved against `crude_oil`, `procedure_template_id`
resolved against the page-2 templates, `procedure_code` kept verbatim.

The requirement's worked example:

| field | value |
|---|---|
| `cargo_type` | `OIL` |
| `from_cargo_id` | Diesel |
| `to_cargo_id` | Gasoline |
| `procedure_template_id` | → `CW` |
| `source_id` | Shell PDF source |
| `source_page_ref` | `3` |

**Conditional cells become several rows, one per condition.** A cell reading
"CWM if Chemical Grade, otherwise NC" is:

| cargo_type | from | to | template | condition |
|---|---|---|---|---|
| `OIL` | X | Y | `CWM` | `If to-load is Chemical Grade` |
| `OIL` | X | Y | `NC` | `otherwise` |

The pair key includes `condition`, so both rows coexist and a re-import upserts
each in place. The conditions named in the requirement — Chemical Grade, Mogas
Blend, Oxy, Feed Stock, NGC, Colour <2.5 NPA, "otherwise NC" — are stored as the
source's own wording in `condition`, never reduced to a boolean. Anything that
qualifies without selecting goes in `remarks`.

**A reader must therefore treat "several rows for one pair" as normal** and
inspect `condition` before acting on the template.

### Page 4 — product characteristics → `crude_oil_property_values`

Measured quantities go to the property table with `source_id` and
`source_page_ref = "4"`, not as columns on `crude_oil`. `normalized_min` /
`normalized_max` already exist there for range-quoted values.

If a page-4 product turns out not to be a crude oil, it needs its own master
row — do not force it into `crude_oil` to make the matrix resolve.

---

## 12. Querying

Every query against the polymorphic columns must filter on `cargo_type`.

```ts
import { getTransition, CargoType } from "./cargo-type.js";

// after Diesel, to load Gasoline
const rules = await getTransition(prisma, CargoType.OIL, "Diesel", "Gasoline");
for (const r of rules) {
  console.log(r.condition ?? "(unconditional)", "->", r.procedure_template?.procedure_code);
}
```

```sql
-- the same question in SQL
SELECT pt.procedure_code, pt.template_name, cp.condition, cp.source_page_ref
  FROM cleaning_process cp
  JOIN crude_oil f ON f.id = cp.from_cargo_id      -- join the master cargo_type names
  JOIN crude_oil t ON t.id = cp.to_cargo_id
  LEFT JOIN procedure_templates pt ON pt.id = cp.procedure_template_id
 WHERE cp.cargo_type = 'OIL'                       -- never omit this
   AND f.oil_name = 'Diesel'
   AND t.oil_name = 'Gasoline';
```

Chemical helpers in `src/cleaning.ts` (`getCleaningProcess`, `getCargoCleaning`,
`getPairProcedure`) already pin `cargo_type = CHEMICAL` internally.

---

## 13. Known gaps

- **Shell page 3 (the matrix) is still not loaded.** The page-2 procedure definitions are in as source 24, and the page-4 product characteristics as source 25 (29 products in `crude_oil`, with their grade names in `crude_oil_synonym`). The cargo-to-cargo matrix that joins the two is not, so no `cleaning_process` row with `cargo_type = OIL` exists yet. §11 remains a mapping for that half.
- **`crude_oil` now holds refined products as well as crude assays** (source 25: ULSD, Jet A1, AVGAS, MTBE). It is acting as the general oil-cargo master. A reader comparing sources will find `API` and `Pour Point` absent there and `Flash Point` / `Density` present instead.
- **`gas` and `gas_property_values` are empty.** Structural, awaiting a gas source.
- **Wrong-but-valid `cargo_type` is undetectable.** See §7. Carry `(cargo_type, id)` pairs through application code.
- **A second database exists.** `192.168.0.132:5440/ship_db` — commented out in `.env`, holds an older, smaller dataset. Both migrations were applied there as well as to the active `localhost:5433`; if it is a live copy rather than a stale one, confirm that is wanted.
