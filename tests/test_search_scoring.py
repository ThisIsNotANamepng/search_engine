"""Unit tests for the BM25 ranking in search.py. No database needed: the scoring is pure
Python and the SQL layer is exercised with a fake cursor.

    NLTK_DATA=~/nltk_data python -m pytest tests/test_search_scoring.py
"""
import math
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import search  # noqa: E402


# ---------------------------------------------------------------- pure functions

def test_idf_rare_words_weigh_more_and_is_positive():
    assert search.idf(1000, 1) > search.idf(1000, 100) > search.idf(1000, 900)
    assert search.idf(1000, 1000) > 0          # a word on every page still has idf > 0
    assert search.idf(1000, 10) == pytest.approx(math.log(1 + 990.5 / 10.5))


def test_tf_saturates():
    avg = 1000
    one, ten, hundred = (search.tf_saturation(t, 1000, avg) for t in (1, 10, 100))
    assert one < ten < hundred < search.K1 + 1       # bounded by k1+1
    assert hundred - ten < ten - one                 # diminishing returns
    assert search.tf_saturation(1, 1000, avg) == pytest.approx(1.0)   # dl == avgdl -> tf=1 gives 1


def test_length_normalisation():
    short = search.tf_saturation(3, 200, 1000)
    long_ = search.tf_saturation(3, 5000, 1000)
    assert short > long_
    # b=0 turns normalisation off
    assert search.tf_saturation(3, 200, 1000, b=0) == search.tf_saturation(3, 5000, 1000, b=0)


def test_bm25_is_sum_over_words():
    n, avg = 1000, 1000
    both = search.bm25_score([2, 3], [10, 50], 1000, n, avg)
    a = search.bm25_score([2], [10], 1000, n, avg)
    b = search.bm25_score([3], [50], 1000, n, avg)
    assert both == pytest.approx(a + b)


def test_coverage_factor():
    assert search.coverage_factor(2, 2) == 1.0
    assert search.coverage_factor(1, 2, power=1) == 0.5
    assert search.coverage_factor(1, 4) < search.coverage_factor(2, 4) < 1.0
    assert search.coverage_factor(0, 0) == 1.0


def test_popularity_is_log_dampened():
    assert search.popularity_boost(0) == 1.0
    assert search.popularity_boost(None) == 1.0
    p1, p1000 = search.popularity_boost(1), search.popularity_boost(1000)
    assert p1 < p1000 < 3.0                       # 1000x the references is < 3x the score
    assert p1000 == pytest.approx(1 + search.POP_ALPHA * math.log1p(1000))


def test_title_and_host_boost():
    m, t, h = search.title_boost("The Linux Kernel Archives", "https://www.kernel.org", ["linux", "kernel"])
    assert (t, h) == (1.0, 0.5)
    assert m == pytest.approx(1 + search.TITLE_BOOST + 0.5 * search.HOST_BOOST + search.HOME_BOOST)
    m2, t2, h2 = search.title_boost("Some blog", "https://example.com/post/1/x", ["linux"])
    assert (m2, t2, h2) == (1.0, 0.0, 0.0)
    # whole-word match only: "net" must not match "internet"
    assert search.title_boost("Internet history", "https://a.com/x", ["net"])[1] == 0.0
    # non-ascii (CJK bigram) words are substring matched
    assert search.title_boost("东京大学 公式", "https://u-tokyo.ac.jp/a/b/c", ["东京"])[1] == 1.0
    assert search.title_boost(None, None, ["x"]) == (1.0, 0.0, 0.0)


def _row(url, tf, df=10, rc=1, title="t", tt=1000, words=("linux",)):
    return (url, title, tt, rc, list(words), [tf] * len(words), [df] * len(words))


def test_text_match_beats_popularity():
    """A popular page that merely matches once must not outrank a clearly relevant page."""
    kw = dict(words=["linux"], n_docs=1000, avgdl=1000)
    relevant = search.score_candidate(_row("https://rel.org/", tf=8, rc=1), **kw)
    popular = search.score_candidate(_row("https://pop.org/", tf=1, rc=500), **kw)
    assert relevant["score"] > popular["score"]


