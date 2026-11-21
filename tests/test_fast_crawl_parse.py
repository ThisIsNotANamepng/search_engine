"""
Parsing for fast_crawl.py: the one-parse extract_page() must match the old BeautifulSoup extract_data_from_html on the same pages.
No database or network needed:
    python -m pytest tests/test_fast_crawl_parse.py
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import utils
from crawl_lib import parse, urls

URL = "https://site.example/blog/post/"

ARTICLE = "<p>" + " ".join(["The quick brown fox jumps over the lazy dog while reading a long article about crawlers."] * 12) + "</p>"


def page(head="", body=""):
    return f"<html><head><title>My Title</title>{head}</head><body><nav><a href='/home'>home</a></nav><article><h1>Heading</h1>{ARTICLE}{body}</article></body></html>"


PAGES = {
    "plain": page(),
    "icon_shortcut": page('<link rel="icon" href="/a.png"><link rel="shortcut icon" href="/s.ico">'),
    "icon_apple": page('<link rel="apple-touch-icon" href="/apple.png">'),
    "icon_none": page('<link rel="stylesheet" href="/x.css">'),
    "icon_empty_href": page('<link rel="icon">'),
    "robots_noindex": page('<meta name="robots" content="noindex, nofollow">', '<a href="/x">x</a>'),
    "robots_for_us": page(f'<meta name="{utils.ROBOTS_NAME}" content="noarchive">'),
    "robots_other": page('<meta name="googlebot" content="noindex">', '<a href="/x">x</a>'),
    "rels": page(body='<a href="a">a</a><a rel="nofollow" href="b">b</a><a rel="noopener Sponsored" href="c">c</a><a rel="ugc" href="d">d</a><a href="mailto:a@b.c">m</a>'),
    "relative": page(body='<a href="page2">p2</a><a href="../up">up</a><a href="//other.example/x">proto</a><a href="https://o.example/y?q=1#f">abs</a>'),
    "noscript_link": page(body='<noscript><a href="/ns">ns</a></noscript><script>var a = "<a href=\'/js\'>";</script>'),
    "no_title": "<html><body>" + ARTICLE + "<a href='/l'>l</a></body></html>",
    "empty_title": "<html><head><title></title></head><body>" + ARTICLE + "</body></html>",
    "latin1": ("<html><head><meta charset='iso-8859-1'><title>Caf\xe9</title></head><body>" + ARTICLE + "</body></html>"),
    "broken_link": page(body='<a href="http://[broken">x</a><a href="/ok">ok</a>'),
}


@pytest.mark.parametrize("name", sorted(PAGES))
def test_matches_bs4_version(name):
    html = PAGES[name]
    body = html.encode("latin-1") if name == "latin1" else html
    if name == "broken_link":
        # the old implementation crashes on this page (ValueError from urljoin); the new one skips just that link
        links = utils.extract_data_from_html(body, URL)[1]
        assert links == ["https://site.example/home", "https://site.example/ok"]
        return
    old = utils._extract_data_from_html_bs4(body, URL, "X-Header: noarchive" if name == "plain" else "")
    new = utils.extract_data_from_html(body, URL, "X-Header: noarchive" if name == "plain" else "")
    assert new[1] == old[1], "links"
    assert new[2] == old[2], "title"
    assert new[3] == old[3], "icon"
    assert new[4] == old[4], "directives"
    if old[0] is None:
        assert new[0] is None
    else:
        # same extractor, same input: the text should be the same up to whitespace
        assert " ".join((new[0] or "").split()) == " ".join(old[0].split())


def test_title_and_icon_values():
    data = parse.extract_page(PAGES["icon_shortcut"], URL)
    assert data[2] == "My Title"
    assert data[3] == "https://site.example/s.ico"
    assert parse.extract_page(PAGES["no_title"], URL)[2] == URL
    assert parse.extract_page(PAGES["empty_title"], URL)[2] is None
    assert parse.extract_page(PAGES["icon_empty_href"], URL)[3] is None


def test_nofollow_page_has_no_links():
    data = parse.extract_page(PAGES["robots_noindex"], URL)
    assert data[1] == [] and {"noindex", "nofollow"} <= data[4]


def test_unparseable_body():
    assert parse.extract_page(b"", URL) == [None, [], URL, None, set()]


def test_bytes_with_declared_charset():
    body = "<html><head><title>Caf\xe9</title></head><body>" + ARTICLE + "</body></html>"
    assert parse.extract_page(body.encode("latin-1"), URL, charset="iso-8859-1")[2] == "Caf\xe9"


# ---------------------------------------------------------------- process_page (what the pool workers run)

def job(html, **kw):
    return {"url": URL, "final_url": URL, "html": html.encode() if isinstance(html, str) else html, "charset": None, "x_robots": "", "keep_text": True, **kw}


def test_process_page_indexed_counts_and_links():
    body = '<a href="/a">a</a><a href="/a/b/c">c</a><a href="https://other.example/page/deep/x">o</a><a href="https://other.example">root</a><a href="/img.png">i</a>'
    res = parse.process_page(job(page(body=body)))
    assert res["status"] == "indexed" and res["lang"] == "en"
    words, bigrams, trigrams, prefixes = res["counts"]
    assert words["fox"] == 12 and "qu" in bigrams and "fox" in trigrams and prefixes
    assert res["page_url"] == "https://site.example/blog/post"
    assert "https://site.example/img.png" in res["links"]            # references keep every link ...
    cands = res["candidates"]
    assert "https://site.example/img.png" not in cands["site.example"]  # ... but images are never enqueued
    assert cands["other.example"][0] == "https://other.example"        # homepage first
    assert "https://other.example" in res["external"] and "https://site.example/a" not in res["external"]


def test_process_page_noindex_and_language():
    res = parse.process_page(job(page('<meta name="robots" content="noindex">')))
    assert res["status"] == "noindex" and res["counts"] is None
    french = "<html><head><title>t</title></head><body><p>" + " ".join(["Le renard brun rapide saute par-dessus le chien paresseux pendant que nous lisons un long article."] * 15) + "</p></body></html>"
    res = parse.process_page(job(french))
    # whether French is indexed is the tokenizer's call (language_gate when it has one, else English only): the page follows that decision
    should_index, lang, _ = parse.language_decision(res["text"])
    assert res["lang"] == lang
    assert res["status"] == ("indexed" if should_index else "not_indexed_language")
    assert (res["counts"] is not None) == should_index


def test_process_page_no_text():
    res = parse.process_page(job("<html><head><title>t</title></head><body><a href='/x'></a></body></html>"))
    assert res["status"] == "no_text" and res["links"]


def test_process_page_total_tokens_inputs_match_store():
    """the counts the writer receives are the ones utils.store() would compute from the same text"""
    import tokenizer
    res = parse.process_page(job(page()))
    text = res["text"]
    words, bigrams, trigrams, prefixes = tokenizer.tokenize_all_counts(text)
    assert res["counts"][0] == {w: n for w, n in words.items() if 0 < len(w) <= utils.MAX_TOKEN_LENGTH}
    assert res["counts"][1] == dict(bigrams) and res["counts"][2] == dict(trigrams)


# ---------------------------------------------------------------- url helpers

@pytest.mark.parametrize("url,expected", [
    ("https://a.example/pic.JPG", True), ("https://a.example/file.pdf", True), ("https://a.example/a.tar.gz", True),
    ("https://a.example/page.html", False), ("https://a.example/page.php", False), ("https://a.example", False),
    ("https://a.example/v1.2/docs", False), ("https://a.example/a.b/c", False), ("https://a.example/feed.xml", True),
])
def test_looks_non_html(url, expected):
    assert urls.looks_non_html(url) is expected


def test_depth_and_host_helpers():
    assert urls.path_depth("https://a.example") == 0
    assert urls.path_depth("https://a.example/x") == 1
    assert urls.path_depth("https://a.example/x/y/z") == 3
    assert urls.host_key("www.a.example") == "a.example" and urls.host_key("blog.a.example") == "blog.a.example"
    assert urls.host_suffixes("a.b.example.co.uk") == ["a.b.example.co.uk", "b.example.co.uk", "example.co.uk", "co.uk"]
