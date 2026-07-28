-- Bring the DB in line with schema.prisma: cargo_reactive_group.group_code was
-- declared (required Int) and is written by link_cargo_reactive_groups.py, but the
-- column was never added to this DB. Add it, backfill from the linked reactive
-- group's numeric code (all values are numeric), then enforce NOT NULL.

ALTER TABLE "cargo_reactive_group" ADD COLUMN "group_code" INTEGER;

UPDATE "cargo_reactive_group" crg
   SET "group_code" = rg."group_code"::int
  FROM "reactive_groups" rg
 WHERE rg."id" = crg."reactive_group_id";

ALTER TABLE "cargo_reactive_group" ALTER COLUMN "group_code" SET NOT NULL;
