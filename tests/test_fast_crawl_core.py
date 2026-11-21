"""
fast_crawl.py: scheduler, politeness, robots, link selection, end-to-end crawl of a local test server (no database needed),
plus database tests that run only when TEST_DATABASE_URL points at a throwaway database:
    python -m pytest tests/test_fast_crawl_core.py
    TEST_DATABASE_URL=postgresql://user@localhost/stultus_test python -m pytest tests/test_fast_crawl_core.py   (DB tests; need STULTUS_ALLOW_DDL=1 to build the schema)
"""

import asyncio
import os
import sys
import time
from collections import defaultdict

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import utils
import fast_crawl
from crawl_lib.frontier import Frontier
from crawl_lib.robots import RobotsCache

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")


# ---------------------------------------------------------------- frontier

def test_frontier_cap_one_inflight_and_delay():
    f = Frontier(max_per_host=2, min_delay=5.0)
    assert f.add("https://a.example/x/y", 0) and f.add("https://a.example", 0)
    assert not f.add("https://a.example/z", 0), "cap counts what's already queued"
    assert f.add("https://b.example/p", 0)
    st, url = f.pop_ready(0)
    assert url in ("https://a.example", "https://b.example/p")
    got = []
    got_state = f.pop_ready(0)
    got.append(got_state)
    assert f.pop_ready(0) is None, "only two hosts, both in flight"
    f.complete(st, 1.0)
    assert f.pop_ready(3.0) is None, "delay not over"
    if st.key == "a.example":
        st2, url2 = f.pop_ready(6.5)
        assert st2 is st and url2 == "https://a.example/x/y"
        f.complete(st2, 7.0)
        assert f.pop_ready(100) is None, "host reached its cap"


def test_frontier_shallow_first_and_drop_over_cap():
    f = Frontier(max_per_host=1, min_delay=0)
    f.add("https://a.example/deep/er/page", 0)
    st, url = f.pop_ready(0)
    f.complete(st, 0)
    f.hosts["a.example"].urls.append((0, 0, "https://a.example/left"))  # a url that slipped in: dropped at the cap
    f.queued += 1
    f._reschedule(st)
    assert f.drain_dropped() == ["https://a.example/left"] and f.queued == 0

    g = Frontier(max_per_host=5, min_delay=0)
    for u in ("https://a.example/a/b/c", "https://a.example", "https://a.example/a"):
        g.add(u, 0)
    order = []
    for _ in range(3):
        st, u = g.pop_ready(0)
        order.append(u)
        g.complete(st, 0)
    assert order == ["https://a.example", "https://a.example/a", "https://a.example/a/b/c"]


def test_frontier_www_is_same_host():
    f = Frontier(max_per_host=1, min_delay=0)
    assert f.add("https://www.a.example/x", 0)
    assert not f.add("https://a.example/y", 0)


def test_frontier_requeue():
    f = Frontier(max_per_host=3, min_delay=0)
    f.add("https://a.example/x", 0)
    st, url = f.pop_ready(0)
    f.requeue(st, url, 2.0)
    assert f.pop_ready(1.0) is None and f.pop_ready(2.0)[1] == url
    assert st.fetched == 0


# ---------------------------------------------------------------- robots

def test_robots_cache_rules_delay_and_errors():
    calls = []

    async def fetch(robots_url):
        calls.append(robots_url)
        if "bad.example" in robots_url:
            raise OSError("down")
        if "none.example" in robots_url:
            return 404, b""
        return 200, b"User-agent: *\nDisallow: /private\nCrawl-delay: 7\n\nUser-agent: StultusSearchEngine\nDisallow: /no-stultus\nCrawl-delay: 9\n"

    async def run():
        cache = RobotsCache(fetch, utils.USER_AGENT)
        e = await cache.get("https://a.example/page")
        assert e.allowed(utils.USER_AGENT, "https://a.example/private")      # our own group wins over *
        assert not e.allowed(utils.USER_AGENT, "https://a.example/no-stultus")
        assert e.delay == 9.0
        await cache.get("https://a.example/other")
        assert calls.count("https://a.example/robots.txt") == 1, "cached per origin"
        assert (await cache.get("https://none.example/x")).allowed(utils.USER_AGENT, "https://none.example/x")
        bad = await cache.get("https://bad.example/x")
        assert bad.allowed(utils.USER_AGENT, "https://bad.example/x") and cache.errors == 1

    asyncio.run(run())


