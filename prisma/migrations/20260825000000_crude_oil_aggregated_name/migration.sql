-- Add crude_oil.aggregated_name.
--
-- A free-text label held alongside oil_name, not instead of it. oil_name stays
-- the source's own wording and is half of the (oil_name, source_id) identity
-- key, so it cannot be normalised in place without collapsing rows that three
-- assay sources deliberately keep apart.
--
-- Nullable with no default: the 593 existing rows have no aggregated name yet,
-- and NULL says "not assigned" where '' would claim the value is known to be
-- blank. Loaders may leave it unset.
--
-- No index. At 593 rows a sequential scan is cheaper than maintaining one; add
-- crude_oil_aggregated_name_idx if this ever becomes a lookup key.

ALTER TABLE "crude_oil" ADD COLUMN IF NOT EXISTS "aggregated_name" TEXT;
