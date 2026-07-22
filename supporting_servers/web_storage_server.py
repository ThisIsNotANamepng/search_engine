"""
We save a copy of text for indexed websites for future research

This is the server for receiving that data from the scrapers

The raw text is stored in data/files/<b0>/<b1>/.../<id>.txt, where <id> is the sha256 of the text and <b0>/<b1>/... are the first SHARD_BYTES bytes of the id (one directory level per byte), used to shard the files across directories so no single directory holds too many entries and Linux breaks

A SQLite database (data/web_storage.db, WAL mode) correlates each <id> to the url, title, and the time the server received it

Efficiency notes:
  - The page text is sent as the raw request body (optionally gzip encoded), not wrapped in JSON, so we don't need to worry about parsing json. Also I hate json. Metadata (url, title) come in query params
  - The body is streamed to disk in chunks instead of being read into memory

Run with Gunicorn for concurrency (which is especially important for this server):
    gunicorn -w 4 -b 0.0.0.0:8003 web_storage_server:app
"""

import os
import sqlite3
import zlib
import hashlib
import tempfile
from datetime import datetime, timezone
from flask import Flask, request, g, jsonify, send_file, abort

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
FILES_DIR = os.path.join(DATA_DIR, "files")
DB_PATH = os.path.join(DATA_DIR, "database.db")
SHARD_BYTES = 6

CHUNK_SIZE = 64 * 1024  # 64 KiB, size of each streamed read/write
# Reject bodies larger than this, this is the size in communication, so a gzipped body can expand to considerably more text than this once decompressed
MAX_CONTENT_LENGTH = 64 * 1024 * 1024  # 64 MiB

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH


# ======== Helpers

def init_db():
    """Create the data directories and the metadata table if they don't exist."""
    os.makedirs(FILES_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        # WAL lets concurrent readers proceed while a write is in flight, which matters once several scrapers are posting at once
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pages (
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                title TEXT,
                byte_size INTEGER NOT NULL,
                stored_at TEXT NOT NULL
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pages_url ON pages(url);")
        conn.commit()
    finally:
        conn.close()

def get_db():
    """Return a per-request SQLite connection, opening one on first use."""
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
    return g.db

@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def path_for_id(page_id):
    """Return the sharded on-disk path for a given content id (no side effects)."""
    # One directory level per byte (each byte == 2 hex chars) for the first SHARD_BYTES bytes of the id, then the full id as the filename
    levels = [page_id[i * 2 : i * 2 + 2] for i in range(SHARD_BYTES)]
    return os.path.join(FILES_DIR, *levels, page_id + ".txt")

def _iter_body():
    """
    Yield decoded chunks of the request body, transparently gunzipping it if the client set `Content-Encoding: gzip`. Streams, so the whole body never has to live in memory at once.
    """
    stream = request.stream
    if request.headers.get("Content-Encoding", "").lower() == "gzip":
        # 16 + MAX_WBITS tells zlib to expect a gzip (not raw zlib) header.
        decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
        while True:
            chunk = stream.read(CHUNK_SIZE)
            if not chunk:
                break
            yield decompressor.decompress(chunk)
        yield decompressor.flush()
    else:
        while True:
            chunk = stream.read(CHUNK_SIZE)
            if not chunk:
                break
            yield chunk


# ================ Server routes

@app.route("/store", methods=["POST"])
def store_page():
    """
    Store the text of one page.

    Body: The raw page text, optionally gzip encoded (set `Content-Encoding: gzip`)

    Query params:
        url    (required) the page's url
        title  (optional) the page's title
    """
    url = request.args.get("url")
    if not url:
        abort(400, description="missing required 'url' query parameter")
    title = request.args.get("title")

    # Stream the body to a temp file in the files root, hashing as we go. We can't know the final (content-addressed) path until the hash is complete, so we write to a temp name first and atomically move it into place after
    hasher = hashlib.sha256()
    byte_size = 0
    tmp_fd, tmp_path = tempfile.mkstemp(dir=FILES_DIR, suffix=".part")
    try:
        with os.fdopen(tmp_fd, "wb") as tmp:
            try:
                for chunk in _iter_body():
                    if not chunk:
                        continue
                    hasher.update(chunk)
                    byte_size += len(chunk)
                    tmp.write(chunk)
            except zlib.error:
                abort(400, description="malformed gzip body")

        if byte_size == 0:
            abort(400, description="empty body")

        page_id = hasher.hexdigest()
        final_path = path_for_id(page_id)

        # If we already have this text already, drop the temp file and just make sure the metadata row exists
        duplicate = os.path.exists(final_path)
        if not duplicate:
            os.makedirs(os.path.dirname(final_path), exist_ok=True)
            os.replace(tmp_path, final_path)
            tmp_path = None  # Consumed by the move, don't clean it up below
    finally:
        if tmp_path is not None and os.path.exists(tmp_path):
            os.remove(tmp_path)

    db = get_db()
    db.execute("INSERT OR IGNORE INTO pages (id, url, title, byte_size, stored_at) VALUES (?, ?, ?, ?, ?)", (page_id, url, title, byte_size, datetime.now(timezone.utc).isoformat(),),)
    db.commit()

    return "Stored"

@app.route("/health", methods=["GET"])
def health():
    return "Up"

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=8003)
else:
    # When launching with Gunicorn
    init_db()
