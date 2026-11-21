"""
One-off migration: add the indexes search.py relies on.

The *_urls tables hold billions of rows and were created without any index, so
every search sequentially scanned all of them. Each index is (token_id, url_id)
so the per-URL token counts can be answered with an index-only scan.

Built with CREATE INDEX CONCURRENTLY, so crawlers keep writing while it runs.
Safe to re-run: existing valid indexes are skipped and leftover INVALID ones
(from an interrupted build) are dropped and rebuilt.

Usage:
    DATABASE_URL=... python3 add_search_indexes.py [table ...]
    (tables: word_urls bigram_urls trigram_urls prefix_urls; default: all)
"""
import os
import sys
import time

import psycopg2

INDEXES = {
    "word_urls":    ("word_urls_word_id_url_id_idx",       "word_id"),
    "prefix_urls":  ("prefix_urls_prefix_id_url_id_idx",   "prefix_id"),
    "bigram_urls":  ("bigram_urls_bigram_id_url_id_idx",   "bigram_id"),
    "trigram_urls": ("trigram_urls_trigram_id_url_id_idx", "trigram_id"),
}


def build(table):
    name, col = INDEXES[table]
    # TCP keepalives: builds run for hours with the connection otherwise idle,
    # and NAT/firewalls drop silent connections ("Connection timed out").
    conn = psycopg2.connect(os.environ["DATABASE_URL"], keepalives=1,
                            keepalives_idle=30, keepalives_interval=10, keepalives_count=6)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SET maintenance_work_mem = '1GB'")
    cur.execute("SET statement_timeout = 0")
    cur.execute(
        "SELECT i.indisvalid FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
        "WHERE c.relname = %s AND c.relnamespace = 'public'::regnamespace", (name,))
    row = cur.fetchone()
    if row and row[0]:
        print(f"{name}: already exists, skipping", flush=True)
        return
    if row:
        print(f"{name}: dropping invalid leftover index", flush=True)
        cur.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
    start = time.time()
    print(f"{name}: building...", flush=True)
    cur.execute(f"CREATE INDEX CONCURRENTLY {name} ON {table} ({col}, url_id)")
    print(f"{name}: done in {time.time() - start:.0f}s", flush=True)
    cur.execute(f"ANALYZE {table}")
    conn.close()


if __name__ == "__main__":
    for t in (sys.argv[1:] or list(INDEXES)):
        build(t)
