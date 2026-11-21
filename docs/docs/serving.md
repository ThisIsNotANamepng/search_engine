# Serving the web app

The search pages, `/api/search`, the dashboard and the node heartbeat endpoint are one Flask app (`frontend/app.py`). Run it with gunicorn, not `flask run` (the dev server is fine for development).

## Production command

From the repo root:

```bash
set -a; . ./.env; set +a            # DATABASE_URL
NLTK_DATA=~/nltk_data gunicorn -c gunicorn.conf.py frontend.app:app
```

or, as `deploy/db01/stultus-head.service` does, from `frontend/` (`python -m gunicorn -c ../gunicorn.conf.py app:app`; add `--bind 0.0.0.0:8000` to override the bind address). `gunicorn.conf.py` settings, all overridable with environment variables:

| Setting | Default | Variable | Why |
|---|---|---|---|
| worker class | `gthread` | | requests mostly wait on Postgres (releases the GIL); idle keep-alive connections cost no thread |
| workers | 1 | `WEB_CONCURRENCY` | see "Workers" below |
| threads per worker | 16 | `GUNICORN_THREADS` | |
| bind | `0.0.0.0:8000` | `GUNICORN_BIND` | |
| keep-alive | 30 s | `GUNICORN_KEEPALIVE` | a page view is HTML + a few assets on one connection (gunicorn's default is 2 s) |
| timeout | 120 s | `GUNICORN_TIMEOUT` | only the "worker is wedged" limit; searches stop at `statement_timeout` (5 s) |
| max requests | 100000 (+10% jitter) | `GUNICORN_MAX_REQUESTS` | slow-leak guard; recycling drops the in-process caches, so it is rare |
| preload app | off | `GUNICORN_PRELOAD=1` | no memory win here (see below) |
| heartbeat dir | `/dev/shm` | | not the SD card |

`post_worker_init` starts the app's background tasks inside each worker (`app.start_background_tasks()`): the dashboard stats prewarm and a **search warm-up**. The warm-up calls `search.warm_up()` if `search.py` defines it, otherwise runs one throwaway query, so the first visitor does not pay for the DB pool and corpus statistics. It never raises (a busy database only logs a warning). Environment switches: `STULTUS_WARMUP=0`, `DASHBOARD_PREWARM=0`. `STULTUS_DEFER_BACKGROUND=1` (set by `gunicorn.conf.py`) stops the app from starting those threads at import time; with `flask run` or `python app.py` they start at import as before.

### Workers

Default is **one worker with 16 threads**:

- every worker holds its own search caches / in-memory index, DB pool and dashboard stats thread; a second worker added about 225-260 MB (PSS) on this 3.7 GB Pi when measured (the in-memory index from `search_memindex.py` loads lazily and is per worker, so it grows with the index);
- the dashboard's search-timing panel and the heavy-stats cache live in the worker's memory, so with several workers each one shows only its share and refreshes the slow stats separately;
- the DB is the scarcer resource: Postgres `max_connections` was 100 and about 100 sessions were open (crawlers), so every extra pool is a risk of "too many clients".

If search CPU is the bottleneck and RAM allows, `WEB_CONCURRENCY=2` gave about +30% throughput (below). `GUNICORN_PRELOAD=1` was measured and does not help: Python's refcounting dirties the shared pages, so the master just keeps a second copy of the app (~140 MB private).

## What the frontend does for speed

- **No third-party requests.** Forum is self-hosted (`static/fonts/forum-*.woff2`, the same four Google subsets, same `unicode-range`, `font-display: swap`) and the latin file is `<link rel=preload>`ed. The CSS (about 9 KB) is inlined into a `<style>` block by `inline_css()`, so there are no render-blocking requests (before: Google Fonts CSS + 3 stylesheets). The stylesheets in `static/css` are unchanged; inlining only strips comments and indentation.
- **Header first.** If a search takes longer than 40 ms (`SEARCH_STREAM_AFTER_SECONDS`), `/search` sends the page head + search box immediately (gzip sync-flushed per chunk) and the results as a second chunk, so fonts/CSS/paint overlap the database work. Faster searches (every cached query) are answered in one piece with a real 503 on failure. Browser prefetches wait for the whole page. A search that fails *after* the header went out says so in the page (status 200, `Cache-Control: no-cache`).
- **One search per request.** The saved results-per-page is mirrored into a `stultus_limit` cookie (set by the inline head script and `settings.js`), and `/search` without `?limit=` uses it. The old client-side redirect, which ran every such search twice, is now only a one-time fallback for a browser that has no cookie yet.
- **Caching headers.** `/` and `/creators`: `max-age=60, stale-while-revalidate=600` + ETag (304 on revalidation). `/search`: `private, max-age=30` + `Vary: Cookie` (back/forward, reload, prefetched next page). `/api/search`: `public, max-age=60, stale-while-revalidate=300`. Versioned static files and fonts: `public, max-age=31536000, immutable`. 5xx: `no-store`. Static text is gzipped once (level 9) and kept in memory.
- **Next-page prefetch.** Hovering/touching/focusing the "Next" link adds a `<link rel=prefetch>`. Not on page load: every prefetch is a real search.
- **Search layer in front of `search.py`** (`_run_search`): a 60 s cache of result pages (`SEARCH_RESULT_CACHE_SECONDS`, 256 entries); identical concurrent searches are coalesced into one; at most `SEARCH_MAX_CONCURRENT` (default: `search.POOL_MAX`) searches run at once, extra ones queue up to `SEARCH_QUEUE_TIMEOUT_SECONDS` (10) and then fail as 503, so a burst cannot open more Postgres connections than the pool; and **stale-if-error**: if a search fails, a result page up to `SEARCH_STALE_SECONDS` (1 h) old is served instead of an error.
- **Dashboard.** `/dashboard` is a page shell (no queries) and `/dashboard/data` serves the cached heavy stats (already stale-while-revalidate). `/dashboard/live` used to run a node query and a url_queue count on every poll (about 1.6 s on the real DB, the count hits its 1 s `statement_timeout`); nodes (2 s) and queue (3 s) are now served stale-while-revalidate, so a poll takes about 5 ms and N open dashboards cost one refresh per interval.

## Measurements

Raspberry Pi, 4 cores, 3.7 GB RAM, remote Postgres, **shared with other running jobs** (load average 2-4 during the runs; the load generator ran on the same machine). Treat differences under ~10% as noise. "Before" = the old frontend on the Flask dev server (port 91xx); "after" = the new frontend under `gunicorn -c gunicorn.conf.py` with 1 worker. Both ran the same `search.py`.

### Page load (headless Chrome, medians of 3-5 runs, ms)

| | before | after |
|---|---|---|
| requests per page view (cold) | 7 | 3 |
| render-blocking resources | 4 | 0 |
| `/` cold, localhost: first contentful paint / load | 304 / 419 | 224 / 174 |
| `/` cold, 4G emulation (1.6 Mbit/s, 150 ms RTT): FCP / LCP / load | 652 / 1124 / 1006 | 396 / 596 / 515 |
| `/search` cold (cached query), 4G: FCP / load | 548 / 875 | 416 / 520 |
| `/search` for a not-yet-cached query, localhost: TTFB / FCP / LCP / load | 328 / 728 / 728 / 708 | 56 / 272 / 312 / 263 |
| same, 4G | TTFB 191, FCP 564, load 899 | TTFB 60, FCP 404, load 522 |
| revisit within 30-60 s, 4G: FCP | 288 | 128 |
| HTML size, gzip (`/` / `/search`) | 929 B / ~2.0 KB | 2.8 KB / ~4.1 KB (CSS and font-face inlined) |

Also measured: with a saved page size of 20, a direct `/search?q=` visit made two document requests (two searches) before and makes one after (the first visit of a browser with no cookie still redirects once).

### Server time (curl, idle box)

| | before | after |
|---|---|---|
| `/`, `/creators`, static files, `/api/search` (warm) | 4-5 ms TTFB | 4-5 ms TTFB (unchanged) |
| `/search`, repeated query | 6.5-7.6 ms | 7.3-8.2 ms |
| `/search`, first request of a new query: TTFB | = search time (53-358 ms in this run) | 46-50 ms (header streamed), total = search time |
| `/dashboard/live` | 1.6 s | ~5 ms (first call after start 2.7 s) |

### Load test (threads + `requests`, 25 s per row, same machine; 60 popular queries, 70% `/search`, 20% `/api/search`, 10% `/` + static)

| scenario | server | req/s | p50 | p95 | p99 | errors |
|---|---|---|---|---|---|---|
| 4 clients, all cached | dev server | 133 | 29 ms | 44 ms | 50 ms | 0 |
| | gunicorn, 1 worker | 163 | 24 ms | 36 ms | 41 ms | 0 |
| 16 clients, all cached | dev server | 137 | 116 ms | 137 ms | 148 ms | 0 |
| | gunicorn, 1 worker | 164 | 94 ms | 148 ms | 183 ms | 0 |
| 16 clients, 10% uncached queries | dev server | 118 | 125 ms | 243 ms | 308 ms | 0 |
| | gunicorn, 1 worker | 132 | 100 ms | 305 ms | 420 ms | 0 |
| 48 clients, 30% uncached | dev server | 90 | 494 ms | 720 ms | 923 ms | 0 |
| | gunicorn, 1 worker | 101 | 406 ms | 815 ms | 952 ms | 0 |
| 4 / 16 clients, all cached (earlier build) | gunicorn, 1 worker | 151 / 152 | 25 / 101 ms | 40 / 163 ms | | 0 |
| | gunicorn, 2 workers | 197 / 195 | 20 / 75 ms | 30 / 158 ms | | 0 |

Reading: when queries are cached the server is CPU-bound (Flask + Jinja + gzip on a Pi, plus the load generator on the same cores), and gunicorn with keep-alive is about 20% faster than the dev server at lower median latency. With many uncached queries the tail (p95/p99) is not better: that time is `search.py` and the GIL, not serving. A second worker gives about +30% throughput on cached traffic for ~250 MB. The dev server stayed up and error-free in these runs as well; it is single-process and has no keep-alive, so it is not recommended beyond development.

An earlier run happened while Postgres had reached `max_connections` (the crawlers use nearly all of them): the web app then answers 503 for searches whose result isn't cached. The search layer above (cache, coalescing, concurrency cap, stale-if-error) exists for that case; the numbers above were taken when connections were available. Keep `SEARCH_POOL_MAX` small on a database shared with the crawlers.
