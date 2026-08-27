-- Record which source first introduced each synonym text.
--
-- synonyms is a shared vocabulary: loaders dedupe on normalized_text, so one
-- row is reused by every source that spells a chemical the same way (853 texts
-- are already linked from more than one source). The per-link source therefore
-- stays where it is, on cargo_synonym.source_id / crude_oil_synonym.source_id -
-- this column answers a different question: who put the text in the vocabulary.
--
-- First writer wins. A loader sets source_id only on the INSERT that creates
-- the row; a later source that reuses the text never re-stamps it, otherwise
-- the column would just track whichever loader ran last.
--
-- Nullable, matching the link tables: master_loader runs with source_id = -1
-- when a source cannot be resolved, and writes NULL rather than a dangling id.

ALTER TABLE "synonyms" ADD COLUMN IF NOT EXISTS "source_id" INTEGER;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='synonyms_source_id_fkey') THEN
    ALTER TABLE "synonyms" ADD CONSTRAINT "synonyms_source_id_fkey"
      FOREIGN KEY ("source_id") REFERENCES "source"("id")
      ON DELETE SET NULL ON UPDATE CASCADE;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS "synonyms_source_id_idx" ON "synonyms"("source_id");

-- Backfill: the earliest link row that names this synonym is the closest record
-- we have of which loader created it. Ordered by created_at then id so the two
-- link tables (independent id sequences) are compared on a shared clock.
UPDATE "synonyms" s
   SET "source_id" = l."source_id"
  FROM (
        SELECT DISTINCT ON (synonym_id) synonym_id, source_id
          FROM (
                SELECT synonym_id, source_id, created_at, id FROM "cargo_synonym"
                 WHERE source_id IS NOT NULL
                UNION ALL
                SELECT synonym_id, source_id, created_at, id FROM "crude_oil_synonym"
                 WHERE source_id IS NOT NULL
               ) links
         ORDER BY synonym_id, created_at, id
       ) l
 WHERE l."synonym_id" = s."id"
   AND s."source_id" IS NULL;
