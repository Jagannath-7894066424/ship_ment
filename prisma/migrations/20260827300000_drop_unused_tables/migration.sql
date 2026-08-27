-- Drop nine tables that were designed but never populated.
--
-- Each was created for a feature that no loader ever fed. All nine are empty in
-- every environment, no ETL script writes them, no application code names them,
-- and every foreign key involving them points OUTWARD (dead table -> live
-- table), so nothing that is kept refers to them and nothing cascades:
--
--   coating_company -> coating_system -> cargo_coating
--       The coating chain from 20260710123245_normalize_cleaning_coatings_and_
--       properties. cargo_chemical.permitted_coatings was normalised into it and
--       the normalising loader was never written.
--
--   cleanliness_standards -> cargo_required_cleanliness
--       The INTERTANKO source row exists (source 'INTERTANKO Cargo Tank
--       Cleanliness Standards') but no extract of it was ever loaded.
--
--   marine_chemicals -> marine_chemical_use
--       Cleaning-product catalogue. Never filled; marine_chemical_use also
--       linked procedure_templates to it.
--
--   changelog
--       Audit trail. Never written; the ETL records history in the migration
--       files and etl/data/load_manifest.json instead.
--
--   dot_hazad_symbol
--       Superseded rather than unfinished. It was meant to be a lookup of the
--       single 49 CFR symbols ("+", "A", "D", "G", "I", "W"), but the source
--       prints combinations ("A W") and dot_hazmat_symbol_loader.py stores the
--       symbol verbatim in cargo_hazard_data.dot_symbol. Nothing FKs to it.
--
-- Bringing any of these back means restoring the model and a CREATE TABLE, which
-- is what the migration named above still holds for the coating chain.
--
-- SAFETY
-- ------
-- The premise of this migration is that the tables are empty. That is checked
-- here rather than assumed: if any row exists, the whole thing aborts and
-- nothing is dropped. It runs inside a transaction (apply_migration.py), so an
-- abort leaves the database untouched.
--
-- Idempotent: the check skips tables that are already gone, and the drops are
-- IF EXISTS. Dropped child-first so a missed dependency raises instead of being
-- silently cascaded away - there is deliberately no CASCADE here.

DO $$
DECLARE
    t     text;
    n     bigint;
    found text[] := '{}';
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'cargo_coating', 'coating_system', 'coating_company',
        'cargo_required_cleanliness', 'cleanliness_standards',
        'marine_chemical_use', 'marine_chemicals',
        'changelog', 'dot_hazad_symbol'
    ] LOOP
        IF to_regclass('public.' || quote_ident(t)) IS NOT NULL THEN
            EXECUTE format('SELECT count(*) FROM %I', t) INTO n;
            IF n > 0 THEN
                found := found || format('%s (%s rows)', t, n);
            END IF;
        END IF;
    END LOOP;

    IF array_length(found, 1) > 0 THEN
        RAISE EXCEPTION
            'Refusing to drop: % is not empty. This migration assumes these '
            'tables were never populated; something has loaded them since. '
            'Nothing was dropped.', array_to_string(found, ', ');
    END IF;
END $$;

DROP TABLE IF EXISTS "cargo_coating";
DROP TABLE IF EXISTS "coating_system";
DROP TABLE IF EXISTS "coating_company";

DROP TABLE IF EXISTS "cargo_required_cleanliness";
DROP TABLE IF EXISTS "cleanliness_standards";

DROP TABLE IF EXISTS "marine_chemical_use";
DROP TABLE IF EXISTS "marine_chemicals";

DROP TABLE IF EXISTS "changelog";
DROP TABLE IF EXISTS "dot_hazad_symbol";
