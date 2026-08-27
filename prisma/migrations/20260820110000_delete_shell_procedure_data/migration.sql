-- Remove the Shell tank-cleaning PROCEDURE data.
--
-- Scope: procedure_templates belonging to "Shell Tank Cleaning Guide 2016.pdf"
-- and their child rows. Deleting the parents is enough - steps, requirements
-- and instructions are all ON DELETE CASCADE:
--
--     procedure_templates              11
--       |- procedure_template_steps         37   CASCADE
--       |- procedure_template_requirement   25   CASCADE
--       '- procedure_template_instruction    4   CASCADE
--
-- NOT touched:
--
--   * The source row itself. It still owns the Shell Cargo Master data - 29
--     crude_oil, 170 property values, 126 synonym links - which is not
--     procedure data and is not in scope here.
--   * Every other source's procedures: Dr Verwey (37 + 279) and Drew Ameroid
--     (22) are matched by source_id, so they cannot be caught by this.
--   * cleaning_process. It references procedure_template_id ON DELETE SET NULL
--     and currently holds ZERO rows pointing at a Shell template, so nothing is
--     orphaned. Should that change, the FK nulls the reference rather than
--     deleting the process - a cleaning_process is not Shell's to remove.
--
-- REVERSIBLE. The procedures live in etl/data/inputs/Shell Tank Cleaning
-- Procedure.xlsx, not only in the database. To restore:
--
--     python3 etl/oil/shell_procedure_templates.py
--
-- Idempotent: re-running deletes nothing once the rows are gone.

DO $$
DECLARE
    src_id  integer;
    removed integer;
BEGIN
    SELECT id INTO src_id FROM source WHERE name = 'Shell Tank Cleaning Guide 2016.pdf';

    IF src_id IS NULL THEN
        RAISE NOTICE 'source "Shell Tank Cleaning Guide 2016.pdf" not registered - nothing to delete';
        RETURN;
    END IF;

    -- Report the children before the cascade takes them, so the log says what
    -- actually went rather than only naming the parents.
    SELECT count(*) INTO removed FROM procedure_template_steps
     WHERE procedure_templates_id IN (SELECT id FROM procedure_templates WHERE source_id = src_id);
    RAISE NOTICE 'procedure_template_steps       : % (cascade)', removed;

    SELECT count(*) INTO removed FROM procedure_template_requirement
     WHERE procedure_template_id IN (SELECT id FROM procedure_templates WHERE source_id = src_id);
    RAISE NOTICE 'procedure_template_requirement : % (cascade)', removed;

    SELECT count(*) INTO removed FROM procedure_template_instruction
     WHERE procedure_templates_id IN (SELECT id FROM procedure_templates WHERE source_id = src_id);
    RAISE NOTICE 'procedure_template_instruction : % (cascade)', removed;

    DELETE FROM procedure_templates WHERE source_id = src_id;
    GET DIAGNOSTICS removed = ROW_COUNT;
    RAISE NOTICE 'procedure_templates            : % deleted (source %)', removed, src_id;
END $$;
