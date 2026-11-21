"""
Stores pages into a real Postgres and searches them

Needs a throwaway database, because it empties the index tables:
    TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/stultus_test python -m pytest tests/
Skipped when TEST_DATABASE_URL isn't set (DATABASE_URL is never used, so this can't touch the real index)
"""

import os
import random
import sys

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
    cur.execute("TRUNCATE urls, url_token_counts, words, prefixes, " + ", ".join(POSTING_TABLES) + " CASCADE;")
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


def test_page_denser_in_the_query_ranks_higher(db):
    import search
    # Same matches, but "python" is a bigger share of the short page; relevance must not be rounded away
    db.store(["python " + filler(40, 2), [], "short", None], "https://short.example")
    db.store(["python " + filler(100, 3), [], "long", None], "https://long.example")

    results = search.search("python")

    assert [r[0] for r in results] == ["https://short.example", "https://long.example"]
    relevance = {r[0]: r[3] for r in results}
    assert relevance["https://short.example"] > relevance["https://long.example"]
