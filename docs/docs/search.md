# Search ranking

`search.py` ranks pages with a BM25-style score computed only from the existing tables
(`word_urls`, `words`, `urls`, `weights`). It never writes to the database and needs no
schema changes or extra indexes.

```
score = bm25 * coverage * title_boost * pop_boost
```

| Part | Definition |
|---|---|
| `bm25` | sum over query words found on the page of `idf(w) * tf*(k1+1) / (tf + k1*(1 - b + b*dl/avgdl))` |
| `tf` | `word_urls.count` for that word and page |
| `dl`, `avgdl` | `urls.total_tokens` of the page, and its mean over indexed pages (`total_tokens IS NOT NULL`). `total_tokens` also counts bigram/trigram/prefix rows, but it is roughly proportional to the word count, and BM25 only uses `dl/avgdl` |
| `idf(w)` | `ln(1 + (N - df + 0.5) / (df + 0.5))`; `df` = number of postings of the word (index-only count), `N` = indexed pages. Always > 0 |
| `coverage` | `(matched query words / query words) ** 1.5`. A page with both words of "linux kernel" beats a page that repeats only one |
| `title_boost` | `1 + 0.6*frac_words_in_title + 0.5*frac_words_in_hostname`, `+0.15` when a hostname hit is on a front page (path depth ≤ 1). Whole-word match for ASCII, substring for CJK |
| `pop_boost` | `1 + 0.15 * ln(1 + reference_count)`. Log-damped, so 1000 references is about 2x, not 1000x |

`search()` still returns `(url, title, search_output, relevance, ref_score, reference_count)`:
`search_output` = final score, `relevance` = `bm25`, `ref_score` = `pop_boost`.

## Which pages are candidates

* **Word postings only** generate candidates. A page that contains none of the query words
  can never be returned by the main path (the old formula let bigram/trigram/prefix
  matches in, so "wikipedia" returned random blogs).
* **Title/hostname matches** (`kind: "title"`): pages whose title or hostname contains at least half
  of the query words but whose body postings do not (titles are not indexed as words,
  so `en.wikipedia.org/...` pages would otherwise be unreachable by "wikipedia"). On the memory path
  this is a lookup in an in-process title-word / host-word index (cheap, no size limit); on the SQL
  fallback it is a scan of `urls`, skipped when the index has more than `SEARCH_NAV_MAX_DOCS` pages.
* **Typo correction** (`kind: "corrected"`): a query word with no postings is replaced by the closest
  vocabulary word (Damerau-Levenshtein, distance 1 for 4-8 letters, 2 for 9+, ASCII only; ties alphabetical).
  Memory path: all ~500 one-edit spellings are probed against `words_pkey` in one statement (only words that
  have postings qualify); long words with no distance-1 fix fall back to the old range scan for distance 2.
  The corrected query is scored normally and multiplied by 0.85.
* **n-gram fallback** (`kind: "fuzzy"`), **off by default** (`SEARCH_FUZZY=1`; trigram overlap is noisy on long pages; it
  forces the SQL path): only if there are still too few results and a word is unknown
  (5+ letters). Pages must contain at least 85% of the word's trigrams; the score uses the `weights` table
  (`trigram`, `prefix`) times 0.25, and is capped below the worst word match, and they always sort after
  word matches. Prefix and bigram postings are not used elsewhere.

## Speed: the in-memory path

`search_memindex.py` keeps a read-only copy of what a query needs from `urls`, loaded in a background
thread when `search` is imported (and refreshed every `SEARCH_MEMINDEX_REFRESH` seconds): per page id
`total_tokens`, `reference_count`, validity, URL path depth; the URL and title strings; and an inverted
index title-word -> page ids and host-word -> page ids. `N` and `avgdl` come from it. Until it has
loaded (a few seconds after start), and without numpy, searches use the SQL path described below.

Per query on the memory path:

1. Tokenise (cached by the result LRU).
2. **Posting lists**: for each query word not in the in-process posting cache, one statement returns the
   word id and its whole posting list as two packed int32 byte strings (`words` pk lookup + one index-only scan
   of the `(word_id, url_id) INCLUDE (count)` index, `LATERAL`, so the plan cannot degrade to a
   scan). With several uncached words, `SEARCH_FETCH_THREADS` statements run concurrently (cold fetches are
   dominated by database I/O latency, which overlaps; measured -20% median cold latency for multi-word
   queries with 3 threads on a loaded DB). `df` is simply the list length, so there is no separate `count(*)`. Cached lists live in an LRU
   with a byte budget (`SEARCH_POSTING_CACHE_MB`); after `SEARCH_POSTING_TTL` a list is *stale*: it is still
   used and refreshed in a background thread (stale-while-revalidate, hard limit `SEARCH_POSTING_HARD_TTL`),
   so a hot word never makes a user wait. Unknown words are cached too (negative entries).
