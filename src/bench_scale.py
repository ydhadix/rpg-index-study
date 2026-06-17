#!/usr/bin/env python3
"""
Scaling study: how does the *benefit* of an index change as the ruleset grows?

For a subset of the workload, measure median latency at several table sizes, both with
NO secondary index (T0) and with the winning index. The expected, and observed, finding
is asymptotic: a sequential scan is O(n) so its latency climbs roughly linearly with the
row count, while a B-tree / hash / GIN lookup is ~O(log n) and stays almost flat -- so the
gap *widens* with scale. That is the quantitative justification for indexing "the whole
encoded ruleset" rather than the few-hundred-row real dataset.

Reuses inflate.py (via subprocess) to resize the table and the EXPLAIN helpers in bench.py.

Run:  python src/bench_scale.py                       (10K, 100K, 1M)
      python src/bench_scale.py --sizes 10000,100000,250000
"""
from __future__ import annotations
import argparse
import csv
import statistics
import subprocess
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from db import connect
from bench import explain, summarize_plan, drop_secondary_indexes
from workload import WORKLOAD

HERE = Path(__file__).resolve().parent
RESULTS = HERE.parent / "results"
TRIALS = 5

# The subset of queries whose scaling we chart, with the index that makes each fast.
SUBSET = {
    "Q1": "point lookup (hash on name)",
    "Q2": "range scan (b-tree on level)",
    "Q4": "full-text search (GIN on body)",
    "Q6": "Top-N browse (b-tree on level)",
}
# Indexes built for the "indexed" measurement (covers every query in SUBSET).
INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS ix_spell_level ON spell (level)",
    "CREATE INDEX IF NOT EXISTS ix_spell_name_hash ON spell USING hash (name)",
    "CREATE INDEX IF NOT EXISTS ix_spell_body_gin ON spell USING gin (body_tsv)",
]
INDEX_SIZES = ["ix_spell_level", "ix_spell_name_hash", "ix_spell_body_gin"]


def median_ms(cur, q) -> tuple[float, str]:
    explain(cur, q.sql, q.params)                                  # warm-up
    samples = [explain(cur, q.sql, q.params) for _ in range(TRIALS)]
    best = min(samples, key=lambda d: d["Execution Time"])
    med = statistics.median(d["Execution Time"] for d in samples)
    return round(med, 3), summarize_plan(best)["node"]


def inflate(rows: int) -> None:
    print(f"\n=== resizing table to {rows:,} rows ===")
    subprocess.run([sys.executable, str(HERE / "inflate.py"), "--rows", str(rows)],
                   check=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="10000,100000,1000000",
                    help="comma-separated target row counts")
    args = ap.parse_args()
    sizes = [int(s) for s in args.sizes.split(",")]
    RESULTS.mkdir(exist_ok=True)

    queries = [q for q in WORKLOAD if q.id in SUBSET]
    rows_out: list[dict] = []
    size_out: list[dict] = []

    for n in sizes:
        inflate(n)
        with connect(autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("SET max_parallel_workers_per_gather = 0")
            cur.execute("SELECT count(*) FROM spell")
            actual = cur.fetchone()[0]

            # --- no index (T0) ---
            drop_secondary_indexes(cur)
            cur.execute("ANALYZE spell")
            print(f"-- {actual:,} rows, NO index --")
            for q in queries:
                med, node = median_ms(cur, q)
                rows_out.append({"rows": actual, "query": q.id, "state": "no_index",
                                 "median_ms": med, "plan_node": node})
                print(f"   {q.id} {med:9.3f} ms  {node}")

            # --- indexed ---
            for s in INDEX_SQL:
                cur.execute(s)
            cur.execute("ANALYZE spell")
            print(f"-- {actual:,} rows, INDEXED --")
            for q in queries:
                med, node = median_ms(cur, q)
                rows_out.append({"rows": actual, "query": q.id, "state": "indexed",
                                 "median_ms": med, "plan_node": node})
                print(f"   {q.id} {med:9.3f} ms  {node}")

            for ix in INDEX_SIZES:
                cur.execute("SELECT pg_relation_size(%s)", (ix,))
                size_out.append({"rows": actual, "index": ix,
                                 "size_mb": round(cur.fetchone()[0] / 1024 / 1024, 2)})

    # ---- write CSVs ----
    with open(RESULTS / "scaling.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["rows", "query", "state", "median_ms", "plan_node"])
        w.writeheader(); w.writerows(rows_out)
    with open(RESULTS / "scaling_index_size.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["rows", "index", "size_mb"])
        w.writeheader(); w.writerows(size_out)

    # ---- chart: 2x2, per query, no-index vs indexed across row counts (log-log) ----
    recorded = sorted({r["rows"] for r in rows_out})
    fig, axes = plt.subplots(2, 2, figsize=(11, 8))
    for ax, (qid, title) in zip(axes.flat, SUBSET.items()):
        no = [next(r["median_ms"] for r in rows_out
                   if r["query"] == qid and r["state"] == "no_index" and r["rows"] == rr)
              for rr in recorded]
        idx = [next(r["median_ms"] for r in rows_out
                    if r["query"] == qid and r["state"] == "indexed" and r["rows"] == rr)
               for rr in recorded]
        ax.plot(recorded, no, "o--", color="#c0504d", label="no index (seq scan)")
        ax.plot(recorded, idx, "o-", color="#4f81bd", label="indexed")
        ax.set_xscale("log"); ax.set_yscale("log")
        ax.set_title(f"{qid}: {title}", fontsize=10)
        ax.set_xlabel("rows (log)"); ax.set_ylabel("median ms (log)")
        ax.grid(True, which="both", ls=":", alpha=0.4)
        ax.legend(fontsize=8)
    fig.suptitle("Index benefit vs table size: seq scan grows ~O(n), index stays ~O(log n)")
    fig.tight_layout(); fig.savefig(RESULTS / "scaling.png", dpi=120); plt.close(fig)

    print(f"\nwrote {RESULTS/'scaling.csv'}, scaling_index_size.csv, scaling.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())