"""
Batched Postgres writes for fast_crawl.py. No schema changes: only the row writes utils.store() & friends already do.

One flush() = one transaction for many pages. It reproduces utils.store() exactly where it matters:
  * same tables / columns, same total_tokens (sum of every stored word, bigram, trigram and prefix count)
  * urls inserted first, then new words/prefixes with "NOT EXISTS ... ORDER BY ... ON CONFLICT DO NOTHING"
    (postgres burns a sequence id for every attempted row, even skipped ones, and unordered inserts deadlock)
  * a re-crawled page keeps its row, gets a new total_tokens and has its old postings deleted
Postings go in with COPY (a plain data write, ~10x faster than INSERT ... VALUES for 5k rows per page).
"""

import io
import os
import random
import socket
import time
from collections import Counter

import psycopg2
import psycopg2.errors
import psycopg2.extras as extras

import utils

POSTING_TABLES = (
    # (table, token column, id column in the posting table)
    ("word_urls", "word_id"), ("bigram_urls", "bigram_id"), ("trigram_urls", "trigram_id"), ("prefix_urls", "prefix_id"),
)


def clean_str(s):
    """Postgres text can't hold NUL bytes"""
    return s.replace("\x00", "") if s and "\x00" in s else s


class Batch:
    """Everything to write in one transaction"""

    def __init__(self):
        self.pages = {}            # url -> dict(title, icon, counts)
        self.seen = set()          # urls recorded without tokens (mark_url_seen)
        self.refs = []             # (url, link) rows for urls_references
        self.ref_counts = Counter()  # link -> +reference_count
        self.enqueue = set()
        self.done = set()          # claims finished: deleted from url_queue
        self.release = set()       # claims handed back
        self.logs = []

    def add_item(self, item):
        for page in item.get("pages", ()):
            self.pages[page["url"]] = page
        self.seen.update(item.get("seen", ()))
        page_url = item.get("refs_from")
        if page_url:
            self.refs.extend((page_url, link) for link in item.get("refs", ()))
        self.ref_counts.update(item.get("external", ()))
        self.enqueue.update(item.get("enqueue", ()))
        self.done.update(item.get("done", ()))
        self.release.update(item.get("release", ()))
        self.logs.extend(item.get("logs", ()))

    def empty(self):
        return not (self.pages or self.seen or self.refs or self.enqueue or self.done or self.release or self.logs or self.ref_counts)


def load_static_maps(conn):
    cur = conn.cursor()
    cur.execute("SELECT bigram, id FROM bigrams;")
    bigrams = {r[0].strip(): r[1] for r in cur.fetchall()}
    cur.execute("SELECT trigram, id FROM trigrams;")
    trigrams = {r[0].strip(): r[1] for r in cur.fetchall()}
    conn.commit()
    cur.close()
    return bigrams, trigrams


def _copy(cur, table, columns, rows):
    if not rows:
        return
    buf = io.StringIO()
    buf.writelines(f"{a}\t{b}\t{c}\n" for a, b, c in rows)
    buf.seek(0)
    cur.copy_expert(f"COPY {table} ({', '.join(columns)}) FROM STDIN", buf)


