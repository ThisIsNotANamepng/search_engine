# Searching function

"""
#Aaron's attribute normalization functions
'''
prefix_score = ceil(len(prefix_matches)/3)
trigram_score = trigram_matches {on the page}
bigram_score = bigram_matches {on the page}
where word_score = len(word)/2
'''

#Aaron's Search:
'''search_output = ((prefix_score + bigram_score + trigram_score + word_score + word_count) / word_count) * reference_count'''

# relevance = sum_of_tokens_of_all_attributes + word_count / word_count
# popularity = reference_count
# search_output = relevance * popularity

"""

import tokenizer
import utils
import time
import os
import threading
from collections import OrderedDict

import psycopg2
from psycopg2 import pool as pg_pool

DEBUG = "DEBUG" in os.environ

def debug_print(text, text1=""):
    # Just prints when DEBUG is passed

    global DEBUG

    if DEBUG:
        print(text, text1)




# ---------------------------------------------------------------------------
# Performance notes
#
# * One round trip: token -> id lookups are inlined as ARRAY(subquery), so the
#   dictionary lookups, the four posting-list scans and the scoring all run in
#   a single statement.
# * Scoring is mathematically identical to the old four-CTE version (the final
#   score only ever used the *sum* weighted word*20 + bigram + trigram +
#   prefix*2), so we aggregate the posting lists with ONE GROUP BY instead of
#   four, and only touch URLs that actually matched instead of joining every
#   row of `urls` on every query.
# * The posting-list scans rely on the (token_id, url_id) indexes created by
#   add_search_indexes.py.
# * Connections are pooled and results are cached for a short time.
# ---------------------------------------------------------------------------

POOL_MIN = 1
POOL_MAX = int(os.environ.get("SEARCH_POOL_MAX", "10"))
CACHE_SIZE = int(os.environ.get("SEARCH_CACHE_SIZE", "512"))
CACHE_TTL = float(os.environ.get("SEARCH_CACHE_TTL", "300"))  # seconds

_pool = None
_pool_lock = threading.Lock()
_cache = OrderedDict()
_cache_lock = threading.Lock()


def _get_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                url = os.getenv("DATABASE_URL")
                if not url:
                    return None
                _pool = pg_pool.ThreadedConnectionPool(POOL_MIN, POOL_MAX, url)
    return _pool


def _acquire():
    """Return (conn, pooled). Falls back to a one-off connection if the pool is unavailable/exhausted."""
    p = _get_pool()
    if p is not None:
        try:
            conn = p.getconn()
            conn.autocommit = True  # read-only; avoids idle-in-transaction sessions
            return conn, True
        except pg_pool.PoolError:
            pass
    return utils.get_conn(), False


def _release(conn, pooled, broken=False):
    if pooled:
        try:
            _pool.putconn(conn, close=broken or conn.closed != 0)
        except Exception:
            pass
    else:
        try:
            conn.close()
        except Exception:
            pass


def _cache_get(key):
    with _cache_lock:
        item = _cache.get(key)
        if item is None:
            return None
        ts, value = item
        if time.time() - ts > CACHE_TTL:
            del _cache[key]
            return None
        _cache.move_to_end(key)
        return list(value)


def _cache_put(key, value):
    if CACHE_SIZE <= 0:
        return
    with _cache_lock:
        _cache[key] = (time.time(), list(value))
        _cache.move_to_end(key)
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)


# Each posting-list branch is only included when the query has tokens of that type.
_BRANCHES = {
    "word": """
            SELECT url_id, 20 AS w FROM word_urls
            WHERE word_id = ANY(ARRAY(SELECT id FROM words WHERE word = ANY(%s)))""",
    "bigram": """
            SELECT url_id, 1 AS w FROM bigram_urls
            WHERE bigram_id = ANY(ARRAY(SELECT id FROM bigrams WHERE bigram = ANY(%s)))""",
    "trigram": """
            SELECT url_id, 1 AS w FROM trigram_urls
            WHERE trigram_id = ANY(ARRAY(SELECT id FROM trigrams WHERE trigram = ANY(%s)))""",
    "prefix": """
            SELECT url_id, 2 AS w FROM prefix_urls
            WHERE prefix_id = ANY(ARRAY(SELECT id FROM prefixes WHERE prefix = ANY(%s)))""",
}

_SQL_TEMPLATE = """
        WITH matched AS (
            SELECT url_id, SUM(w) AS token_score
            FROM (
                {branches}
            ) m
            GROUP BY url_id
        ),
        scored AS (
            SELECT
                u.url,
                u.title,
                ((m.token_score + utc.word_count) / utc.word_count) AS relevance,
                (2.0 - (POWER(u.reference_count, 1.0/1.2) / u.reference_count))::double precision AS ref_score,
                u.reference_count
            FROM matched m
            JOIN url_token_counts utc ON utc.url_id = m.url_id
            JOIN urls u ON u.id = m.url_id
        )
        SELECT
            url,
            title,
            relevance * ref_score AS search_output,
            relevance,
            ref_score,
            reference_count
        FROM scored
        ORDER BY search_output DESC
        LIMIT 10;
"""


def search(query):
    """Run search against the Postgres DB.

    Returns a list of (url, title, search_output, relevance, ref_score, reference_count).
    """
    tokenized = tokenizer.tokenize_all(query)

    words    = list(tokenized[0]) if tokenized and len(tokenized) > 0 and tokenized[0] else []
    bigrams  = list(tokenized[1]) if tokenized and len(tokenized) > 1 and tokenized[1] else []
    trigrams = list(tokenized[2]) if tokenized and len(tokenized) > 2 and tokenized[2] else []
    prefixes = list(tokenized[3]) if tokenized and len(tokenized) > 3 and tokenized[3] else []

    groups = [("word", words), ("bigram", bigrams), ("trigram", trigrams), ("prefix", prefixes)]
    groups = [(name, sorted(set(vals))) for name, vals in groups if vals]
    if not groups:
        return []

    key = tuple((name, tuple(vals)) for name, vals in groups)
    cached = _cache_get(key)
    if cached is not None:
        debug_print("Cache hit for", query)
        return cached

    sql_query = _SQL_TEMPLATE.format(
        branches="\n            UNION ALL".join(_BRANCHES[name] for name, _ in groups))
    params = tuple(vals for _, vals in groups)

    debug_print("Searching....", query)
    debug_print("words:", words)
    debug_print("bigrams:", bigrams)
    debug_print("trigrams:", trigrams)
    debug_print("prefixes:", prefixes)

    results = None
    for attempt in (0, 1):
        conn, pooled = _acquire()
        broken = False
        try:
            start = time.time()
            with conn.cursor() as cur:
                cur.execute(sql_query, params)
                results = cur.fetchall()
            debug_print("Time taken:", time.time() - start)
            break
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            broken = True  # stale pooled connection: retry once on a fresh one
            if attempt == 1:
                raise
        finally:
            _release(conn, pooled, broken)

    for url, title, score, relevance, ref_score, ref_count in results:
        debug_print(f"{url}  | title: {title} |  score: {score}  |  relevance: {relevance}  |  ref_score: {ref_score} | reference_count: {ref_count}")

    _cache_put(key, results)
    return results

# Example usage:
# query = input("Search query: ")
# print(search(query))
