-- Cargo family groups: the cleaning-family identity a guide assigns to a cargo.
--
-- The Verwey guide numbers 431 families. 406 of them hold exactly one cargo, so
-- the family and the chemical look like the same thing; the other 25 hold 2-19
-- chemicals under one printed heading ("[10] ALCOHOL ETHOXYLATES" covers the 12
-- NEODOL / ethoxylate grades). The FROM->TO cleaning matrix is keyed on the
-- family number ONLY - it never names a member - so the matrix cannot be stored
-- against cargo_chemical without inventing assertions the book never made.
--
-- The family is a CLEANING classification, not a chemical one: family [152]
-- EXXSOLS spans MARPOL A1, Z, Y and X. Members share residue behaviour and
-- nothing else, so no property ever hangs off this table.
--
-- Source-scoped: Verwey number 10, Drew index 10 and Miracle entry 10 are
-- unrelated, hence UNIQUE(source_id, family_code).
--
-- cleaning_process gains family columns ALONGSIDE from_cargo_id / to_cargo_id;
-- nothing existing is altered or dropped, so the loaded Drew (source 9) and old
-- Verwey (source 8) matrix rows keep working unchanged.

CREATE TABLE IF NOT EXISTS "cargo_family_group" (
    "id"          SERIAL       PRIMARY KEY,
    "source_id"   INTEGER      NOT NULL,
    "family_code" TEXT         NOT NULL,   -- the guide's own number, e.g. "10"
    "family_name" TEXT         NOT NULL,   -- printed heading, or the sole member's name
    "notes"       TEXT,
    "created_at"  TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at"  TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "cargo_family_group_source_id_fkey"
        FOREIGN KEY ("source_id") REFERENCES "source"("id")
        ON DELETE CASCADE ON UPDATE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS "cargo_family_group_source_id_family_code_key"
    ON "cargo_family_group"("source_id", "family_code");
CREATE INDEX IF NOT EXISTS "cargo_family_group_source_id_idx"
    ON "cargo_family_group"("source_id");
CREATE INDEX IF NOT EXISTS "cargo_family_group_family_name_idx"
    ON "cargo_family_group"("family_name");

-- ---------------------------------------------------------------------------
-- cargo_chemical -> its family
-- ---------------------------------------------------------------------------
-- Nullable: only cargoes loaded from a guide that groups them have a family.
-- ON DELETE SET NULL so removing a family never deletes chemical records.
ALTER TABLE "cargo_chemical"
    ADD COLUMN IF NOT EXISTS "cargo_family_group_id" INTEGER;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                 WHERE conname = 'cargo_chemical_cargo_family_group_id_fkey') THEN
    ALTER TABLE "cargo_chemical" ADD CONSTRAINT "cargo_chemical_cargo_family_group_id_fkey"
      FOREIGN KEY ("cargo_family_group_id") REFERENCES "cargo_family_group"("id")
      ON DELETE SET NULL ON UPDATE CASCADE;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS "cargo_chemical_cargo_family_group_id_idx"
    ON "cargo_chemical"("cargo_family_group_id");

-- ---------------------------------------------------------------------------
-- cleaning_process -> family pair (additive; cargo columns untouched)
-- ---------------------------------------------------------------------------
ALTER TABLE "cleaning_process"
    ADD COLUMN IF NOT EXISTS "from_cargo_family_group_id" INTEGER;
ALTER TABLE "cleaning_process"
    ADD COLUMN IF NOT EXISTS "to_cargo_family_group_id" INTEGER;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                 WHERE conname = 'cleaning_process_from_cargo_family_group_id_fkey') THEN
    ALTER TABLE "cleaning_process"
      ADD CONSTRAINT "cleaning_process_from_cargo_family_group_id_fkey"
      FOREIGN KEY ("from_cargo_family_group_id") REFERENCES "cargo_family_group"("id")
      ON DELETE CASCADE ON UPDATE CASCADE;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                 WHERE conname = 'cleaning_process_to_cargo_family_group_id_fkey') THEN
    ALTER TABLE "cleaning_process"
      ADD CONSTRAINT "cleaning_process_to_cargo_family_group_id_fkey"
      FOREIGN KEY ("to_cargo_family_group_id") REFERENCES "cargo_family_group"("id")
      ON DELETE CASCADE ON UPDATE CASCADE;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS "cleaning_process_from_cargo_family_group_id_idx"
    ON "cleaning_process"("from_cargo_family_group_id");
CREATE INDEX IF NOT EXISTS "cleaning_process_to_cargo_family_group_id_idx"
    ON "cleaning_process"("to_cargo_family_group_id");

-- Idempotency key for family-grain matrix rows. Partial, because the existing
-- cargo-grain rows (sources 8 and 9) leave these columns NULL.
CREATE UNIQUE INDEX IF NOT EXISTS "cleaning_process_family_pair_source_key"
    ON "cleaning_process"("source_id", "from_cargo_family_group_id", "to_cargo_family_group_id")
    WHERE "from_cargo_family_group_id" IS NOT NULL
      AND "to_cargo_family_group_id" IS NOT NULL;