def flush(conn, maps, batch, ip, claims=True):
    """Write the batch in one transaction (caller commits / rolls back). Returns stats dict"""
    bigram_map, trigram_map = maps
    cur = conn.cursor()
    stats = {"pages": 0, "rows": 0}

    # ---- pages: urls rows (sorted by url so concurrent writers lock in the same order)
    pages = sorted(batch.pages.values(), key=lambda p: p["url"])
    pages = [p for p in pages if len(p["url"]) <= utils.MAX_URL_LENGTH]
    if pages:
        for p in pages:
            w, b, t, pr = p["counts"]
            p["total_tokens"] = (sum(w.values()) + sum(n for k, n in b.items() if k in bigram_map)
                                 + sum(n for k, n in t.items() if k in trigram_map) + sum(pr.values()))
        urls = [clean_str(p["url"]) for p in pages]
        titles = [clean_str(p["title"][:utils.MAX_TITLE_LENGTH]) if p["title"] else None for p in pages]
        icons = [clean_str(p["icon"]) if p["icon"] and len(p["icon"]) <= utils.MAX_URL_LENGTH else None for p in pages]
        totals = [p["total_tokens"] for p in pages]
        cur.execute("""
            INSERT INTO urls (url, title, icon_link, total_tokens)
            SELECT u, t, i, n FROM unnest(%s::varchar[], %s::varchar[], %s::varchar[], %s::int[]) AS x(u, t, i, n)
            WHERE NOT EXISTS (SELECT 1 FROM urls WHERE urls.url = x.u)
            ORDER BY u
            ON CONFLICT (url) DO NOTHING
            RETURNING url, id;""", (urls, titles, icons, totals))
        url_ids = dict(cur.fetchall())
        existing = [u for u in urls if u not in url_ids]
        skip = set()
        if existing:
            # Already stored. A page with total_tokens is fully indexed: leave it alone (re-storing means DELETE ... WHERE url_id =,
            # a full scan here: the postings tables have no url_id index). A seen-only stub (total_tokens NULL) has no postings:
            # fill it in, nothing to delete.
            cur.execute("SELECT url, id, total_tokens FROM urls WHERE url = ANY(%s);", (existing,))
            stubs = []
            for u, i, total in cur.fetchall():
                url_ids[u] = i
                if total is None:
                    stubs.append(u)
                else:
                    skip.add(u)
            if stubs:
                by_url = {u: k for k, u in enumerate(urls)}
                idx = [by_url[u] for u in stubs]
                cur.execute("""
                    UPDATE urls SET total_tokens = x.n, title = coalesce(urls.title, x.t), icon_link = coalesce(urls.icon_link, x.i)
                    FROM unnest(%s::varchar[], %s::int[], %s::varchar[], %s::varchar[]) AS x(u, n, t, i)
                    WHERE urls.url = x.u AND urls.total_tokens IS NULL;""",
                    ([urls[k] for k in idx], [totals[k] for k in idx], [titles[k] for k in idx], [icons[k] for k in idx]))
            stats["skipped_stored"] = len(skip)
            pages = [p for p in pages if clean_str(p["url"]) not in skip]

        # ---- new words / prefixes, one statement each for the whole batch
        all_words = sorted({w for p in pages for w in p["counts"][0]})
        all_prefixes = sorted({x for p in pages for x in p["counts"][3]})
        for column, table, tokens in (("word", "words", all_words), ("prefix", "prefixes", all_prefixes)):
            if tokens:
                cur.execute(f"""
                    INSERT INTO {table} ({column})
                    SELECT t FROM unnest(%s::varchar[]) AS t
                    WHERE NOT EXISTS (SELECT 1 FROM {table} WHERE {column} = t)
                    ORDER BY t
                    ON CONFLICT ({column}) DO NOTHING;""", (tokens,))
        word_map, prefix_map = {}, {}
        for column, table, tokens, target in (("word", "words", all_words, word_map), ("prefix", "prefixes", all_prefixes, prefix_map)):
            for start in range(0, len(tokens), 20000):
                cur.execute(f"SELECT {column}, id FROM {table} WHERE {column} = ANY(%s);", (tokens[start:start + 20000],))
                target.update(cur.fetchall())

        # ---- postings
        rows = {name: [] for name, _ in POSTING_TABLES}
        for p in pages:
            uid = url_ids[clean_str(p["url"])]
            w, b, t, pr = p["counts"]
            rows["word_urls"].extend((word_map[k], uid, n) for k, n in w.items() if k in word_map)
            rows["bigram_urls"].extend((bigram_map[k], uid, n) for k, n in b.items() if k in bigram_map)
            rows["trigram_urls"].extend((trigram_map[k], uid, n) for k, n in t.items() if k in trigram_map)
            rows["prefix_urls"].extend((prefix_map[k], uid, n) for k, n in pr.items() if k in prefix_map)
        for table, id_col in POSTING_TABLES:
            _copy(cur, table, (id_col, "url_id", "count"), rows[table])
            stats["rows"] += len(rows[table])
        stats["pages"] = len(pages)

    # ---- links this batch's pages contain
    if batch.refs:
        refs = sorted({(clean_str(u), clean_str(l)) for u, l in batch.refs if len(u) <= utils.MAX_URL_LENGTH and len(l) <= utils.MAX_URL_LENGTH})
        extras.execute_values(cur, "INSERT INTO urls_references (url, referenced_domain) VALUES %s ON CONFLICT DO NOTHING;", refs, page_size=2000)

    # ---- urls recorded as seen, without tokens
    seen = sorted(clean_str(u) for u in batch.seen if len(u) <= utils.MAX_URL_LENGTH and u not in batch.pages)
    if seen:
        cur.execute("""
            INSERT INTO urls (url) SELECT u FROM unnest(%s::varchar[]) AS u
            WHERE NOT EXISTS (SELECT 1 FROM urls WHERE urls.url = u)
            ORDER BY u ON CONFLICT (url) DO NOTHING;""", (seen,))

    # ---- reference_count += 1 per page linking to an already-stored external url (locked in url order: no deadlocks between crawlers)
    if batch.ref_counts:
        links = list(batch.ref_counts)
        cur.execute("SELECT id, url FROM urls WHERE url = ANY(%s) ORDER BY url FOR UPDATE;", (links,))
        locked = cur.fetchall()
        if locked:
            cur.execute("""
                UPDATE urls SET reference_count = reference_count + x.c FROM unnest(%s::int[], %s::int[]) AS x(id, c)
                WHERE urls.id = x.id;""", ([i for i, _ in locked], [batch.ref_counts[u] for _, u in locked]))

    # ---- new urls to crawl: filter_new_urls + enqueue_urls in one statement
    enqueue = sorted(u for u in batch.enqueue if len(u) <= utils.MAX_URL_LENGTH)
    if enqueue:
        stats["enqueued"] = enqueue_new(cur, enqueue, claims)

    # ---- claims
    if claims:
        if batch.done:
            cur.execute("DELETE FROM url_queue WHERE url = ANY(%s);", (sorted(batch.done),))
        if batch.release:
            cur.execute("UPDATE url_queue SET claimed_at = NULL WHERE url = ANY(%s);", (sorted(batch.release),))
    elif batch.release:
        enqueue_new(cur, sorted(batch.release), claims)  # claimless queue: popped urls are gone, handing back = inserting again
    # (claimless queue: finished urls were removed when they were popped, nothing to delete)

    # ---- logs: one multi-row insert
    if batch.logs:
        extras.execute_values(cur, "INSERT INTO logs (ip, message) VALUES %s;", [(ip, clean_str(m)) for m in batch.logs], page_size=2000)
    cur.close()
    return stats


