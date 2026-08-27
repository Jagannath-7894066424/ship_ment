-- Cargo-to-cargo compatibility for the OIL branch.
--
-- WHY A NEW TABLE
-- ---------------
-- Neither existing table can express it:
--
--   compatibility            group_a_id / group_b_id FK reactive_groups
--   compatibility_exception  cargo_a_id / cargo_b_id FK cargo_chemical
--
-- Both are chemical-branch structures, and crude_oil is a separate master with
-- no FK to either (see the crude_oil docstring in schema.prisma). An oil pair
-- has nowhere to go in them.
--
-- DIRECTIONAL, NOT SYMMETRIC
-- --------------------------
-- This is the one way it deliberately does NOT mirror `compatibility`. That
-- table stores a symmetric pair once, in canonical order group_a_id <=
-- group_b_id, because reactive-group incompatibility is mutual. Cargo
-- compatibility is not: in the Dr Verwey oil matrix 13 of the 28 unordered
-- pairs disagree by direction -
--
--     Crude    -> Gasoil    No        Gasoil -> Crude    Yes
--     Gasolene -> Jet Fuel  No        Jet Fuel -> Gasolene Yes
--
-- which is the physical reality: what may follow what in a tank depends on
-- which cargo left the residue. Storing these in canonical order would collapse
-- the two answers into one and silently keep whichever was written last.
-- from_crude_oil_id and to_crude_oil_id therefore mean exactly what they say,
-- and both directions are separate rows.
--
-- The key includes source_id so two guides may disagree about the same pair and
-- each keeps its own answer, as everywhere else in this schema.
--
-- Idempotent: IF NOT EXISTS throughout.

CREATE TABLE IF NOT EXISTS "crude_oil_compatibility" (
    "id"                SERIAL       PRIMARY KEY,
    "from_crude_oil_id" INTEGER      NOT NULL,
    "to_crude_oil_id"   INTEGER      NOT NULL,
    "compatible"        BOOLEAN      NOT NULL,
    "source_id"         INTEGER      NOT NULL,
    "procedure_code"    TEXT,
    "notes"             TEXT,
    "created_at"        TIMESTAMP(3) NOT NULL DEFAULT now(),
    "updated_at"        TIMESTAMP(3) NOT NULL DEFAULT now()
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'crude_oil_compatibility_from_fkey') THEN
        ALTER TABLE "crude_oil_compatibility"
            ADD CONSTRAINT "crude_oil_compatibility_from_fkey"
            FOREIGN KEY ("from_crude_oil_id") REFERENCES "crude_oil"("id") ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'crude_oil_compatibility_to_fkey') THEN
        ALTER TABLE "crude_oil_compatibility"
            ADD CONSTRAINT "crude_oil_compatibility_to_fkey"
            FOREIGN KEY ("to_crude_oil_id") REFERENCES "crude_oil"("id") ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'crude_oil_compatibility_source_fkey') THEN
        ALTER TABLE "crude_oil_compatibility"
            ADD CONSTRAINT "crude_oil_compatibility_source_fkey"
            FOREIGN KEY ("source_id") REFERENCES "source"("id") ON DELETE CASCADE;
    END IF;
END $$;

-- One answer per directed pair per source; the upsert key for the loader.
CREATE UNIQUE INDEX IF NOT EXISTS "crude_oil_compatibility_pair_source_key"
    ON "crude_oil_compatibility"("from_crude_oil_id", "to_crude_oil_id", "source_id");

CREATE INDEX IF NOT EXISTS "crude_oil_compatibility_from_idx"
    ON "crude_oil_compatibility"("from_crude_oil_id");
CREATE INDEX IF NOT EXISTS "crude_oil_compatibility_to_idx"
    ON "crude_oil_compatibility"("to_crude_oil_id");
CREATE INDEX IF NOT EXISTS "crude_oil_compatibility_source_idx"
    ON "crude_oil_compatibility"("source_id");
CREATE INDEX IF NOT EXISTS "crude_oil_compatibility_compatible_idx"
    ON "crude_oil_compatibility"("compatible");
