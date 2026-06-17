# Real-time data and its challenges

The read benchmark studies a *static* ruleset. A live DM agent also mutates game state in real time, where the same indexes that accelerate reads must be maintained on every write and where PostgreSQL's MVCC storage leaves work behind. These three measurements quantify challenges the static study cannot show. Write workloads run server-side so the numbers reflect database work, not client round-trips.

## B1 - The write/read trade-off: every read index is a write tax

Bulk-inserting 50,000 rows under a growing set of indexes. Each index must be
updated on every insert, so throughput falls as indexes are added; the GIN full-text index is by far the most expensive to maintain.

| index stage      |   # idx |   rows/sec |   us/row | slowdown   |
|------------------|---------|------------|----------|------------|
| 0: none          |       0 |     3353   |   298.24 | 1.0x       |
| 1: +level b-tree |       1 |     3260.5 |   306.71 | 1.03x      |
| 2: +name hash    |       2 |     3334.7 |   299.87 | 1.01x      |
| 3: +body GIN     |       3 |     2900.8 |   344.73 | 1.16x      |
| 4: +school,conc  |       5 |     2876.3 |   347.67 | 1.17x      |

## B2 - MVCC churn and bloat: the cost of real-time edits

PostgreSQL never overwrites a row in place: an `UPDATE` writes a new version and tombstones the old one. Under continuous edits the table bloats with dead tuples and full scans slow down, until `VACUUM` reclaims the space. (Autovacuum was disabled here to make the effect visible; in production it runs continuously precisely to prevent this.)

| table state               |   size_MB |   full-scan_ms |   dead_tuples |
|---------------------------|-----------|----------------|---------------|
| clean (after VACUUM FULL) |    148.7  |         17.114 |               |
| bloated (6 updates)       |    863.8  |         93.644 |        601234 |
| after VACUUM FULL         |    145.59 |         17.096 |               |

The 601,234 dead tuples grew the table ~5.8x and slowed a full scan accordingly; `VACUUM FULL` returned it to the clean baseline.

## B3 - Stale statistics: real-time drift fools the optimizer

A burst of inserts shifts the `level` distribution. Until `ANALYZE` re-runs, the planner's cached statistics are stale, so it badly mis-estimates how many rows match `level >= 8` -- which is exactly the input it uses to choose a plan.

| statistics state            | plan              |   est rows |   actual rows | est error   |
|-----------------------------|-------------------|------------|---------------|-------------|
| after burst, stale stats    | Bitmap Index Scan |       9147 |         68592 | 7.5x        |
| after ANALYZE (fresh stats) | Bitmap Index Scan |      68912 |         68592 | 1.0x        |

## Design conclusion

Reads and writes pull in opposite directions, so a real-time system should index its two data populations asymmetrically: index the large, near-static ruleset aggressively (reads dominate; writes are rare bulk loads), but index fast-mutating game state conservatively, and schedule `ANALYZE`/`VACUUM` to keep statistics fresh and bloat bounded.
