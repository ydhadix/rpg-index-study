# Testing PostgreSQL Indexing Strategies for Efficient Lookup of Tabletop RPG Rules

> Tabletop RPG play depends on lookup of specific rules - spells, items, monster abilities, etc. - from a mass of thousands of individual options, in a limited amount of time.
> **The problem:** Given a normalized relational encoding of an RPG ruleset, which physical indexing strategy minimizes latency of different kinds of rule-lookup queries, and by how much?

The motivating application is an automated Game Master, or Game Master's assistant: an AI agent that is capable of managing the game world, the rules, and the live game state while simultaneously adjudicating player actions and narrating in real time.  Resolving a single player action can fan out into dozens of rule lookups in rapid succession, so per-query latency compounds quickly.

Fast, index-backed retrieval is therefore a hard requirement for such an agent to feel responsive.  This project studies **spells** as a representative slice of the ruleset, on the principle that all other rules would be encoded and indexed similarly.

## Prototype

The current project is a miniature of the full study.  `make all` runs the following steps in sequence.

1. Parse a real, complete ruleset into a normalized schema.
2. Scale the data to a realistic size.
3. Run a fixed read workload under different index treatments.
4. Measure and chart the results.

The schema (`sql/schema.sql`) is normalized: a `spell` fact table; `school` and `caster_class` lookups; and `spell_class` / `spell_damage` relationship tables for the many-to-many dimensions.  Indexes are deliberately excluded from the schema, instead added one at a time (`sql/treatments.sql`) so each one's effect is isolated.

## Results

_To be determined..._

> The prototype produces a sample results set for searching 100,000 entries; a proper analysis of these results is beyond the scope of the current prototype.

## Dataset

