#!/usr/bin/env python3
"""
Benchmark harness: run the workload under every (cumulative) index treatment, record
EXPLAIN (ANALYZE, BUFFERS) metrics, and run the agent-style throughput test.

Outputs:
    results/results.csv      one row per (treatment, query): median ms, plan node, buffers
    results/throughput.csv   point-lookup queries/sec, baseline vs indexed
    results/summary.md       human-readable pivot tables (the headline artifact)
    results/latency.png      per-query baseline vs best median latency
    results/throughput.png   point-lookup queries/sec baseline vs indexed

Run:  python src/bench.py            (full run)
      python src/bench.py --report-only   (rebuild charts/summary from existing CSVs)
"""
from __future__ import annotations
import argparse
import csv
import json
import re
import statistics
import time
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from tabulate import tabulate

from db import connect
from workload import WORKLOAD, THROUGHPUT_SQL

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
RESULTS = REPO / "results"
TREATMENTS_SQL = REPO / "sql" / "treatments.sql"

TRIALS = 5            # timed trials per (treatment, query); median is reported
THROUGHPUT_N = 1000   # point lookups fired back-to-back in the throughput test


# --------------------------------------------------------------------------- #
# treatment parsing
# --------------------------------------------------------------------------- #
def parse_treatments() -> list[tuple[str, str, list[str]]]:
    """Return [(name, description, [sql, ...]), ...] including a leading T0 baseline."""
    treatments: list[tuple[str, str, list[str]]] = [("T0_baseline", "primary keys only", [])]
    name = desc = None
    stmts: list[str] = []
    buf = ""
    for line in TREATMENTS_SQL.read_text().splitlines():
        m = re.match(r"--\s*@treatment\s+(\S+)\s*:\s*(.*)", line)
        if m:
            if name:
                treatments.append((name, desc, stmts))
            name, desc, stmts, buf = m.group(1), m.group(2).strip(), [], ""
            continue
        if name is None or line.strip().startswith("--") or not line.strip():
            continue
        buf += " " + line
        if ";" in buf:
            for s in buf.split(";"):
                if s.strip():
                    stmts.append(s.strip())
            buf = ""
    if name:
        treatments.append((name, desc, stmts))
    return treatments


def drop_secondary_indexes(cur) -> None:
    """Remove every non-primary-key index so T0 is a true baseline."""
    cur.execute("""
        SELECT indexname FROM pg_indexes
        WHERE schemaname = 'public'
          AND tablename IN ('spell', 'spell_class', 'spell_damage')
          AND indexname NOT LIKE '%_pkey'
    """)
    for (idx,) in cur.fetchall():
        cur.execute(f"DROP INDEX IF EXISTS {idx}")


# --------------------------------------------------------------------------- #
# EXPLAIN parsing
# --------------------------------------------------------------------------- #
def explain(cur, sql: str, params: tuple) -> dict:
    cur.execute(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}", params)
    raw = cur.fetchone()[0]
    plan_doc = json.loads(raw) if isinstance(raw, str) else raw
    return plan_doc[0]


def summarize_plan(doc: dict) -> dict:
    root = doc["Plan"]
    hit = read = 0

    def walk(node):
        nonlocal hit, read
        hit += node.get("Shared Hit Blocks", 0)
        read += node.get("Shared Read Blocks", 0)
        for child in node.get("Plans", []):
            walk(child)

    walk(root)
    return {
        "exec_ms": doc["Execution Time"],
        "plan_ms": doc["Planning Time"],
        "node": describe_node(_characteristic_node(root)),
        "shared_blocks": hit + read,
    }


WRAPPERS = {"Limit", "Aggregate", "Gather", "Gather Merge", "Sort",
            "Hash", "Hash Join", "Nested Loop", "Merge Join", "Result",
            "Incremental Sort", "Materialize", "Memoize"}


def _characteristic_node(root: dict) -> dict:
    """The costliest access-method node — the one that characterizes the plan's work
    (skip join/sort wrappers and bitmap sub-nodes; a Bitmap Heap Scan counts as a unit)."""
    sub = {"BitmapAnd", "BitmapOr", "Bitmap Index Scan"}
    best = None

    def walk(node):
        nonlocal best
        nt = node["Node Type"]
        if nt not in WRAPPERS and nt not in sub:
            if best is None or node.get("Total Cost", 0) > best.get("Total Cost", 0):
                best = node
        if nt == "Bitmap Heap Scan":
            return                       # its bitmap children are handled by describe_node
        for child in node.get("Plans", []):
            walk(child)

    walk(root)
    return best or root


