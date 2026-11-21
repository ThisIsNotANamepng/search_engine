"""
Refreshing the local blocklist.db copy (utils._refresh_blocklist_if_stale). No database or blocklist server needed
    python -m pytest tests/test_blocklist_refresh.py
"""

import os
import sqlite3
import sys
import time

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import utils


def write_blocklist(path, domains, age):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE blocklist (domain TEXT PRIMARY KEY)")
    db.execute("CREATE TABLE last_updated (last_updated TEXT)")
    db.executemany("INSERT INTO blocklist VALUES (?)", [(d,) for d in domains])
    db.execute("INSERT INTO last_updated VALUES (?)", (time.time() - age,))
    db.commit()
    db.close()
    os.utime(path, (time.time() - age,) * 2)


@pytest.fixture
def server_down(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db.invalid:5432/x")

    def refuse(*args, **kwargs):
        raise requests.ConnectionError("blocklist server down")
    monkeypatch.setattr(utils.requests, "get", refuse)


def test_server_down_keeps_using_the_old_copy(server_down):
    write_blocklist("blocklist.db", ["blocked-site.com"], age=7200)
    assert utils.check_url_in_blocklist("https://www.blocked-site.com/page")
    assert not utils.check_url_in_blocklist("https://fine-site.com/page")


def test_failed_refresh_waits_before_trying_again(server_down, monkeypatch):
    write_blocklist("blocklist.db", [], age=7200)
    calls = []

    def refuse(*args, **kwargs):
        calls.append(1)
        raise requests.ConnectionError("blocklist server down")
    monkeypatch.setattr(utils.requests, "get", refuse)
    for _ in range(3):
        utils.check_url_in_blocklist("https://fine-site.com/page")
    assert len(calls) == 1


def test_stale_updater_doesnt_cause_a_download_per_url(server_down, monkeypatch, tmp_path):
    # update_local_blocklist.py stopped running: the served copy's own last_updated is 2 hours old
    write_blocklist(tmp_path / "served.db", [], age=7200)
    served = (tmp_path / "served.db").read_bytes()
    calls = []

    class Response:
        def __enter__(self): return self
        def __exit__(self, *exc): pass
        def raise_for_status(self): pass
        def iter_content(self, chunk_size): yield served

    monkeypatch.setattr(utils.requests, "get", lambda *a, **k: calls.append(1) or Response())
    for _ in range(3):
        utils.check_url_in_blocklist("https://fine-site.com/page")
    assert len(calls) == 1


def test_server_down_without_any_copy_raises(server_down):
    # Scraping without a blocklist isn't allowed: scrape.py must see the error and retry the url later
    with pytest.raises(requests.ConnectionError):
        utils.check_url_in_blocklist("https://fine-site.com/page")
