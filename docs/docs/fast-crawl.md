# fast_crawl.py: the concurrent crawler

`fast_crawl.py` is a faster replacement for running many copies of `scrape.py`. It shares the database, the log formats and the url queue with it, so both can run side by side. It never changes the schema.

## Run it

```bash
set -a; . ./.env; set +a            # DATABASE_URL (REDIS uses the same host and password)
export NLTK_DATA=/home/ubuntu/nltk_data

# 5,000 pages across thousands of sites, starting from a seed list (seeds are claimed first, links are queued as it goes)
python fast_crawl.py --seed-file /home/ubuntu/.hermes/cache/scratch/seeds/seeds_8k.txt --max-pages 5000

# keep crawling whatever is in url_queue until stopped (Ctrl-C / SIGTERM hands unfinished urls back)
python fast_crawl.py

# no database at all (benchmarks, dry runs)
python fast_crawl.py --nodb -f crawl_lib/bench_urls.txt
python fast_crawl.py --nodb --follow --seed-file seed_urls.csv --max-pages 500 --dump pages.jsonl

# tiny real-database test: store these urls, don't queue their links
python fast_crawl.py -f some_urls.txt --no-enqueue --max-pages 25
```

Progress prints every 10 s: urls done, pages/min (now and average), active fetches, frontier size, estimated `url_queue` size, and RSS of the main process plus the parser processes.

## Flags

| flag | default | meaning |
|---|---|---|
| `-u URL`, `-f FILE` | | crawl just these urls (not from the queue; no queue rows are touched) |
| `--seed-file FILE` | | one url per line (or first CSV column). Enqueued at startup and crawled first |
| `--nodb` / `--dry-run` | | no postgres, no redis: everything except the writes. Needs `-u`, `-f` or `--seed-file` |
| `--follow` | | with `--nodb`: also crawl found links (kept in memory) |
| `--no-enqueue` | | with a database: don't add discovered links to `url_queue` |
| `--max-pages N` | 0 | stop after starting N pages |
| `--max-time S` | 0 | stop after S seconds |
| `--max-pages-per-host N` | 4 | pages per host per run (`www.x.com` and `x.com` count as one host) |
| `--fetchers N` | 64 | concurrent downloads (never two to the same host) |
| `--parsers N` | cores-1 | parser processes |
| `--batch-pages N`, `--flush-interval S` | 25, 1.0 | pages per transaction at most, and how often a flush happens |
| `--min-delay S` | 3.0 | minimum gap between requests to one host (plus 0-25% jitter) |
| `--max-crawl-delay S` | 30 | cap for a robots.txt `Crawl-delay` |
| `--no-redis` | | don't share host cooldowns with other scrapers |
| `--internal-links N`, `--external-hosts N` | 3, 30 | links queued per page: shallowest same-host ones, and one link for each of N other hosts |
| `--blocklist FILE` | `blocklist.db` | local blocklist (sqlite, same schema as `update_local_blocklist.py`) |
| `--claim-batch N` | 200 | urls popped from the queue at a time; also bounds what a hard kill can lose (about `claim_batch` + in-flight urls) |
| `--db-watchdog S` | 120 | abort and retry a database flush that runs longer than S seconds; complain when no flush completed for S seconds while work waits |
| `--parse-timeout S` | 60 | abandon a page's parse after S seconds |
| `--exit-when-empty` | | exit when the queue is empty instead of waiting |
| `--dump FILE`, `-v` | | JSON line per page; print every log message |

`.env` in the repo root is loaded automatically (real environment variables win).

## Design

