-- Descriptive (narrative) hazard information per chemical per source.
--
-- Keeps the schema normalized: measurable / numeric properties stay in
-- cargo_property_values; only free-text hazard descriptions live here. One row
-- per (cargo_id, source_id).

CREATE TABLE "cargo_hazard_data" (
    "id"                       SERIAL       PRIMARY KEY,
    "cargo_id"                 INTEGER      NOT NULL,
    "source_id"                INTEGER      NOT NULL,
    -- Health
    "health_hazard_rating"     TEXT,
    "general_hazard"           TEXT,
    "symptoms"                 TEXT,
    "short_exposure_tolerance" TEXT,
    "exposure_procedure"       TEXT,
    -- Fire
    "fire_grade"               TEXT,
    "electrical_group"         TEXT,
    "extinguishing_agents"     TEXT,
    "special_fire_procedure"   TEXT,
    -- Reactivity
    "stability"                TEXT,
    "material_compatibility"   TEXT,
    "cargo_compatibility_note" TEXT,
    -- Spill
    "spill_procedure"          TEXT,
    "notes"                    TEXT,
    "created_at"               TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at"               TIMESTAMP(3) NOT NULL,
    CONSTRAINT "cargo_hazard_data_cargo_id_fkey"
        FOREIGN KEY ("cargo_id") REFERENCES "cargo_chemical"("id")
        ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT "cargo_hazard_data_source_id_fkey"
        FOREIGN KEY ("source_id") REFERENCES "source"("id")
        ON DELETE CASCADE ON UPDATE CASCADE
);

CREATE UNIQUE INDEX "cargo_hazard_data_cargo_id_source_id_key" ON "cargo_hazard_data"("cargo_id", "source_id");
CREATE INDEX "cargo_hazard_data_cargo_id_idx"  ON "cargo_hazard_data"("cargo_id");
CREATE INDEX "cargo_hazard_data_source_id_idx" ON "cargo_hazard_data"("source_id");
