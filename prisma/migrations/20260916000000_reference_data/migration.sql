-- Reference numbers published by a source, as a source-scoped lookup table.
--
-- WHAT A ROW IS
-- -------------
-- One numbered reference as a source prints it: a clause number, a table
-- number, a paragraph number, a chart number. `reference_no` is the number the
-- source shows, `sub_reference_no` the sub-division under it when the source
-- splits one ("4.2" / "a"), and `description` the caption or heading printed
-- beside it. `additional_reference_no` is a cross-reference the source itself
-- points at from that entry ("see also 7.1").
--
-- WHY THE NUMBER COLUMNS ARE TEXT AND NOT INTEGER
-- -----------------------------------------------
-- Published reference numbers are not integers. They are dotted ("4.2.1"),
-- lettered ("17a"), prefixed ("Table 3", "Annex II"), and sometimes ranged
-- ("15-17"). Storing them as text keeps what the source printed; parsing them
-- into numbers would lose the form a reader has to be shown back, and there is
-- no arithmetic this table needs to do on them.
--
-- WHY source_id IS NOT NULL
-- -------------------------
-- A reference number means nothing detached from the document that numbered it
-- - "4.2" is a different clause in every source - so a row without a source is
-- not a weaker row, it is an unusable one. Every other source-scoped table in
-- this schema that could not be read without its source makes the column
-- mandatory, and this follows them. ON DELETE CASCADE, as everywhere else here:
-- deleting a source removes what that source asserted.
--
-- NO UNIQUE KEY, DELIBERATELY
-- ---------------------------
-- The natural key would be (source_id, reference_no, sub_reference_no), but
-- sub_reference_no is nullable and PostgreSQL treats NULLs as distinct, so such
-- an index would not stop duplicate top-level entries and could not serve as an
-- ON CONFLICT target for them either - it would look like protection while
-- giving none. The loaders for this database reload their table wholesale
-- rather than upserting row by row, so nothing needs the key today. If a loader
-- ever does, add it then together with a decision about how a missing
-- sub-reference is represented.
--
-- Applied by hand via psycopg2 (this database is not prisma-migrate tracked).
-- Idempotent, so it can be re-run safely.

CREATE TABLE IF NOT EXISTS "reference_data" (
    "id"                      SERIAL       PRIMARY KEY,

    -- The number as printed: "4.2.1", "Table 3", "Annex II". See the header.
    "reference_no"            TEXT         NOT NULL,
    -- The sub-division under reference_no when the source splits one.
    "sub_reference_no"        TEXT,
    "description"             TEXT,
    -- A cross-reference the source points at from this entry.
    "additional_reference_no" TEXT,

    -- The document that numbered this reference. Mandatory: see the header.
    "source_id"               INTEGER      NOT NULL,
    -- The source's own grouping for the entry, kept in the source's words.
    "category"                TEXT,

    "created_at"              TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at"              TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'reference_data_source_id_fkey') THEN
        ALTER TABLE "reference_data"
            ADD CONSTRAINT "reference_data_source_id_fkey"
            FOREIGN KEY ("source_id") REFERENCES "source"("id")
            ON DELETE CASCADE ON UPDATE CASCADE;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS "reference_data_source_id_idx"
    ON "reference_data" ("source_id");
-- The query this table exists for: "what is reference 4.2 in this document?"
CREATE INDEX IF NOT EXISTS "reference_data_source_id_reference_no_idx"
    ON "reference_data" ("source_id", "reference_no");
CREATE INDEX IF NOT EXISTS "reference_data_category_idx"
    ON "reference_data" ("category");