def test_full_coverage_beats_single_word_hit():
    kw = dict(words=["linux", "kernel"], n_docs=1000, avgdl=1000)
    both = search.score_candidate(_row("https://a/", 1, words=("kernel", "linux")), **kw)
    one = search.score_candidate(_row("https://b/", 5, words=("linux",)), **kw)
    assert both["score"] > one["score"]


def test_debug_dict_fields():
    d = search.score_candidate(_row("https://www.x.org/", 2, df=7, rc=3, title="X linux"),
                               ["linux", "kernel"], 100, 500)
    for key in ("bm25", "coverage", "title_boost", "pop_boost", "matched_words", "total_tokens",
                "doc_freq", "term_freq", "reference_count", "score"):
        assert key in d
    assert d["doc_freq"] == {"linux": 7} and d["term_freq"] == {"linux": 2}
    assert d["score"] == pytest.approx(d["bm25"] * d["coverage"] * d["title_boost"] * d["pop_boost"])


def test_rank_puts_fuzzy_last_and_is_stable():
    mk = lambda u, s, k: {"url": u, "score": s, "kind": k}
    out = search.rank([mk("c", 99, "fuzzy"), mk("b", 1, "word"), mk("a", 1, "word"), mk("d", 5, "title")])
    assert [d["url"] for d in out] == ["d", "a", "b", "c"]


def test_split_drive_probe():
    dfs = {"the": (1, 500000), "linux": (2, 40), "kernel": (3, 90000), "x": (4, 100001)}
    drive, probe = search.split_drive_probe(dfs, heavy=100000)
    assert drive == ["linux", "kernel"] and probe == ["x", "the"]
    # all heavy -> drive on the rarest word only
    drive, probe = search.split_drive_probe({"a": (1, 9), "b": (2, 7)}, heavy=5)
    assert drive == ["b"] and probe == ["a"]


def test_edit_distance_and_correction():
    assert search.edit_distance("kernal", "kernel") == 1
    assert search.edit_distance("pyhton", "python") == 1      # transposition
    assert search.edit_distance("abc", "") == 3
    vocab = ["kernel", "kernels", "colonel", "linux"]
    assert search.pick_correction("kernal", vocab) == "kernel"
    assert search.pick_correction("zzzzzz", vocab) is None
    assert search.pick_correction("go", ["no"]) is None        # too short to correct
    assert search.pick_correction("东京大", ["东京大学"]) is None   # non-ascii never corrected


# ---------------------------------------------------------------- fake cursor

class FakeCursor:
    """Answers the statements search.py issues, based on the SQL text."""

    def __init__(self, word_table, postings_rows, nav_rows=(), fuzzy_rows=(), vocab=(), stats=(100, 1000.0)):
        self.word_table = word_table            # word -> (id, df)
        self.postings_rows = postings_rows
        self.nav_rows, self.fuzzy_rows, self.vocab = nav_rows, fuzzy_rows, vocab
        self.stats = stats
        self.log = []
        self._res = []

    def execute(self, sql, params=None):
        self.log.append((sql, params))
        if "FROM urls" in sql and "count(total_tokens)" in sql:
            self._res = [self.stats]
        elif "FROM weights" in sql:
            self._res = [("trigram", 1.3), ("prefix", 1.2)]
        elif "FROM words w WHERE" in sql:
            self._res = [(w, self.word_table[w][0], self.word_table[w][1])
                         for w in params["words"] if w in self.word_table]
        elif "unnest(" in sql:
            self._res = list(self.postings_rows(params) if callable(self.postings_rows) else self.postings_rows)
        elif "lower(title) LIKE" in sql:
            self._res = list(self.nav_rows)
        elif "FROM trigrams" in sql:
            self._res = list(self.fuzzy_rows)
        elif "FROM words WHERE word >=" in sql:
            self._res = [(v,) for v in self.vocab]
        else:
            raise AssertionError("unexpected SQL: " + sql[:80])

    def fetchall(self):
        return self._res

    def fetchone(self):
        return self._res[0]

    def count(self, needle):
        return sum(1 for s, _ in self.log if needle in s)


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    search._stats.update(ts=0.0, n_docs=None, avgdl=None)
    search._df_cache.clear()
    search._corr_cache.clear()
    search._cache.clear()
    monkeypatch.setattr(search, "NAV_MAX_DOCS", 0)
    yield


