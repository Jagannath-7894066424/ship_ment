-- Alternative names for a crude oil / refined product.
--
-- The synonyms table is owner-agnostic - it holds the text and nothing else -
-- so reaching a name from the thing it names needs a link table per master.
-- cargo_synonym already does this for cargo_chemical; this is its crude-oil
-- sibling, and it is the reason the Shell "Grade Names" column can be stored at
-- all. Without it those names would sit in synonyms with no path back to the oil.
--
--        crude_oil <--- crude_oil_synonym ---> synonyms
--                              |
--                              v
--                            source        which document asserted the name
--
-- Shaped on cargo_synonym so the two behave alike. Nothing existing is modified.
--
-- Applied by hand via psycopg2:
--     python3 etl/apply_migration.py prisma/migrations/20260819100000_crude_oil_synonym/migration.sql
-- Idempotent, so it can be re-run safely.

CREATE TABLE IF NOT EXISTS "crude_oil_synonym" (
    "id"                   SERIAL       PRIMARY KEY,
    "crude_oil_id"         INTEGER      NOT NULL,
    "synonym_id"           INTEGER      NOT NULL,
    -- Where the name came from in the source. "grade_name" for the Shell Cargo
    -- Master column; leaves room for trade names and abbreviations later.
    "relationship_type"    TEXT         NOT NULL,
    -- True when the same text names more than one product within one source.
    -- "V-Power Diesel" is listed against both ULSD grades AND is a product in
    -- its own right, so a lookup on it cannot resolve to a single oil. Flagged
    -- rather than resolved: the source really is ambiguous there.
    "ambiguity_flag"       BOOLEAN      NOT NULL DEFAULT false,
    "source_id"            INTEGER,
    "preferred_for_search" BOOLEAN,
    "notes"                TEXT,
    "created_at"           TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at"           TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'crude_oil_synonym_crude_oil_id_fkey') THEN
        ALTER TABLE "crude_oil_synonym"
            ADD CONSTRAINT "crude_oil_synonym_crude_oil_id_fkey"
            FOREIGN KEY ("crude_oil_id") REFERENCES "crude_oil"("id")
            ON DELETE CASCADE ON UPDATE CASCADE;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'crude_oil_synonym_synonym_id_fkey') THEN
        ALTER TABLE "crude_oil_synonym"
            ADD CONSTRAINT "crude_oil_synonym_synonym_id_fkey"
            FOREIGN KEY ("synonym_id") REFERENCES "synonyms"("id")
            ON DELETE CASCADE ON UPDATE CASCADE;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'crude_oil_synonym_source_id_fkey') THEN
        ALTER TABLE "crude_oil_synonym"
            ADD CONSTRAINT "crude_oil_synonym_source_id_fkey"
            FOREIGN KEY ("source_id") REFERENCES "source"("id")
            ON DELETE CASCADE ON UPDATE CASCADE;
    END IF;
END $$;

-- Link an oil to a name at most once. This is the loader's idempotency key.
CREATE UNIQUE INDEX IF NOT EXISTS "crude_oil_synonym_crude_oil_id_synonym_id_key"
    ON "crude_oil_synonym"("crude_oil_id", "synonym_id");

CREATE INDEX IF NOT EXISTS "crude_oil_synonym_crude_oil_id_idx" ON "crude_oil_synonym"("crude_oil_id");
CREATE INDEX IF NOT EXISTS "crude_oil_synonym_synonym_id_idx"   ON "crude_oil_synonym"("synonym_id");
CREATE INDEX IF NOT EXISTS "crude_oil_synonym_source_id_idx"    ON "crude_oil_synonym"("source_id");
