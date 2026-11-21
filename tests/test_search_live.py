"""Live, read-only checks of the BM25 search against the real index.
Skipped unless DATABASE_URL is set. Only SELECTs are issued (search.py never writes).

    set -a; . ./.env; set +a; NLTK_DATA=~/nltk_data python -m pytest tests/test_search_live.py
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import search  # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")


def _wait_for_memindex(timeout=180):
    """The memory index loads in a background thread at import; give it time so the checks below run
    on the real (memory) path. If it never loads they run on the SQL path."""
    import time
    import search_memindex
    if not search.FAST or search.np is None:
        return None
    search.warmup()
    t0 = time.time()
    while search_memindex.get() is None and time.time() - t0 < timeout:
        time.sleep(0.2)
    return search_memindex.get()


@pytest.fixture(autouse=True)
def _nocache(monkeypatch):
    import psycopg2
    _wait_for_memindex()
    search._cache.clear()
    orig = search.search_debug

    def guarded(*a, **kw):          # the crawler fleet can make the shared DB too slow: skip, don't fail
        try:
            return orig(*a, **kw)
        except psycopg2.errors.QueryCanceled:
            pytest.skip("database too slow right now (statement timeout)")
    monkeypatch.setattr(search, "search_debug", guarded)


def test_sql_candidate_scores_match_python_formula():
    res = search.search_debug("linux kernel", 10, 0, mode="bm25")
    assert res
    for d in res:
        if d["kind"] != "word":
            continue
        expect = search.bm25_score(list(d["term_freq"].values()), list(d["doc_freq"].values()),
                                   d["total_tokens"], d["n_docs"], d["avgdl"])
        assert d["bm25"] == pytest.approx(expect)


def test_word_matches_come_first_and_each_result_contains_a_query_word():
    res = search.search_debug("linux kernel", 10, 0, mode="bm25")
    kinds = [d["kind"] for d in res]
    assert kinds == sorted(kinds, key=lambda k: k == "fuzzy")
    for d in res:
        if d["kind"] == "word":
            assert set(d["matched_words"]) & {"linux", "kernel"}


def test_scores_are_descending_within_word_matches():
    res = [d for d in search.search_debug("free software", 20, 0, mode="bm25") if d["kind"] != "fuzzy"]
    scores = [d["score"] for d in res]
    assert scores == sorted(scores, reverse=True)


def test_legacy_mode_still_returns_tuples():
    rows = search.search("python", 5, 0, mode="legacy")
    assert rows and len(rows[0]) == 6


def test_pagination_is_consistent():
    full = [d["url"] for d in search.search_debug("software", 6, 0, mode="bm25")]
    p1 = [d["url"] for d in search.search_debug("software", 3, 0, mode="bm25")]
    p2 = [d["url"] for d in search.search_debug("software", 3, 3, mode="bm25")]
    assert p1 + p2 == full


# ---- in-memory path (search_memindex) vs the SQL path --------------------------------------

@pytest.fixture(scope="module")
def memindex_loaded():
    import search_memindex
    if not search.FAST or search.np is None:
        pytest.skip("memory path disabled (SEARCH_FAST=0 or numpy missing)")
    if _wait_for_memindex() is None:
        pytest.skip("memory index did not load: %s" % search_memindex._index.error)
    return search_memindex.get()


def _run(fn, *a, **kw):
    import psycopg2
    try:
        return fn(*a, **kw)
    except psycopg2.errors.QueryCanceled:
        pytest.skip("database too slow right now (statement timeout)")


def test_memory_index_matches_the_database(memindex_loaded):
    def count(cur):
        cur.execute("SELECT count(total_tokens) FROM urls")
        return cur.fetchone()[0]
    n = _run(search._run_with_conn, count)
    assert abs(memindex_loaded.snap.n_docs - n) <= max(50, 0.05 * n)     # the crawler keeps adding pages


@pytest.mark.parametrize("q", ["linux kernel", "python", "free software", "kernal"])
def test_memory_path_agrees_with_sql_path(memindex_loaded, q, monkeypatch):
    t = {}
    mem = _run(search.search_debug, q, 10, 0, mode="bm25", timings=t)
    assert t["path"] == "memory"
    search._cache.clear()
    monkeypatch.setattr(search, "FAST", False)
    sql = _run(search.search_debug, q, 10, 0, mode="bm25", timings=t)
    assert t["path"] == "sql"
    assert mem and sql
    assert mem[0]["url"] == sql[0]["url"]                                  # same #1
    overlap = len({d["url"] for d in mem} & {d["url"] for d in sql}) / len(sql)
    assert overlap >= 0.7                                                  # tolerate pages added in between
    by_url = {d["url"]: d for d in sql}
    for d in mem:                                                          # same scores up to N/avgdl drift
        if d["url"] in by_url and d["kind"] == by_url[d["url"]]["kind"]:
            assert d["score"] == pytest.approx(by_url[d["url"]]["score"], rel=0.02)


def test_timings_breakdown_is_exposed(memindex_loaded):
    search.clear_caches()
    t = {}
    _run(search.search_debug, "linux kernel", 10, 0, mode="bm25", timings=t)
    for key in ("tokenize", "score", "nav", "finalize", "total_ms", "db_ms", "round_trips", "path"):
        assert key in t
    assert t["round_trips"] <= 2 and search.last_timings["total_ms"] == t["total_ms"]
    t2 = {}
    search._cache.clear()
    search.search_debug("linux kernel", 10, 0, mode="bm25", timings=t2)       # postings are cached now
    assert t2["round_trips"] == 0 and t2["total_ms"] < 250


def test_unknown_word_costs_no_database_work_when_cached(memindex_loaded):
    search.clear_caches()
    _run(search.search_debug, "zzzqxj", 5, 0, mode="bm25")
    search._cache.clear()
    t = {}
    assert search.search_debug("zzzqxj", 5, 0, mode="bm25", timings=t) == []
    assert t["round_trips"] == 0 and t["total_ms"] < 10
