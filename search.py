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

# Current formula (token counts from the `count` column of each *_urls table):
# search_output = ((prefix_score + bigram_score + trigram_score + word_score + total_tokens) / total_tokens) * reference_count
#   word_score    = sum of word_urls.count    for the query's words    on that page
#   prefix_score  = sum of prefix_urls.count  for the query's prefixes on that page
#   bigram_score  = sum of bigram_urls.count  for the query's bigrams  on that page
#   trigram_score = sum of trigram_urls.count for the query's trigrams on that page
#   total_tokens  = urls.total_tokens

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
# * The score only uses the *sum* word_score + bigram_score + trigram_score +
#   prefix_score, so we aggregate the posting lists with ONE GROUP BY instead of
#   four, and only touch URLs that actually matched instead of joining every
#   row of `urls` on every query.
# * The posting-list scans rely on the (token_id, url_id) INCLUDE (count)
#   indexes created by add_search_indexes.py, so url_id and count are both read
#   straight from the index (index-only scan).
# * Connections are pooled and results are cached for a short time.
# ---------------------------------------------------------------------------

POOL_MIN = 1
POOL_MAX = int(os.environ.get("SEARCH_POOL_MAX", "10"))
CACHE_SIZE = int(os.environ.get("SEARCH_CACHE_SIZE", "512"))
CACHE_TTL = float(os.environ.get("SEARCH_CACHE_TTL", "300"))  # seconds

# Per-session planner hints (these never change server-wide settings). They let
# Postgres fan the UNION ALL branches and the final aggregation out across
# parallel workers. Set SEARCH_PG_PARALLEL_WORKERS=0 to disable.
PG_PARALLEL_WORKERS = int(os.environ.get("SEARCH_PG_PARALLEL_WORKERS", "4"))


SEARCH_WORK_MEM = os.environ.get("SEARCH_WORK_MEM", "512MB")


def _session_options():
    if PG_PARALLEL_WORKERS <= 0:
        return None
    return (f"-c max_parallel_workers_per_gather={PG_PARALLEL_WORKERS} "
            "-c parallel_setup_cost=100 -c parallel_tuple_cost=0.01 "
            "-c enable_parallel_append=on -c enable_parallel_hash=on "
            f"-c work_mem={SEARCH_WORK_MEM} -c enable_nestloop=off")


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
                opts = _session_options()
                kwargs = {"options": opts} if opts else {}
                _pool = pg_pool.ThreadedConnectionPool(POOL_MIN, POOL_MAX, url, **kwargs)
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
# w is how many times that token appears on the page, so SUM(w) per page is
# word_score + bigram_score + trigram_score + prefix_score.
_BRANCHES = {
    "word": """
            SELECT url_id, count AS w FROM word_urls
            WHERE word_id = ANY(ARRAY(SELECT id FROM words WHERE word = ANY(%s)))""",
    "bigram": """
            SELECT url_id, count AS w FROM bigram_urls
            WHERE bigram_id = ANY(ARRAY(SELECT id FROM bigrams WHERE bigram = ANY(%s)))""",
    "trigram": """
            SELECT url_id, count AS w FROM trigram_urls
            WHERE trigram_id = ANY(ARRAY(SELECT id FROM trigrams WHERE trigram = ANY(%s)))""",
    "prefix": """
            SELECT url_id, count AS w FROM prefix_urls
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
                (m.token_score + {total_tokens}) / {total_tokens} AS relevance,
                u.reference_count::double precision AS ref_score,
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


# ---------------------------------------------------------------------------
# Fast (approximate) ranking -- the default; set SEARCH_MODE=exact for the
# original full-score query.
#
# The exact score adds the bigram/trigram counts of every matching page. Common
# letter pairs ("th", "in", "ing", ...) match tens of millions of posting rows per
# query, so summing them for every page is slow. Fast mode therefore ranks in two
# stages:
#   1. Score every page matching a word/prefix with those two terms only and
#      keep the best SEARCH_RESCORE_K candidates.
#   2. Re-score just those candidates with the full original formula (exact
#      bigram/trigram counts via index probes) and return the top 10.
# Differences from exact mode: a page that only matches via bigrams/trigrams
# (no word or prefix hit) is never returned, and a page ranked below the top K
# by word/prefix score alone cannot be lifted into the top 10 by its n-grams.
# ---------------------------------------------------------------------------

# total_tokens as a float, so the division isn't rounded down to a whole number.
# Pages stored before urls.total_tokens existed have it NULL: fall back to the token
# totals in url_token_counts so they still get a score. GREATEST(..., 1) avoids dividing by 0.
_TOTAL_TOKENS = ("GREATEST(COALESCE(u.total_tokens, utc.word_count + utc.bigram_count"
                 " + utc.trigram_count + utc.prefix_count), 1)::double precision")

SEARCH_MODE = os.environ.get("SEARCH_MODE", "fast").lower()
STAGE1_BRANCHES = tuple(os.environ.get("SEARCH_STAGE1", "word,prefix").split(","))
RESCORE_K = int(os.environ.get("SEARCH_RESCORE_K", "300"))

_STAGE1_SQL = """
        WITH core AS (
            SELECT url_id, SUM(w)::bigint AS s
            FROM (
                {branches}
            ) m
            GROUP BY url_id
        )
        SELECT core.url_id
        FROM core
        JOIN url_token_counts utc ON utc.url_id = core.url_id
        JOIN urls u ON u.id = core.url_id
        ORDER BY ((core.s + {total_tokens}) / {total_tokens}) * u.reference_count DESC
        LIMIT {k};
"""

# Same branches, restricted to candidate url_ids, for the exact re-scoring stage.
_BRANCHES_FOR_CANDIDATES = {
    name: sql + "\n              AND url_id = ANY(%s)" for name, sql in _BRANCHES.items()
}


def _fast_search(cur, groups):
    core = [n for n in STAGE1_BRANCHES if n in dict(groups)]
    if not core:
        return []
    g = dict(groups)
    cur.execute(
        _STAGE1_SQL.format(
            branches="\n                UNION ALL".join(_BRANCHES[n] for n in core), k=RESCORE_K,
            total_tokens=_TOTAL_TOKENS),
        tuple(g[n] for n in core))
    cand = [r[0] for r in cur.fetchall()]
    if not cand:
        return []
    sql = _SQL_TEMPLATE.format(
        branches="\n            UNION ALL".join(_BRANCHES_FOR_CANDIDATES[n] for n, _ in groups),
        total_tokens=_TOTAL_TOKENS)
    params = []
    for _, vals in groups:
        params += [vals, cand]
    cur.execute(sql, tuple(params))
    return cur.fetchall()


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

    key = (SEARCH_MODE,) + tuple((name, tuple(vals)) for name, vals in groups)
    cached = _cache_get(key)
    if cached is not None:
        debug_print("Cache hit for", query)
        return cached

    sql_query = _SQL_TEMPLATE.format(
        branches="\n            UNION ALL".join(_BRANCHES[name] for name, _ in groups),
        total_tokens=_TOTAL_TOKENS)
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
                if SEARCH_MODE == "exact":
                    cur.execute(sql_query, params)
                    results = cur.fetchall()
                else:
                    results = _fast_search(cur, groups)
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
query = input("Search query: ")
print(search(query))