# ---------------------------------------------------------------- end to end against a local server

TEXT = " ".join(["Crawlers download pages from many different websites and the search engine stores the words that these pages contain."] * 10)


def make_site(port, log):
    from aiohttp import web

    inflight = defaultdict(int)
    max_inflight = defaultdict(int)

    @web.middleware
    async def record(request, handler):
        host = request.host.split(":")[0]
        inflight[host] += 1
        max_inflight[host] = max(max_inflight[host], inflight[host])
        log.append((host, request.path, time.monotonic()))
        try:
            await asyncio.sleep(0.05)
            return await handler(request)
        finally:
            inflight[host] -= 1

    def html(title, body):
        return web.Response(text=f"<html><head><title>{title}</title></head><body><article><h1>{title}</h1><p>{TEXT}</p>{body}</article></body></html>", content_type="text/html")

    async def home(request):
        host = request.host.split(":")[0]
        if host == "127.0.0.5":
            return web.Response(status=429, text="slow down")
        links = "".join(f'<a href="/{p}">{p}</a>' for p in ("a", "b", "c", "d", "e", "private/x", "file.pdf", "redir", "missing"))
        links += "".join(f'<a href="http://127.0.0.{i}:{port}/">h{i}</a>' for i in (2, 3, 4, 5))
        return html(f"Home of {host}", links)

    async def sub(request):
        return html(f"Page {request.path} of {request.host}", "")

    async def robots(request):
        if request.host.startswith("127.0.0.3"):
            return web.Response(text="User-agent: *\nDisallow: /private\n")
        return web.Response(status=404)

    async def pdf(request):
        return web.Response(body=b"%PDF", content_type="application/pdf")

    async def redir(request):
        raise web.HTTPMovedPermanently(f"http://127.0.0.4:{port}/landing")

    async def missing(request):
        return web.Response(status=404, text="<html><body>nothing</body></html>", content_type="text/html")

    app = web.Application(middlewares=[record])
    app.router.add_get("/", home)
    app.router.add_get("/robots.txt", robots)
    app.router.add_get("/file.pdf", pdf)
    app.router.add_get("/redir", redir)
    app.router.add_get("/missing", missing)
    app.router.add_get("/{tail:.*}", sub)
    return app, inflight, max_inflight


