# Searching function

"""
#Aaron's attribute normalization functions
'''
prefix_score = ceil(len(prefix_matches)/3)
trigram_score = trigram_matches {on the page}
bigram_score = bigram_matches {on the page}
word_score = word_matches {on the page}

# Aaron's Search:
# relevance     = (word_score + bigram_score + trigram_score + prefix_score) / total_tokens
# ref_score     = 1 / reference_count^0.166667
# search_output = relevance / ref_score
#   word_score    = sum of word_urls.count    for the query's words    on that page
#   prefix_score  = sum of prefix_urls.count  for the query's prefixes on that page
#   bigram_score  = sum of bigram_urls.count  for the query's bigrams  on that page
#   trigram_score = sum of trigram_urls.count for the query's trigrams on that page
#   total_tokens  = urls.total_tokens
#   reference_count = urls.reference_count

"""

import tokenizer
import utils
import time
import os
import threading
import heapq
from collections import OrderedDict

import psycopg2
from psycopg2 import pool as pg_pool

import search_memindex as memindex
from types import SimpleNamespace

try:
    import numpy as np
except ImportError:  # the in-memory fast path is optional; SQL path is used without numpy
    np = None

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
CACHE_SIZE = int(os.environ.get("SEARCH_CACHE_SIZE", "2048"))
CACHE_TTL = float(os.environ.get("SEARCH_CACHE_TTL", "300"))  # seconds

# Per-session settings (these never change server-wide settings). Parallel workers let
# Postgres fan the posting-list scans out. Set SEARCH_PG_PARALLEL_WORKERS=0 to disable.
PG_PARALLEL_WORKERS = int(os.environ.get("SEARCH_PG_PARALLEL_WORKERS", "2"))
SEARCH_WORK_MEM = os.environ.get("SEARCH_WORK_MEM", "64MB")
STATEMENT_TIMEOUT_MS = int(os.environ.get("SEARCH_STATEMENT_TIMEOUT_MS", "5000"))


def _session_options():
    opts = f"-c work_mem={SEARCH_WORK_MEM} -c statement_timeout={STATEMENT_TIMEOUT_MS}"
    if PG_PARALLEL_WORKERS > 0:
        opts += (f" -c max_parallel_workers_per_gather={PG_PARALLEL_WORKERS}"
                 " -c parallel_setup_cost=100 -c parallel_tuple_cost=0.01"
                 " -c enable_parallel_append=on -c enable_parallel_hash=on")
    return opts


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
                m.token_score / {total_tokens} AS relevance,
                {ref_score} AS ref_score,
                u.reference_count
            FROM matched m
            JOIN urls u ON u.id = m.url_id
        )
        SELECT
            url,
            title,
            relevance / ref_score AS search_output,
            relevance,
            ref_score,
            reference_count
        FROM scored
        ORDER BY search_output DESC NULLS LAST, url  -- url breaks ties so results are stable
        LIMIT {limit} OFFSET {offset};
"""


# ---------------------------------------------------------------------------
# LEGACY ranking (SEARCH_MODE=legacy/fast or exact; the default is now bm25, see below).
# Fast (approximate) legacy ranking; SEARCH_MODE=exact is the original full-score query.
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
# GREATEST(..., 1) avoids dividing by 0. A page with no total_tokens (NULL) gets a NULL
# score, and NULLS LAST keeps it below every scored page.
_TOTAL_TOKENS = "GREATEST(u.total_tokens, 1)::double precision"

# ref_score = 1 / reference_count^0.166667. reference_count is never below 1 (column default),
# so this is never 0 and search_output = relevance / ref_score is always defined.
_REF_SCORE = "(1.0 / POWER(u.reference_count, 0.166667))::double precision"

SEARCH_MODE = os.environ.get("SEARCH_MODE", "bm25").lower()
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
        JOIN urls u ON u.id = core.url_id
        ORDER BY (core.s / {total_tokens}) / {ref_score} DESC NULLS LAST
        LIMIT {k};
"""

# Same branches, restricted to candidate url_ids, for the exact re-scoring stage.
_BRANCHES_FOR_CANDIDATES = {
    name: sql + "\n              AND url_id = ANY(%s)" for name, sql in _BRANCHES.items()
}


def _fast_search(cur, groups, limit=10, offset=0):
    core = [n for n in STAGE1_BRANCHES if n in dict(groups)]
    if not core:
        return []
    g = dict(groups)
    cur.execute(
        _STAGE1_SQL.format(
            branches="\n                UNION ALL".join(_BRANCHES[n] for n in core),
            k=max(RESCORE_K, offset + limit),
            total_tokens=_TOTAL_TOKENS, ref_score=_REF_SCORE),
        tuple(g[n] for n in core))
    cand = [r[0] for r in cur.fetchall()]
    if not cand:
        return []
    sql = _SQL_TEMPLATE.format(
        branches="\n            UNION ALL".join(_BRANCHES_FOR_CANDIDATES[n] for n, _ in groups),
        total_tokens=_TOTAL_TOKENS, ref_score=_REF_SCORE, limit=limit, offset=offset)
    params = []
    for _, vals in groups:
        params += [vals, cand]
    cur.execute(sql, tuple(params))
    return cur.fetchall()




# ---------------------------------------------------------------------------
# BM25 ranking -- the default (SEARCH_MODE=bm25). See docs/docs/search.md.
#
#   score = bm25 * coverage * title_boost * pop_boost
#
#   bm25       = sum over query words w found on the page of
#                  idf(w) * tf*(k1+1) / (tf + k1*(1 - b + b*dl/avgdl))
#                tf  = word_urls.count, dl = urls.total_tokens,
#                avgdl = mean total_tokens over indexed pages,
#                idf = ln(1 + (N - df + 0.5)/(df + 0.5)), df = #postings of the word
#   coverage   = (matched query words / query words) ** SEARCH_COVERAGE_POW
#   title_boost= 1 + TITLE*frac_in_title + HOST*frac_in_hostname (+ small bonuses)
#   pop_boost  = 1 + ALPHA * ln(1 + reference_count)
#
# Only *word* postings generate candidates, so a page with none of the query words
# can never outrank one that has them. Prefix/trigram postings (weighted with the
# `weights` table) are used only as a typo/partial-match fallback, appended after
# the word matches, and only when there are not enough word matches to fill the page.
# Everything below the SQL is pure Python (testable without a database).
# ---------------------------------------------------------------------------

import math
import re