| | |
|---|---|
| **Source** | [5etools](https://5e.tools) open D&D 5e data set — repo: [5etools-mirror-3/5etools-src](https://github.com/5etools-mirror-3/5etools-src), spells under `data/spells/` |
| **Format** | structured JSON, one file per source book (`spells-*.json`) |
| **Raw records** | 936 spell printings across 17 source books |
| **Canonical spells** | **557** after de-duplicating reprints (one row per spell name) |
| **Attributes** | level (0–9), school (8), casting time, range (+ feet), components (v/s/m + material), duration, concentration, ritual, full-text body |
| **Relationships** | spell ↔ class (M:N, 10 classes, 1,201 pairs); spell ↔ damage type (316 pairs) |

This dataset was chosen because it is complete and popular — it spans all ten spell levels and every school, giving a realistic spread of column cardinalities (the property that actually determines whether a given index helps).  Class membership is taken from 5etools' generated `gendata-spell-source-lookup.json`.

**Synthetic Inflation.**  A real ruleset is only hundreds-to-thousands of rows — too small for index effects to show above timer noise. `src/inflate.py` grows the table to a configurable size (default 100,000 rows) by replicating each real spell with a unique surrogate name (`"Fireball #4217"`) while copying every other attribute verbatim.  Replication preserves the real marginal and joint distributions of all indexed columns exactly so selectivity stays realistic.  This inflated table stands in for "the entire encoded ruleset" that a GM agent would query.

## Technologies Used

- **PostgreSQL 16** (run in Docker): chosen for its readable `EXPLAIN (ANALYZE, BUFFERS)` output and breadth of index types.
- **Index Types / Physical Organization**:  B-tree (range & ordered access), Hash (point equality), GIN inverted index (full-text), and bitmap index scans / BitmapAnd (combining low-cardinality predicates).
- **Query Processing & Optimization** reading query plans, scan vs. index-scan vs. bitmap-scan trade-offs, join methods (hash join vs. indexed nested loop), buffer/I-O accounting, and selectivity.
- **Python 3** with `psycopg` for ETL, in-database synthetic data generation, and the benchmark harness; `matplotlib`/`tabulate` for reporting.
- **Experiment Design**: a treatment × query matrix, warm-up runs, median of repeated trials, and a fixed disclosed dataset.

## Data Flow

_ASCII Graph Generated with Claude Sonnet 4.6_

```
                ┌────────────────────────┐
                │ 5etools  spells-*.json │   (real ruleset, 936 records / 17 books)
                │ gendata-…-lookup.json  │
                └───────────┬────────────┘
                            │  etl.py  (parse, de-dup reprints, flatten body, join classes)
                            ▼
                ┌────────────────────────┐
                │  data/*.csv (557 spells)│
                └───────────┬────────────┘
                            │  load.py  (schema.sql + COPY)
                            ▼
                ┌────────────────────────┐        ┌───────────────────────────┐
                │  PostgreSQL  (557 rows) │──────▶ │ inflate.py: replicate to  │
                └────────────────────────┘        │ 100,000 rows (in-database)│
                            ▲                       └───────────┬───────────────┘
                            │                                   ▼
       sql/treatments.sql  │                       ┌───────────────────────────┐
       (T0→T5 index sets) ─┘──────────────────────▶│  bench.py                 │
                                                    │  workload.py × treatments │
                                                    │  EXPLAIN(ANALYZE,BUFFERS) │
                                                    │  + throughput test        │
                                                    └───────────┬───────────────┘
                                                                ▼
                                            results/results.csv · summary.md · *.png
                                                                ▼
                                            Analysis → per-query index recommendation
```

**Explanation.**

- `etl.py` reads the raw 5etools JSON, keeps one canonical row per spell name (resolving reprints by source priority), flattens the nested rules text into a searchable body, and attaches each spell's classes and damage types.  This creates three normalized CSVs.
- `load.py` creates the schema and loads those 557 real spells.
- `inflate.py` replicates the spells inside Postgres to 100K rows, preserving all distributions and refreshes planner statistics.
- `bench.py` drops to the T0 baseline and, applying the `treatments.sql` index sets one at a time, runs every query in `workload.py` under each
treatment with `EXPLAIN (ANALYZE, BUFFERS)`, plus a 1,000-lookup throughput test.
- The outputs are a results matrix, a human-readable summary, and charts (`results/`).

## Code

Clone and run with: `make setup && make up && make all`.

## Challenges

1. **Normalizing irregular, nested source data.**  The 5etools JSON is denormalized and ragged: the same spell is *reprinted across multiple source books* (936 printings collapse to 557 real spells), ranges and durations are typed objects rather than scalars, material components are sometimes a string and sometimes an object with a cost, and the rules text is a recursively nested tree of prose, tables, and inline `{@tag …}` markup.  Turning this into a clean normalized schema required a source-priority de-duplication rule and a recursive text flattener that strips markup while keeping every keyword searchable, otherwise the full-text index would miss terms buried in nested entries.

2. **Inflating a tiny dataset without distorting it.** A few hundred real spells is far too small for index effects to exceed timer noise, but naively generating random rows would destroy the column cardinalities and correlations that determine whether an index helps.  The challenge was to scale to 100K rows while keeping the measurements honest.  The solution I settled on was replicating each real spell with a unique surrogate name,  but otherwise verbatim attributes.  This preserves the exact distributions of every indexed column, so the selectivity the optimizer sees is the real ruleset's selectivity, just at scale.

## Running it

Requires Docker (for PostgreSQL 16) and Python 3.12+.

**Dataset dependency.** The ETL step reads the 5etools spell JSON, which is *not* bundled
with this repo. Clone the dataset and place it as a sibling of this project (so the data
lives at `../5etools-src/data/`), or point the ETL elsewhere with the `FIVET_DATA` env var:

```bash
# from this project's parent directory:
git clone https://github.com/5etools-mirror-3/5etools-src.git
#   -> data is then at ../5etools-src/data/spells/   (the default the ETL looks for)
#   or, if the checkout lives somewhere else:
export FIVET_DATA=/path/to/5etools-src/data
```

Then build and run:

```bash
make setup        # create venv + install psycopg, tabulate, matplotlib
make up           # start PostgreSQL 16 in Docker, wait until ready
make all          # etl -> load -> inflate (100K) -> bench
#   or step by step:
make etl          # parse 5etools JSON (../5etools-src/data) -> data/*.csv
make load         # create schema + load the 557 real spells
make inflate ROWS=100000
make bench        # run the treatment x query matrix + throughput test
make psql         # open a psql shell to poke at plans by hand
make down         # stop the database
```

## Layout

```
sql/schema.sql       normalized DDL (no secondary indexes)
sql/treatments.sql   the cumulative CREATE INDEX statements, one block per treatment
src/etl.py           5etools JSON -> normalized CSVs (reprint de-dup, body flattening)
src/load.py          schema + base load
src/inflate.py       in-database replication to ~100K rows
src/workload.py      the 7 canonical queries (single source of truth)
src/bench.py         EXPLAIN(ANALYZE,BUFFERS) matrix + throughput + charts
results/             results.csv, throughput.csv, summary.md, *.png
```