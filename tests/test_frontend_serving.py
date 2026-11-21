"""
Frontend serving behavior: caching headers, inlined CSS + self-hosted font, page size cookie,
streamed search page, result cache / coalescing / stale-if-error / concurrency cap, the dashboard's
stale-while-revalidate polling, and the gunicorn config.

No database is needed or touched: DATABASE_URL points at a closed port and search.search_debug is
replaced by a fake.

    python -m pytest tests/test_frontend_serving.py
"""
import importlib.util
import os
import re
import sys
import threading
import time
import zlib

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND = os.path.join(ROOT, "frontend")

os.environ["DATABASE_URL"] = "postgresql://nobody:nothing@127.0.0.1:1/none"   # never reachable
os.environ["STULTUS_DEFER_BACKGROUND"] = "1"      # no dashboard/warm-up threads at import
os.environ["DASHBOARD_PREWARM"] = "0"
os.environ["STULTUS_WARMUP"] = "0"
os.environ.setdefault("NLTK_DATA", os.path.expanduser("~/nltk_data"))
sys.path.insert(0, ROOT)
sys.path.insert(0, FRONTEND)

appmod = pytest.importorskip("app")


def _row(i):
    return {"url": f"https://example{i}.org/p/{i}", "title": f"Title {i}", "score": 10.0 - i / 100,
            "bm25": 5.0, "pop_boost": 1.1, "reference_count": i, "matched_words": ["x"], "kind": "word",
            "title_boost": 1.0, "total_tokens": 100, "doc_freq": {"x": 3}, "term_freq": {"x": 1}}


class FakeSearch:
    """Stands in for search.search_debug. Counts calls; can be slow, blocked or failing."""

    def __init__(self):
        self.calls = 0
        self.delay = 0.0
        self.fail = None
        self.gate = None          # threading.Event the search waits for
        self.lock = threading.Lock()

    def __call__(self, query, limit=10, offset=0, mode=None):
        with self.lock:
            self.calls += 1
        if self.gate is not None:
            assert self.gate.wait(10), "test gate never opened"
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise self.fail
        return [_row(i) for i in range(offset, offset + limit) if i < 35]


@pytest.fixture
def fake(monkeypatch):
    f = FakeSearch()
    monkeypatch.setattr(appmod.search, "search_debug", f)
    appmod._result_cache.clear()
    monkeypatch.setattr(appmod, "get_db", lambda: (_ for _ in ()).throw(RuntimeError("no database in tests")))
    return f


@pytest.fixture
def client():
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def n_results(html):
    return html.count('class="result"')


# ---- pages: font + CSS ---------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/", "/creators", "/search?q=hello"])
def test_no_third_party_font_and_css_is_inlined(client, fake, path):
    html = client.get(path).get_data(as_text=True)
    assert "fonts.googleapis.com" not in html and "fonts.gstatic.com" not in html
    assert 'rel="stylesheet"' not in html, "no render-blocking stylesheet request"
    assert '<link rel="preload" href="/static/fonts/forum-latin.woff2" as="font" type="font/woff2" crossorigin>' in html
    assert "<style>" in html and "font-family: 'Forum'" in html.replace('"', "'") or "font-family:'Forum'" in html


def _squash(css):
    return re.sub(r"\s+", "", re.sub(r"/\*.*?\*/", "", css, flags=re.S))


@pytest.mark.parametrize("path,files", [
    ("/", ["fonts.css", "index.css", "search.css", "settings.css"]),
    ("/search?q=hello", ["fonts.css", "index.css", "search-results.css", "settings.css"]),
    ("/creators", ["fonts.css", "creators.css"]),
])
def test_inlined_css_is_the_stylesheets_unchanged(client, fake, path, files):
    """Same rules, same order: only comments and indentation are removed."""
    html = client.get(path).get_data(as_text=True)
    style = re.search(r"<style>(.*?)</style>", html, flags=re.S).group(1)
    expected = "".join(_squash(open(os.path.join(FRONTEND, "static", "css", f), encoding="utf-8").read()) for f in files)
    assert _squash(style) == expected


def test_font_files_match_the_google_font_faces():
    css = open(os.path.join(FRONTEND, "static", "css", "fonts.css"), encoding="utf-8").read()
    faces = re.findall(r"@font-face\s*{(.*?)}", css, flags=re.S)
    assert len(faces) == 4
    for face in faces:
        assert "font-family: 'Forum'" in face and "font-weight: 400" in face and "font-display: swap" in face
        assert "unicode-range:" in face
        name = re.search(r"url\(/static/fonts/(.*?)\)", face).group(1)
        with open(os.path.join(FRONTEND, "static", "fonts", name), "rb") as f:
            assert f.read(4) == b"wOF2"
    assert "U+0000-00FF" in css   # the latin subset