def describe_node(node: dict) -> str:
    """A concise, index-naming label, e.g. 'Bitmap Heap Scan (ix_spell_level)'."""
    nt = node["Node Type"]
    if nt in ("Index Scan", "Index Only Scan", "Bitmap Index Scan"):
        return f"{nt} ({node.get('Index Name', '?')})"
    if nt == "Bitmap Heap Scan":
        for child in node.get("Plans", []):
            ct = child["Node Type"]
            if ct in ("BitmapAnd", "BitmapOr"):
                idxs = "+".join(
                    g.get("Index Name", "?") for g in child.get("Plans", [])
                    if g["Node Type"] == "Bitmap Index Scan")
                return f"{ct} ({idxs})"
            if ct == "Bitmap Index Scan":
                return f"Bitmap Heap Scan ({child.get('Index Name', '?')})"
        return nt
    return nt


# --------------------------------------------------------------------------- #
# benchmark run
# --------------------------------------------------------------------------- #
def run_matrix(cur) -> list[dict]:
    rows: list[dict] = []
    treatments = parse_treatments()
    drop_secondary_indexes(cur)

    for tname, tdesc, stmts in treatments:
        for s in stmts:
            cur.execute(s)
        if stmts:
            cur.execute("ANALYZE spell")
        print(f"\n== {tname} ({tdesc}) ==")
        for q in WORKLOAD:
            explain(cur, q.sql, q.params)              # warm-up (cache + plan)
            samples = [explain(cur, q.sql, q.params) for _ in range(TRIALS)]
            best = min(samples, key=lambda d: d["Execution Time"])  # least-noisy run
            info = summarize_plan(best)
            med = statistics.median(d["Execution Time"] for d in samples)
            rows.append({
                "treatment": tname, "query": q.id, "label": q.label,
                "median_ms": round(med, 3), "plan_node": info["node"],
                "shared_blocks": info["shared_blocks"],
            })
            print(f"  {q.id:3} {med:8.3f} ms  {info['node']:<18} "
                  f"({info['shared_blocks']} buffers)  {q.label}")
    return rows


