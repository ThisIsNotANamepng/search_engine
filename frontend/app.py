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

import os, sys, time, threading, socket, collections
import urllib.request, urllib.error
import urllib.parse
from urllib.parse import urlsplit
from concurrent.futures import ThreadPoolExecutor

# search.py lives in the directory above this one
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, render_template, request, jsonify
import search
import psycopg2
import psycopg2.extras
import psycopg2.errors
import json

app = Flask(__name__)

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

def init_control_schema():
    """
    Create the crawler_nodes table used by the node agents' heartbeats. This is
    the whole 'control plane' state that replaced Kubernetes: one row per box,
    holding the desired crawler count (set from the dashboard) and the latest
    reported liveness/host stats.
    """
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

# Runs at import so it's created under gunicorn too. DATABASE_URL is required
# above, so a failure here is a real, fail-fast configuration problem.
init_control_schema()

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
    conn = psycopg2.connect(DATABASE_URL, sslmode="disable", connect_timeout=max(1, int(HEALTH_TIMEOUT_SECONDS)))  # libpq wants a whole number
    try:
        conn.cursor().execute("SELECT 1;")
    finally:
        conn.close()
    return None


# The proxy check fetches a real page through the whole chain (db01:8888 -> upstream proxy -> site),
# because a proxy that's listening but can't reach its upstream stops every crawler. It needs the
# shared proxy password (the crawlers' SCRAPER_PROXY_PASSWORD) in the head's env file.
PROXY_CHECK_PASSWORD = os.getenv("SCRAPER_PROXY_PASSWORD") or os.getenv("PROXY_PASSWORD") or ""
PROXY_CHECK_URL = os.getenv("DASHBOARD_PROXY_CHECK_URL", "https://example.com/")
PROXY_CHECK_TIMEOUT_SECONDS = 8.0
PROXY_CHECK_CACHE_SECONDS = 30.0  # don't send the test site a request every poll
_proxy_check_cache = {"at": 0.0, "result": None}
_proxy_check_lock = threading.Lock()


def _check_proxy_chain():
    if not PROXY_CHECK_PASSWORD:
        _check_tcp(8888)
        return "warn", "listening; set SCRAPER_PROXY_PASSWORD in /etc/stultus/head.env to test fetching through it"
    with _proxy_check_lock:
        if _proxy_check_cache["result"] is not None and time.time() - _proxy_check_cache["at"] < PROXY_CHECK_CACHE_SECONDS:
            outcome = _proxy_check_cache["result"]
        else:
            outcome = _fetch_through_proxy()
            _proxy_check_cache.update(at=time.time(), result=outcome)
    if isinstance(outcome, Exception):
        raise outcome
    return outcome


def _fetch_through_proxy():
    """None if a page came back through the chain, else ("down", reason) or an exception."""
    query = urllib.parse.urlencode({"url": PROXY_CHECK_URL})
    req = urllib.request.Request(f"http://{HEALTH_HOST}:8888/fetch?{query}",
                                 headers={"X-Proxy-Auth": PROXY_CHECK_PASSWORD})
    try:
        with urllib.request.urlopen(req, timeout=PROXY_CHECK_TIMEOUT_SECONDS):
            return None
    except urllib.error.HTTPError as e:
        reason = (e.read(200).decode("utf-8", "replace").strip().splitlines() or [""])[0]
        if e.code == 401:
            reason = "password rejected (head.env's SCRAPER_PROXY_PASSWORD doesn't match the proxy's)"
        return "down", f"can't fetch pages: HTTP {e.code} {reason}".strip()
    except Exception as e:  # timeouts, refused connections
        return e