def test_end_to_end_local_site(tmp_path):
    dump = tmp_path / "dump.jsonl"
    log = []
    result = {}

    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    (tmp_path / "seeds.txt").write_text(f"http://127.0.0.1:{port}/\n")

    async def run_fixed():
        from aiohttp import web
        app, inflight, max_inflight = make_site(port, log)
        runner = web.AppRunner(app)
        await runner.setup()
        for i in (1, 2, 3, 4, 5):
            await web.TCPSite(runner, f"127.0.0.{i}", port).start()
        args = fast_crawl.parse_args(["--nodb", "--follow", "--seed-file", str(tmp_path / "seeds.txt"), "--max-pages-per-host", "3", "--min-delay", "0.4",
                                      "--parsers", "1", "--fetchers", "16", "--dump", str(dump), "--blocklist", str(tmp_path / "none.db"), "--max-time", "90"])
        crawler = fast_crawl.Crawler(args)

        async def public(host):
            return True
        crawler.host_public = public
        crawler.note = lambda msg: None
        try:
            await crawler.setup()
            await crawler.run()
        finally:
            await crawler.close()
            await runner.cleanup()
        result["max_inflight"] = dict(max_inflight)
        result["stats"] = crawler.stats

    asyncio.run(run_fixed())

    import json
    pages = [json.loads(l) for l in dump.read_text().splitlines()]
    urls_done = {p["url"] for p in pages}
    by_host = defaultdict(list)
    for h, path, t in log:
        by_host[h].append((path, t))
    paths = {(h, p) for h, p, _ in log}

    # politeness: one request in flight per host, delay between requests
    assert max(result["max_inflight"].values()) == 1
    for host, reqs in by_host.items():
        times = [t for p, t in reqs if p != "/robots.txt"]
        for a, b in zip(times, times[1:]):
            assert b - a >= 0.35, f"{host} requested {b - a:.2f}s apart"
    # cap per host (robots.txt requests don't count)
    for host, reqs in by_host.items():
        assert len([p for p, _ in reqs if p != "/robots.txt"]) <= 3, host
    # breadth: all five hosts reached
    assert {h for h, _ in paths} >= {f"127.0.0.{i}" for i in range(1, 6)}
    # robots.txt honored, non-HTML skipped before fetching
    assert not any(p.startswith("/private") and h == "127.0.0.3" for h, p in paths)
    assert not any(p == "/file.pdf" for _, p in paths)
    # cross-host redirect was queued and then crawled as a page of the other host
    assert any(u.startswith("http://127.0.0.4") for u in urls_done)
    # 404 pages are not indexed; 429 host is left alone after one request
    assert not any("/missing" in u for u in urls_done)
    assert len([1 for h, p, _ in log if h == "127.0.0.5" and p != "/robots.txt"]) == 1
    assert result["stats"]["rate_limited"] == 1
    assert all(p["status"] == "indexed" and p["lang"] == "en" for p in pages)


# ---------------------------------------------------------------- database

def test_create_database_never_runs_ddl_without_permission(monkeypatch):
    monkeypatch.delenv("STULTUS_ALLOW_DDL", raising=False)
    monkeypatch.setattr(utils, "missing_tables", lambda: ["words"])
    monkeypatch.setattr(utils, "get_conn", lambda: pytest.fail("touched the database"))
    with pytest.raises(RuntimeError, match="STULTUS_ALLOW_DDL"):
        utils.create_database()


@pytest.fixture
def db(monkeypatch):
    # These tests TRUNCATE and ALTER: only ever against a local throwaway database, never the real index
    from urllib.parse import urlparse
    host = urlparse(TEST_DATABASE_URL).hostname
    if host not in ("localhost", "127.0.0.1", "::1") or TEST_DATABASE_URL == os.getenv("DATABASE_URL"):
        pytest.skip("TEST_DATABASE_URL must be a local throwaway database")
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("STULTUS_ALLOW_DDL", "1")
    monkeypatch.setattr(utils, "NODB", False)  # the end-to-end test (a --nodb crawler) sets it
    utils._QUEUE_HAS_CLAIMS = None
    utils.create_database()
    conn = utils.get_conn()
    cur = conn.cursor()
    cur.execute("TRUNCATE url_queue, urls, urls_references, words, prefixes, word_urls, bigram_urls, trigram_urls, prefix_urls, logs;")
    conn.commit()
    conn.close()
    return utils


def table(utils_mod, query, args=()):
    conn = utils_mod.get_conn()
    cur = conn.cursor()
    cur.execute(query, args)
    rows = cur.fetchall()
    conn.rollback()
    conn.close()
    return rows


def counts_for(text):
    import tokenizer
    w, b, t, p = tokenizer.tokenize_all_counts(text)
    return ({k: n for k, n in w.items() if 0 < len(k) <= utils.MAX_TOKEN_LENGTH}, dict(b), dict(t), {k: n for k, n in p.items() if 0 < len(k) <= utils.MAX_TOKEN_LENGTH})


