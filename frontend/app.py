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

import os, re, sys, time, threading, socket, collections, gzip, logging, zlib
import urllib.request, urllib.error
from concurrent.futures import Future, wait as wait_futures
from urllib.parse import urlsplit, urlencode
from concurrent.futures import ThreadPoolExecutor

# search.py lives in the directory above this one
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, render_template, request, jsonify, redirect, stream_with_context, g
from markupsafe import Markup
from werkzeug.wsgi import ClosingIterator
import search
import psycopg2
import psycopg2.extras
import psycopg2.errors
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


# Compute the stats at startup so the first dashboard visit finds them ready. Under gunicorn
# (gunicorn.conf.py sets STULTUS_DEFER_BACKGROUND) this is started in each worker after the fork
# instead (start_background_tasks), because a thread started before a preload-fork does not exist
# in the workers.
def start_background_tasks():
    """Dashboard stats prewarm + search warm-up. Idempotent per process; never raises."""
    if getattr(sys.modules[__name__], "_background_started", False):
        return
    globals()["_background_started"] = True
    if os.getenv("DASHBOARD_PREWARM", "1") != "0":
        get_dashboard_stats_nowait()
    if os.getenv("STULTUS_WARMUP", "1") != "0":
        threading.Thread(target=_warm_search, name="search-warmup", daemon=True).start()


def _warm_search():
    """Pay the first-query costs (DB pool, corpus statistics, word-list caches) before a visitor
    does. Uses search.warm_up() when the search module has one; otherwise runs one throwaway query.
    Calls search directly, not _run_search, so it doesn't show up in the dashboard's search timings."""
    try:
        if hasattr(search, "warm_up"):
            search.warm_up()
        else:
            list(search.search_debug("the web", 10, 0))
    except Exception as e:
        log.warning("search warm-up failed: %s", e)


if os.getenv("STULTUS_DEFER_BACKGROUND") != "1":
    start_background_tasks()


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
    conn = psycopg2.connect(DATABASE_URL, sslmode="disable", connect_timeout=max(1, int(HEALTH_TIMEOUT_SECONDS)))  # libpq wants a whole number
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


# ---- URL queue: status for the dashboard, and "scrape this now" ---------------
# Crawlers claim the queue's lowest ids first (utils.get_next_urls: ORDER BY id), so a URL
# is pushed to the front by giving it an id below every other queued one. No crawler
# change needed; ids are BIGSERIAL so negative ids are fine.
QUEUE_PREVIEW = 8
MAX_URLS_PER_REQUEST = 50
MAX_URL_LENGTH = 2048  # url_queue.url is VARCHAR(2048)


def _queue_status():
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # Exact counts are cheap while the queue is small; past a second, fall back to the planner's estimate.
        cur.execute("SET statement_timeout = 1000;")
        try:
            cur.execute("""
                SELECT count(*) FILTER (WHERE claimed_at IS NULL)     AS waiting,
                       count(*) FILTER (WHERE claimed_at IS NOT NULL) AS claimed
                FROM url_queue;
            """)
            counts = dict(cur.fetchone(), estimated=False)
        except psycopg2.errors.QueryCanceled:
            conn.rollback()
            cur.execute("SELECT reltuples::bigint AS waiting FROM pg_class WHERE relname = 'url_queue';")
            counts = {"waiting": cur.fetchone()["waiting"], "claimed": None, "estimated": True}
        cur.execute("""
            SELECT url, enqueued_at, id < 0 AS pushed
            FROM url_queue WHERE claimed_at IS NULL ORDER BY id LIMIT %s;
        """, (QUEUE_PREVIEW,))
        return dict(counts, next=cur.fetchall())
    finally:
        conn.close()


def _normalize_url(raw):
    """Return (url, None) or (None, reason). Adds https:// when no scheme is given."""
    url = raw.strip()
    if not url:
        return None, "empty"
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or " " in url:
        return None, "not an http(s) URL"
    if len(url) > MAX_URL_LENGTH:
        return None, f"longer than {MAX_URL_LENGTH} characters"
    if not parts.path:
        url += "/"  # https://example.com -> https://example.com/
    return url, None


def _push_to_front(cur, url):
    """Queue url ahead of everything else. Returns a short status string."""
    cur.execute("SELECT claimed_at IS NOT NULL FROM url_queue WHERE url = %s;", (url,))
    row = cur.fetchone()
    if row and row[0]:
        return "already being scraped"
    cur.execute("SELECT LEAST(COALESCE(MIN(id), 0), 0) - 1 FROM url_queue;")
    front_id = cur.fetchone()[0]
    if row:
        cur.execute("UPDATE url_queue SET id = %s, enqueued_at = now() WHERE url = %s AND claimed_at IS NULL;",
                    (front_id, url))
        return "moved to the front" if cur.rowcount else "already being scraped"
    cur.execute("INSERT INTO url_queue (id, url) VALUES (%s, %s);", (front_id, url))
    return "queued at the front"


