-- Rename the gas master and its property table to match the other two masters.
--
--     gas                 -> cargo_gas
--     gas_property_values -> cargo_gas_property_values
--     gas_property_values.gas_id -> cargo_gas_property_values.cargo_gas_id
--
-- Naming only; no column is added, dropped or retyped and no row moves. Both
-- tables are empty (no gas source has been loaded yet), so this cannot lose
-- data - but it is written to be correct either way.
--
-- gas_name is deliberately NOT renamed: it is the natural-name column, the
-- counterpart of crude_oil.oil_name, and "cargo_gas.gas_name" reads correctly.
-- gas_id IS renamed, because a column named gas_id pointing at cargo_gas is the
-- kind of half-rename that costs an hour a year from now.
--
-- EVERYTHING NAMED AFTER THE OLD TABLE MOVES WITH IT
-- --------------------------------------------------
-- PostgreSQL renames a table without touching the names of its indexes,
-- constraints or sequences, so those are renamed explicitly. Leaving them would
-- ALSO break Prisma: it derives expected constraint names from the model name,
-- so `prisma migrate diff` would report drift on every one of them forever.
--
-- THE TRIGGER FUNCTION IS THE PART THAT ACTUALLY BREAKS
-- -----------------------------------------------------
-- cargo_ref_exists() resolves cleaning_process's polymorphic cargo columns and
-- names the table as a literal:
--
--     WHEN 'GAS' THEN RETURN EXISTS (SELECT 1 FROM "gas" WHERE "id" = ref);
--
-- plpgsql resolves that at RUN time, not at definition time, so a rename leaves
-- the function referring to a table that no longer exists. It would not fail
-- now - no GAS row is ever written - it would fail the first time one is, which
-- is the worst possible moment. Replaced below.
--
-- Idempotent: every step is guarded on the old name still being present.

DO $$
BEGIN
    IF to_regclass('public.gas') IS NOT NULL THEN
        ALTER TABLE "gas" RENAME TO "cargo_gas";
        ALTER SEQUENCE "gas_id_seq" RENAME TO "cargo_gas_id_seq";

        ALTER INDEX "gas_pkey"                    RENAME TO "cargo_gas_pkey";
        ALTER INDEX "gas_id_key"                  RENAME TO "cargo_gas_id_key";
        ALTER INDEX "gas_gas_name_source_id_key"  RENAME TO "cargo_gas_gas_name_source_id_key";
        ALTER INDEX "gas_gas_name_idx"            RENAME TO "cargo_gas_gas_name_idx";
        ALTER INDEX "gas_source_id_idx"           RENAME TO "cargo_gas_source_id_idx";

        ALTER TABLE "cargo_gas"
            RENAME CONSTRAINT "gas_source_id_fkey" TO "cargo_gas_source_id_fkey";
    END IF;

    IF to_regclass('public.gas_property_values') IS NOT NULL THEN
        ALTER TABLE "gas_property_values" RENAME TO "cargo_gas_property_values";
        ALTER SEQUENCE "gas_property_values_id_seq"
            RENAME TO "cargo_gas_property_values_id_seq";

        ALTER TABLE "cargo_gas_property_values" RENAME COLUMN "gas_id" TO "cargo_gas_id";

        ALTER INDEX "gas_property_values_pkey"
            RENAME TO "cargo_gas_property_values_pkey";
        ALTER INDEX "gas_property_values_id_key"
            RENAME TO "cargo_gas_property_values_id_key";
        ALTER INDEX "gas_property_values_gas_id_source_id_field_name_key"
            RENAME TO "cargo_gas_property_values_cargo_gas_id_source_id_field_name_key";
        ALTER INDEX "gas_property_values_gas_id_idx"
            RENAME TO "cargo_gas_property_values_cargo_gas_id_idx";
        ALTER INDEX "gas_property_values_source_id_idx"
            RENAME TO "cargo_gas_property_values_source_id_idx";
        ALTER INDEX "gas_property_values_field_name_idx"
            RENAME TO "cargo_gas_property_values_field_name_idx";
        ALTER INDEX "gas_property_values_gas_id_source_id_idx"
            RENAME TO "cargo_gas_property_values_cargo_gas_id_source_id_idx";
        ALTER INDEX "gas_property_values_gas_id_field_name_idx"
            RENAME TO "cargo_gas_property_values_cargo_gas_id_field_name_idx";

        ALTER TABLE "cargo_gas_property_values"
            RENAME CONSTRAINT "gas_property_values_gas_id_fkey"
                           TO "cargo_gas_property_values_cargo_gas_id_fkey";
        ALTER TABLE "cargo_gas_property_values"
            RENAME CONSTRAINT "gas_property_values_source_id_fkey"
                           TO "cargo_gas_property_values_source_id_fkey";
        ALTER TABLE "cargo_gas_property_values"
            RENAME CONSTRAINT "gas_property_values_field_name_fkey"
                           TO "cargo_gas_property_values_field_name_fkey";
        ALTER TABLE "cargo_gas_property_values"
            RENAME CONSTRAINT "gas_property_values_source_synonym_id_fkey"
                           TO "cargo_gas_property_values_source_synonym_id_fkey";
    END IF;
END $$;

-- Point the polymorphic-reference check at the new table name. CREATE OR
-- REPLACE keeps the OID, so the trigger that calls it needs no change.
--
-- The parameter MUST stay named `ct`: CREATE OR REPLACE cannot rename an input
-- parameter, and renaming it here would abort the migration. The body is
-- otherwise unchanged from the original - one table name, nothing else - so the
-- diff is exactly the fix and nothing rides along with it.
CREATE OR REPLACE FUNCTION cargo_ref_exists(ct "CargoType", ref integer)
RETURNS boolean AS $$
BEGIN
    IF ref IS NULL THEN
        RETURN TRUE;
    END IF;

    CASE ct
        WHEN 'CHEMICAL' THEN RETURN EXISTS (SELECT 1 FROM "cargo_chemical" WHERE "id" = ref);
        WHEN 'OIL'      THEN RETURN EXISTS (SELECT 1 FROM "crude_oil"      WHERE "id" = ref);
        WHEN 'GAS'      THEN RETURN EXISTS (SELECT 1 FROM "cargo_gas"      WHERE "id" = ref);
    END CASE;
END;
$$ LANGUAGE plpgsql STABLE;
