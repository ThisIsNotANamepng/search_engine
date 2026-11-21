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
  so `en.wikipedia.org/...` pages would otherwise be unreachable by "wikipedia"). This needs a scan of
  `urls`, so it is skipped when the index has more than `SEARCH_NAV_MAX_DOCS` pages.
* **Typo correction** (`kind: "corrected"`): a query word with no postings is compared (Damerau-Levenshtein,
  distance 1 for 4-8 letters, 2 for 9+, ASCII only) with vocabulary words that share its first letters
  (range scan of `words_pkey`). The corrected query is scored normally and multiplied by 0.85.
* **n-gram fallback** (`kind: "fuzzy"`), **off by default** (`SEARCH_FUZZY=1`; trigram overlap is noisy on long pages): only if there are still too few results and a word is unknown
  (5+ letters). Pages must contain at least 85% of the word's trigrams; the score uses the `weights` table
  (`trigram`, `prefix`) times 0.25, and is capped below the worst word match, and they always sort after
  word matches. Prefix and bigram postings are not used elsewhere.

## Speed and scaling

* Warm path = **one round trip**: word ids and document frequencies (one index-only
  `count(*)` per word) and `N`/`avgdl`/`weights` are cached in-process for `SEARCH_STATS_TTL` seconds.
  The first query for a word costs one extra statement. Final results are in an LRU cache
  (`SEARCH_CACHE_SIZE`, `SEARCH_CACHE_TTL`).
* The ranking statement scans `word_urls` through the `(word_id, url_id) INCLUDE (count)` index
  (index-only, `Heap Fetches: 0`), joins `urls` by primary key, aggregates per page and keeps the
  top `SEARCH_RESCORE_K` candidates; Python then applies the exact formula plus title boost.
* Cost is bounded for common words: words with more than `SEARCH_HEAVY_DF` postings are not scanned.
  They are probed by `(word_id, url_id)` for candidates that the rarer words produced
  (`LATERAL ... OFFSET 0` forces nested index probes). If every word is common, only the rarest is scanned.
  Pages that contain *only* common query words and none of the rarer words are not candidates.
* Session settings (on our own pooled connections only): `work_mem=64MB`, `statement_timeout=5000ms`,
  up to 2 parallel workers per gather. Optional steps (title scan, corrections, n-gram fallback) swallow
  a timeout; the main query does not.
* Not verified at millions of rows: the live index is a few hundred pages, so scaling is from the query
  plans (index-only scans, bounded probes), not from a measured large run.

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
| `SEARCH_STATS_TTL` | 600 | seconds to cache N, avgdl, weights, word ids/df, corrections |
| `SEARCH_CACHE_SIZE`, `SEARCH_CACHE_TTL` | 2048, 300 | result LRU size and lifetime |
| `SEARCH_POOL_MAX` | 10 | connection pool size |
| `SEARCH_WORK_MEM` | 64MB | per-session `work_mem` |
| `SEARCH_STATEMENT_TIMEOUT_MS` | 5000 | per-session `statement_timeout` |
| `SEARCH_PG_PARALLEL_WORKERS` | 2 | `max_parallel_workers_per_gather` (0 = leave default) |

Legacy-only: `SEARCH_STAGE1` (stage-1 branches of the fast mode).

## Tests and benchmark

* `tests/test_search_scoring.py`: scoring math and the SQL layer with a fake cursor (no DB).
* `tests/test_search_live.py`: read-only checks against `DATABASE_URL` (skipped without it).
* `tests/search_bench.py`: ~34 queries, top 5 for legacy vs bm25, cold/warm latencies (p50/p95).
