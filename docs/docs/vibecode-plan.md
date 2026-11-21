# vibecode-experiment plan

Branch `vibecode-experiment`, based on `ai-supported`. This branch is an AI-driven
experiment and is kept separate from the hand-written mainline.

## Hard constraints

1. **No database changes.** No `CREATE`, `ALTER`, `DROP`, `TRUNCATE`, new indexes,
   new tables or columns, or changed server settings. Normal crawler data writes
   (rows in `urls`, `words`, `*_urls`, `url_queue`, `logs`, ...) are fine, because
   crawling is how the index gets filled. Code paths that issue DDL at startup
   (`utils.create_database()`, `frontend/app.py:init_control_schema()`) get a
   read-only check first and only run DDL when `STULTUS_ALLOW_DDL=1` is set.
2. **The frontend must look the same.** The only visible additions are a settings
   menu and the debug fields it can toggle. The current responsive layout stays.
3. All work happens on `vibecode-experiment`.

## Workstreams

### A. Multilingual tokenization (fixes the foreign-language bug)
Problem: `tokenizer.clean()` drops every word that isn't ASCII a-z, so a page
with 5,000 words of German/Japanese and 2 English words is indexed as "2 words"
(and `total_tokens` is wrong, which skews relevance). `scrape.py` also drops
whole pages when langdetect returns anything other than `en`.

Plan:
- Unicode-aware `clean()`: NFKC normalization plus `casefold()`, `\w` word
  splitting (digits/underscore filtered), apostrophes and hyphens handled.
- Space-delimited scripts (Latin, Cyrillic, Greek, Arabic, Hebrew, Devanagari,
  ...) are tokenized as words. Stopwords for each nltk stopword language.
- CJK (Han/Hiragana/Katakana/Hangul): overlapping character bigrams as "words",
  the standard Lucene CJK approach, so they can be searched without a dictionary.
- Scripts with no spaces that we can't segment (Thai, Lao, Khmer, Myanmar,
  Tibetan): detected by script share and skipped (only the links are kept),
  so they never get indexed as a few stray words.
- Page language policy via `INDEX_LANGUAGES` (default `all`; `en` keeps the old
  English-only behaviour). Language detection on a capped text sample (fast).
- ASCII words tokenize exactly as before, so English ranking doesn't change.
  Bigram/trigram tables only contain a-z, so non-ASCII n-grams are skipped as before.
- Queries use the same tokenizer, so foreign-language queries work.

### B. Crawler / scraper performance
The current scraper is one synchronous page at a time per process, with about 10
DB round trips per page plus one round trip per log line. On a 4-core / 3 GB Pi:
- New `fast_crawl.py`: a pipeline with many concurrent fetchers (threads or
  asyncio), per-host politeness (Redis when available, otherwise in-process,
  plus robots.txt `Crawl-delay`), and a process pool for the CPU-heavy
  parse/tokenize step.
- Parse HTML once with lxml (links, title, icon, robots meta) and hand the same
  tree to trafilatura, instead of parsing with BeautifulSoup and then trafilatura.
- Batched DB writes: group many pages per transaction, batch log inserts, batch
  queue claims, enqueues and reference-count updates.
- Skip obvious non-HTML URLs (images, archives, PDFs) before queueing.
- Cap per-host pages per crawl so we get breadth (~5k sites) instead of 5k pages
  from one site.
- Same semantics as scrape.py: robots.txt, noindex/nofollow/noarchive,
  blocklist, the url_queue claim protocol and log message formats (the
  dashboard depends on them).
- Benchmark before and after (pages/min, CPU, RAM) with `--nodb` and against the DB.

### C. Search
- Same scoring by default (relevance × reference_count), with an optional
  BM25-style ranking mode computed from existing columns (`count`,
  `total_tokens`). No new indexes or tables.
- Query tokenization shared with workstream A. Precomputed/prepared SQL,
  pooled connections and an LRU result cache (already there, to be tuned).
- Result snippets and favicons would need stored text; the DB has no text
  column, so this is left out (no schema changes).
- Benchmark query latency with `tests/` tools once the index has data.

### D. Frontend
- Settings menu (gear button, small popover/modal): debug mode (score,
  relevance, ref score, reference count, query time), open results in a new
  tab, results per page, theme follows system / light / dark (only if the
  existing CSS variables support it). Stored in localStorage, no server state.
- Debug stats are hidden unless debug mode is on. The layout is otherwise the
  same, including the 700px breakpoint behaviour.
- `/api/search` JSON endpoint, pagination, HTML escaping checks, static file
  caching, and gzip.
- Check mobile, tablet and desktop widths with screenshots before and after.

### E. Crawl ~5,000 sites
- Seed list: the existing `seed_urls.csv` plus a broad, mostly-English set of
  well-known sites (news, reference, universities, docs, blogs, Kagi smallweb).
- Run `fast_crawl.py` with a per-host cap so we index thousands of distinct
  sites, then check search quality with sample queries.

## Execution
Subagents (a cheaper model) implement A, B and D in parallel on separate files.
The orchestrator reviews every diff, runs the tests and verifies results, then
does C and E against the real DB once `.env` is in place.
