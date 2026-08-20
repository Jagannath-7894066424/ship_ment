-- Make procedure_templates and cleaning_process COMMON to all three cargo
-- masters (chemical / oil / gas) instead of chemical-only.
--
-- MASTER DATA STAYS SEPARATE. cargo_chemical, crude_oil and gas are untouched
-- by this migration, as are their property tables. What changes is the two
-- operational tables that sit below all three:
--
--        cargo_chemical      crude_oil          gas
--               \               |               /
--                +--------------+--------------+
--                               |
--                    +----------+----------+
--                    |                     |
--            procedure_templates    cleaning_process
--                    |                     |
--          procedure_template_steps  cleaning_process_step
--
-- Applied by hand via psycopg2 (this database is not prisma-migrate tracked).
-- Idempotent, so it can be re-run safely.
--
-- Every cleaning_process and procedure_templates row that exists today comes
-- from a chemical tank-cleaning guide (sources 7 Verwey, 8 Drew Ameroid,
-- 9 Miracle), so DEFAULT 'CHEMICAL' backfills all of them correctly and no
-- existing behaviour changes.

-- ---------------------------------------------------------------------------
-- 1. CargoType
-- ---------------------------------------------------------------------------
-- Separate from SourceCategory on purpose: SourceCategory describes a
-- *document*, CargoType describes a *row of operational data*.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'CargoType') THEN
        CREATE TYPE "CargoType" AS ENUM ('CHEMICAL', 'OIL', 'GAS');
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 2. procedure_templates: cargo_type + source_page_ref
-- ---------------------------------------------------------------------------
ALTER TABLE "procedure_templates"
    ADD COLUMN IF NOT EXISTS "cargo_type" "CargoType" NOT NULL DEFAULT 'CHEMICAL';

-- Which page of the source the definition was read from - the Shell matrix
-- defines its codes on page 2 and uses them on page 3.
ALTER TABLE "procedure_templates"
    ADD COLUMN IF NOT EXISTS "source_page_ref" TEXT;

CREATE INDEX IF NOT EXISTS "procedure_templates_cargo_type_idx"
    ON "procedure_templates"("cargo_type");
CREATE INDEX IF NOT EXISTS "procedure_templates_source_id_idx"
    ON "procedure_templates"("source_id");
CREATE INDEX IF NOT EXISTS "procedure_templates_procedure_code_idx"
    ON "procedure_templates"("procedure_code");
CREATE INDEX IF NOT EXISTS "procedure_templates_cargo_type_source_id_idx"
    ON "procedure_templates"("cargo_type", "source_id");

-- ---------------------------------------------------------------------------
-- 3. cleaning_process: cargo_type, condition, remarks, procedure_template_id
-- ---------------------------------------------------------------------------
ALTER TABLE "cleaning_process"
    ADD COLUMN IF NOT EXISTS "cargo_type" "CargoType" NOT NULL DEFAULT 'CHEMICAL';

-- The qualifier printed in a matrix cell, verbatim ("If to-load is Chemical
-- Grade", "Mogas Blend", "Colour <2.5 NPA", "otherwise NC"). A cell carrying
-- two conditional outcomes becomes two rows. Deliberately NOT reduced to a
-- compatible/not-compatible boolean - that would throw the rule away and keep
-- only its verdict.
ALTER TABLE "cleaning_process"
    ADD COLUMN IF NOT EXISTS "condition" TEXT;

-- Free-text qualification that is not itself a selection condition.
ALTER TABLE "cleaning_process"
    ADD COLUMN IF NOT EXISTS "remarks" TEXT;

ALTER TABLE "cleaning_process"
    ADD COLUMN IF NOT EXISTS "procedure_template_id" INTEGER;

-- ---------------------------------------------------------------------------
-- 4. Replace the (source_id, procedure_code) composite FK with a direct id FK
-- ---------------------------------------------------------------------------
-- The composite FK forced a process and its template into the same source row,
-- which no longer holds: an operator matrix may cite a procedure defined in a
-- different document. procedure_code is KEPT (verbatim source wording, and the
-- idempotency key of the template rows), it just stops being a foreign key.
UPDATE "cleaning_process" cp
   SET "procedure_template_id" = pt."id"
  FROM "procedure_templates" pt
 WHERE pt."source_id" = cp."source_id"
   AND pt."procedure_code" = cp."procedure_code"
   AND cp."procedure_template_id" IS NULL
   AND cp."procedure_code" IS NOT NULL;

ALTER TABLE "cleaning_process"
    DROP CONSTRAINT IF EXISTS "cleaning_process_source_id_procedure_code_fkey";

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'cleaning_process_procedure_template_id_fkey') THEN
        ALTER TABLE "cleaning_process"
            ADD CONSTRAINT "cleaning_process_procedure_template_id_fkey"
            FOREIGN KEY ("procedure_template_id") REFERENCES "procedure_templates"("id")
            ON DELETE SET NULL ON UPDATE CASCADE;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS "cleaning_process_procedure_template_id_idx"
    ON "cleaning_process"("procedure_template_id");

