"""
The scraper's side of the proxy: the proxy's own failures must make the url retry later instead of dropping it,
and a temporary robots.txt error must not be cached as "no robots.txt" for the day. No database or Go needed
    python -m pytest tests/test_proxy_errors.py
"""

import http.server
import os
import sys
import threading
import time
from urllib.parse import parse_qs, urlparse

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import utils

PAGE = b"<html><body>a page</body></html>"


@pytest.fixture
def fake_proxy(monkeypatch):
    """
    A stand-in for our /fetch proxy. answers[url or path] = (status, marked[, location]) decides each answer, default (200, False)
    A 200 robots.txt disallows everything. answers["fetched"] lists every url asked for
    """
    answers = {"/robots.txt": (404, False), "fetched": []}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            url = parse_qs(urlparse(self.path).query)["url"][0]
            answers["fetched"].append(url)
            target = urlparse(url).path
            status, marked, *location = answers.get(url) or answers.get(target, (200, False))
            body = b"User-agent: *\nDisallow: /\n" if target == "/robots.txt" else PAGE
            self.send_response(status)
            if marked:
                self.send_header("X-Proxy-Error", "1")
            if location:
                self.send_header("Location", location[0])
            answers.setdefault("cookies", []).append(self.headers.get("Cookie"))
            self.send_header("Set-Cookie", f"session={urlparse(url).hostname}")
            # Errors are text/plain like the proxy's own http.Error answers
            self.send_header("Content-Type", "text/plain" if target == "/robots.txt" or status >= 400 else "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(utils, "SCRAPER_PROXY", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setattr(utils, "is_public_host", lambda url: True)
    utils._robots_parser.cache_clear()
    yield answers
    server.shutdown()
    utils._robots_parser.cache_clear()


@pytest.mark.parametrize("path", ["/robots.txt", "/page"])
def test_proxy_failure_raises_so_the_url_is_retried(fake_proxy, path):
    fake_proxy[path] = (502, True)
    with pytest.raises(requests.ConnectionError):
        utils.get_page_html("http://site.example/page")


def test_site_error_is_the_pages_fault(fake_proxy):
    fake_proxy["/page"] = (502, False)  # the site itself is down: no marker
    assert utils.get_page_html("http://site.example/page")[0] is False


def test_robots_txt_redirect_is_followed_through_the_proxy(fake_proxy):
    # http -> https is the usual case; requests following it itself would go around the proxy, which the workers can't
    fake_proxy["http://site.example/robots.txt"] = (301, False, "https://site.example/robots.txt")
    fake_proxy["https://site.example/robots.txt"] = (200, False)
    assert not utils.allowed_by_robots("http://site.example/page", utils.USER_AGENT)
    assert fake_proxy["fetched"] == ["http://site.example/robots.txt", "https://site.example/robots.txt"]


def test_no_cookies_carried_from_one_site_to_another(fake_proxy):
    # Every request goes to the proxy's host, so a cookie jar would file each site's cookies there and send them to all sites
    utils.get_page_html("http://one.example/page")
    utils.get_page_html("http://two.example/page")
    assert fake_proxy["cookies"] == [None] * 4  # robots.txt + page, twice


def test_robots_txt_server_error_is_not_cached(fake_proxy):
    url = "http://site.example/page"
    fake_proxy["/robots.txt"] = (503, False)
    assert utils.allowed_by_robots(url, utils.USER_AGENT)
    fake_proxy["/robots.txt"] = (200, False)  # back up, and it disallows everything
    assert not utils.allowed_by_robots(url, utils.USER_AGENT)
