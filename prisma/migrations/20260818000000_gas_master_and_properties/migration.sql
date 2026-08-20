-- Gas master + gas property values.
--
-- Third sibling of cargo_chemical and crude_oil under source. There is
-- deliberately NO FK between the three masters: a gas is not a subtype of a
-- chemical cargo or of a crude oil. Each keeps its own source-scoped
-- property-value table:
--
--                            source
--                              |
--            +-----------------+-----------------+
--            |                 |                 |
--      cargo_chemical      crude_oil            gas
--            |                 |                 |
--            v                 v                 v
--  cargo_property_values  crude_oil_          gas_property_values
--                         property_values
--
-- Nothing in the cargo_chemical or crude_oil branch is modified here.
--
-- Applied by hand via psycopg2 (this database is not prisma-migrate tracked).
-- Idempotent, so it can be re-run safely.

-- ---------------------------------------------------------------------------
-- 1. gas
-- ---------------------------------------------------------------------------
-- Identity is (gas_name, source_id), matching crude_oil: the same gas quoted by
-- two sources keeps two rows with their own values rather than being forced to
-- share one. Measured quantities do NOT belong here - they go in
-- gas_property_values.
CREATE TABLE IF NOT EXISTS "gas" (
    "id"                SERIAL       PRIMARY KEY,
    "gas_name"          TEXT         NOT NULL,
    "source_id"         INTEGER      NOT NULL,
    "country_of_origin" TEXT,
    "created_at"        TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at"        TIMESTAMP(3) NOT NULL
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'gas_source_id_fkey') THEN
        ALTER TABLE "gas"
            ADD CONSTRAINT "gas_source_id_fkey"
            FOREIGN KEY ("source_id") REFERENCES "source"("id")
            ON DELETE CASCADE ON UPDATE CASCADE;
    END IF;
END $$;

-- `id` is @id @unique in the schema (same as crude_oil), which Prisma backs
-- with a second unique index alongside the primary key.
CREATE UNIQUE INDEX IF NOT EXISTS "gas_id_key" ON "gas"("id");
CREATE UNIQUE INDEX IF NOT EXISTS "gas_gas_name_source_id_key" ON "gas"("gas_name", "source_id");
CREATE INDEX IF NOT EXISTS "gas_gas_name_idx"  ON "gas"("gas_name");
CREATE INDEX IF NOT EXISTS "gas_source_id_idx" ON "gas"("source_id");

-- ---------------------------------------------------------------------------
-- 2. gas_property_values
-- ---------------------------------------------------------------------------
-- Mirrors crude_oil_property_values exactly, normalized_min / normalized_max
-- included: gas specifications are quoted as ranges at least as often as crude
-- assays are.
CREATE TABLE IF NOT EXISTS "gas_property_values" (
    "id"                SERIAL       PRIMARY KEY,
    "gas_id"            INTEGER      NOT NULL,
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
    "updated_at"        TIMESTAMP(3) NOT NULL
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'gas_property_values_gas_id_fkey') THEN
        ALTER TABLE "gas_property_values"
            ADD CONSTRAINT "gas_property_values_gas_id_fkey"
            FOREIGN KEY ("gas_id") REFERENCES "gas"("id")
            ON DELETE CASCADE ON UPDATE CASCADE;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'gas_property_values_source_id_fkey') THEN
        ALTER TABLE "gas_property_values"
            ADD CONSTRAINT "gas_property_values_source_id_fkey"
            FOREIGN KEY ("source_id") REFERENCES "source"("id")
            ON DELETE CASCADE ON UPDATE CASCADE;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'gas_property_values_field_name_fkey') THEN
        ALTER TABLE "gas_property_values"
            ADD CONSTRAINT "gas_property_values_field_name_fkey"
            FOREIGN KEY ("field_name") REFERENCES "field_definitions"("field_name")
            ON DELETE CASCADE ON UPDATE CASCADE;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'gas_property_values_source_synonym_id_fkey') THEN
        ALTER TABLE "gas_property_values"
            ADD CONSTRAINT "gas_property_values_source_synonym_id_fkey"
            FOREIGN KEY ("source_synonym_id") REFERENCES "synonyms"("id")
            ON DELETE SET NULL ON UPDATE CASCADE;
    END IF;
END $$;

-- One value per (gas, source, field): two sources may legitimately disagree,
-- so source_id is part of the key.
CREATE UNIQUE INDEX IF NOT EXISTS "gas_property_values_id_key" ON "gas_property_values"("id");
CREATE UNIQUE INDEX IF NOT EXISTS "gas_property_values_gas_id_source_id_field_name_key"
    ON "gas_property_values"("gas_id", "source_id", "field_name");

CREATE INDEX IF NOT EXISTS "gas_property_values_gas_id_idx"            ON "gas_property_values"("gas_id");
CREATE INDEX IF NOT EXISTS "gas_property_values_source_id_idx"         ON "gas_property_values"("source_id");
CREATE INDEX IF NOT EXISTS "gas_property_values_field_name_idx"        ON "gas_property_values"("field_name");
CREATE INDEX IF NOT EXISTS "gas_property_values_gas_id_source_id_idx"  ON "gas_property_values"("gas_id", "source_id");
CREATE INDEX IF NOT EXISTS "gas_property_values_gas_id_field_name_idx" ON "gas_property_values"("gas_id", "field_name");
