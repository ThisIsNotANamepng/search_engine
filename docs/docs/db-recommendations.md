# Database optimization recommendations

> **Status: analysis only. Nothing in this document has been applied.** Every number below was read
> from the live production database (`stultus`, PostgreSQL 13.23) on **2026-11-21, 06:17–06:45 DB time
> (UTC−5)** with read-only statements, while the 12-box crawler fleet was writing at full speed.
> Every SQL block is a proposal for you to review and run yourself. Anything labelled **estimate** was
> reasoned from the measurements, not measured, because measuring it would have meant changing the database.

**Caveats you should know before trusting a number**

* The box is I/O-saturated by the crawler fleet (section 2), so the same query varies 5–10× between runs
  (e.g. `open source operating system`: 311 ms … 2685 ms). Single timings are indicative; plan *shapes*, buffer counts
  and `Heap Fetches` are the reliable evidence.
* `pg_stat_*` counters accumulate since `stats_reset` (2026-03-14 for the database, 2026-02-11 for bgwriter), and
  several tables were evidently truncated/reloaded since (e.g. `logs` has 620k rows but 184M inserts ever), so lifetime
  counters are used only for *ratios and relative usage*, and the 60–120 s delta samples are used for *rates*.
* `pg_stat_statements`, `pg_trgm`, `pgstattuple`, `pg_repack`, `fuzzystrmatch` are **not available**:
  `pg_available_extensions` lists only `plpgsql`, i.e. the `postgresql13-contrib` package is not installed.
  Bloat figures are therefore estimates from bytes-per-row arithmetic, not `pgstattuple` measurements.
* Build-time figures are estimates (nothing was built). Disk figures for new indexes are estimates from per-entry sizes.

---

## 1. Executive summary

### What is actually slow, in one paragraph

Search is *fast when the box is quiet* (ranking statement: `linux kernel` ≈ 28 ms, `wikipedia` ≈ 10 ms warm) but has three structural costs:
(1) a **title/host `LIKE` scan of `urls`** that costs 58–90 ms on *every* uncached query and **switches itself off at 200,000 indexed pages
(`SEARCH_NAV_MAX_DOCS`) — at the measured ingest rate that happens in roughly two hours**; (2) a **first-touch document-frequency
`count(*)`** per word that costs 0.25–1.0 s per query on a cold word; (3) a **ranking statement whose work is proportional to the
number of pages containing the query words** (55k–97k candidates for 4-word queries, 120 ms–4.4 s), which will hit the 5 s
`statement_timeout` as the corpus grows. Underneath all of it, the database sits on **one 7200 rpm spinning disk
(`ST2000DM001`) that is ~97 % busy with writes**, with `shared_buffers = 128 MB` on a 32 GB machine, so
**79 % of all buffer writes are done by query backends themselves** (even `SELECT`s show `written=1700` pages in `EXPLAIN (BUFFERS)`).
The crawl side is throttled by the same disk plus **lock convoys** (5–8 of ~12 active writer sessions are waiting on another
writer's open transaction at any moment) and a **connection count at the limit** (92–108 of `max_connections = 100`; my own
read-only sessions were repeatedly refused with *too many clients*).

### Top 5 recommendations, ranked by impact ÷ effort

| # | Recommendation | Helps | Effort | Needs fleet at 0? | Evidence in one line |
|---|---|---|---|---|---|
| **1** | **Server config bundle** (`shared_buffers` 8 GB, `effective_cache_size`, checkpoint/WAL/bgwriter, `wal_compression`, `track_io_timing`, logging). Section 4.1 | search tail latency, crawl throughput | 15 min (+1 restart) | restart drops connections; everything else is a reload | backends write 79 % of buffers; disk 97 % util; 1.1 TB of temp files; WAL ≈ 392 B per posting row |
| **2** | **Autovacuum tuning for the 4 posting tables + `url_queue`** (per-table reloptions + cost limit). Section 4.2 | search (index-only scans), queue claims, stats | 10 min, no locks | **No** | `Heap Fetches` 760–9,897 per search; `word_urls` vacuum running 26+ min with 26 M unvacuumed rows; `url_queue` 36–42 % dead, claim scans 2,056 buffers for 100 rows |
| **3** | **Decide the fate of `bigram_urls`/`trigram_urls`/`prefix_urls`** (10.3 GiB = 64 % of the DB, 78 % of every posting row written, **zero index scans in the last 22 min**; default BM25 search never reads them). Section 4.3 | crawl write throughput, disk, vacuum, backups | product decision + 1 app change | only for `TRUNCATE`/`DROP` | 0 scans on the three covering indexes between 06:17 and 06:39; 657k of 841k rows/min are n-gram rows |
| **4** | **`word_df` (+ `corpus_stats`) summary tables**, refreshed in batches. Section 4.4 | search first-touch latency (0.25–1 s → <1 ms) | 1 table + 1 cron + 3-line app change | **No** | statement #3: 988 ms cold for 4 words (`Heap Fetches: 2131`), 7 ms warm |
| **5** | **GIN full-text index for the title/host path** (built-in `tsvector`, no extension needed). Section 4.5 | search: −58…90 ms on every uncached query, and removes the 200k-page cliff | 1 index + app SQL change | **No** (`CONCURRENTLY`) | 58–90 ms parallel seq scan of `urls` per query; I verified the proposed expression returns exactly the same pages as the app's whole-word filter (354 = 354, 0 missing) |

Honorable mentions (section 4): **connection pooling** (4.6), **drop `logs_message_pattern_idx`** and add a log retention
policy (4.8), **`n_distinct` override on `word_urls`** (4.7), dashboard `COUNT(*)`/`DISTINCT` queries (4.9).

### Highest-value things that are *not* database changes

* **Storage.** The data directory *and* the WAL live on a single rotational disk. Moving `pg_wal` (and `temp_tablespaces`) to an SSD
  is likely worth more than every index change here combined. It is outside the SQL scope, but it is the root cause of the I/O saturation.
* **Crawler write path (app side, free):** commit the `words`/`prefixes` inserts in their own short transaction *before* the long
  postings transaction (kills the lock convoy); sort `COPY` rows by token id (turns random index-leaf writes into ordered ones);
  skip the four `DELETE ... WHERE url_id =` statements for urls that were just inserted. Section 5.
* **Search (app side, free):** cast the BM25 literals to `float8` (≈ 20–25 % on the ranking statement in my tests). Section 3.4.

### Do NOT do (evidence says it would not help)

`random_page_cost` lowering (the disk *is* rotational, 4 is right), `CLUSTER`/`VACUUM FULL`/`pg_repack` of the posting tables (the covering
indexes are already in `(token, url)` order and searches are index-only), partitioning the posting tables (nothing here is limited by table
size yet), replacing the token tables with `tsvector` GIN (cannot reproduce BM25 idf/dl ranking and makes the write path worse),
`synchronous_commit = off` *for throughput* (only ~3.7 commits/s: nothing to gain, see 5.5), widening `url_queue.id` (**already done**: column and
sequence are `bigint`, `last_value` 2,148,059,088 of 9.2 × 10¹⁸).

---

### >>> Do NOT run without scaling the crawler fleet to 0 first <<<

Only these statements take locks that block (or break) the running crawlers. Everything else in this document is `CONCURRENTLY`, a `SHARE UPDATE EXCLUSIVE` reloption change, a brand-new table, or a config reload.

