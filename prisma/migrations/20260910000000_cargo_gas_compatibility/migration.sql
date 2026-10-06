-- Cargo-to-cargo compatibility for the GAS branch.
--
-- WHY A NEW TABLE
-- ---------------
-- None of the three tables that already hold a compatibility verdict can hold a
-- gas pair:
--
--   compatibility            keyed on reactive_groups. That is the 46 CFR
--                            reactive-group chart - a verdict about two CLASSES
--                            of chemistry, not about two named cargoes. A gas
--                            matrix names its cargoes.
--   compatibility_exception  FKs cargo_chemical on both sides.
--   crude_oil_compatibility  exactly the right SHAPE, and FKs crude_oil.
--
-- cargo_gas is a separate master with no FK to either of the others, so this is
-- the gas twin of crude_oil_compatibility - deliberately the same columns,
-- because it answers the same question about a different master and any
-- divergence between the two would be an accident rather than a decision.
--
-- SYMMETRIC, UNLIKE THE OIL TWIN
-- ------------------------------
-- crude_oil_compatibility is DIRECTIONAL: cargo compatibility in a tank-cleaning
-- guide depends on which cargo left the residue, and in the Verwey oil matrix 13
-- of 28 unordered pairs disagree by direction.
--
-- This table is not that question. A gas compatibility matrix states whether two
-- substances may CO-EXIST - whether ammonia and ethylene oxide react - and that
-- is a fact about the pair, not about an order of events. The source matrix is
-- provably symmetric: all 289 cells of the 17x17 grid agree with their
-- transpose, and the diagonal is compatible throughout.
--
-- So a pair is stored ONCE, in canonical order gas_a_id <= gas_b_id, the same
-- arrangement `compatibility` uses for the reactive-group chart and for the same
-- reason. Storing both directions would double every row and create the
-- possibility of the two halves disagreeing, which the source cannot express.
--
-- Read gas_a_id / gas_b_id as an unordered pair. A reader looking up (X, Y) must
-- normalise the order first; there is no row for (Y, X).
--
-- WHAT A ROW IS NOT
-- -----------------
-- This says nothing about what to DO between two cargoes. "These two must not
-- share a tank" is a different statement from "wash, inspect and purge before
-- loading", which lives in cleaning_process with its ordered steps. A source
-- publishing both - as this one does - writes to both tables.
--
-- Applied by hand via psycopg2 (this database is not prisma-migrate tracked).
-- Idempotent, so it can be re-run safely.

CREATE TABLE IF NOT EXISTS "cargo_gas_compatibility" (
    "id"             SERIAL       PRIMARY KEY,

    -- An UNORDERED pair, stored canonically as gas_a_id <= gas_b_id.
    "gas_a_id"       INTEGER      NOT NULL,
    "gas_b_id"       INTEGER      NOT NULL,

    "compatible"     BOOLEAN      NOT NULL,
    "source_id"      INTEGER      NOT NULL,

    -- The mark the source printed, kept verbatim beside the boolean it was read
    -- as. 'X' and 'Y' mean nothing without the legend, and a later source may
    -- use a third mark; keeping it makes the reading checkable.
    "raw_value"      TEXT,

    -- Where the verdict was read from, and anything the source says about WHY
    -- the pair is incompatible.
    "source_page_ref" TEXT,
    "notes"          TEXT,
    "created_at"     TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at"     TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'cargo_gas_compatibility_gas_a_id_fkey') THEN
        ALTER TABLE "cargo_gas_compatibility"
            ADD CONSTRAINT "cargo_gas_compatibility_gas_a_id_fkey"
            FOREIGN KEY ("gas_a_id") REFERENCES "cargo_gas"("id")
            ON DELETE CASCADE ON UPDATE CASCADE;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'cargo_gas_compatibility_gas_b_id_fkey') THEN
        ALTER TABLE "cargo_gas_compatibility"
            ADD CONSTRAINT "cargo_gas_compatibility_gas_b_id_fkey"
            FOREIGN KEY ("gas_b_id") REFERENCES "cargo_gas"("id")
            ON DELETE CASCADE ON UPDATE CASCADE;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'cargo_gas_compatibility_source_id_fkey') THEN
        ALTER TABLE "cargo_gas_compatibility"
            ADD CONSTRAINT "cargo_gas_compatibility_source_id_fkey"
            FOREIGN KEY ("source_id") REFERENCES "source"("id")
            ON DELETE CASCADE ON UPDATE CASCADE;
    END IF;

    -- Enforce the canonical order in the database rather than trusting every
    -- future loader to remember it. Without this the table would silently
    -- accept (X, Y) and (Y, X) as two different pairs and the unique index
    -- below would not stop it.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'cargo_gas_compatibility_canonical_order') THEN
        ALTER TABLE "cargo_gas_compatibility"
            ADD CONSTRAINT "cargo_gas_compatibility_canonical_order"
            CHECK ("gas_a_id" <= "gas_b_id");
    END IF;
END $$;

-- One answer per pair per source, so two guides may disagree and each keeps its
-- own. Combined with the CHECK above, this makes a duplicate pair impossible in
-- either order.
CREATE UNIQUE INDEX IF NOT EXISTS "cargo_gas_compatibility_pair_source_key"
    ON "cargo_gas_compatibility" ("gas_a_id", "gas_b_id", "source_id");

CREATE INDEX IF NOT EXISTS "cargo_gas_compatibility_gas_a_id_idx"
    ON "cargo_gas_compatibility" ("gas_a_id");
CREATE INDEX IF NOT EXISTS "cargo_gas_compatibility_gas_b_id_idx"
    ON "cargo_gas_compatibility" ("gas_b_id");
CREATE INDEX IF NOT EXISTS "cargo_gas_compatibility_source_id_idx"
    ON "cargo_gas_compatibility" ("source_id");
CREATE INDEX IF NOT EXISTS "cargo_gas_compatibility_compatible_idx"
    ON "cargo_gas_compatibility" ("compatible");
