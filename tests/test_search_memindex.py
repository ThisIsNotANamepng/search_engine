"""Tests for the in-memory search path (search_memindex.py + search._mem_search). No database:
a fake `run(fn)` / fake cursor stands in for Postgres.

    NLTK_DATA=~/nltk_data python -m pytest tests/test_search_memindex.py
"""
import math
import os
import random
import re
import struct
import sys
import time

import pytest

np = pytest.importorskip("numpy")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import search  # noqa: E402
import search_memindex as mi  # noqa: E402


# ------------------------------------------------------------------ fixtures

URLS = {
    1: ("https://python.org/", "Welcome to Python.org", 500, 900),
    2: ("https://docs.python.org/3/tutorial/", "The Python Tutorial", 3000, 40),
    3: ("https://example.com/linux/kernel", "Linux kernel notes", 800, 5),
    4: ("https://kernel.org/", "The Linux Kernel Archives", 1200, 300),
    5: ("https://example.org/pets", "All about snakes", 400, 0),
    7: ("https://news.example.net/a", "Python and the Linux kernel", 100, 2),
}


class FakeCursor:
    """Answers the three SELECT shapes the memory index / fast path issue."""

    def __init__(self, db):
        self.db, self.rows, self.log = db, [], db.log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.log.append(" ".join(sql.split())[:90])
        if "FROM urls WHERE total_tokens" in sql:
            self.rows = [(i, v[2], v[3]) for i, v in self.db.urls.items()]
        elif "FROM urls WHERE id = ANY" in sql:
            self.rows = [(i, self.db.urls[i][0], self.db.urls[i][1]) for i in params[0] if i in self.db.urls]
        elif "string_agg" in sql:
            out = []
            for w in params["words"]:
                post = self.db.postings.get(w)
                if post:
                    ids = sorted(post)
                    out.append((w, 100 + len(out), struct.pack(">%di" % len(ids), *ids),
                                struct.pack(">%di" % len(ids), *[post[i] for i in ids])))
            self.rows = out
        elif "EXISTS" in sql:
            self.rows = [(w,) for w in params["cands"] if w in self.db.postings]
        else:
            raise AssertionError(sql)

    def fetchall(self):
        return self.rows


class FakeDB:
    def __init__(self):
        self.urls = dict(URLS)
        self.postings = {
            "python": {1: 10, 2: 6, 7: 1},
            "linux": {3: 4, 4: 8, 7: 1},
            "kernel": {3: 5, 4: 9, 7: 1},
            "snake": {5: 2},
        }
        self.log = []

    def run(self, fn):
        return fn(FakeCursor(self))


@pytest.fixture
def index(monkeypatch):
    db = FakeDB()
    idx = mi.reset()
    idx.refresh(db.run)
    monkeypatch.setattr(search, "_run_with_conn", db.run)
    monkeypatch.setattr(search, "FUZZY", False)
    monkeypatch.setattr(search, "np", np)
    monkeypatch.setattr(search, "_started_pid", os.getpid())      # no real background loader
    monkeypatch.setattr(search, "FETCH_THREADS", 1)               # one statement per query: deterministic counts
    search.clear_caches()
    yield idx, db
    mi.reset()
    search.clear_caches()


# ------------------------------------------------------------------ memindex

def test_refresh_builds_numeric_arrays(index):
    idx, _ = index
    s = idx.snap
    assert s.n_docs == len(URLS) and s.M == 8
    assert list(np.flatnonzero(s.valid)) == sorted(URLS)
    assert s.tt[4] == 1200 and s.ref[1] == 900
    assert s.avgdl == pytest.approx(sum(v[2] for v in URLS.values()) / len(URLS))


def test_incremental_refresh_only_loads_unseen_strings(index):
    idx, db = index
    db.log.clear()
    idx.refresh(db.run)
    assert not any("id = ANY" in s for s in db.log)       # nothing new -> no string query
    db.urls[9] = ("https://new.example/linux", "Brand new linux page", 50, 1)
    db.log.clear()
    idx.refresh(db.run)
    assert sum("id = ANY" in s for s in db.log) == 1
    assert idx.snap.valid[9] and idx.row(9)[1] == "Brand new linux page"
    assert 9 in idx.nav_candidates(["linux"], k=100)


