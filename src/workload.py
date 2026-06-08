"""
The canonical read workload. Each query models a concrete action an automated
Dungeon Master agent would issue while adjudicating play. Imported by bench.py and
referenced by the docs so there is one source of truth.

`winner` names the treatment we expect to first make the query fast (for the writeup).
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class Query:
    id: str
    label: str
    agent_action: str
    sql: str
    params: tuple
    winner: str


WORKLOAD: list[Query] = [
    Query(
        "Q1", "point lookup by name",
        'agent resolves "the NPC casts Fireball" -> fetch that one rule (hot path)',
        "SELECT * FROM spell WHERE name = %s",
        ("Fireball",), "T2_hash"),
    Query(
        "Q2", "range scan by level",
        'agent enumerates high-level threats: spells of level 8 and up',
        "SELECT id, name, level FROM spell WHERE level >= %s",
        (8,), "T1_btree"),
    Query(
        "Q3", "two low-cardinality predicates (BitmapAnd)",
        'agent builds a candidate list: Divination spells that require concentration',
        "SELECT id, name FROM spell WHERE school_id = %s AND concentration = %s",
        (3, True), "T4_bitmap"),
    Query(
        "Q4", "full-text body search",
        'agent answers "which rules involve fire damage?" (keyword retrieval)',
        "SELECT id, name FROM spell WHERE body_tsv @@ to_tsquery('english', %s)",
        ("fire & damage",), "T3_gin"),
    Query(
        "Q5", "class-membership join",
        'agent stats out a caster: "spells a Wizard can cast at level <= 1"',
        """SELECT s.id, s.name
             FROM spell s
             JOIN spell_class sc ON sc.spell_id = s.id
             JOIN caster_class c ON c.id = sc.class_id
            WHERE c.name = %s AND s.level <= %s""",
        ("Wizard", 1), "T5_join"),
    Query(
        "Q6", "Top-N ordered browse",
        'agent pages through rules: order by level, name limit 20',
        "SELECT id, name, level FROM spell ORDER BY level, name LIMIT %s",
        (20,), "T1_btree"),
    Query(
        "Q7", "array dimension join",
        'agent finds "all spells that deal poison damage"',
        """SELECT s.id, s.name
             FROM spell s
             JOIN spell_damage sd ON sd.spell_id = s.id
            WHERE sd.damage_type = %s""",
        ("poison",), "T5_join"),
]

# The hot point-lookup query reused (with many different keys) for the throughput test.
THROUGHPUT_SQL = "SELECT * FROM spell WHERE name = %s"
