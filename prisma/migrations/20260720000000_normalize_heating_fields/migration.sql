-- Normalize the heating-related fields on cargo_chemical.
--
-- Before: three Boolean columns whose source cells actually mixed data types —
--   "N" (not required), "Y" (required, no temperature) or a number (required,
--   with the minimum/maximum temperature in °C). The temperature was lost from
--   the wide table (it only survived as a cargo_property_values row).
--
-- After: each heating stage is split into a Boolean flag + a DECIMAL(5,2)
--   temperature:
--     heat_adjacent      -> heat_adjacent_required        + heat_adjacent_temperature
--     heating_required_voyage    (kept) + heating_voyage_temperature
--     heating_required_discharge (kept) + heating_discharge_temperature
--
-- Backward compatibility: the existing Boolean values are preserved (the
-- adjacent column is renamed, the voyage/discharge columns keep their names) and
-- the temperatures are backfilled from cargo_property_values where available.

-- 1) Adjacent: rename the Boolean (preserves existing data), add its temperature.
ALTER TABLE "cargo_chemical" RENAME COLUMN "heat_adjacent" TO "heat_adjacent_required";
ALTER TABLE "cargo_chemical" ADD COLUMN "heat_adjacent_temperature" DECIMAL(5,2);

-- 2) Voyage / discharge: the Boolean columns already exist and keep their names;
--    only the new temperature columns are added.
ALTER TABLE "cargo_chemical" ADD COLUMN "heating_voyage_temperature" DECIMAL(5,2);
ALTER TABLE "cargo_chemical" ADD COLUMN "heating_discharge_temperature" DECIMAL(5,2);

-- 3) Backfill temperatures from cargo_property_values (best effort). LARS stored
--    the heating temperature there as a numeric property; normalized_value is the
--    parsed number. The range guard keeps the DECIMAL(5,2) cast from overflowing.
UPDATE "cargo_chemical" cc
   SET "heat_adjacent_temperature" = pv."normalized_value"
  FROM "cargo_property_values" pv
 WHERE pv."cargo_id" = cc."id"
   AND pv."field_name" = 'heat_adjacent_temp_c'
   AND pv."normalized_value" IS NOT NULL
   AND pv."normalized_value" BETWEEN -999.99 AND 999.99;

UPDATE "cargo_chemical" cc
   SET "heating_voyage_temperature" = pv."normalized_value"
  FROM "cargo_property_values" pv
 WHERE pv."cargo_id" = cc."id"
   AND pv."field_name" = 'heating_voyage_temp_c'
   AND pv."normalized_value" IS NOT NULL
   AND pv."normalized_value" BETWEEN -999.99 AND 999.99;

UPDATE "cargo_chemical" cc
   SET "heating_discharge_temperature" = pv."normalized_value"
  FROM "cargo_property_values" pv
 WHERE pv."cargo_id" = cc."id"
   AND pv."field_name" = 'heating_discharge_temp_c'
   AND pv."normalized_value" IS NOT NULL
   AND pv."normalized_value" BETWEEN -999.99 AND 999.99;