def test_bm25_search_orders_by_score_and_returns_page():
    rows = [("https://a.org/", "A", 1000, 1, ["linux"], [1], [20]),
            ("https://b.org/", "B linux", 1000, 1, ["linux"], [6], [20]),
            ("https://c.org/", "C", 1000, 400, ["linux"], [1], [20])]
    cur = FakeCursor({"linux": (7, 20)}, rows)
    out = search._bm25_search(cur, ["linux"], 2, 0)
    assert [d["url"] for d in out][0] == "https://b.org/"
    assert len(out) == 2
    assert out[0]["score"] >= out[1]["score"]
    page2 = search._bm25_search(cur, ["linux"], 2, 2)
    assert len(page2) == 1


def test_stats_and_df_are_cached():
    cur = FakeCursor({"linux": (7, 20)}, [("https://a.org/", "A", 1000, 1, ["linux"], [1], [20])])
    search._bm25_search(cur, ["linux"], 10, 0)
    search._bm25_search(cur, ["linux"], 10, 0)
    assert cur.count("count(total_tokens)") == 1
    assert cur.count("FROM words w WHERE") == 1      # df lookups cached for the 2nd query
    assert cur.count("unnest(") == 2


def test_sql_params_carry_idf_and_literal_ids():
    cur = FakeCursor({"linux": (7, 20), "kernel": (9, 5)}, [])
    search._bm25_search(cur, ["kernel", "linux"], 10, 0)
    sql, params = [e for e in cur.log if "unnest(" in e[0]][0]
    assert params["ids"] == [9, 7] and params["words"] == ["kernel", "linux"]
    assert params["idfs"] == [pytest.approx(search.idf(100, 5)), pytest.approx(search.idf(100, 20))]
    assert params["n_words"] == 2 and params["k"] >= 10
    assert "DROP" not in sql.upper() and "INSERT" not in sql.upper() and "UPDATE" not in sql.upper()


def test_heavy_word_is_probed_not_scanned(monkeypatch):
    monkeypatch.setattr(search, "HEAVY_DF", 100)
    cur = FakeCursor({"the": (1, 5000), "linux": (7, 20)}, [])
    search._bm25_search(cur, ["linux", "the"], 10, 0)
    sql, params = [e for e in cur.log if "unnest(" in e[0]][0]
    assert params["drive"] == [7] and params["probe"] == [1]
    assert "pr AS" in sql


def test_ngram_fallback_only_when_words_missing_and_never_outranks_words(monkeypatch):
    # "linux" matches one page; "kernl" is unknown and has no close vocabulary word.
    monkeypatch.setattr(search, "CORRECTIONS", False)
    monkeypatch.setattr(search, "FUZZY", True)
    word_rows = [("https://w.org/", "W", 1000, 1, ["linux"], [1], [20])]
    fuzzy = [("https://f.org/", "F", 1000, 900, 4, True), ("https://w.org/", "W", 1000, 1, 4, True)]
    cur = FakeCursor({"linux": (7, 20)}, word_rows, fuzzy_rows=fuzzy)
    out = search._bm25_search(cur, ["kernl", "linux"], 10, 0)
    assert [d["url"] for d in out] == ["https://w.org/", "https://f.org/"]   # no duplicate
    assert out[0]["kind"] == "word" and out[1]["kind"] == "fuzzy"
    assert out[1]["score"] < out[0]["score"]
    # all query words known and enough results: no fallback statement at all
    cur2 = FakeCursor({"linux": (7, 20)}, word_rows, fuzzy_rows=fuzzy)
    search._bm25_search(cur2, ["linux"], 1, 0)
    assert cur2.count("FROM trigrams") == 0


def test_fuzzy_page_without_any_word_never_beats_word_page_even_if_popular(monkeypatch):
    monkeypatch.setattr(search, "CORRECTIONS", False)
    monkeypatch.setattr(search, "FUZZY", True)
    word_rows = [("https://w.org/", "W", 1000, 0, ["linux"], [1], [20])]
    fuzzy = [("https://f.org/", "F", 1000, 10 ** 6, 5, True)]
    cur = FakeCursor({"linux": (7, 20)}, word_rows, fuzzy_rows=fuzzy)
    out = search._bm25_search(cur, ["linux", "zzzzq"], 10, 0)
    assert out[0]["url"] == "https://w.org/"


