-- Let family-grain matrix rows past the "template row" uniqueness rule.
--
-- cleaning_process_template_key enforces one row per (procedure_code, source_id)
-- for TEMPLATE rows - the rows that carry a procedure but no cargo pair. Its
-- predicate identified those as "all three cargo columns NULL", which was
-- correct until cleaning_process gained the family columns in
-- 20260805000000_cargo_family_group.
--
-- A family-grain matrix row also leaves cargo_id / from_cargo_id / to_cargo_id
-- NULL, so all 184,468 Verwey PDF Book cells matched the predicate and
-- collided on the ~200 distinct procedure codes.
--
-- A template row is one with no cargo pair AND no family pair. The family pair
-- has its own key (cleaning_process_family_pair_source_key).
--
-- Sources 8 and 9 leave the family columns NULL, so their template rows stay
-- covered exactly as before.

DROP INDEX IF EXISTS "cleaning_process_template_key";

CREATE UNIQUE INDEX IF NOT EXISTS "cleaning_process_template_key"
    ON "cleaning_process"("procedure_code", "source_id")
    WHERE "cargo_id" IS NULL
      AND "from_cargo_id" IS NULL
      AND "to_cargo_id" IS NULL
      AND "from_cargo_family_group_id" IS NULL
      AND "to_cargo_family_group_id" IS NULL;
