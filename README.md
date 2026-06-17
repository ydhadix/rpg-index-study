# Testing PostgreSQL Indexing Strategies for Efficient Lookup of Tabletop RPG Rules

## Intermediate Update

In response to the prototype review, the project scope has been expanded to include real-time data and its challenges.

> Tabletop RPG play depends on lookup of specific rules - spells, items, monster abilities, etc. - from a mass of thousands of individual options, in a limited amount of time.  **Additionally, the Dungeon Master is responsible for maintaining the current state of the world, which affects which options are available at a given time.**
> Given a normalized relational encoding of an RPG ruleset, which physical indexing strategy minimizes latency of each class of rule-lookup query, by how much, **and how do those answers change when the data is mutating in real time**?

The motivating application is an automated Game Master, or Game Master's assistant: an AI agent that is capable of managing the game world, the rules, and the live game state while simultaneously adjudicating player actions and narrating in real time.  Resolving a single player action can fan out into dozens of rule lookups in rapid succession, so per-query latency compounds quickly.

Fast, index-backed retrieval is a hard requirement for such an agent to feel responsive.  This project studies **spells** as a representative slice of the ruleset, on the principle that all other rules would be encoded and indexed similarly.

The goals of this project are to:

1. **Map each kind of lookup a DM agent issues** onto a canonical SQL query, and to the index structure that should serve it (B-tree, hash, GIN inverted index, bitmap/BitmapAnd).
2. **Quantify the read benefit in detail** as the size of the ruleset scales; i.e., how the latency distribution (tail) looks, and what each index costs to build and store.
3. **Address what happens to these indexes in real time** when the data is continuously inserted, updated, and deleted — the write/read trade-off, MVCC storage bloat, and planner-statistics drift — and derive a design rule from the findings.

## Implementation

