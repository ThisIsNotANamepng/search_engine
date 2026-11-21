"""
The blocklist (blocklist.db, sqlite, same schema as supporting_servers/update_local_blocklist.py) held in memory as a set,
so checking a host costs nothing instead of an sqlite connect per url.

    python -m crawl_lib.blocklist build [path]    # build a local blocklist.db from StevenBlack's hosts file (no postgres needed)
"""

import os
import sqlite3
import sys
import time

from crawl_lib.urls import host_suffixes

# The production list is the "fakenews" alternate; this is the plain StevenBlack list, for local use without the blocklist server
HOSTS_URL = "https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts"
_NOT_DOMAINS = {"localhost", "localhost.localdomain", "broadcasthost", "local", "ip6-localhost", "ip6-loopback", "0.0.0.0"}


class Blocklist:
    def __init__(self, path="blocklist.db"):
        self.path = path
        self.domains = frozenset()
        self.mtime = None
        self.reload()

    def reload(self):
        """Re-read the file if it changed. Returns True if a (new) list was loaded"""
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return False
        if mtime == self.mtime:
            return False
        conn = sqlite3.connect(self.path)
        try:
            self.domains = frozenset(r[0].lower() for r in conn.execute("SELECT domain FROM blocklist"))
        finally:
            conn.close()
        self.mtime = mtime
        return True

    def is_blocked(self, host):
        """Exact host (hosts-file subdomain entries) or any parent domain (opt-outs: example.com covers www.example.com)"""
        if not self.domains:
            return False
        return any(s in self.domains for s in host_suffixes(host))

    def __len__(self):
        return len(self.domains)


def refresh_from_server(path, database_url, max_age=3600, timeout=5):
    """
    Same as utils._refresh_blocklist_if_stale (download http://<dbhost>:8002/blocklist.db when the local copy is older than an hour)
    but with a short timeout, so an unreachable blocklist server costs seconds, not 30 s. Raises on failure
    """
    import requests
    from urllib.parse import urlparse
    try:
        if time.time() - os.path.getmtime(path) < max_age:
            return False
    except OSError:
        pass
    host = urlparse(database_url or "").hostname
    if not host:
        raise RuntimeError("no host in DATABASE_URL")
    tmp = path + ".tmp"
    with requests.get(f"http://{host}:8002/blocklist.db", stream=True, timeout=timeout) as resp:
        resp.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(65536):
                f.write(chunk)
    os.replace(tmp, path)
    return True


def build_from_hosts(path="blocklist.db", hosts_url=HOSTS_URL):
    import requests
    resp = requests.get(hosts_url, timeout=60)
    resp.raise_for_status()
    domains = []
    for line in resp.text.splitlines():
        parts = line.split("#", 1)[0].split()
        if len(parts) >= 2 and parts[1] not in _NOT_DOMAINS:
            domains.append(parts[1])
    tmp = path + ".tmp"
    open(tmp, "w").close()
    conn = sqlite3.connect(tmp)
    conn.execute("CREATE TABLE blocklist (domain TEXT PRIMARY KEY);")
    conn.execute("CREATE TABLE last_updated (last_updated TEXT);")
    conn.executemany("INSERT OR IGNORE INTO blocklist VALUES (?)", [(d,) for d in domains])
    conn.execute("INSERT INTO last_updated VALUES (?)", (time.time(),))
    conn.commit()
    conn.close()
    os.replace(tmp, path)
    return len(domains)


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "build":
        out = sys.argv[2] if len(sys.argv) > 2 else "blocklist.db"
        print(f"wrote {build_from_hosts(out)} domains to {out}")
    else:
        print(__doc__)
