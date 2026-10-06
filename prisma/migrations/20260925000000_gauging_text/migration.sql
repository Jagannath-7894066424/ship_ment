-- cargo_chemical.gauging: enum -> text.
--
-- WHY
-- ---
-- The column was the enum `gauging`, whose only labels are O, R and C. That is
-- the code a source prints, not what it means, and the IBC Code sheet prints
-- BOTH: 797 rows carry a bare letter and three carry the legend itself -
-- "C (closed gouging)", "R (restricted gauging)", "O(open gauging)". The
-- expanded form is not a valid label, so the loader could not coerce it and
-- stored NULL: Acetic acid, Acetic anhydride and Acetochlor lost their gauging
-- while every other row kept it. The bug looked like a broken mapping and was
-- not one - the mapping worked, the type refused the value.
--
-- Storing the wording rather than the code is a deliberate instruction, and an
-- enum cannot hold it without either adding the three sentences as labels
-- (leaving six labels, three of them legacy) or rewriting the type each time a
-- source words the legend differently. Text holds what a source prints, which
-- is what every other source-scoped column in this schema already does.
--
-- WHAT IS NOT DONE HERE
-- ---------------------
-- No value is rewritten by this migration. It only widens the type; the
-- expansion of source 1's rows is the loader's job, so that re-running the
-- import reproduces the database rather than depending on a one-off UPDATE.
-- Source 9 (Miracle) keeps its bare letters, by instruction.
--
-- The enum TYPE is left in place, unused. Dropping it is a separate decision:
-- nothing references it after this, but a rollback would need it back.
--
-- Applied by hand via psycopg2 (this database is not prisma-migrate tracked).
-- Idempotent: re-running finds the column already text and does nothing.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_name = 'cargo_chemical'
                  AND column_name = 'gauging'
                  AND udt_name = 'gauging') THEN
        ALTER TABLE "cargo_chemical"
            ALTER COLUMN "gauging" TYPE TEXT USING "gauging"::text;
    END IF;
END $$;