[Github Link](https://github.com/ydhadix/rpg-index-study)

The current project is a reproducible measurement pipeline.  `make setup up all` runs it end to end; each stage is a single-responsibility Python module.

### Dataset

| | |
|---|---|
| **Source** | [5etools](https://5e.tools) open D&D 5e data set — repo: [5etools-mirror-3/5etools-src](https://github.com/5etools-mirror-3/5etools-src), spells under `data/spells/` |
| **Format** | structured JSON, one file per source book (`spells-*.json`) |
| **Raw records** | 936 spell printings across 17 source books |
| **Canonical spells** | **557** after de-duplicating reprints (one row per spell name) |
| **Attributes** | level (0–9), school (8), casting time, range, components, duration, concentration, ritual, full-text body |
| **Relationships** | spell <-> class (M:N, 10 classes, 1,201 pairs); spell <-> damage type (316 pairs) |
| **Scaled sizes** | 10,000 / 100,000 / 1,000,000 rows via distribution-preserving replication |

The dataset was chosen because it is complete and popular: it spans all ten spell levels and every school, giving a realistic spread of column cardinalities — the property that actually determines whether a given index helps.  Synthetic inflation is the main limitation: it preserves cardinality and selectivity exactly but not text diversity (bodies repeat), which is why the GIN full-text result is the one most flattered by scale.

### Data Flow

_ASCII Graph Generated with Claude Sonnet 4.6_

```
            ┌────────────────────────────┐
            │ 5etools spells-*.json      │  real ruleset: 936 printings / 17 books
            │ gendata-…-lookup.json      │
            └───────────────┬────────────┘
                            │  etl.py   parse · de-dup reprints · flatten rules text · join classes
                            ▼
            ┌────────────────────────────┐
            │ data/*.csv  (557 spells)   │
            └───────────────┬────────────┘
                            │  load.py   schema.sql + COPY
                            ▼
            ┌────────────────────────────┐      ┌────────────────────────────────┐
            │ PostgreSQL 16  (557 rows)  │────▶ │ inflate.py: replicate in-DB to │
            └────────────────────────────┘      │ 10K / 100K / 1M rows           │
                            ▲                   └───────────────┬────────────────┘
        sql/treatments.sql  │                                   ▼
        (T0->T5 index sets)  │      ┌────────────────────────────────────────────────────┐
                            └─────▶│  READ framework         │  REAL-TIME framework     │
                                   │  bench.py               │  bench_write.py          │
                                   │  • treatment×query      │  • B1 write tax vs #idx  │
                                   │    EXPLAIN(ANALYZE,     │  • B2 MVCC bloat->VACUUM  │
                                   │    BUFFERS) matrix      │  • B3 stale statistics   │
                                   │  • hot-path p50/p95/p99 │                          │
                                   │  • index build+size     │  bench_scale.py          │
                                   │                         │  • latency vs table size │
                                   └───────────┬─────────────────────────┬──────────────┘
                                               ▼                         ▼
                              results/summary.md · *.png    results/realtime.md · scaling.* · *.png
                                               ▼
                              per-query index recommendation + real-time design rule
```

#### Goal 1 - Map each kind of lookup a DM agent issues

1. **`etl.py` creates the normalized schema (`sql/schema.sql`).**  It parses the ragged 5etools JSON into a normalized model: a `spell` fact table; `school` / `caster_class` lookups; and `spell_class` / `spell_damage` relationship tables for the M:N dimensions. Normalization is what *creates* the different query shapes (equality, range, multi-predicate, full-text, join) that the study then indexes.

#### Goal 2 - Quantify the read benefit in detail

2. **`sql/treatments.sql` + `bench.py` (read framework).**  Indexes are deliberately excluded from the schema and added one treatment at a time (T1 b-tree > T2 hash > T3 GIN > T4 bitmap pair > T5 join indexes) so each index's effect is isolated.  `bench.py` drops to a bare-schema T0 baseline, applies each treatment, and re-measures all seven queries with `EXPLAIN (ANALYZE, BUFFERS)` (median of 5 trials), recording execution time, the plan access method, and buffer touches.  It also runs an agent-style throughput test and now reports the full latency distribution and per-index build time + disk size.

3. **`inflate.py` + `bench_scale.py` (scaling).**  A few hundred real spells are too small for index effects to rise above timer noise, so `inflate.py` replicates each real spell with a unique surrogate name while copying every other attribute verbatim — preserving the real marginal and joint column distributions exactly, just at scale. `bench_scale.py` drives it to 10K, 100K, and 1M rows and re-measures, turning a single before/after into an asymptotic curve.

#### Goal 3 - Address what happens to these indexes in real time

4. **`bench_write.py` (real-time framework).** The piece added for the intermediate step.  It mutates the table and measures three real-time challenges.

### Hypothesis

> **Read-heavy Ruleset:** The right physical index reduces per-query latency by orders of magnitude, and the size of the win is governed by query class and column selectivity — equality lookups gain the most and stay constant-time as the data grows, while low-selectivity range/text queries gain less and their advantage deteriorates at scale.
> **Real-time mutation Ruleset:** Indexes are not free; each index imposes a write-maintenance cost that grows with the number of indexes, in-place edits bloat MVCC storage until VACUUM reclaims it, and a fast-changing distribution makes planner statistics stale.  So a real-time system needs to index its near-static ruleset and its live game state *asymmetrically*.

The read framework, the scaling study, and the real-time framework each test one clause of
this hypothesis.

## Results

All figures are at the engine's warm cache, parallelism disabled for plan stability, median of repeated trials. Full tables in [results/summary.md](results/summary.md) and [results/realtime.md](results/realtime.md).

### 1 Per-query read latency and the winning plan (100K spells)

| Query (DM-agent action) | T0 baseline | best index | plan after | ms before -> after |
|---|---|---|---|---|
| **Q1** fetch one spell by name (hot path) | Seq Scan | hash | Index Scan (hash) | 17.16 -> **0.004** (>4,000×) |
| **Q2** spells of level ≥ 8 | Seq Scan | b-tree | Bitmap Heap Scan | 14.36 -> **1.25** (11×) |
| **Q3** school ∧ concentration | Seq Scan | bitmap pair | **BitmapAnd** of two b-trees | 18.01 -> **2.55** (7×) |
| **Q4** keyword search of rules text | Seq Scan | GIN | Bitmap Heap Scan (GIN) | 81.20 -> **3.75** (22×) |
| **Q5** a class's spells ≤ level | Seq + hash join | join idx | Index-Only Scan + join | 63.25 -> **33.35** (1.9×) |
| **Q6** Top-N ordered browse | Seq + Sort | b-tree | Index Scan (ordered, no Sort) | 21.69 -> **1.49** (15×) |
| **Q7** spells by damage type | Seq + hash join | join idx | Index-Only Scan + join | 2.67 -> **1.28** (2.1×) |

The plan-transition table in `summary.md` shows *why*: the right index flips Seq Scan -> Index/Bitmap/GIN scan and, for Q3, makes the planner combine two unselective predicates with a BitmapAnd. Buffer accounting makes the mechanism concrete — Q1 falls from ~15,600 shared-buffer touches to 2.

### 2 The hot path is a distribution, not an average

A DM agent fans out many lookups per turn, so the tail bounds responsiveness.  Firing 1,000 point lookups back-to-back:

| state | queries/sec | p50 (ms) | p95 (ms) | p99 (ms) | max (ms) |
|---|---|---|---|---|---|
| baseline (seq scan) | 98 | 9.71 | 12.33 | 17.56 | 18.42 |
| hash index | **6,273** | **0.149** | **0.217** | **0.309** | 0.339 |

The index improves throughput ~64× *and collapses the tail*: p99 drops from 17.6 ms to
0.31 ms. Twenty sequential baseline lookups would already blow a ~250 ms interactivity
budget; indexed, the agent can issue thousands.

### 3 The other half of performance — what indexes cost (100K)

| index | build time (ms) | on-disk size (MB) |
|---|---|---|
| ix_spell_level (b-tree) | 23 | 0.68 |
| ix_spell_name_hash (hash) | 58 | 4.02 |
| **ix_spell_body_gin (GIN)** | **722** | **14.60** |
| ix_spell_school / ix_spell_conc | 25 / 25 | 0.68 / 0.68 |
| ix_spellclass_class / ix_spelldamage_type | 48 / 30 | 6.52 / 1.88 |

Total secondary-index footprint is 29.1 MB on top of the base table.  The GIN full-text index is an outlier on both axes — slowest to build and largest to store — which foreshadows its write cost in section 5 below.  Indexing is a space/build trade, not free speed.

### 4 Scaling: the index benefit is asymptotic, but selectivity-dependent

Re-measuring a query subset at 10K -> 100K -> 1M rows
([results/scaling.png](results/scaling.png)):

| query | seq scan 10K -> 1M (ms) | indexed 10K -> 1M (ms) | reading |
|---|---|---|---|
| **Q1** point lookup (hash) | 4.4 -> 25.9 -> **190.9** | 0.004 -> 0.004 -> **0.005** | seq scan grows ~linearly (O(n)); hash lookup is **flat** (O(1)) -> **~38,000× at 1M** |
| **Q2** range level ≥ 8 (b-tree) | 4.0 -> 26.5 -> 187.4 | 0.12 -> 1.15 -> **87.4** | indexed path *also* grows because the result set is ~13% of the table |
| **Q4** keyword search (GIN) | 11.5 -> 85.3 -> 842.2 | 0.23 -> 2.00 -> **413.9** | match set inflates with the replicated text -> advantage compresses |
| **Q6** Top-N browse (b-tree) | 5.1 -> 34.4 -> 272.0 | 0.19 -> 1.55 -> **82.4** | Top-N stays cheap relative to the scan |

This is the most interesting finding: **the index that wins biggest is the *selective* one.**  A hash point-lookup returns one row, so it is constant-time regardless of ruleset size — exactly the property a DM agent's hot path needs.  A range or text query that returns a *growing fraction* of the table sees its index advantage degrade at scale, because returning more rows is inherently more work no matter the access path (Q2's indexed path touches 13% of a 1M table).  Index size scales with the data as well: the GIN index grows 1.3 -> 9.5 -> 117 MB.  Thus, the ideal implementation indexes the selective hot paths hard; for unselective bulk queries an index is not automatically the answer.

### 5. Real-time data and its challenges

The read study treats the ruleset as static. A DM also needs to alter the game state in real time, and that is where the costs above come into play.  `bench_write.py` measures three challenges ([results/realtime.md](results/realtime.md)); write workloads run server-side so the numbers reflect database work, not client round-trips.

#### The write/read trade-off

Every read index is a write tax.  Bulk-inserting 50,000 rows under a growing index set:

| index stage | # idx | rows/sec | µs/row | slowdown |
|---|---|---|---|---|
| none | 0 | 3,353 | 298 | 1.0× |
| +level b-tree | 1 | 3,260 | 307 | 1.03× |
| +name hash | 2 | 3,335 | 300 | 1.01× |
| **+body GIN** | 3 | 2,901 | 345 | **1.16×** |
| +school,conc | 5 | 2,876 | 348 | 1.17× |

Write throughput falls monotonically as indexes are added, as every index must be updated on every insert.  The finding is that B-tree and hash indexes are cheap to maintain; the GIN full-text index causes almost the entire write tax (the jump at stage 3).  So, the index that most helps text *reads* is also the one that most hurts *writes*.

#### The cost of in-place edits

PostgreSQL never overwrites a row; an `UPDATE` writes a new version and tombstones the old one.  With autovacuum disabled to make the effect visible, six full-table updates on an indexed column ([results/bloat.png](results/bloat.png)):

| table state | size (MB) | full-scan (ms) | dead tuples |
|---|---|---|---|
| clean | 148.7 | 17.1 | — |
| bloated (6 updates) | **863.8** | **93.6** | 601,234 |
| after VACUUM FULL | 145.6 | 17.1 | — |

601K dead tuples bloated the table 5.8× and slowed a full scan 5.5×; `VACUUM FULL` restored the baseline. In production, autovacuum runs continuously precisely to keep this bounded, which is the background write load the system must budget for.

#### Real-time drift fools the optimizer

A burst of 60,000 inserts shifts the `level` distribution.  Until `ANALYZE` re-runs:

| statistics state | plan | est. rows | actual rows | error |
|---|---|---|---|---|
| stale (after burst) | Bitmap Index Scan | 9,147 | 68,592 | **7.5× off** |
| after ANALYZE | Bitmap Index Scan | 68,912 | 68,592 | 1.0× |

The optimizer chooses plans from cached statistics.  When data changes faster than `ANALYZE` runs, those estimates are wrong (here by 7.5×), and a bad estimate produces a bad plan.  Re-analyzing fixes it.

## The Conclusion

Reads and writes pull in opposite directions, so a real-time system should index its two populations **asymmetrically**: index the large, near-static **ruleset** aggressively (reads dominate; writes are rare bulk loads), but index fast-mutating **game state** conservatively, and schedule `ANALYZE`/`VACUUM` to keep statistics fresh and bloat bounded.

## Technologies Used

- **PostgreSQL 16** (run in Docker): chosen for its readable `EXPLAIN (ANALYZE, BUFFERS)` output and breadth of index types.
- **Index Types / Physical Organization**:  B-tree (range & ordered access), Hash (point equality), GIN inverted index (full-text), and bitmap index scans / BitmapAnd (combining low-cardinality predicates).
- **Query Processing & Optimization** reading query plans, scan vs. index-scan vs. bitmap-scan trade-offs, join methods (hash join vs. indexed nested loop), buffer/I-O accounting, and selectivity.
- **Python 3** with `psycopg` for ETL, in-database synthetic data generation, and the benchmark harness; `matplotlib`/`tabulate` for reporting.
- **Experiment Design**: a treatment × query matrix, warm-up runs, median of repeated trials, and a fixed disclosed dataset.

## Running the Project

Requires Docker (for PostgreSQL 16) and Python 3.12+.

**Dataset.**  The ETL step reads the 5etools spell JSON, which is not bundled with this repo.  Clone the dataset and place it as a sibling of this project (so the data lives at `../5etools-src/data/`), or point the ETL elsewhere with the `FIVET_DATA` env var:

```bash
# from this project's parent directory:
git clone https://github.com/5etools-mirror-3/5etools-src.git
#   -> data is then at ../5etools-src/data/spells/   (the default the ETL looks for)
#      or, if the checkout lives somewhere else:
export FIVET_DATA=/path/to/5etools-src/data
```

Once the data is in place, launch Docker.  You can then build and reproduce the project with these commands:

```bash
make setup up        # venv + start PostgreSQL 16
make all             # etl -> load -> inflate(100K) -> read benchmark (summary.md, charts)
make bench-write     # real-time: write tax + bloat + stale stats (realtime.md, charts)
make bench-scale     # scaling curve across 10K/100K/1M (scaling.png)
#   smaller scaling run:  make bench-scale SIZES=10000,100000,250000
```

## Layout

```
sql/schema.sql       normalized DDL (no secondary indexes)
sql/treatments.sql   the cumulative CREATE INDEX statements, one block per treatment
src/etl.py           5etools JSON -> normalized CSVs (reprint de-dup, body flattening)
src/load.py          schema + base load
src/inflate.py       in-database replication to 10K/100K/1M rows
src/workload.py      the 7 canonical queries (single source of truth)
src/bench.py         read framework: EXPLAIN(ANALYZE,BUFFERS) matrix + throughput
                     distribution (p50/p95/p99) + index build-time/size + charts
src/bench_scale.py   scaling study: latency vs table size across 10K/100K/1M
src/bench_write.py   real-time framework: write tax + MVCC bloat->VACUUM + stale stats
results/             summary.md, realtime.md, *.csv, *.png
```
