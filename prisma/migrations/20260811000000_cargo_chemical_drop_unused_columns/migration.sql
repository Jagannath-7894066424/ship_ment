-- Drop cargo_chemical columns that hold no data and participate in no relationship.
--
-- Only five cargo_chemical columns are referenced by anything outside the table:
--   id                    - 15 foreign keys across 12 tables
--   source_id             - FK -> source.id
--   cargo_family_group_id - FK -> cargo_family_group.id
--   canonical_name        - UNIQUE(source_id, canonical_name) + index
--   cas_number            - indexed; cross-source join key to Sittig / DOT / USCG
--
-- The 19 columns dropped here are outside that set AND empty across all 4,404
-- rows after a full load of every source. No loader writes them and no
-- application code reads them (verified: the only cargo_chemical column the app
-- touches is canonical_name).
--
-- Where the same information does exist, it is already in cargo_property_values
-- and is NOT lost by this migration:
--   autoignition_temp_c  -> field 'auto_ignition_temperature'  229 rows
--   carcinogen_iarc      -> field 'carcinogen_iarc'            220 rows
--
-- permitted_coatings was already marked DEPRECATED in schema.prisma, superseded
-- by cargo_coating -> coating_system -> coating_company.
--
-- Columns that are non-relational but DO hold data (44,212 values across 43
-- columns - the IBC carriage block, the heating block, the duplicated physical
-- properties) are deliberately left alone. Dropping those is a separate,
-- data-losing decision that needs a backfill plan first.

ALTER TABLE "cargo_chemical"
    DROP COLUMN IF EXISTS "chris_code",
    DROP COLUMN IF EXISTS "imdg_class",
    DROP COLUMN IF EXISTS "dot_hazmat_id",
    DROP COLUMN IF EXISTS "physical_state_20c",
    DROP COLUMN IF EXISTS "autoignition_temp_c",
    DROP COLUMN IF EXISTS "ghs_pictograms",
    DROP COLUMN IF EXISTS "ghs_signal_word",
    DROP COLUMN IF EXISTS "h_statements",
    DROP COLUMN IF EXISTS "carcinogen_iarc",
    DROP COLUMN IF EXISTS "tlv_twa_ppm",
    DROP COLUMN IF EXISTS "idlh_ppm",
    DROP COLUMN IF EXISTS "inert_gas_required",
    DROP COLUMN IF EXISTS "heating_temp_min_c",
    DROP COLUMN IF EXISTS "heating_temp_max_c",
    DROP COLUMN IF EXISTS "permitted_tank_materials",
    DROP COLUMN IF EXISTS "permitted_coatings",
    DROP COLUMN IF EXISTS "data_completeness_score",
    DROP COLUMN IF EXISTS "odour",
    DROP COLUMN IF EXISTS "date_example";