| Statement | Lock / effect | Where |
|---|---|---|
| `TRUNCATE bigram_urls, trigram_urls, prefix_urls` / `DROP TABLE` of them | `ACCESS EXCLUSIVE`: every writer blocks or errors | 4.3 option A |
| `ALTER TABLE logs ALTER COLUMN id TYPE bigint` (and any other `ALTER COLUMN TYPE`) | `ACCESS EXCLUSIVE` + full table rewrite | 4.8 |
| `CLUSTER`, `VACUUM FULL`, plain `REINDEX`, non-`CONCURRENTLY` `CREATE/DROP INDEX` | `ACCESS EXCLUSIVE` (the repo's own docs warn a missing index rebuilt without `CONCURRENTLY` blocks inserts for hours) | not recommended |
| **Restart** for `shared_buffers`/`wal_buffers` | not a lock, but every connection drops and in-flight batches roll back | 4.1 |

Use `SET lock_timeout = '5s'` before any `ALTER TABLE` so a statement fails fast instead of queueing behind a crawler transaction and blocking everyone behind it.

---

## 2. Environment and workload (measured)

### 2.1 Hardware and server

| Item | Value | Source |
|---|---|---|
| DB host | `137.28.211.66:5432` (`db01`), RHEL, PostgreSQL **13.23** (x86_64) | `version()` |
| CPU | Intel Core i7-6700, 4 cores / 8 threads; load average 6.3 / 6.6 / 6.1 | `/proc/cpuinfo`, `/proc/loadavg` |
| RAM | 32,477,052 kB (31 GiB); page cache 29.3 GB; swap 8 GB; `Dirty` 2.9–3.1 GB steady | `/proc/meminfo` |
| Disk | **one `ST2000DM001` (Seagate Barracuda 2 TB, `rotational=1`)**, XFS on `/dev/sdb1` mounted at `/mnt/harddrive`; data dir `/mnt/harddrive/postgres/data` (WAL inside it); I/O scheduler `mq-deadline` | `/proc/mounts`, `/sys/block/sdb` |
| DB size | 16 GB (10 GB of it indexes), fully fits in the OS page cache | `pg_database_size` |
| Uptime | postmaster started 2026-11-20 22:49 | `pg_postmaster_start_time()` |
| Clients | 12 distinct client IPs; **all connect as the `postgres` superuser** | `pg_stat_activity` |

Live sampling over 120 s (06:23:14 → 06:25:14):

| Metric | Value |
|---|---|
| Disk writes (`/proc/diskstats`, `sdb`) | 4,475,424 sectors = **2.13 GB → 17.8 MB/s, 234 write IOPS** |
| Disk busy (`io_ticks` delta) | 116,323 ms in 120 s = **≈ 97 % utilisation** |
| Disk reads | 14 IOPS, 16 MB total (everything is served from page cache) |
| CPU | user+sys 4.7 %, **iowait 46 %**, idle 48 % |
| WAL generated | 694 MB / 120 s = **≈ 5.5 MB/s** |
| Commits | 444 / 120 s ≈ **3.7 per second** |

Row ingest, 60 s delta (06:20:17 → 06:21:17):

| Table | rows / min | share of posting rows |
|---|---|---|
| `trigram_urls` | 393,643 | 46.8 % |
| `word_urls` | 183,872 | 21.9 % |
| `bigram_urls` | 149,834 | 17.8 % |
| `prefix_urls` | 113,193 | 13.5 % |
| `urls_references` | 55,262 | — |
| `url_queue` (inserts) | 87,936 | — |
| `logs` | 5,214 | — |

Total ≈ 840,500 posting rows/min ≈ 14,000 rows/s. With the WAL rate that is **≈ 392 bytes of WAL per posting row**,
against ≈ 40–50 bytes of actual heap data per row (see 5.2).
Indexed pages grew 115,403 → 130,795 in 25 min ≈ **616 pages/min** (`count(total_tokens)` on `urls`).

### 2.2 Settings that matter (all `default`/`configuration file`; nothing is tuned)

| Setting | Current | Comment |
|---|---|---|
| `shared_buffers` | **128 MB** (16384 × 8 kB) | 0.4 % of RAM. See 4.1 |
| `effective_cache_size` | 4 GB (default) | actual page cache is 29 GB |
| `work_mem` | 4 MB | `temp_bytes` = **1.137 TB** in **57,211** temp files (avg 19.9 MB) since reset |
| `maintenance_work_mem` | 64 MB | also what autovacuum uses (`autovacuum_work_mem = -1`) |
| `random_page_cost` / `seq_page_cost` | 4 / 1 | correct for a spinning disk — leave |
| `effective_io_concurrency` | 1 | fine for one spindle |
| `max_wal_size` / `min_wal_size` | 1 GB / 80 MB | WAL fills 1 GB in ≈ 3 min at 5.5 MB/s |
| `checkpoint_timeout` / `checkpoint_completion_target` | 5 min / 0.5 | |
| `wal_compression`, `wal_buffers` | off, 4 MB | `full_page_writes = on` |
| `jit` | on (`llvmjit`) | **no plan I captured used JIT** (all costs < 100,000) — see 4.10 |
| `track_io_timing` | **off** | `blk_read_time`/`blk_write_time` are 0, so I/O wait cannot be attributed per query |
| `max_connections` | 100 (3 reserved) | **92 → 108 connections observed; clients refused repeatedly** |
| `autovacuum_*` | naptime 60 s, 3 workers, `vacuum_cost_limit` 200 (shared by all workers), `cost_delay` 2 ms, scale factors 0.2 / 0.1, `insert_scale_factor` 0.2 | see 4.2 |
| `max_parallel_workers(_per_gather)` | 8 / 2 | search sessions set 2 per gather |
| `bgwriter_lru_maxpages` / `bgwriter_delay` | 100 / 200 ms | ≤ 500 pages/s: the disk absorbs ~2,300 pages/s |
| `log_lock_waits`, `log_checkpoints`, `log_min_duration_statement` | off, off, off | no evidence is being recorded |
| `fsync`, `synchronous_commit`, `data_checksums` | on, on, off | |

### 2.3 Buffer-cache and write-behaviour evidence

* `pg_stat_database`: `blks_hit` 450.4 G vs `blks_read` 208.0 G ⇒ **68 % PostgreSQL-level hit ratio** (the OS cache hides most of the misses).
* `pg_stat_bgwriter`: `buffers_backend` 492,577,395 · `buffers_clean` 123,481,732 · `buffers_checkpoint` 8,391,832 ⇒ **backends write 78.9 %,
  the bgwriter 19.8 %, the checkpointer 1.3 %** of all buffers. Backends only write when `shared_buffers` is too small to hold their working set.
* Read-only statements write pages: `SELECT count(total_tokens), avg(...) FROM urls` showed `read=3287 written=2176`; the df `count(*)` showed
  `read=2023 written=1721`; `SELECT COUNT(*) FROM urls` (dashboard) `written=2559`.
* `temp_bytes` grows ≈ 0.5 MB/s even now (58.6 MB in 120 s): sorts and hashes spill at 4 MB `work_mem`.
* `deadlocks` = 8,917 since 2026-03-14 (≈ 35 / day). `xact_rollback` is 15.7 % of transactions (mostly connections closed without commit after read queries; harmless).

---

## 3. Search: what the actual statements do

I imported `search.py` unchanged, ran `_bm25_search` for each query through a wrapper that first ran
`EXPLAIN (ANALYZE, BUFFERS)` on every `SELECT` it issues (same parameters, same session options: `work_mem=64MB`, 2 parallel workers) and then
executed it once more (the "warm" figure). Statement order is the order `search.py` issues them with empty caches.

### 3.1 Per-statement timings (ms; *first* = EXPLAIN run on a cold-ish cache, *warm* = immediate re-run)

| Query | #1 N/avgdl (`count(total_tokens)`) | #2 weights | #3 df `count(*)` per word | #4 ranking | #5 title/host scan | typo range scans |
|---|---|---|---|---|---|---|
| `linux kernel` | 16 / 13 | 0.04 | **87** / 1.6 | **111** / 32 | **66** / 67 | – |
| `open source operating system` | **152** / 19 | 0.03 | **988** / 7 | **306** / 277 | **90** / 85 | – |
| `go programming` | 16 / 13 | 0.01 | **866** / 3.7 | **126** / 121 | **65** / 68 | – |
| `wikipedia` | 16 / 14 | 0.01 | 0.4 / 0.8 | 8.5 / 9.9 | **58** / 60 | – |
| `kernal` (a real word with 3 postings) | 16 / 13 | 0.01 | 0.1 | 0.09 | 58 / 64 | – |
| `zzzqxj` (no such word) | 15 / 14 | 0.01 | 15.6 / 0.9 | – | 68 / 57 | 2 × 0.02 |
| `machine learning research data` (extra) | – | – | – | **566 … 4,437** over 8 runs | – | – |

In production the in-process caches (`_load_stats`, `_df_cache`, result LRU) remove #1–#3 on repeat queries, so the **steady-state
cost is #4 + #5**, and the **first query for each word pays #3**.

### 3.2 Findings from the plans

1. **#5 title/host `LIKE` scan** (`Parallel Seq Scan on urls`, `Rows Removed by Filter: 58,621`, 3,382 buffers) is the biggest
   fixed cost: 58–90 ms with 3 processes, for **every** uncached query, even `zzzqxj`. It cannot use an index (`lower(title) LIKE '%w%'`).
   With `NAV_MAX_DOCS = 200000` it is skipped above 200k indexed pages: **130,795 at 06:42, +616/min ⇒ ≈ 08:30 DB time**. After that the
   title/host candidates silently vanish from results. See 4.5.
2. **#3 df `count(*)`** is an `Index Only Scan` on `word_urls_word_id_url_id_count_idx` that must read *every* posting of the word
   (13,720 entries each for `open`, `operating`, `source`, `system`) and showed **`Heap Fetches: 2131`**: the visibility map is stale
   because vacuum is behind (4.2). Cold it costs 247–433 ms *per word*, with `written=1721` pages (the backend flushing dirty
   buffers it had to evict). Warm it is 1–7 ms. A 1-row lookup in a precomputed table would be sub-millisecond (4.4).
3. **#4 ranking** is one nested loop: index-only scan of the word postings → **one `urls_pkey` probe per posting** (54,880 probes for
   the 4-word query, 4 buffers each ⇒ 219,155 buffer hits) → `Sort` by `u.id` (12.7 MB, in `work_mem`, no spill) → `GroupAggregate` →
   top-N. Roughly 90 ms of 306 is the urls probes, 20 ms the sort, 175 ms the aggregate arithmetic. The aggregate is slow partly because
   psycopg2 renders `1.2`, `0.75`, `6284.87…` as **`numeric`** literals, so the BM25 denominator is evaluated in `numeric`
   (`((count)::numeric + (1.2 * (0.25 + ((0.75 * (total_tokens)::numeric) / 6284.87))))::double precision`).
   Cost is **linear in the candidate count**: 4,826 (linux kernel) → 21,379 (go programming) → 54,879 (4 words) → 96,928 (machine learning research data).
   All words are below `SEARCH_HEAVY_DF = 100000` (the most common word, `new`, has ≈ 46,500 postings), so the "probe, don't scan"
   branch never triggers at the current corpus size, and every word's whole posting list is scanned.
4. **Planner misestimates are large but harmless today.** Estimated `rows=1` for the join where actual is 4,826–96,928, and
   `rows=5` per word where actual is 24,000. Cause: `pg_stats` for `word_urls.word_id` says `n_distinct = 19,577` but there are ≥ 1.43 million words
   (and `url_id` says 16,201 distinct vs ~131,000 indexed pages) — the sampling estimator is badly fooled by data that is physically ordered by `url_id`
   (`correlation` 0.9995). The shape is right because the only sensible access path is the index. It would matter for any
   future join-order choice (4.7).
