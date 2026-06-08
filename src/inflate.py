#!/usr/bin/env python3
"""
Synthetically inflate the spell table to ~ROWS rows so timing differences between
index strategies are measurable at a realistic scale.

Method (disclosed as a limitation in the writeup): each real spell is replicated
many times with a unique surrogate name ("Fireball #4217"); EVERY other attribute
(level, school, components, body, class list, damage types) is copied verbatim.
Replication therefore preserves the real marginal AND joint distributions of all
indexed columns exactly -- it is not uniform-random noise. The clone's
`source_spell_id` points back to the real spell it came from.

The work is done with INSERT ... SELECT inside Postgres (no per-row round trips),
so 100K rows load in well under a second.

Run:  python src/inflate.py --rows 100000   (or `make inflate ROWS=100000`)
"""
from __future__ import annotations
import argparse

from db import connect

SPELL_COLS = """
    slug, name, source, level, school_id, casting_time, range_text, range_feet,
    comp_v, comp_s, comp_m, material, duration_text, concentration, ritual, body,
    source_spell_id
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=100_000,
                    help="target total spell count (default 100000)")
    args = ap.parse_args()
    target = args.rows

    with connect(autocommit=False) as conn, conn.cursor() as cur:
        # idempotent: drop any previous clones (cascades to their class/damage rows)
        cur.execute("DELETE FROM spell WHERE source_spell_id IS NOT NULL")
        cur.execute("SELECT count(*) FROM spell")
        base = cur.fetchone()[0]
        if target <= base:
            conn.commit()
            print(f"target {target} <= base {base}; nothing to inflate.")
            return 0

        needed = target - base
        reps = -(-needed // base)          # ceil division
        print(f"base={base} spells; cloning to reach {target} "
              f"(reps={reps}, exact clones={needed}) ...")

        # Build clones with a unique surrogate name, capped to exactly `needed`.
        cur.execute(f"""
            WITH base AS (SELECT * FROM spell WHERE source_spell_id IS NULL),
                 nums AS (SELECT generate_series(1, %s) AS g),
                 clones AS (
                     SELECT b.*, g, row_number() OVER () AS rn
                     FROM base b CROSS JOIN nums
                 )
            INSERT INTO spell ({SPELL_COLS})
            SELECT slug || '-' || g,
                   name || ' #' || g,
                   source, level, school_id, casting_time, range_text, range_feet,
                   comp_v, comp_s, comp_m, material, duration_text, concentration,
                   ritual, body,
                   id                       -- provenance: the real spell id
            FROM clones
            WHERE rn <= %s
        """, (reps, needed))

        # Propagate the M:N relationships to every clone from its source spell.
        cur.execute("""
            INSERT INTO spell_class (spell_id, class_id)
            SELECT c.id, sc.class_id
            FROM spell c
            JOIN spell_class sc ON sc.spell_id = c.source_spell_id
            WHERE c.source_spell_id IS NOT NULL
        """)
        cur.execute("""
            INSERT INTO spell_damage (spell_id, damage_type)
            SELECT c.id, sd.damage_type
            FROM spell c
            JOIN spell_damage sd ON sd.spell_id = c.source_spell_id
            WHERE c.source_spell_id IS NOT NULL
        """)
        conn.commit()

        # Refresh planner statistics so EXPLAIN reflects the inflated table.
        cur.execute("ANALYZE spell")
        cur.execute("ANALYZE spell_class")
        cur.execute("ANALYZE spell_damage")

        cur.execute("SELECT count(*) FROM spell")
        total = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM spell_class")
        nclass = cur.fetchone()[0]
        print(f"done. spell rows: {total:,} | spell_class rows: {nclass:,}")
        # show the preserved distribution
        cur.execute("SELECT level, count(*) FROM spell GROUP BY level ORDER BY level")
        dist = ", ".join(f"L{lv}:{c:,}" for lv, c in cur.fetchall())
        print(f"level distribution (preserved): {dist}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
