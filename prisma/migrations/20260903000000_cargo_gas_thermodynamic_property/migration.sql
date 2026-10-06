-- Thermodynamic property tables for gas cargoes.
--
-- WHY A NEW TABLE AND NOT cargo_gas_property_values
-- --------------------------------------------------
-- cargo_gas_property_values is keyed:
--
--     UNIQUE (cargo_gas_id, source_id, field_name)
--
-- which says: one gas has ONE value of a given property per source. That is
-- correct for molecular weight and for a boiling point, and it is exactly wrong
-- for a thermodynamic table. Butadiene's saturated vapour density is 0.5 kg/m3
-- at -50 C and 11.6 kg/m3 at +50 C - 101 values, one per degree. The unique key
-- admits one of them, so loading the table there would either fail on the
-- second row or, with an upsert, silently overwrite 100 values and leave the
-- last one looking like "the" vapour density.
--
-- Widening that key with temperature/pressure columns is not an option either:
-- it would put two nullable dimensions on every ordinary property row (a
-- molecular weight has no temperature) and make the "one property per source"
-- guarantee unenforceable for the rows that DO need it.
--
-- So the two live apart, on the same master:
--
--                              source
--                                |
--                            cargo_gas
--                                |
--            +-------------------+--------------------+
--            |                                        |
--  cargo_gas_property_values         cargo_gas_thermodynamic_property
--  one value per (gas, source,        one value per (gas, source, state,
--  field) - state-independent         phase, property) - state-dependent
--
-- PRESSURE IS NOT THE SAME KIND OF COLUMN IN BOTH STATES
-- ------------------------------------------------------
-- In the saturated table temperature is the ONLY independent variable: the
-- pressure printed beside it is the vapour pressure AT that temperature, a
-- dependent property, and it is stored as one - property_name =
-- 'vapour_pressure'. Writing it into the pressure column as well would state
-- that the row was measured at a pressure chosen independently, which is not
-- what a saturation table means.
--
-- In the superheated table temperature and pressure are BOTH independent: the
-- source prints a grid of them. There pressure is the state column.
--
-- The check constraint below enforces exactly that: pressure IS NULL for
-- SATURATED rows, NOT NULL for SUPERHEATED ones.
--
-- WHY TWO PARTIAL UNIQUE INDEXES INSTEAD OF ONE UNIQUE CONSTRAINT
-- ---------------------------------------------------------------
-- The natural key differs by state - (temperature) for saturated,
-- (temperature, pressure) for superheated - and a single unique constraint
-- covering both would have to include the nullable pressure column. In
-- PostgreSQL two NULLs are never equal, so such a constraint would permit
-- unlimited duplicate saturated rows: precisely the rows it exists to protect.
-- NULLS NOT DISTINCT would fix that, and this server is PostgreSQL 12, where it
-- does not exist (PG 15+).
--
-- Two partial unique indexes solve it with no NULL in either key, and they say
-- the true rule rather than a weaker version of it. Prisma cannot express a
-- partial index, so neither appears in schema.prisma - the model carries a
-- comment pointing here, the same arrangement cleaning_process already uses for
-- its polymorphic FK trigger.
--
-- Applied by hand via psycopg2 (this database is not prisma-migrate tracked).
-- Idempotent, so it can be re-run safely.

-- ---------------------------------------------------------------------------
-- 1. Enums
-- ---------------------------------------------------------------------------
-- Which of the source's two tables a row came from. These are not
-- interchangeable readings of one quantity: a saturated state sits on the
-- boiling line, a superheated state is above it.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'GasThermoPropertyType') THEN
        CREATE TYPE "GasThermoPropertyType" AS ENUM ('SATURATED', 'SUPERHEATED');
    END IF;
END $$;