-- ---------------------------------------------------------------------------
-- 5. Make the cargo columns polymorphic
-- ---------------------------------------------------------------------------
-- cargo_id / from_cargo_id / to_cargo_id now hold an id from cargo_chemical,
-- crude_oil OR gas depending on cargo_type. PostgreSQL cannot retarget a
-- foreign key per row, so the three FKs to cargo_chemical MUST go.
--
-- This is the one integrity loss in this migration and it is compensated, not
-- accepted: the trigger in section 6 checks the referent in the right master
-- table on every insert/update, and src/cargo-type.ts checks it again on the
-- write path. What the FKs also gave us - ON DELETE CASCADE from
-- cargo_chemical - is replaced by the delete trigger in section 7.
ALTER TABLE "cleaning_process" DROP CONSTRAINT IF EXISTS "cleaning_process_cargo_id_fkey";
ALTER TABLE "cleaning_process" DROP CONSTRAINT IF EXISTS "cleaning_process_from_cargo_id_fkey";
ALTER TABLE "cleaning_process" DROP CONSTRAINT IF EXISTS "cleaning_process_to_cargo_id_fkey";

-- The family columns KEEP their FK: cargo_family_group hangs off cargo_chemical
-- and is chemical-only, so it is not polymorphic.
ALTER TABLE "cleaning_process"
    DROP CONSTRAINT IF EXISTS "cleaning_process_family_is_chemical";
ALTER TABLE "cleaning_process"
    ADD CONSTRAINT "cleaning_process_family_is_chemical" CHECK (
        "cargo_type" = 'CHEMICAL'
        OR ("from_cargo_family_group_id" IS NULL AND "to_cargo_family_group_id" IS NULL)
    );

-- A transition is a pair: either both ends are set or neither is. Guards
-- against a half-written matrix row that silently matches nothing.
ALTER TABLE "cleaning_process"
    DROP CONSTRAINT IF EXISTS "cleaning_process_pair_complete";
ALTER TABLE "cleaning_process"
    ADD CONSTRAINT "cleaning_process_pair_complete" CHECK (
        ("from_cargo_id" IS NULL) = ("to_cargo_id" IS NULL)
    );

-- A row is a single-cargo procedure or a transition, never both.
ALTER TABLE "cleaning_process"
    DROP CONSTRAINT IF EXISTS "cleaning_process_cargo_xor_pair";
ALTER TABLE "cleaning_process"
    ADD CONSTRAINT "cleaning_process_cargo_xor_pair" CHECK (
        "cargo_id" IS NULL OR ("from_cargo_id" IS NULL AND "to_cargo_id" IS NULL)
    );

-- ---------------------------------------------------------------------------
-- 6. Referential integrity for the polymorphic columns
-- ---------------------------------------------------------------------------
-- Does the work a dynamic FK would do if PostgreSQL had one. Application-layer
-- validation (src/cargo-type.ts) is the first line of defence; this is the
-- backstop that also covers psql, the ETL loaders and any future writer.
--
-- NOT VALID-style deferral is not used: existing rows are all CHEMICAL with
-- valid cargo_chemical ids, so the trigger is satisfiable from the start.
-- Resolves one polymorphic reference. NULL ref is always valid (the column is
-- optional); otherwise the id must exist in the master cargo_type names.
CREATE OR REPLACE FUNCTION "cargo_ref_exists"(ct "CargoType", ref INTEGER)
RETURNS BOOLEAN AS $$
BEGIN
    IF ref IS NULL THEN
        RETURN TRUE;
    END IF;

    CASE ct
        WHEN 'CHEMICAL' THEN RETURN EXISTS (SELECT 1 FROM "cargo_chemical" WHERE "id" = ref);
        WHEN 'OIL'      THEN RETURN EXISTS (SELECT 1 FROM "crude_oil"      WHERE "id" = ref);
        WHEN 'GAS'      THEN RETURN EXISTS (SELECT 1 FROM "gas"            WHERE "id" = ref);
    END CASE;
END;
$$ LANGUAGE plpgsql STABLE;