def test_token_extraction_matches_title_and_host_boost_semantics():
    words = ["python", "kernel", "linux", "org", "tutorial", "3"]
    rnd = random.Random(1)
    pieces = ["Python", "the", "LINUX", "kernel's", "café", "C++", "3.11", "x-kernel", "tutorial!", "org"]
    for _ in range(300):
        title = " ".join(rnd.choice(pieces) for _ in range(rnd.randint(0, 6)))
        url = "https://%s.%s/%s" % (rnd.choice(["docs", "python", "kernel", "a-b"]), rnd.choice(["org", "com"]),
                                   rnd.choice(["", "x", "x/y"]))
        for w in words:
            assert (w in mi.title_tokens(title)) == search._word_in_text(w, title.lower()), (title, w)
            host, _ = search._host_and_depth(url)
            ref_tokens = set(re.split(r"[^0-9a-z\u0080-\uffff]+", host)) - {""}
            assert (w in mi.host_tokens(url)) == (w in ref_tokens), (url, w)


def test_title_boost_unchanged_by_tokenised_fast_path():
    def old(title, url, words):
        title_l = (title or "").lower()
        t_hits = sum(1 for w in words if search._word_in_text(w, title_l))
        host, depth = search._host_and_depth(url)
        ht = set(re.split(r"[^0-9a-z\u0080-\uffff]+", host)) - {""}
        h_hits = sum(1 for w in words if w in ht)
        mult = 1.0 + search.TITLE_BOOST * t_hits / len(words) + search.HOST_BOOST * h_hits / len(words)
        if h_hits and depth <= 1:
            mult += search.HOME_BOOST
        return mult, t_hits / len(words), h_hits / len(words)
    rnd = random.Random(2)
    vocab = ["python", "linux", "kernel", "东京", "node", "js", "c", "the"]
    for _ in range(300):
        title = " ".join(rnd.choice(vocab + ["PYTHON", "Kernel."]) for _ in range(rnd.randint(0, 5)))
        url = "https://%s.org/%s" % (rnd.choice(vocab), rnd.choice(["", "a", "a/b"]))
        words = rnd.sample(vocab, rnd.randint(1, 3))
        assert search.title_boost(title, url, words) == old(title, url, words)


def test_nav_candidates_need_half_the_words_in_title_or_host(index):
    idx, _ = index
    # both words in the title (3, 4, 7); order = most referenced first
    assert idx.nav_candidates(["linux", "kernel"], k=10) == [4, 3, 7]
    # one of two words is enough ("half")
    assert set(idx.nav_candidates(["python", "zzz"], k=10)) == {1, 2, 7}
    # host match (kernel.org) with a word that is not in the title
    assert 4 in idx.nav_candidates(["kernel"], k=10)
    # exclusion and k
    assert idx.nav_candidates(["linux", "kernel"], exclude={4}, k=10) == [3, 7]
    assert idx.nav_candidates(["linux", "kernel"], k=1) == [4]
    assert idx.nav_candidates(["nomatchword"], k=10) == []


def test_nav_candidates_non_ascii_words_use_substring_semantics(index):
    idx, db = index
    db.urls[11] = ("https://jp.example/", "東京大学のページ", 10, 3)
    db.urls[12] = ("https://jp2.example/", "大学 東京 京大", 10, 9)
    db.urls[13] = ("https://en.example/", "Tokyo University", 10, 1)
    idx.refresh(db.run)
    assert idx.nav_candidates(["東京"], k=5) == [12, 11]
    assert set(idx.nav_candidates(["京大"], k=5)) == {11, 12}         # inside "東京大学" and as a word
    assert idx.nav_candidates(["北京"], k=5) == []
    ids = idx._slow_title_ids("大学")
    assert sorted(ids.tolist()) == [11, 12]                            # one entry per page, offsets mapped back
    # same answer as the per-title python check
    for w in ("東京", "京大", "大学", "ページ"):
        expect = [i for i, t in enumerate(idx.titles) if t and mi.word_in_text(w, t.lower())]
        assert sorted(idx._slow_title_ids(w).tolist()) == expect


