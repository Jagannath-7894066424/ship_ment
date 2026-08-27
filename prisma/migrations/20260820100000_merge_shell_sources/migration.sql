-- Merge the two Shell sources into the one document they were both read from.
--
-- The Shell material arrived as two spreadsheet extracts and was registered as
-- two sources:
--
--     24  Shell Tank Cleaning Procedure    procedure_templates + children
--     25  Shell Tank Cleanning Data        crude_oil, properties, synonyms
--
-- They are not two authorities. Both are extracts of ONE document, "Shell Tank
-- Cleaning Guide 2016.pdf", so they collapse into a single source row. Source
-- 24 is kept and renamed - it already carries procedure_templates, whose key is
-- (source_id, procedure_code), so reusing its id leaves that data untouched.
--
-- Source 25's rows are REPOINTED, not deleted: no oil, property value or
-- synonym link is lost. Source 24 holds no crude_oil rows, so repointing cannot
-- collide with anything on the (oil_name, source_id) identity.
--
-- Ranks are merged rather than picked: the guide is the cleaning authority
-- (rank_cleaning 1, from 24) AND the physical-property authority for its
-- refined products (rank_physical 3, from 25). Dropping either would silently
-- change which source wins a conflict.
--
-- Idempotent: re-running finds the merge already done and makes no change.

DO $$
DECLARE
    guide_id integer;
    data_id  integer;
    moved    integer;
BEGIN
    -- The surviving row, under either its old or its new name.
    SELECT id INTO guide_id FROM source
     WHERE name IN ('Shell Tank Cleaning Procedure', 'Shell Tank Cleaning Guide 2016.pdf')
     ORDER BY id LIMIT 1;

    IF guide_id IS NULL THEN
        RAISE EXCEPTION
            'Neither "Shell Tank Cleaning Procedure" nor "Shell Tank Cleaning Guide 2016.pdf" '
            'is registered; run `python3 etl/common/source.py` first.';
    END IF;

    SELECT id INTO data_id FROM source WHERE name = 'Shell Tank Cleanning Data';

    IF data_id IS NOT NULL THEN
        UPDATE crude_oil SET source_id = guide_id, updated_at = now()
         WHERE source_id = data_id;
        GET DIAGNOSTICS moved = ROW_COUNT;
        RAISE NOTICE 'crude_oil                 : % rows -> source %', moved, guide_id;

        UPDATE crude_oil_property_values SET source_id = guide_id, updated_at = now()
         WHERE source_id = data_id;
        GET DIAGNOSTICS moved = ROW_COUNT;
        RAISE NOTICE 'crude_oil_property_values : % rows -> source %', moved, guide_id;

        UPDATE crude_oil_synonym SET source_id = guide_id, updated_at = now()
         WHERE source_id = data_id;
        GET DIAGNOSTICS moved = ROW_COUNT;
        RAISE NOTICE 'crude_oil_synonym         : % rows -> source %', moved, guide_id;

        -- `synonyms` is the vocabulary the chemical branch shares. Only the
        -- rows this Shell extract introduced carry source_id 25, so only those
        -- move; nothing the chemical branch owns is touched.
        UPDATE synonyms SET source_id = guide_id, updated_at = now()
         WHERE source_id = data_id;
        GET DIAGNOSTICS moved = ROW_COUNT;
        RAISE NOTICE 'synonyms                  : % rows -> source %', moved, guide_id;

        DELETE FROM source WHERE id = data_id;
        RAISE NOTICE 'deleted source % (Shell Tank Cleanning Data)', data_id;
    ELSE
        RAISE NOTICE 'source "Shell Tank Cleanning Data" already gone - nothing to move';
    END IF;

    UPDATE source
       SET name          = 'Shell Tank Cleaning Guide 2016.pdf',
           edition       = '2016',
           file_path     = 'Shell Tank Cleaning Guide 2016.pdf',
           rank_cleaning = 1,
           rank_physical = 3,
           notes         = 'Single source for all Shell tank-cleaning material. '
                           'Merged from "Shell Tank Cleaning Procedure" (procedures) '
                           'and "Shell Tank Cleanning Data" (cargo master).',
           updated_at    = now()
     WHERE id = guide_id;

    RAISE NOTICE 'source % is now "Shell Tank Cleaning Guide 2016.pdf"', guide_id;
END $$;