def test_font_and_versioned_static_are_immutable(client):
    r = client.get("/static/fonts/forum-latin.woff2")
    assert r.status_code == 200
    assert "immutable" in r.headers["Cache-Control"] and "max-age=31536000" in r.headers["Cache-Control"]
    r = client.get("/static/js/settings.js?v=123")
    assert "immutable" in r.headers["Cache-Control"]


def test_static_text_is_gzipped_and_cached_in_memory(client):
    r = client.get("/static/js/settings.js?v=1", headers={"Accept-Encoding": "gzip"})
    assert r.headers["Content-Encoding"] == "gzip" and "Accept-Encoding" in r.headers["Vary"]
    assert b"stultus.settings" in zlib.decompress(r.data, 31)
    plain = client.get("/static/js/settings.js?v=1")
    assert "Content-Encoding" not in plain.headers and "Accept-Encoding" in plain.headers["Vary"]


@pytest.mark.parametrize("path", ["/", "/creators"])
def test_static_pages_have_etag_and_answer_304(client, path):
    r = client.get(path)
    assert r.status_code == 200 and "max-age=60" in r.headers["Cache-Control"] and "stale-while-revalidate" in r.headers["Cache-Control"]
    etag = r.headers["ETag"]
    again = client.get(path, headers={"If-None-Match": etag})
    assert again.status_code == 304 and not again.data


# ---- /search: page size cookie, headers ------------------------------------------------------

def test_cookie_sets_default_page_size_on_first_request(client, fake):
    assert n_results(client.get("/search?q=x").get_data(as_text=True)) == 10
    client.set_cookie("stultus_limit", "20")
    r = client.get("/search?q=x")
    assert n_results(r.get_data(as_text=True)) == 20
    assert fake.calls == 2, "one search per request: no second, redirected search"
    assert n_results(client.get("/search?q=x&limit=10").get_data(as_text=True)) == 10   # explicit wins
    client.set_cookie("stultus_limit", "30")
    assert n_results(client.get("/search?q=x").get_data(as_text=True)) == 30
    for bad in ("7", "abc", "-5", "99999"):
        client.set_cookie("stultus_limit", bad)
        assert n_results(client.get("/search?q=x").get_data(as_text=True)) == 10


def test_pagination_links_keep_the_cookie_page_size(client, fake):
    client.set_cookie("stultus_limit", "20")
    html = client.get("/search?q=x").get_data(as_text=True)
    assert 'id="page-next"' in html and "limit=20" in re.search(r'id="page-next"[^>]*href="([^"]+)"', html).group(1)


def test_search_page_headers(client, fake):
    r = client.get("/search?q=x")
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "private, max-age=30"
    assert "Cookie" in r.headers["Vary"]
    assert "Server-Timing" in r.headers
    assert n_results(r.get_data(as_text=True)) == 10


def test_search_error_is_503_and_never_cached(client, fake):
    fake.fail = RuntimeError("db down")
    r = client.get("/search?q=boom")
    assert r.status_code == 503 and "temporarily unavailable" in r.get_data(as_text=True)
    assert r.headers["Cache-Control"] == "no-store"
    assert client.get("/search?q=").status_code == 302   # blank query still redirects home


def test_api_headers_and_payload(client, fake):
    r = client.get("/api/search?q=x&limit=3")
    assert r.status_code == 200 and r.json["count"] == 3 and r.json["has_more"] is True
    assert r.headers["Cache-Control"] == "public, max-age=60, stale-while-revalidate=300"
    assert client.get("/api/search").status_code == 400
    fake.fail = RuntimeError("down")
    err = client.get("/api/search?q=other")
    assert err.status_code == 503 and err.headers["Cache-Control"] == "no-store"


# ---- streamed search page --------------------------------------------------------------------

def test_slow_search_streams_header_first_and_matches_the_full_page(client, fake, monkeypatch):
    monkeypatch.setattr(appmod, "STREAM_AFTER_SECONDS", 0.02)
    fake.gate = threading.Event()
    resp = client.get("/search?q=slow", buffered=False)
    chunks = iter(resp.response)
    first = next(chunks).decode()
    assert "<!DOCTYPE html>" in first and 'id="search-input"' in first and "forum-latin.woff2" in first
    assert 'id="results-container"' not in first, "header is sent before the search finished"
    fake.gate.set()
    rest = b"".join(chunks).decode()
    assert n_results(rest) == 10 and rest.rstrip().endswith("</html>")
    assert resp.headers["Cache-Control"] == "no-cache"
    streamed = first + rest
    full = client.get("/search?q=slow").get_data(as_text=True)      # now a result-cache hit: one piece
    norm = lambda h: re.sub(r"\s+", " ", re.sub(r"\d+(\.\d+)? ms", "", h))
    assert norm(streamed) == norm(full)