CREATE OR REPLACE FUNCTION "cleaning_process_check_cargo_refs"()
RETURNS TRIGGER AS $$
BEGIN
    IF NOT "cargo_ref_exists"(NEW."cargo_type", NEW."cargo_id") THEN
        RAISE EXCEPTION 'cleaning_process.cargo_id=% is not a % master row',
            NEW."cargo_id", NEW."cargo_type" USING ERRCODE = 'foreign_key_violation';
    END IF;

    IF NOT "cargo_ref_exists"(NEW."cargo_type", NEW."from_cargo_id") THEN
        RAISE EXCEPTION 'cleaning_process.from_cargo_id=% is not a % master row',
            NEW."from_cargo_id", NEW."cargo_type" USING ERRCODE = 'foreign_key_violation';
    END IF;

    IF NOT "cargo_ref_exists"(NEW."cargo_type", NEW."to_cargo_id") THEN
        RAISE EXCEPTION 'cleaning_process.to_cargo_id=% is not a % master row',
            NEW."to_cargo_id", NEW."cargo_type" USING ERRCODE = 'foreign_key_violation';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS "cleaning_process_cargo_refs" ON "cleaning_process";
CREATE TRIGGER "cleaning_process_cargo_refs"
    BEFORE INSERT OR UPDATE OF "cargo_type", "cargo_id", "from_cargo_id", "to_cargo_id"
    ON "cleaning_process"
    FOR EACH ROW EXECUTE FUNCTION "cleaning_process_check_cargo_refs"();

-- ---------------------------------------------------------------------------
-- 7. Replace the cascade the dropped FKs used to provide
-- ---------------------------------------------------------------------------
-- Deleting a master row used to take its cleaning_process rows with it. Without
-- the FK that stops happening, leaving rows pointing at nothing. One AFTER
-- DELETE trigger per master restores it, scoped by cargo_type so a chemical
-- delete never touches an oil row that happens to share an id.
CREATE OR REPLACE FUNCTION "cleaning_process_cascade_master_delete"()
RETURNS TRIGGER AS $$
DECLARE
    ct "CargoType" := TG_ARGV[0]::"CargoType";
BEGIN
    DELETE FROM "cleaning_process"
     WHERE "cargo_type" = ct
       AND (   "cargo_id"      = OLD."id"
            OR "from_cargo_id" = OLD."id"
            OR "to_cargo_id"   = OLD."id");
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS "cargo_chemical_cleaning_process_cascade" ON "cargo_chemical";
CREATE TRIGGER "cargo_chemical_cleaning_process_cascade"
    AFTER DELETE ON "cargo_chemical"
    FOR EACH ROW EXECUTE FUNCTION "cleaning_process_cascade_master_delete"('CHEMICAL');

DROP TRIGGER IF EXISTS "crude_oil_cleaning_process_cascade" ON "crude_oil";
CREATE TRIGGER "crude_oil_cleaning_process_cascade"
    AFTER DELETE ON "crude_oil"
    FOR EACH ROW EXECUTE FUNCTION "cleaning_process_cascade_master_delete"('OIL');

DROP TRIGGER IF EXISTS "gas_cleaning_process_cascade" ON "gas";
CREATE TRIGGER "gas_cleaning_process_cascade"
    AFTER DELETE ON "gas"
    FOR EACH ROW EXECUTE FUNCTION "cleaning_process_cascade_master_delete"('GAS');

-- ---------------------------------------------------------------------------
-- 8. Idempotency keys
-- ---------------------------------------------------------------------------
-- The cargo-pair key gains condition. One matrix cell can carry several
-- conditional outcomes for the SAME pair ("CWM if Chemical Grade, otherwise
-- NC"), which are separate rows; the old key collapsed them into one.
-- COALESCE is required because NULL <> NULL in a unique index, so an
-- unconditional row would otherwise never conflict with itself.
--
-- cargo_type is deliberately NOT in this key: source.category is single-valued,
-- so all rows of one source share a cargo_type and it would add nothing.
DROP INDEX IF EXISTS "cleaning_process_pair_key";
CREATE UNIQUE INDEX IF NOT EXISTS "cleaning_process_pair_key"
    ON "cleaning_process"("from_cargo_id", "to_cargo_id", "source_id", (COALESCE("condition", '')))
    WHERE "from_cargo_id" IS NOT NULL AND "to_cargo_id" IS NOT NULL;

CREATE INDEX IF NOT EXISTS "cleaning_process_cargo_type_idx"
    ON "cleaning_process"("cargo_type");
CREATE INDEX IF NOT EXISTS "cleaning_process_cargo_type_source_id_idx"
    ON "cleaning_process"("cargo_type", "source_id");
CREATE INDEX IF NOT EXISTS "cleaning_process_cargo_type_from_cargo_id_idx"
    ON "cleaning_process"("cargo_type", "from_cargo_id");
CREATE INDEX IF NOT EXISTS "cleaning_process_cargo_type_to_cargo_id_idx"
    ON "cleaning_process"("cargo_type", "to_cargo_id");
