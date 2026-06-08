#!/usr/bin/env python3
"""
ETL: 5etools spell JSON  ->  normalized CSVs.

Reads every data/spells/spells-*.json from the 5etools-src checkout, deduplicates
reprints to one canonical row per spell name (source-priority order), flattens the
free-text body, joins the class<->spell mapping from the generated lookup, and writes:

    data/spells.csv         one row per canonical spell
    data/spell_classes.csv  (spell_slug, class_name) M:N pairs
    data/spell_damage.csv   (spell_slug, damage_type) array-dimension pairs

Run:  python src/etl.py   (or `make etl`)
"""
from __future__ import annotations
import csv
import glob
import json
import os
import re
import sys
from pathlib import Path

# --- locate the 5etools data (sibling checkout by default; override with env) ---
HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DATA_5ET = Path(os.environ.get("FIVET_DATA", REPO.parent / "5etools-src" / "data"))
SPELLS_DIR = DATA_5ET / "spells"
LOOKUP = DATA_5ET / "generated" / "gendata-spell-source-lookup.json"
OUT = REPO / "data"

SCHOOLS = {
    "A": "Abjuration", "C": "Conjuration", "D": "Divination", "E": "Enchantment",
    "V": "Evocation", "I": "Illusion", "N": "Necromancy", "T": "Transmutation",
}

# When the same spell name appears in multiple books, keep the highest-priority
# printing as the canonical row. Anything not listed sorts last (priority 99).
SOURCE_PRIORITY = {"XPHB": 0, "PHB": 1, "TCE": 2, "XGE": 3}


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


# 5etools markup uses {@tag display|args}. Keep the human-readable display text.
TAG_RE = re.compile(r"\{@\w+\s*([^|}]*)(?:\|[^}]*)?\}")


def strip_tags(text: str) -> str:
    prev = None
    while prev != text:                      # tags can nest; loop to fixpoint
        prev = text
        text = TAG_RE.sub(lambda m: m.group(1).strip(), text)
    return re.sub(r"\s+", " ", text).strip()


def flatten_entries(entries) -> str:
    """Recursively pull all prose out of a 5etools `entries` tree into one string."""
    parts: list[str] = []

    def walk(node):
        if isinstance(node, str):
            parts.append(node)
        elif isinstance(node, list):
            for x in node:
                walk(x)
        elif isinstance(node, dict):
            if node.get("name"):
                parts.append(str(node["name"]))
            for key in ("entries", "items", "entry"):
                if key in node:
                    walk(node[key])
            # tables: flatten headers + rows so keywords remain searchable
            if "rows" in node:
                for row in node["rows"]:
                    walk(row)
            if "colLabels" in node:
                walk(node["colLabels"])

    walk(entries)
    return strip_tags(" ".join(p for p in parts if isinstance(p, str)))


def casting_time(time) -> str:
    if not time:
        return ""
    t = time[0]
    n, unit = t.get("number", ""), t.get("unit", "")
    cond = t.get("condition")
    s = f"{n} {unit}".strip()
    return f"{s}, {cond}" if cond else s


def range_fields(rng) -> tuple[str, int | None]:
    """Return (human text, range in feet or None)."""
    if not rng:
        return "", None
    rtype = rng.get("type", "")
    dist = rng.get("distance") or {}
    dtype = dist.get("type")
    amount = dist.get("amount")
    feet = None
    if dtype == "feet":
        feet = amount
    elif dtype == "miles" and amount is not None:
        feet = amount * 5280
    elif dtype in ("self", "touch"):
        feet = 0
    text = dtype if dtype in ("self", "touch", "sight", "unlimited") else \
        (f"{amount} {dtype}" if amount is not None else dtype or rtype)
    if rtype not in ("point", None) and dtype not in ("self", "touch"):
        text = f"{text} ({rtype})"
    return (text or rtype or "").strip(), feet