def test_start_loads_in_background_and_get_returns_none_until_ready():
    mi.reset()
    assert mi.get() is None
    db = FakeDB()
    assert mi.start(db.run, interval=3600)
    for _ in range(100):
        if mi.get() is not None:
            break
        time.sleep(0.02)
    assert mi.get() is not None and mi.get().snap.n_docs == len(URLS)
    mi.reset()


# ------------------------------------------------------------------ scoring

def test_np_candidates_match_python_bm25(index):
    idx, db = index
    snap = idx.snap
    words = ["kernel", "linux"]
    posts = {}
    for w in words:
        ids = np.array(sorted(db.postings[w]), dtype=np.int32)
        posts[w] = (1, ids, np.array([db.postings[w][i] for i in ids], dtype=np.int32))
    got = search._np_candidates(snap, posts, 2, 10, snap.n_docs, snap.avgdl)
    assert set(got.ids.tolist()) == {3, 4, 7}
    for j, i in enumerate(got.ids.tolist()):
        tfs = [int(x) for x in got.tf[j] if x > 0]
        expect = search.bm25_score(tfs, got.dfs, snap.tt[i], snap.n_docs, snap.avgdl)
        assert got.text[j] == pytest.approx(expect)
        assert got.cov[j] == pytest.approx(search.coverage_factor(len(tfs), 2))
        assert got.pop[j] == pytest.approx(search.popularity_boost(int(snap.ref[i])))
        assert got.pre[j] == pytest.approx(got.text[j] * got.cov[j] * got.pop[j])
    assert got.names == ["kernel", "linux"] and got.dfs == [3, 3]
    assert list(got.pre) == sorted(got.pre, reverse=True)


def test_np_candidates_top_k_and_unknown_page_ids(index):
    idx, _ = index
    snap = idx.snap
    ids = np.array([1, 2, 3, 4, 7, 99999], dtype=np.int32)       # 99999: newer than the memindex
    posts = {"python": (1, ids, np.array([1, 2, 3, 4, 5, 6], dtype=np.int32))}
    got = search._np_candidates(snap, posts, 1, 2, snap.n_docs, snap.avgdl)
    assert got.ids.size == 2 and 99999 not in got.ids.tolist()
    assert got.text[0] == pytest.approx(
        search.bm25_score([int(got.tf[0][0])], [6], snap.tt[got.ids[0]], snap.n_docs, snap.avgdl))


def test_decode_postings_sorts_and_dedupes():
    import struct as st
    u = st.pack(">4i", 5, 2, 5, 9)
    t = st.pack(">4i", 1, 2, 3, 4)
    uu, tt = search._decode_postings(u, t)
    assert uu.tolist() == [2, 5, 9] and tt.tolist() == [2, 1, 4]


def test_vector_boost_matches_python_title_boost(index):
    idx, db = index
    snap = idx.snap
    ids = np.array(sorted(URLS), dtype=np.int64)
    for words in (["python"], ["linux", "kernel"], ["python", "org", "tutorial"], ["kernel", "nothing"]):
        mult, t_frac, h_frac = search._boost(idx, snap, words, ids)
        for j, i in enumerate(ids.tolist()):
            url, title = idx.row(i)
            em, et, eh = search.title_boost(title, url, words)
            assert mult[j] == pytest.approx(em) and t_frac[j] == pytest.approx(et) and h_frac[j] == pytest.approx(eh)


def test_edits1_covers_all_four_edit_types():
    e = search._edits1("kernal")
    assert {"kernel", "kernl", "kernall", "kenral", "krnal"} <= e
    assert "kernal" not in e


