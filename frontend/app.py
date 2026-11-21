"""
Dashboard

Graphs:
- Pie chart scraped vs blocked
- Bar chart scraped vs blocked
- List of current scrapers and how many urls they've scraped, how long they've been alive
- Current urls scraped per minute
- File size of the postgres database
- Number of urls in the database
- Number of domains in the database
- 
"""

import os, sys, time, threading, socket, collections, gzip, logging
import urllib.request, urllib.error
from urllib.parse import urlsplit, urlencode
from concurrent.futures import ThreadPoolExecutor

# search.py lives in the directory above this one
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, render_template, request, jsonify, redirect
import search
import psycopg2
import psycopg2.extras
import json

log = logging.getLogger("stultus.frontend")

app = Flask(__name__)
# /static files are served with a 1-year cache lifetime; templates add ?v=<mtime> to
# every static link (see asset()) so a changed file gets a new URL.
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 31536000

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is not set")

MAX_DESIRED = 64  # crawlers per node; a typo shouldn't spawn thousands of containers

def get_db():
    return psycopg2.connect(
        DATABASE_URL,
        sslmode="disable"
    )

# A node is considered stale (agent silent / box down) if we haven't heard a
# heartbeat from it in this many seconds.
NODE_STALE_SECONDS = 30

def crawler_nodes_exists():
    """Read-only check (no DDL) that the crawler_nodes table exists."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT to_regclass('public.crawler_nodes') IS NOT NULL;")
        return bool(cur.fetchone()[0])
    finally:
        conn.close()


def init_control_schema():
    """
    The crawler_nodes table is used by the node agents' heartbeats. This is
    the whole 'control plane' state that replaced Kubernetes: one row per box,
    holding the desired crawler count (set from the dashboard) and the latest
    reported liveness/host stats.

    By default this only CHECKS (read-only) that the table exists and logs a
    warning if it doesn't. The CREATE TABLE only runs when STULTUS_ALLOW_DDL=1,
    so merely starting the web app never changes the database schema.
    """
    if crawler_nodes_exists():
        return
    if os.getenv("STULTUS_ALLOW_DDL") != "1":
        log.warning("table crawler_nodes does not exist; heartbeat/scale endpoints will "
                    "return 503 (set STULTUS_ALLOW_DDL=1 to let the app create it)")
        return
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS crawler_nodes (
                node_id       TEXT PRIMARY KEY,
                hostname      TEXT,
                desired_count INTEGER NOT NULL DEFAULT 0,
                alive_count   INTEGER NOT NULL DEFAULT 0,
                cpu_percent   REAL,
                mem_percent   REAL,
                procs         JSONB,
                first_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
                last_seen     TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """)
        conn.commit()
    finally:
        conn.close()

# Runs at import (so it also runs under gunicorn). It's read-only unless
# STULTUS_ALLOW_DDL=1; an unreachable database must not stop the search pages.
try:
    init_control_schema()
except Exception as e:
    log.warning("could not check crawler_nodes at startup: %s", e)


def _control_plane_unavailable():
    """A 503 JSON response if crawler_nodes is missing/unreachable, else None."""
    try:
        if crawler_nodes_exists():
            return None
        reason = "crawler_nodes table does not exist (start with STULTUS_ALLOW_DDL=1 to create it)"
    except Exception as e:
        reason = f"database unavailable: {type(e).__name__}"
    return jsonify({"error": "control plane unavailable", "detail": reason}), 503