@needs_db
def test_batch_store_matches_utils_store(db):
    from crawl_lib import db as cdb
    text_a = "Crawlers download many pages and the engine stores the words. Words, words, words about crawlers."
    text_b = "A completely different page about gardening, tomatoes and roses."
    links = ["https://other.example/x", "https://one.example/y"]

    # reference: utils.store on url A
    db.store([text_a, links, "Title A", None], "https://one.example/a")
    ref = table(db, "SELECT id, total_tokens FROM urls WHERE url = 'https://one.example/a'")[0]
    ref_rows = {t: table(db, f"SELECT count(*), sum(count) FROM {t} WHERE url_id = %s", (ref[0],))[0] for t in ("word_urls", "bigram_urls", "trigram_urls", "prefix_urls")}

    writer = cdb.DbWriter()
    writer.connect()
    batch = cdb.Batch()
    batch.add_item({"pages": [{"url": "https://two.example/a", "title": "Title A", "icon": None, "counts": counts_for(text_a)},
                              {"url": "https://two.example/b", "title": "Title B\x00", "icon": "https://two.example/i.ico", "counts": counts_for(text_b)}],
                    "refs_from": "https://two.example/a", "refs": links, "external": links, "enqueue": ["https://q.example/1", "https://q.example/2"],
                    "logs": ["Scraped https://two.example/a"], "seen": ["https://seen.example/x"]})
    stats = writer.write(batch)
    assert stats["pages"] == 2
    rows = table(db, "SELECT id, total_tokens, title FROM urls WHERE url = 'https://two.example/a'")[0]
    assert rows[1] == ref[1], "same total_tokens as utils.store"
    for t, expected in ref_rows.items():
        assert table(db, f"SELECT count(*), sum(count) FROM {t} WHERE url_id = %s", (rows[0],))[0] == expected
    assert table(db, "SELECT title FROM urls WHERE url = 'https://two.example/b'")[0][0] == "Title B"
    assert table(db, "SELECT total_tokens FROM urls WHERE url = 'https://seen.example/x'")[0][0] is None
    assert table(db, "SELECT count(*) FROM urls_references WHERE url = 'https://two.example/a'")[0][0] == 2
    assert sorted(r[0] for r in table(db, "SELECT url FROM url_queue")) == ["https://q.example/1", "https://q.example/2"]
    assert table(db, "SELECT message FROM logs")[0][0] == "Scraped https://two.example/a"

    # re-storing an already indexed page is skipped (no postings duplicated, no url_id scan)
    before = table(db, "SELECT count(*) FROM word_urls")[0][0]
    again = cdb.Batch()
    again.add_item({"pages": [{"url": "https://two.example/a", "title": "Title A", "icon": None, "counts": counts_for(text_a)}]})
    assert writer.write(again)["skipped_stored"] == 1
    assert table(db, "SELECT count(*) FROM word_urls")[0][0] == before

    # a seen-only stub is filled in without any DELETE on the postings tables
    stub = cdb.Batch()
    stub.add_item({"pages": [{"url": "https://seen.example/x", "title": "Was a stub", "icon": None, "counts": counts_for(text_b)}]})
    writer.write(stub)
    row = table(db, "SELECT id, total_tokens, title FROM urls WHERE url = 'https://seen.example/x'")[0]
    assert row[1] and row[2] == "Was a stub"
    assert table(db, "SELECT count(*) FROM word_urls WHERE url_id = %s", (row[0],))[0][0] > 0
    writer.close()