HEALTH_CHECKS = [
    # (name, what the crawlers use it for, check)
    ("Postgres", "index + URL queue", _check_postgres),
    # Redis answers PING with +PONG, or -NOAUTH if it wants a password (still up).
    ("Redis", "domain politeness locks", lambda: _check_tcp(6379, b"PING\r\n", (b"+PONG", b"-NOAUTH"))),
    ("Proxy", "all page fetches", _check_proxy_chain),
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


# ---- Who is actually scraping -------------------------------------------------
# Scrapers log each stored page as "Scraped <url>" with their host's IP. Matching those IPs
# to the nodes' hostnames (db01 resolves in01..in12) gives each node its own pages/minute,
# so a node whose crawlers run but store nothing stands out. Hosts that aren't nodes
# (laptops, a Pi, "unknown") are listed separately.
SCRAPE_WINDOW_MINUTES = 10
NOT_SCRAPING_AFTER_SECONDS = 300   # a node with crawlers up but no stored page for this long is flagged
SCRAPE_RATES_CACHE_SECONDS = 15
_scrape_rates_cache = {"at": 0.0, "rows": None}
_host_ip_cache = {}                # hostname -> (resolved_at, set of IPs)
HOST_IP_CACHE_SECONDS = 600
_rates_lock = threading.Lock()


def _host_ips(hostname):
    now = time.time()
    cached = _host_ip_cache.get(hostname)
    if cached and now - cached[0] < HOST_IP_CACHE_SECONDS:
        return cached[1]
    try:
        ips = set(socket.gethostbyname_ex(hostname)[2])
    except OSError:
        ips = set()
    _host_ip_cache[hostname] = (now, ips)
    return ips


def _scrape_rates():
    """Per-IP counts of pages stored in the last SCRAPE_WINDOW_MINUTES (logs_scraped_ip_ts_idx / _ts_idx)."""
    with _rates_lock:
        if _scrape_rates_cache["rows"] is not None and time.time() - _scrape_rates_cache["at"] < SCRAPE_RATES_CACHE_SECONDS:
            return _scrape_rates_cache["rows"]
        conn = get_db()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("""
                SELECT COALESCE(NULLIF(ip, ''), 'unknown') AS ip,
                       count(*) FILTER (WHERE ts >= now() - INTERVAL '1 minute') AS last_1m,
                       count(*) AS last_window,
                       EXTRACT(EPOCH FROM (now() - max(ts)))::float AS last_age_s
                FROM logs
                WHERE message LIKE 'Scraped%%' AND ts >= now() - make_interval(mins => %s)
                GROUP BY 1;
            """, (SCRAPE_WINDOW_MINUTES,))
            rows = cur.fetchall()
        finally:
            conn.close()
        _scrape_rates_cache.update(at=time.time(), rows=rows)
        return rows


def _attach_scrape_rates(nodes):
    """Adds pages_per_min / last_page_age_s / scraping to each node; returns the non-node hosts."""
    rows = {r["ip"]: r for r in _scrape_rates()}
    claimed = set()
    for n in nodes:
        ips = _host_ips(n.get("hostname") or n["node_id"]) | _host_ips(n["node_id"])
        mine = [rows[ip] for ip in ips if ip in rows]
        claimed.update(ip for ip in ips if ip in rows)
        n["ips"] = sorted(ips)
        n["pages_last_1m"] = sum(r["last_1m"] for r in mine)
        n["pages_per_min"] = round(sum(r["last_window"] for r in mine) / SCRAPE_WINDOW_MINUTES, 1)
        ages = [r["last_age_s"] for r in mine]
        n["last_page_age_s"] = min(ages) if ages else None
    others = [dict(r, pages_per_min=round(r["last_window"] / SCRAPE_WINDOW_MINUTES, 1))
              for ip, r in rows.items() if ip not in claimed]
    others.sort(key=lambda r: -r["last_window"])
    return others


@app.route("/dashboard/live")
def dashboard_live():
    """Cheap, frequently polled dashboard state."""
    try:
        nodes, nodes_error = _crawler_nodes(), None
    except psycopg2.Error as e:
        nodes, nodes_error = [], str(e)
    try:
        other_scrapers = _attach_scrape_rates(nodes)
    except psycopg2.Error as e:
        other_scrapers, nodes_error = [], nodes_error or str(e)
    try:
        queue, queue_error = _queue_status(), None
    except psycopg2.Error as e:
        queue, queue_error = None, str(e)
    payload = {
        "nodes": nodes,
        "nodes_error": nodes_error,
        "node_stale_seconds": NODE_STALE_SECONDS,
        "not_scraping_after_seconds": NOT_SCRAPING_AFTER_SECONDS,
        "scrape_window_minutes": SCRAPE_WINDOW_MINUTES,
        "other_scrapers": other_scrapers,
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
    }
    # default=str keeps timestamps/decimals rendering exactly as the old server-side template did
    return app.response_class(json.dumps(payload, default=str), mimetype="application/json")

@app.route("/creators")
def creators():
    return render_template("creators.html")

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/search")
def perform_search():
    query_string = request.args.get("q")
    started = time.perf_counter()
    search_results = search.search(query_string or "")  # a missing/empty q used to crash the tokenizer (500)
    if query_string:
        elapsed = time.perf_counter() - started
        count = len(search_results) if hasattr(search_results, "__len__") else None
        with _search_timings_lock:
            _search_timings.append((time.time(), elapsed, count))
    return render_template("search.html", search_results=search_results, query_string=query_string)


if __name__ == "__main__":
    # No debug=True: the Werkzeug debugger lets anyone who reaches it run code
    app.run(host="0.0.0.0", port=5000)