@app.route("/agent/heartbeat", methods=["POST"])
def agent_heartbeat():
    """
    Receive a heartbeat from a node agent and return its desired crawler count.

    The desired count is authoritative on the head: a brand-new node adopts the
    count the agent reports (its INITIAL_DESIRED); after that the dashboard owns
    it and this endpoint never overwrites it.
    """
    data = request.get_json(silent=True) or {}
    node_id = data.get("node_id")
    if not node_id:
        return jsonify({"error": "missing node_id"}), 400
    unavailable = _control_plane_unavailable()
    if unavailable:
        return unavailable

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO crawler_nodes
                (node_id, hostname, desired_count, alive_count,
                 cpu_percent, mem_percent, procs, last_seen)
            VALUES (%s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (node_id) DO UPDATE SET
                hostname    = EXCLUDED.hostname,
                alive_count = EXCLUDED.alive_count,
                cpu_percent = EXCLUDED.cpu_percent,
                mem_percent = EXCLUDED.mem_percent,
                procs       = EXCLUDED.procs,
                last_seen   = now()
            RETURNING desired_count;
        """, (
            node_id,
            data.get("hostname"),
            int(data.get("desired_count", 0)),   # only used on first INSERT
            int(data.get("alive_count", 0)),
            data.get("cpu_percent"),
            data.get("mem_percent"),
            json.dumps(data.get("procs", [])),
        ))
        desired_count = cur.fetchone()[0]
        conn.commit()
    finally:
        conn.close()

    return jsonify({"desired_count": desired_count})

@app.route("/nodes/<node_id>/scale", methods=["POST"])
def scale_node(node_id):
    """Set a node's desired crawler count. The agent picks it up on next heartbeat."""
    data = request.get_json(silent=True) or {}
    try:
        desired = int(data.get("desired"))
    except (TypeError, ValueError):
        return jsonify({"error": "desired must be an integer"}), 400
    if not 0 <= desired <= MAX_DESIRED:
        return jsonify({"error": f"desired must be between 0 and {MAX_DESIRED}"}), 400
    unavailable = _control_plane_unavailable()
    if unavailable:
        return unavailable

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE crawler_nodes SET desired_count = %s WHERE node_id = %s;",
            (desired, node_id),
        )
        updated = cur.rowcount
        conn.commit()
    finally:
        conn.close()

    if not updated:
        return jsonify({"error": "unknown node"}), 404
    return jsonify({"node_id": node_id, "desired_count": desired})


# ---- Heavy dashboard queries ------------------------------------------------
# `logs` is append-only, so the scraped/blocked/error totals only need the rows added
# since the last refresh. The running totals are kept in a small JSON file (so a restart
# doesn't force a multi-minute recount) and are recounted from scratch every
# DASHBOARD_FULL_RECOUNT_SECONDS to correct any drift (e.g. if logs were ever pruned).
DASHBOARD_STATE_FILE = os.getenv(
    "DASHBOARD_STATE_FILE",
    os.path.join(os.path.expanduser("~"), ".cache", "stultus_dashboard_counts.json"))
DASHBOARD_FULL_RECOUNT_SECONDS = int(os.getenv("DASHBOARD_FULL_RECOUNT_SECONDS", "86400"))
DASHBOARD_HEAVY_CACHE_SECONDS = int(os.getenv("DASHBOARD_HEAVY_CACHE_SECONDS", "600"))
# Rows just below max(id) may belong to transactions that haven't committed yet; stay
# this far behind so they are counted on the next refresh instead of being skipped.
_LOG_ID_SAFETY_MARGIN = 1000

_STATUS_COUNT_SQL = """
    SELECT
        CASE
            WHEN message LIKE 'Sc%%' THEN 'scraped'
            WHEN message LIKE 'Bl%%' THEN 'blocked'
            WHEN message LIKE 'Er%%' THEN 'error'
        END AS status,
        COUNT(*) AS count
    FROM logs
    WHERE id > %s AND id <= %s
      AND (message LIKE 'Sc%%' OR message LIKE 'Bl%%' OR message LIKE 'Er%%')
    GROUP BY status;
"""


def _load_counts_state():
    try:
        with open(DASHBOARD_STATE_FILE) as f:
            state = json.load(f)
        if {"last_id", "counts", "full_at"} <= state.keys():
            return state
    except (OSError, ValueError):
        pass
    return None


