-- Track which reference each UN number came from.
--
-- cargo_un_number rows were only traceable to a source indirectly, via
-- cargo_chemical.source_id. That breaks as soon as one chemical carries UN
-- numbers from more than one guide (Miracle, LARS and USCG all publish them),
-- so the source is recorded on the row itself.
--
-- Nullable: rows created by the 20260720100000 backfill predate any loader run.
-- The UPDATE below fills them in from the owning chemical, which is correct for
-- the current data (every existing row came in through a source-scoped
-- cargo_chemical), but new rows should be written with an explicit source_id.

ALTER TABLE "cargo_un_number" ADD COLUMN IF NOT EXISTS "source_id" INTEGER;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='cargo_un_number_source_id_fkey') THEN
    ALTER TABLE "cargo_un_number" ADD CONSTRAINT "cargo_un_number_source_id_fkey"
      FOREIGN KEY ("source_id") REFERENCES "source"("id")
      ON DELETE CASCADE ON UPDATE CASCADE;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS "cargo_un_number_source_id_idx" ON "cargo_un_number"("source_id");

-- Backfill from the owning chemical (see note above).
UPDATE "cargo_un_number" u
   SET "source_id" = c."source_id"
  FROM "cargo_chemical" c
 WHERE c."id" = u."cargo_id"
   AND u."source_id" IS NULL
   AND c."source_id" IS NOT NULL;
