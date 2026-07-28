-- Let cleaning_process store Dr. Verwey cleaning procedures (templates) and the
-- FROM -> TO cleaning matrix, alongside the existing cargo-specific procedures.
-- No new tables. All changes are additive / nullability-relaxing.

-- 1) Relax columns that only apply to cargo-specific rows.
ALTER TABLE "cleaning_process" ALTER COLUMN "cargo_id"       DROP NOT NULL;
ALTER TABLE "cleaning_process" ALTER COLUMN "cleaning_stage" DROP NOT NULL;
ALTER TABLE "cleaning_process" ALTER COLUMN "method_number"  DROP NOT NULL;

-- 2) New columns for Verwey template + matrix rows.
ALTER TABLE "cleaning_process" ADD COLUMN "from_cargo_id"  INTEGER;
ALTER TABLE "cleaning_process" ADD COLUMN "to_cargo_id"    INTEGER;
ALTER TABLE "cleaning_process" ADD COLUMN "procedure_code" TEXT;

ALTER TABLE "cleaning_process" ADD CONSTRAINT "cleaning_process_from_cargo_id_fkey"
    FOREIGN KEY ("from_cargo_id") REFERENCES "cargo_chemical"("id") ON DELETE CASCADE ON UPDATE CASCADE;
ALTER TABLE "cleaning_process" ADD CONSTRAINT "cleaning_process_to_cargo_id_fkey"
    FOREIGN KEY ("to_cargo_id") REFERENCES "cargo_chemical"("id") ON DELETE CASCADE ON UPDATE CASCADE;

CREATE INDEX "cleaning_process_from_cargo_id_idx"  ON "cleaning_process"("from_cargo_id");
CREATE INDEX "cleaning_process_to_cargo_id_idx"    ON "cleaning_process"("to_cargo_id");
CREATE INDEX "cleaning_process_procedure_code_idx" ON "cleaning_process"("procedure_code");

-- 3) Idempotency: one template per (procedure_code, source) and one matrix row
--    per (from_cargo, to_cargo, source). Partial UNIQUE indexes (Prisma can't
--    express these) — also serve as ON CONFLICT targets.
CREATE UNIQUE INDEX "cleaning_process_template_key"
    ON "cleaning_process"("procedure_code", "source_id")
    WHERE "cargo_id" IS NULL AND "from_cargo_id" IS NULL AND "to_cargo_id" IS NULL;

CREATE UNIQUE INDEX "cleaning_process_pair_key"
    ON "cleaning_process"("from_cargo_id", "to_cargo_id", "source_id")
    WHERE "from_cargo_id" IS NOT NULL AND "to_cargo_id" IS NOT NULL;

-- 4) Step: mark every listed instruction mandatory.
ALTER TABLE "cleaning_process_step" ADD COLUMN "mandatory" BOOLEAN;
