"""
Stores pages into a real Postgres and searches them

Needs a throwaway database, because it empties the index tables:
    TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/stultus_test python -m pytest tests/
Skipped when TEST_DATABASE_URL isn't set (DATABASE_URL is never used, so this can't touch the real index)
"""

import os
import random
import sys
import threading

import psycopg2
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")

POSTING_TABLES = ("word_urls", "bigram_urls", "trigram_urls", "prefix_urls")


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    import utils
    utils.create_database()
    conn = utils.get_conn()
    cur = conn.cursor()
    cur.execute("TRUNCATE urls, words, prefixes, " + ", ".join(POSTING_TABLES) + " CASCADE;")
    conn.commit()
    conn.close()
    return utils


def filler(n, seed):
    # Words without any of the letters in "python", so they can't share its bigrams/trigrams/prefixes
    rnd = random.Random(seed)
    return " ".join("".join(rnd.choice("abcdefgijklmqrsuvwxz") for _ in range(7)) for _ in range(n))


def posting_counts(utils):
    conn = utils.get_conn()
    cur = conn.cursor()
    counts = []
    for table in POSTING_TABLES:
        cur.execute(f"SELECT count(*) FROM {table};")
        counts.append(cur.fetchone()[0])
    conn.close()
    return counts


def test_storing_a_page_again_does_not_duplicate_its_postings(db):
    page = ["python gardening tomatoes " + filler(30, 1), [], "page", None]
    db.store(page, "https://a.example")
    once = posting_counts(db)
    db.store(page, "https://a.example")
    assert posting_counts(db) == once


def page_item(utils, monkeypatch, content, url):
    """The item store() would send to the batcher for this page"""
    sent = []
    monkeypatch.setattr(utils, "_write", lambda kind, items: sent.extend(items))
    utils.store(content, url)
    monkeypatch.undo()
    return sent[0]


class PausedInCopy:
    """A cursor whose first COPY waits for release, to hold a write_pages transaction open mid-postings like a slow disk does"""
    def __init__(self, cur):
        self._cur, self.copying, self.release = cur, threading.Event(), threading.Event()

    def copy_expert(self, *args, **kwargs):
        self.copying.set()
        assert self.release.wait(30)
        return self._cur.copy_expert(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._cur, name)


def write_pages_in_thread(utils, page, cur_wrapper=lambda cur: cur):
    conn = psycopg2.connect(TEST_DATABASE_URL)
    cur = cur_wrapper(conn.cursor())
    def run():
        utils.write_pages(cur, [page])
        conn.commit()
        conn.close()
    thread = threading.Thread(target=run)
    thread.start()
    return thread, cur


def test_a_page_batch_mid_copy_does_not_block_reference_counts(db, monkeypatch):
    content = ["python gardening tomatoes " + filler(30, 4), [], "page", None]
    db.store(content, "https://a.example")
    page = page_item(db, monkeypatch, content, "https://a.example")

    writer, paused = write_pages_in_thread(db, page, PausedInCopy)
    assert paused.copying.wait(10)
    try:
        conn = psycopg2.connect(TEST_DATABASE_URL)
        cur = conn.cursor()
        cur.execute("SET statement_timeout = '3s';")  # waiting on the page batch's urls row lock would hit this
        db.write_references(cur, ["https://a.example"])
        conn.commit()
        conn.close()
    finally:
        paused.release.set()
        writer.join(10)

    conn = psycopg2.connect(TEST_DATABASE_URL)
    cur = conn.cursor()
    cur.execute("SELECT reference_count FROM urls WHERE url = 'https://a.example';")
    assert cur.fetchone() == (2,)
    conn.close()


def test_two_batches_storing_the_same_page_at_once_do_not_duplicate_its_postings(db, monkeypatch):
    content = ["python gardening tomatoes " + filler(30, 5), [], "page", None]
    db.store(content, "https://a.example")
    once = posting_counts(db)
    page = page_item(db, monkeypatch, content, "https://a.example")

    first, paused = write_pages_in_thread(db, page, PausedInCopy)
    assert paused.copying.wait(10)
    second, _ = write_pages_in_thread(db, page)
    second.join(1)
    assert second.is_alive()  # waits for the first batch's per-url lock instead of deleting and copying alongside it
    paused.release.set()
    first.join(10)
    second.join(10)

    assert posting_counts(db) == once


def test_page_denser_in_the_query_ranks_higher(db):
    import search
    # Same matches, but "python" is a bigger share of the short page; relevance must not be rounded away
    db.store(["python " + filler(40, 2), [], "short", None], "https://short.example")
    db.store(["python " + filler(100, 3), [], "long", None], "https://long.example")

    results = search.search("python")

    assert [r[0] for r in results] == ["https://short.example", "https://long.example"]
    relevance = {r[0]: r[3] for r in results}
    assert relevance["https://short.example"] > relevance["https://long.example"]
