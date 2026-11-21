"""
The batcher (supporting_servers/batcher.py): buffering, retrying and dropping writes, and that writes through it land in postgres like direct ones

The end to end tests need a throwaway database, because they empty the index tables:
    TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/stultus_test python -m pytest tests/test_batcher.py
They're skipped when TEST_DATABASE_URL isn't set (DATABASE_URL is never used, so this can't touch the real index)
"""

import os
import sys
import threading
import time

import psycopg2
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "supporting_servers"))
import batcher

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")

PAGE = {"url": "https://a.example", "title": "a", "icon_link": None, "words": {"python": 2}, "bigrams": {"py": 1},
        "trigrams": {"pyt": 1}, "prefixes": {"py": 1}, "links": ["https://b.example"]}


@pytest.fixture(autouse=True)
def empty_buffers():
    for items in batcher._buffers.values():
        items.clear()
    batcher._flush_now.clear()


def test_takes_only_well_formed_writes():
    assert batcher.add("pages", [PAGE]) == 200
    assert batcher.add("logs", [["1.2.3.4", "Scraped x", time.time()]]) == 200
    assert batcher.add("queue", ["https://c.example"]) == 200
    assert batcher.add("nope", ["x"]) == 400
    assert batcher.add("queue", "https://c.example") == 400  # not a list
    assert batcher.add("queue", [None]) == 400
    assert batcher.add("pages", [{**PAGE, "words": {"python": "2"}}]) == 400
    assert batcher.add("logs", [["1.2.3.4", "no time"]]) == 400
    assert batcher._buffers["pages"] == [PAGE] and batcher._buffers["queue"] == ["https://c.example"]


def test_full_buffer_answers_503(monkeypatch):
    monkeypatch.setitem(batcher.MAX_BUFFERED, "queue", 3)
    assert batcher.add("queue", ["a", "b"]) == 200
    assert batcher.add("queue", ["c", "d"]) == 503
    assert batcher._buffers["queue"] == ["a", "b"]


def test_enough_pages_flushes_early(monkeypatch):
    monkeypatch.setattr(batcher, "BATCH_PAGES", 2)
    batcher.add("pages", [PAGE])
    assert not batcher._flush_now.is_set()
    batcher.add("pages", [PAGE])
    assert batcher._flush_now.is_set()


def test_connection_errors_keep_the_writes_in_order(monkeypatch):
    def down(kind, items):
        batcher.add("queue", ["c"])  # a scraper sending more while the flush is waiting on postgres
        raise psycopg2.OperationalError("server closed the connection")
    monkeypatch.setattr(batcher.utils, "write_now", down)
    batcher.add("queue", ["a", "b"])
    assert batcher.flush() == 3

    written = []
    monkeypatch.setattr(batcher.utils, "write_now", lambda kind, items: written.extend(items))
    assert batcher.flush() == 0
    assert written == ["a", "b", "c"]


def test_a_rejected_item_is_dropped_and_the_rest_written(monkeypatch):
    written = []
    def write_now(kind, items):
        if "bad" in items:
            raise psycopg2.DataError("value too long")
        written.extend(items)
    monkeypatch.setattr(batcher.utils, "write_now", write_now)
    batcher.add("queue", ["a", "bad", "b"])
    assert batcher.flush() == 0
    assert written == ["a", "b"]


def test_connection_error_while_writing_one_at_a_time_keeps_the_rest(monkeypatch):
    written = []
    def write_now(kind, items):
        if "bad" in items:
            raise psycopg2.DataError("value too long")
        if "b" in items:
            raise psycopg2.OperationalError("server closed the connection")
        written.extend(items)
    monkeypatch.setattr(batcher.utils, "write_now", write_now)
    batcher.add("queue", ["a", "bad", "b", "c"])
    assert batcher.flush() == 2
    assert written == ["a"] and batcher._buffers["queue"] == ["b", "c"]


