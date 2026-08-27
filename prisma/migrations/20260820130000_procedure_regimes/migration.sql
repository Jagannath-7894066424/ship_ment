-- Support reusable procedures, conditional steps and shared notes.
--
-- Driven by the Shell white-oil cleaning regimes (30 regime codes, CSV extract
-- of "Shell Tank Cleaning Guide 2016.pdf"). Three things that source needs and
-- the schema could not express:
--
--   1. A step that REFERENCES another procedure instead of restating it.
--      "Perform WD" appears in 25 of the 30 regimes; copying WD's steps into
--      each would be 25 copies of one procedure, and a correction to WD would
--      have to be made 25 times.
--
--   2. A step that only applies under a stated CONDITION. The source's second
--      instruction column is not a further step - it is an exception, an
--      additional requirement or a compatibility decision. Flattening it into
--      the main sequence would assert unconditionally what the source states
--      conditionally.
--
--   3. Explanatory notes that belong to MANY procedures (what ROB means, when
--      purging applies, washing on non-inerted vessels). Copying the text onto
--      every procedure would make correcting it a hunt.
--
-- Nothing here changes an existing column or row. The new columns are nullable
-- and the new tables start empty, so every loader that does not know about them
-- keeps working unchanged.

-- ---------------------------------------------------------------------------
-- 1. Step role values.
-- ---------------------------------------------------------------------------
-- These describe a step's ROLE (is it the main line, a reference, a condition,
-- a decision, a warning) rather than the physical operation (CLEANING,
-- DRAINING, ...) the existing values describe. They share one column by
-- instruction: a step records either its role or its operation, not both.
--
-- ALTER TYPE ... ADD VALUE cannot run inside a transaction block that later
-- uses the value; etl/common/apply_migration.py detects these and runs the file
-- with autocommit. Each statement is individually idempotent.
ALTER TYPE "CleaningStepType" ADD VALUE IF NOT EXISTS 'MAIN';
ALTER TYPE "CleaningStepType" ADD VALUE IF NOT EXISTS 'REFERENCE';
ALTER TYPE "CleaningStepType" ADD VALUE IF NOT EXISTS 'CONDITIONAL';
ALTER TYPE "CleaningStepType" ADD VALUE IF NOT EXISTS 'DECISION';
ALTER TYPE "CleaningStepType" ADD VALUE IF NOT EXISTS 'WARNING';

-- ---------------------------------------------------------------------------
-- 2. Conditional and referencing steps.
-- ---------------------------------------------------------------------------
-- `condition` holds the source's own wording of when the step applies, kept
-- verbatim. NULL means unconditional - the overwhelming majority of steps.
ALTER TABLE procedure_template_steps
    ADD COLUMN IF NOT EXISTS condition text;

-- `reference_template_id` points at the procedure this step defers to. SET NULL
-- on delete: losing the referenced procedure must not delete the step that
-- mentions it, or a regime would silently lose a line.
ALTER TABLE procedure_template_steps
    ADD COLUMN IF NOT EXISTS reference_template_id integer;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'procedure_template_steps_reference_template_id_fkey'
    ) THEN
        ALTER TABLE procedure_template_steps
            ADD CONSTRAINT procedure_template_steps_reference_template_id_fkey
            FOREIGN KEY (reference_template_id) REFERENCES procedure_templates(id)
            ON DELETE SET NULL ON UPDATE CASCADE;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS procedure_template_steps_reference_template_id_idx
    ON procedure_template_steps (reference_template_id);

-- ---------------------------------------------------------------------------
-- 3. Reusable notes.
-- ---------------------------------------------------------------------------
-- One row per explanatory note, linked to the procedures it applies to. Keyed
-- on note_code so a re-import updates the wording in one place.
CREATE TABLE IF NOT EXISTS procedure_note (
    id         serial PRIMARY KEY,
    note_code  text NOT NULL,
    title      text NOT NULL,
    body       text NOT NULL,
    source_id  integer REFERENCES source(id) ON DELETE SET NULL ON UPDATE CASCADE,
    created_at timestamp NOT NULL DEFAULT now(),
    updated_at timestamp NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS procedure_note_note_code_key
    ON procedure_note (note_code);

CREATE TABLE IF NOT EXISTS procedure_template_note (
    id                     serial PRIMARY KEY,
    procedure_templates_id integer NOT NULL
        REFERENCES procedure_templates(id) ON DELETE CASCADE ON UPDATE CASCADE,
    procedure_note_id      integer NOT NULL
        REFERENCES procedure_note(id) ON DELETE CASCADE ON UPDATE CASCADE,
    created_at             timestamp NOT NULL DEFAULT now()
);

-- The link carries no payload, so the pair is the identity: linking the same
-- note to the same procedure twice is the same fact, not a second one.
CREATE UNIQUE INDEX IF NOT EXISTS procedure_template_note_template_note_key
    ON procedure_template_note (procedure_templates_id, procedure_note_id);

CREATE INDEX IF NOT EXISTS procedure_template_note_note_idx
    ON procedure_template_note (procedure_note_id);
