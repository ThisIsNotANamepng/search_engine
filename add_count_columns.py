"""
One-off migration: add the token count columns to an existing database.

    word_urls.count, bigram_urls.count, trigram_urls.count, prefix_urls.count
        INT NOT NULL DEFAULT 1 -- how many times the token appears on the page.
        Rows stored before this migration get 1, since we only know the token
        appeared at least once.
    urls.total_tokens
        INT, NULL until the page is scraped again with the new scraper.

On Postgres 11+ adding a column with a constant default only changes the table's
metadata, so this takes seconds even on the billion-row *_urls tables (no rewrite).
Each ALTER still needs a brief exclusive lock, so it waits at most a few seconds
for running crawler inserts and retries instead of queueing behind them and
blocking everything else.

Run this BEFORE deploying the new scraper: the new scraper inserts into these
columns and will fail on a database that doesn't have them.

Safe to re-run: columns that already exist are skipped.

Usage:
    DATABASE_URL=... python3 add_count_columns.py
"""
import os
import sys
import time

import psycopg2
from psycopg2 import errors

COLUMNS = [
    ("word_urls", "count", "INT NOT NULL DEFAULT 1"),
    ("bigram_urls", "count", "INT NOT NULL DEFAULT 1"),
    ("trigram_urls", "count", "INT NOT NULL DEFAULT 1"),
    ("prefix_urls", "count", "INT NOT NULL DEFAULT 1"),
    ("urls", "total_tokens", "INT"),
]

LOCK_TIMEOUT = "5s"
ATTEMPTS = 20


def column_exists(cur, table, column):
    cur.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s AND column_name = %s",
        (table, column))
    return cur.fetchone() is not None


def main():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = True
    cur = conn.cursor()

    cur.execute("SHOW server_version_num")
    if int(cur.fetchone()[0]) < 110000:
        # Before Postgres 11, ADD COLUMN ... DEFAULT rewrites every row of the table
        sys.exit("Postgres 11 or newer is required: older versions would rewrite every row of the *_urls tables.")

    cur.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")

    for table, column, definition in COLUMNS:
        if column_exists(cur, table, column):
            print(f"{table}.{column}: already exists, skipping", flush=True)
            continue
        for attempt in range(1, ATTEMPTS + 1):
            try:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}")
                print(f"{table}.{column}: added", flush=True)
                break
            except errors.LockNotAvailable:
                print(f"{table}.{column}: table busy, retrying ({attempt}/{ATTEMPTS})", flush=True)
                time.sleep(2)
        else:
            sys.exit(f"{table}.{column}: couldn't get a lock after {ATTEMPTS} tries. "
                     "Pause the crawlers and run this again.")

    conn.close()


if __name__ == "__main__":
    main()