def _save_counts_state(state):
    try:
        os.makedirs(os.path.dirname(DASHBOARD_STATE_FILE), exist_ok=True)
        tmp = DASHBOARD_STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, DASHBOARD_STATE_FILE)
    except OSError:
        pass  # caching only; the dashboard still works without the file


def _status_counts(cur):
    """Same result as the old single GROUP BY over all of `logs`, computed incrementally."""
    state = _load_counts_state()
    now = time.time()
    if state is None or now - state["full_at"] > DASHBOARD_FULL_RECOUNT_SECONDS:
        state = {"last_id": 0, "counts": {}, "full_at": now}

    cur.execute("SELECT COALESCE(MAX(id), 0) AS m FROM logs;")
    upto = max(cur.fetchone()["m"] - _LOG_ID_SAFETY_MARGIN, state["last_id"])
    if upto > state["last_id"]:
        cur.execute(_STATUS_COUNT_SQL, (state["last_id"], upto))
        for row in cur.fetchall():
            state["counts"][row["status"]] = state["counts"].get(row["status"], 0) + row["count"]
        state["last_id"] = upto
        _save_counts_state(state)

    return [{"status": k, "count": v} for k, v in sorted(state["counts"].items()) if v]


_unique_domains_cache = {"at": 0.0, "value": None}


def _unique_domains(cur):
    if (_unique_domains_cache["value"] is None
            or time.time() - _unique_domains_cache["at"] > DASHBOARD_HEAVY_CACHE_SECONDS):
        cur.execute("""
            SELECT COUNT(DISTINCT SUBSTRING(url FROM '^(?:https?://)?([^/]+)'))
                AS unique_domains
            FROM urls;
        """)
        _unique_domains_cache["value"] = cur.fetchone()["unique_domains"]
        _unique_domains_cache["at"] = time.time()
    return _unique_domains_cache["value"]


def _compute_dashboard_stats():
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Scraped vs Blocked vs Error counts. Counting ~80M log rows takes minutes, so this is
    # maintained incrementally (see _status_counts).
    status_counts = _status_counts(cur)

    # Total unique domains (a ~30s scan of `urls`, so refreshed at most every
    # DASHBOARD_HEAVY_CACHE_SECONDS and reused in between)
    unique_domains = _unique_domains(cur)

    # Scrapes in the last minute (uses logs_scraped_ts_idx partial index)
    cur.execute("""
        SELECT COUNT(*) AS count
        FROM logs
        WHERE message LIKE 'Scraped%'
        AND ts >= now() - INTERVAL '1 minute';
    """)
    scrapes_per_minute = cur.fetchone()['count']

    # Daily + cumulative scrapes in one pass
    cur.execute("""
        SELECT
            date_trunc('day', ts) AS day,
            COUNT(*) AS daily_scrapes,
            SUM(COUNT(*)) OVER (ORDER BY date_trunc('day', ts)) AS cumulative_scrapes
        FROM logs
        WHERE message LIKE 'Scraped%'
        GROUP BY date_trunc('day', ts)
        ORDER BY day;
    """)
    cumulative_scrapes_per_day = cur.fetchall()
    scrapes_over_time = [
        {"minute": r["day"], "count": r["daily_scrapes"]}
        for r in cumulative_scrapes_per_day
    ]

    # Last 8 urls (uses logs_scraped_id_idx partial index)
    cur.execute("""
        SELECT message, ts
        FROM logs
        WHERE message LIKE 'Scraped%'
        ORDER BY id DESC
        LIMIT 8;
    """)
    last_10_scraped = cur.fetchall()
    real_last_10_scraped = [
        (i['message'][i['message'].index(" "):], i['ts'])
        for i in last_10_scraped
    ]

    # Active scrapers (last three minutes)
    cur.execute("""
        SELECT
            ip,
            COUNT(*) AS urls_scraped,
            MIN(ts) AS started_at,
            MAX(ts) AS last_seen
        FROM logs
        WHERE message LIKE 'Scraped%'
        GROUP BY ip
        HAVING MAX(ts) >= NOW() - INTERVAL '3 minutes';
    """)
    scrapers = cur.fetchall()

    # URL count and DB size
    cur.execute("""
        SELECT
            (SELECT COUNT(*) FROM urls) AS url_count,
            pg_size_pretty(pg_database_size(current_database())) AS db_size;
    """)
    totals = cur.fetchone()
    url_count = totals["url_count"]
    db_size = totals["db_size"]

    cur.close()
    conn.close()

    return {
        "status_counts": status_counts,
        "unique_domains": unique_domains,
        "scrapes_per_minute": scrapes_per_minute,
        "scrapes_over_time": scrapes_over_time,
        "cumulative_scrapes_per_day": cumulative_scrapes_per_day,
        "real_last_10_scraped": real_last_10_scraped,
        "scrapers": scrapers,
        "url_count": totals["url_count"],
        "db_size": totals["db_size"],
    }

