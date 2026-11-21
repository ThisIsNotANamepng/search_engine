# Frontend

Flask app in `frontend/app.py` (templates in `frontend/templates`, assets in `frontend/static`). Pages: `/` (search box), `/search`, `/creators`, `/dashboard`. JSON: `/api/search`, `/dashboard/live`, `/dashboard/data`.

## Search

- `/search?q=&limit=&offset=` renders the results page. An empty or blank `q` redirects to `/`. Queries are trimmed and cut at 200 characters. `limit` is 1-50 (default 10), `offset` is 0-1000. Previous/Next links appear under the results; the app asks `search.search(q, limit + 1, offset)` for one extra row to know whether a next page exists.
- `/api/search?q=&limit=&offset=` returns `{query, limit, offset, count, has_more, took_ms, results: [{url, title, score, relevance, ref_score, reference_count}]}`. Missing `q` is a 400; a database failure is a 503.
- Result links only use `http`/`https` URLs (anything else becomes `#`); text is escaped by Jinja autoescape.

## Settings menu

The gear button (bottom-right corner of the index and search pages, `static/js/settings.js` + `static/css/settings.css`) opens a small dialog. Settings live in `localStorage["stultus.settings"]`; a tiny inline script in `<head>` (`templates/_settings_head.html`) sets `class="debug"` on `<html>` before first paint.

| Setting | Default | Effect |
|---|---|---|
| Debug mode | off | Shows the per-result Search Score / Relevance / Ref Score / Reference Ct block, plus a line with the result count and query time. Off hides them and the title/URL column takes the full row. |
| Open results in a new tab | off | Result links get `target="_blank" rel="noopener noreferrer"`. |
| Results per page | 10 | 10/20/30, sent as `?limit=`. A direct visit to `/search` without `limit` is redirected to the saved value. |

There is no theme setting: the CSS only defines a dark palette (and many colors are hard-coded), so a light theme would be a redesign.

## Server details

- `/static` is served with a one-year cache lifetime. Templates link assets with `asset('css/x.css')`, which appends `?v=<file mtime>`, so edited files get a new URL. Never link `/static/...` by hand.
- Text responses (HTML, JSON, CSS, JS) are gzip-compressed by a small `after_request` hook when the client accepts it.
- The control-plane table `crawler_nodes` is **not** created at startup any more. The app only checks for it (read-only) and logs a warning if it is missing; `/agent/heartbeat` and `/nodes/<id>/scale` then return a 503 JSON error. Start the app once with `STULTUS_ALLOW_DDL=1` to create the table.

## UI testing without a database

```bash
NLTK_DATA=~/nltk_data python tests/frontend_dev_server.py --port 5055
```

Serves the real app with a dummy `DATABASE_URL`, all DB access blocked, canned search results (queries containing `empty`, `none`, `cjk`, `long`, `xss` select special data sets; anything else gives 35 rows) and stubbed dashboard stats. `--frontend-dir PATH` serves another copy of `frontend/` (e.g. a `git worktree` of an older commit) for before/after comparisons. The dev server also makes `settings.js` honor `?_debug=1`, `?_settings=open`, `?_newtab=1`, `?_perpage=N` so interactive states can be screenshotted.
