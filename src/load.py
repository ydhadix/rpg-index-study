#!/usr/bin/env python3
"""
Create the schema and load the real (canonical) spells from the ETL CSVs.

Run:  python src/load.py   (or `make load`)
Requires the database to be up (`make up`) and `make etl` to have produced data/*.csv.
"""
from __future__ import annotations
import csv
from pathlib import Path

from db import connect

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DATA = REPO / "data"
SCHEMA = REPO / "sql" / "schema.sql"

SCHOOLS = [
    (1, "A", "Abjuration"), (2, "C", "Conjuration"), (3, "D", "Divination"),
    (4, "E", "Enchantment"), (5, "V", "Evocation"), (6, "I", "Illusion"),
    (7, "N", "Necromancy"), (8, "T", "Transmutation"),
]


def _bool(v: str) -> bool:
    return str(v).strip().lower() in ("true", "1", "t", "yes")


def main() -> int:
    spells = list(csv.DictReader(open(DATA / "spells.csv")))
    classes = list(csv.DictReader(open(DATA / "spell_classes.csv")))
    damage = list(csv.DictReader(open(DATA / "spell_damage.csv")))

    with connect(autocommit=False) as conn, conn.cursor() as cur:
        cur.execute(SCHEMA.read_text())

        # dimensions
        cur.executemany(
            "INSERT INTO school (id, code, name) VALUES (%s, %s, %s)", SCHOOLS)
        code_to_school = {c: i for (i, c, _n) in SCHOOLS}

        class_names = sorted({r["class_name"] for r in classes})
        cur.executemany(
            "INSERT INTO caster_class (name) VALUES (%s)",
            [(n,) for n in class_names])
        cur.execute("SELECT id, name FROM caster_class")
        class_to_id = {n: i for (i, n) in cur.fetchall()}

        # fact table — keep slug->id so the relationship tables can resolve FKs
        slug_to_id: dict[str, int] = {}
        for r in spells:
            cur.execute(
                """INSERT INTO spell
                   (slug, name, source, level, school_id, casting_time, range_text,
                    range_feet, comp_v, comp_s, comp_m, material, duration_text,
                    concentration, ritual, body)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   RETURNING id""",
                (r["slug"], r["name"], r["source"], int(r["level"]),
                 code_to_school[r["school_code"]], r["casting_time"], r["range_text"],
                 int(r["range_feet"]) if r["range_feet"] else None,
                 _bool(r["comp_v"]), _bool(r["comp_s"]), _bool(r["comp_m"]),
                 r["material"] or None, r["duration_text"],
                 _bool(r["concentration"]), _bool(r["ritual"]), r["body"]))
            slug_to_id[r["slug"]] = cur.fetchone()[0]

        cur.executemany(
            "INSERT INTO spell_class (spell_id, class_id) VALUES (%s, %s)",
            [(slug_to_id[r["spell_slug"]], class_to_id[r["class_name"]])
             for r in classes if r["spell_slug"] in slug_to_id])

        cur.executemany(
            "INSERT INTO spell_damage (spell_id, damage_type) VALUES (%s, %s)",
            [(slug_to_id[r["spell_slug"]], r["damage_type"])
             for r in damage if r["spell_slug"] in slug_to_id])

        conn.commit()
        cur.execute("SELECT count(*) FROM spell")
        n_spell = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM spell_class")
        n_class = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM spell_damage")
        n_dmg = cur.fetchone()[0]

    print(f"loaded {n_spell} spells, {len(class_names)} classes, "
          f"{n_class} spell_class pairs, {n_dmg} spell_damage pairs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
