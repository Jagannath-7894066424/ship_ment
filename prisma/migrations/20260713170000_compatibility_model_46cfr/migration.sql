-- Compatibility data model for 46 CFR Part 150.
--
--   1. reactive_groups.group_type  — REACTIVE (codes 1–22) / CARGO (codes 30–43)
--   2. compatibility.compatible    — String -> Boolean (X => false, blank => true)
--                                    symmetric pairs deduped to canonical order
--                                    group_a_id <= group_b_id, UNIQUE(group_a_id, group_b_id)
--   3. compatibility_overrides     -> compatibility_exception (Appendix I exceptions)

-- 1) Enums -------------------------------------------------------------------
CREATE TYPE "ReactiveGroupType" AS ENUM ('REACTIVE', 'CARGO');
CREATE TYPE "CompatibilityExceptionType" AS ENUM ('MORE_COMPATIBLE', 'LESS_COMPATIBLE');

-- 2) reactive_groups.group_type ---------------------------------------------
ALTER TABLE "reactive_groups" ADD COLUMN "group_type" "ReactiveGroupType";
UPDATE "reactive_groups"
   SET "group_type" = CASE
     WHEN "group_code" ~ '^[0-9]+$' AND "group_code"::int BETWEEN 1 AND 22
       THEN 'REACTIVE'::"ReactiveGroupType"
     ELSE 'CARGO'::"ReactiveGroupType"
   END;
ALTER TABLE "reactive_groups" ALTER COLUMN "group_type" SET NOT NULL;

-- 3) compatibility: canonical symmetric pairs + Boolean ---------------------
-- a) drop self-pairs, then normalise to canonical order (group_a_id <= group_b_id).
DELETE FROM "compatibility" WHERE "group_a_id" = "group_b_id";
UPDATE "compatibility"
   SET "group_a_id" = LEAST("group_a_id", "group_b_id"),
       "group_b_id" = GREATEST("group_a_id", "group_b_id");

-- b) collapse mirror duplicates: keep the incompatible ('NO') row if the pair
--    disagrees, otherwise keep the lowest id.
DELETE FROM "compatibility" a
 USING "compatibility" b
 WHERE a."group_a_id" = b."group_a_id"
   AND a."group_b_id" = b."group_b_id"
   AND a."id" <> b."id"
   AND (
        (b."compatible" = 'NO' AND a."compatible" <> 'NO')
     OR (a."compatible" = b."compatible" AND a."id" > b."id")
   );

-- c) String -> Boolean (X / NO / false => false; anything else => true).
ALTER TABLE "compatibility" ADD COLUMN "compatible_bool" BOOLEAN;
UPDATE "compatibility"
   SET "compatible_bool" = CASE
     WHEN upper(btrim("compatible")) IN ('NO', 'X', 'FALSE', 'F', '0') THEN false
     ELSE true
   END;
ALTER TABLE "compatibility" DROP COLUMN "compatible";
ALTER TABLE "compatibility" RENAME COLUMN "compatible_bool" TO "compatible";
ALTER TABLE "compatibility" ALTER COLUMN "compatible" SET NOT NULL;

CREATE UNIQUE INDEX "compatibility_group_a_id_group_b_id_key"
    ON "compatibility" ("group_a_id", "group_b_id");

-- 4) compatibility_overrides -> compatibility_exception ----------------------
-- (Appendix I of 46 CFR Part 150; table currently holds no rows.)
ALTER TABLE "compatibility_overrides" RENAME TO "compatibility_exception";

ALTER TABLE "compatibility_exception" DROP COLUMN "severity";
ALTER TABLE "compatibility_exception" DROP COLUMN "reaction_description";
ALTER TABLE "compatibility_exception" DROP COLUMN "compatible";
ALTER TABLE "compatibility_exception" ADD COLUMN "compatible" BOOLEAN NOT NULL;
ALTER TABLE "compatibility_exception" ADD COLUMN "exception_type" "CompatibilityExceptionType" NOT NULL;
ALTER TABLE "compatibility_exception" ADD COLUMN "appendix" TEXT;
ALTER TABLE "compatibility_exception" ADD COLUMN "section" TEXT;
ALTER TABLE "compatibility_exception" ADD COLUMN "reason" TEXT;

CREATE UNIQUE INDEX "compatibility_exception_cargo_a_id_cargo_b_id_key"
    ON "compatibility_exception" ("cargo_a_id", "cargo_b_id");
CREATE INDEX "compatibility_exception_cargo_a_id_idx" ON "compatibility_exception" ("cargo_a_id");
CREATE INDEX "compatibility_exception_cargo_b_id_idx" ON "compatibility_exception" ("cargo_b_id");