@app.route("/queue/push", methods=["POST"])
def queue_push():
    """Put URLs at the front of the crawl queue. Body: {"urls": "one per line"} or {"urls": [...]}."""
    data = request.get_json(silent=True) or {}
    raw = data.get("urls", "")
    items = raw.splitlines() if isinstance(raw, str) else [str(u) for u in raw]
    items = [i for i in (x.strip() for x in items) if i]
    if not items:
        return jsonify({"error": "no URLs given"}), 400
    if len(items) > MAX_URLS_PER_REQUEST:
        return jsonify({"error": f"at most {MAX_URLS_PER_REQUEST} URLs at a time"}), 400

    results = []
    conn = get_db()
    try:
        cur = conn.cursor()
        # Push in reverse so the first URL typed ends up first in the queue.
        for raw_url in reversed(items):
            url, problem = _normalize_url(raw_url)
            if problem:
                results.append({"input": raw_url, "status": problem, "ok": False})
                continue
            for attempt in range(3):  # two people pushing at once can pick the same front id
                try:
                    status = _push_to_front(cur, url)
                    conn.commit()
                    break
                except psycopg2.errors.UniqueViolation:
                    conn.rollback()
            else:
                status = "busy, try again"
            cur.execute("SELECT 1 FROM urls WHERE url = %s;", (url,))
            indexed = cur.fetchone() is not None
            results.append({"input": raw_url, "url": url, "status": status, "ok": status != "busy, try again",
                            "already_indexed": indexed})
    finally:
        conn.close()
    results.reverse()
    return jsonify({"results": results})


# The queue count can take over a second on the real database (it falls back to an estimate
# after statement_timeout) and every open dashboard polls this endpoint, so the node list and
# queue status are served stale-while-revalidate: a poll never waits on them once they have been
# loaded once, and N open dashboards cost one refresh per interval instead of N.
LIVE_NODES_TTL_SECONDS = float(os.getenv("DASHBOARD_NODES_TTL_SECONDS", "2"))
LIVE_QUEUE_TTL_SECONDS = float(os.getenv("DASHBOARD_QUEUE_TTL_SECONDS", "3"))
_live_cache = {}
_live_cache_lock = threading.Lock()


def _swr(name, ttl, fn):
    """fn() -> value (it must catch its own errors). Returns the cached value while it is younger
    than ttl; a stale value is returned at once while one background thread refreshes it; with
    nothing cached yet it is computed inline."""
    with _live_cache_lock:
        entry = _live_cache.setdefault(name, {"has": False, "at": 0.0, "value": None, "refreshing": False})
        if entry["has"] and time.time() - entry["at"] < ttl:
            return entry["value"]
        if entry["has"]:
            if not entry["refreshing"]:
                entry["refreshing"] = True
                threading.Thread(target=_swr_refresh, args=(entry, fn), daemon=True).start()
            return entry["value"]
    value = fn()
    with _live_cache_lock:
        entry.update(has=True, at=time.time(), value=value)
    return value


def _swr_refresh(entry, fn):
    try:
        value = fn()
        with _live_cache_lock:
            entry.update(at=time.time(), value=value)
    except Exception:
        log.exception("dashboard refresh failed")
    finally:
        with _live_cache_lock:
            entry["refreshing"] = False


def _nodes_state():
    try:
        return _crawler_nodes(), None
    except psycopg2.Error as e:
        return [], str(e)


def _queue_state():
    try:
        return _queue_status(), None
    except psycopg2.Error as e:
        return None, str(e)


