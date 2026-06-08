-- Index treatments, applied CUMULATIVELY (T0 -> T5). bench.py parses this file on
-- the `-- @treatment NAME : description` markers, drops all secondary indexes to get
-- the T0 baseline, then adds each treatment's indexes in order and re-measures the
-- whole workload after each step. You can also apply any block by hand in `make psql`.
--
-- T0 (baseline) has no statements: it is the bare schema (primary keys only),
-- which forces sequential scans -- the "before" picture.

-- @treatment T1_btree : B-tree on spell.level (range scans + ordered browse)
CREATE INDEX IF NOT EXISTS ix_spell_level ON spell (level);

-- @treatment T2_hash : Hash index on spell.name (exact point lookups)
CREATE INDEX IF NOT EXISTS ix_spell_name_hash ON spell USING hash (name);

-- @treatment T3_gin : GIN index on the full-text vector (keyword body search)
CREATE INDEX IF NOT EXISTS ix_spell_body_gin ON spell USING gin (body_tsv);

-- @treatment T4_bitmap : single-column B-trees on TWO moderately-selective columns
--                        (school_id, concentration) so the planner combines them with a
--                        BitmapAnd for the multi-predicate candidate-list query (Q3)
CREATE INDEX IF NOT EXISTS ix_spell_school ON spell (school_id);
CREATE INDEX IF NOT EXISTS ix_spell_conc ON spell (concentration);

-- @treatment T5_join : B-tree support for the relationship-table joins
CREATE INDEX IF NOT EXISTS ix_spellclass_class ON spell_class (class_id, spell_id);
CREATE INDEX IF NOT EXISTS ix_spelldamage_type ON spell_damage (damage_type, spell_id);
