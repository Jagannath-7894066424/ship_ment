-- Adapt the procedure template tables for the Dr. Verwey Tank Cleaning Guide
-- (Table 2 — Cleaning Procedures List). Values in the guide are free text
-- (e.g. "About 2½ hours", "80°C", "Cold", "Teepol 0.05%"), so the structured
-- numeric/JSON step columns become text. No new tables; relationships and FKs
-- are unchanged.

-- 1) procedure_templates: official cleaning procedure code (A, B, C, D, EE, LL...)
ALTER TABLE "procedure_templates" ADD COLUMN "procedure_code" TEXT NOT NULL;
CREATE UNIQUE INDEX "procedure_templates_procedure_code_key" ON "procedure_templates"("procedure_code");

-- 2) procedure_template_steps: store instructions exactly as published.
--    Replace the structured columns with free-text ones and add `medium`.
ALTER TABLE "procedure_template_steps" DROP COLUMN "default_duration_minutes";
ALTER TABLE "procedure_template_steps" DROP COLUMN "default_temperature_c";
ALTER TABLE "procedure_template_steps" DROP COLUMN "default_chemicals";

ALTER TABLE "procedure_template_steps" ADD COLUMN "medium"      TEXT;
ALTER TABLE "procedure_template_steps" ADD COLUMN "temperature" TEXT;
ALTER TABLE "procedure_template_steps" ADD COLUMN "duration"    TEXT;
ALTER TABLE "procedure_template_steps" ADD COLUMN "cleaner"     TEXT;