K1 = float(os.environ.get("SEARCH_BM25_K1", "1.2"))
B = float(os.environ.get("SEARCH_BM25_B", "0.75"))
POP_ALPHA = float(os.environ.get("SEARCH_POP_ALPHA", "0.15"))
TITLE_BOOST = float(os.environ.get("SEARCH_TITLE_BOOST", "0.6"))
HOST_BOOST = float(os.environ.get("SEARCH_HOST_BOOST", "0.5"))
HOME_BOOST = float(os.environ.get("SEARCH_HOME_BOOST", "0.15"))
COVERAGE_POW = float(os.environ.get("SEARCH_COVERAGE_POW", "1.5"))
FUZZY = os.environ.get("SEARCH_FUZZY", "0") == "1"   # off by default: trigram overlap is noisy on long pages
FUZZY_MIN = float(os.environ.get("SEARCH_FUZZY_MIN", "0.85"))     # share of query trigrams a page needs
FUZZY_SCALE = float(os.environ.get("SEARCH_FUZZY_SCALE", "0.25"))
STATS_TTL = float(os.environ.get("SEARCH_STATS_TTL", "600"))
HEAVY_DF = int(os.environ.get("SEARCH_HEAVY_DF", "100000"))  # words with more postings are probed, not scanned
CORRECTIONS = os.environ.get("SEARCH_CORRECTIONS", "1") != "0"      # typo correction for unknown words
CORRECTION_PENALTY = float(os.environ.get("SEARCH_CORRECTION_PENALTY", "0.85"))
# Pages whose *title or hostname* contains the query words but whose body postings do not
# (titles are not indexed as words). Needs a scan of `urls`, so it is skipped once the
# index is larger than SEARCH_NAV_MAX_DOCS pages (set 0 to disable).
NAV_MAX_DOCS = int(os.environ.get("SEARCH_NAV_MAX_DOCS", "200000"))
NAV_BASE = float(os.environ.get("SEARCH_NAV_BASE", "2.0"))

DEFAULT_WEIGHTS = {"word": 1.7, "bigram": 1.2, "trigram": 1.3, "prefix": 1.2}


# ---- pure scoring functions -------------------------------------------------

def idf(n_docs, df):
    """Robertson/Lucene idf, always > 0."""
    return math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))


def tf_saturation(tf, dl, avgdl, k1=None, b=None):
    k1 = K1 if k1 is None else k1
    b = B if b is None else b
    avgdl = avgdl if avgdl and avgdl > 0 else 1.0
    dl = max(dl or 0, 1)
    return tf * (k1 + 1.0) / (tf + k1 * (1.0 - b + b * dl / avgdl))


def bm25_score(tfs, dfs, dl, n_docs, avgdl, k1=None, b=None):
    """tfs/dfs: parallel lists for the query words found on the page."""
    return sum(idf(n_docs, df) * tf_saturation(tf, dl, avgdl, k1, b) for tf, df in zip(tfs, dfs))


def coverage_factor(matched, n_query_words, power=None):
    power = COVERAGE_POW if power is None else power
    if n_query_words <= 0:
        return 1.0
    return (min(matched, n_query_words) / n_query_words) ** power


def popularity_boost(reference_count, alpha=None):
    alpha = POP_ALPHA if alpha is None else alpha
    return 1.0 + alpha * math.log1p(max(reference_count or 0, 0))


def _word_in_text(word, text):
    if not word.isascii():
        return word in text
    return re.search(r"(?<![0-9a-z])" + re.escape(word) + r"(?![0-9a-z])", text) is not None


def _host_and_depth(url):
    m = re.match(r"^[a-z][a-z0-9+.-]*://([^/?#]*)([^?#]*)", (url or "").lower())
    if not m:
        return "", 99
    host = m.group(1).rsplit("@", 1)[-1].split(":")[0]
    depth = len([p for p in m.group(2).split("/") if p])
    return host, depth


_SIMPLE_WORD = re.compile(r"[0-9a-z]+")


def _title_hits(words, title_l):
    """How many query words occur as whole words in the lower-cased title (same result as
    _word_in_text per word, but tokenises the title once for plain [0-9a-z] words)."""
    toks = None
    hits = 0
    for w in words:
        if w.isascii() and _SIMPLE_WORD.fullmatch(w):
            if toks is None:
                toks = set(_SIMPLE_WORD.findall(title_l))
            hits += w in toks
        elif _word_in_text(w, title_l):
            hits += 1
    return hits


def title_boost(title, url, words):
    """Returns (multiplier, title_fraction, host_fraction). Words found in the title or hostname."""
    if not words:
        return 1.0, 0.0, 0.0
    title_l = (title or "").lower()
    t_hits = _title_hits(words, title_l)
    host, depth = _host_and_depth(url)
    host_tokens = set(re.split(r"[^0-9a-z\u0080-\uffff]+", host)) - {""}
    h_hits = sum(1 for w in words if w in host_tokens)
    t_frac, h_frac = t_hits / len(words), h_hits / len(words)
    mult = 1.0 + TITLE_BOOST * t_frac + HOST_BOOST * h_frac
    if h_hits and depth <= 1:
        mult += HOME_BOOST  # the site's front page for a query naming the site
    return mult, t_frac, h_frac


def score_candidate(row, words, n_docs, avgdl):
    """row = (url, title, total_tokens, reference_count, matched_words, tfs, dfs) -> debug dict."""
    url, title, total_tokens, ref_count, m_words, tfs, dfs = row
    text = bm25_score(tfs, dfs, total_tokens, n_docs, avgdl)
    cov = coverage_factor(len(m_words), len(words))
    tb, t_frac, h_frac = title_boost(title, url, words)
    pop = popularity_boost(ref_count)
    return {
        "url": url, "title": title, "score": text * cov * tb * pop,
        "bm25": text, "coverage": cov, "title_boost": tb,
        "title_frac": t_frac, "host_frac": h_frac,
        "pop_boost": pop, "reference_count": ref_count or 0,
        "matched_words": list(m_words), "query_words": list(words),
        "total_tokens": total_tokens, "avgdl": avgdl, "n_docs": n_docs,
        "term_freq": dict(zip(m_words, tfs)), "doc_freq": dict(zip(m_words, dfs)),
        "kind": "word",
    }


def score_fuzzy(row, words, n_tri, weights):
    """row = (url, title, total_tokens, reference_count, tri_hits, prefix_hit)."""
    url, title, total_tokens, ref_count, tri_hits, prefix_hit = row
    frac = tri_hits / max(n_tri, 1)
    text = FUZZY_SCALE * (weights["trigram"] * frac + weights["prefix"] * (1.0 if prefix_hit else 0.0))
    tb, t_frac, h_frac = title_boost(title, url, words)
    pop = popularity_boost(ref_count)
    return {
        "url": url, "title": title, "score": text * tb * pop,
        "bm25": text, "coverage": frac, "title_boost": tb,
        "title_frac": t_frac, "host_frac": h_frac,
        "pop_boost": pop, "reference_count": ref_count or 0,
        "matched_words": [], "query_words": list(words),
        "total_tokens": total_tokens, "avgdl": None, "n_docs": None,
        "term_freq": {}, "doc_freq": {}, "kind": "fuzzy",
        "trigram_hits": tri_hits, "prefix_hit": bool(prefix_hit),
    }


