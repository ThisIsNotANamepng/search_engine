"""
One-off migration: widen url_queue.id (and its sequence) from INT to BIGINT.

Every url ever queued takes a new id and ids are never reused, so the INT
sequence runs out at 2,147,483,647. After that every enqueue fails with
"reached maximum value of sequence" and the crawl stops finding new pages.
See how close you are:
    SELECT last_value FROM url_queue_id_seq;

Changing the column type rewrites the whole table and its indexes while holding
a lock that blocks every scraper's queue reads and writes, so scale every node
to 0 crawlers on the dashboard first, run this, then scale back up.

Safe to re-run: does nothing once the column is BIGINT.

Usage:
    DATABASE_URL=... python3 widen_url_queue_id.py
"""
import os
import sys
import time

import psycopg2
from psycopg2 import errors

conn = psycopg2.connect(os.environ["DATABASE_URL"])
conn.autocommit = True
cur = conn.cursor()
cur.execute("SELECT data_type FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'url_queue' AND column_name = 'id'")
if cur.fetchone()[0] == "bigint":
    print("url_queue.id is already bigint, nothing to do")
else:
    # Don't queue behind a running crawler and block everything else; fail fast so they can be stopped first
    cur.execute("SET lock_timeout = '5s'")
    cur.execute("SET statement_timeout = 0")
    start = time.time()
    print("widening url_queue.id to bigint...", flush=True)
    try:
        cur.execute("BEGIN")
        cur.execute("ALTER TABLE url_queue ALTER COLUMN id TYPE bigint")
        cur.execute("ALTER SEQUENCE url_queue_id_seq AS bigint")
        cur.execute("COMMIT")
    except errors.LockNotAvailable:
        sys.exit("url_queue is in use: scale every node to 0 crawlers on the dashboard, then run this again")
    print(f"done in {time.time() - start:.0f}s")
conn.close()
