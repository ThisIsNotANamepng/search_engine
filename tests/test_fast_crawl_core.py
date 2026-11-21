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
        n, claimed = writer.enqueue_and_claim(["https://seed.example", "https://d.example/1"])
        assert n == 1 and sorted(claimed) == ["https://d.example/1", "https://seed.example"]
        assert not {"https://seed.example", "https://d.example/1"} & {r[0] for r in table(db, "SELECT url FROM url_queue")}
        writer.close()
    finally:
        utils._QUEUE_HAS_CLAIMS = None
