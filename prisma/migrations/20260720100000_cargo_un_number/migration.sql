-- Support multiple UN numbers per chemical.
--
-- A chemical can have several UN numbers, each optionally qualified (by
-- concentration or physical state), so the single cargo_chemical.un_number
-- column is replaced by a dedicated cargo_un_number table.

-- 1) New table --------------------------------------------------------------
CREATE TABLE "cargo_un_number" (
    "id"              SERIAL       PRIMARY KEY,
    "cargo_id"        INTEGER      NOT NULL,
    "un_number"       TEXT         NOT NULL,
    "qualifier_type"  TEXT,
    "qualifier_value" TEXT,
    "remarks"         TEXT,
    "created_at"      TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at"      TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "cargo_un_number_cargo_id_fkey"
        FOREIGN KEY ("cargo_id") REFERENCES "cargo_chemical"("id")
        ON DELETE CASCADE ON UPDATE CASCADE
);
CREATE INDEX "cargo_un_number_cargo_id_idx"  ON "cargo_un_number"("cargo_id");
CREATE INDEX "cargo_un_number_un_number_idx" ON "cargo_un_number"("un_number");

-- 2) Backfill existing single UN numbers ------------------------------------
-- Current data holds only plain single numbers (no qualifiers), so extract the
-- first 3-4 digit UN number. Junk values (formulas, "MIXTURE", "Y") have no
-- such run and are skipped. Complex multi-UN cells are re-parsed by the loader.
INSERT INTO "cargo_un_number" ("cargo_id", "un_number", "created_at", "updated_at")
SELECT "id", substring("un_number" from '\d{3,4}'), now(), now()
  FROM "cargo_chemical"
 WHERE "un_number" ~ '\d{3,4}';

-- 3) Drop the old column (its index is dropped with it) ---------------------
ALTER TABLE "cargo_chemical" DROP COLUMN "un_number";

-- 4) Remove the now-unused field_definitions catalog row --------------------
DELETE FROM "field_definitions" WHERE "field_name" = 'un_number';