def test_posting_cache_budget_ttl_and_negative_entries(monkeypatch):
    monkeypatch.setattr(search, "POSTING_CACHE_MB", 0.001)       # ~1 KB
    c = search._PostingCache()
    big = (1, np.zeros(1000, np.int32), np.zeros(1000, np.int32))
    c.put("big", big, time.time())
    assert c.get("big", time.time(), 60)[0] == "miss"            # larger than the budget: not stored
    small = (1, np.zeros(2, np.int32), np.zeros(2, np.int32))
    c.put("a", small, time.time())
    c.put("none", None, time.time())
    assert c.get("a", time.time(), 60)[0] == "fresh" and c.get("none", time.time(), 60) == ("fresh", None)
    assert c.get("a", time.time() + 100, 60)[0] == "miss"        # expired (no hard ttl given)
    c.put("a", small, time.time())
    assert c.get("a", time.time() + 100, 60, 1000)[0] == "stale"  # past ttl, before hard ttl
    assert c.get("a", time.time() + 2000, 60, 1000)[0] == "miss"
    assert c.bytes >= 0
    c.clear()
    assert len(c) == 0 and c.bytes == 0


def test_stale_postings_are_served_and_refreshed_in_the_background(index, monkeypatch):
    idx, db = index
    search.search_debug("python", 5, 0, mode="bm25")
    # age the cached entry past the fresh ttl but not the hard ttl
    wid_entry = search._posting_cache.d["python"]
    search._posting_cache.d["python"] = (wid_entry[0] - search.POSTING_TTL - 5, wid_entry[1])
    spawned = []
    monkeypatch.setattr(search, "_spawn", lambda fn: spawned.append(fn))
    db.postings["python"] = {1: 10, 2: 6, 7: 1, 3: 2}             # a new page appeared
    search._cache.clear()
    t = {}
    res = search.search_debug("python", 10, 0, mode="bm25", timings=t)
    assert t["round_trips"] == 0 and len(spawned) == 1            # answered from the stale list
    assert "https://example.com/linux/kernel" not in {d["url"] for d in res if d["kind"] == "word"}
    spawned[0]()                                                  # run the background refresh
    assert search._posting_cache.get("python", time.time(), search.POSTING_TTL)[0] == "fresh"
    search._cache.clear()
    res = search.search_debug("python", 10, 0, mode="bm25")
    assert "https://example.com/linux/kernel" in {d["url"] for d in res if d["kind"] == "word"}
    assert not search._inflight


def test_cold_words_can_be_fetched_concurrently(index, monkeypatch):
    idx, db = index
    one = [d["url"] for d in search.search_debug("python linux kernel", 6, 0, mode="bm25")]
    search.clear_caches()
    monkeypatch.setattr(search, "FETCH_THREADS", 3)
    t = {}
    par = [d["url"] for d in search.search_debug("python linux kernel", 6, 0, mode="bm25", timings=t)]
    assert par == one and t["round_trips"] == 3                    # one statement per word, same ranking
    search._cache.clear()
    t = {}
    search.search_debug("python linux kernel", 6, 0, mode="bm25", timings=t)
    assert t["round_trips"] == 0                                   # and all three lists are cached afterwards


def test_concurrent_fetch_failure_propagates(index, monkeypatch):
    idx, db = index
    monkeypatch.setattr(search, "FETCH_THREADS", 2)

    def boom(fn):
        raise RuntimeError("db down")
    monkeypatch.setattr(search, "_run_with_conn", boom)
    search.clear_caches()
    with pytest.raises(RuntimeError):
        search.search_debug("python linux", 5, 0, mode="bm25")


def test_prewarm_fetches_common_title_words_once(index, monkeypatch):
    idx, db = index
    monkeypatch.setattr(search, "PREWARM_WORDS", 6)
    db.log.clear()
    search._prewarm()
    assert any("string_agg" in s for s in db.log)
    state, val = search._posting_cache.get("python", time.time(), search.POSTING_TTL)
    assert state == "fresh" and val is not None
    n = len(db.log)
    search._prewarm()                                             # everything cached: no more queries
    assert len(db.log) == n