@needs_db
def test_claimless_queue_protocol(db):
    """The production schema: no claimed_at, exhausted id sequence. Simulated on the throwaway database"""
    from crawl_lib import db as cdb
    conn = utils.get_conn()
    cur = conn.cursor()
    cur.execute("ALTER TABLE url_queue DROP COLUMN IF EXISTS claimed_at;")
    cur.execute("SELECT setval('url_queue_id_seq', 2147483647);")  # the next default id is out of range
    conn.commit()
    conn.close()
    utils._QUEUE_HAS_CLAIMS = None
    try:
        assert utils.queue_has_claims() is False
        utils.enqueue_urls(["https://a.example/1", "https://a.example/2", "https://b.example/1"])
        utils.enqueue_urls(["https://a.example/2", "https://c.example/1"])  # dup skipped
        assert [r[0] for r in table(db, "SELECT url FROM url_queue ORDER BY id")] == ["https://a.example/1", "https://a.example/2", "https://b.example/1", "https://c.example/1"]
        popped = utils.get_next_urls(3)
        assert popped == ["https://a.example/1", "https://a.example/2", "https://b.example/1"]
        assert table(db, "SELECT count(*) FROM url_queue")[0][0] == 1
        utils.finish_claims(done=popped[:1], release=popped[1:])
        assert [r[0] for r in table(db, "SELECT url FROM url_queue ORDER BY id")] == ["https://c.example/1", "https://a.example/2", "https://b.example/1"]

        writer = cdb.DbWriter()
        writer.connect()
        assert writer.claims is False
        batch = cdb.Batch()
        batch.add_item({"enqueue": ["https://d.example/1", "https://c.example/1"], "done": ["https://zzz.example"]})
        writer.write(batch)
        assert "https://d.example/1" in [r[0] for r in table(db, "SELECT url FROM url_queue")]
        q = cdb.QueueDb()
        n = q.enqueue(["https://seed.example", "https://d.example/1"])
        claimed = q.claim_urls(["https://seed.example", "https://d.example/1"])
        assert n == 1 and sorted(claimed) == ["https://d.example/1", "https://seed.example"]
        assert not {"https://seed.example", "https://d.example/1"} & {r[0] for r in table(db, "SELECT url FROM url_queue")}
        q.release(claimed)
        assert {"https://seed.example", "https://d.example/1"} <= {r[0] for r in table(db, "SELECT url FROM url_queue")}
        q.close()
        writer.close()
    finally:
        utils._QUEUE_HAS_CLAIMS = None


# ---------------------------------------------------------------- regression: shared connection / hung flush / bounded seed claims

def test_dbwriter_refuses_concurrent_use(monkeypatch):
    """A DbWriter (one psycopg2 connection) used by two threads at once must fail loudly, never interleave statements"""
    import threading
    from crawl_lib import db as cdb
    inside, release = threading.Event(), threading.Event()

    def slow_flush(conn, maps, batch, ip, claims):
        inside.set()
        assert release.wait(10)
        return {"rows": 0}

    class FakeConn:
        closed = 0
        def commit(self): pass
        def rollback(self): pytest.fail("rolled back the writer's transaction from another thread")
        def fileno(self): return -1

    monkeypatch.setattr(cdb, "flush", slow_flush)
    w = cdb.DbWriter("dsn")
    w.conn, w.maps, w.claims = FakeConn(), ({}, {}), False
    out = {}
    t = threading.Thread(target=lambda: out.update(stats=w.write(cdb.Batch())))
    t.start()
    assert inside.wait(10)
    with pytest.raises(RuntimeError, match="two threads"):
        w.write(cdb.Batch())
    release.set()
    t.join(10)
    assert out["stats"] == {"rows": 0}


def test_queue_db_has_its_own_connection_and_never_waits_when_busy():
    import threading
    from crawl_lib import db as cdb
    q = cdb.QueueDb("dsn", claims=False)
    entered, leave = threading.Event(), threading.Event()

    def hold(cur):
        entered.set()
        leave.wait(10)
        return 1
    q._connect = lambda: setattr(q, "conn", type("C", (), {"closed": 0, "cursor": lambda s: type("K", (), {"close": lambda k: None})(), "commit": lambda s: None, "rollback": lambda s: None})())
    t = threading.Thread(target=lambda: q._run(hold))
    t.start()
    assert entered.wait(10)
    assert q.queue_estimate() is None, "the progress estimate must not wait for (or share) a busy connection"
    leave.set()
    t.join(10)


