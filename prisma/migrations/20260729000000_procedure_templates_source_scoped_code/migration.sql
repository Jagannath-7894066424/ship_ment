-- Source-scope the procedure code so multiple cleaning guides can each define
-- codes A..Y. Replaces the global unique on procedure_code with (source_id, code).
ALTER TABLE "procedure_templates" DROP CONSTRAINT IF EXISTS "procedure_templates_procedure_code_key";
DROP INDEX IF EXISTS "procedure_templates_procedure_code_key";
CREATE UNIQUE INDEX IF NOT EXISTS "procedure_templates_source_id_procedure_code_key"
    ON "procedure_templates"("source_id", "procedure_code");