def score_nav(row, words):
    """row = (url, title, total_tokens, reference_count): a title/hostname-only match."""
    url, title, total_tokens, ref_count = row
    tb, t_frac, h_frac = title_boost(title, url, words)
    hits = max(t_frac, h_frac)
    if hits * len(words) < max(1, len(words) / 2.0):
        return None
    text = NAV_BASE * hits
    pop = popularity_boost(ref_count)
    return {
        "url": url, "title": title, "score": text * tb * pop,
        "bm25": text, "coverage": hits, "title_boost": tb,
        "title_frac": t_frac, "host_frac": h_frac,
        "pop_boost": pop, "reference_count": ref_count or 0,
        "matched_words": [], "query_words": list(words),
        "total_tokens": total_tokens, "avgdl": None, "n_docs": None,
        "term_freq": {}, "doc_freq": {}, "kind": "title",
    }


def rank(scored):
    """Stable ordering: word/title matches first, n-gram fallbacks last; then score, then url."""
    return sorted(scored, key=lambda d: (d["kind"] == "fuzzy", -d["score"], d["url"]))


# ---- SQL --------------------------------------------------------------------
# Word ids and document frequencies come from one cheap index-only count per word and are
# cached in-process (SEARCH_STATS_TTL), so the planner sees literal word ids and real
# statistics instead of guessing. `d` is an index-only scan of the (word_id, url_id)
# INCLUDE (count) index for the *driving* words (df <= SEARCH_HEAVY_DF). Very common words
# are not scanned at all: they are probed by (word_id, url_id) for the candidates that the
# rarer words produced, so cost is bounded by the rare words' posting lists, not by "the".
# The SQL ordering only picks the top-K candidates; the final score is computed by the
# Python functions above (same formula, plus title/host boost).
_BM25_SQL_HEAD = """
WITH w AS (
    SELECT * FROM unnest(%(ids)s::int[], %(words)s::text[], %(idfs)s::float8[], %(dfs)s::int[]) AS t(id, word, idf, df)
), d AS (
    SELECT word_id, url_id, count AS tf FROM word_urls WHERE word_id = ANY(%(drive)s::int[])
){probe}
"""
_BM25_SQL_PROBE = """, pr AS (
    -- LATERAL ... OFFSET 0 forces one (word_id, url_id) index probe per candidate instead of
    -- letting the planner scan the whole posting list of the common word into a hash join.
    SELECT wu.word_id, wu.url_id, wu.tf
    FROM (SELECT DISTINCT url_id FROM d) c
    CROSS JOIN LATERAL (
        SELECT word_id, url_id, count AS tf FROM word_urls
        WHERE url_id = c.url_id AND word_id = ANY(%(probe)s::int[]) OFFSET 0
    ) wu
), h AS (
    SELECT * FROM d UNION ALL SELECT * FROM pr
)"""
_BM25_SQL_TAIL = """
SELECT u.url, u.title, u.total_tokens, COALESCE(u.reference_count, 0),
       array_agg(w.word ORDER BY w.word), array_agg(h.tf ORDER BY w.word), array_agg(w.df ORDER BY w.word)
FROM {src} h
JOIN w ON w.id = h.word_id
JOIN urls u ON u.id = h.url_id
WHERE u.total_tokens IS NOT NULL
GROUP BY u.id, u.url, u.title, u.total_tokens, u.reference_count
ORDER BY sum(w.idf * h.tf * (%(k1)s + 1) / (h.tf + %(k1)s * (1 - %(b)s + %(b)s * u.total_tokens / %(avgdl)s)))
         * power(count(*)::float8 / %(n_words)s, %(cov_pow)s)
         * (1 + %(alpha)s * ln(1 + COALESCE(u.reference_count, 0)::float8)) DESC,
         u.id
LIMIT %(k)s
"""
_BM25_SQL = _BM25_SQL_HEAD.format(probe="") + _BM25_SQL_TAIL.format(src="d")
_BM25_SQL_WITH_PROBE = _BM25_SQL_HEAD.format(probe=_BM25_SQL_PROBE) + _BM25_SQL_TAIL.format(src="h")

_DF_SQL = """
SELECT w.word, w.id, (SELECT count(*) FROM word_urls wu WHERE wu.word_id = w.id)
FROM words w WHERE w.word = ANY(%(words)s)
"""

_FUZZY_SQL = """
WITH tq AS (
    SELECT id FROM trigrams WHERE trigram = ANY(%(trigrams)s)
), tm AS (
    SELECT url_id, count(DISTINCT trigram_id) AS tg FROM trigram_urls
    WHERE trigram_id = ANY(ARRAY(SELECT id FROM tq))
    GROUP BY url_id HAVING count(DISTINCT trigram_id) >= %(min_tg)s
), pm AS (
    SELECT url_id, sum(count) AS pc FROM prefix_urls
    WHERE prefix_id = ANY(ARRAY(SELECT id FROM prefixes WHERE prefix = ANY(%(prefixes)s)))
      AND url_id IN (SELECT url_id FROM tm)
    GROUP BY url_id
)
SELECT u.url, u.title, u.total_tokens, COALESCE(u.reference_count, 0), tm.tg, pm.pc IS NOT NULL
FROM tm
JOIN urls u ON u.id = tm.url_id
LEFT JOIN pm ON pm.url_id = tm.url_id
WHERE u.total_tokens IS NOT NULL
ORDER BY tm.tg DESC, (pm.pc IS NOT NULL) DESC, COALESCE(u.reference_count, 0) DESC, u.id
LIMIT %(k)s
"""

_NAV_SQL = """
SELECT url, title, total_tokens, COALESCE(reference_count, 0) FROM urls
WHERE total_tokens IS NOT NULL
  AND (lower(title) LIKE ANY(%(pats)s) OR lower(url) LIKE ANY(%(pats)s))
ORDER BY reference_count DESC NULLS LAST, id
LIMIT %(k)s
"""

_stats = {"ts": 0.0, "n_docs": None, "avgdl": None, "weights": dict(DEFAULT_WEIGHTS), "approx": False}
_stats_lock = threading.Lock()


def _load_stats(cur):
    """N, avgdl and n-gram weights, cached for STATS_TTL seconds. Read-only."""
    now = time.time()
    with _stats_lock:
        if _stats["n_docs"] is not None and now - _stats["ts"] < STATS_TTL:
            return _stats["n_docs"], _stats["avgdl"], _stats["weights"]
    approx = False
    try:
        cur.execute("SELECT count(total_tokens), COALESCE(avg(total_tokens), 1) FROM urls")
        n_docs, avgdl = cur.fetchone()
    except psycopg2.errors.QueryCanceled:
        # Huge table + tight timeout: fall back to the planner's estimate.
        approx = True
        cur.execute("SELECT GREATEST(reltuples, 1)::bigint FROM pg_class WHERE oid = 'urls'::regclass")
        n_docs, avgdl = cur.fetchone()[0], (_stats["avgdl"] or 1500.0)
    weights = dict(DEFAULT_WEIGHTS)
    try:
        cur.execute("SELECT type, weight FROM weights")
        weights.update({t: float(w) for t, w in cur.fetchall() if t in weights})
    except psycopg2.Error:
        pass
    n_docs, avgdl = max(int(n_docs or 0), 1), max(float(avgdl or 1), 1.0)
    with _stats_lock:
        _stats.update(ts=now, n_docs=n_docs, avgdl=avgdl, weights=weights, approx=approx)
    return n_docs, avgdl, weights