3. **Scoring** with numpy over the lists only (O(postings), not O(pages)): BM25 contribution per posting
   (cached with the list per memory-index snapshot), summed per page, times coverage and popularity
   (`pop_boost` is precomputed for all pages), top `SEARCH_RESCORE_K` by that pre-score.
4. **Title/host boost** for all candidates at once from the in-memory token index (pages with non-ASCII
   query words are checked on the candidates with the substring rule), nav candidates and typo-corrected
   candidates as extra sources; the best score per page wins; only the pages of the requested result page become
   dicts. The final score is the same product as the SQL path.

A warm query therefore costs no database round trip; a cold word costs one. A no-result query costs
~0.1 ms. At start the posting lists of the `SEARCH_PREWARM_WORDS` most common title words are fetched in the
background.

Trade-offs: pages crawled after the last memory-index refresh (default 3 min) are not searchable
until the next refresh; `N`/`avgdl` are as old as the refresh. `idf` uses the list length at fetch time.
Memory per process: see "Memory" below; with 2 gunicorn workers it is paid twice.

### SQL path (fallback)

Used until the memory index is loaded, without numpy, with `SEARCH_FAST=0`, or with `SEARCH_FUZZY=1`.

* Word ids and document frequencies (one index-only `count(*)` per word) and `N`/`avgdl`/`weights` are cached
  in-process for `SEARCH_STATS_TTL` seconds. Final results are in an LRU cache (`SEARCH_CACHE_SIZE`, `SEARCH_CACHE_TTL`).
* The ranking statement scans `word_urls` through the `(word_id, url_id) INCLUDE (count)` index
  (index-only), joins `urls` by primary key, aggregates per page and keeps the
  top `SEARCH_RESCORE_K` candidates; Python then applies the exact formula plus title boost.
* Cost is bounded for common words: words with more than `SEARCH_HEAVY_DF` postings are not scanned.
  They are probed by `(word_id, url_id)` for candidates that the rarer words produced
  (`LATERAL ... OFFSET 0` forces nested index probes). If every word is common, only the rarest is scanned.
  Pages that contain *only* common query words and none of the rarer words are not candidates.
* Session settings (on our own pooled connections only): `work_mem=64MB`, `statement_timeout=5000ms`,
  up to 2 parallel workers per gather. Optional steps (title scan, corrections, n-gram fallback) swallow
  a timeout; the main query does not.

### Measured (Raspberry Pi 4, 4 cores, Postgres 13 over the LAN, ~125k searchable pages, 34-query bench)

The shared database was heavily loaded by the crawler fleet while this was measured (50+ sessions on
row locks, an autovacuum of `word_urls` running, `max_connections` exhausted at times), so absolute cold
numbers are pessimistic and noisy; the in-process stages are not affected.

| | SQL path | memory path |
|---|---|---|
| df-warm p50 / p95 / max (only the result LRU empty) | 602 / 2761 / 4976 ms | 6.9 / 18.1 / 25.7 ms |
| cold p50 / p95 / max (all caches empty, memory index loaded) | 762 / 6332 / 6738 ms | 229 / 2474 / 2804 ms |
| no-result query | 55-80 ms | ~0.1 ms (cached) / 6 ms (first time) |
| in-process memory | ~0 | +~100 MB RSS after load (index data ~48 MB), posting cache <= 48 MB |