* **asyncio + aiohttp for the network, a process pool for the CPU.** A crawl is mostly waiting (DNS, TLS, slow servers), and 64+ threads cost far more memory than 64+ coroutines on a 3 GB Pi. The parse (lxml, trafilatura, language detection, tokenizing) is CPU-bound Python, so it runs in `--parsers` spawned processes, which also keeps the event loop responsive. Backpressure: a fetch task holds its slot until its page is parsed and queued for the writer.
* **Scheduler (`crawl_lib/frontier.py`).** Per-host queues and a heap of "host ready at time t". One request in flight per host, delay = max(`--min-delay`, robots `Crawl-delay`) after each request, shallowest url first, hard cap per host. The delay clock starts when the download ends, not after the parse. With Redis reachable, the same `domain:<host>` keys as `scrape.py` are used, so both kinds of scraper respect each other's cooldowns. If Redis is down, in-process politeness is used.
* **Breadth.** Links are chosen in the parser: non-HTML extensions are dropped, a page queues up to `--internal-links` shallow links of its own host and one shallowest link for each of up to `--external-hosts` other hosts. Hosts already at their cap, blocklisted hosts and urls already in flight are not queued. Popped urls that belong to a full host are dropped.
* **Checks per url.** Blocklist (set in memory), public-IP check (cached DNS, same rule as `utils.is_public_host`), robots.txt (cached per origin for a day, errors cached 10 minutes, permissive on errors like the old scraper), same-host redirects followed hop by hop with the same checks, other-host redirects queued, max page size, `noindex`/`nofollow`/`noarchive` honored. Differences from `scrape.py`: HTTP error pages (404/500 with an HTML body) are not indexed; `example.com` to `www.example.com` redirects are followed in place.
* **Parse once (`crawl_lib/parse.py`).** `extract_page()` parses with lxml once and hands the tree to trafilatura (the old code did a BeautifulSoup parse and a second parse in trafilatura). Same output contract as `utils.extract_data_from_html`, which now calls it (the old implementation stays as `utils._extract_data_from_html_bs4` and `tests/test_fast_crawl_parse.py` checks they agree). Pages over 1 MB skip trafilatura's slow fallback extractors. The language decision uses `tokenizer.language_gate(text)` when it exists, else the old rule (English only).
* **Writes (`crawl_lib/db.py`).** One writer thread, one transaction per ≤25 pages: urls rows, new words/prefixes (`NOT EXISTS ... ORDER BY ... ON CONFLICT DO NOTHING` so no sequence ids are burned and writers lock in the same order), postings with `COPY`, `urls_references`, `reference_count` updates, queued links and logs as multi-row statements. `total_tokens` is computed exactly like `utils.store()` (a test compares the two). Log messages keep the dashboard formats (`Scraped <url>`, `Error ...`, `Blocked by robots.txt ...`, `Language Not in English ...`, plus `Misc Language <code> <url>`). A failed batch is retried, then written page by page; a page that can't be stored is logged and dropped.
* **Production schema quirks.** Already-stored pages are skipped (the postings tables have no `url_id` index, so re-storing would scan them); a seen-only stub (`total_tokens IS NULL`) is filled in without any DELETE. `url_queue` without `claimed_at` uses the claimless protocol: pop = `DELETE ... FOR UPDATE SKIP LOCKED RETURNING`, in-flight urls are tracked in memory, unfinished ones are re-inserted at shutdown; inserts use explicit ids (`max(id)+row_number`) because the id sequence is exhausted, retried on collisions. `utils.get_next_urls`, `finish_claims` and `enqueue_urls` detect the schema once and do the same, so `scrape.py` works too. If a process is killed hard (SIGKILL), its in-flight urls are lost in claimless mode (SIGINT/SIGTERM re-insert them).
* **Connections and threads.** A psycopg2 connection is never shared between threads. `DbWriter` (crawl_lib/db.py) owns the writer thread's connection and raises if two threads use it at once; seeds, claims, hand-backs and the progress estimate go through `QueueDb`, which has its own connection (one caller at a time). Every crawler connection is opened with `lock_timeout=15s`, `idle_in_transaction_session_timeout=60s`, `statement_timeout=300s` (the queue connection 60s) and `application_name=fast_crawl`, and connections made through `utils.get_conn()` get the same via `PGOPTIONS`. Lock timeouts and cancelled statements are retried like deadlocks.
* **Watchdog.** A flush running longer than `--db-watchdog` seconds is aborted (cancel + socket shutdown from the event-loop thread, so the server rolls the transaction back and drops its locks) and retried on a fresh connection; if the writer thread does not come back, it is abandoned and a new thread/connection takes over. The progress loop prints `lastflush=Ns`, never waits for the database (the `dbq~` estimate has a 3 s bound and shows `?` when the connection is busy) and also aborts a writer that completed nothing for `--db-watchdog` seconds while work is pending. All `WATCHDOG:` lines go to stdout.
* **Bounded frontier.** All seeds are inserted into `url_queue` up front, but only about `--claim-batch` urls are popped at a time (seeds first, then the normal queue), so a SIGKILL loses at most the popped urls, not the whole seed list. Per-host caps are kept in the frontier across claims. On SIGINT/SIGTERM every wait is bounded (5 s for in-flight pages, 4 s for the final flush, 4 s to hand claims back, hard exit after 22 s); unfinished popped urls are re-queued.
* **No DDL.** `utils.create_database()` now only checks (read-only) that the tables exist and raises if not; it runs DDL only with `STULTUS_ALLOW_DDL=1`. `populate_static_ngrams` raises instead of inserting unless that variable is set. Tests that build a throwaway database (`tests/test_url_queue_claims.py` etc.) need `STULTUS_ALLOW_DDL=1`.
* **Optional pieces.** The blocklist server (`http://<dbhost>:8002/blocklist.db`) is tried with a 5 s timeout; if it is down a warning is printed and the local `blocklist.db` (or nothing) is used. Build one without Postgres: `python -m crawl_lib.blocklist build`. `WEB_TEXT_STORAGE_SERVER_ADDRESS` and `SCRAPER_PROXY` are used when set and ignored otherwise.
* **Shutdown.** SIGINT/SIGTERM: stop dispatching, let in-flight pages finish (25 s), flush the writer, put unfinished urls back, exit.

## Memory

On the 4-core Pi, 3 parsers use about 240-315 MB each and the main process 200-400 MB (up to 64 downloads of up to 5 MB each can be in memory), roughly 1.2-1.3 GB in total. Use `--parsers 2` or `--fetchers 32` if you need more room.

## Tests

```bash
python -m pytest tests/test_fast_crawl_parse.py tests/test_fast_crawl_core.py          # no database needed
TEST_DATABASE_URL=postgresql://user@host/stultus_test STULTUS_ALLOW_DDL=1 python -m pytest tests/test_fast_crawl_core.py   # adds the database tests
```

The end-to-end test crawls a local server with five loopback "hosts" and checks one request in flight per host, delays, the per-host cap, robots.txt, skipped PDFs, cross-host redirects, 404s and a 429 host.

## English only (default)

`INDEX_LANGUAGES` defaults to `en`. In that mode the crawler:

- indexes only pages the language gate (`tokenizer.language_gate`) detects as English; pages with no detectable language pass only if their letters are mostly Latin
- skips urls that are very likely non-English *before fetching them*: non-English country TLDs (`.de`, `.ru`, `.jp`, `.com.br`, ...), language subdomains (`de.wikipedia.org`, `pt-br.example.com`) and language path prefixes (`/fr/`, `/de-de/`), see `crawl_lib/urls.looks_foreign`
- once a host serves a non-English page, fetches nothing more from it and doesn't follow that page's links

Set `INDEX_LANGUAGES=all` (or a list such as `en,de`) to index other languages with the multilingual tokenizer.
