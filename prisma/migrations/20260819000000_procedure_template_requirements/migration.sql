-- Lossless, structured storage for source-defined cleaning procedures.
--
-- A procedure has four parts and they are NOT interchangeable:
--
--   procedure_templates             what the code means        (WD, CW, NC)
--     |- procedure_template_steps        ordered actions       (1..n, sequenced)
--     |- procedure_template_requirement  rules and limits      (unsequenced)
--     '- procedure_template_instruction  source-level notes    (unsequenced)
--
-- cleaning_process points at procedure_templates and is NOT touched here. It
-- must never restate the steps; it names the procedure and nothing more.
--
-- Applied by hand via psycopg2 (this database is not prisma-migrate tracked).
-- Idempotent, so it can be re-run safely.
--
-- NOTE ON TRANSACTIONS: section 1 uses ALTER TYPE ... ADD VALUE. On PostgreSQL
-- 12 a value added inside a transaction cannot be USED until that transaction
-- commits, so this file is applied with autocommit on (see the runbook in
-- docs/SHELL_PROCEDURE_IMPORT.md). Every statement is individually idempotent,
-- so a re-run after a partial failure is safe.

-- ---------------------------------------------------------------------------
-- 1. CleaningStepType: five new kinds of step
-- ---------------------------------------------------------------------------
-- Existing values are kept exactly as they are - removing or reordering one
-- would silently change the meaning of the 1,437 Verwey/Drew step rows already
-- stored. PRECONDITION goes first because it describes a state the tank must
-- already be in rather than an action performed; the rest are appended.
ALTER TYPE "CleaningStepType" ADD VALUE IF NOT EXISTS 'PRECONDITION' BEFORE 'PRECLEANING';
ALTER TYPE "CleaningStepType" ADD VALUE IF NOT EXISTS 'VENTILATING'  AFTER  'DRYING';
ALTER TYPE "CleaningStepType" ADD VALUE IF NOT EXISTS 'PURGING'      AFTER  'VENTILATING';
ALTER TYPE "CleaningStepType" ADD VALUE IF NOT EXISTS 'GAS_FREEING'  AFTER  'PURGING';
ALTER TYPE "CleaningStepType" ADD VALUE IF NOT EXISTS 'MOPPING'      AFTER  'GAS_FREEING';

-- ---------------------------------------------------------------------------
-- 2. procedure_templates: keep the source's own wording
-- ---------------------------------------------------------------------------
-- Structured steps and requirements are a normalisation of the source text, and
-- normalisation loses nuance. Keeping the original sentence next to the derived
-- rows is what makes the loss auditable and lets rows be re-derived later.
ALTER TABLE "procedure_templates"
    ADD COLUMN IF NOT EXISTS "source_definition" TEXT;

-- False only for decision codes that forbid the transition (NC). Defaulting to
-- true backfills every existing Verwey/Drew procedure correctly - they are all
-- real cleaning procedures.
ALTER TABLE "procedure_templates"
    ADD COLUMN IF NOT EXISTS "loading_allowed" BOOLEAN DEFAULT true;

-- ---------------------------------------------------------------------------
-- 3. procedure_template_steps: step_type + slot uniqueness
-- ---------------------------------------------------------------------------
-- step_type lets a reader act on a step without parsing step_name. Nullable:
-- the rows already loaded have no classification and are not being guessed at.
ALTER TABLE "procedure_template_steps"
    ADD COLUMN IF NOT EXISTS "step_type" "CleaningStepType";

-- Idempotency key for the importer. Verified free of duplicates before adding:
-- 1,437 existing rows, 0 colliding (procedure_templates_id, step_order) pairs.
-- Without this a second import appends a second copy of every procedure.
CREATE UNIQUE INDEX IF NOT EXISTS "procedure_template_steps_procedure_templates_id_step_order_key"
    ON "procedure_template_steps"("procedure_templates_id", "step_order");

CREATE INDEX IF NOT EXISTS "procedure_template_steps_procedure_templates_id_idx"
    ON "procedure_template_steps"("procedure_templates_id");
CREATE INDEX IF NOT EXISTS "procedure_template_steps_step_type_idx"
    ON "procedure_template_steps"("step_type");

-- ---------------------------------------------------------------------------
-- 4. procedure_template_requirement
-- ---------------------------------------------------------------------------
-- Rules and limits that constrain a procedure but hold no position in the step
-- sequence. Three things live here that a step cannot express:
--
--   measured limits   ROB_LIMIT <= 0.05 %
--   preconditions     PRECONDITION = WD
--   disjunctions      ATMOSPHERE_TREATMENT = VENTILATE_OR_PURGE
--
-- The disjunction is the reason this table exists. The Shell source says
-- "ventilate OR purge"; recording that as ventilation_required = true AND
-- inert_gas_required = true would assert both are mandatory, which the source
-- does not say. One row keeps the OR intact.
--
-- requirement_type is TEXT, not an enum, on purpose: every new source document
-- brings its own vocabulary and a migration per new rule type would make adding
-- a source harder than it needs to be.
CREATE TABLE IF NOT EXISTS "procedure_template_requirement" (
    "id"                    SERIAL       PRIMARY KEY,
    "procedure_template_id" INTEGER      NOT NULL,
    "requirement_type"      TEXT         NOT NULL,
    "requirement_value"     TEXT,
    "operator"              TEXT,
    "unit"                  TEXT,
    "mandatory"             BOOLEAN      NOT NULL DEFAULT true,
    "description"           TEXT,
    "display_order"         INTEGER      NOT NULL,
    "created_at"            TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at"            TIMESTAMP(3) NOT NULL
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'procedure_template_requirement_procedure_template_id_fkey') THEN
        ALTER TABLE "procedure_template_requirement"
            ADD CONSTRAINT "procedure_template_requirement_procedure_template_id_fkey"
            FOREIGN KEY ("procedure_template_id") REFERENCES "procedure_templates"("id")
            ON DELETE CASCADE ON UPDATE CASCADE;
    END IF;
END $$;

-- Slot-based idempotency, matching procedure_template_instruction. NOT keyed on
-- requirement_type: a source may legitimately state two requirements of the
-- same type (two preconditions, say), and that must stay representable.
-- Named explicitly (and short): the default name would exceed PostgreSQL's
-- 63-character identifier limit, and Prisma and PostgreSQL truncate it
-- differently, leaving schema and database disagreeing forever. The schema
-- pins the same name with `map:`.
CREATE UNIQUE INDEX IF NOT EXISTS "procedure_template_requirement_display_order_key"
    ON "procedure_template_requirement"("procedure_template_id", "display_order");

CREATE INDEX IF NOT EXISTS "procedure_template_requirement_procedure_template_id_idx"
    ON "procedure_template_requirement"("procedure_template_id");
CREATE INDEX IF NOT EXISTS "procedure_template_requirement_requirement_type_idx"
    ON "procedure_template_requirement"("requirement_type");
