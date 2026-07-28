-- Verbatim import target for the 49 CFR 172.101 Hazardous Materials Table.
-- Every source row is preserved; no cargo_chemical rows are created. cargo_id is
-- NULL until a row is linked to a canonical chemical.

CREATE TABLE "cargo_dot_hazad" (
    "id"                       SERIAL PRIMARY KEY,

    "cargo_id"                 INTEGER,
    "source_id"                INTEGER NOT NULL,

    -- idempotency: hash of all CFR columns for this row
    "row_hash"                 TEXT    NOT NULL,

    -- classification
    "entry_type"               TEXT    NOT NULL,   -- material | generic | forbidden | cross_reference
    "see_reference"            TEXT,

    -- CFR columns (1)..(10B)
    "symbol"                   TEXT,               -- (1)
    "proper_shipping_name"     TEXT    NOT NULL,   -- (2)
    "hazard_class"             TEXT,               -- (3)
    "identification_number"    TEXT,               -- (4)
    "packing_group"            TEXT,               -- (5)
    "label_codes"              TEXT,               -- (6)
    "special_provisions"       TEXT,               -- (7)
    "packaging_exception"      TEXT,               -- (8A)
    "packaging_non_bulk"       TEXT,               -- (8B)
    "packaging_bulk"           TEXT,               -- (8C)
    "passenger_quantity_limit" TEXT,               -- (9A)
    "cargo_quantity_limit"     TEXT,               -- (9B)
    "vessel_location"          TEXT,               -- (10A)
    "vessel_other"             TEXT,               -- (10B)

    "created_at"               TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at"               TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT "cargo_dot_hazad_cargo_id_fkey"
        FOREIGN KEY ("cargo_id") REFERENCES "cargo_chemical"("id")
        ON DELETE SET NULL ON UPDATE CASCADE,
    CONSTRAINT "cargo_dot_hazad_source_id_fkey"
        FOREIGN KEY ("source_id") REFERENCES "source"("id")
        ON DELETE CASCADE ON UPDATE CASCADE
);

-- Idempotency key: one row per (source, content-hash-of-CFR-columns).
CREATE UNIQUE INDEX "cargo_dot_hazad_source_id_row_hash_key"
    ON "cargo_dot_hazad"("source_id", "row_hash");

CREATE INDEX "cargo_dot_hazad_cargo_id_idx"              ON "cargo_dot_hazad"("cargo_id");
CREATE INDEX "cargo_dot_hazad_source_id_idx"             ON "cargo_dot_hazad"("source_id");
CREATE INDEX "cargo_dot_hazad_proper_shipping_name_idx"  ON "cargo_dot_hazad"("proper_shipping_name");
CREATE INDEX "cargo_dot_hazad_identification_number_idx" ON "cargo_dot_hazad"("identification_number");
CREATE INDEX "cargo_dot_hazad_entry_type_idx"            ON "cargo_dot_hazad"("entry_type");
