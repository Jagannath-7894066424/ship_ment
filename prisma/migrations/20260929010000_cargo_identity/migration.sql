-- cargo_identity(id): every cargo_chemical row that is the same chemical as `id`.
--
-- WHY
-- ---
-- Each source loads its own cargo_chemical row (5279 rows for ~4000 names), and an
-- exception or reactive-group link points at ONE of them. A lookup that uses only
-- the ids it was given misses the rule whenever the caller picked another source's
-- row (e.g. "Caustic soda ≤50%" source 4 vs "Caustic soda 50%" source 13).
--
-- RULE
-- ----
--   strict = same normalized canonical_name (the normalization master_loader uses
--            for synonyms.normalized_text: lowercase, punctuation -> space, collapse).
--   loose  = one hop through a non-ambiguous synonym, either direction, from any
--            strict row. Not transitive.
-- CAS is deliberately NOT used: one CAS spans concentrations (Dimethylamine
-- solution 45% or less / >45%) and product families, and Appendix I outcomes
-- depend on concentration. Callers may apply loose matches only to make a result
-- MORE restrictive (incompatible), never to grant compatibility.

CREATE OR REPLACE FUNCTION cargo_norm_name(t text) RETURNS text
LANGUAGE sql IMMUTABLE STRICT AS $$
  SELECT btrim(regexp_replace(regexp_replace(lower(t), '[^\w\s]', ' ', 'g'), '\s+', ' ', 'g'))
$$;

CREATE INDEX IF NOT EXISTS cargo_chemical_norm_name_idx ON cargo_chemical (cargo_norm_name(canonical_name));
CREATE INDEX IF NOT EXISTS synonyms_normalized_text_idx ON synonyms (normalized_text);

CREATE OR REPLACE FUNCTION cargo_identity(p_id int) RETURNS TABLE (cargo_id int, strict boolean)
LANGUAGE sql STABLE AS $$
  WITH me AS (
    SELECT cargo_norm_name(canonical_name) AS nm FROM cargo_chemical WHERE id = p_id
  ), strict_ids AS (
    SELECT c.id FROM cargo_chemical c, me WHERE cargo_norm_name(c.canonical_name) = me.nm
  ), strict_syn AS (
    SELECT s.normalized_text AS t
    FROM cargo_synonym cs JOIN synonyms s ON s.id = cs.synonym_id
    WHERE cs.cargo_id IN (SELECT id FROM strict_ids) AND NOT cs.ambiguity_flag
  ), loose_ids AS (
    SELECT c.id FROM cargo_chemical c
    WHERE cargo_norm_name(c.canonical_name) IN (SELECT t FROM strict_syn)
    UNION
    SELECT cs.cargo_id
    FROM cargo_synonym cs JOIN synonyms s ON s.id = cs.synonym_id, me
    WHERE NOT cs.ambiguity_flag AND s.normalized_text = me.nm
  )
  SELECT id, true FROM strict_ids
  UNION ALL
  SELECT id, false FROM loose_ids WHERE id NOT IN (SELECT id FROM strict_ids)
$$;