# The stats queries scan millions of `logs`/`urls` rows and are slow on the production
# database (minutes cold), so they never run inside a page request:
#   * /dashboard returns the page shell immediately;
#   * the page's JavaScript calls /dashboard/data, which returns the cached stats (or
#     {"ready": false} while the first computation is still running in the background);
#   * stale stats are served instantly while one background thread refreshes them
#     (stale-while-revalidate, DASHBOARD_CACHE_SECONDS).
# Node heartbeats are cheap and stay live (rendered with the page).
DASHBOARD_CACHE_SECONDS = int(os.getenv("DASHBOARD_CACHE_SECONDS", "60"))
DASHBOARD_RETRY_AFTER_ERROR_SECONDS = 30
_dashboard_cache = {"at": 0.0, "stats": None, "refreshing": False, "error": None, "error_at": 0.0}
_dashboard_lock = threading.Lock()


def _refresh_dashboard_stats():
    try:
        stats = _compute_dashboard_stats()
        with _dashboard_lock:
            _dashboard_cache.update(stats=stats, at=time.time(), error=None)
    except Exception as e:  # keep serving stale data; retry later
        with _dashboard_lock:
            _dashboard_cache.update(error=str(e), error_at=time.time())
    finally:
        with _dashboard_lock:
            _dashboard_cache["refreshing"] = False


def get_dashboard_stats_nowait():
    """Return the cached stats (None if never computed) and start a background refresh if needed."""
    with _dashboard_lock:
        stats = _dashboard_cache["stats"]
        stale = stats is None or time.time() - _dashboard_cache["at"] > DASHBOARD_CACHE_SECONDS
        recently_failed = time.time() - _dashboard_cache["error_at"] < DASHBOARD_RETRY_AFTER_ERROR_SECONDS
        start = stale and not _dashboard_cache["refreshing"] and not recently_failed
        if start:
            _dashboard_cache["refreshing"] = True
    if start:
        threading.Thread(target=_refresh_dashboard_stats, daemon=True).start()
    return stats


# Compute the stats at startup so the first dashboard visit finds them ready.
if os.getenv("DASHBOARD_PREWARM", "1") != "0":
    get_dashboard_stats_nowait()


@app.route("/dashboard")
def dashboard():
    """Page shell only: no queries at all, so it renders immediately. The page's JavaScript
    polls /dashboard/live (cheap, every few seconds) and /dashboard/data (slow, cached)."""
    return render_template("dashboard.html", node_stale_seconds=NODE_STALE_SECONDS)


# ---- Live dashboard data: nodes, service health, search timings ---------------
# Everything here is cheap, so the page polls it every few seconds.

# Timings of real /search requests. Kept in memory: the head runs a single gunicorn
# process (see deploy/db01/stultus-head.service), so every request lands here.
_search_timings = collections.deque(maxlen=2000)  # (unix_time, seconds, result_count)
_search_timings_lock = threading.Lock()
SEARCH_STATS_WINDOW_SECONDS = 3600