5. **Heap fetches are the cold-latency driver.** In the 4.4 s run of `machine learning research data`, the index-only scan reported
   `Heap Fetches: 9,806` of 96,931 entries (10 %), each a random heap read on a busy HDD, and one word's loop took 983 ms.
   `relallvisible` for `word_urls` is 32,634 of 40,467 *vacuumed-time* pages; `n_ins_since_vacuum` = 26.2 M rows (88 % of the table's live rows).
6. **Typo correction is cheap for rare prefixes, not for common ones.** `zzzqxj`: 0.017 ms. For a common 2-letter prefix
   (`co`, lengths 7–9, as for the typo `comunity`): **281 ms** (`Index Only Scan using words_pkey`, 37,260 index entries
   scanned, 7,858 kept, `Heap Fetches: 5149`, estimated `rows=188`). Currently below the `LIMIT 20000`, so results are not truncated yet.
7. **No JIT** appears in any plan (costs 30–19,767, threshold 100,000). **No sort spills** in the search statements at `work_mem = 64MB`.
8. **Search never uses the `url_id` indexes** (all four plans use only `*_url_id_count_idx`; their `idx_scan` counters grow at ≈ 12.7/s,
   in lock step with the crawler's `DELETE ... WHERE url_id = ANY(...)`, see 5.3) **and does not use the bigram/trigram/prefix tables at all**
   in the default `bm25` mode.

### 3.3 `pg_stat_user_indexes` usage (lifetime, with 22-minute deltas)

| Index | size | `idx_scan` @06:17 | `idx_scan` @06:39 | Reader |
|---|---|---|---|---|
| `word_urls_word_id_url_id_count_idx` | 1229 MB | 2,535 | 4,311 | search (+ my EXPLAIN runs) |
| `trigram_urls_trigram_id_url_id_count_idx` | **2981 MB** | 10,293 | **10,293** | legacy/fuzzy modes, tests |
| `bigram_urls_bigram_id_url_id_count_idx` | 1128 MB | 1,975 | **1,975** | legacy mode, tests |
| `prefix_urls_prefix_id_url_id_count_idx` | 813 MB | 922 | **922** | legacy mode, fuzzy, tests |
| `idx_word_urls_url_id` | 287 MB | 39,274 | 56,094 | crawler `DELETE` only |
| `idx_trigram_urls_url_id` | 634 MB | 38,505 | 54,919 | crawler `DELETE` only |
| `idx_bigram_urls_url_id` | 268 MB | 42,246 | 58,630 | crawler `DELETE` only |
| `idx_prefix_urls_url_id` | 193 MB | 39,135 | 55,519 | crawler `DELETE` only |

### 3.4 What I tried (read-only): `float8` casts

I rewrote the ranking statement's literals as `(…)::float8` and re-ran it 4× each (same session, same parameters):
`go programming` ≈ 126 → ≈ 99 ms (4 runs each: 129/126/124/144 → 95/98/100/103 ms), `linux kernel` ≈ 28 → ≈ 22 ms (30/28/27/28 → 23/24/23/21 ms).
For 4–5-word queries disk contention made the numbers unusable (311–2,685 ms vs 234–1,350 ms). Treat **≈ 20–25 % on the ranking statement** as the
reliable figure. This is an application change (`search.py`), listed here because it is free.

---

## 4. Detailed recommendations

Priorities: **P0** = do first, high impact and low risk; **P1** = clear win; **P2** = worthwhile; **P3** = hygiene / when convenient.
"Fleet at 0" means scale every node to 0 crawlers on the dashboard first.

### 4.1 Server configuration (P0)

**Problem.** The server runs on stock settings on a 32 GB / spinning-disk machine. `shared_buffers = 128 MB` makes backends evict and write dirty pages themselves (79 % of
buffer writes), checkpoints are forced by `max_wal_size = 1 GB` (5.5 MB/s fills it in ≈ 3 min) and each checkpoint re-emits full-page images of the randomly-touched
index leaves (392 B WAL per posting row vs ≈ 50 B of heap payload), `work_mem = 4 MB` produces 1.1 TB of temp-file traffic, vacuum uses 64 MB, and `track_io_timing`/lock/checkpoint logging are off so none of this is visible per query.

**Evidence.** §2.1–2.3 above (disk 97 % busy at 17.8 MB/s and 234 write IOPS; `buffers_backend` 78.9 %; `temp_bytes` 1.137 TB; `blk_*_time` = 0).

**SQL** (run as superuser; `ALTER SYSTEM` writes `postgresql.auto.conf`; values assume this 32 GB host; **only the two marked lines need a restart**):

```sql
-- memory
ALTER SYSTEM SET shared_buffers = '8GB';              -- RESTART. ~25 % of RAM; keeps the hot posting-list index pages and removes backend write stalls
ALTER SYSTEM SET effective_cache_size = '22GB';       -- planner hint only; matches the 29 GB page cache
ALTER SYSTEM SET maintenance_work_mem = '2GB';        -- index builds / manual VACUUM
ALTER SYSTEM SET autovacuum_work_mem = '1GB';         -- avoids multi-pass index vacuums
ALTER SYSTEM SET work_mem = '8MB';                    -- default for all sessions (was 4MB); search already asks for 64MB per session
-- WAL / checkpoints: fewer, smoother checkpoints, fewer full-page images
ALTER SYSTEM SET max_wal_size = '16GB';               -- needs 16 GB free on the data disk: check `df` first
ALTER SYSTEM SET min_wal_size = '2GB';
ALTER SYSTEM SET checkpoint_timeout = '15min';
ALTER SYSTEM SET checkpoint_completion_target = 0.9;
ALTER SYSTEM SET wal_compression = on;                -- CPU is 48 % idle; cuts full-page-image bytes (typically 30–50 %)
ALTER SYSTEM SET wal_buffers = '16MB';                -- RESTART
-- background writer: be able to keep up with ~2,300 pages/s instead of ≤ 500
ALTER SYSTEM SET bgwriter_lru_maxpages = 1000;
ALTER SYSTEM SET bgwriter_delay = '50ms';
-- visibility into what is going on
ALTER SYSTEM SET track_io_timing = on;                -- negligible overhead on modern CPUs; run pg_test_timing once to confirm
ALTER SYSTEM SET log_checkpoints = on;
ALTER SYSTEM SET log_lock_waits = on;                 -- logs every wait > deadlock_timeout (1 s): proves the convoys in 5.1
ALTER SYSTEM SET log_autovacuum_min_duration = '30s';
ALTER SYSTEM SET log_temp_files = '32MB';
ALTER SYSTEM SET log_min_duration_statement = '2000ms';
ALTER SYSTEM SET jit = off;                           -- see 4.10; zero downside for this workload
SELECT pg_reload_conf();                              -- everything except the RESTART lines takes effect now
```

**Expected impact (estimate).** Search: removes the "SELECT writes 1,700 pages" stalls and keeps the `word_urls` index hot (the df and
ranking statements' *cold* costs are dominated by exactly those reads/evictions). Crawl: fewer forced checkpoints and ~30–50 % smaller WAL
shrinks the random-write burst; I would expect a visible drop in `DataFileWrite`/`WALWrite` waits, **but the single spinning disk remains the ceiling** (see §1: SSD for `pg_wal`).
I cannot promise a throughput number without trying it.

**Cost / risk.**
* `shared_buffers`/`wal_buffers` need a **restart**: all ~80 crawler connections drop and in-flight transactions roll back (crawlers re-queue via
  claim timeout, but better to scale the fleet down for it). With `huge_pages = try` and 8 GB the server will use huge pages if configured, else normal.
* A larger `max_wal_size`/`checkpoint_timeout` means **crash recovery replays up to ~16 GB of WAL**: on this HDD expect several minutes to
  tens of minutes of startup after a crash. Pick `8GB`/`10min` if that is unacceptable.
* `wal_compression` costs CPU; there is 48 % idle.
* `work_mem = 8MB` × 100 connections × several nodes is still < 4 GB worst case.
* All of these are reversible with `ALTER SYSTEM RESET <name>` + reload/restart.

**OS-level companions (outside PostgreSQL):** `vm.dirty_background_bytes = 268435456`, `vm.dirty_bytes = 1073741824` (the kernel is holding 3 GB of dirty pages and flushes them in bursts to a
disk that is already at 97 %); mount `/mnt/harddrive` with `noatime`.

**Priority: P0.**

### 4.2 Autovacuum tuning for the posting tables, `urls` and `url_queue` (P0)

**Problem.**
1. Posting tables are append-mostly, so their visibility map is only set by vacuum. Vacuum is throttled (`cost_limit` 200 *shared* by 3 workers,
   `cost_delay` 2 ms) and the disk is saturated, so it never catches up: `word_urls` vacuum has been running for **26 min** (06:15 → 06:42, still running) and its
   previous completion was 05:08 with **26.2 M rows inserted since**. Every index-only search scan then pays heap fetches (760 → 9,897 per statement).
2. The tables' `reltuples` are badly stale because they are only refreshed by those vacuums: `word_urls` 7.47 M vs 29.6 M live (−75 %),
   `trigram_urls` 15.9 M vs 59.9 M, `url_queue` 69,720 vs 619,705 at one moment. `fast_crawl.py`'s `queue_estimate()` reads `pg_class.reltuples`, so the
   crawler's queue-size view is wrong by an order of magnitude. `word_urls` was last *manually* analyzed on 2026-11-10 (`analyze_count = 1`).
3. `url_queue` is a classic queue-bloat case: **42 % dead tuples** (457,675 dead vs 619,705 live at 06:17), 320 autovacuums, 4.85 M sequential scans reading 7.03 × 10¹² tuples lifetime,
   and `heap_blks_read` 102 G at a 12 % hit rate. The claim query's index-only scan on `url_queue_unclaimed_idx` took **763 ms** with 2,056 buffers and `Heap Fetches: 5,935` for **100 rows**
   (the dead index entries at the queue head have to be skipped), and a second sample read 2,057 buffers for 10 rows.
4. `urls` is updated constantly (`n_tup_upd` 32 M; `reference_count` bumps), only 296 of 3,376 pages are all-visible (9 %).

**SQL** (per-table reloptions take `SHARE UPDATE EXCLUSIVE`: reads and writes continue; a running autovacuum on that table is cancelled and restarts with the new options.
Use `SET lock_timeout = '5s'`):

```sql
SET lock_timeout = '5s';

-- append-mostly posting tables: vacuum after ~2 % new rows instead of 20 %, give vacuum a real I/O budget, keep stats fresh
ALTER TABLE word_urls    SET (autovacuum_vacuum_insert_scale_factor = 0.02, autovacuum_vacuum_scale_factor = 0.05,
                              autovacuum_analyze_scale_factor = 0.02, autovacuum_vacuum_cost_limit = 2000);
ALTER TABLE bigram_urls  SET (autovacuum_vacuum_insert_scale_factor = 0.02, autovacuum_vacuum_scale_factor = 0.05,
                              autovacuum_analyze_scale_factor = 0.02, autovacuum_vacuum_cost_limit = 2000);
ALTER TABLE trigram_urls SET (autovacuum_vacuum_insert_scale_factor = 0.02, autovacuum_vacuum_scale_factor = 0.05,
                              autovacuum_analyze_scale_factor = 0.02, autovacuum_vacuum_cost_limit = 2000);
ALTER TABLE prefix_urls  SET (autovacuum_vacuum_insert_scale_factor = 0.02, autovacuum_vacuum_scale_factor = 0.05,
                              autovacuum_analyze_scale_factor = 0.02, autovacuum_vacuum_cost_limit = 2000);

-- the queue: vacuum every ~5,000 dead rows with no throttling, so the head of the partial index stays clean
ALTER TABLE url_queue SET (autovacuum_vacuum_scale_factor = 0.0, autovacuum_vacuum_threshold = 5000,
                           autovacuum_vacuum_cost_delay = 0, autovacuum_vacuum_cost_limit = 2000,
                           autovacuum_analyze_scale_factor = 0.02);

-- urls: frequent small vacuums keep the visibility map and hint bits fresh (it is only ~30 MB)
ALTER TABLE urls SET (autovacuum_vacuum_scale_factor = 0.02, autovacuum_vacuum_cost_delay = 0, autovacuum_analyze_scale_factor = 0.02);

-- global: workers share one budget today, so 3 busy workers get ~67 units each
ALTER SYSTEM SET autovacuum_vacuum_cost_limit = 1000;
ALTER SYSTEM SET autovacuum_naptime = '15s';
SELECT pg_reload_conf();
```

**Expected impact.** `Heap Fetches` in search scans drop from thousands to hundreds (the unvacuumed tail shrinks from ~26 M rows to ~0.6 M for `word_urls`); `url_queue` claims go
from up to 763 ms (a 100-row claim wading through ~2,000 buffers of dead index entries) to the ~1 ms a clean index head gives (my `LIMIT 10` sample took 0.94 ms only because those 2,057 buffers were already cached; with a vacuumed head the same scan touches a handful of pages);
`reltuples` stays within a few percent, so `queue_estimate()` becomes meaningful; plans use current statistics.

**Cost / risk.** **A genuine trade-off on HDD:** every vacuum of a posting table also scans all of its indexes in full when it finds dead tuples (≈ 3 % of rows are deleted by re-crawls, so it
always does), e.g. ≈ 3.1 GB for `trigram_urls`. Vacuuming 10× more often multiplies that I/O. Mitigations: (a) apply 4.3 first, which removes ~78 % of this work;
(b) start with `insert_scale_factor = 0.05` and watch `log_autovacuum_min_duration` output (set in 4.1); (c) the `DELETE`-for-fresh-urls fix in 5.3 removes most of the dead tuples.
If the disk gets worse rather than better, raise `autovacuum_vacuum_cost_delay` back to 2 ms on the big tables. Reversible with `ALTER TABLE ... RESET (...)`. **No fleet downtime needed.**

**Priority: P0.**

### 4.3 The bigram / trigram / prefix tables (P0 decision, biggest write-path lever)

**Problem.** Default `SEARCH_MODE=bm25` generates candidates from **word postings only**; the n-gram tables are read only by the
legacy/fuzzy modes (`SEARCH_MODE=legacy|exact`, `SEARCH_FUZZY=1`, both off by default), `tests/` and benchmarks. Yet they are:

| | `trigram_urls` | `bigram_urls` | `prefix_urls` | all three | `word_urls` (for comparison) |
|---|---|---|---|---|---|
| live rows | 59.96 M | 23.60 M | 17.00 M | 100.6 M | 26.55 M |
| size (heap + indexes) | 6324 MB | 2449 MB | 1774 MB | **10,547 MB (10.3 GiB, 64 % of the 16 GB DB)** | 2725 MB |
| token-first covering index | 2981 MB | 1128 MB | 813 MB | 4922 MB | 1229 MB |
| rows written / min | 393,643 | 149,834 | 113,193 | **656,670 = 78 % of all posting rows** | 183,872 |
| `idx_scan` between 06:17 and 06:39 | **0** | **0** | **0** | | search uses it |

So ~78 % of the crawl's posting write volume (and, by the WAL-per-row estimate in 5.2, roughly the same share of the 5.5 MB/s WAL and of the
saturated disk), 64 % of the disk, and most of every vacuum and backup go to data the live search path does not read.
Also note `total_tokens` is defined as the sum over **all four** token kinds (`write_pages`), so changing what is stored changes `dl` — BM25 only uses `dl/avgdl`, so it
scales consistently as long as the writer and `avgdl` stay on the same definition, but check this before deploying.

**Options** (choose one; this is a product decision, I cannot know whether you want to keep legacy/fuzzy search):

* **A (recommended if legacy/fuzzy are retired):** change the writers (`utils.write_pages`, `crawl_lib/db.py`) to stop inserting n-gram rows. Then, in a quiet moment, reclaim the space:
  ```sql
  -- >>> Do NOT run without scaling the crawler fleet to 0 first <<<  (TRUNCATE takes ACCESS EXCLUSIVE; writers still inserting would block or fail)
  TRUNCATE bigram_urls, trigram_urls, prefix_urls;        -- instant, returns ~10 GiB, resets autovacuum debt
  ```
  (or `DROP TABLE` once nothing references them; `add_search_indexes.py`, `add_count_columns.py` and the tests list them). Pages indexed before the change keep a `total_tokens` that includes n-grams; re-crawl or ignore (it's a small constant-ish factor).
* **B (keep the data, drop the cost of indexing it):** drop only the three token-first covering indexes (leaves legacy/fuzzy modes working only by sequential scan, i.e. effectively off):
  ```sql
  DROP INDEX CONCURRENTLY trigram_urls_trigram_id_url_id_count_idx;   -- 2981 MB
  DROP INDEX CONCURRENTLY bigram_urls_bigram_id_url_id_count_idx;     -- 1128 MB
  DROP INDEX CONCURRENTLY prefix_urls_prefix_id_url_id_count_idx;     --  813 MB
  ```
  Saves ≈ 4.8 GiB and the random-leaf-write cost of the most expensive indexes; **rebuilding them later is hours** (`add_search_indexes.py` says so itself).
  `CONCURRENTLY` drops need no fleet downtime.
* **C (keep everything):** at least apply 4.1/4.2 and the sorted-COPY change in 5.4.

**Expected impact (estimate).** Option A removes ≈ 78 % of posting rows, ≈ 64 % of disk, and the dominant share of WAL/dirty pages/vacuum I/O on a disk that is the bottleneck: the
largest single improvement available to **crawl throughput**. Search is unaffected in `bm25` mode.

**Cost / risk.** Loses legacy/fuzzy/typo-by-trigram capability (the code paths would need to be removed or guarded, otherwise `SEARCH_MODE=legacy` returns nothing or errors). Option A's `TRUNCATE` is irreversible
without a re-crawl. Option B is cheap to try (but expensive to undo). **Priority: P0 (decision) / large impact.**

### 4.4 `word_df` and `corpus_stats` summary tables (P1)

**Problem.** `search._DF_SQL` runs `SELECT count(*) FROM word_urls WHERE word_id = ...` per query word to get `df` for idf. It is cached 10 minutes per process,
but every process and every new word pays it: measured **87 / 988 / 866 ms** cold for 2 / 4 / 2 words (`Heap Fetches` 119 / 2,131 / 777), 1–7 ms warm.
At 10× the corpus, a common word's `count(*)` reads 10× more index entries. `_load_stats` similarly seq-scans `urls` every 10 minutes (13–152 ms now; it falls back to `reltuples`
after 5 s, i.e. becomes approximate silently, at ~25M+ pages).

**SQL** (new tables only; nothing existing is altered):

```sql
CREATE TABLE word_df (
    word_id      int  PRIMARY KEY,
    df           int  NOT NULL,
    refreshed_at timestamptz NOT NULL DEFAULT now()
) WITH (fillfactor = 70, autovacuum_vacuum_scale_factor = 0.05);

CREATE TABLE corpus_stats (
    id           boolean PRIMARY KEY DEFAULT true CHECK (id),
    n_docs       bigint NOT NULL,
    avgdl        double precision NOT NULL,
    refreshed_at timestamptz NOT NULL DEFAULT now()
);

-- batch refresh, from cron every 30–60 min (one index-only scan of the covering index + a 1.4 M-group hash aggregate)
SET work_mem = '256MB';
INSERT INTO word_df (word_id, df)
SELECT word_id, count(*) FROM word_urls GROUP BY word_id
ON CONFLICT (word_id) DO UPDATE SET df = EXCLUDED.df, refreshed_at = now()
WHERE word_df.df IS DISTINCT FROM EXCLUDED.df;            -- only touch rows that changed: keeps the table HOT/small

INSERT INTO corpus_stats (n_docs, avgdl)
SELECT count(total_tokens), COALESCE(avg(total_tokens), 1) FROM urls
ON CONFLICT (id) DO UPDATE SET n_docs = EXCLUDED.n_docs, avgdl = EXCLUDED.avgdl, refreshed_at = now();

-- lookup used by search (replaces _DF_SQL); words created since the last refresh fall back to the exact count, which is cheap for rare words
SELECT w.word, w.id,
       COALESCE(d.df, (SELECT count(*) FROM word_urls wu WHERE wu.word_id = w.id)) AS df
FROM words w LEFT JOIN word_df d ON d.word_id = w.id
WHERE w.word = ANY(%(words)s);
```

An incremental alternative for huge tables: add counts only for postings with `url_id > :last_max_url_id` (uses `idx_word_urls_url_id`) and do the full recompute nightly (re-crawl deletes make
incremental drift slightly).

**Expected impact.** First-touch df cost 0.25–1 s → < 1 ms; `_load_stats` 13–152 ms → < 1 ms and exact at any size. BM25 idf accuracy is unchanged up to the refresh interval (idf is a log, tiny drift).
**Cost / risk.** Table is ~1.4 M rows × ~40 B ≈ 60 MB. Refresh estimate on this box: 20–60 s of index-only scan (est.) off-peak; at 8× the data, minutes; the `IS DISTINCT FROM` clause keeps write volume to the changed words.
Needs a small change to `search._word_dfs`/`_load_stats` (app side). No locks on existing tables. **Priority: P1.**

### 4.5 GIN full-text index for the title/hostname path (P1; time-sensitive)

**Problem.** See 3.2 #1: 58–90 ms parallel seq scan of `urls` per uncached query, plus the self-disabling cliff at 200k pages (≈ 08:30 today). `pg_trgm` would be the usual answer, but **contrib is
not installed on this server** (and `LIKE '%w%'` is more general than the app needs: `score_nav` keeps only *whole-word* matches in the title and *hostname tokens*). The built-in
`tsvector` machinery handles whole words with no extension.

**I validated the semantics read-only.** For the query words `kernel | linux`, the whole-word set (`title ~ '(^|[^0-9a-z])(kernel|linux)([^0-9a-z]|$)'` or the same on the host) has **354** pages, the proposed
expression below returns **354** and misses **0** (the current SQL's substring `LIKE` returns 433 rows; the 79 extra are substring-only hits that `score_nav`'s whole-word check would discard). A first draft that did not normalize title punctuation missed 12, so keep the `regexp_replace` on both parts.

**SQL.**

```sql
-- no fleet downtime: CONCURRENTLY. No WHERE predicate on purpose: urls rows are updated constantly (reference_count, total_tokens) and a predicate on total_tokens would defeat HOT updates.
CREATE INDEX CONCURRENTLY urls_nav_fts_idx ON urls USING gin (
    to_tsvector('simple',
        regexp_replace(lower(coalesce(title, '')), '[^0-9a-z]+', ' ', 'g') || ' ' ||
        regexp_replace(lower(substring(url from '^(?:https?://)?([^/?#:]+)')), '[^0-9a-z]+', ' ', 'g'))
);

-- replacement for _NAV_SQL (keep the `total_tokens IS NOT NULL` filter; the Python whole-word check stays as is). %(q)s = e.g. 'kernel | linux'
SELECT url, title, total_tokens, COALESCE(reference_count, 0)
FROM urls
WHERE total_tokens IS NOT NULL
  AND to_tsvector('simple',
        regexp_replace(lower(coalesce(title, '')), '[^0-9a-z]+', ' ', 'g') || ' ' ||
        regexp_replace(lower(substring(url from '^(?:https?://)?([^/?#:]+)')), '[^0-9a-z]+', ' ', 'g'))
      @@ to_tsquery('simple', %(q)s)
ORDER BY reference_count DESC NULLS LAST, id
LIMIT 100;
```

(The query expression must match the index expression exactly. Build the `to_tsquery` string from the already-validated `[a-z0-9]+` words joined with ` | `.) Non-ASCII words: `score_nav` matches CJK by substring; keep the old `LIKE` path for non-ASCII queries.

**Expected impact.** ≈ 58–90 ms → low single-digit ms per uncached query (estimate: bitmap scan of a GIN with a few hundred matches + top-100 sort), and `SEARCH_NAV_MAX_DOCS` can be raised/removed.
**Cost / risk.** GIN with `fastupdate`: `urls` receives ~1.1k inserts/min, each adds ~10 lexemes to the pending list (merged by autovacuum): negligible write cost; size is a few MB now and low single-digit GB at
~100 M pages (estimate). Build time: one sequential scan of `urls` plus tsvector evaluation (the evaluation alone took 0.76 s for 207k rows across 3 processes under load), so seconds now, tens of minutes at 100 M rows (estimate). `CREATE INDEX CONCURRENTLY` takes two scans and waits for old transactions; if it fails it leaves an `INVALID` index which must be dropped (`DROP INDEX CONCURRENTLY urls_nav_fts_idx`). **Priority: P1.**

### 4.6 Connection pooling (P1)

**Problem.** `max_connections = 100`; I observed 92 → 108 backends (82 idle in `ROLLBACK`/`COMMIT` state, 21 active) and my own read-only sessions were refused with `FATAL: sorry, too many clients already`
repeatedly (≥ 15 refused attempts in total, in bursts of up to 8 retries 10–20 s apart, over roughly 15 minutes), which means the search frontend and dashboard can be refused too. There are ~80 crawler connections for a machine with 8 threads and one disk; the
disk-bound write path does not benefit from that many writers (§5.1: 5–8 of ~12 active sessions are blocked on locks), and each backend costs memory.

**Proposal.** pgbouncer on `db01` in front of PostgreSQL, **transaction pooling for the crawlers** (they use short transactions, `SAVEPOINT`/`SET LOCAL` inside a transaction are fine),
a small separate pool for the search app (which sets `work_mem` and `statement_timeout` as *startup options*, which pgbouncer does not forward; use a dedicated role with `ALTER ROLE search_ro SET work_mem = '64MB'` and
`statement_timeout = '5s'`, or `connect_query` on a dedicated pgbouncer database). Sketch:

```ini
[databases]
stultus        = host=127.0.0.1 port=5432 dbname=stultus pool_size=24        ; crawlers + dashboard
stultus_search = host=127.0.0.1 port=5432 dbname=stultus pool_size=8 \
                 connect_query='SET work_mem=''64MB''; SET statement_timeout=5000'

[pgbouncer]
pool_mode = transaction
max_client_conn = 1000
default_pool_size = 24
ignore_startup_parameters = extra_float_digits,options
server_idle_timeout = 60
```

**Cost / risk.** One more moving part; `psycopg2` features that need session state (`SET` without `LOCAL`, advisory locks, `LISTEN`, server-side cursors) break in transaction mode: grep shows the crawler/search code uses none of these, but test with one
crawler box first. Also consider **separate roles** (`crawler`, `search_ro` with `SELECT` only, `dashboard_ro`): every client is the `postgres` superuser today, so `search.py`'s "never writes" is not enforced by the database. **Priority: P1.**

### 4.7 Statistics quality (P2)

**Problem.** `pg_stats` for the posting tables (sample of 30k rows) badly under-estimates distinct counts because the data is clustered by `url_id`:

| Table.column | `n_distinct` in `pg_stats` | Actual (≥) | `correlation` |
|---|---|---|---|
| `word_urls.word_id` | 19,577 | 1.43 M words exist | 0.017 |
| `word_urls.url_id` | 16,201 | ≈ 131,000 indexed pages | 0.9995 |
| `bigram_urls.url_id` | 32,011 | ≈ 131,000 | 0.9995 |
| `trigram_urls.url_id` | 17,191 | ≈ 131,000 | 0.9998 |
| `prefix_urls.url_id` | 22,563 | ≈ 131,000 | 0.9992 |

Consequences seen in plans: `rows=1466`/`rows=5` per word and `rows=1` for a 54,880-row join. Plans are currently right because there is one sensible access path.
`word_urls.word_id` has the MCV list at the default target 100, so only the top 100 words have exact frequencies.

**SQL** (cheap, no sampling cost; `SHARE UPDATE EXCLUSIVE`; resets only at next ANALYZE):

```sql
-- positive = absolute count, negative = fraction of rows. Words grow slowly (Heaps' law) so use an absolute number and refresh it occasionally.
ALTER TABLE word_urls ALTER COLUMN word_id SET (n_distinct = 1400000);
-- pages: rows per page is ~constant (~230 word postings, ~520 trigram, ~200 bigram, ~147 prefix), so a fraction scales as the table grows
ALTER TABLE word_urls    ALTER COLUMN url_id SET (n_distinct = -0.0043);
ALTER TABLE trigram_urls ALTER COLUMN url_id SET (n_distinct = -0.0019);
ALTER TABLE bigram_urls  ALTER COLUMN url_id SET (n_distinct = -0.0050);
ALTER TABLE prefix_urls  ALTER COLUMN url_id SET (n_distinct = -0.0068);
ANALYZE word_urls;   -- (autoanalyze will pick it up; run manually only if you want it now)
```

I would **not** raise `default_statistics_target`/`SET STATISTICS` globally: a 10× larger sample means ANALYZE reads most of a 1–2.7 GB heap and, with 4.2's frequent autoanalyze, adds real I/O on a saturated disk.
If you want exact estimates for the top common words, `ALTER TABLE word_urls ALTER COLUMN word_id SET STATISTICS 300` is the compromise. **Priority: P2** (no current plan regression; protects against one).

### 4.8 `logs`: unused index, retention, `id` range (P2/P3)

**Problem / evidence.**
* `logs_message_pattern_idx` (`text_pattern_ops` on `message`, **60 MB, 11 lifetime scans**) is maintained on every log insert (~87 rows/s) and the dashboard uses the partial indexes instead.
* `logs` grows ≈ 3 M rows/day (620,839 rows in 4.8 h, avg 58 B message) ⇒ ≈ 0.75 GB/day with indexes; 66 % of the rows are `Starting…`/`Error…` noise (242,330 + 204,867 of 620,839); the dashboard recount runs a full pass of all `Scraped%` rows (174 ms now for 149k rows with `Heap Fetches: 39,071`; the code comment says "~80 M log rows … minutes").
* `logs.id` is `SERIAL` (int): `logs_id_seq.last_value` = 185,443,677 of 2,147,483,647 (8.6 %); at ≈ 3 M/day that is ≈ 650 days — fine for now.

**SQL.**
```sql
DROP INDEX CONCURRENTLY logs_message_pattern_idx;   -- 60 MB, 11 scans in months; safe, reversible by recreating it (CREATE INDEX CONCURRENTLY ... (message text_pattern_ops))
```
Retention: the dashboard computes lifetime totals from `logs` (`_status_counts`, daily chart), so **pruning silently changes those totals** unless a rollup exists first. Suggested design (cron):
```sql
CREATE TABLE scrape_daily (day date PRIMARY KEY, scraped bigint NOT NULL, blocked bigint NOT NULL, errors bigint NOT NULL);
-- nightly, for the previous day:
INSERT INTO scrape_daily SELECT ts::date, count(*) FILTER (WHERE message LIKE 'Sc%'), count(*) FILTER (WHERE message LIKE 'Bl%'), count(*) FILTER (WHERE message LIKE 'Er%')
FROM logs WHERE ts >= current_date - 1 AND ts < current_date GROUP BY 1 ON CONFLICT (day) DO UPDATE SET scraped = EXCLUDED.scraped, blocked = EXCLUDED.blocked, errors = EXCLUDED.errors;
-- then, in batches of ~50k, e.g. keep 7 days:
DELETE FROM logs WHERE id IN (SELECT id FROM logs WHERE ts < now() - interval '7 days' LIMIT 50000);
```
Long term, daily range partitions on `logs(ts)` with `DROP`/`DETACH` give O(1) retention (needs the primary key to include `ts`, i.e. a migration; not worth it below ~100 M rows). Widen `logs.id` with `ALTER TABLE logs ALTER COLUMN id TYPE bigint` (**rewrites the table under ACCESS EXCLUSIVE: do this only with the fleet at 0, and only when the sequence passes ~1.5 B**).
**Priority:** index drop P3 (trivial win); retention P2.

### 4.9 Dashboard queries (P2)

Measured (EXPLAIN ANALYZE on the exact statements from `frontend/app.py`):

| Statement | Time | Plan note | Fix |
|---|---|---|---|
| `SELECT COUNT(DISTINCT SUBSTRING(url …)) FROM urls` | **2,233 ms** | seq scan 184k rows, `temp read=593 written=596` (4 MB `work_mem` spill), `written=1638` | already cached 10 min in the app; at 100 M urls this is minutes. Maintain a `domains` counter (distinct host count) in `corpus_stats` from the batch job, or `SET LOCAL work_mem='64MB'` |
| `SELECT COUNT(*) FROM urls` (+ `pg_database_size`) | **1,196 ms** | parallel seq scan, `written=2559` | not cached in the app; use `n_live_tup` / the `corpus_stats` row |
| active scrapers: `GROUP BY ip` over *all* `Scraped%` rows | **446 ms** | parallel index scan of `logs_scraped_ts_idx` reading 149k rows, 41,408 buffers | filter `ts >= now() - interval '3 minutes'` in `WHERE` (changes `started_at`/`urls_scraped` from lifetime to window), or read `crawler_nodes` |
| daily scrapes `date_trunc('day', ts)` | 174 ms | index-only scan + external merge sort 2.6 MB | `scrape_daily` rollup (4.8); `work_mem` |
| `SELECT count(*) FILTER … FROM url_queue` (waiting/claimed) | 70 ms | parallel seq scan of the queue | keep the 1 s timeout + `reltuples` fallback (accurate after 4.2) or maintain counters |
| `MAX(id)` of `logs`, last-8, per-minute count, incremental status counts | 0.06–65 ms / 0.35 ms / 2 ms | fine (partial indexes work) | none |

`url_queue` has `seq_scan` 4,850,876 and `seq_tup_read` 7.03 × 10¹² lifetime: of the queue statements in the code only the dashboard `COUNT(*) FILTER` and
no-index plans can sequentially scan (the claim/release/delete plans I checked all use `url_queue_unclaimed_idx`, `url_queue_claimed_idx`, `url_queue_url_key` or `url_queue_pkey`; see Appendix A.8), so that dashboard statement and any
manual `select * from …` are the likely source (this is an inference; the database cannot attribute seq scans to statements without `pg_stat_statements`).
Also observed: three idle `psql` sessions running `select * from logs` / `select * from logs order by id desc` (one 1 h 54 min old) hold connections and, if re-run, drag the whole table through the cache.

### 4.10 JIT, parallelism, collation (P3)

* **JIT** is on (`llvmjit`) but none of the captured plans (30 … 19,767 total cost) exceeded `jit_above_cost = 100000`, so it has never fired for search; the risk is that a mis-estimated plan crosses the threshold and adds tens/hundreds of ms of compile time to an OLTP query.
  `jit = off` (4.1) is free insurance.
* **Parallel query** is only chosen for sequential scans of `urls` (the N/avgdl statement, the nav scan, the dashboard counts). Under a saturated 8-thread box with a fleet running, launching 2 workers for a 60 ms scan is marginal; 4.4/4.5 remove those statements anyway. Leave as is.
* **Collation:** `en_US.UTF-8`. `words_pkey` range scans for typo correction run under locale collation (slower comparisons than `C`). If typo correction becomes hot, an extra index `CREATE INDEX CONCURRENTLY words_word_c_idx ON words (word COLLATE "C")` plus the same collation in the query would speed the 281 ms common-prefix scan (est. 20–40 %); not worth it unless that path matters.
  The real fix for typo correction needs `pg_trgm` or a precomputed deletion-neighbourhood table (see section 6).

### 4.11 Index bloat (P3, estimates)

Without `pgstattuple`, bytes per live entry vs ideal:

| Index | size | live rows | bytes/entry | ideal (24 B tuple + 4 B pointer, fillfactor 90 ≈ 31 B) |
|---|---|---|---|---|
| `word_urls_word_id_url_id_count_idx` | 1229 MB | 29.6 M | 43.5 | **≈ 30 % reclaimable, ≈ 350 MB** |
| `trigram_urls_trigram_id_url_id_count_idx` | 2981 MB | 59.96 M | 52.1 | ≈ 40 % reclaimable, ≈ 1.2 GB |
| `bigram_urls_…` / `prefix_urls_…` | 1128 / 813 MB | 23.6 M / 17.0 M | 50.1 / 50.1 | ≈ 38 % |
| `idx_*_url_id` (btree dedup in PG13 helps) | 193–634 MB | | ≈ 10.8 B | fine |
| heap, e.g. `word_urls` | 1208 MB | 26.6 M (+2.5 M dead) | 42 B incl. dead | ≈ 40 B: no heap bloat |

Part of the excess is the ≈ 9 % dead entries not yet vacuumed (fixed by 4.2) and part is page splits from random inserts (improved by 5.4). A `REINDEX INDEX CONCURRENTLY <name>` (PG12+) rebuilds without blocking writers but needs disk space equal to the index and, on this disk, would run for tens of minutes (est.) per GB while competing with the fleet.
**Only worth doing after 4.2/5.4 have run for a while, and skip entirely for the n-gram tables if you choose 4.3.** Never use plain `REINDEX`, `CLUSTER` or `VACUUM FULL` (ACCESS EXCLUSIVE).

---

## 5. Write path (crawl throughput)

### 5.1 Lock convoys and why transactions last minutes

The batcher flushes up to 200 pages (≈ 220,000 posting rows) per transaction (`BATCH_PAGES = 200`) from each of 12 boxes, and `write_pages` order is: `INSERT INTO urls` → `DELETE` ×4 → `INSERT INTO words`/`prefixes` →
`INSERT urls_references` → four `COPY`s. The new words are *uncommitted* while the COPYs run for **1–3 minutes** on the saturated disk, so any other writer that tries to insert the same new word
(unique index on `words.word`, speculative insertion for `ON CONFLICT DO NOTHING`) or the same url, or `UPDATE urls SET reference_count` on a locked row, **waits for the first transaction to finish** — which itself waits on I/O or another writer.

Sampled 3× over 60 s (06:30–06:32):

| Sample | active sessions | `Lock/transactionid` waiters | longest wait | longest open xact |
|---|---|---|---|---|
| 1 | 12 | 5–6 | 2 min 38 s (`INSERT INTO words`) | 2 min 38 s |
| 2 (+20 s) | 12 | 6 | 3 min 19 s | 3 min 19 s |
| 3 (+40 s) | 11 | 6 | 3 min 19 s (chains up to depth 4: A→B→C→D) | 3 min 19 s |

Blockers were mostly `COPY prefix_urls/word_urls …` transactions (`DataFileWrite`, later `WALWrite`) and `INSERT INTO urls`/`UPDATE urls` ones.
Earlier at 06:17: 8 `transactionid` waiters among 17 active, waits of 17–50 s. Effective write concurrency is therefore roughly half of the session count, and the disk is idle-waiting-for-itself while sessions queue.
`deadlocks` = 8,917 lifetime (35/day) shows ordering mistakes still happen between code paths (`utils.write_pages` vs `crawl_lib/db.py` vs the batcher).

**DB-side:** `log_lock_waits = on` (4.1) will prove this in the server log; faster I/O (4.1, SSD) shortens every transaction; fewer concurrent writers via pooling (4.6) lowers contention.
**App-side (no DB change, biggest effect):** run the `words`/`prefixes` inserts as their *own committed transaction* first (they are idempotent and tiny), then do the heavy `COPY`s in a second transaction that no one waits on; flush smaller batches (e.g. 25–50 pages) so each transaction is seconds, not minutes (`fast_crawl.py` already uses ≤ 25 pages).

### 5.2 Write amplification per posting row (estimate from the measurements)

Per inserted `word_urls`-style row (3 × int):

| Component | bytes stored | WAL (est.) | Random I/O? |
|---|---|---|---|
| heap tuple (24 B header + 12 B + 4 B line pointer) | ≈ 40 | ≈ 45–50 (COPY multi-insert, amortised) | no (append) |
| token-first covering btree `(token_id, url_id) INCLUDE (count)` | ≈ 28–31 ideal, 43–52 observed | ≈ 70 + **full-page images** (each distinct leaf page touched for the first time after a checkpoint writes 8 kB) | **yes**: inserts land at random leaf pages across 1–3 GB |
| `url_id` btree (`idx_*_url_id`) | ≈ 11 (with PG13 dedup) | ≈ 70 | **no**: `url_id` correlation 0.9995, i.e. right-edge appends |
| **Total** | ≈ 90–110 | measured **≈ 392 per row** (5.5 MB/s ÷ 14 k rows/s) | |

So the four `url_id` indexes (1,382 MB total, 4 of the 8 posting indexes) are ≈ **10 % of posting bytes and ≈ 15–20 % of the WAL bytes (est.) but ≈ 0 % of the random I/O**; they are cheap *per row*
but they are why a `DELETE ... WHERE url_id = ANY(...)` can be an index scan at all. The measured remainder (≈ 200 B of the 392 B) is consistent with **full-page images from the random covering-index writes**, which is what
`wal_compression`, longer checkpoints (4.1) and ordered inserts (5.4) address, and what dropping the n-gram covering indexes (4.3) removes.

### 5.3 Are the single-column `url_id` indexes worth keeping? (answer: yes for now)

* They were created by the fleet (`utils.create_database()` with `STULTUS_ALLOW_DDL=1`, lines 376–379) and **search does not use them** (plans use only the covering indexes; `fast_crawl`/`crawl_lib/db.py` comments still say "the postings tables have no url_id index", which is out of date for this database).
* They **are** used by every `utils.write_pages` batch: `DELETE FROM <posting table> WHERE url_id = ANY(%s)` ×4 per batch (≈ 12.7 index scans/s fleet-wide; about 5.5 k posting rows/min, ≈ 3 %, are actually deleted by re-crawls). Without them each `DELETE` would be a sequential scan of 1–2.7 GB heap.
* Cheaper design (app side): add `RETURNING url, id, (xmax = 0) AS inserted` to the `urls` upsert and only `DELETE` for urls that already existed (97 % of rows are first inserts and have nothing to delete). That keeps the indexes valid for re-crawls
  but removes 3 of every 4 probes. If re-crawls are retired entirely (as `crawl_lib/db.py` already does: "a page with total_tokens is fully indexed: leave it alone"), the four indexes (1.38 GB, ≈ 15–20 % of posting WAL) can be dropped with `DROP INDEX CONCURRENTLY idx_word_urls_url_id; …`.
* A BRIN index on `url_id` (a few hundred KB each) could replace them *in principle* because the heap is physically ordered by `url_id` (correlation ≥ 0.9992) and re-crawl deletes touch contiguous batches. **Untested and risky**: interleaved concurrent batches and free-space reuse after vacuum widen BRIN ranges over time. Evaluate on a restored copy only.

### 5.4 Ordered `COPY` (app-side, no DB change)

`write_pages` builds each table's COPY text by iterating a `Counter` in dict order, so token ids arrive in random order and each row lands in a different leaf page of the covering index. Sorting the rows by `(token_id, url_id)` before `COPY`
makes every touched leaf page get all its new tuples in one visit instead of one tuple at a time: fewer page splits, **far fewer full-page images and dirty pages per batch** (this is the dominant 200 B/row of the WAL), better cache behaviour. Same data, same SQL. Estimated to be among the largest crawl-side wins after 4.3.

### 5.5 `synchronous_commit = off` for crawler sessions (app-side option; low value here)

Measured commit rate is only **3.7/s** (444 in 120 s), so removing the per-commit `fsync` (WALSync/`COMMIT` waits appeared in 1 of ~40 session-samples) saves very little; the WAL *volume* and `FPI`s are the load. It remains a safe, per-session switch
(`SET synchronous_commit = off`; a crash can lose the last ≈ 600 ms of commits but never corrupts, and the queue's claim-timeout recovers the urls). I would only do it after 4.1/5.4 and only if `WALSync` shows up in `pg_stat_activity` sampling afterwards.

### 5.6 Other write-path observations

* `urls_references` (2,477 MB, **8.0 M rows, 15 % of the DB**) is **never read by any code in this repo** (only `INSERT … ON CONFLICT DO NOTHING`); its primary key is two `varchar(2048)` columns (1,483 MB, **699 M index probes**, the busiest index in the database).
  If nothing external consumes it, stop writing it (saves ≈ 55 k rows/min and the busiest index); if it is needed for research, an `int`-pair `(src_url_id, dst_url_id)` representation is ~10× smaller.
* `weights` (4 rows) received **24.7 M updates** (≈ 1/s) and `crawler_nodes` 7.4 M (heartbeats): both cause continuous autovacuum of tiny tables (109,137 and 85,304 vacuums). Nothing in this repo updates `weights`
  (grep finds only the read in `search.py`), so something external does. Harmless but wasteful; `ALTER TABLE crawler_nodes SET (fillfactor = 50)` keeps those updates HOT.
* `words` is 1.43 M rows and 77,186 of them are ≤ 3 characters. Junk tokens (`somebodywbal`, `youzhuo`) keep growing it; fine at this size.
* `ON CONFLICT … DO UPDATE SET total_tokens` in the `urls` upsert (utils path) writes a new row version for every conflict (32 M `urls` updates lifetime, 56 % HOT). `DO NOTHING` + a separate `UPDATE … WHERE total_tokens IS NULL` for stubs (what `crawl_lib/db.py` does) avoids that.

---

## 6. Queue operations

* **`url_queue.id` is already `bigint`** (column and sequence; `last_value` 2,148,059,088, `max_value` 9.22 × 10¹⁸). `widen_url_queue_id.py` has evidently been run; **no reset is needed** and the INT-exhaustion problem described in the task no longer exists in this database. (`utils.write_queue`/`enqueue_explicit_ids` still contain the explicit-id fallback; it is no longer triggered.)
* Plans for every queue statement the code issues are index-based and sound (Appendix A.8): claim `UPDATE … WHERE id IN (SELECT id … claimed_at IS NULL ORDER BY id LIMIT n FOR UPDATE SKIP LOCKED)` → `LockRows` over `url_queue_unclaimed_idx` + nested loop on `url_queue_pkey`; expiry release → bitmap scan of `url_queue_claimed_idx` (0.75 ms); `DELETE/UPDATE … WHERE url = ANY(…)` → `url_queue_url_key`.
* **The problem is dead entries at the head of the queue, not the plan:** every claim cycle updates a row (non-HOT, because `claimed_at` is in two partial-index predicates) and every finished url deletes it, so the head of `url_queue_unclaimed_idx` is a wall of dead entries between vacuums.
  Measured: **763 ms, 2,056 buffers, `Heap Fetches: 5,935` for `LIMIT 100`** and **2,057 buffers for `LIMIT 10`**; table 42 % dead; 413 M inserts and 413 M deletes lifetime. Fix: 4.2 (aggressive per-table vacuum, no cost delay).
* The fleet-wide claim path is also fully serialised by its own convoy only if many boxes claim at the same instant; `SKIP LOCKED` prevents waiting. No change needed.
* Design alternative (app side, bigger change): claim by `DELETE … RETURNING` into an in-flight table, or claim ranges of ids per box, would avoid the update-then-delete double churn. Not recommended until 4.2 is measured.
* `reltuples` for `url_queue` oscillates between 69,720 and ~1.07 M depending on when vacuum last ran vs real 0.6–1.8 M live, so `fast_crawl.queue_estimate()` is unreliable until 4.2 is in; `n_live_tup` from `pg_stat_user_tables` is a better free estimate.

---

## 7. Ideas evaluated on paper

| Idea | Verdict | Reasoning / evidence |
|---|---|---|
| Per-word `df`/idf table | **Do (4.4)** | measured 0.25–1 s first-touch |
| Per-url doc length precomputed | Already exists (`urls.total_tokens`); duplicating it into the postings would remove the 54k–97k `urls_pkey` probes per search (≈ 30–40 % of ranking time) but costs +4 B × every posting, a rewrite of 26 M rows, and writer changes | not worth it at this size |
| Covering index `urls (id) INCLUDE (total_tokens, reference_count)` | **Skip for now** | `urls` heap is 26 MB and cached; only 9 % of its pages are all-visible (32 M updates) so the index-only scan would mostly still hit the heap; the join also needs `url`/`title` for the winners |
| `pg_trgm` GIN on `urls.title`/`host` | Replaced by the built-in GIN in 4.5 | contrib not installed (`postgresql13-contrib` package would be needed); `score_nav` only wants whole words |
| `pg_trgm` GIN on `words.word` for typo correction | **Needs contrib install**; good fit at scale | `similarity()`/`%` with `word % 'kernal'` ordered by `<->` replaces the 281 ms common-prefix range scan with a few ms; `CREATE INDEX CONCURRENTLY words_word_trgm ON words USING gin (word gin_trgm_ops)`: ≈ 60–120 MB for 1.4 M words (est.). Without contrib: a precomputed "deletion neighbourhood" table (SymSpell style) is a bigger build |
| Covering indexes | Already in use for `word_urls`; the bm25 path is index-only | the *real* problem is stale visibility map (4.2) |
| `CLUSTER` / `pg_repack` posting tables by `word_id` | **No** | heap is never read by search (index-only); the btree is already `(word_id, url_id)`-ordered; `CLUSTER` = ACCESS EXCLUSIVE rewrite of 1–2.7 GB on a saturated HDD; `pg_repack` not installed |
| Partial indexes | Only `url_queue` (already has them) | no hot, selective predicate elsewhere |
| BRIN | Only as a possible `url_id` replacement (5.3); **untested** | heap is `url_id`-ordered; widening risk |
| Partition posting tables | **Not yet** | benefits (parallel vacuum, per-partition drop) appear at hundreds of GB; today the biggest table is 6 GB and nothing is table-size-limited. Requires a reload (cannot attach existing tables without a rewrite). Hash-partitioning by `word_id` would help `autovacuum` scale at ≥ 500 GB |
| `url_queue.id` → bigint | **Already done** | see §6 |
| Queue "reset" | Not needed | bigint |
| `tsvector` GIN instead of token tables | **No** | not BM25 (`ts_rank` ignores global idf and per-doc length normalisation as used here), requires storing a per-page `tsvector` (≈ the same row volume), and GIN pending-list/merge writes are random I/O on a disk that is already the bottleneck |
| Posting lists as arrays (one row per `word_id` block) | Radical redesign, **not now** | would cut heap bytes ~10× but turns appends into read-modify-write of large rows |
| Logs retention / partitioning | **Do retention (4.8)**, partition later | |
| pgbouncer | **Do (4.6)** | |
| `synchronous_commit = off` | Low value | 3.7 commits/s (5.5) |
| Lower `random_page_cost` | **No** | rotational disk |
| `effective_io_concurrency` > 1 | **Maybe 2–4**, test | one spindle with NCQ; affects bitmap heap scans only |
| Faster storage / WAL on SSD | **Yes (outside SQL)** | 97 % util at 234 write IOPS |

### Scaling the estimates to "billions of rows"

Everything above was measured at 127 M posting rows / 131 k indexed pages (16 GB). If the table sizes are ~10× larger on the production system you
described, expect: index builds (`CREATE INDEX CONCURRENTLY` on a 60–400 GB table) to take **hours** (the repo's own `add_search_indexes.py` docstring says the same; my estimate on this HDD is order 10–25 min per GB of heap, i.e. 10+ hours for the biggest table), `REINDEX CONCURRENTLY` likewise,
`word_df`'s refresh to take minutes, and the ranking statement (linear in candidate count) to need the "heavy-word probing" path (`SEARCH_HEAVY_DF`) to trigger on realistic words: at the current corpus **no word reaches
`SEARCH_HEAVY_DF = 100000`** (max ≈ 46.5 k), so that code path is untested in production.

---

## 8. Suggested rollout order

1. **Today, no downtime:** 4.2 (autovacuum reloptions; `lock_timeout` 5 s), then 4.1's *reload-only* settings (`wal_compression`, checkpoint/WAL, bgwriter, `track_io_timing`, logging, `jit = off`, `maintenance_work_mem`/`autovacuum_work_mem`, `autovacuum_*`), `DROP INDEX CONCURRENTLY logs_message_pattern_idx`, 4.7's `n_distinct` overrides.
2. **This week:** `CREATE INDEX CONCURRENTLY urls_nav_fts_idx` (4.5, before the 200k-page cliff) and the `word_df`/`corpus_stats` tables (4.4) + the app changes; app-side 5.1/5.3/5.4; float8 casts.
3. **Planned window:** 4.1 restart items (`shared_buffers`, `wal_buffers`); pgbouncer (4.6); decision on 4.3 and, if A, the `TRUNCATE` with the fleet at 0.
4. **Afterwards:** re-measure with the verification queries below; only then consider `REINDEX CONCURRENTLY` (4.11), `url_id` index options (5.3) and the dashboard rollups.

### Verification queries (all read-only) to compare before/after

```sql
-- how far behind is vacuum?  (want n_ins_since_vacuum in the hundreds of thousands, not tens of millions)
SELECT relname, n_live_tup, n_dead_tup, n_ins_since_vacuum, last_autovacuum, last_autoanalyze FROM pg_stat_user_tables
WHERE relname IN ('word_urls','bigram_urls','trigram_urls','prefix_urls','url_queue','urls');
-- who writes buffers?  (want backends well under 50 %)
SELECT buffers_backend, buffers_clean, buffers_checkpoint, checkpoints_req, checkpoints_timed FROM pg_stat_bgwriter;
-- temp spill and deadlocks
SELECT temp_files, temp_bytes, deadlocks, blks_read, blks_hit FROM pg_stat_database WHERE datname = 'stultus';
-- lock convoys right now
SELECT count(*) FILTER (WHERE wait_event_type = 'Lock') AS lock_waiters, count(*) FILTER (WHERE state = 'active') AS active FROM pg_stat_activity WHERE backend_type = 'client backend';
-- heap fetches per search: run  EXPLAIN (ANALYZE, BUFFERS)  on the search statements and look at "Heap Fetches" and "written=".
```

---

## Appendix A — Raw evidence

### A.1 Table and index sizes (06:17; `pg_class` / `pg_relation_size`)

| Table | est. rows (`reltuples`, stale) | live (`n_live_tup`) | heap | indexes | total | `relallvisible`/`relpages` (stale) |
|---|---|---|---|---|---|---|
| `trigram_urls` | 15,912,035 | 58.7 M → 59.96 M | 2708 MB | 3614 MB | 6324 MB | 81,853 / 86,011 |
| `word_urls` | 7,467,587 | 25.97 M → 29.6 M | 1208 MB | 1516 MB | 2725 MB | 32,634 / 40,467 |
| `urls_references` | 5,776,692 | 7.79 M → 7.98 M | 993 MB | 1483 MB | 2477 MB | 84,785 / 93,470 |
| `bigram_urls` | 12,046,662 | 23.1 M | 1052 MB | 1397 MB | 2449 MB | 63,092 / 67,550 |
| `prefix_urls` | 5,268,019 | 16.6 M | 768 MB | 1006 MB | 1774 MB | 24,222 / 28,548 |
| `url_queue` | 69,720 | 619,705 → 857,458 → 1.84 M | 67 → 94 MB | 105 MB | 171 MB | 0 / 777 |
| `logs` | 516,358 | 594,474 → 620,839 | 70 MB | 84 MB | 154 MB | 7,658 / 7,818 |
| `words` | 1,364,450 | 1.40 M → 1.43 M | 67 MB | 55 MB | 122 MB | 8,518 / 8,564 |
| `urls` | 177,197 | 177,996 → 207,104 (130,795 indexed) | 26 MB | 25 MB | 51 MB | 296 / 3,376 |
| `prefixes` | 229,794 | 236,530 | 11 MB | 7.9 MB | 18 MB | 1,355 / 1,355 |
| small: `trigrams` (17,576), `bigrams` (676), `crawler_nodes` (10), `weights` (4), `blocked_domains`, `entities`, `entity_urls`, `url_token_counts` (0) | | | | | | |

Index list (name, size, lifetime `idx_scan`): `trigram_urls_trigram_id_url_id_count_idx` 2981 MB / 10,293 · `urls_references_pkey` 1483 MB / 699,276,427 · `word_urls_word_id_url_id_count_idx` 1229 MB / 2,535→4,311 ·
`bigram_urls_bigram_id_url_id_count_idx` 1128 MB / 1,975 · `prefix_urls_prefix_id_url_id_count_idx` 813 MB / 922 · `idx_trigram_urls_url_id` 634 MB / 38,505 · `idx_word_urls_url_id` 287 MB / 39,274 ·
`idx_bigram_urls_url_id` 268 MB / 42,246 · `idx_prefix_urls_url_id` 193 MB / 39,135 · `url_queue_url_key` 77 MB / 2.94 G · `logs_message_pattern_idx` 60 MB / **11** · `words_pkey` 55 MB / 2.39 G · `url_queue_pkey` 14 MB / 28,280 ·
`urls_url_key` 14 MB / 963 M · `url_queue_unclaimed_idx` 14 MB / 417 · `logs_pkey` 13 MB / 1,617 · `prefixes_pkey` 7.9 MB / 1.54 G · `urls_domain_idx` 6.6 MB / 83,354 · `logs_scraped_id_idx` 5.7 MB / 6,775 · `urls_pkey` 4.7 MB / 124.7 M ·
`logs_scraped_ip_ts_idx` 4.0 MB / 1,982 · `logs_scraped_ts_idx` 2.1 MB / 31,443 · `url_queue_claimed_idx` 256 kB / 143,730. All indexes `indisvalid = true`. No exact duplicate indexes. Total index bytes in `public`: 10 GB.

### A.2 Vacuum / analyze state (06:17, 06:42)

| Table | dead | dead % | `n_ins_since_vacuum` | `last_autovacuum` | `last_autoanalyze` | notes |
|---|---|---|---|---|---|---|
| `word_urls` | 2,516,463 → 2,719,449 | 8.8 % | 22.4 M → **26.2 M** | 05:08 | 2026-11-10 20:41 (`last_analyze`), 05:11 (auto) | autovacuum worker **26 min in**, `VacuumDelay`/`DataFileRead` waits |
| `trigram_urls` | 5,282,066 | 8.3 % | 48.6 M | 05:09 | 05:10 | |
| `bigram_urls` | 1,680,136 | 6.8 % | 12.8 M | 05:27 | 05:27 | worker 25 min in |
| `prefix_urls` | 1,470,955 | 8.1 % | 13.6 M | 05:10 | 05:13 | worker started 06:37 |
| `urls_references` | 443,546 | 5.4 % | 2.3 M | 05:45 | – | one vacuum ran 10 min 37 s |
| `url_queue` | 457,675 → 482,821 | **42.5 %** | 0.70 M | 05:19 | 05:19 | 320 autovacuums; `n_tup_hot_upd` 9 of 522k updates |
| `urls` | 6,967 | 3.8 % | 8,938 | 06:09 | 06:16 | `n_tup_upd` 32.0 M, HOT 18.1 M |
| `crawler_nodes` / `weights` | 215 / 20 dead | 95 % / 83 % | | | | 85,304 / 109,137 autovacuums |

XID age: all large tables ≈ 428 k; `datfrozenxid` age 91.5 M (aggressive vacuum at 200 M): no wraparound risk now.

### A.3 `pg_stats` (sample-based)

`word_urls`: `word_id` n_distinct 19,577, corr 0.0167, MCV 100; `url_id` n_distinct 16,201, corr 0.99948; `count` n_distinct 86, corr 0.518 (67.9 % are 1).
`bigram_urls`: `bigram_id` 610 (676 real), `url_id` 32,011, corr 0.9995. `trigram_urls`: `trigram_id` 4,476 (17,576 real), `url_id` 17,191, corr 0.99976. `prefix_urls`: `prefix_id` 5,164 (≈ 252k real), `url_id` 22,563, corr 0.99916.
`urls`: `id` corr 0.754, `total_tokens` n_distinct 10,192. `url_queue.claimed_at` n_distinct 112. The five most frequent words and their estimated postings (MCV × `n_live_tup`): `new` 46,507 · `use` 42,035 · `work` 37,563 · `research` 35,774 · `first` 34,880.

### A.4 Settings dump
See §2.2. Additional: `superuser_reserved_connections` 3, `tcp_keepalives_idle` 7200 (default: dead crawler boxes hold slots for 2 h; consider `tcp_keepalives_idle = 60`, `tcp_keepalives_interval = 10`, `tcp_keepalives_count = 6`), `vacuum_cost_page_miss/hit/dirty` 10/1/20, `autovacuum_freeze_max_age` 200 M, `hash_mem_multiplier` 1, `archive_mode` off, `max_wal_senders` 10, `datcollate` `en_US.UTF-8`, `data_checksums` off, extensions installed: `plpgsql` only (and it is the only entry in `pg_available_extensions`).

### A.5 Session samples (06:17, then 06:30–06:32)

06:17: 70 idle + 17 active on `stultus` (92 total backends); waits: `Client/ClientRead` 72, `Lock/transactionid` 8, `IO/DataFileWrite` 3, `IO/DataFileRead` 2, `LWLock/WALWrite` 1, `Timeout/VacuumDelay` 1. Three autovacuum workers running concurrently (`urls_references` 10:37, `word_urls` 2:45, `bigram_urls` 1:43).
06:42: 108 backends (82 idle, 21 active); autovacuum: `word_urls` 26:17, `bigram_urls` 25:15, `prefix_urls` 4:32. 66–82 idle crawler connections show `ROLLBACK`/`COMMIT` as last statement.
Connection refusals (`too many clients already`) hit my read-only sessions at least 15 times between ≈ 06:26 and ≈ 06:40 (retried every 10–20 s).

### A.6 Search plan excerpts

`open source operating system`, df statement (cold):
```
Index Scan using words_pkey on words w  (actual time=927.531..988.120 rows=4)
  Buffers: shared hit=1569 read=2034 dirtied=260 written=1730
  SubPlan 1 -> Aggregate (actual time=246.983..246.983 rows=1 loops=4)
     -> Index Only Scan using word_urls_word_id_url_id_count_idx on word_urls wu  (actual time=0.031..246.279 rows=13720 loops=4)
          Heap Fetches: 2131      Buffers: shared hit=1562 read=2023 dirtied=260 written=1721
Execution Time: 988.161 ms          (second run: 7.0 ms)
```
ranking statement (same query):
```
Limit (actual time=305.830..305.866 rows=300)  Buffers: shared hit=222850 read=561 dirtied=36 written=510
 -> Sort (top-N heapsort 191kB)
   -> GroupAggregate (actual time=118.612..296.151 rows=36504)   Group Key: u.id
     -> Sort (quicksort 12720kB) (actual rows=54879)             Sort Key: u.id
       -> Nested Loop (actual time=0.047..99.861 rows=54879)
          -> Nested Loop -> Index Only Scan using word_urls_word_id_url_id_count_idx (rows=13720 loops=4)  Heap Fetches: 2111
          -> Index Scan using urls_pkey on urls u (actual rows=1 loops=54880)   Filter: (total_tokens IS NOT NULL)   Buffers: shared hit=219155 read=561 written=510
Execution Time: 306.372 ms          (second run: 276.8 ms)
```
Planner estimates in that plan: `rows=5` (sort), `rows=8` (outer nested loop) vs actual 54,879.

title/host scan: `Gather Merge -> Sort -> Parallel Seq Scan on urls (actual rows=1408 loops=3) Filter: total_tokens IS NOT NULL AND (lower(title) ~~ ANY(...) OR lower(url) ~~ ANY(...)) Rows Removed by Filter: 58621 Buffers: shared hit=3382` → 90.2 ms.

`machine learning research data`, worst sample: `Index Only Scan … (actual … 983.331 …) Heap Fetches: 9806` → `Execution Time: 4437.321 ms`.

`SELECT count(total_tokens), avg(total_tokens) FROM urls` cold: `Parallel Seq Scan on urls … Buffers: shared hit=89 read=3287 dirtied=33 written=2176` 151.7 ms (warm 13–19 ms).

Typo range scan, prefix `co`: `Index Only Scan using words_pkey on words (cost=0.43..6900.12 rows=188) (actual rows=7858) Rows Removed by Filter: 29402 Heap Fetches: 5149` 281 ms.

### A.7 Dashboard / write-path plans

```
COUNT(DISTINCT SUBSTRING(url…)) FROM urls: Seq Scan (rows=184197) Buffers: shared read=3338 dirtied=565 written=1638, temp read=593 written=596   2232.9 ms
SELECT COUNT(*) FROM urls (+db size): Parallel Seq Scan … Buffers: shared read=3411 written=2559   1196.3 ms
logs GROUP BY ip: Parallel Index Scan using logs_scraped_ts_idx (rows=74599 loops=2) Buffers: shared hit=41408 read=4229 written=3658   445.7 ms
logs daily: Index Only Scan using logs_scraped_ts_idx … Heap Fetches: 39071; Sort Method: external merge Disk: 2640kB   174.3 ms
last 8 scraped: Index Scan using logs_scraped_id_idx   0.06 ms
count last minute: Index Only Scan using logs_scraped_ts_idx (rows=323) Heap Fetches: 323   0.35 ms
incremental status counts: Index Scan using logs_pkey (id range)   2.05 ms
SELECT count(*) FILTER … FROM url_queue: Parallel Seq Scan (rows=285827 loops=3) Buffers: shared hit=458 read=11566 written=58   70.6 ms
DELETE FROM word_urls WHERE url_id = ANY(…)   plan (EXPLAIN only): Index Scan using idx_word_urls_url_id
```

### A.8 Queue plans (EXPLAIN; the three DML ones are plain EXPLAIN, never executed)

```
claim:   EXPLAIN SELECT id FROM url_queue WHERE claimed_at IS NULL ORDER BY id LIMIT 100
         Index Only Scan using url_queue_unclaimed_idx (actual time=763.795..763.866 rows=100)  Heap Fetches: 5935  Buffers: shared hit=2056 read=179 written=36   → 763.9 ms
         (LIMIT 10 variant) Index Scan using url_queue_unclaimed_idx … Buffers: shared hit=2057 → 0.94 ms
         UPDATE … WHERE id IN (… LIMIT 100 FOR UPDATE SKIP LOCKED): Nested Loop → HashAggregate → LockRows → Index Scan using url_queue_unclaimed_idx; Index Scan using url_queue_pkey
expiry:  Bitmap Heap Scan on url_queue … Bitmap Index Scan on url_queue_claimed_idx   0.75 ms ; UPDATE form: LockRows → Bitmap scan → nested loop on url_queue_pkey
by url:  Index Scan using url_queue_url_key   0.1 ms (SELECT/DELETE/UPDATE forms)
max id:  Index Only Scan Backward using url_queue_pkey   0.06 ms
```
State at 06:21: 857,480 rows, 849,384 unclaimed, 8,096 claimed (claims 05:51–06:21), min id 16,386, max id 2,148,308,949, 0 pushed to front, oldest `enqueued_at` 04:56 (the whole queue turns over in ≈ 1.5 h).

### A.9 Crawler write statements seen live
`INSERT INTO urls (url, title, icon_link, total_tokens) SELECT * FROM unnest(...) ORDER BY 1 ON CONFLICT (url) DO UPDATE SET total_tokens = EXCLUDED.total_tokens RETURNING url, id` (waiting on `transactionid`), `INSERT INTO "words" ("word") SELECT t FROM unnest(ARRAY[...]) … ON CONFLICT DO NOTHING` (waiting 17 s – 3 min 19 s),
`COPY word_urls|bigram_urls|trigram_urls|prefix_urls … FROM STDIN` (`DataFileRead`/`DataFileWrite`, `WALWrite`), `UPDATE urls SET reference_count = urls.reference_count + r.n …` (blocked), `INSERT INTO urls_references … ON CONFLICT DO NOTHING` (`WALWrite`). These are the `utils.write_pages` statements via the batcher; `crawl_lib/db.py` statements were not observed.

### A.10 Disk/WAL/CPU samples (`pg_read_file` on `/proc` and `/sys`, superuser read of server files)
`sdb`: sectors written 635,701,240 → 640,176,664; write I/Os 1,701,669 → 1,729,822; `io_ticks` 10,350,371 → 10,466,694 ms; reads 3,823,229 → 3,824,916. `/proc/stat` deltas (jiffies, 120 s): user 3,508, sys 946, idle 46,385, iowait 44,468. WAL LSN 25B/FEC06240 → 25C/281FACA0 (694,110,816 B).

## Appendix B — How this was produced (read-only)

Scripts live in `/home/ubuntu/.hermes/cache/scratch/dbreport/` (`q.py` runs `.sql` files in a read-only session with `statement_timeout`; `plans.py` wraps `search.py`'s own SQL with `EXPLAIN (ANALYZE, BUFFERS)`; `exp1.py` the float8 experiment). Statement types run, and nothing else:

* `SELECT` on `pg_settings`, `pg_stat_activity`, `pg_stat_database`, `pg_stat_bgwriter`, `pg_stat_user_tables`, `pg_stat_user_indexes`, `pg_statio_*`, `pg_stats`, `pg_class`, `pg_index`, `pg_locks`, `pg_sequences`, `pg_attribute`, `pg_constraint`, `pg_extension`, `pg_available_extensions`, `pg_database`, `information_schema.columns`, plus `pg_blocking_pids()`, `pg_current_wal_lsn()`, `pg_database_size()`, `age()`;
* `SHOW data_directory`;
* superuser `pg_read_file()` of `/proc/meminfo|cpuinfo|mounts|loadavg|stat|diskstats` and `/sys/block/sdb/…` (read-only server files; no `COPY … PROGRAM`, no writes);
* read-only `SELECT`s on application tables: `count(*)`/`avg` on `urls`, counts and group-bys on `logs`, `url_queue`, the `SELECT` statements `search.py` issues (`words`, `word_urls`, `urls`, `weights`), the whole-word/FTS comparison queries on `urls`;
* `EXPLAIN (ANALYZE, BUFFERS)` on `SELECT` statements only;
* plain `EXPLAIN` (planner only, **not executed**) on three `UPDATE`/`DELETE` statements to see their plans.

Every session was opened with `default_transaction_read_only = on`, `readonly=True` and a 30–60 s `statement_timeout`. No DDL, no `ANALYZE`/`VACUUM`/`REINDEX`/`CLUSTER`, no `SET` outside the session, no `ALTER SYSTEM`, no extensions created, no writes.
(Like any `SELECT`, reading pages can set hint bits and my queries added some read load while the fleet was busy.) I never held more than one connection at a time.
