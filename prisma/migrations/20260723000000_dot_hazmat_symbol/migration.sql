-- DOT Hazardous Materials Table (49 CFR 172.101) Column (1) symbols.
-- 1) A legend table storing the single symbols + their meaning.
-- 2) A verbatim symbol column on cargo_hazard_data (may combine symbols, e.g. "A W").

-- 1) Legend / reference table (the symbol only, plus its meaning).
CREATE TABLE "dot_hazmat_symbol" (
    "symbol"      TEXT PRIMARY KEY,
    "description" TEXT NOT NULL
);

INSERT INTO "dot_hazmat_symbol" ("symbol", "description") VALUES
    ('+', 'Fixed proper shipping name, hazard class and packing group.'),
    ('A', 'Aircraft transportation entry.'),
    ('D', 'Domestic transportation entry.'),
    ('G', 'Technical name required.'),
    ('I', 'International transportation entry.'),
    ('W', 'Vessel transportation entry.')
ON CONFLICT ("symbol") DO NOTHING;

-- 2) Store the raw Column (1) text against a cargo's hazard record.
ALTER TABLE "cargo_hazard_data" ADD COLUMN "dot_symbol" TEXT;
