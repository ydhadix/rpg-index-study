#!/usr/bin/env python3
"""
Real-time data and its challenges.

The read benchmark (bench.py) treats the ruleset as static: load once, then only query.
A Dungeon-Master agent also drives *live, mutating game state* in real time -- spells get
homebrewed, edited, retired mid-session -- and that is where indexes stop being free and
where the storage engine's bookkeeping starts to show. This module measures three
real-time challenges that the static read study cannot:

  B1  write/read trade-off : INSERT throughput as a function of how many indexes exist.
                             Every index a read needs must also be maintained on every
                             write, so write speed falls as read speed rises.
  B2  MVCC churn & bloat   : in-place UPDATEs do not overwrite -- they leave dead row
                             versions behind, so the table bloats and scans slow down
                             until VACUUM reclaims the space. The cost of "real-time edits."
  B3  stale statistics     : after a burst of inserts shifts a column's distribution, the
                             planner's cached statistics are wrong until ANALYZE re-runs,
                             so it mis-estimates selectivity and can pick the wrong plan.

To isolate database work from client/network overhead, the write workloads run
server-side (one set-based statement), not row-by-row from Python.

Outputs:
    results/write_tradeoff.csv / .png   rows/sec vs index count
    results/bloat.csv / .png            table size + scan time: clean / bloated / vacuumed
    results/stale_stats.csv             planner row estimate vs actual, before/after ANALYZE
    results/realtime.md                 human-readable summary of all three

Run:  python src/bench_write.py        (after `make load inflate`)
"""
from __future__ import annotations
import argparse
import csv
import statistics
import time
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from tabulate import tabulate

from db import connect

HERE = Path(__file__).resolve().parent
RESULTS = HERE.parent / "results"

# Cumulative index stages over the spell table (the indexes the read workload wants).
STAGES: list[tuple[str, list[str]]] = [
    ("0: none", []),
    ("1: +level b-tree", ["CREATE INDEX ix_spell_level ON spell (level)"]),
    ("2: +name hash", ["CREATE INDEX ix_spell_name_hash ON spell USING hash (name)"]),
    ("3: +body GIN", ["CREATE INDEX ix_spell_body_gin ON spell USING gin (body_tsv)"]),
    ("4: +school,conc", ["CREATE INDEX ix_spell_school ON spell (school_id)",
                         "CREATE INDEX ix_spell_conc ON spell (concentration)"]),
]


def drop_secondary(cur) -> None:
    cur.execute("""
        SELECT indexname FROM pg_indexes
        WHERE schemaname='public' AND tablename IN ('spell','spell_class','spell_damage')
          AND indexname NOT LIKE '%_pkey'
    """)
    for (idx,) in cur.fetchall():
        cur.execute(f"DROP INDEX IF EXISTS {idx}")


def base_template_id(cur) -> int:
    """Id of a real base spell with the longest body, so GIN maintenance does real work."""
    cur.execute("SELECT id FROM spell WHERE source_spell_id IS NULL "
                "ORDER BY length(body) DESC LIMIT 1")
    return cur.fetchone()[0]


def bulk_insert(cur, base_id: int, n: int, source: str, level_sql: str) -> None:
    """Server-side insert of n rows cloned from one base spell. `level_sql` is a SQL
    expression over the series counter `g` (e.g. '(g %% 10)::smallint' or '9')."""
    cur.execute(f"""
        INSERT INTO spell (slug, name, source, level, school_id, casting_time, range_text,
            range_feet, comp_v, comp_s, comp_m, material, duration_text, concentration,
            ritual, body, source_spell_id)
        SELECT %s || '-' || g, %s || ' ' || g, %s, {level_sql}, b.school_id, b.casting_time,
            b.range_text, b.range_feet, b.comp_v, b.comp_s, b.comp_m, b.material,
            b.duration_text, (g %% 2 = 0), b.ritual, b.body, NULL
        FROM generate_series(1, %s) g
        CROSS JOIN (SELECT * FROM spell WHERE id = %s) b
    """, (source.lower(), source, source, n, base_id))


