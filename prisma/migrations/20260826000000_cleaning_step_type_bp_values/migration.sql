-- Add the two step kinds the BP Tank Cleaning Guide names.
--
-- BP's step sheet types its rows RESTRICTION ("Product NOT to be loaded unless
-- specific instructions have been issued by BP Head Office") and CONDITION
-- ("If ROB is less than 0.1% ... cargo may be loaded directly on top without
-- washing"). Neither was a CleaningStepType value.
--
-- DECISION and CONDITIONAL already carry those two meanings, and mapping onto
-- them was the alternative considered. Storing the source's own vocabulary was
-- chosen instead, so a BP step reads back as the guide types it rather than as
-- a translation this project applied. The overlap is therefore deliberate:
--
--     DECISION     a load/do-not-load determination        (Shell's NC)
--     RESTRICTION  the same, as the BP guide names it
--     CONDITIONAL  applies only when `condition` holds     (Shell)
--     CONDITION    the same, as the BP guide names it
--
-- Anything reading step_type must treat DECISION and RESTRICTION alike, and
-- CONDITIONAL and CONDITION alike. Prefer `loading_allowed` on the template for
-- "may this be loaded", which is unambiguous across all sources.
--
-- Idempotent: IF NOT EXISTS. Must run OUTSIDE a transaction (PostgreSQL 12
-- cannot use an enum value added in the still-open transaction) -
-- etl/common/apply_migration.py detects ADD VALUE and switches to autocommit.

ALTER TYPE "CleaningStepType" ADD VALUE IF NOT EXISTS 'RESTRICTION';
ALTER TYPE "CleaningStepType" ADD VALUE IF NOT EXISTS 'CONDITION';
