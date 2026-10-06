-- reactive_groups: add group 0 "Unassigned Cargoes".
--
-- WHY
-- ---
-- cargo_chemicals_groupData.csv and the Appendix I exceptions file both use group
-- code 0 for 31 cargoes (Oleum, Acetone cyanohydrin, Sodium chlorate solution, ...),
-- but the compatibility chart has no row/column 0, so reactive_groups never got
-- one. master_cargo_chemical_group_details.group_code is an FK to
-- reactive_groups.group_code, so the loader dropped all 31 rows as orphans, those
-- cargoes were never linked in cargo_reactive_group, and exception rows stored
-- group_a_id NULL.
--
-- Group 0 has no compatibility-chart entries on purpose: an unassigned cargo is
-- decided case by case, so the lookup never treats two group-0 cargoes as
-- "same group => compatible" and answers only from exceptions.
--
-- APPLY
-- -----
-- Run the two statements separately (autocommit): a new enum label cannot be
-- used in the transaction that adds it.

ALTER TYPE "ReactiveGroupType" ADD VALUE IF NOT EXISTS 'UNASSIGNED';

INSERT INTO reactive_groups (system, group_code, group_name, group_type, source_id, created_at, updated_at)
SELECT system, '0', 'Unassigned Cargoes', 'UNASSIGNED', source_id, now(), now()
FROM reactive_groups WHERE group_code = '1'
ON CONFLICT (group_code) DO NOTHING;