# --------------------------------------------------------------------------- #
# B1  write / read trade-off
# --------------------------------------------------------------------------- #
def b1_write_tradeoff(n_insert: int = 50000, trials: int = 5) -> list[dict]:
    print(f"\n== B1: INSERT throughput vs index count "
          f"({n_insert:,} rows/stage, median of {trials}) ==")
    out: list[dict] = []
    with connect(autocommit=False) as conn, conn.cursor() as cur:
        base_id = base_template_id(cur)
        applied: list[str] = []
        drop_secondary(cur); conn.commit()
        for label, stmts in STAGES:
            for s in stmts:
                cur.execute(s)
            applied += stmts
            conn.commit()

            # repeat the insert+reset cycle so the reported rate is a median, not a single
            # noisy timing -- index maintenance below the GIN step is small enough that one
            # un-replicated run can order the cheap stages arbitrarily.
            rates: list[float] = []
            for _ in range(trials):
                t0 = time.perf_counter()
                bulk_insert(cur, base_id, n_insert, "WRITE_TEST", "(g %% 10)::smallint")
                conn.commit()
                rates.append(n_insert / (time.perf_counter() - t0))
                cur.execute("DELETE FROM spell WHERE source = 'WRITE_TEST'")
                conn.commit()
            rps = statistics.median(rates)

            out.append({"stage": label, "n_indexes": len(applied), "rows": n_insert,
                        "rows_per_sec": round(rps, 1),
                        "us_per_row": round(1e6 / rps, 2)})
            print(f"  {label:<18} {rps:9.1f} rows/s  ({1e6/rps:7.2f} us/row)")
        drop_secondary(cur); conn.commit()

    base = out[0]["rows_per_sec"]
    for r in out:
        r["slowdown_vs_none"] = round(base / r["rows_per_sec"], 2) if r["rows_per_sec"] else 0
    print(f"  -> all indexes make writes {out[-1]['slowdown_vs_none']:.1f}x slower than none")
    return out


# --------------------------------------------------------------------------- #
# B2  MVCC churn -> bloat -> VACUUM
# --------------------------------------------------------------------------- #
def _scan_ms(cur, trials: int = 3) -> float:
    """Median time of a full-table scan (sum forces every heap tuple to be read)."""
    cur.execute("SELECT sum(level) FROM spell"); cur.fetchall()        # warm
    ts = []
    for _ in range(trials):
        t0 = time.perf_counter()
        cur.execute("SELECT sum(level) FROM spell"); cur.fetchall()
        ts.append((time.perf_counter() - t0) * 1000.0)
    return round(statistics.median(ts), 3)


def _table_mb(cur) -> float:
    cur.execute("SELECT pg_total_relation_size('spell')")
    return round(cur.fetchone()[0] / 1024 / 1024, 2)


def b2_bloat(passes: int = 6) -> list[dict]:
    print(f"\n== B2: MVCC churn -> bloat -> VACUUM ({passes} full-table UPDATEs) ==")
    out: list[dict] = []
    with connect(autocommit=True) as conn, conn.cursor() as cur:
        # realistic indexed table; turn off autovacuum so we can see the bloat we create
        drop_secondary(cur)
        cur.execute("CREATE INDEX ix_spell_conc ON spell (concentration)")
        cur.execute("ALTER TABLE spell SET (autovacuum_enabled = false)")
        cur.execute("VACUUM (FULL, ANALYZE) spell")

        out.append({"state": "clean (after VACUUM FULL)", "size_mb": _table_mb(cur),
                    "scan_ms": _scan_ms(cur)})
        print(f"  clean    size {out[-1]['size_mb']:7.2f} MB   scan {out[-1]['scan_ms']:7.3f} ms")

        # churn: each UPDATE on an indexed column rewrites every row -> dead tuples pile up
        for i in range(passes):
            cur.execute("UPDATE spell SET concentration = NOT concentration")
        cur.execute("ANALYZE spell")
        cur.execute("SELECT n_dead_tup FROM pg_stat_user_tables WHERE relname='spell'")
        dead = cur.fetchone()[0]
        out.append({"state": f"bloated ({passes} updates)", "size_mb": _table_mb(cur),
                    "scan_ms": _scan_ms(cur), "dead_tuples": dead})
        print(f"  bloated  size {out[-1]['size_mb']:7.2f} MB   scan {out[-1]['scan_ms']:7.3f} ms"
              f"   dead_tuples {dead:,}")

        cur.execute("VACUUM (FULL, ANALYZE) spell")
        out.append({"state": "after VACUUM FULL", "size_mb": _table_mb(cur),
                    "scan_ms": _scan_ms(cur)})
        print(f"  vacuumed size {out[-1]['size_mb']:7.2f} MB   scan {out[-1]['scan_ms']:7.3f} ms")

        cur.execute("ALTER TABLE spell SET (autovacuum_enabled = true)")
        drop_secondary(cur); cur.execute("ANALYZE spell")
    return out


