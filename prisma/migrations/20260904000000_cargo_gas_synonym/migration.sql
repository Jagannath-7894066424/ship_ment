-- Link table joining cargo_gas to the shared `synonyms` table.
--
-- WHY THIS TABLE EXISTS
-- ---------------------
-- `synonyms` is owner-agnostic: it holds a name and nothing about what the name
-- names. Each master therefore needs its own link table, and two already exist -
-- cargo_synonym for cargo_chemical and crude_oil_synonym for crude_oil. There
-- was none for cargo_gas, so a gas source's alternative names had nowhere to go
-- that a lookup could reach: a name could be stored on the gas as a property
-- value, but then "which cargo is 'VCM'?" is a scan of property text rather than
-- an indexed join, and the same text is stored again per source instead of
-- resolving to the one `synonyms` row the other two branches already share.
--
--                             synonyms
--                                 |
--            +--------------------+--------------------+
--            |                    |                    |
--     cargo_synonym       crude_oil_synonym     cargo_gas_synonym
--            |                    |                    |
--     cargo_chemical          crude_oil            cargo_gas
--
-- This table is a deliberate copy of crude_oil_synonym's shape - same columns,
-- same nullability, same unique key - because it answers the same question
-- about a different master. Divergence between the three would be an accident,
-- not a design.
--
-- WHY source_id IS HERE WHEN cargo_gas IS ALREADY PER-SOURCE
-- ----------------------------------------------------------
-- cargo_gas is keyed (gas_name, source_id), so a gas row already belongs to one
-- source. The link still carries its own source_id, for the same reason the
-- other two do: it records WHO ASSERTED THE NAME. The `synonyms` row is shared
-- across branches and keeps the source that first published the text, so
-- without this column a name introduced by one source and reused by another
-- could not be attributed to the source that applied it to THIS cargo.
--
-- WHY relationship_type IS NOT NULLABLE
-- --------------------------------------
-- A name is not simply "another name": crude_oil_synonym distinguishes a grade
-- name from a trade name, and a gas source may print an abbreviation ('AMA'),
-- a trade name, or a true alternative chemical name. Storing which kind it is
-- is what keeps a three-letter code from being served as the cargo's name.
--
-- WHY ambiguity_flag IS NOT NULLABLE EITHER
-- ------------------------------------------
-- Some codes resolve to more than one cargo within a single source - IMO
-- Cargo.XLSX gives both Butane-n and Butane-i the code 'BUT' - so a lookup on
-- the name cannot pick one gas. The flag records that the source is ambiguous
-- there rather than resolving it by guesswork. Defaulted false so a loader that
-- has not computed it yet states the ordinary case rather than NULL.
--
-- Applied by hand via psycopg2 (this database is not prisma-migrate tracked).
-- Idempotent, so it can be re-run safely.

CREATE TABLE IF NOT EXISTS "cargo_gas_synonym" (
    "id"                   SERIAL       PRIMARY KEY,
    "cargo_gas_id"         INTEGER      NOT NULL,
    "synonym_id"           INTEGER      NOT NULL,

    -- What kind of name this is, in the source's own terms: 'abbreviation',
    -- 'trade_name', 'common', ... See the header.
    "relationship_type"    TEXT         NOT NULL,
    -- True when this text names more than one cargo within the same source.
    "ambiguity_flag"       BOOLEAN      NOT NULL DEFAULT false,
    -- The source that applied this name to THIS cargo, which is not always the
    -- source that first published the text into `synonyms`.
    "source_id"            INTEGER,
    "preferred_for_search" BOOLEAN,
    "notes"                TEXT,

    "created_at"           TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at"           TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'cargo_gas_synonym_cargo_gas_id_fkey') THEN
        ALTER TABLE "cargo_gas_synonym"
            ADD CONSTRAINT "cargo_gas_synonym_cargo_gas_id_fkey"
            FOREIGN KEY ("cargo_gas_id") REFERENCES "cargo_gas"("id")
            ON DELETE CASCADE ON UPDATE CASCADE;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'cargo_gas_synonym_synonym_id_fkey') THEN
        ALTER TABLE "cargo_gas_synonym"
            ADD CONSTRAINT "cargo_gas_synonym_synonym_id_fkey"
            FOREIGN KEY ("synonym_id") REFERENCES "synonyms"("id")
            ON DELETE CASCADE ON UPDATE CASCADE;
    END IF;

    -- Nullable FK, matching cargo_synonym and crude_oil_synonym: a link may
    -- predate knowing which source applied the name.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'cargo_gas_synonym_source_id_fkey') THEN
        ALTER TABLE "cargo_gas_synonym"
            ADD CONSTRAINT "cargo_gas_synonym_source_id_fkey"
            FOREIGN KEY ("source_id") REFERENCES "source"("id")
            ON DELETE CASCADE ON UPDATE CASCADE;
    END IF;
END $$;

-- Link a gas to a name at most once. cargo_gas is already per-source, so this
-- says "one link per (gas row, name)" and lets two sources that both call the
-- same gas 'BUT' each keep their own link on their own gas row.
CREATE UNIQUE INDEX IF NOT EXISTS "cargo_gas_synonym_cargo_gas_id_synonym_id_key"
    ON "cargo_gas_synonym"("cargo_gas_id", "synonym_id");

CREATE INDEX IF NOT EXISTS "cargo_gas_synonym_cargo_gas_id_idx"
    ON "cargo_gas_synonym"("cargo_gas_id");
-- The query this table exists for: "which cargo is 'VCM'?"
CREATE INDEX IF NOT EXISTS "cargo_gas_synonym_synonym_id_idx"
    ON "cargo_gas_synonym"("synonym_id");
CREATE INDEX IF NOT EXISTS "cargo_gas_synonym_source_id_idx"
    ON "cargo_gas_synonym"("source_id");
