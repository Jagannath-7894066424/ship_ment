-- Crude oil data model + source categorisation.
--
-- crude_oil and cargo_chemical are INDEPENDENT entities. There is deliberately
-- no FK between them: a crude oil is not a subtype of a chemical cargo. Each
-- has its own source-scoped property-value table:
--
--                       source
--                          |
--              +-----------+-----------+
--              |                       |
--        cargo_chemical            crude_oil
--              |                       |
--              v                       v
--   cargo_property_values   crude_oil_property_values
--
-- Nothing in the cargo_chemical branch is modified by this migration.
--
-- Applied by hand via psycopg2 (this database is not prisma-migrate tracked -
-- _prisma_migrations is empty because the schema was built with `prisma db
-- push`). Written to be idempotent so it can be re-run safely.

-- ---------------------------------------------------------------------------
-- 1. source.category
-- ---------------------------------------------------------------------------
-- Every source that exists today is a chemical source, so DEFAULT 'chemical'
-- backfills them correctly as the column is added. The two crude-oil sources
-- are inserted by etl/source.py from source.json with category 'oil'.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'SourceCategory') THEN
        CREATE TYPE "SourceCategory" AS ENUM ('chemical', 'oil', 'gas');
    END IF;
END $$;

ALTER TABLE "source"
    ADD COLUMN IF NOT EXISTS "category" "SourceCategory" NOT NULL DEFAULT 'chemical';

CREATE INDEX IF NOT EXISTS "source_category_idx" ON "source"("category");

-- ---------------------------------------------------------------------------
-- 2. crude_oil
-- ---------------------------------------------------------------------------
-- Identity is (oil_name, source_id), NOT oil_name alone. The same physical
-- crude appears in several sources with different assay values, and each
-- source's record must stand on its own. A global unique on oil_name would
-- force two sources to share one row and destroy that separation.
CREATE TABLE IF NOT EXISTS "crude_oil" (
    "id"                SERIAL       PRIMARY KEY,
    "oil_name"          TEXT         NOT NULL,
    "source_id"         INTEGER      NOT NULL,
    "country_of_origin" TEXT,
    "created_at"        TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at"        TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'crude_oil_source_id_fkey') THEN
        ALTER TABLE "crude_oil"
            ADD CONSTRAINT "crude_oil_source_id_fkey"
            FOREIGN KEY ("source_id") REFERENCES "source"("id")
            ON DELETE CASCADE ON UPDATE CASCADE;
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS "crude_oil_oil_name_source_id_key"
    ON "crude_oil"("oil_name", "source_id");
CREATE INDEX IF NOT EXISTS "crude_oil_oil_name_idx"  ON "crude_oil"("oil_name");
CREATE INDEX IF NOT EXISTS "crude_oil_source_id_idx" ON "crude_oil"("source_id");

-- ---------------------------------------------------------------------------
-- 3. crude_oil_property_values
-- ---------------------------------------------------------------------------
-- Mirrors cargo_property_values, with two additions the crude-oil sources
-- require: normalized_min / normalized_max, because the assay source quotes
-- most properties as Min/Max ranges (API 31.60 - 33.80) rather than points.
-- cargo_property_values is left untouched.
CREATE TABLE IF NOT EXISTS "crude_oil_property_values" (
    "id"                SERIAL       PRIMARY KEY,
    "crude_oil_id"      INTEGER      NOT NULL,
    "source_id"         INTEGER      NOT NULL,
    "field_name"        TEXT         NOT NULL,
    "value"             TEXT,
    "normalized_value"  DOUBLE PRECISION,
    "normalized_min"    DOUBLE PRECISION,
    "normalized_max"    DOUBLE PRECISION,
    "unit"              TEXT,
    "value_type"        TEXT,
    "source_synonym_id" INTEGER,
    "source_page_ref"   TEXT,
    "as_of_date"        TIMESTAMP(3),
    "entered_date"      TIMESTAMP(3) NOT NULL,
    "entered_by"        TEXT         NOT NULL,
    "entry_type"        TEXT         NOT NULL,
    "is_winning"        BOOLEAN      NOT NULL,
    "conflict_flag"     BOOLEAN      NOT NULL,
    "notes"             TEXT,
    "created_at"        TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at"        TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'crude_oil_property_values_crude_oil_id_fkey') THEN
        ALTER TABLE "crude_oil_property_values"
            ADD CONSTRAINT "crude_oil_property_values_crude_oil_id_fkey"
            FOREIGN KEY ("crude_oil_id") REFERENCES "crude_oil"("id")
            ON DELETE CASCADE ON UPDATE CASCADE;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'crude_oil_property_values_source_id_fkey') THEN
        ALTER TABLE "crude_oil_property_values"
            ADD CONSTRAINT "crude_oil_property_values_source_id_fkey"
            FOREIGN KEY ("source_id") REFERENCES "source"("id")
            ON DELETE CASCADE ON UPDATE CASCADE;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'crude_oil_property_values_field_name_fkey') THEN
        ALTER TABLE "crude_oil_property_values"
            ADD CONSTRAINT "crude_oil_property_values_field_name_fkey"
            FOREIGN KEY ("field_name") REFERENCES "field_definitions"("field_name")
            ON DELETE CASCADE ON UPDATE CASCADE;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'crude_oil_property_values_source_synonym_id_fkey') THEN
        ALTER TABLE "crude_oil_property_values"
            ADD CONSTRAINT "crude_oil_property_values_source_synonym_id_fkey"
            FOREIGN KEY ("source_synonym_id") REFERENCES "synonyms"("id")
            ON DELETE SET NULL ON UPDATE CASCADE;
    END IF;
END $$;

-- One value per (oil, source, field). Two sources may hold different values for
-- the same field on the same oil - that is legitimate, not a conflict, so the
-- constraint includes source_id.
CREATE UNIQUE INDEX IF NOT EXISTS "crude_oil_property_values_crude_oil_id_source_id_field_name_key"
    ON "crude_oil_property_values"("crude_oil_id", "source_id", "field_name");

CREATE INDEX IF NOT EXISTS "crude_oil_property_values_crude_oil_id_idx"
    ON "crude_oil_property_values"("crude_oil_id");
CREATE INDEX IF NOT EXISTS "crude_oil_property_values_source_id_idx"
    ON "crude_oil_property_values"("source_id");
CREATE INDEX IF NOT EXISTS "crude_oil_property_values_field_name_idx"
    ON "crude_oil_property_values"("field_name");
CREATE INDEX IF NOT EXISTS "crude_oil_property_values_crude_oil_id_source_id_idx"
    ON "crude_oil_property_values"("crude_oil_id", "source_id");
CREATE INDEX IF NOT EXISTS "crude_oil_property_values_crude_oil_id_field_name_idx"
    ON "crude_oil_property_values"("crude_oil_id", "field_name");
