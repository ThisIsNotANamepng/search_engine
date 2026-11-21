"""
Batches the scrapers' writes to postgres: one of these runs on each crawler box, every scraper on the box sends its inserts here, and every few seconds they all go to postgres together

Instead of every scraper sending ~15 small transactions per page (log lines, the page's urls row, its tokens and postings, seen urls, queued links, reference counts) to db01,
the box sends one transaction per kind of write every BATCH_INTERVAL seconds, with the words and prefixes shared by the batch's pages inserted and looked up once

Scrapers send here when BATCHER_ADDRESS is set (node_agent.py sets it when RUN_BATCHER=1), see utils._write():
    POST /<kind>   body: a JSON list of items, kind is one of utils.WRITERS (pages, seen, references, queue, logs)
    200 = buffered, 400 = bad request, 503 = buffer full (postgres is down or behind), which scrape.py treats like any other down server: retry the url later

Writes that fail because of the connection (postgres down, deadlock) are kept and retried on the next flush. A batch postgres rejects is written one item at a time, dropping only the items it rejects, so one bad page can't block the box
On SIGTERM it stops taking writes and flushes what it has. Writes buffered here when it's killed are lost: those pages aren't stored, and get queued again the next time a page links to them

Environment variables:
    DATABASE_URL     (required) the index postgres
    BATCHER_HOST     address to listen on (default 127.0.0.1: scrapers reach it over the host network, nothing else should)
    BATCHER_PORT     (default 8004)
    BATCH_INTERVAL   seconds between flushes (default 5)
    BATCH_PAGES      flush early once this many pages are buffered (default 200)

Run with:
    python3 supporting_servers/batcher.py
Only one process: every worker of a multi-process server would hold its own buffer
"""

import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import psycopg2
import utils

HOST = os.getenv("BATCHER_HOST", "127.0.0.1")
PORT = int(os.getenv("BATCHER_PORT", "8004"))
BATCH_INTERVAL = float(os.getenv("BATCH_INTERVAL", "5"))
BATCH_PAGES = int(os.getenv("BATCH_PAGES", "200"))
# Past these the batcher answers 503 instead of buffering more (pages are ~100 KB each as JSON, everything else ~100 bytes)
MAX_BUFFERED = {"pages": 2000, "seen": 200_000, "references": 500_000, "queue": 500_000, "logs": 200_000}
MAX_BODY_BYTES = 64 * 1024 * 1024
FINAL_FLUSH_SECONDS = 15  # on SIGTERM, keep retrying a failing flush this long (node_agent.py waits 20 s before SIGKILL)

# A connection problem, or a deadlock/serialization failure (TransactionRollbackError is an OperationalError): nothing was written, try the same batch again
TRANSIENT = (psycopg2.OperationalError, psycopg2.InterfaceError)

_lock = threading.Lock()
_buffers = {kind: [] for kind in utils.WRITERS}
_flush_now = threading.Event()  # set when enough pages are buffered, or to stop


def log(message):
    print(f"[batcher] {message}", flush=True)


def _valid(kind, item):
    """Whether item has the shape utils.WRITERS[kind] expects (scrapers are ours, but a malformed item would otherwise only fail at flush time)"""
    if kind == "pages":
        return (isinstance(item, dict) and isinstance(item.get("url"), str)
                and all(item.get(k) is None or isinstance(item[k], str) for k in ("title", "icon_link"))
                and all(isinstance(item.get(k), dict) and all(type(n) is int for n in item[k].values())
                        for k in ("words", "bigrams", "trigrams", "prefixes"))
                and isinstance(item.get("links"), list) and all(isinstance(l, str) for l in item["links"]))
    if kind == "logs":
        return (isinstance(item, list) and len(item) == 3 and isinstance(item[0], str) and isinstance(item[1], str)
                and type(item[2]) in (int, float))
    return isinstance(item, str)


def add(kind, items):
    """Buffer a request's items. Returns the HTTP status for the scraper"""
    if kind not in _buffers or not isinstance(items, list) or not all(_valid(kind, i) for i in items):
        return 400
    with _lock:
        if len(_buffers[kind]) + len(items) > MAX_BUFFERED[kind]:
            return 503
        _buffers[kind].extend(items)
        if len(_buffers["pages"]) >= BATCH_PAGES:
            _flush_now.set()
    return 200


def write(kind, items):
    """Write items to postgres. Returns the ones still to write (left after a connection problem)"""
    try:
        utils.write_now(kind, items)
        return []
    except TRANSIENT as e:
        log(f"Couldn't write {len(items)} {kind}, retrying next flush: {e!r}")
        return items
    except Exception as e:
        if len(items) == 1:
            log(f"Postgres rejected a {kind} item, dropping it: {e!r} {str(items[0])[:200]}")
            return []
        log(f"Postgres rejected a batch of {len(items)} {kind}, writing them one at a time: {e!r}")
        for i, item in enumerate(items):
            if write(kind, [item]):
                return items[i:]
        return []


def flush():
    """Write everything buffered, kind by kind in utils.WRITERS order. Returns how many items are still buffered"""
    left = 0
    for kind in utils.WRITERS:
        with _lock:
            items, _buffers[kind] = _buffers[kind], []
        if items:
            started = time.time()
            # A backlog of pages (after postgres was down) goes in BATCH_PAGES sized transactions, not one huge one holding its locks the whole time
            size = BATCH_PAGES if kind == "pages" else len(items)
            for start in range(0, len(items), size):
                failed = write(kind, items[start:start + size])
                if failed:
                    failed += items[start + size:]
                    break
            if len(failed) < len(items):
                log(f"Wrote {len(items) - len(failed)} {kind} in {time.time() - started:.2f}s")
            if failed:
                with _lock:
                    _buffers[kind][:0] = failed  # back in front of what arrived meanwhile, so the order is kept
        with _lock:
            left += len(_buffers[kind])
    return left


class Handler(BaseHTTPRequestHandler):
    timeout = 30  # a stuck client can't hold up shutdown forever

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY_BYTES:
            return self._reply(400 if length < 0 else 413)
        try:
            items = json.loads(self.rfile.read(length))
        except ValueError:
            return self._reply(400)
        self._reply(add(self.path.strip("/"), items))

    def _reply(self, status):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format, *args):
        pass  # no line per request


class Server(ThreadingHTTPServer):
    daemon_threads = False  # so server_close() waits for requests still being buffered before the final flush


def main():
    if not os.getenv("DATABASE_URL"):
        sys.exit("[batcher] DATABASE_URL is not set")

    stopping = threading.Event()

    def stop(signum, frame):
        stopping.set()
        _flush_now.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    server = Server((HOST, PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"Listening on {HOST}:{PORT}, flushing every {BATCH_INTERVAL}s or {BATCH_PAGES} pages")

    while not stopping.is_set():
        _flush_now.wait(BATCH_INTERVAL)
        _flush_now.clear()
        flush()

    log("Stopping: no new writes, flushing what's buffered")
    server.shutdown()
    server.server_close()
    deadline = time.time() + FINAL_FLUSH_SECONDS
    while flush() and time.time() < deadline:
        time.sleep(1)
    with _lock:
        lost = sum(len(items) for items in _buffers.values())
    if lost:
        log(f"Gave up on {lost} buffered writes")


if __name__ == "__main__":
    main()