_df_cache = {}
_df_lock = threading.Lock()


def _word_dfs(cur, words):
    """{word: (word_id, df)} for the words that exist in the index with >= 1 posting. Cached."""
    now = time.time()
    out, todo = {}, []
    with _df_lock:
        for w in words:
            hit = _df_cache.get(w)
            if hit and now - hit[0] < STATS_TTL:
                if hit[1]:
                    out[w] = hit[1]
            else:
                todo.append(w)
    if todo:
        cur.execute(_DF_SQL, {"words": todo})
        found = {w: (i, int(df)) for w, i, df in cur.fetchall() if df}
        with _df_lock:
            for w in todo:
                _df_cache[w] = (now, found.get(w))
            if len(_df_cache) > 20000:
                _df_cache.clear()
        out.update(found)
    return out


def split_drive_probe(dfs, heavy=None):
    """dfs: {word: (id, df)} -> (drive words, probe words). Common words are only probed."""
    heavy = HEAVY_DF if heavy is None else heavy
    order = sorted(dfs, key=lambda w: (dfs[w][1], w))
    drive = [w for w in order if dfs[w][1] <= heavy] or order[:1]
    return drive, [w for w in order if w not in drive]


def _bm25_candidates(cur, words, n_docs, avgdl, k, query_words=None):
    """Top-k pages for `words` scored with the Python formula. Returns (dicts, unknown words)."""
    query_words = query_words or words
    dfs = _word_dfs(cur, words)
    unknown = [w for w in words if w not in dfs]
    if not dfs:
        return [], unknown
    drive, probe = split_drive_probe(dfs)
    names = sorted(dfs)
    params = {"ids": [dfs[w][0] for w in names], "words": names,
              "idfs": [idf(n_docs, dfs[w][1]) for w in names], "dfs": [dfs[w][1] for w in names],
              "drive": [dfs[w][0] for w in drive], "probe": [dfs[w][0] for w in probe],
              "k1": K1, "b": B, "avgdl": float(avgdl), "n_words": len(query_words),
              "cov_pow": COVERAGE_POW, "alpha": POP_ALPHA, "k": k}
    cur.execute(_BM25_SQL_WITH_PROBE if probe else _BM25_SQL, params)
    return [score_candidate(r, query_words, n_docs, avgdl) for r in cur.fetchall()], unknown