def _search_stats():
    now = time.time()
    with _search_timings_lock:
        recent = [t for t in _search_timings if now - t[0] <= SEARCH_STATS_WINDOW_SECONDS]
    durations = sorted(t[1] for t in recent)

    def pct_ms(p):
        if not durations:
            return None
        return round(1000 * durations[min(len(durations) - 1, int(p * len(durations)))])

    return {
        "window_s": SEARCH_STATS_WINDOW_SECONDS,
        "count": len(recent),
        "p50_ms": pct_ms(0.50),
        "p95_ms": pct_ms(0.95),
        "max_ms": round(1000 * durations[-1]) if durations else None,
        # Timings only, never the query text: this page shouldn't expose what people search for.
        "recent": [{"at": at, "ms": round(1000 * s), "results": n}
                   for at, s, n in reversed(recent[-10:])],
    }


# Services the crawlers depend on. They all run on db01 next to the head, so they're
# checked on localhost; DASHBOARD_HEALTH_HOST overrides that.
HEALTH_HOST = os.getenv("DASHBOARD_HEALTH_HOST", "127.0.0.1")
HEALTH_TIMEOUT_SECONDS = 2.0
HEALTH_CACHE_SECONDS = 5.0


def _check_tcp(port, send=None, ok_replies=()):
    with socket.create_connection((HEALTH_HOST, port), timeout=HEALTH_TIMEOUT_SECONDS) as s:
        if send:
            s.sendall(send)
            reply = s.recv(64)
            if not reply.startswith(tuple(ok_replies)):
                raise RuntimeError(f"unexpected reply {reply[:24]!r}")
    return None


def _check_http(port, path, method="GET"):
    req = urllib.request.Request(f"http://{HEALTH_HOST}:{port}{path}", method=method)
    try:
        with urllib.request.urlopen(req, timeout=HEALTH_TIMEOUT_SECONDS):
            return None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return "warn", "up, but returned 404"
        raise


def _check_postgres():
    conn = psycopg2.connect(DATABASE_URL, sslmode="disable", connect_timeout=HEALTH_TIMEOUT_SECONDS)
    try:
        conn.cursor().execute("SELECT 1;")
    finally:
        conn.close()
    return None


HEALTH_CHECKS = [
    # (name, what the crawlers use it for, check)
    ("Postgres", "index + URL queue", _check_postgres),
    # Redis answers PING with +PONG, or -NOAUTH if it wants a password (still up).
    ("Redis", "domain politeness locks", lambda: _check_tcp(6379, b"PING\r\n", (b"+PONG", b"-NOAUTH"))),
    ("Proxy", "all page fetches", lambda: _check_tcp(8888)),
    ("Text storage", "page text archive", lambda: _check_tcp(8003)),
    ("Blocklist", "blocked-domain list", lambda: _check_http(8002, "/blocklist.db", "HEAD")),
    ("Registry", "crawler images", lambda: _check_http(5000, "/v2/")),
    ("Registry cache", "image pull cache", lambda: _check_http(5001, "/v2/")),
]
_health_executor = ThreadPoolExecutor(max_workers=len(HEALTH_CHECKS), thread_name_prefix="health")
_health_cache = {"at": 0.0, "results": None}
_health_lock = threading.Lock()


def _run_check(name, purpose, check):
    started = time.perf_counter()
    try:
        outcome = check()
        status, detail = outcome if outcome else ("ok", "")
    except Exception as e:  # any failure to connect/answer means the service is down
        if isinstance(e, urllib.error.URLError) and not isinstance(e, urllib.error.HTTPError):
            e = e.reason if isinstance(e.reason, Exception) else e
        # "[Errno 111] Connection refused" -> "Connection refused"
        detail = (getattr(e, "strerror", None) or str(e) or type(e).__name__)
        status = "down"
    return {"name": name, "purpose": purpose, "status": status, "detail": detail,
            "ms": round(1000 * (time.perf_counter() - started))}


