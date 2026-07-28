-- Deduplicate then enforce uniqueness so re-running the loader can upsert
-- instead of creating duplicate rows.

-- 1) Remove duplicate children not already removed by cascade (keep MIN id).
DELETE FROM "cargo_property_values" a USING "cargo_property_values" b
 WHERE a.cargo_id=b.cargo_id AND a.source_id=b.source_id
   AND a.field_name=b.field_name AND a.id>b.id;
DELETE FROM "cargo_synonym" a USING "cargo_synonym" b
 WHERE a.cargo_id=b.cargo_id AND a.synonym_id=b.synonym_id AND a.id>b.id;

-- 2) Remove duplicate chemicals (keep MIN id); cascades clean their children.
DELETE FROM "cargo_chemical" a USING "cargo_chemical" b
 WHERE a.source_id IS NOT DISTINCT FROM b.source_id
   AND a.canonical_name=b.canonical_name AND a.id>b.id;

-- 3) Unique constraints.
CREATE UNIQUE INDEX "cargo_chemical_source_id_canonical_name_key" ON "cargo_chemical"("source_id", "canonical_name");
CREATE UNIQUE INDEX "cargo_property_values_cargo_id_source_id_field_name_key" ON "cargo_property_values"("cargo_id", "source_id", "field_name");
CREATE UNIQUE INDEX "cargo_synonym_cargo_id_synonym_id_key" ON "cargo_synonym"("cargo_id", "synonym_id");