-- Which phase a property was measured in. The saturated table prints specific
-- volume, density and enthalpy TWICE - once for vapour, once for liquid - so
-- the phase has to be part of the row's identity. NONE is for the properties
-- that have no phase: vapour pressure and latent heat are properties of the
-- equilibrium, not of one side of it.
--
-- NONE exists rather than a NULL phase for the same reason as above: phase is
-- part of the unique key, and a nullable key column silently stops enforcing
-- uniqueness on PostgreSQL 12.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'GasPhase') THEN
        CREATE TYPE "GasPhase" AS ENUM ('VAPOUR', 'LIQUID', 'NONE');
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 2. cargo_gas_thermodynamic_property
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS "cargo_gas_thermodynamic_property" (
    "id"               SERIAL       PRIMARY KEY,
    "cargo_gas_id"     INTEGER      NOT NULL,
    "source_id"        INTEGER      NOT NULL,

    "property_type"    "GasThermoPropertyType" NOT NULL,

    -- State. temperature is always independent; pressure only in SUPERHEATED.
    "temperature"      DOUBLE PRECISION NOT NULL,
    "temperature_unit" TEXT         NOT NULL DEFAULT '°C',
    "pressure"         DOUBLE PRECISION,
    "pressure_unit"    TEXT,

    "phase"            "GasPhase"   NOT NULL DEFAULT 'NONE',
    "property_name"    TEXT         NOT NULL,

    -- value is the parsed number; raw_value is the cell exactly as printed, so
    -- a reader can always check what was normalised. A row exists only where
    -- the source gives a figure - see the loader on why a printed 0 in the
    -- superheated grid is an absent state, not a measurement.
    "value"            DOUBLE PRECISION NOT NULL,
    "raw_value"        TEXT         NOT NULL,
    "unit"             TEXT         NOT NULL,

    "source_page_ref"  TEXT,
    "notes"            TEXT,
    "created_at"       TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at"       TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'cargo_gas_thermodynamic_property_cargo_gas_id_fkey') THEN
        ALTER TABLE "cargo_gas_thermodynamic_property"
            ADD CONSTRAINT "cargo_gas_thermodynamic_property_cargo_gas_id_fkey"
            FOREIGN KEY ("cargo_gas_id") REFERENCES "cargo_gas"("id")
            ON DELETE CASCADE ON UPDATE CASCADE;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'cargo_gas_thermodynamic_property_source_id_fkey') THEN
        ALTER TABLE "cargo_gas_thermodynamic_property"
            ADD CONSTRAINT "cargo_gas_thermodynamic_property_source_id_fkey"
            FOREIGN KEY ("source_id") REFERENCES "source"("id")
            ON DELETE CASCADE ON UPDATE CASCADE;
    END IF;

    -- Pressure belongs to the superheated grid only. See the header.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'cargo_gas_thermodynamic_property_pressure_by_type_check') THEN
        ALTER TABLE "cargo_gas_thermodynamic_property"
            ADD CONSTRAINT "cargo_gas_thermodynamic_property_pressure_by_type_check"
            CHECK (
                ("property_type" = 'SATURATED'   AND "pressure" IS NULL) OR
                ("property_type" = 'SUPERHEATED' AND "pressure" IS NOT NULL)
            );
    END IF;

    -- A pressure without its unit is a number nobody can use.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'cargo_gas_thermodynamic_property_pressure_unit_check') THEN
        ALTER TABLE "cargo_gas_thermodynamic_property"
            ADD CONSTRAINT "cargo_gas_thermodynamic_property_pressure_unit_check"
            CHECK (("pressure" IS NULL) = ("pressure_unit" IS NULL));
    END IF;
END $$;

-- One reading per saturated state: temperature identifies the state on its own.
CREATE UNIQUE INDEX IF NOT EXISTS "cargo_gas_thermo_saturated_key"
    ON "cargo_gas_thermodynamic_property"
       ("cargo_gas_id", "source_id", "temperature", "phase", "property_name")
    WHERE "property_type" = 'SATURATED';

-- One reading per superheated state: here it takes both temperature and pressure.
CREATE UNIQUE INDEX IF NOT EXISTS "cargo_gas_thermo_superheated_key"
    ON "cargo_gas_thermodynamic_property"
       ("cargo_gas_id", "source_id", "temperature", "pressure", "phase", "property_name")
    WHERE "property_type" = 'SUPERHEATED';

CREATE INDEX IF NOT EXISTS "cargo_gas_thermodynamic_property_cargo_gas_id_idx"
    ON "cargo_gas_thermodynamic_property"("cargo_gas_id");
CREATE INDEX IF NOT EXISTS "cargo_gas_thermodynamic_property_source_id_idx"
    ON "cargo_gas_thermodynamic_property"("source_id");
CREATE INDEX IF NOT EXISTS "cargo_gas_thermodynamic_property_property_name_idx"
    ON "cargo_gas_thermodynamic_property"("property_name");
CREATE INDEX IF NOT EXISTS "cargo_gas_thermodynamic_property_property_type_idx"
    ON "cargo_gas_thermodynamic_property"("property_type");
-- The query this table exists for: "give me property X of gas G across the
-- temperature range", and its superheated form with a pressure.
CREATE INDEX IF NOT EXISTS "cargo_gas_thermo_lookup_idx"
    ON "cargo_gas_thermodynamic_property"
       ("cargo_gas_id", "property_type", "property_name", "temperature");