# --------------------------------------------------------------------------- #
# B3  stale planner statistics
# --------------------------------------------------------------------------- #
def _scan_node(plan: dict) -> dict:
    """The scan node carrying the WHERE-matched rows (largest-actual-rows 'Scan' node)."""
    best = plan
    found = {"n": None}

    def walk(node):
        if "Scan" in node["Node Type"]:
            if found["n"] is None or node.get("Actual Rows", 0) >= found["n"].get("Actual Rows", 0):
                found["n"] = node
        for c in node.get("Plans", []):
            walk(c)

    walk(plan)
    return found["n"] or best


def b3_stale_stats(n_burst: int = 60000) -> list[dict]:
    print(f"\n== B3: stale statistics after a {n_burst:,}-row burst (all level 9) ==")
    Q = "SELECT id FROM spell WHERE level >= 8"
    out: list[dict] = []
    with connect(autocommit=False) as conn, conn.cursor() as cur:
        drop_secondary(cur)
        cur.execute("CREATE INDEX ix_spell_level ON spell (level)")
        cur.execute("ANALYZE spell")
        conn.commit()
        base_id = base_template_id(cur)

        def snapshot(when: str) -> None:
            cur.execute(f"EXPLAIN (ANALYZE, FORMAT JSON) {Q}")
            node = _scan_node(cur.fetchone()[0][0]["Plan"])
            est, act = node.get("Plan Rows", 0), node.get("Actual Rows", 0)
            row = {"when": when, "plan_node": node["Node Type"],
                   "est_rows": est, "actual_rows": act,
                   "est_error_x": round(act / max(est, 1), 1)}
            out.append(row)
            print(f"  {when:<28} {row['plan_node']:<18} est={est:>7} actual={act:>7}"
                  f"  off by {row['est_error_x']}x")

        bulk_insert(cur, base_id, n_burst, "STALE_TEST", "9")   # all level 9, no ANALYZE
        conn.commit()
        snapshot("after burst, stale stats")
        cur.execute("ANALYZE spell")
        snapshot("after ANALYZE (fresh stats)")

        cur.execute("DELETE FROM spell WHERE source = 'STALE_TEST'")
        drop_secondary(cur)
        cur.execute("ANALYZE spell")
        conn.commit()
    return out


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #
def write_report(b1, b2, b3) -> None:
    t1 = tabulate([[r["stage"], r["n_indexes"], r["rows_per_sec"], r["us_per_row"],
                    f'{r["slowdown_vs_none"]}x'] for r in b1],
                  headers=["index stage", "# idx", "rows/sec", "us/row", "slowdown"],
                  tablefmt="github")
    t2 = tabulate([[r["state"], r["size_mb"], r["scan_ms"], r.get("dead_tuples", "")]
                   for r in b2],
                  headers=["table state", "size_MB", "full-scan_ms", "dead_tuples"],
                  tablefmt="github")
    t3 = tabulate([[r["when"], r["plan_node"], r["est_rows"], r["actual_rows"],
                    f'{r["est_error_x"]}x'] for r in b3],
                  headers=["statistics state", "plan", "est rows", "actual rows", "est error"],
                  tablefmt="github")
    bloat_x = round(b2[1]["size_mb"] / b2[0]["size_mb"], 1) if b2[0]["size_mb"] else 0
    dead = b2[1].get("dead_tuples", 0)
    md = f"""# Real-time data and its challenges

The read benchmark studies a *static* ruleset. A live DM agent also mutates game state in
real time, where the same indexes that accelerate reads must be maintained on every write
and where PostgreSQL's MVCC storage leaves work behind. These three measurements quantify
challenges the static study cannot show. Write workloads run server-side so the numbers
reflect database work, not client round-trips.

## B1 - The write/read trade-off: every read index is a write tax

Bulk-inserting {b1[0]['rows']:,} rows under a growing set of indexes. Each index must be
updated on every insert, so throughput falls as indexes are added; the GIN full-text index
is by far the most expensive to maintain.

{t1}

## B2 - MVCC churn and bloat: the cost of real-time edits

PostgreSQL never overwrites a row in place: an `UPDATE` writes a new version and tombstones
the old one. Under continuous edits the table bloats with dead tuples and full scans slow
down, until `VACUUM` reclaims the space. (Autovacuum was disabled here to make the effect
visible; in production it runs continuously precisely to prevent this.)

{t2}

The {dead:,} dead tuples grew the table ~{bloat_x}x and slowed a full scan accordingly;
`VACUUM FULL` returned it to the clean baseline.

## B3 - Stale statistics: real-time drift fools the optimizer

A burst of inserts shifts the `level` distribution. Until `ANALYZE` re-runs, the planner's
cached statistics are stale, so it badly mis-estimates how many rows match `level >= 8` --
which is exactly the input it uses to choose a plan.

{t3}

## Design conclusion

Reads and writes pull in opposite directions, so a real-time system should index its two
data populations **asymmetrically**: index the large, near-static **ruleset** aggressively
(reads dominate; writes are rare bulk loads), but index fast-mutating **game state**
conservatively, and schedule `ANALYZE`/`VACUUM` to keep statistics fresh and bloat bounded.
"""
    (RESULTS / "realtime.md").write_text(md)


