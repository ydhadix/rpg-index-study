-- Normalized schema for the spell-lookup indexing study.
-- Deliberately ships with NO secondary indexes: treatment T0 (baseline) is the
-- bare schema, and sql/treatments.sql adds indexes one treatment at a time so we
-- can measure the effect of each.

DROP TABLE IF EXISTS spell_class, spell_damage, spell, caster_class, school CASCADE;

-- ---- dimension / lookup tables ------------------------------------------------
CREATE TABLE school (
    id    smallint PRIMARY KEY,
    code  char(1)  NOT NULL UNIQUE,
    name  text     NOT NULL
);

CREATE TABLE caster_class (
    id    smallint    PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    name  text        NOT NULL UNIQUE
);

-- ---- fact table ---------------------------------------------------------------
CREATE TABLE spell (
    id              bigint   PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    slug            text     NOT NULL,
    name            text     NOT NULL,
    source          text     NOT NULL,
    level           smallint NOT NULL,          -- 0..9, low cardinality
    school_id       smallint NOT NULL REFERENCES school(id),
    casting_time    text,
    range_text      text,
    range_feet      int,                          -- NULL for self/touch/special
    comp_v          boolean  NOT NULL DEFAULT false,
    comp_s          boolean  NOT NULL DEFAULT false,
    comp_m          boolean  NOT NULL DEFAULT false,
    material        text,
    duration_text   text,
    concentration   boolean  NOT NULL DEFAULT false,
    ritual          boolean  NOT NULL DEFAULT false,
    body            text     NOT NULL DEFAULT '',
    -- precomputed full-text vector over name + body; the GIN treatment indexes this
    body_tsv        tsvector GENERATED ALWAYS AS
                        (to_tsvector('english', name || ' ' || body)) STORED,
    -- provenance: NULL for the real base spells, set to the base id for inflated clones
    source_spell_id bigint
);

-- ---- relationship tables ------------------------------------------------------
CREATE TABLE spell_class (
    spell_id  bigint   NOT NULL REFERENCES spell(id) ON DELETE CASCADE,
    class_id  smallint NOT NULL REFERENCES caster_class(id),
    PRIMARY KEY (spell_id, class_id)
);

CREATE TABLE spell_damage (
    spell_id     bigint NOT NULL REFERENCES spell(id) ON DELETE CASCADE,
    damage_type  text   NOT NULL,
    PRIMARY KEY (spell_id, damage_type)
);