def _service_health():
    """All checks run in parallel; results are shared for a few seconds so several open
    dashboards don't multiply the probing."""
    with _health_lock:
        if _health_cache["results"] is not None and time.time() - _health_cache["at"] < HEALTH_CACHE_SECONDS:
            return _health_cache["results"]
        futures = [_health_executor.submit(_run_check, *c) for c in HEALTH_CHECKS]
        results = [f.result() for f in futures]
        _health_cache.update(at=time.time(), results=results)
        return results


def _crawler_nodes():
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # `procs` is the agent's list of running crawler containers (slot, container,
        # pid, uptime_s, restarts); `age_s` flags a node whose agent has gone silent.
        cur.execute("""
            SELECT node_id, hostname, desired_count, alive_count, cpu_percent, mem_percent,
                   procs, first_seen, last_seen,
                   EXTRACT(EPOCH FROM (now() - last_seen))::float AS age_s
            FROM crawler_nodes
            ORDER BY node_id;
        """)
        return cur.fetchall()
    finally:
        conn.close()


@app.route("/dashboard/live")
def dashboard_live():
    """Cheap, frequently polled dashboard state."""
    try:
        nodes, nodes_error = _crawler_nodes(), None
    except psycopg2.Error as e:
        nodes, nodes_error = [], str(e)
    payload = {
        "nodes": nodes,
        "nodes_error": nodes_error,
        "node_stale_seconds": NODE_STALE_SECONDS,
        "health": _service_health(),
        "search": _search_stats(),
        "server_time": time.time(),
    }
    return app.response_class(json.dumps(payload, default=str), mimetype="application/json")


@app.route("/dashboard/data")
def dashboard_data():
    """Slow dashboard stats as JSON (cached). {"ready": false} until the first computation finishes."""
    stats = get_dashboard_stats_nowait()
    if stats is None:
        with _dashboard_lock:
            error = _dashboard_cache["error"]
        return jsonify({"ready": False, "error": error}), 202
    payload = {
        "ready": True,
        "db_size": stats["db_size"],
        "url_count": stats["url_count"],
        "unique_domains": stats["unique_domains"],
        "scrapes_per_minute": stats["scrapes_per_minute"],
        "status_counts": stats["status_counts"],
        "scrapes_over_time": stats["scrapes_over_time"],
        "cumulative_scrapes_per_day": stats["cumulative_scrapes_per_day"],
        "last_10_scraped": stats["real_last_10_scraped"],
        "scrapers": stats["scrapers"],
    }
    # default=str keeps timestamps/decimals rendering exactly as the old server-side template did
    return app.response_class(json.dumps(payload, default=str), mimetype="application/json")

@app.route("/creators")
def creators():
    return render_template("creators.html")

@app.route("/")
def index():
    return render_template("index.html")


# ---- Search pages + JSON API ------------------------------------------------

MAX_QUERY_LEN = 200      # longer queries are truncated (they only make the tokenizer slower)
DEFAULT_LIMIT = 10
MAX_LIMIT = 50
MAX_OFFSET = 1000        # fast mode only ranks a few hundred candidates anyway


def _clean_query(raw):
    return (raw or "").strip()[:MAX_QUERY_LEN].strip()


def _int_arg(name, default, lo, hi):
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


def _run_search(query, limit, offset):
    """Returns (rows, has_more, seconds). Asks for one extra row to learn whether a next page exists."""
    started = time.perf_counter()
    rows = list(search.search(query, limit + 1, offset))
    elapsed = time.perf_counter() - started
    page = rows[:limit]
    with _search_timings_lock:
        _search_timings.append((time.time(), elapsed, len(page)))
    return page, len(rows) > limit, elapsed


@app.template_filter("safe_href")
def safe_href(url):
    """Only http(s) URLs may become a link target (blocks javascript:, data:, ...)."""
    url = (url or "").strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return "#"
    if parts.scheme.lower() in ("http", "https") and parts.netloc:
        return url
    return "#"


