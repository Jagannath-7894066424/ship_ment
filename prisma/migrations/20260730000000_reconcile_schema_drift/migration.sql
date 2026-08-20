-- Reconcile the DB back to schema.prisma after a `prisma db push` (pulled in via
-- a git merge of an older schema) reverted these tables. All additive / defensive.
ALTER TABLE "procedure_templates" ADD COLUMN IF NOT EXISTS "procedure_code" TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS "procedure_templates_source_id_procedure_code_key"
    ON "procedure_templates"("source_id","procedure_code");

ALTER TABLE "procedure_template_steps" ADD COLUMN IF NOT EXISTS "medium" TEXT;
ALTER TABLE "procedure_template_steps" ADD COLUMN IF NOT EXISTS "temperature" TEXT;
ALTER TABLE "procedure_template_steps" ADD COLUMN IF NOT EXISTS "duration" TEXT;
ALTER TABLE "procedure_template_steps" ADD COLUMN IF NOT EXISTS "cleaner" TEXT;

ALTER TABLE "cleaning_process" ALTER COLUMN "cargo_id" DROP NOT NULL;
ALTER TABLE "cleaning_process" ALTER COLUMN "cleaning_stage" DROP NOT NULL;
ALTER TABLE "cleaning_process" ALTER COLUMN "method_number" DROP NOT NULL;
ALTER TABLE "cleaning_process" ADD COLUMN IF NOT EXISTS "from_cargo_id" INTEGER;
ALTER TABLE "cleaning_process" ADD COLUMN IF NOT EXISTS "to_cargo_id" INTEGER;
ALTER TABLE "cleaning_process" ADD COLUMN IF NOT EXISTS "procedure_code" TEXT;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='cleaning_process_from_cargo_id_fkey') THEN
    ALTER TABLE "cleaning_process" ADD CONSTRAINT "cleaning_process_from_cargo_id_fkey"
      FOREIGN KEY ("from_cargo_id") REFERENCES "cargo_chemical"("id") ON DELETE CASCADE ON UPDATE CASCADE;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='cleaning_process_to_cargo_id_fkey') THEN
    ALTER TABLE "cleaning_process" ADD CONSTRAINT "cleaning_process_to_cargo_id_fkey"
      FOREIGN KEY ("to_cargo_id") REFERENCES "cargo_chemical"("id") ON DELETE CASCADE ON UPDATE CASCADE;
  END IF;
END $$;
CREATE INDEX IF NOT EXISTS "cleaning_process_from_cargo_id_idx"  ON "cleaning_process"("from_cargo_id");
CREATE INDEX IF NOT EXISTS "cleaning_process_to_cargo_id_idx"    ON "cleaning_process"("to_cargo_id");
CREATE INDEX IF NOT EXISTS "cleaning_process_procedure_code_idx" ON "cleaning_process"("procedure_code");
CREATE UNIQUE INDEX IF NOT EXISTS "cleaning_process_template_key"
    ON "cleaning_process"("procedure_code","source_id")
    WHERE "cargo_id" IS NULL AND "from_cargo_id" IS NULL AND "to_cargo_id" IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS "cleaning_process_pair_key"
    ON "cleaning_process"("from_cargo_id","to_cargo_id","source_id")
    WHERE "from_cargo_id" IS NOT NULL AND "to_cargo_id" IS NOT NULL;

ALTER TABLE "cleaning_process_step" ADD COLUMN IF NOT EXISTS "mandatory" BOOLEAN;
ALTER TABLE "cargo_hazard_data" ADD COLUMN IF NOT EXISTS "dot_symbol" TEXT;