def test_typo_correction_reruns_with_corrected_word():
    def postings(params):
        if "kernel" in params["words"]:
            return [("https://k.org/", "Kernel", 1000, 1, ["kernel"], [3], [5])]
        return []
    cur = FakeCursor({"kernel": (9, 5)}, postings, vocab=["kernel", "colonel"])
    out = search._bm25_search(cur, ["kernal"], 10, 0)
    assert [d["url"] for d in out] == ["https://k.org/"]
    assert out[0]["kind"] == "corrected" and out[0]["corrections"] == {"kernal": "kernel"}
    base = search.score_candidate(("https://k.org/", "Kernel", 1000, 1, ["kernel"], [3], [5]),
                                  ["kernel"], 100, 1000.0)
    assert out[0]["score"] == pytest.approx(base["score"] * search.CORRECTION_PENALTY)


def test_nav_match_finds_title_only_pages(monkeypatch):
    monkeypatch.setattr(search, "NAV_MAX_DOCS", 10 ** 6)
    nav = [("https://en.wikipedia.org/wiki/Web_crawler", "Web crawler - Wikipedia", 900, 1),
           ("https://unrelated.org/", "Nothing here", 900, 5)]
    cur = FakeCursor({"wikipedia": (3, 1)}, [("https://body.org/", "B", 1000, 1, ["wikipedia"], [1], [1])], nav_rows=nav)
    out = search._bm25_search(cur, ["wikipedia"], 10, 0)
    urls = [d["url"] for d in out]
    assert "https://en.wikipedia.org/wiki/Web_crawler" in urls and "https://unrelated.org/" not in urls
    assert [d for d in out if d["url"].startswith("https://en.wiki")][0]["kind"] == "title"


def test_search_returns_frontend_tuple_shape(monkeypatch):
    rows = [("https://a.org/", "A", 1000, 7, ["linux"], [2], [20])]
    cur = FakeCursor({"linux": (7, 20)}, rows)
    monkeypatch.setattr(search, "_run_with_conn", lambda fn: fn(cur))
    res = search.search("linux", 5)
    assert len(res) == 1
    url, title, score, relevance, ref_score, ref_count = res[0]
    assert (url, title, ref_count) == ("https://a.org/", "A", 7)
    assert score == pytest.approx(relevance * ref_score * search.title_boost("A", url, ["linux"])[0])
    assert ref_score == pytest.approx(search.popularity_boost(7))
    # second call is served from the LRU cache
    n = cur.count("unnest(")
    search.search("linux", 5)
    assert cur.count("unnest(") == n
    dbg = search.search_debug("linux", 5)
    assert dbg[0]["matched_words"] == ["linux"] and dbg[0]["doc_freq"] == {"linux": 20}


def test_cache_ttl_and_lru(monkeypatch):
    monkeypatch.setattr(search, "CACHE_SIZE", 2)
    for i in range(3):
        search._cache_put(i, [i])
    assert search._cache_get(0) is None and search._cache_get(2) == [2]
    monkeypatch.setattr(search, "CACHE_TTL", -1)
    assert search._cache_get(2) is None


def test_empty_and_stopword_queries_do_not_touch_db(monkeypatch):
    monkeypatch.setattr(search, "_run_with_conn", lambda fn: pytest.fail("db used"))
    assert search.search("") == []
    assert search.search("   ") == []


def test_mode_resolution():
    assert search._resolve_mode(None) == "bm25"
    assert search._resolve_mode("legacy") == "fast"
    assert search._resolve_mode("exact") == "exact"
    assert search._resolve_mode("nonsense") == "bm25"


def test_fuzzy_is_off_by_default(monkeypatch):
    monkeypatch.setattr(search, "CORRECTIONS", False)
    cur = FakeCursor({"linux": (7, 20)}, [], fuzzy_rows=[("https://f.org/", "F", 1000, 1, 4, True)])
    assert search._bm25_search(cur, ["zzzzqq"], 10, 0) == []
    assert cur.count("FROM trigrams") == 0