def test_pages_are_written_in_batch_sized_transactions(monkeypatch):
    monkeypatch.setattr(batcher, "BATCH_PAGES", 2)
    calls = []
    def write_now(kind, items):
        if len(calls) == 1:
            calls.append("down")
            raise psycopg2.OperationalError("server closed the connection")
        calls.append([p["url"] for p in items])
    monkeypatch.setattr(batcher.utils, "write_now", write_now)
    batcher.add("pages", [{**PAGE, "url": u} for u in "abcde"])
    assert batcher.flush() == 3  # c, d and e still to write
    assert calls == [["a", "b"], "down"]
    assert batcher.flush() == 0
    assert calls[2:] == [["c", "d"], ["e"]]


# ---- through a real batcher into postgres

@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    utils = batcher.utils
    utils.create_database()
    conn = utils.get_conn()
    conn.cursor().execute("TRUNCATE urls, words, prefixes, urls_references, url_queue, logs, "
                          "word_urls, bigram_urls, trigram_urls, prefix_urls CASCADE;")
    conn.commit()
    conn.close()
    return utils


@pytest.fixture
def through_batcher(db, monkeypatch):
    server = batcher.Server(("127.0.0.1", 0), batcher.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(db, "BATCHER_ADDRESS", f"http://127.0.0.1:{server.server_address[1]}")
    yield db
    server.shutdown()
    server.server_close()


def query(utils, sql):
    conn = utils.get_conn()
    cur = conn.cursor()
    cur.execute(sql)
    rows = cur.fetchall()
    conn.close()
    return rows


def postings(utils):
    rows = []
    for table, token_table, column in (("word_urls", "words", "word"), ("bigram_urls", "bigrams", "bigram"),
                                       ("trigram_urls", "trigrams", "trigram"), ("prefix_urls", "prefixes", "prefix")):
        rows += query(utils, f"SELECT '{table}', u.url, t.{column}, p.count FROM {table} p "
                             f"JOIN urls u ON u.id = p.url_id JOIN {token_table} t ON t.id = p.{column}_id")
    return sorted(rows)


PAGES = [
    (["python gardening tomatoes and peppers", ["https://x.example/a", "https://b.example"], "garden", None], "https://a.example"),
    (["python snakes are not venomous", [], "snakes", None], "https://b.example"),
    (["tomatoes again, python again", [], "garden 2", None], "https://a.example"),  # a re-crawl in the same batch
]


@needs_db
def test_writes_wait_for_the_flush_then_land(through_batcher):
    utils = through_batcher
    utils.log("Scraped https://a.example")
    for content, url in PAGES:
        utils.store(content, url)
    utils.mark_url_seen("https://seen.example")
    utils.enqueue_urls(["https://q1.example", "https://q2.example", "https://q1.example"])
    utils.add_references(["https://b.example", "https://b.example", "https://unknown.example"])

    assert query(utils, "SELECT count(*) FROM urls") == [(0,)]  # still buffered
    assert batcher.flush() == 0

    assert query(utils, "SELECT url, title, reference_count FROM urls ORDER BY url") == [
        ("https://a.example", "garden 2", 1), ("https://b.example", "snakes", 3), ("https://seen.example", None, 1)]
    assert query(utils, "SELECT url FROM url_queue ORDER BY url") == [("https://q1.example",), ("https://q2.example",)]
    (message, age), = query(utils, "SELECT message, extract(epoch FROM now() - ts) FROM logs")
    assert message == "Scraped https://a.example" and 0 <= age < 5
    assert query(utils, "SELECT count(*) FROM urls_references") == [(0,)]  # the last scrape of a.example had no links


@needs_db
def test_batched_pages_store_the_same_postings_as_direct_ones(db, monkeypatch):
    for content, url in PAGES[:2]:
        db.store(content, url)
    direct = postings(db)

    conn = db.get_conn()
    conn.cursor().execute("TRUNCATE urls, word_urls, bigram_urls, trigram_urls, prefix_urls;")
    conn.commit()
    conn.close()

    sent = []
    write = db._write
    monkeypatch.setattr(db, "_write", lambda kind, items: sent.extend(items))
    for content, url in PAGES[:2]:
        db.store(content, url)
    monkeypatch.setattr(db, "_write", write)
    db.write_now("pages", sent)
    assert postings(db) == direct and direct