# ------------------------------------------------------------------ end to end (fake DB)

def test_mem_search_ranks_and_reports_timings(index):
    idx, db = index
    t = {}
    res = search.search_debug("linux kernel", 10, 0, mode="bm25", timings=t)
    assert t["path"] == "memory" or search._fast_ready()
    urls = [d["url"] for d in res]
    assert urls[0] in ("https://kernel.org/", "https://example.com/linux/kernel")
    assert set(urls) >= {"https://kernel.org/", "https://example.com/linux/kernel",
                         "https://news.example.net/a"}
    top = res[0]
    assert top["matched_words"] == ["kernel", "linux"] and top["doc_freq"] == {"kernel": 3, "linux": 3}
    assert top["score"] == pytest.approx(top["bm25"] * top["coverage"] * top["title_boost"] * top["pop_boost"])
    assert t["round_trips"] == 1 and t["db_ms"] >= 0 and "total_ms" in t
    assert search.last_timings["total_ms"] == t["total_ms"]
    # second identical query: served from the result cache with no round trip
    t2 = {}
    search.search_debug("linux kernel", 10, 0, mode="bm25", timings=t2)
    assert t2["path"] == "cache" and t2["round_trips"] == 0


def test_mem_search_warm_postings_need_no_round_trip(index):
    idx, db = index
    search.search_debug("python", 5, 0, mode="bm25")
    search._cache.clear()                                         # result cache only
    t = {}
    search.search_debug("python", 5, 0, mode="bm25", timings=t)
    assert t["round_trips"] == 0 and t["path"] == "memory"
    search.clear_caches()
    t = {}
    search.search_debug("python", 5, 0, mode="bm25", timings=t)
    assert t["round_trips"] == 1


def test_mem_search_nav_page_without_body_postings_is_found(index):
    idx, db = index
    # page 5 has the word only in its URL host/title, not as a posting for "pets"
    res = search.search_debug("pets", 5, 0, mode="bm25")
    assert res == [] or all(d["kind"] != "word" for d in res)
    db.urls[8] = ("https://pets.example/", "Pets of the world", 60, 10)
    idx.refresh(db.run)
    search._cache.clear()
    res = search.search_debug("pets", 5, 0, mode="bm25")
    assert res and res[0]["url"] == "https://pets.example/" and res[0]["kind"] == "title"


def test_mem_search_typo_correction_one_probe(index):
    idx, db = index
    t = {}
    res = search.search_debug("kernal", 5, 0, mode="bm25", timings=t)
    assert res and res[0]["kind"] == "corrected" and res[0]["corrections"] == {"kernal": "kernel"}
    assert t["round_trips"] == 3           # unknown word, variant probe, postings of the fix
    search._cache.clear()
    t = {}
    search.search_debug("kernal", 5, 0, mode="bm25", timings=t)
    assert t["round_trips"] == 0           # negative posting entry + correction + postings all cached


def test_mem_search_pruning_gives_the_same_page_as_full_scoring(index, monkeypatch):
    idx, db = index
    full = [d["url"] for d in search.search_debug("python linux kernel", 6, 0, mode="bm25")]
    search._cache.clear()
    page1 = [d["url"] for d in search.search_debug("python linux kernel", 2, 0, mode="bm25")]
    search._cache.clear()
    page2 = [d["url"] for d in search.search_debug("python linux kernel", 2, 2, mode="bm25")]
    assert page1 + page2 == full[:4]


def test_falls_back_to_sql_path_until_memindex_is_loaded(monkeypatch):
    mi.reset()
    monkeypatch.setattr(search, "_started_pid", os.getpid())
    monkeypatch.setattr(search, "_run_with_conn", lambda fn: ["sql"])
    monkeypatch.setattr(search, "_bm25_search", lambda cur, words, limit, offset: [])
    search.clear_caches()
    t = {}
    search.search_debug("python", 5, 0, mode="bm25", timings=t)
    assert t["path"] == "sql"
    search.clear_caches()
