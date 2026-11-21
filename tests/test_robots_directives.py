"""
Robots meta tags, X-Robots-Tag and rel="nofollow" handling in utils.extract_data_from_html. No database needed
    python -m pytest tests/test_robots_directives.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import utils

URL = "https://site.example/blog/post/"


def page(head="", body=""):
    return f"<html><head><title>t</title>{head}</head><body>{body}<p>Some text on the page.</p></body></html>"


def test_rel_nofollow_sponsored_ugc_links_dropped():
    body = '<a href="a">a</a><a rel="nofollow" href="b">b</a><a rel="noopener Sponsored" href="c">c</a><a rel="ugc" href="d">d</a><a rel="noopener" href="/e">e</a>'
    links = utils.extract_data_from_html(page(body=body), URL)[1]
    assert links == [URL + "a", "https://site.example/e"]


def test_meta_robots():
    data = utils.extract_data_from_html(page('<meta name="ROBOTS" content="NoIndex, nofollow">', '<a href="a">a</a>'), URL)
    assert data[1] == []
    assert {"noindex", "nofollow"} <= data[4]


def test_meta_aimed_at_us_and_none():
    data = utils.extract_data_from_html(page(f'<meta name="{utils.ROBOTS_NAME}" content="none">', '<a href="a">a</a>'), URL)
    assert data[1] == []
    assert {"noindex", "nofollow"} <= data[4]


def test_meta_for_another_crawler_ignored():
    data = utils.extract_data_from_html(page('<meta name="googlebot" content="noindex, nofollow">', '<a href="a">a</a>'), URL)
    assert data[1] == [URL + "a"]
    assert "noindex" not in data[4]


def test_x_robots_tag():
    assert utils.robots_directives(["noarchive"]) == {"noarchive"}
    # A crawler-name prefix applies to every directive after it, until the next prefix
    assert utils.robots_directives(["googlebot: noindex, nofollow"]) == set()
    assert utils.robots_directives([f"googlebot: noindex, {utils.ROBOTS_NAME}: noarchive, nofollow"]) == {"noarchive", "nofollow"}
    # A valued directive's colon isn't a crawler name
    assert "noindex" in utils.robots_directives(["unavailable_after: 2030-01-01, noindex"])
    data = utils.extract_data_from_html(page(body='<a href="a">a</a>'), URL, "noindex, nofollow")
    assert data[1] == [] and "noindex" in data[4]


def test_no_directives():
    data = utils.extract_data_from_html(page(body='<a href="a">a</a>'), URL)
    assert data[1] == [URL + "a"]
    assert data[4] == set()
