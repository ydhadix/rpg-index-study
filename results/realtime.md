# Real-time data and its challenges

The read benchmark studies a *static* ruleset. A live DM agent also mutates game state in
real time, where the same indexes that accelerate reads must be maintained on every write
and where PostgreSQL's MVCC storage leaves work behind. These three measurements quantify
challenges the static study cannot show. Write workloads run server-side so the numbers
reflect database work, not client round-trips.

## B1 - The write/read trade-off: every read index is a write tax

Bulk-inserting 50,000 rows under a growing set of indexes. Each index must be
updated on every insert, so throughput falls as indexes are added; the GIN full-text index
is by far the most expensive to maintain.

| index stage      |   # idx |   rows/sec |   us/row | slowdown   |
|------------------|---------|------------|----------|------------|
| 0: none          |       0 |     3268.7 |   305.94 | 1.0x       |
| 1: +level b-tree |       1 |     3238.5 |   308.78 | 1.01x      |
| 2: +name hash    |       2 |     3201   |   312.4  | 1.02x      |
| 3: +body GIN     |       3 |     2781.2 |   359.56 | 1.18x      |
| 4: +school,conc  |       5 |     2734   |   365.76 | 1.2x       |

## B2 - MVCC churn and bloat: the cost of real-time edits

PostgreSQL never overwrites a row in place: an `UPDATE` writes a new version and tombstones
the old one. Under continuous edits the table bloats with dead tuples and full scans slow
down, until `VACUUM` reclaims the space. (Autovacuum was disabled here to make the effect
visible; in production it runs continuously precisely to prevent this.)

| table state               |   size_MB |   full-scan_ms |   dead_tuples |
|---------------------------|-----------|----------------|---------------|
| clean (after VACUUM FULL) |    146.23 |         17.6   |               |
| bloated (6 updates)       |    877.1  |        104.587 |        599150 |
| after VACUUM FULL         |    145.55 |         16.357 |               |

The 599,150 dead tuples grew the table ~6.0x and slowed a full scan accordingly;
`VACUUM FULL` returned it to the clean baseline.

## B3 - Stale statistics: real-time drift fools the optimizer

A burst of inserts shifts the `level` distribution. Until `ANALYZE` re-runs, the planner's
cached statistics are stale, so it badly mis-estimates how many rows match `level >= 8` --
which is exactly the input it uses to choose a plan.

| statistics state            | plan       |   est rows |   actual rows | est error   |
|-----------------------------|------------|------------|---------------|-------------|
| after burst, stale stats    | Index Scan |       8869 |         68380 | 7.7x        |
| after ANALYZE (fresh stats) | Index Scan |      67472 |         68380 | 1.0x        |

## Design conclusion

Reads and writes pull in opposite directions, so a real-time system should index its two
data populations **asymmetrically**: index the large, near-static **ruleset** aggressively
(reads dominate; writes are rare bulk loads), but index fast-mutating **game state**
conservatively, and schedule `ANALYZE`/`VACUUM` to keep statistics fresh and bloat bounded.
