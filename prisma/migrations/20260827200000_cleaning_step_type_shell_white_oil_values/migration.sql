-- Add the six step kinds the Shell White Oil Tank Cleaning Guide names.
--
-- The guide's step sheet types its 40 rows with a vocabulary of ten, four of
-- which CleaningStepType already had (CLEANING, DRAINING, DRYING, GAS_FREEING).
-- The other six had no value:
--
--     PREPARATION       getting the tank ready before any wash begins
--     ASSESSMENT        judging how much cleaning this transition needs
--     INSPECTION        checking the result of a wash
--     SPECIAL_CLEANING  the extra treatment black-oil transitions call for
--     TEMPERATURE       a heating requirement stated as a step of its own
--                       ("Pour point + 15 °C minimum")
--     NITROGEN          carriage and handling under dry nitrogen (lub. oil)
--
-- Mapping them onto existing values was the alternative considered - ASSESSMENT
-- onto DECISION, NITROGEN onto PURGING, TEMPERATURE onto PRECONDITION. Storing
-- the source's own vocabulary was chosen instead, for the same reason as
-- 20260826000000_cleaning_step_type_bp_values: a step should read back as the
-- guide types it, not as a translation this project applied.
--
-- INSPECTION and ASSESSMENT are distinct on purpose: one looks at the tank
-- after work, the other decides what work is needed before it.
--
-- Idempotent: IF NOT EXISTS. Must run OUTSIDE a transaction (PostgreSQL 12
-- cannot use an enum value added in the still-open transaction) -
-- etl/common/apply_migration.py detects ADD VALUE and switches to autocommit.

ALTER TYPE "CleaningStepType" ADD VALUE IF NOT EXISTS 'PREPARATION';
ALTER TYPE "CleaningStepType" ADD VALUE IF NOT EXISTS 'ASSESSMENT';
ALTER TYPE "CleaningStepType" ADD VALUE IF NOT EXISTS 'INSPECTION';
ALTER TYPE "CleaningStepType" ADD VALUE IF NOT EXISTS 'SPECIAL_CLEANING';
ALTER TYPE "CleaningStepType" ADD VALUE IF NOT EXISTS 'TEMPERATURE';
ALTER TYPE "CleaningStepType" ADD VALUE IF NOT EXISTS 'NITROGEN';
