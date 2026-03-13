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

from flask import Flask, render_template
import psycopg2
import psycopg2.extras
import json
import os, time

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

    start=time.time()

    # Scraped vs Blocked counts
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

    print(1, time.time()-start)
    start=time.time()

    # Total unique domains
    cur.execute("""
        SELECT COUNT(DISTINCT 
            CASE 
                WHEN url LIKE 'http://%' THEN 
                    SUBSTRING(url FROM '^http://([^/]+)')
                WHEN url LIKE 'https://%' THEN 
                    SUBSTRING(url FROM '^https://([^/]+)')
                ELSE 
                    SUBSTRING(url FROM '^([^/]+)')
            END
        ) as unique_domains
        FROM urls;
    """)
    unique_domains = cur.fetchone()['unique_domains']

    print(2, time.time()-start)
    start=time.time()

    # Scrapes per minute
    cur.execute("""
        SELECT COUNT(*) 
        FROM logs 
        WHERE ts >= now() - INTERVAL '1 minute'
        AND message LIKE 'Sc%';
    """)
    scrapes_per_minute = cur.fetchone()['count']

    print(3, time.time()-start)
    start=time.time()

    # Scrapes per minute over time
    cur.execute("""
        SELECT date_trunc('day', ts) AS minute, COUNT(*) AS count
        FROM logs
        WHERE message LIKE 'Sc%'
        GROUP BY minute
        ORDER BY minute;
    """)
    scrapes_over_time = cur.fetchall()

    print(4, time.time()-start)
    start=time.time()

    # Cummalative scrapes per day
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

    print(5, time.time()-start)
    start=time.time()

    # Last 8 urls
    cur.execute("""
        SELECT *
        FROM logs
        WHERE message LIKE 'Scraped%'
        ORDER BY id DESC
        LIMIT 8;
        
    """)
    last_10_scraped = cur.fetchall()
    real_last_10_scraped = []
    for i in last_10_scraped:
        real_last_10_scraped.append((
            i['message'][i['message'].index(" "):],
            i['ts']
        ))

    print(6, time.time()-start)
    start=time.time()

    # Active scrapers (last three minutes)
    cur.execute("""
        SELECT
            ip,
            COUNT(*) AS urls_scraped,
            MIN(ts) AS started_at,
            MAX(ts) AS last_seen
        FROM logs
        WHERE message LIKE 'Sc%'
        GROUP BY ip
        HAVING MAX(ts) >= NOW() - INTERVAL '3 minutes';
    """)
    scrapers = cur.fetchall()

    print(7, time.time()-start)
    start=time.time()

    # URLs in database
    cur.execute("SELECT COUNT(*) AS count FROM urls;")
    url_count = cur.fetchone()["count"]


    print(8, time.time()-start)
    start=time.time()

    # Postgres DB size
    cur.execute("""
        SELECT pg_size_pretty(pg_database_size(current_database())) AS size;
    """)
    db_size = cur.fetchone()["size"]

    print(9, time.time()-start)
    start=time.time()

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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