@app.route("/search")
def perform_search():
    query = _clean_query(request.args.get("q"))
    if not query:
        return redirect("/", 302)
    limit = _int_arg("limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
    offset = _int_arg("offset", 0, 0, MAX_OFFSET)

    def page_url(off):
        args = {"q": query, "limit": limit}
        if off:
            args["offset"] = off
        return "/search?" + urlencode(args)

    error = None
    try:
        results, has_more, elapsed = _run_search(query, limit, offset)
    except Exception:
        log.exception("search failed")
        results, has_more, elapsed, error = [], False, 0.0, "Search is temporarily unavailable."
    prev_url = page_url(max(0, offset - limit)) if offset > 0 else None
    next_url = page_url(offset + limit) if has_more and offset + limit <= MAX_OFFSET else None
    resp = app.make_response(render_template(
        "search.html", search_results=results, query_string=query, error=error,
        elapsed_ms=round(elapsed * 1000, 1), offset=offset, limit=limit,
        prev_url=prev_url, next_url=next_url))
    if error:
        resp.status_code = 503
    return resp


@app.route("/api/search")
def api_search():
    query = _clean_query(request.args.get("q"))
    if not query:
        return jsonify({"error": "missing q"}), 400
    limit = _int_arg("limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
    offset = _int_arg("offset", 0, 0, MAX_OFFSET)
    try:
        results, has_more, elapsed = _run_search(query, limit, offset)
    except Exception:
        log.exception("api search failed")
        return jsonify({"error": "search unavailable"}), 503
    return jsonify({
        "query": query,
        "limit": limit,
        "offset": offset,
        "count": len(results),
        "has_more": has_more,
        "took_ms": round(elapsed * 1000, 1),
        "results": [
            {"url": u, "title": t, "score": s, "relevance": r,
             "ref_score": rs, "reference_count": rc}
            for u, t, s, r, rs, rc in results
        ],
    })


# ---- Static assets: cache-busting links + gzip --------------------------------

@app.template_global("asset")
def asset(path):
    """/static/<path>?v=<mtime>: lets /static be cached for a year without serving stale files."""
    try:
        version = int(os.path.getmtime(os.path.join(app.static_folder, path)))
    except OSError:
        version = 0
    return f"/static/{path}?v={version}"


_GZIP_TYPES = ("text/", "application/json", "application/javascript", "image/svg+xml")
_GZIP_MIN_BYTES = 500
_gzip_static_cache = {}   # (path, etag) -> compressed bytes; static files are small and few


@app.after_request
def compress_response(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    try:
        if (resp.status_code != 200 or "Content-Encoding" in resp.headers
                or "gzip" not in request.headers.get("Accept-Encoding", "")
                or not resp.mimetype.startswith(_GZIP_TYPES)):
            return resp
        resp.headers.add("Vary", "Accept-Encoding")
        is_static = request.path.startswith("/static/")
        etag = resp.headers.get("ETag")
        cache_key = (request.path, etag) if is_static and etag else None
        if cache_key and cache_key in _gzip_static_cache:
            data = _gzip_static_cache[cache_key]
            resp.direct_passthrough = False
        else:
            resp.direct_passthrough = False
            raw = resp.get_data()
            if len(raw) < _GZIP_MIN_BYTES:
                return resp
            data = gzip.compress(raw, compresslevel=5)
            if cache_key:
                if len(_gzip_static_cache) > 64:
                    _gzip_static_cache.clear()
                _gzip_static_cache[cache_key] = data
        resp.set_data(data)
        resp.headers["Content-Encoding"] = "gzip"
        if etag:
            resp.set_etag(etag.strip('"').removeprefix("W/").strip('"'), weak=True)
    except Exception:
        log.exception("gzip failed; sending uncompressed")
    return resp


if __name__ == "__main__":
    # No debug=True: the Werkzeug debugger lets anyone who reaches it run code
    app.run(host="0.0.0.0", port=5000)