class _FakeDb:
    """db.write blocks; abort() may or may not free it"""
    def __init__(self, hang_first=1, abort_frees=True):
        import threading
        self.dsn, self.maps, self.claims = "dsn", ({}, {}), False
        self.hang = hang_first
        self.abort_frees = abort_frees
        self.calls = 0
        self.aborts = 0
        self.stuck = threading.Event()
        self.written = []

    def write(self, batch):
        from crawl_lib.db import FlushAborted
        self.calls += 1
        if self.hang > 0:
            self.hang -= 1
            self.stuck.wait(30)
            raise FlushAborted("aborted")
        self.written.append(batch)
        return {"rows": 1}

    def abort(self):
        self.aborts += 1
        if self.abort_frees:
            self.stuck.set()


class _FakeCrawler:
    def __init__(self):
        from collections import Counter
        self.args = fast_crawl.parse_args(["--nodb", "-u", "http://x.example/", "--batch-pages", "5"])
        self.args.db_watchdog = 0.5
        self.stats = Counter()
        self.claimed_open = {"http://x.example/"}
        self.notes = []
        self.db = None

    def note(self, msg):
        self.notes.append(msg)


def _run_writer(db, monkeypatch, replace_with=None):
    from crawl_lib import db as cdb
    c = _FakeCrawler()
    c.db = db
    if replace_with is not None:
        monkeypatch.setattr(cdb, "DbWriter", lambda dsn: replace_with)
    async def go():
        w = fast_crawl.AsyncWriter(c, db)
        w.ABORT_GRACE = 0.5
        w.submit({"done": ["http://x.example/"], "logs": ["x"]})
        t0 = time.monotonic()
        await asyncio.wait_for(w.flush_now(), 20)
        return w, time.monotonic() - t0
    w, took = asyncio.run(go())
    return c, w, took


def test_watchdog_aborts_a_hung_flush_and_retries(monkeypatch):
    db = _FakeDb(hang_first=1, abort_frees=True)
    c, w, took = _run_writer(db, monkeypatch)
    assert db.aborts == 1 and len(db.written) == 1, "aborted once, then the same batch was written"
    assert any("WATCHDOG" in n for n in c.notes)
    assert not c.claimed_open, "the retried batch committed: its claim is finished"
    assert took < 10


def test_watchdog_replaces_a_writer_thread_that_ignores_the_abort(monkeypatch):
    stuck = _FakeDb(hang_first=1, abort_frees=False)
    fresh = _FakeDb(hang_first=0)
    c, w, took = _run_writer(stuck, monkeypatch, replace_with=fresh)
    stuck.stuck.set()  # let the abandoned thread end
    assert w.stats["writer_replaced"] == 1 and w.db is fresh and c.db is fresh
    assert len(fresh.written) == 1 and not c.claimed_open


def test_session_options_are_set_on_every_connection():
    from crawl_lib import db as cdb
    assert "lock_timeout=15s" in cdb.SESSION_OPTIONS
    assert "idle_in_transaction_session_timeout=60s" in cdb.SESSION_OPTIONS
    assert "statement_timeout=300s" in cdb.SESSION_OPTIONS
    assert cdb.psycopg2.errors.LockNotAvailable in cdb.RETRYABLE and cdb.psycopg2.errors.QueryCanceled in cdb.RETRYABLE