Cold time on the memory path is the database read of the posting lists (median 158 ms, worst 2.8 s in that run);
the computation after it is ~10 ms. Top-10 of the memory path equals the SQL path run at the same moment for
all queries compared (mean overlap@10 1.000, same #1, scores within 0.05%).

## Timing breakdown

`search_debug(q, timings=d)` fills `d` (and the module dict `search.last_timings`, per thread via
`search.get_timings()`) with milliseconds per stage: `tokenize`, `result_cache`, `postings_cache`, `postings_db`,
`score`, `nav`, `corrections`, `finalize` (SQL path: `sql_path`), plus `db_ms` (time inside database calls),
`round_trips`, `total_ms` and `path` (`memory` | `sql` | `cache` | `empty`). `tests/search_bench.py` prints
the median and worst of each stage.

## Debug output

`search.search_debug(query, limit, offset)` returns one dict per result with `score`, `bm25`, `coverage`,
`title_boost` (+ `title_frac`, `host_frac`), `pop_boost`, `reference_count`, `matched_words`, `term_freq`,
`doc_freq`, `total_tokens`, `avgdl`, `n_docs`, `kind` (`word` | `title` | `corrected` | `fuzzy`).
`/api/search?q=...&debug=1` adds them under `debug`. With debug mode on in the settings menu, the results
page adds the matched words, title boost, token count and document frequencies to the existing debug blocks.

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `SEARCH_MODE` | `bm25` | `bm25`, or the old formula: `legacy` (= `fast`, two-stage) / `exact` |
| `SEARCH_BM25_K1`, `SEARCH_BM25_B` | 1.2, 0.75 | BM25 saturation and length normalisation |
| `SEARCH_POP_ALPHA` | 0.15 | popularity weight (0 turns it off) |
| `SEARCH_TITLE_BOOST`, `SEARCH_HOST_BOOST`, `SEARCH_HOME_BOOST` | 0.6, 0.5, 0.15 | title / hostname / front-page boosts |
| `SEARCH_COVERAGE_POW` | 1.5 | penalty for missing query words (0 = off) |
| `SEARCH_RESCORE_K` | 300 | candidates kept before the exact re-score |
| `SEARCH_HEAVY_DF` | 100000 | words with more postings are probed rather than scanned |
| `SEARCH_NAV_MAX_DOCS`, `SEARCH_NAV_BASE` | 200000, 2.0 | title/hostname scan limit (0 = off) and its base text score |
| `SEARCH_CORRECTIONS`, `SEARCH_CORRECTION_PENALTY` | 1, 0.85 | typo correction on/off, score multiplier |
| `SEARCH_FUZZY`, `SEARCH_FUZZY_MIN`, `SEARCH_FUZZY_SCALE` | 0, 0.85, 0.25 | n-gram fallback on/off, threshold, scale |
| `SEARCH_STATS_TTL` | 600 | SQL path: seconds to cache N, avgdl, weights, word ids/df; also typo-correction cache |
| `SEARCH_FAST` | 1 | 0 = never use the memory path (SQL only) |
| `SEARCH_MEMINDEX_REFRESH` | 180 | seconds between refreshes of the memory index |
| `SEARCH_POSTING_CACHE_MB` | 48 | byte budget of the posting-list cache |
| `SEARCH_POSTING_TTL`, `SEARCH_POSTING_HARD_TTL` | 300, 3600 | a list older than the first is refreshed in the background; older than the second it is dropped |
| `SEARCH_PREWARM_WORDS` | 48 | common title words whose posting lists are fetched at start (0 = off) |
| `SEARCH_FETCH_THREADS` | 2 | uncached words of one query are fetched concurrently, one statement each, this many at a time (1 = a single statement for all) |
| `SEARCH_CACHE_SIZE`, `SEARCH_CACHE_TTL` | 2048, 300 | result LRU size and lifetime |
| `SEARCH_POOL_MAX` | 10 | connection pool size |
| `SEARCH_WORK_MEM` | 64MB | per-session `work_mem` |
| `SEARCH_STATEMENT_TIMEOUT_MS` | 5000 | per-session `statement_timeout` |
| `SEARCH_PG_PARALLEL_WORKERS` | 2 | `max_parallel_workers_per_gather` (0 = leave default) |

Legacy-only: `SEARCH_STAGE1` (stage-1 branches of the fast mode).

## Tests and benchmark

* `tests/test_search_scoring.py`: scoring math and the SQL layer with a fake cursor (no DB).
* `tests/test_search_memindex.py`: the memory index, numpy scoring, vectorised title boost, posting cache
  (budget, TTL, stale-while-revalidate), pre-warm, typo probe and the whole memory path against a fake DB (needs numpy).
* `tests/test_search_live.py`: read-only checks against `DATABASE_URL` (skipped without it), including
  memory path vs SQL path agreement (they skip, not fail, when the shared DB is too slow).
* `tests/search_bench.py`: ~34 queries, cold / df-warm / cached latencies (p50/p95/max), per-stage breakdown,
  `--sql` to time the fallback path, `--save` / `--compare` to check top-10 overlap against an earlier run.