def _bm25_search(cur, words, limit, offset):
    """Core ranking on an open cursor. Returns the ranked list of debug dicts for the page."""
    n_docs, avgdl, weights = _load_stats(cur)
    want = limit + offset
    k = max(RESCORE_K, want)
    scored, unknown = _bm25_candidates(cur, words, n_docs, avgdl, k)
    if 0 < n_docs <= NAV_MAX_DOCS:
        have = {d["url"] for d in scored}
        try:
            pats = ["%" + w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" for w in words]
            cur.execute(_NAV_SQL, {"pats": pats, "k": 100})
            for r in cur.fetchall():
                if r[0] not in have:
                    d = score_nav(r, words)
                    if d:
                        scored.append(d)
        except psycopg2.errors.QueryCanceled:
            pass

    missing = unknown  # words with no postings at all
    if missing and CORRECTIONS:
        fixes = _corrections(cur, missing)
        if fixes:
            fixed_words = sorted({fixes.get(w, w) for w in words})
            cand, _ = _bm25_candidates(cur, fixed_words, n_docs, avgdl, k, query_words=fixed_words)
            best = {d["url"]: d for d in scored}
            for d in cand:
                d["score"] *= CORRECTION_PENALTY
                d["kind"] = "corrected"
                d["corrections"] = dict(fixes)
                cur_best = best.get(d["url"])
                if cur_best is None or d["score"] > cur_best["score"]:
                    best[d["url"]] = d
            scored = list(best.values())
            missing = [w for w in missing if w not in fixes]
    ranked = rank(scored)

    if FUZZY and len(ranked) < want and missing:
        fuzzy = _fuzzy_search(cur, missing, words, weights, k)
        have = {d["url"] for d in ranked}
        fuzzy = [d for d in fuzzy if d["url"] not in have]
        if ranked and fuzzy:  # never let an n-gram-only page look better than a word match
            floor = min(d["score"] for d in ranked)
            for d in fuzzy:
                d["score"] = min(d["score"], floor * 0.99)
        ranked = ranked + rank(fuzzy)
    return ranked[offset:offset + limit]


# ---- typo correction against the vocabulary (words.word is a btree: range scan) ----

def edit_distance(a, b):
    """Optimal-string-alignment (Damerau) distance: insert/delete/substitute/transpose."""
    la, lb = len(a), len(b)
    d = [[0] * (lb + 1) for _ in range(la + 1)]
    for i in range(la + 1):
        d[i][0] = i
    for j in range(lb + 1):
        d[0][j] = j
    for i in range(1, la + 1):
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[la][lb]


def max_typo_distance(word):
    n = len(word)
    return 0 if n < 4 or not word.isascii() else (1 if n < 9 else 2)


def pick_correction(word, vocabulary):
    """Closest vocabulary word within max_typo_distance (ties -> alphabetical), or None."""
    limit = max_typo_distance(word)
    best = None
    for v in vocabulary:
        if v == word or abs(len(v) - len(word)) > limit:
            continue
        dist = edit_distance(word, v)
        if dist <= limit and (best is None or (dist, v) < best):
            best = (dist, v)
    return best[1] if best else None


_CORR_SQL = """SELECT word FROM words WHERE word >= %(lo)s AND word < %(hi)s
               AND length(word) BETWEEN %(minl)s AND %(maxl)s LIMIT 20000"""
_corr_cache = OrderedDict()


def _corrections(cur, missing):
    out = {}
    for w in missing:
        lim = max_typo_distance(w)
        if not lim:
            continue
        if w in _corr_cache and time.time() - _corr_cache[w][0] < STATS_TTL:
            fix = _corr_cache[w][1]
        else:
            fix = None
            for plen in (2, 1):  # narrow range first (typos rarely hit the first two letters)
                lo = w[:plen]
                hi = lo[:-1] + chr(ord(lo[-1]) + 1)
                try:
                    cur.execute(_CORR_SQL, {"lo": lo, "hi": hi, "minl": len(w) - lim, "maxl": len(w) + lim})
                except psycopg2.errors.QueryCanceled:
                    break
                fix = pick_correction(w, [r[0] for r in cur.fetchall()])
                if fix:
                    break
            _corr_cache[w] = (time.time(), fix)
            while len(_corr_cache) > 2048:
                _corr_cache.popitem(last=False)
        if fix:
            out[w] = fix
    return out


def _fuzzy_search(cur, missing_words, all_words, weights, k):
    trigrams, prefixes = set(), set()
    for w in missing_words:
        if len(w) < 5:
            continue  # too short for trigram overlap to mean anything
        t = tokenizer.tokenize_all(w)
        trigrams |= set(t[2])
        prefixes |= set(t[3])
    if not trigrams:
        return []
    min_tg = max(1, math.ceil(FUZZY_MIN * len(trigrams)))
    try:
        cur.execute(_FUZZY_SQL, {"trigrams": sorted(trigrams), "prefixes": sorted(prefixes),
                                 "min_tg": min_tg, "k": k})
        rows = cur.fetchall()
    except psycopg2.errors.QueryCanceled:
        return []  # the fallback is best-effort; never fail the search over it
    return [score_fuzzy(r, all_words, len(trigrams), weights) for r in rows]


# ---- in-memory fast path ------------------------------------------------------
# One round trip per *uncached* word (a single statement that resolves the word and returns its whole
# posting list as two packed int32 byte strings); everything else -- df (= posting count), BM25,
# coverage, popularity, title/host boosts, nav candidates, top-K selection -- runs in-process on
# numpy arrays from search_memindex. Posting lists are cached (LRU, byte budget, TTL), so a repeated
# word costs no round trip at all. The formula is the same as the SQL path above.

FAST = os.environ.get("SEARCH_FAST", "1") != "0"
POSTING_CACHE_MB = float(os.environ.get("SEARCH_POSTING_CACHE_MB", "48"))
POSTING_TTL = float(os.environ.get("SEARCH_POSTING_TTL", "300"))            # fresh for this long
POSTING_HARD_TTL = float(os.environ.get("SEARCH_POSTING_HARD_TTL", "3600"))  # then stale-while-revalidate
PREWARM_WORDS = int(os.environ.get("SEARCH_PREWARM_WORDS", "48"))           # 0 = no start-up pre-warm
FETCH_THREADS = int(os.environ.get("SEARCH_FETCH_THREADS", "2"))            # concurrent cold fetches (1 = one statement)

_POSTINGS_SQL = """
SELECT w.word, w.id, a.urls, a.tfs
FROM words w
CROSS JOIN LATERAL (
    SELECT string_agg(int4send(wu.url_id), ''::bytea) AS urls, string_agg(int4send(wu.count), ''::bytea) AS tfs
    FROM word_urls wu WHERE wu.word_id = w.id
) a
WHERE w.word = ANY(%(words)s) AND a.urls IS NOT NULL
"""
_VARIANT_SQL = """SELECT w.word FROM words w WHERE w.word = ANY(%(cands)s)
                  AND EXISTS (SELECT 1 FROM word_urls wu WHERE wu.word_id = w.id)"""


class _PostingCache:
    """word -> (word_id, url_ids int32[], tfs int32[], slot) or None (unknown word). LRU with a byte
    budget. Entries older than `ttl` are 'stale': still usable (callers refresh them in the
    background) until `hard_ttl`, after which they are misses."""

    def __init__(self):
        self.lock = threading.Lock()
        self.d = OrderedDict()
        self.bytes = 0

    def get(self, word, now, ttl, hard_ttl=None):
        """-> (state, value) with state 'fresh' | 'stale' | 'miss'."""
        hard_ttl = ttl if hard_ttl is None else hard_ttl
        with self.lock:
            hit = self.d.get(word)
            if hit is None:
                return "miss", None
            age = now - hit[0]
            if age > hard_ttl:
                self._drop(word)
                return "miss", None
            self.d.move_to_end(word)
            return ("fresh" if age <= ttl else "stale"), hit[1]

    @staticmethod
    def _size(value):
        return 0 if value is None else value[1].nbytes + value[2].nbytes + value[1].size * 8 + 200

    def _drop(self, word):
        old = self.d.pop(word, None)
        if old is not None:
            self.bytes -= self._size(old[1])

    def put(self, word, value, now):
        size = self._size(value)
        budget = POSTING_CACHE_MB * 1024 * 1024
        if size > budget:
            return
        with self.lock:
            self._drop(word)
            self.d[word] = (now, value)
            self.bytes += size
            while self.bytes > budget or len(self.d) > 200000:
                self._drop(next(iter(self.d)))

    def clear(self):
        with self.lock:
            self.d.clear()
            self.bytes = 0

    def __len__(self):
        return len(self.d)


_posting_cache = _PostingCache()
_corr_lock = threading.Lock()


class _DfCache(dict):
    """The SQL path's df cache; clearing it also clears the in-memory path's posting cache
    (that cache is what replaces df lookups), so 'df-cache cold' benchmarks stay honest."""

    def clear(self):
        super().clear()
        _posting_cache.clear()


_df_cache = _DfCache()


def clear_caches():
    """Empty every in-process result/df/posting/correction cache (not the memory index)."""
    _cache.clear()
    _df_cache.clear()
    _posting_cache.clear()
    with _corr_lock:
        _corr_cache.clear()


class _Stages:
    """Per-query timing breakdown in ms: mark(name) charges the time since the previous mark."""
    __slots__ = ("d", "t", "t0")

    def __init__(self):
        self.d = {"round_trips": 0, "db_ms": 0.0}
        self.t = self.t0 = time.perf_counter()

    def mark(self, name):
        now = time.perf_counter()
        self.d[name] = self.d.get(name, 0.0) + (now - self.t) * 1000.0
        self.t = now

    def finish(self):
        self.d["total_ms"] = (time.perf_counter() - self.t0) * 1000.0
        return self.d


last_timings = {}        # breakdown of the most recent search_debug call in this process
_tl = threading.local()  # ... and per thread (use get_timings() under a threaded server)


def get_timings():
    return dict(getattr(_tl, "timings", None) or last_timings)


def _db(st, fn):
    """_run_with_conn with round-trip accounting."""
    t = time.perf_counter()
    try:
        return _run_with_conn(fn)
    finally:
        st.d["round_trips"] += 1
        st.d["db_ms"] += (time.perf_counter() - t) * 1000.0


def _fast_ready():
    return FAST and not FUZZY and np is not None and memindex.get() is not None


_started_pid = None


def warmup():
    """Start loading the memory index in a background thread (idempotent, per process).
    Called at import when DATABASE_URL is set and lazily from the first search."""
    global _started_pid
    if not FAST or FUZZY or np is None or not os.getenv("DATABASE_URL"):
        return False
    if _started_pid != os.getpid():
        _started_pid = os.getpid()
        memindex.start(_run_with_conn, on_ready=_prewarm)
    return True


def _prewarm():
    """Background: fetch the posting lists of the most common title words so the first searches
    for popular topics do not wait for the database. Bounded (SEARCH_PREWARM_WORDS words, 4 per
    round trip, paused between batches); failures are ignored."""
    idx = memindex.get()
    if PREWARM_WORDS <= 0 or idx is None:
        return
    words = []
    for tok in idx.top_title_tokens(PREWARM_WORDS * 2):
        for w in dict(_query_groups(tok)).get("word", []):
            if w not in words:
                words.append(w)
    words = words[:PREWARM_WORDS]
    for i in range(0, len(words), 4):
        batch = [w for w in words[i:i + 4] if _posting_cache.get(w, time.time(), POSTING_TTL)[0] == "miss"]
        if not batch:
            continue
        try:
            found = _run_with_conn(lambda cur: _fetch_postings(cur, batch))
        except Exception:
            return
        now = time.time()
        for w in batch:
            _posting_cache.put(w, found.get(w), now)
        time.sleep(0.05)


def _decode_postings(raw_urls, raw_tfs):
    """Packed big-endian int32 pairs -> (url_ids, tfs) sorted by url id, one row per page."""
    u = np.frombuffer(raw_urls, dtype=">i4").astype(np.int32)
    t = np.frombuffer(raw_tfs, dtype=">i4").astype(np.int32)
    if u.size > 1 and not (u[1:] > u[:-1]).all():
        order = np.argsort(u, kind="stable")
        u, t = u[order], t[order]
        keep = np.concatenate(([True], u[1:] != u[:-1]))
        u, t = u[keep], t[keep]
    return u, t


def _fetch_postings(cur, words):
    cur.execute(_POSTINGS_SQL, {"words": words})
    found = {}
    for word, wid, ub, tb in cur.fetchall():
        u, t = _decode_postings(ub, tb)
        found[word] = (wid, u, t, [None])    # [None]: slot for the per-snapshot BM25 contribution
    return found


_fetch_pool = None
_fetch_pool_lock = threading.Lock()


def _fetch_many(st, words):
    """Posting lists for `words` that are not cached. One statement for a single word; several words are
    fetched concurrently (one statement each, SEARCH_FETCH_THREADS at a time) because a cold fetch is
    dominated by database I/O latency, which overlaps across connections."""
    global _fetch_pool
    if len(words) < 2 or FETCH_THREADS < 2:
        return _db(st, lambda cur: _fetch_postings(cur, words))
    from concurrent.futures import ThreadPoolExecutor
    with _fetch_pool_lock:
        if _fetch_pool is None:
            _fetch_pool = ThreadPoolExecutor(max_workers=FETCH_THREADS, thread_name_prefix="search-fetch")
    t = time.perf_counter()
    futures = [_fetch_pool.submit(_run_with_conn, lambda cur, w=w: _fetch_postings(cur, [w])) for w in words]
    found, err = {}, None
    for f in futures:
        try:
            found.update(f.result())
        except Exception as e:           # let every statement finish first, then fail like a single one would
            err = err or e
    st.d["round_trips"] += len(words)
    st.d["db_ms"] += (time.perf_counter() - t) * 1000.0
    if err is not None:
        raise err
    return found


_inflight = set()
_inflight_lock = threading.Lock()


def _spawn(fn):
    th = threading.Thread(target=fn, name="search-refresh", daemon=True)
    th.start()
    return th


def _refresh_async(words):
    """Refresh stale posting lists in the background (stale-while-revalidate): the query that
    noticed them is answered from the old lists, so nobody waits for the round trip."""
    with _inflight_lock:
        words = [w for w in words if w not in _inflight]
        _inflight.update(words)
    if not words:
        return

    def work():
        try:
            found = _run_with_conn(lambda cur: _fetch_postings(cur, words))
            now = time.time()
            for w in words:
                _posting_cache.put(w, found.get(w), now)
        except Exception:
            pass                      # keep serving the stale copy; the next query retries
        finally:
            with _inflight_lock:
                _inflight.difference_update(words)
    _spawn(work)


def _get_postings(words, st):
    """{word: (word_id, url_ids, tfs, slot)} for words that have postings. Cached; one round trip
    for the misses; stale entries are served and refreshed in the background."""
    now = time.time()
    out, todo, stale = {}, [], []
    for w in words:
        state, val = _posting_cache.get(w, now, POSTING_TTL, POSTING_HARD_TTL)
        if state == "miss":
            todo.append(w)
            continue
        if state == "stale":
            stale.append(w)
        if val is not None:
            out[w] = val
    if stale:
        _refresh_async(stale)
    st.mark("postings_cache")
    if todo:
        found = _fetch_many(st, todo)
        for w in todo:
            _posting_cache.put(w, found.get(w), now)
        out.update(found)
        st.mark("postings_db")
    return out


def _np_candidates(snap, posts, n_words, k, n_docs, avgdl):
    """BM25 x coverage x popularity over the posting lists in `posts` ({word: (id, urls, tfs)}; urls
    sorted and unique). Work is O(postings), not O(index size). Returns None or a namespace with the
    top-k pages (best first, ties by id): ids, pre (= the old SQL ordering expression; the final score
    also multiplies by the title/host boost), text (BM25), pop, cov, tf (k x words), names, dfs."""
    M = snap.M
    names = sorted(posts)
    score = np.zeros(M, dtype=np.float64)
    seen = np.zeros(M, dtype=np.int8)
    terms = []
    # longest list first: it initialises the accumulators with plain assignment (no gather)
    for n, w in enumerate(sorted(names, key=lambda w: -posts[w][1].size)):
        p = posts[w]
        df = int(p[1].size)
        slot = p[3] if len(p) > 3 else [None]
        hit = slot[0]
        if hit is None or hit[0] is not snap:
            # per-posting BM25 contribution depends only on (word, memindex snapshot): cache it with the list
            urls, tfs = p[1], p[2]
            cut = int(np.searchsorted(urls, M)) if df and urls[-1] >= M else df   # pages newer than the memindex
            u, t = urls[:cut], tfs[:cut]
            tf = t.astype(np.float64)
            dl = np.maximum(snap.tt[u], 1)
            x = (tf * (K1 + 1.0) / (tf + K1 * (1.0 - B + B * dl / avgdl))) * idf(n_docs, df)
            hit = (snap, u, t, x)
            slot[0] = hit
        _, u, t, x = hit
        if n == 0:
            score[u] = x
            seen[u] = 1
        else:
            score[u] += x
            seen[u] += 1
        terms.append((w, u, t, df))
    terms.sort()
    cand = np.flatnonzero(seen)
    cand = cand[snap.valid[cand]]
    if not cand.size:
        return None
    covtab = (np.arange(len(names) + 1) / float(n_words)).clip(max=1.0) ** COVERAGE_POW
    cov = covtab[seen[cand]]
    pop = _pop_vector(snap)[cand]
    text = score[cand]
    pre = text * cov * pop
    if cand.size > k:
        thr = np.partition(pre, cand.size - k)[cand.size - k]
        keep = pre >= thr
        cand, pre, text, pop, cov = cand[keep], pre[keep], text[keep], pop[keep], cov[keep]
    order = np.lexsort((cand, -pre))[:k]                # best first, ties by id
    cand, pre, text, pop, cov = cand[order], pre[order], text[order], pop[order], cov[order]
    tf_cols = []
    for _, u, t, _ in terms:
        if u.size:
            pos = np.minimum(np.searchsorted(u, cand), u.size - 1)
            tf_cols.append(np.where(u[pos] == cand, t[pos], 0))
        else:
            tf_cols.append(np.zeros(cand.size, np.int32))
    return SimpleNamespace(ids=cand, pre=pre, text=text, pop=pop, cov=cov, tf=np.stack(tf_cols, axis=1),
                           names=names, dfs=[df for _, _, _, df in terms])


def _pop_vector(snap):
    """popularity_boost() for every page id, computed once per memindex snapshot."""
    cached = getattr(snap, "pop_cache", None)
    if cached is None or cached[0] != POP_ALPHA:
        cached = (POP_ALPHA, 1.0 + POP_ALPHA * np.log1p(snap.ref.astype(np.float64)))
        snap.pop_cache = cached
    return cached[1]


def _edits1(word):
    """All strings one insert/delete/substitute/adjacent-transposition away (a-z0-9)."""
    letters = "abcdefghijklmnopqrstuvwxyz0123456789"
    splits = [(word[:i], word[i:]) for i in range(len(word) + 1)]
    out = {a + b[1:] for a, b in splits if b}
    out |= {a + b[1] + b[0] + b[2:] for a, b in splits if len(b) > 1}
    out |= {a + c + b[1:] for a, b in splits if b for c in letters}
    out |= {a + c + b for a, b in splits for c in letters}
    out.discard(word)
    return out


def _corrections_fast(missing, st):
    """Typo fixes for words with no postings. Distance-1 fixes come from one indexed probe of the
    ~500 candidate spellings against words.pk (no scan); longer words with no distance-1 fix fall
    back to the old range scan for distance 2."""
    out = {}
    for w in missing:
        lim = max_typo_distance(w)
        if not lim:
            continue
        with _corr_lock:
            hit = _corr_cache.get(w)
        if hit and time.time() - hit[0] < STATS_TTL:
            fix = hit[1]
        else:
            def probe(cur, w=w):
                cur.execute(_VARIANT_SQL, {"cands": sorted(_edits1(w))})
                return [r[0] for r in cur.fetchall()]
            found = _db(st, probe)
            fix = min(found, key=lambda v: (edit_distance(w, v), v)) if found else None
            if fix is None and lim > 1:
                fix = _db(st, lambda cur, w=w: _corrections(cur, [w]).get(w))
            with _corr_lock:
                _corr_cache[w] = (time.time(), fix)
                while len(_corr_cache) > 2048:
                    _corr_cache.popitem(last=False)
        if fix:
            out[w] = fix
    return out


def _boost(idx, snap, words, ids):
    """Vectorised title_boost(): (multiplier, title_frac, host_frac) arrays for page ids."""
    t_hits, h_hits = idx.hits(words, ids)
    n = float(len(words))
    t_frac, h_frac = t_hits / n, h_hits / n
    home = (h_hits > 0) & (snap.depth[ids] <= 1)
    mult = 1.0 + TITLE_BOOST * t_frac + HOST_BOOST * h_frac + HOME_BOOST * home
    return mult, t_frac, h_frac


def _word_dict(idx, snap, src, j, kind):
    i = int(src.ids[j])
    url, title = idx.row(i)
    row = src.tf[j]
    hit = [x for x in range(len(src.names)) if row[x] > 0]
    m_words = [src.names[x] for x in hit]
    d = {
        "url": url, "title": title, "score": float(src.score[j]),
        "bm25": float(src.text[j]), "coverage": float(src.cov[j]), "title_boost": float(src.mult[j]),
        "title_frac": float(src.t_frac[j]), "host_frac": float(src.h_frac[j]),
        "pop_boost": float(src.pop[j]), "reference_count": int(snap.ref[i]),
        "matched_words": m_words, "query_words": list(src.qwords),
        "total_tokens": int(snap.tt[i]), "avgdl": snap.avgdl, "n_docs": snap.n_docs,
        "term_freq": {m_words[a]: int(row[x]) for a, x in enumerate(hit)},
        "doc_freq": {m_words[a]: src.dfs[x] for a, x in enumerate(hit)},
        "kind": kind,
    }
    if src.fixes is not None:
        d["corrections"] = dict(src.fixes)
    return d


def _nav_dict(idx, snap, src, j):
    i = int(src.ids[j])
    url, title = idx.row(i)
    return {
        "url": url, "title": title, "score": float(src.score[j]),
        "bm25": float(src.text[j]), "coverage": float(src.cov[j]), "title_boost": float(src.mult[j]),
        "title_frac": float(src.t_frac[j]), "host_frac": float(src.h_frac[j]),
        "pop_boost": float(src.pop[j]), "reference_count": int(snap.ref[i]),
        "matched_words": [], "query_words": list(src.qwords),
        "total_tokens": int(snap.tt[i]), "avgdl": None, "n_docs": None,
        "term_freq": {}, "doc_freq": {}, "kind": "title",
    }


def _word_source(idx, snap, c, qwords, kind, penalty=1.0, fixes=None):
    mult, t_frac, h_frac = _boost(idx, snap, qwords, c.ids)
    return SimpleNamespace(kind=kind, ids=c.ids, score=c.pre * mult * penalty, text=c.text, cov=c.cov,
                           pop=c.pop, mult=mult, t_frac=t_frac, h_frac=h_frac, tf=c.tf, names=c.names,
                           dfs=c.dfs, qwords=qwords, fixes=fixes, make=_word_dict)


def _nav_source(idx, snap, words, ids, t_hits, h_hits):
    n = float(len(words))
    t_frac, h_frac = t_hits / n, h_hits / n
    home = (h_hits > 0) & (snap.depth[ids] <= 1)
    mult = 1.0 + TITLE_BOOST * t_frac + HOST_BOOST * h_frac + HOME_BOOST * home
    hits = np.maximum(t_frac, h_frac)
    text = NAV_BASE * hits
    pop = 1.0 + POP_ALPHA * np.log1p(snap.ref[ids].astype(np.float64))
    return SimpleNamespace(kind="title", ids=ids, score=text * mult * pop, text=text, cov=hits, pop=pop,
                           mult=mult, t_frac=t_frac, h_frac=h_frac, qwords=words, make=_nav_make)


def _nav_make(idx, snap, src, j, kind):
    return _nav_dict(idx, snap, src, j)


def _mem_search(words, limit, offset, st):
    """Ranked debug dicts for one page; same pipeline as _bm25_search without per-query SQL scans.
    Every source (word matches, title/host matches, typo-corrected matches) is scored as numpy
    vectors; only the pages that make the requested page are turned into result dicts."""
    idx = memindex.get()
    snap = idx.snap
    n_docs, avgdl = snap.n_docs, snap.avgdl
    want = limit + offset
    k = max(RESCORE_K, want)
    sources = []

    posts = _get_postings(words, st)
    unknown = [w for w in words if w not in posts]
    c = _np_candidates(snap, posts, len(words), k, n_docs, avgdl) if posts else None
    if c is not None:
        sources.append(_word_source(idx, snap, c, words, "word"))
    st.mark("score")

    if NAV_MAX_DOCS != 0:
        nav_ids, t_hits, h_hits = idx.nav(words, c.ids if c is not None else (), 100)
        if nav_ids.size:
            src = _nav_source(idx, snap, words, nav_ids, t_hits, h_hits)
            keep = src.cov * len(words) >= max(1, len(words) / 2.0)     # score_nav's acceptance test
            if keep.any():
                for f in ("ids", "score", "text", "cov", "pop", "mult", "t_frac", "h_frac"):
                    setattr(src, f, getattr(src, f)[keep])
                sources.append(src)
    st.mark("nav")

    if unknown and CORRECTIONS:
        fixes = _corrections_fast(unknown, st)
        if fixes:
            fixed = sorted({fixes.get(w, w) for w in words})
            posts2 = _get_postings(fixed, st)
            c2 = _np_candidates(snap, posts2, len(fixed), k, n_docs, avgdl) if posts2 else None
            if c2 is not None:
                sources.append(_word_source(idx, snap, c2, fixed, "corrected", CORRECTION_PENALTY, fixes))
    st.mark("corrections")

    if not sources:
        st.mark("finalize")
        return []
    ids = np.concatenate([s.ids for s in sources])
    scores = np.concatenate([s.score for s in sources])
    src_no = np.concatenate([np.full(s.ids.size, n, dtype=np.int32) for n, s in enumerate(sources)])
    row_no = np.concatenate([np.arange(s.ids.size) for s in sources])
    order = np.argsort(-scores, kind="stable")                  # best first; earlier source wins ties
    _, first = np.unique(ids[order], return_index=True)         # one entry per page: its best score
    best = order[first]
    if best.size > want:
        thr = np.partition(scores[best], best.size - want)[best.size - want]
        best = best[scores[best] >= thr]
    out = []
    for e in best.tolist():
        s = sources[src_no[e]]
        if idx.row(int(ids[e])) is None:
            continue
        out.append(s.make(idx, snap, s, int(row_no[e]), s.kind))
    ranked = rank(out)
    st.mark("finalize")
    return ranked[offset:offset + limit]


def _query_groups(query):
    tokenized = tokenizer.tokenize_all(query)
    parts = [list(tokenized[i]) if tokenized and len(tokenized) > i and tokenized[i] else [] for i in range(4)]
    return [(name, sorted(set(vals))) for name, vals in zip(("word", "bigram", "trigram", "prefix"), parts) if vals]


def _run_with_conn(fn):
    """Run fn(cursor) on a pooled connection; retry once on a stale connection."""
    for attempt in (0, 1):
        conn, pooled = _acquire()
        broken = False
        try:
            with conn.cursor() as cur:
                return fn(cur)
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            if isinstance(e, psycopg2.errors.QueryCanceled):
                raise
            broken = True
            if attempt == 1:
                raise
        finally:
            _release(conn, pooled, broken)


def _legacy_rows_to_dicts(rows):
    return [{"url": u, "title": t, "score": s, "bm25": r, "coverage": None, "title_boost": 1.0,
             "title_frac": 0.0, "host_frac": 0.0, "pop_boost": rs, "reference_count": rc,
             "matched_words": [], "query_words": [], "total_tokens": None, "avgdl": None,
             "n_docs": None, "term_freq": {}, "doc_freq": {}, "kind": "legacy"}
            for u, t, s, r, rs, rc in rows]


def _legacy_search(groups, limit, offset, mode):
    sql_query = _SQL_TEMPLATE.format(
        branches="\n            UNION ALL".join(_BRANCHES[name] for name, _ in groups),
        total_tokens=_TOTAL_TOKENS, ref_score=_REF_SCORE, limit=limit, offset=offset)
    params = tuple(vals for _, vals in groups)

    def run(cur):
        if mode == "exact":
            cur.execute(sql_query, params)
            return cur.fetchall()
        return _fast_search(cur, groups, limit, offset)
    return _run_with_conn(run)


def _resolve_mode(mode):
    mode = (mode or SEARCH_MODE or "bm25").lower()
    return "bm25" if mode not in ("legacy", "exact", "fast") else ("fast" if mode == "legacy" else mode)


def search_debug(query, limit=10, offset=0, mode=None, timings=None):
    """Like search() but returns one dict per result with the score components:
    score, bm25, coverage, title_boost, pop_boost, reference_count, matched_words,
    total_tokens, doc_freq, term_freq, kind ("word" | "fuzzy" | "legacy").

    Per-stage timings (ms: tokenize, result_cache, postings_cache, postings_db, score, nav,
    corrections, finalize, db_ms, round_trips, total_ms) land in the module dict `last_timings`
    (and get_timings()); pass a dict as `timings=` to receive them directly."""
    global last_timings
    st = _Stages()
    limit = max(1, int(limit))
    offset = max(0, int(offset))
    mode = _resolve_mode(mode)
    warmup()
    groups = _query_groups(query)
    st.mark("tokenize")

    def done(res, **extra):
        global last_timings
        d = st.finish()
        d["path"] = "empty"
        d.update(extra)
        last_timings = _tl.timings = d
        if timings is not None:
            timings.update(d)
        return res

    if not groups:
        return done([])

    key = (mode, limit, offset) + tuple((name, tuple(vals)) for name, vals in groups)
    cached = _cache_get(key)
    st.mark("result_cache")
    if cached is not None:
        debug_print("Cache hit for", query)
        return done(cached, path="cache")

    start = time.time()
    path = mode
    if mode == "bm25":
        words = dict(groups).get("word")
        if not words:
            return done([])
        if _fast_ready():
            path = "memory"
            results = _mem_search(words, limit, offset, st)
        else:
            path = "sql"
            results = _run_with_conn(lambda cur: _bm25_search(cur, words, limit, offset))
            st.mark("sql_path")
    else:
        results = _legacy_rows_to_dicts(_legacy_search(groups, limit, offset, mode))
    debug_print("Time taken:", time.time() - start)
    _cache_put(key, results)
    return done(results, path=path)


def search(query, limit=10, offset=0, mode=None):
    """Run search against the Postgres DB.

    Returns a list of (url, title, search_output, relevance, ref_score, reference_count),
    at most `limit` rows starting at row `offset`. In bm25 mode search_output is the final
    score, relevance the BM25 text part and ref_score the popularity multiplier.
    """
    return [(d["url"], d["title"], d["score"], d["bm25"], d["pop_boost"], d["reference_count"])
            for d in search_debug(query, limit, offset, mode)]


# Pre-warm the memory index in the background so the first user is not slow (no-op without DATABASE_URL).
try:
    warmup()
except Exception:  # never let a warm-up problem break an import
    pass
