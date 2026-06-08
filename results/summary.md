# Benchmark results

Engine: PostgreSQL 16 (Docker). Parallelism disabled for stable plan comparison.
Each cell is the **median of 5 timed `EXPLAIN (ANALYZE, BUFFERS)` runs** (ms).
Treatments are applied cumulatively (T0 = primary keys only).

## Median execution time (ms) by treatment

| query                                         |   T0_baseline |   T1_btree |   T2_hash |   T3_gin |   T4_bitmap |   T5_join |
|-----------------------------------------------|---------------|------------|-----------|----------|-------------|-----------|
| Q1 point lookup by name                       |         14.23 |      11.63 |      0    |     0    |        0    |      0    |
| Q2 range scan by level                        |         13.46 |       1.26 |      1.25 |     1.26 |        1.26 |      1.22 |
| Q3 two low-cardinality predicates (BitmapAnd) |         16.53 |      15.31 |     16.57 |    18.54 |        2.61 |      2.59 |
| Q4 full-text body search                      |         77.68 |      76.4  |     75.79 |     3.77 |        3.75 |      3.72 |
| Q5 class-membership join                      |         60.02 |      61.78 |     59.68 |    57.7  |       58.97 |     47.64 |
| Q6 Top-N ordered browse                       |         19.41 |       1.55 |      1.54 |     1.68 |        1.56 |      1.56 |
| Q7 array dimension join                       |          2.91 |       2.9  |      2.85 |     3.31 |        2.78 |      1.85 |

## Query plan access method by treatment

| query   | T0_baseline   | T1_btree                          | T2_hash                           | T3_gin                               | T4_bitmap                                 | T5_join                                   |
|---------|---------------|-----------------------------------|-----------------------------------|--------------------------------------|-------------------------------------------|-------------------------------------------|
| Q1      | Seq Scan      | Seq Scan                          | Index Scan (ix_spell_name_hash)   | Index Scan (ix_spell_name_hash)      | Index Scan (ix_spell_name_hash)           | Index Scan (ix_spell_name_hash)           |
| Q2      | Seq Scan      | Bitmap Heap Scan (ix_spell_level) | Bitmap Heap Scan (ix_spell_level) | Bitmap Heap Scan (ix_spell_level)    | Bitmap Heap Scan (ix_spell_level)         | Bitmap Heap Scan (ix_spell_level)         |
| Q3      | Seq Scan      | Seq Scan                          | Seq Scan                          | Seq Scan                             | BitmapAnd (ix_spell_school+ix_spell_conc) | BitmapAnd (ix_spell_school+ix_spell_conc) |
| Q4      | Seq Scan      | Seq Scan                          | Seq Scan                          | Bitmap Heap Scan (ix_spell_body_gin) | Bitmap Heap Scan (ix_spell_body_gin)      | Bitmap Heap Scan (ix_spell_body_gin)      |
| Q5      | Seq Scan      | Seq Scan                          | Seq Scan                          | Seq Scan                             | Seq Scan                                  | Bitmap Heap Scan (ix_spellclass_class)    |
| Q6      | Seq Scan      | Index Scan (ix_spell_level)       | Index Scan (ix_spell_level)       | Index Scan (ix_spell_level)          | Index Scan (ix_spell_level)               | Index Scan (ix_spell_level)               |
| Q7      | Seq Scan      | Seq Scan                          | Seq Scan                          | Seq Scan                             | Seq Scan                                  | Bitmap Heap Scan (ix_spelldamage_type)    |

## Agent-style throughput: 1000 point lookups (the hot path)

| state               |   lookups |   total_s |   avg_ms |   queries/sec |
|---------------------|-----------|-----------|----------|---------------|
| baseline (seq scan) |      1000 |    9.6393 |   9.6393 |         103.7 |
| hash index          |      1000 |    0.1496 |   0.1496 |        6685.2 |

**Indexing the hot point-lookup path gave a 64.5x throughput improvement.**