def enqueue_new(cur, urls, claims=True):
    """
    Queue the urls that are neither stored nor already queued (filter_new_urls + enqueue_urls in one statement).
    The filter is in the SELECT so skipped urls don't burn sequence ids.
    claims=False (no claimed_at column = the production schema): explicit ids, never the exhausted id sequence. Runs in a savepoint and
    retries when a concurrent writer took the same ids
    """
    if not claims:
        urls = [u for u in urls]
        cur.execute("SELECT u FROM unnest(%s::varchar[]) AS u WHERE NOT EXISTS (SELECT 1 FROM urls WHERE urls.url = u);", (urls,))
        fresh = [r[0] for r in cur.fetchall()]
        if not fresh:
            return 0
        for attempt in range(8):
            cur.execute("SAVEPOINT enq;")
            try:
                n = utils.enqueue_explicit_ids(cur, fresh)
                cur.execute("RELEASE SAVEPOINT enq;")
                return n
            except psycopg2.errors.UniqueViolation:
                cur.execute("ROLLBACK TO SAVEPOINT enq;")
                time.sleep(0.05 * (attempt + 1) + random.random() * 0.1)
        raise RuntimeError("enqueue kept colliding with concurrent writers")
    cur.execute("""
        INSERT INTO url_queue (url)
        SELECT u FROM unnest(%s::varchar[]) AS u
        WHERE NOT EXISTS (SELECT 1 FROM urls WHERE urls.url = u)
          AND NOT EXISTS (SELECT 1 FROM url_queue WHERE url_queue.url = u)
        ORDER BY u
        ON CONFLICT (url) DO NOTHING;""", (urls,))
    return cur.rowcount


class DbWriter:
    """Owns one connection. All methods are blocking: call from a single dedicated thread"""

    def __init__(self, dsn=None):
        self.dsn = dsn or os.getenv("DATABASE_URL")
        self.conn = None
        self.maps = None
        self.ip = utils.get_host_ip()
        self.flushes = 0
        self.claims = None  # url_queue has claimed_at?

    def connect(self):
        if self.conn is not None and not self.conn.closed:
            return
        self.conn = psycopg2.connect(self.dsn, application_name="fast_crawl")
        if self.maps is None:
            self.maps = load_static_maps(self.conn)
        if self.claims is None:
            self.claims = utils.queue_has_claims()

    def close(self):
        if self.conn is not None and not self.conn.closed:
            self.conn.close()

    def write(self, batch):
        """Write a batch, retrying deadlocks / dropped connections. Returns (stats, failed_batch_or_None)"""
        last = None
        for attempt in range(4):
            try:
                self.connect()
                stats = flush(self.conn, self.maps, batch, self.ip, self.claims)
                self.conn.commit()
                self.flushes += 1
                return stats
            except (psycopg2.errors.DeadlockDetected, psycopg2.errors.SerializationFailure, psycopg2.errors.UniqueViolation) as e:
                last = e
                self._rollback()
                time.sleep(0.2 + random.random() * 0.5 * (attempt + 1))
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                last = e
                self._rollback()
                try:
                    self.conn.close()
                except Exception:
                    pass
                time.sleep(1 + attempt * 2)
            except Exception:
                self._rollback()
                raise
        raise last

    def _rollback(self):
        try:
            if self.conn is not None and not self.conn.closed:
                self.conn.rollback()
        except Exception:
            pass

    def queue_estimate(self):
        """Approximate url_queue length (planner statistics, free) or None"""
        try:
            self.connect()
            cur = self.conn.cursor()
            cur.execute("SELECT reltuples::bigint FROM pg_class WHERE oid = to_regclass('url_queue');")
            row = cur.fetchone()
            self.conn.rollback()
            return row[0] if row else None
        except Exception:
            self._rollback()
            return None

    def enqueue_and_claim(self, urls):
        """For --seed-file: queue the new seeds and claim them right away (so they're crawled first, not after a long backlog).
        Returns (newly_queued, claimed_urls)"""
        self.connect()
        cur = self.conn.cursor()
        n = enqueue_new(cur, sorted(urls), self.claims)
        if self.claims:
            cur.execute("UPDATE url_queue SET claimed_at = now() WHERE url = ANY(%s) AND claimed_at IS NULL RETURNING url;", (list(urls),))
        else:
            cur.execute("DELETE FROM url_queue WHERE url = ANY(%s) RETURNING url;", (list(urls),))
        claimed = [r[0] for r in cur.fetchall()]
        self.conn.commit()
        cur.close()
        return n, claimed