def chart_write_tradeoff(b1: list[dict]) -> None:
    """Write throughput vs index count. A line chart, so the y-axis is zoomed to the data
    range (not anchored at 0) -- the interesting variation is a few hundred rows/sec and
    would be invisible squashed against the top of a 0-based axis."""
    fig, ax = plt.subplots(figsize=(7, 4.5))
    labels = [r["stage"] for r in b1]
    rps = [r["rows_per_sec"] for r in b1]
    ax.plot(range(len(labels)), rps, "o-", color="#c0504d")
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, rotation=20, ha="right", fontsize=8)
    ax.set_ylabel("INSERT throughput (rows/sec)")

    lo, hi = min(rps), max(rps)
    pad = (hi - lo) * 0.25 if hi > lo else hi * 0.02
    ax.set_ylim(lo - pad, hi + pad * 1.8)        # extra top headroom for the value labels
    ax.margins(x=0.08)

    ax.set_title("The write tax of indexing: throughput falls as indexes are added")
    for i, v in enumerate(rps):
        ax.text(i, v, f"{v:,.0f}", ha="center", va="bottom", fontsize=8)
    fig.tight_layout(); fig.savefig(RESULTS / "write_tradeoff.png", dpi=120); plt.close(fig)


def chart_bloat(b2: list[dict]) -> None:
    """Table size (bars) + full-scan time (line) across clean / bloated / vacuumed."""
    fig, ax = plt.subplots(figsize=(7, 4.5))
    states = [r["state"] for r in b2]
    sizes = [r["size_mb"] for r in b2]
    scans = [r["scan_ms"] for r in b2]
    x = range(len(states))
    ax.bar(x, sizes, color="#4f81bd", label="table size (MB)")
    ax.set_xticks(list(x)); ax.set_xticklabels(states, fontsize=8)
    ax.set_ylabel("table size (MB)")
    ax2 = ax.twinx()
    ax2.plot(x, scans, "o-", color="#c0504d", label="full-scan ms")
    ax2.set_ylabel("full-scan time (ms)", color="#c0504d")
    ax.set_title("MVCC bloat from real-time edits, reclaimed by VACUUM")
    fig.tight_layout(); fig.savefig(RESULTS / "bloat.png", dpi=120); plt.close(fig)


def _read_csv(name: str) -> list[dict]:
    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return v
    with open(RESULTS / name) as fh:
        return [{k: num(v) for k, v in row.items()} for row in csv.DictReader(fh)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--charts-only", action="store_true",
                    help="rebuild charts from the existing results CSVs (no DB work)")
    args = ap.parse_args()
    RESULTS.mkdir(exist_ok=True)

    if args.charts_only:
        chart_write_tradeoff(_read_csv("write_tradeoff.csv"))
        chart_bloat(_read_csv("bloat.csv"))
        print("rebuilt write_tradeoff.png, bloat.png from existing CSVs")
        return 0

    b1 = b1_write_tradeoff()
    b2 = b2_bloat()
    b3 = b3_stale_stats()

    with open(RESULTS / "write_tradeoff.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(b1[0].keys())); w.writeheader(); w.writerows(b1)
    with open(RESULTS / "bloat.csv", "w", newline="") as fh:
        cols = ["state", "size_mb", "scan_ms", "dead_tuples"]
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader(); w.writerows(b2)
    with open(RESULTS / "stale_stats.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(b3[0].keys())); w.writeheader(); w.writerows(b3)

    chart_write_tradeoff(b1)
    chart_bloat(b2)
    write_report(b1, b2, b3)
    print(f"\nwrote {RESULTS/'realtime.md'}, write_tradeoff.{{csv,png}}, bloat.{{csv,png}}, "
          f"stale_stats.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())