@app.route("/dashboard/live")
def dashboard_live():
    """Cheap, frequently polled dashboard state."""
    nodes, nodes_error = _swr("nodes", LIVE_NODES_TTL_SECONDS, _nodes_state)
    queue, queue_error = _swr("queue", LIVE_QUEUE_TTL_SECONDS, _queue_state)
    payload = {
        "nodes": nodes,
        "nodes_error": nodes_error,
        "node_stale_seconds": NODE_STALE_SECONDS,
        "queue": queue,
        "queue_error": queue_error,
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

def _static_page(template):
    """A page that only changes on deploy: short browser cache plus an ETag, so a reload or
    revisit is answered with a 304 (or nothing at all while it is fresh)."""
    resp = app.make_response(render_template(template))
    resp.headers["Cache-Control"] = "public, max-age=60, stale-while-revalidate=600"
    resp.add_etag()
    return resp.make_conditional(request)


@app.route("/creators")
def creators():
    return _static_page("creators.html")

@app.route("/")
def index():
    return _static_page("index.html")


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


# ---- Search execution: result cache, de-duplication, concurrency limit ------------
# search.py has its own cache, pool and statistics; this layer sits in front of it and adds
#  * a short-lived cache of finished result pages (a hit costs no search call and no thread);
#  * coalescing: identical searches that arrive while one is running wait for that one instead of
#    each hitting Postgres (a popular query that just expired is otherwise run N times at once);
#  * a cap on searches running at once (default: the size of search.py's connection pool), so a
#    burst queues here instead of opening extra Postgres connections ("too many clients") --
#    waiting longer than SEARCH_QUEUE_TIMEOUT_SECONDS fails fast as a 503;
#  * stale-if-error: if Postgres is down or the search fails, a result page from up to
#    SEARCH_STALE_SECONDS ago is served instead of an error page.
RESULT_CACHE_SECONDS = float(os.getenv("SEARCH_RESULT_CACHE_SECONDS", "60"))
RESULT_STALE_SECONDS = float(os.getenv("SEARCH_STALE_SECONDS", "3600"))
RESULT_CACHE_SIZE = int(os.getenv("SEARCH_RESULT_CACHE_SIZE", "256"))
SEARCH_MAX_CONCURRENT = int(os.getenv("SEARCH_MAX_CONCURRENT", str(getattr(search, "POOL_MAX", 10))))
SEARCH_QUEUE_TIMEOUT_SECONDS = float(os.getenv("SEARCH_QUEUE_TIMEOUT_SECONDS", "10"))

_result_cache = collections.OrderedDict()   # (query, limit, offset) -> (time, (page, has_more, details))
_result_cache_lock = threading.Lock()
_inflight = {}                              # key -> Future of a running search
_inflight_lock = threading.Lock()
_search_slots = threading.BoundedSemaphore(max(1, SEARCH_MAX_CONCURRENT))


class SearchBusy(Exception):
    """Too many searches running and the queue wait ran out."""


def _result_cache_get(key, max_age):
    with _result_cache_lock:
        item = _result_cache.get(key)
        if item is None or time.time() - item[0] > max_age:
            return None
        _result_cache.move_to_end(key)
        return item[1]


def _result_cache_put(key, value):
    if RESULT_CACHE_SIZE <= 0:
        return
    with _result_cache_lock:
        _result_cache[key] = (time.time(), value)
        _result_cache.move_to_end(key)
        while len(_result_cache) > RESULT_CACHE_SIZE:
            _result_cache.popitem(last=False)


def _search_uncached(query, limit, offset):
    """Asks search.py for one extra row to learn whether a next page exists."""
    if not _search_slots.acquire(timeout=SEARCH_QUEUE_TIMEOUT_SECONDS):
        raise SearchBusy("too many searches in progress")
    try:
        details = list(search.search_debug(query, limit + 1, offset))
    finally:
        _search_slots.release()
    details = details[:limit + 1]
    page = [(d["url"], d["title"], d["score"], d["bm25"], d["pop_boost"], d["reference_count"])
            for d in details[:limit]]
    return page, len(details) > limit, details[:limit]


def _search_coalesced(key):
    with _inflight_lock:
        fut = _inflight.get(key)
        leader = fut is None
        if leader:
            fut = _inflight[key] = Future()
    if not leader:
        return fut.result()
    try:
        value = _search_uncached(*key)
        _result_cache_put(key, value)
        fut.set_result(value)
        return value
    except BaseException as e:
        stale = _result_cache_get(key, RESULT_STALE_SECONDS)
        if stale is not None and isinstance(e, Exception):
            log.warning("search failed (%s: %s); serving a cached result page", type(e).__name__, e)
            fut.set_result(stale)
            return stale
        fut.set_exception(e)
        raise
    finally:
        with _inflight_lock:
            _inflight.pop(key, None)


def _is_cached(query, limit, offset):
    return _result_cache_get((query, limit, offset), RESULT_CACHE_SECONDS) is not None


def _run_search(query, limit, offset):
    """Returns (rows, has_more, seconds, details). `seconds` is what this request waited."""
    started = time.perf_counter()
    key = (query, limit, offset)
    value = _result_cache_get(key, RESULT_CACHE_SECONDS)
    if value is None:
        value = _search_coalesced(key)
    page, has_more, details = value
    elapsed = time.perf_counter() - started
    with _search_timings_lock:
        _search_timings.append((time.time(), elapsed, len(page)))
    return page, has_more, elapsed, details


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


def _spawn(fn, *args):
    """Run fn(*args) on its own thread; returns a Future."""
    fut = Future()

    def run():
        try:
            fut.set_result(fn(*args))
        except BaseException as e:  # delivered to whoever calls fut.result()
            fut.set_exception(e)

    threading.Thread(target=run, name="search-req", daemon=True).start()
    return fut


# If the search has not finished after this long, /search sends the page header (and starts the
# font download) while it keeps running, instead of sending nothing. Searches that finish sooner
# (every cached query) are answered in one piece, with a proper 503 on failure.
STREAM_AFTER_SECONDS = float(os.getenv("SEARCH_STREAM_AFTER_SECONDS", "0.04"))
# Short browser cache for result pages: back/forward, reload and the prefetched "Next" page.
SEARCH_PAGE_CACHE_CONTROL = "private, max-age=30"
API_CACHE_CONTROL = "public, max-age=60, stale-while-revalidate=300"


def _default_limit():
    """Page size for a /search URL without ?limit=. The settings menu keeps the saved value in a
    cookie, so the first request is already the right one (the old fix was a client-side redirect,
    which ran every such search twice)."""
    return int(request.cookies["stultus_limit"]) if request.cookies.get("stultus_limit") in ("20", "30") \
        else DEFAULT_LIMIT


def _results_context(query, limit, offset, fut):
    """Template variables for the results half of the page; waits for the search if needed."""
    def page_url(off):
        args = {"q": query, "limit": limit}
        if off:
            args["offset"] = off
        return "/search?" + urlencode(args)

    error = None
    try:
        results, has_more, elapsed, details = fut.result()
    except Exception:
        log.exception("search failed")
        results, has_more, elapsed, details, error = [], False, 0.0, [], "Search is temporarily unavailable."
    prev_url = page_url(max(0, offset - limit)) if offset > 0 else None
    next_url = page_url(offset + limit) if has_more and offset + limit <= MAX_OFFSET else None
    return dict(search_results=results, search_details=details, query_string=query, error=error,
                elapsed_ms=round(elapsed * 1000, 1), offset=offset, limit=limit,
                prev_url=prev_url, next_url=next_url)


@app.route("/search")
def perform_search():
    query = _clean_query(request.args.get("q"))
    if not query:
        return redirect("/", 302)
    limit = _int_arg("limit", _default_limit(), 1, MAX_LIMIT)
    offset = _int_arg("offset", 0, 0, MAX_OFFSET)

    started = time.perf_counter()
    if _is_cached(query, limit, offset):
        # a hit costs microseconds: no thread, answered in one piece
        fut = Future()
        try:
            fut.set_result(_run_search(query, limit, offset))
        except Exception as e:
            fut.set_exception(e)
    else:
        fut = _spawn(_run_search, query, limit, offset)
        # A browser prefetch (the "Next" link hover) has nobody looking at the header, so wait for
        # the whole page: it can then carry a real status code and be cached.
        purpose = request.headers.get("Sec-Purpose", "") + request.headers.get("Purpose", "")
        wait_futures([fut], timeout=None if "prefetch" in purpose else STREAM_AFTER_SECONDS)

    if fut.done():
        ctx = _results_context(query, limit, offset, fut)
        resp = app.make_response(render_template("search.html", **ctx))
        if ctx["error"]:
            resp.status_code = 503
        else:
            resp.headers["Cache-Control"] = SEARCH_PAGE_CACHE_CONTROL
            resp.headers["Server-Timing"] = f"app;dur={1000 * (time.perf_counter() - started):.1f}"
    else:
        # Slow search: flush the <head> + search box now. The browser parses the inline CSS and
        # starts the font while the database works, then the results arrive as the second chunk.
        # (A failure here can only be reported in the page text, not as a 503: the 200 is sent.)
        head = render_template("_search_head.html", query_string=query)

        def page():
            yield head.encode("utf-8")
            ctx = _results_context(query, limit, offset, fut)
            yield render_template("_search_results.html", **ctx).encode("utf-8")

        resp = app.response_class(stream_with_context(page()), mimetype="text/html")
        g.stream_gzip = True   # compress_response() compresses it chunk by chunk
        # The 200 is already on its way, so if the search then fails the page says so without a
        # 503: that page must not be reused by the browser cache (a retry would show the error
        # again). no-cache still lets back/forward show it; the result cache above makes repeats instant.
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["X-Accel-Buffering"] = "no"   # nginx: don't hold the first chunk back
    # the page size can come from the stultus_limit cookie
    resp.headers.add("Vary", "Cookie")
    return resp


@app.route("/api/search")
def api_search():
    query = _clean_query(request.args.get("q"))
    if not query:
        return jsonify({"error": "missing q"}), 400
    limit = _int_arg("limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
    offset = _int_arg("offset", 0, 0, MAX_OFFSET)
    try:
        results, has_more, elapsed, details = _run_search(query, limit, offset)
    except Exception:
        log.exception("api search failed")
        return jsonify({"error": "search unavailable"}), 503
    payload = {
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
    }
    if request.args.get("debug", "").lower() in ("1", "true", "yes"):
        payload["debug"] = [
            {k: v for k, v in d.items() if k not in ("query_words",)} for d in details
        ]
    resp = jsonify(payload)
    resp.headers["Cache-Control"] = API_CACHE_CONTROL
    return resp


# ---- Static assets: cache-busting links, inlined CSS + gzip --------------------

@app.template_global("asset")
def asset(path):
    """/static/<path>?v=<mtime>: lets /static be cached for a year without serving stale files."""
    try:
        version = int(os.path.getmtime(os.path.join(app.static_folder, path)))
    except OSError:
        version = 0
    return f"/static/{path}?v={version}"


_inline_css_cache = {}   # path -> (mtime, minified text)


def _minify_css(text):
    """Drop comments and indentation only (no value or selector is touched)."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


@app.template_global("inline_css")
def inline_css(*paths):
    """The given static CSS files, concatenated for a <style> block. Inlining the (few KB) of CSS
    removes the render-blocking stylesheet requests from the critical path; the text is re-read
    only when a file's mtime changes."""
    parts = []
    for path in paths:
        full = os.path.join(app.static_folder, path)
        try:
            mtime = os.path.getmtime(full)
            hit = _inline_css_cache.get(full)
            if hit is None or hit[0] != mtime:
                with open(full, encoding="utf-8") as f:
                    hit = _inline_css_cache[full] = (mtime, _minify_css(f.read()))
            parts.append(hit[1])
        except OSError:
            log.warning("inline_css: cannot read %s", full)
    return Markup("\n".join(parts))


_GZIP_TYPES = ("text/", "application/json", "application/javascript", "image/svg+xml")
_GZIP_MIN_BYTES = 500
_gzip_static_cache = {}   # (path, etag) -> compressed bytes; static files are small and few


@app.after_request
def compress_response(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    path = request.path
    # Cache headers. Versioned static URLs (?v=<mtime>) and the font files never change under
    # the same URL, so the browser need not even revalidate them.
    if path.startswith("/static/") and resp.status_code == 200 and ("v" in request.args or path.startswith("/static/fonts/")):
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif resp.status_code >= 500:
        resp.headers["Cache-Control"] = "no-store"
    try:
        compressible = resp.mimetype.startswith(_GZIP_TYPES)
        if compressible:
            resp.headers.add("Vary", "Accept-Encoding")
        if (resp.status_code != 200 or "Content-Encoding" in resp.headers
                or "gzip" not in request.headers.get("Accept-Encoding", "")
                or not compressible):
            return resp
        if g.get("stream_gzip"):
            # The streamed search page: compress each chunk and sync-flush, so the first chunk
            # (the page header) leaves the server immediately instead of waiting for the results.
            co = zlib.compressobj(5, zlib.DEFLATED, 31)
            inner = resp.response

            def gz_chunks():
                for chunk in inner:
                    out = co.compress(chunk) + co.flush(zlib.Z_SYNC_FLUSH)
                    if out:
                        yield out
                yield co.flush()

            resp.response = ClosingIterator(gz_chunks(), [inner.close] if hasattr(inner, "close") else [])
            resp.headers["Content-Encoding"] = "gzip"
            resp.headers.pop("Content-Length", None)
            return resp
        is_static = path.startswith("/static/")
        etag = resp.headers.get("ETag")
        cache_key = (path, etag) if is_static and etag else None
        if cache_key and cache_key in _gzip_static_cache:
            data = _gzip_static_cache[cache_key]
            resp.direct_passthrough = False
        else:
            resp.direct_passthrough = False
            raw = resp.get_data()
            if len(raw) < _GZIP_MIN_BYTES:
                return resp
            data = gzip.compress(raw, compresslevel=9 if cache_key else 5)
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
