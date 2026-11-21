"""
This script builds blocklist.db, the sqlite database of domains the scrapers must not scrape, for them to download and check locally on every url

It holds two lists:
  - Steven Black's hosts file of unpalatable domains (fake news/gambling scams/adware/malware sites)
  - The postgres blocked_domains table (opt-outs), so scrapers don't query postgres for it on every url

Needs DATABASE_URL set. If the hosts download or postgres fails, the script crashes and leaves the old blocklist.db in place, so opt-outs are never dropped

Run every 10 minutes or so
"""

import os, requests, sqlite3, time, psycopg2

HOSTS_URL = "https://raw.githubusercontent.com/StevenBlack/hosts/master/alternates/fakenews/hosts"
TMP_PATH = "blocklist.db.tmp"

response = requests.get(HOSTS_URL, timeout=60)
response.raise_for_status()

# Hosts file lines are "<ip> <domain>", with # comments (whole-line, indented or after the entry)
hosts_domains = []
for line in response.text.splitlines():
    parts = line.split("#", 1)[0].split()
    if len(parts) >= 2:
        hosts_domains.append(parts[1])

# Built in a temp file and swapped in at the end, so serve_blocklist.py never serves a half-written database
with open(TMP_PATH, "w") as f:
    f.write("")

conn = sqlite3.connect(TMP_PATH)
cursor = conn.cursor()
# PRIMARY KEY: scrapers look up every url, so this needs an index
cursor.execute("CREATE TABLE blocklist (domain TEXT PRIMARY KEY);")
cursor.execute("CREATE TABLE last_updated (last_updated TEXT);")
cursor.executemany("INSERT OR IGNORE INTO blocklist VALUES (?)", [(d,) for d in hosts_domains])

pg = psycopg2.connect(os.environ["DATABASE_URL"])
pg_cursor = pg.cursor()
pg_cursor.execute("SELECT domain FROM blocked_domains;")
cursor.executemany("INSERT OR IGNORE INTO blocklist VALUES (?)", pg_cursor)
pg.close()

# Scrapers now judge staleness by the file's age; kept for scrapers on older images, which read this row
cursor.execute("INSERT INTO last_updated VALUES (?)", (time.time(),))
conn.commit()
conn.close()
os.replace(TMP_PATH, "blocklist.db")
