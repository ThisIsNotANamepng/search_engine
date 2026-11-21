"""Dev server for UI testing the Flask frontend WITHOUT a database.

    python tests/frontend_dev_server.py --port 5055 [--frontend-dir PATH]

* sets a dummy DATABASE_URL, blocks every psycopg2 connection, stubs init_control_schema
  and the dashboard stats;
* monkeypatches search.search to return canned results. Query keywords pick the data set:
    "empty"  -> no results          "none"   -> rows whose titles are None / empty
    "cjk"    -> CJK / unicode titles "long"  -> very long titles and URLs
    "xss"    -> javascript: / data: URLs and HTML in titles
    anything else -> 35 mixed rows (so pagination works at 10/20/30 per page)
* --frontend-dir serves another copy of the frontend (e.g. a pristine git worktree) by
  swapping Flask's template_folder / static_folder -- that is how BEFORE screenshots are made;
* injects window.__STULTUS_DEV__ into HTML so settings.js honours ?_debug=1, ?_settings=open,
  ?_newtab=1 and ?_perpage=N (screenshots of interactive states). Never active in production.
"""
import argparse
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

os.environ["DATABASE_URL"] = "postgresql://dummy:dummy@127.0.0.1:1/dummy"
os.environ["DASHBOARD_PREWARM"] = "0"
os.environ.setdefault("NLTK_DATA", "/home/ubuntu/nltk_data")

import psycopg2  # noqa: E402


def _no_db(*args, **kwargs):
    raise psycopg2.OperationalError("database access is disabled in the dev server")


psycopg2.connect = _no_db

import search  # noqa: E402


def _rows(n, start=0):
    out = []
    for i in range(start, start + n):
        out.append((f"https://example{i}.org/page/{i}", f"Example page number {i}: a typical title",
                    1000.0 / (i + 1), 1.0 + i / 10, 100.0 / (i + 1), 100.0 / (i + 1)))
    return out


def canned(query, limit=10, offset=0):
    q = (query or "").lower()
    if "empty" in q:
        rows = []
    elif "none" in q:
        rows = [("https://example.com/no-title", None, 12.5, 1.5, 8.0, 8.0),
                ("https://example.com/empty-title", "", 3.0, 1.1, 3.0, 3.0)]
    elif "cjk" in q:
        rows = [("https://ja.wikipedia.org/wiki/日本語", "日本語 - Wikipedia", 99.0, 4.0, 24.0, 24.0),
                ("https://zh.wikipedia.org/wiki/中文维基百科", "中文维基百科，自由的百科全书", 80.5, 3.0, 20.0, 20.0),
                ("https://ko.wikipedia.org/wiki/한국어", "한국어 - 위키백과, 우리 모두의 백과사전", 70.0, 2.5, 18.0, 18.0),
                ("https://example.com/émoji-🔍", "Ünïcödé títle with emoji 🔍🚀 and Ω≈ç√", 60.0, 2.0, 14.0, 14.0)]
    elif "long" in q:
        long_title = "An extremely long page title " + "that just keeps going and going " * 8
        long_url = "https://very-long-subdomain.example.com/" + "/".join("segment-number-%d" % i for i in range(25))
        solid = "https://example.com/" + "a" * 160
        rows = [(long_url, long_title, 50.0, 3.0, 17.0, 17.0),
                (solid, "Unbreakable URL below", 40.0, 2.0, 12.0, 12.0)] + _rows(8)
    elif "xss" in q:
        rows = [("javascript:alert(1)", "<script>alert('title')</script>", 5.0, 1.0, 1.0, 1.0),
                ("data:text/html,<b>x</b>", "<b>bold?</b> \"quotes\" & ampersand", 4.0, 1.0, 1.0, 1.0),
                ("https://example.com/?a=1&b=<2>", "Normal page", 3.0, 1.0, 1.0, 1.0)]
    else:
        rows = _rows(35)
    return rows[offset:offset + limit]


def fake_search(query, limit=10, offset=0):
    time.sleep(0.012)
    return canned(query, limit, offset)


search.search = fake_search

import importlib  # noqa: E402

sys.path.insert(0, os.path.join(REPO, "frontend"))
appmod = importlib.import_module("app")
app = appmod.app

appmod.init_control_schema = lambda: None
appmod._crawler_nodes = lambda: []
appmod._service_health = lambda: []
appmod._compute_dashboard_stats = lambda: {
    "status_counts": [{"status": "scraped", "count": 10}, {"status": "blocked", "count": 2}],
    "unique_domains": 5, "scrapes_per_minute": 3, "scrapes_over_time": [],
    "cumulative_scrapes_per_day": [], "real_last_10_scraped": [], "scrapers": [],
    "url_count": 12, "db_size": "1 MB"}
appmod._control_plane_unavailable = lambda: None


@app.after_request
def inject_dev_flag(resp):
    if resp.mimetype == "text/html" and not resp.direct_passthrough:
        body = resp.get_data(as_text=True)
        if "</head>" in body:
            resp.set_data(body.replace("</head>", "<script>window.__STULTUS_DEV__=true</script></head>", 1))
    return resp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5055)
    ap.add_argument("--frontend-dir", help="serve templates/ and static/ from this directory instead")
    args = ap.parse_args()
    if args.frontend_dir:
        d = os.path.abspath(args.frontend_dir)
        app.template_folder = os.path.join(d, "templates")
        app.static_folder = os.path.join(d, "static")
        app.jinja_loader.searchpath = [app.template_folder]
        print("serving frontend from", d)
    app.run(host="127.0.0.1", port=args.port, threaded=True)


if __name__ == "__main__":
    main()
