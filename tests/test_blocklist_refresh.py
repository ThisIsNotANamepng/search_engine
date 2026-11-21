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


def test_server_down_without_any_copy_raises(server_down):
    # Scraping without a blocklist isn't allowed: scrape.py must see the error and retry the url later
    with pytest.raises(requests.ConnectionError):
        utils.check_url_in_blocklist("https://fine-site.com/page")