def test_streamed_page_is_gzipped_chunk_by_chunk(client, fake, monkeypatch):
    monkeypatch.setattr(appmod, "STREAM_AFTER_SECONDS", 0.02)
    fake.gate = threading.Event()
    resp = client.get("/search?q=slowgz", buffered=False, headers={"Accept-Encoding": "gzip"})
    assert resp.headers["Content-Encoding"] == "gzip" and "Content-Length" not in resp.headers
    chunks = iter(resp.response)
    d = zlib.decompressobj(31)
    head = d.decompress(next(chunks)).decode()
    assert 'id="search-input"' in head and 'id="results-container"' not in head
    fake.gate.set()
    tail = b"".join(d.decompress(c) for c in chunks).decode()
    assert n_results(tail) == 10 and d.eof


def test_prefetch_waits_for_the_whole_page_and_is_cacheable(client, fake, monkeypatch):
    monkeypatch.setattr(appmod, "STREAM_AFTER_SECONDS", 0.01)
    fake.delay = 0.15
    r = client.get("/search?q=pre", headers={"Sec-Purpose": "prefetch"})
    assert r.headers["Cache-Control"] == "private, max-age=30" and "Content-Length" in r.headers
    assert n_results(r.get_data(as_text=True)) == 10


def test_failing_slow_search_streams_an_error_page(client, fake, monkeypatch):
    monkeypatch.setattr(appmod, "STREAM_AFTER_SECONDS", 0.01)
    fake.delay = 0.1
    fake.fail = RuntimeError("late failure")
    r = client.get("/search?q=late")
    assert "temporarily unavailable" in r.get_data(as_text=True)
    assert r.headers["Cache-Control"] == "no-cache"        # never stored as a fresh page


# ---- search execution layer --------------------------------------------------------------------

def test_result_cache_serves_repeats_without_searching(client, fake):
    for _ in range(3):
        assert client.get("/api/search?q=same").status_code == 200
    assert fake.calls == 1
    client.get("/api/search?q=same&offset=10")
    assert fake.calls == 2     # a different page is a different search


def test_identical_concurrent_searches_run_once(client, fake):
    fake.gate = threading.Event()
    out = []
    ts = [threading.Thread(target=lambda: out.append(client.get("/api/search?q=herd").status_code)) for _ in range(6)]
    [t.start() for t in ts]
    time.sleep(0.3)
    fake.gate.set()
    [t.join(10) for t in ts]
    assert out == [200] * 6 and fake.calls == 1


def test_stale_result_is_served_when_the_search_fails(client, fake, monkeypatch):
    assert client.get("/api/search?q=stale").status_code == 200
    monkeypatch.setattr(appmod, "RESULT_CACHE_SECONDS", 0.0)      # expire the fresh copy
    fake.fail = RuntimeError("postgres down")
    r = client.get("/api/search?q=stale")
    assert r.status_code == 200 and r.json["count"] == 10
    assert client.get("/api/search?q=never-seen").status_code == 503   # nothing to fall back to
    monkeypatch.setattr(appmod, "RESULT_STALE_SECONDS", 0.0)
    assert client.get("/api/search?q=stale").status_code == 503        # too old to serve


def test_concurrent_searches_are_capped_and_overflow_fails_fast(client, fake, monkeypatch):
    monkeypatch.setattr(appmod, "_search_slots", threading.BoundedSemaphore(1))
    monkeypatch.setattr(appmod, "SEARCH_QUEUE_TIMEOUT_SECONDS", 0.2)
    fake.gate = threading.Event()
    first = []
    t = threading.Thread(target=lambda: first.append(client.get("/api/search?q=one").status_code))
    t.start()
    time.sleep(0.2)
    started = time.time()
    assert client.get("/api/search?q=two").status_code == 503          # waited 0.2 s for a slot, then gave up
    assert time.time() - started < 2
    fake.gate.set()
    t.join(10)
    assert first == [200]
    assert fake.calls == 1, "the rejected search never reached the database layer"


def test_search_timings_still_recorded_for_the_dashboard(client, fake):
    before = len(appmod._search_timings)
    client.get("/api/search?q=timed")
    client.get("/api/search?q=timed")   # a cache hit counts too: it is what the visitor experienced
    assert len(appmod._search_timings) == before + 2


# ---- dashboard polling ---------------------------------------------------------------------------

