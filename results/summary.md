# Benchmark results

Engine: PostgreSQL 16 (Docker). Parallelism disabled for stable plan comparison.  Each cell is the **median of 5 timed `EXPLAIN (ANALYZE, BUFFERS)` runs** (ms).  Treatments are applied cumulatively (T0 = primary keys only).

## Median execution time (ms) by treatment

| query                                         |   T0_baseline |   T1_btree |   T2_hash |   T3_gin |   T4_bitmap |   T5_join |
|-----------------------------------------------|---------------|------------|-----------|----------|-------------|-----------|
| Q1 point lookup by name                       |         17.16 |      13.23 |      0    |     0    |        0    |      0    |
| Q2 range scan by level                        |         14.36 |       1.28 |      1.25 |     1.24 |        1.27 |      1.26 |
| Q3 two low-cardinality predicates (BitmapAnd) |         18.01 |      17.14 |     20.32 |    20.87 |        2.55 |      2.59 |
| Q4 full-text body search                      |         81.2  |      78.38 |     78.12 |     3.96 |        3.79 |      3.75 |
| Q5 class-membership join                      |         63.25 |      64.34 |     64.61 |    62.76 |       62.38 |     33.35 |
| Q6 Top-N ordered browse                       |         21.69 |       1.52 |      1.52 |     1.51 |        1.49 |      1.53 |
| Q7 array dimension join                       |          2.67 |       2.67 |      2.68 |     2.68 |        2.7  |      1.28 |

## Query plan access method by treatment

| query   | T0_baseline   | T1_btree                          | T2_hash                           | T3_gin                               | T4_bitmap                                 | T5_join                                   |
|---------|---------------|-----------------------------------|-----------------------------------|--------------------------------------|-------------------------------------------|-------------------------------------------|
| Q1      | Seq Scan      | Seq Scan                          | Index Scan (ix_spell_name_hash)   | Index Scan (ix_spell_name_hash)      | Index Scan (ix_spell_name_hash)           | Index Scan (ix_spell_name_hash)           |
| Q2      | Seq Scan      | Bitmap Heap Scan (ix_spell_level) | Bitmap Heap Scan (ix_spell_level) | Bitmap Heap Scan (ix_spell_level)    | Bitmap Heap Scan (ix_spell_level)         | Bitmap Heap Scan (ix_spell_level)         |
| Q3      | Seq Scan      | Seq Scan                          | Seq Scan                          | Seq Scan                             | BitmapAnd (ix_spell_school+ix_spell_conc) | BitmapAnd (ix_spell_school+ix_spell_conc) |
| Q4      | Seq Scan      | Seq Scan                          | Seq Scan                          | Bitmap Heap Scan (ix_spell_body_gin) | Bitmap Heap Scan (ix_spell_body_gin)      | Bitmap Heap Scan (ix_spell_body_gin)      |
| Q5      | Seq Scan      | Seq Scan                          | Seq Scan                          | Seq Scan                             | Seq Scan                                  | Index Only Scan (ix_spellclass_class)     |
| Q6      | Seq Scan      | Index Scan (ix_spell_level)       | Index Scan (ix_spell_level)       | Index Scan (ix_spell_level)          | Index Scan (ix_spell_level)               | Index Scan (ix_spell_level)               |
| Q7      | Seq Scan      | Seq Scan                          | Seq Scan                          | Seq Scan                             | Seq Scan                                  | Index Only Scan (ix_spelldamage_type)     |

## Agent-style throughput: 1000 point lookups (the hot path)

Mean throughput plus the per-lookup latency distribution. The tail (p95/p99) is what bounds how responsive an interactive agent feels when it fans out many lookups per turn.

| state               |   lookups |   queries/sec |   avg_ms |   p50_ms |   p95_ms |   p99_ms |   max_ms |
|---------------------|-----------|---------------|----------|----------|----------|----------|----------|
| baseline (seq scan) |      1000 |          98.4 |  10.1666 |   9.7138 |  12.3326 |  17.562  |  18.4179 |
| hash index          |      1000 |        6273.2 |   0.1594 |   0.1485 |   0.217  |   0.3094 |   0.3388 |

**Indexing the hot point-lookup path gave a 63.8x throughput improvement.**

## Cost of indexing: build time + on-disk size (100K spells)

Indexes are not free: each one is built once and then stored and maintained. Total secondary-index footprint here is **29.1 MB** on top of the base table.

| index               |   build_ms |   size_mb |
|---------------------|------------|-----------|
| ix_spell_level      |       23   |      0.68 |
| ix_spell_name_hash  |       58   |      4.02 |
| ix_spell_body_gin   |      721.6 |     14.6  |
| ix_spell_school     |       25.1 |      0.68 |
| ix_spell_conc       |       25.1 |      0.68 |
| ix_spellclass_class |       47.5 |      6.52 |
| ix_spelldamage_type |       29.9 |      1.88 |
