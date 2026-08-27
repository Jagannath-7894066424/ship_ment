-- Drop eight never-populated columns from procedure_templates.
--
-- All eight are NULL in every one of the 338 rows, and always have been: no
-- loader in this repository writes them and no service reads them. They are
-- leftovers from the original schema sketch, describing a procedure as a set of
-- flags and defaults before the design settled on ordered `procedure_template_steps`
-- plus `procedure_template_instruction`, which is where this information actually
-- lives.
--
--     target_purpose            never written
--     default_temperature_c     never written
--     default_duration_hours    never written
--     default_chemicals         never written
--     ventilation_required      never written
--     gas_free_required         never written
--     wall_wash_required        never written
--     inert_gas_required        never written
--
-- NOT dropped, though currently also all-NULL:
--
--   * water_type, source_definition and procedure_template_steps.step_type.
--     These were populated - by the Shell source, which was removed on
--     2026-08-20. They are read by src/procedure.ts and describe how a
--     procedure is meant to be modelled, so they stay for the next source that
--     fills them.
--   * source_page_ref. All-NULL here, but written by other loaders and part of
--     the shared source-provenance pattern.
--
-- A note on `default_temperature_c`: etl/chemical/verwey_pdf_book_procedures.py
-- names it in an INSERT, but that INSERT targets procedure_template_steps, which
-- has no such column - the statement is already broken and is not a writer of
-- this one. Dropping this column does not change that loader's behaviour.
--
-- Idempotent: IF EXISTS, so a re-run is a no-op.

ALTER TABLE procedure_templates
    DROP COLUMN IF EXISTS target_purpose,
    DROP COLUMN IF EXISTS default_temperature_c,
    DROP COLUMN IF EXISTS default_duration_hours,
    DROP COLUMN IF EXISTS default_chemicals,
    DROP COLUMN IF EXISTS ventilation_required,
    DROP COLUMN IF EXISTS gas_free_required,
    DROP COLUMN IF EXISTS wall_wash_required,
    DROP COLUMN IF EXISTS inert_gas_required;