@needs_db
def test_connections_carry_timeouts(db):
    from crawl_lib import db as cdb
    w = cdb.DbWriter()
    w.connect()
    q = cdb.QueueDb()
    seen = []
    for conn in (w.conn, ):
        cur = conn.cursor()
        cur.execute("SHOW lock_timeout; ")
        seen.append(cur.fetchone()[0])
        cur.execute("SHOW idle_in_transaction_session_timeout;")
        seen.append(cur.fetchone()[0])
        cur.execute("SHOW application_name;")
        seen.append(cur.fetchone()[0])
        conn.rollback()
    assert seen == ["15s", "1min", "fast_crawl"]
    assert q._run(lambda cur: (cur.execute("SHOW statement_timeout;"), cur.fetchone()[0])[1]) == "1min"
    w.close()
    q.close()


@needs_db
def test_queue_estimate_concurrent_with_writes_never_disturbs_the_writer(db):
    """The incident: queue_estimate() ran on the writer's connection from another thread and rolled back its transaction mid-flush.
    Now: it has its own connection. Hammer both at once and check every page lands and nothing errors"""
    import threading
    from crawl_lib import db as cdb
    writer = cdb.DbWriter()
    writer.connect()
    q = cdb.QueueDb(claims=writer.claims)
    assert q._connect() is None
    assert q.conn is not writer.conn
    stop = threading.Event()
    errors, estimates = [], []

    def hammer():
        while not stop.is_set():
            try:
                estimates.append(q.queue_estimate())
                q.claim(5)
            except Exception as e:  # pragma: no cover
                errors.append(e)

    th = [threading.Thread(target=hammer) for _ in range(3)]
    for t in th:
        t.start()
    try:
        for i in range(40):
            batch = cdb.Batch()
            batch.add_item({"pages": [{"url": f"https://conc{i}.example/p{j}", "title": "t", "icon": None,
                                       "counts": counts_for(f"Concurrent page {i} {j} about crawlers and words and databases")} for j in range(3)],
                            "logs": [f"Scraped {i}"], "enqueue": [f"https://next{i}.example/"]})
            writer.write(batch)
    finally:
        stop.set()
        for t in th:
            t.join(20)
    assert not errors, errors
    assert estimates, "the estimate ran"
    assert table(db, "SELECT count(*) FROM urls WHERE url LIKE 'https://conc%%'")[0][0] == 120
    assert table(db, "SELECT count(*) FROM logs WHERE message LIKE 'Scraped %%'")[0][0] == 40
    writer.close()
    q.close()


@needs_db
def test_abort_breaks_a_flush_blocked_on_a_lock(db, monkeypatch):
    """A flush stuck behind someone else's lock: abort() from another thread ends it quickly with FlushAborted and the server drops its transaction"""
    import threading
    from crawl_lib import db as cdb
    monkeypatch.setattr(cdb, "SESSION_OPTIONS", cdb.SESSION_OPTIONS.replace("lock_timeout=15s", "lock_timeout=120s"))
    blocker = utils.get_conn()
    cur = blocker.cursor()
    cur.execute("LOCK TABLE urls IN ACCESS EXCLUSIVE MODE;")
    writer = cdb.DbWriter()
    writer.connect()
    out = {}
    batch = cdb.Batch()
    batch.add_item({"pages": [{"url": "https://blocked.example/", "title": "t", "icon": None, "counts": counts_for("blocked page words")}]})

    def run():
        try:
            writer.write(batch)
        except BaseException as e:
            out["err"] = e
    t = threading.Thread(target=run)
    t.start()
    time.sleep(1.5)
    assert t.is_alive(), "the flush is waiting for the lock"
    t0 = time.monotonic()
    writer.abort()
    t.join(15)
    blocker.rollback()
    blocker.close()
    assert not t.is_alive() and isinstance(out.get("err"), cdb.FlushAborted), out
    assert time.monotonic() - t0 < 10
    assert table(db, "SELECT count(*) FROM urls WHERE url = 'https://blocked.example/'")[0][0] == 0
    writer.close()


