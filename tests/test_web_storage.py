"""
The web text storage server (supporting_servers/web_storage_server.py). No servers needed
    python -m pytest tests/test_web_storage.py
"""

import gzip
import importlib.util
import os
import shutil
import sqlite3

SERVER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "supporting_servers", "web_storage_server.py")


def load_server(tmp_path):
    # The server keeps its data next to its own file, so run a copy in a temp dir
    shutil.copy(SERVER, tmp_path)
    spec = importlib.util.spec_from_file_location("web_storage_server_copy", tmp_path / "web_storage_server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    return server


def test_same_text_at_two_urls_records_both(tmp_path):
    server = load_server(tmp_path)
    client = server.app.test_client()
    body = gzip.compress(b"The same page text served by two different urls.")
    for url in ("https://a-site.com/x", "https://mirror-site.com/x"):
        response = client.post("/store", query_string={"url": url}, data=body, headers={"Content-Encoding": "gzip"})
        assert response.status_code == 200
    rows = sqlite3.connect(server.DB_PATH).execute("SELECT url FROM pages ORDER BY url").fetchall()
    assert rows == [("https://a-site.com/x",), ("https://mirror-site.com/x",)]