def duration_fields(duration) -> tuple[str, bool]:
    if not duration:
        return "", False
    d = duration[0]
    conc = any(x.get("concentration") for x in duration)
    dtype = d.get("type")
    if dtype == "timed":
        dur = d.get("duration", {})
        text = f"{dur.get('amount','')} {dur.get('type','')}".strip()
    else:
        text = dtype or ""
    if conc:
        text = f"concentration, up to {text}" if text else "concentration"
    return text, conc


def material_text(components) -> str:
    m = components.get("m")
    if isinstance(m, str):
        return m
    if isinstance(m, dict):
        return m.get("text", "")
    return ""


def load_class_lookup() -> dict[str, set[str]]:
    """name(lowercased) -> set of base class names (unioned across all sources)."""
    raw = json.load(open(LOOKUP))
    out: dict[str, set[str]] = {}
    for _src, spells in raw.items():
        for name_l, entry in spells.items():
            classes = out.setdefault(name_l, set())
            for _book, cls in entry.get("class", {}).items():
                classes.update(cls.keys())
    return out


def main() -> int:
    if not SPELLS_DIR.exists():
        sys.exit(f"5etools spell data not found at {SPELLS_DIR} (set FIVET_DATA env)")

    class_lookup = load_class_lookup()

    # Gather every record, then keep the best-priority printing per name.
    best: dict[str, dict] = {}
    raw_count = 0
    for f in sorted(glob.glob(str(SPELLS_DIR / "spells-*.json"))):
        for s in json.load(open(f)).get("spell", []):
            raw_count += 1
            name = s["name"]
            prio = SOURCE_PRIORITY.get(s.get("source", ""), 99)
            cur = best.get(name)
            if cur is None or prio < cur[0]:
                best[name] = (prio, s)

    spells = [v[1] for v in best.values()]
    spells.sort(key=lambda s: (s.get("level", 0), s["name"]))

    OUT.mkdir(exist_ok=True)
    spell_rows, class_rows, dmg_rows = [], [], []
    for s in spells:
        name = s["name"]
        slug = slugify(name)
        comps = s.get("components", {}) or {}
        rng_text, rng_feet = range_fields(s.get("range"))
        dur_text, conc = duration_fields(s.get("duration"))
        spell_rows.append({
            "slug": slug,
            "name": name,
            "source": s.get("source", ""),
            "level": s.get("level", 0),
            "school_code": s.get("school", ""),
            "casting_time": casting_time(s.get("time")),
            "range_text": rng_text,
            "range_feet": rng_feet if rng_feet is not None else "",
            "comp_v": bool(comps.get("v")),
            "comp_s": bool(comps.get("s")),
            "comp_m": bool(comps.get("m")),
            "material": material_text(comps),
            "duration_text": dur_text,
            "concentration": conc,
            "ritual": bool((s.get("meta") or {}).get("ritual")),
            "body": flatten_entries(s.get("entries", [])),
        })
        for cname in sorted(class_lookup.get(name.lower(), [])):
            class_rows.append({"spell_slug": slug, "class_name": cname})
        for dmg in sorted(set(s.get("damageInflict", []))):
            dmg_rows.append({"spell_slug": slug, "damage_type": dmg})

    _write_csv(OUT / "spells.csv", spell_rows)
    _write_csv(OUT / "spell_classes.csv", class_rows)
    _write_csv(OUT / "spell_damage.csv", dmg_rows)

    schools = sorted({r["school_code"] for r in spell_rows})
    levels = sorted({r["level"] for r in spell_rows})
    print(f"read {raw_count} records -> {len(spell_rows)} canonical spells "
          f"(deduped {raw_count - len(spell_rows)} reprints)")
    print(f"  schools: {len(schools)} {schools}")
    print(f"  levels:  {levels}")
    print(f"  spell_classes pairs: {len(class_rows)}  "
          f"({len({r['spell_slug'] for r in class_rows})} spells have >=1 class)")
    print(f"  spell_damage pairs:  {len(dmg_rows)}")
    print(f"wrote spells.csv, spell_classes.csv, spell_damage.csv to {OUT}")
    return 0


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("")
        return
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    raise SystemExit(main())
