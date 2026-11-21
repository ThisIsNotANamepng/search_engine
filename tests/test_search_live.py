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


@pytest.fixture(autouse=True)
def _nocache(monkeypatch):
    search._cache.clear()


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
