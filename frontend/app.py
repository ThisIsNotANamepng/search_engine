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

import os, sys, time

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

@app.route("/dashboard")
def dashboard():
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Scraped vs Blocked vs Error counts, uses logs_message_pattern_idx
    cur.execute("""
        SELECT
            CASE
                WHEN message LIKE 'Sc%' THEN 'scraped'
                WHEN message LIKE 'Bl%' THEN 'blocked'
                WHEN message LIKE 'Er%' THEN 'error'
            END AS status,
            COUNT(*) AS count
        FROM logs
        WHERE message LIKE 'Sc%' OR message LIKE 'Bl%' OR message LIKE 'Er%'
        GROUP BY status;
    """)
    status_counts = cur.fetchall()

    # Total unique domains
    cur.execute("""
        SELECT COUNT(DISTINCT SUBSTRING(url FROM '^(?:https?://)?([^/]+)'))
            AS unique_domains
        FROM urls;
    """)
    unique_domains = cur.fetchone()['unique_domains']

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

    return render_template(
        "dashboard.html",
        status_counts=json.dumps(status_counts, default=str),
        scrapes_over_time=json.dumps(scrapes_over_time, default=str),
        scrapers=scrapers,
        url_count=url_count,
        db_size=db_size,
        unique_domains=unique_domains,
        scrapes_per_minute=scrapes_per_minute,
        last_10_scraped=real_last_10_scraped,
        cumulative_scrapes_per_day=json.dumps(cumulative_scrapes_per_day, default=str),
    )

@app.route("/creators")
def creators():
    return render_template("creators.html")

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/search")
def perform_search():
    query_string = request.args.get("q")
    search_results = search.search(query_string)
    return render_template("search.html", search_results=search_results, query_string=query_string)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