def run_throughput(cur) -> list[dict]:
    """Fire THROUGHPUT_N point lookups back-to-back, baseline vs hash-indexed."""
    cur.execute("SELECT name FROM spell ORDER BY random() LIMIT %s", (THROUGHPUT_N,))
    keys = [r[0] for r in cur.fetchall()]

    def measure() -> tuple[float, float]:
        # warm + prepared (psycopg auto-prepares after a few executions)
        for k in keys[:10]:
            cur.execute(THROUGHPUT_SQL, (k,)); cur.fetchall()
        t0 = time.perf_counter()
        for k in keys:
            cur.execute(THROUGHPUT_SQL, (k,)); cur.fetchall()
        elapsed = time.perf_counter() - t0
        return elapsed, len(keys) / elapsed

    drop_secondary_indexes(cur)
    cur.execute("ANALYZE spell")
    base_elapsed, base_qps = measure()

    cur.execute("CREATE INDEX IF NOT EXISTS ix_spell_name_hash ON spell USING hash (name)")
    cur.execute("ANALYZE spell")
    idx_elapsed, idx_qps = measure()

    out = [
        {"state": "baseline (seq scan)", "lookups": THROUGHPUT_N,
         "total_s": round(base_elapsed, 4), "avg_ms": round(base_elapsed / THROUGHPUT_N * 1000, 4),
         "qps": round(base_qps, 1)},
        {"state": "hash index", "lookups": THROUGHPUT_N,
         "total_s": round(idx_elapsed, 4), "avg_ms": round(idx_elapsed / THROUGHPUT_N * 1000, 4),
         "qps": round(idx_qps, 1)},
    ]
    print(f"\n== throughput ({THROUGHPUT_N} point lookups) ==")
    print(f"  baseline : {base_qps:9.1f} q/s  ({base_elapsed/THROUGHPUT_N*1000:.4f} ms/lookup)")
    print(f"  hash idx : {idx_qps:9.1f} q/s  ({idx_elapsed/THROUGHPUT_N*1000:.4f} ms/lookup)")
    print(f"  speedup  : {idx_qps/base_qps:.1f}x")
    return out


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #
def write_csv(path: Path, rows: list[dict]) -> None:
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def build_report(matrix: list[dict], throughput: list[dict]) -> None:
    treatments = []
    for r in matrix:
        if r["treatment"] not in treatments:
            treatments.append(r["treatment"])
    qids = [q.id for q in WORKLOAD]
    by = {(r["treatment"], r["query"]): r for r in matrix}

    # latency pivot (median ms)
    lat_header = ["query"] + treatments
    lat_rows = []
    for q in WORKLOAD:
        row = [f"{q.id} {q.label}"]
        for t in treatments:
            row.append(f'{by[(t, q.id)]["median_ms"]:.2f}')
        lat_rows.append(row)

    # plan-node pivot (shows seq scan -> index/bitmap/gin transition)
    node_rows = []
    for q in WORKLOAD:
        row = [q.id]
        for t in treatments:
            row.append(by[(t, q.id)]["plan_node"])
        node_rows.append(row)

    lat_table = tabulate(lat_rows, headers=lat_header, tablefmt="github")
    node_table = tabulate(node_rows, headers=["query"] + treatments, tablefmt="github")
    tput_table = tabulate(
        [[r["state"], r["lookups"], r["total_s"], r["avg_ms"], r["qps"]] for r in throughput],
        headers=["state", "lookups", "total_s", "avg_ms", "queries/sec"], tablefmt="github")

    speedup = throughput[1]["qps"] / throughput[0]["qps"] if throughput[0]["qps"] else 0
    md = f"""# Benchmark results

Engine: PostgreSQL 16 (Docker). Parallelism disabled for stable plan comparison.
Each cell is the **median of {TRIALS} timed `EXPLAIN (ANALYZE, BUFFERS)` runs** (ms).
Treatments are applied cumulatively (T0 = primary keys only).

## Median execution time (ms) by treatment

{lat_table}

## Query plan access method by treatment

{node_table}

## Agent-style throughput: {THROUGHPUT_N} point lookups (the hot path)

{tput_table}

**Indexing the hot point-lookup path gave a {speedup:.1f}x throughput improvement.**
"""
    (RESULTS / "summary.md").write_text(md)

    # ---- charts ----
    # 1) per-query baseline (T0) vs best treatment latency
    base_t = treatments[0]
    qlabels, base_ms, best_ms = [], [], []
    for q in WORKLOAD:
        qlabels.append(q.id)
        base_ms.append(by[(base_t, q.id)]["median_ms"])
        best_ms.append(min(by[(t, q.id)]["median_ms"] for t in treatments))
    x = range(len(qlabels))
    fig, ax = plt.subplots(figsize=(9, 4.5))
    ax.bar([i - 0.2 for i in x], base_ms, 0.4, label=f"{base_t} (no index)", color="#c0504d")
    ax.bar([i + 0.2 for i in x], best_ms, 0.4, label="best index", color="#4f81bd")
    ax.set_yscale("log")
    ax.set_xticks(list(x)); ax.set_xticklabels(qlabels)
    ax.set_ylabel("median execution time (ms, log scale)")
    ax.set_title("Query latency: no index vs best index (100K spells)")
    ax.legend()
    fig.tight_layout(); fig.savefig(RESULTS / "latency.png", dpi=120); plt.close(fig)

    # 2) throughput bar
    fig, ax = plt.subplots(figsize=(5, 4.5))
    states = [r["state"] for r in throughput]
    qps = [r["qps"] for r in throughput]
    ax.bar(states, qps, color=["#c0504d", "#4f81bd"])
    ax.set_ylabel("point lookups / sec")
    ax.set_title(f"Hot-path throughput ({THROUGHPUT_N} lookups)")
    for i, v in enumerate(qps):
        ax.text(i, v, f"{v:,.0f}", ha="center", va="bottom")
    fig.tight_layout(); fig.savefig(RESULTS / "throughput.png", dpi=120); plt.close(fig)

    print(f"\nwrote {RESULTS/'summary.md'}, latency.png, throughput.png")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-only", action="store_true",
                    help="rebuild summary/charts from existing results CSVs")
    args = ap.parse_args()
    RESULTS.mkdir(exist_ok=True)

    if args.report_only:
        matrix = [dict(r, median_ms=float(r["median_ms"]),
                       shared_blocks=int(r["shared_blocks"]))
                  for r in csv.DictReader(open(RESULTS / "results.csv"))]
        throughput = [dict(r, lookups=int(r["lookups"]), total_s=float(r["total_s"]),
                           avg_ms=float(r["avg_ms"]), qps=float(r["qps"]))
                      for r in csv.DictReader(open(RESULTS / "throughput.csv"))]
        build_report(matrix, throughput)
        return 0

    with connect(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SET max_parallel_workers_per_gather = 0")
        cur.execute("SELECT count(*) FROM spell")
        print(f"benchmarking against {cur.fetchone()[0]:,} spells")
        matrix = run_matrix(cur)
        throughput = run_throughput(cur)

    write_csv(RESULTS / "results.csv", matrix)
    write_csv(RESULTS / "throughput.csv", throughput)
    build_report(matrix, throughput)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
