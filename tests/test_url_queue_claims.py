"""
Claiming urls from url_queue: a claimed url can't be queued again or claimed by another scraper until it's finished or its claim times out

Needs a throwaway database, because it empties url_queue and urls:
    TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/stultus_test python -m pytest tests/test_url_queue_claims.py
The database tests are skipped when TEST_DATABASE_URL isn't set (DATABASE_URL is never used, so this can't touch the real queue)
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")

A, B = "https://a.example", "https://b.example"


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    import utils
    utils.create_database()
    conn = utils.get_conn()
    conn.cursor().execute("TRUNCATE url_queue, urls CASCADE;")
    conn.commit()
    conn.close()
    return utils


def execute(utils, query):
    conn = utils.get_conn()
    conn.cursor().execute(query)
    conn.commit()
    conn.close()


@needs_db
def test_claimed_url_is_not_queued_or_claimed_again(db):
    db.enqueue_urls([A, B])
    assert db.get_next_urls(1) == [A]

    # Another scraper finds a link to A while we hold it
    assert db.filter_new_urls([A]) == []
    db.enqueue_urls([A])
    assert db.get_next_urls(10) == [B]
    assert db.get_next_urls(10) == []


@needs_db
def test_finish_claims_releases_and_deletes(db):
    db.enqueue_urls([A, B])
    assert set(db.get_next_urls(10)) == {A, B}  # RETURNING doesn't promise row order

    db.finish_claims(done=[A], release=[B])
    assert db.filter_new_urls([A]) == [A]  # done: out of the queue (and not stored here, so it reads as new)
    assert db.get_next_urls(10) == [B]  # released: claimable again


@needs_db
def test_expired_claim_is_claimable_again(db):
    db.enqueue_urls([A])
    assert db.get_next_urls(10) == [A]
    execute(db, "UPDATE url_queue SET claimed_at = now() - interval '1 hour';")
    assert db.get_next_urls(10) == [A]
    assert db.get_next_urls(10) == []  # and that new claim holds


@needs_db
def test_create_database_adds_claim_column_to_old_queue(db):
    execute(db, "ALTER TABLE url_queue DROP COLUMN claimed_at;")
    db.create_database()
    db.enqueue_urls([A])
    assert db.get_next_urls(10) == [A]


def test_clean_links_lowercases_scheme_and_host_only():
    import utils
    assert utils.clean_links(["HTTPS://Example.COM/Some/Path/", "https://example.com/Some/Path", "HTTP://X.com", "https://ex.com/A?q=1#f"]) == [
        "https://example.com/Some/Path", "http://x.com", "https://ex.com/A"]
