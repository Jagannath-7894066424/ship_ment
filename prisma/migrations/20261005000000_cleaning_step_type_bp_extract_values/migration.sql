-- Step types used by the revised BP Tank Cleaning Guide extract
-- ("BP Tank Cleaning Guide - procedure template steps.xlsx"), stored as the
-- sheet types them. PURGE, GAS_FREE and DRY sit beside the older PURGING,
-- GAS_FREEING and DRYING, which other guides' rows still use; nothing is renamed.
--
-- Idempotent: IF NOT EXISTS. Runs outside a transaction -
-- etl/common/apply_migration.py detects ADD VALUE and switches to autocommit.

ALTER TYPE "CleaningStepType" ADD VALUE IF NOT EXISTS 'STRIP';
ALTER TYPE "CleaningStepType" ADD VALUE IF NOT EXISTS 'BLOW';
ALTER TYPE "CleaningStepType" ADD VALUE IF NOT EXISTS 'WASH';
ALTER TYPE "CleaningStepType" ADD VALUE IF NOT EXISTS 'PURGE';
ALTER TYPE "CleaningStepType" ADD VALUE IF NOT EXISTS 'GAS_FREE';
ALTER TYPE "CleaningStepType" ADD VALUE IF NOT EXISTS 'DRY';
ALTER TYPE "CleaningStepType" ADD VALUE IF NOT EXISTS 'DRAIN';
ALTER TYPE "CleaningStepType" ADD VALUE IF NOT EXISTS 'LOAD';