def test_dashboard_live_serves_nodes_and_queue_stale_while_revalidate(client, monkeypatch):
    monkeypatch.setattr(appmod, "_live_cache", {})
    monkeypatch.setattr(appmod, "_service_health", lambda: [])
    calls = {"nodes": 0, "queue": 0}
    gate = threading.Event()

    def nodes():
        calls["nodes"] += 1
        return [{"node_id": "n1", "generation": calls["nodes"]}]

    def queue():
        calls["queue"] += 1
        if calls["queue"] > 1:
            gate.wait(5)               # the refresh is slow, like the real url_queue count
        return {"waiting": calls["queue"], "claimed": 0, "estimated": False, "next": []}

    monkeypatch.setattr(appmod, "_crawler_nodes", nodes)
    monkeypatch.setattr(appmod, "_queue_status", queue)
    monkeypatch.setattr(appmod, "LIVE_NODES_TTL_SECONDS", 0.3)
    monkeypatch.setattr(appmod, "LIVE_QUEUE_TTL_SECONDS", 0.3)

    j = client.get("/dashboard/live").json
    assert j["nodes"][0]["generation"] == 1 and j["queue"]["waiting"] == 1
    client.get("/dashboard/live"); client.get("/dashboard/live")
    assert calls == {"nodes": 1, "queue": 1}, "polls inside the TTL cost no queries"

    time.sleep(0.4)
    started = time.time()
    j = client.get("/dashboard/live").json            # stale: answered at once, refresh runs behind
    assert time.time() - started < 1.0
    assert j["queue"]["waiting"] == 1
    gate.set()
    deadline = time.time() + 5
    while time.time() < deadline and client.get("/dashboard/live").json["queue"]["waiting"] == 1:
        time.sleep(0.05)
    assert client.get("/dashboard/live").json["queue"]["waiting"] == 2
    assert client.get("/dashboard/live").json["nodes"][0]["generation"] >= 2


def test_dashboard_live_reports_database_errors_like_before(client, monkeypatch):
    import psycopg2
    monkeypatch.setattr(appmod, "_live_cache", {})
    monkeypatch.setattr(appmod, "_service_health", lambda: [])
    monkeypatch.setattr(appmod, "_crawler_nodes", lambda: (_ for _ in ()).throw(psycopg2.OperationalError("down")))
    monkeypatch.setattr(appmod, "_queue_status", lambda: (_ for _ in ()).throw(psycopg2.OperationalError("down")))
    j = client.get("/dashboard/live").json
    assert j["nodes"] == [] and "down" in j["nodes_error"] and j["queue"] is None and "down" in j["queue_error"]


# ---- startup + gunicorn ----------------------------------------------------------------------------

def test_warm_up_uses_search_warm_up_when_it_exists(monkeypatch):
    called = []
    monkeypatch.setattr(appmod.search, "warm_up", lambda: called.append(1), raising=False)
    appmod._warm_search()
    assert called == [1]


def test_warm_up_falls_back_to_one_query_and_never_raises(monkeypatch, fake):
    monkeypatch.delattr(appmod.search, "warm_up", raising=False)
    appmod._warm_search()
    assert fake.calls == 1
    fake.fail = RuntimeError("db down")
    appmod._warm_search()          # logged, not raised


def test_background_tasks_start_once_and_respect_the_switches(monkeypatch):
    started = []
    monkeypatch.setattr(appmod, "_background_started", False, raising=False)
    monkeypatch.setattr(appmod, "get_dashboard_stats_nowait", lambda: started.append("dashboard"))
    monkeypatch.setattr(appmod, "_warm_search", lambda: started.append("warm"))
    monkeypatch.setenv("DASHBOARD_PREWARM", "1")
    monkeypatch.setenv("STULTUS_WARMUP", "1")
    appmod.start_background_tasks()
    appmod.start_background_tasks()
    time.sleep(0.1)
    assert sorted(started) == ["dashboard", "warm"]


def test_gunicorn_config_is_sane(monkeypatch):
    monkeypatch.delenv("STULTUS_DEFER_BACKGROUND", raising=False)
    spec = importlib.util.spec_from_file_location("gunicorn_conf_under_test", os.path.join(ROOT, "gunicorn.conf.py"))
    conf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(conf)
    assert conf.worker_class == "gthread" and conf.workers >= 1 and conf.threads >= 4
    assert conf.keepalive >= 5 and conf.timeout >= 60
    assert os.environ["STULTUS_DEFER_BACKGROUND"] == "1"     # workers start the background tasks themselves
    started = []

    class W:   # what gunicorn passes to post_worker_init
        wsgi = appmod.app

    monkeypatch.setattr(appmod, "start_background_tasks", lambda: started.append(1))
    conf.post_worker_init(W)
    assert started == [1]
