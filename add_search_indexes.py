"""
One-off migration: add the indexes search.py relies on.

The *_urls tables hold billions of rows and were created without any index, so
every search sequentially scanned all of them. Each index is
(token_id, url_id) INCLUDE (count), so search can read the url_id AND how many
times the token appears on that page straight from the index (an index-only
scan), without fetching every matching row from the table.

Run add_count_columns.py first: these indexes need the count column.

Older databases have the earlier (token_id, url_id) indexes without count. Once
the new index for a table has finished building, the old one is dropped, since
the new one does everything it did. Both exist while the new one builds, so
make sure there's enough free disk space for one extra index at a time.

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

# table: (new index name, token column, old index name it replaces)
INDEXES = {
    "word_urls":    ("word_urls_word_id_url_id_count_idx",       "word_id",    "word_urls_word_id_url_id_idx"),
    "prefix_urls":  ("prefix_urls_prefix_id_url_id_count_idx",   "prefix_id",  "prefix_urls_prefix_id_url_id_idx"),
    "bigram_urls":  ("bigram_urls_bigram_id_url_id_count_idx",   "bigram_id",  "bigram_urls_bigram_id_url_id_idx"),
    "trigram_urls": ("trigram_urls_trigram_id_url_id_count_idx", "trigram_id", "trigram_urls_trigram_id_url_id_idx"),
}


def index_state(cur, name):
    """None if the index doesn't exist, else whether it's valid (finished building)."""
    cur.execute(
        "SELECT i.indisvalid FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
        "WHERE c.relname = %s AND c.relnamespace = 'public'::regnamespace", (name,))
    row = cur.fetchone()
    return row[0] if row else None


def build(table):
    name, col, old_name = INDEXES[table]
    # TCP keepalives: builds run for hours with the connection otherwise idle,
    # and NAT/firewalls drop silent connections ("Connection timed out").
    conn = psycopg2.connect(os.environ["DATABASE_URL"], keepalives=1,
                            keepalives_idle=30, keepalives_interval=10, keepalives_count=6)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SET maintenance_work_mem = '1GB'")
    cur.execute("SET statement_timeout = 0")
    cur.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s AND column_name = 'count'", (table,))
    if cur.fetchone() is None:
        sys.exit(f"{table} has no count column yet: run add_count_columns.py first.")

    state = index_state(cur, name)
    if state:
        print(f"{name}: already exists, skipping", flush=True)
    else:
        if state is False:
            print(f"{name}: dropping invalid leftover index", flush=True)
            cur.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
        start = time.time()
        print(f"{name}: building...", flush=True)
        cur.execute(f"CREATE INDEX CONCURRENTLY {name} ON {table} ({col}, url_id) INCLUDE (count)")
        print(f"{name}: done in {time.time() - start:.0f}s", flush=True)
        cur.execute(f"ANALYZE {table}")

    # The new index covers everything the old (token_id, url_id) one did
    if index_state(cur, name) and index_state(cur, old_name) is not None:
        print(f"{old_name}: replaced by {name}, dropping", flush=True)
        cur.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {old_name}")
    conn.close()


if __name__ == "__main__":
    for t in (sys.argv[1:] or list(INDEXES)):
        build(t)
