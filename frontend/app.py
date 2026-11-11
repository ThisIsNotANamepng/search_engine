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

import os, sys, time, threading

# search.py lives in the directory above this one
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, render_template, request, jsonify
import search
import psycopg2
import psycopg2.extras
import json

app = Flask(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is not set")

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
    if desired < 0:
        return jsonify({"error": "desired must be >= 0"}), 400

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
    """Page shell only: no slow queries, so it renders immediately. Stats arrive via /dashboard/data."""
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Crawler nodes (from the node agents' heartbeats). `age_s` lets the
    # template flag a node whose agent has gone silent.
    cur.execute("""
        SELECT
            node_id,
            hostname,
            desired_count,
            alive_count,
            cpu_percent,
            mem_percent,
            last_seen,
            EXTRACT(EPOCH FROM (now() - last_seen)) AS age_s
        FROM crawler_nodes
        ORDER BY node_id;
    """)
    nodes = cur.fetchall()

    cur.close()
    conn.close()

    return render_template(
        "dashboard.html",
        nodes=nodes,
        node_stale_seconds=NODE_STALE_SECONDS,
    )


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

@app.route("/search")
def perform_search():
    query_string = request.args.get("q")
    search_results = search.search(query_string or "")  # a missing/empty q used to crash the tokenizer (500)
    return render_template("search.html", search_results=search_results, query_string=query_string)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