@needs_db
def test_seed_claims_are_bounded_and_keep_per_host_caps(db):
    """Seeds are all queued up front but popped claim_batch at a time: a hard kill loses only what was popped.
    The per-host cap still holds across claims"""
    from crawl_lib import db as cdb
    seeds = [f"https://h{i % 20}.example/p{i}" for i in range(100)]  # 20 hosts x 5 urls
    args = fast_crawl.parse_args(["--seed-file", "x", "--claim-batch", "12", "--fetchers", "4", "--max-pages-per-host", "2"])

    async def go():
        crawler = fast_crawl.Crawler(args)
        crawler.note = lambda m: None

        class Writer:
            def __init__(self): self.items = []
            def submit(self, item): self.items.append(item)
        crawler.writer = Writer()
        q = crawler.queue = cdb.QueueDb()
        n = await crawler.queue_call(q.enqueue, seeds)
        assert n == 100
        crawler.seed_pending = list(seeds)
        sizes = []
        remaining = []
        for _ in range(3):
            urls = await crawler.claim_some()
            sizes.append(len(urls))
            for u in urls:
                crawler.add_url(u, claimed=True)
            remaining.append(table(db, "SELECT count(*) FROM url_queue")[0][0])
            crawler.frontier.queued = 0  # as if the fetchers had drained it
        q.close()
        return crawler, sizes, remaining

    crawler, sizes, remaining = asyncio.run(go())
    assert all(0 < s <= 12 for s in sizes), sizes
    if utils.queue_has_claims():
        assert table(db, "SELECT count(*) FROM url_queue WHERE claimed_at IS NULL")[0][0] == 100 - sum(sizes)
    else:
        assert remaining == [100 - sum(sizes[:k + 1]) for k in range(3)], (sizes, remaining)
    assert len(crawler.claimed_open) == sum(sizes), "only the popped urls are held in memory"
    for st in crawler.frontier.hosts.values():
        assert len(st.urls) + st.fetched <= 2, st.key


@needs_db
def test_enqueue_survives_an_exhausted_id_sequence_with_claimed_at(db, monkeypatch):
    """The real database: url_queue has claimed_at (other scrapers added it) AND its id sequence is at the INT limit.
    A default-id insert raises SequenceGeneratorLimitExceeded: enqueue falls back to explicit ids, in the same transaction"""
    from crawl_lib import db as cdb
    conn = utils.get_conn()
    cur = conn.cursor()
    cur.execute("ALTER TABLE url_queue ADD COLUMN IF NOT EXISTS claimed_at timestamp;")  # throwaway database only
    cur.execute("SELECT setval('url_queue_id_seq', 2147483647);")
    conn.commit()
    conn.close()
    utils._QUEUE_HAS_CLAIMS = None
    monkeypatch.setattr(cdb, "_SEQUENCE_EXHAUSTED", False)
    try:
        assert utils.queue_has_claims() is True
        writer = cdb.DbWriter()
        writer.connect()
        batch = cdb.Batch()
        batch.add_item({"enqueue": ["https://e.example/1", "https://e.example/2"], "logs": ["x"]})
        writer.write(batch)
        q = cdb.QueueDb(claims=True)
        assert q.enqueue(["https://e.example/2", "https://e.example/3"]) == 1
        assert sorted(r[0] for r in table(db, "SELECT url FROM url_queue")) == ["https://e.example/1", "https://e.example/2", "https://e.example/3"]
        got = q.claim(2)
        assert len(got) == 2 and table(db, "SELECT count(*) FROM url_queue WHERE claimed_at IS NOT NULL")[0][0] == 2
        q.release(got)
        assert table(db, "SELECT count(*) FROM url_queue WHERE claimed_at IS NOT NULL")[0][0] == 0
        q.close()
        writer.close()
    finally:
        utils._QUEUE_HAS_CLAIMS = None
        conn = utils.get_conn()
        cur = conn.cursor()
        cur.execute("SELECT setval('url_queue_id_seq', 1000, false);")
        conn.commit()
        conn.close()
