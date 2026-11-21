#!/usr/bin/env python3
"""
fast_crawl.py: a concurrent crawler for the search engine. See docs/docs/fast-crawl.md.

    # 5,000 pages across thousands of sites, starting from the seed list:
    python fast_crawl.py --seed-file seed_urls.csv --max-pages 5000

    # no database (benchmarks, dry runs): crawl a url list, print stats
    python fast_crawl.py --nodb -f crawl_lib/bench_urls.txt
    python fast_crawl.py --nodb --follow --seed-file seed_urls.csv --max-pages 500

Architecture: one asyncio event loop (aiohttp) runs the fetchers, the per-host politeness scheduler and the DB queue;
the CPU-heavy part (HTML parse + trafilatura + language gate + tokenize) runs in a process pool; one writer thread
commits batches of pages. No schema changes: all writes are the row writes utils.store() already does.
"""

import argparse
import asyncio
import functools
import ipaddress
import json
import math
import os
import queue
import random
import signal
import socket
import sys
import threading
import time
from collections import Counter
from concurrent.futures import Executor, Future, ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from urllib.parse import unquote, urljoin, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

REDIRECT_STATUSES = (301, 302, 303, 307, 308)
REDIRECT_LIMIT = 5


class ProxyDown(Exception):
    """SCRAPER_PROXY unreachable: not the page's fault, the url is retried later"""


def load_dotenv(path):
    """KEY=VALUE lines; real environment variables win"""
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError:
        return False
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)
    return True


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Fast concurrent crawler (see docs/docs/fast-crawl.md)")
    src = p.add_argument_group("where urls come from (default: the url_queue table)")
    src.add_argument("-u", "--url", help="crawl just this url (not from the queue)")
    src.add_argument("-f", "--file", help="crawl the urls in this file, one per line (not from the queue)")
    src.add_argument("--seed-file", help="enqueue these urls (one per line, or first CSV column) at startup and crawl them first")
    src.add_argument("--nodb", "--dry-run", dest="nodb", action="store_true", help="no postgres/redis: everything except the writes (needs -u, -f or --seed-file)")
    src.add_argument("--follow", action="store_true", help="also crawl links found, from an in-memory frontier (with --nodb, or with -f/-u against a database; combine with --no-enqueue to keep this crawl off the shared url_queue)")
    src.add_argument("--no-enqueue", action="store_true", help="don't add discovered links to the queue (use for tiny test runs against a real db)")
    lim = p.add_argument_group("limits")
    lim.add_argument("--max-pages", type=int, default=0, help="stop after starting this many pages (0 = no limit)")
    lim.add_argument("--max-time", type=float, default=0, help="stop after this many seconds (0 = no limit)")
    lim.add_argument("--max-pages-per-host", type=int, default=4, help="pages fetched per host per run; also bounds what's queued per host (default 4)")
    lim.add_argument("--exit-when-empty", action="store_true", help="exit when the queue is empty instead of waiting for new urls")
    conc = p.add_argument_group("concurrency")
    conc.add_argument("--fetchers", type=int, default=64, help="concurrent downloads, at most one per host (default 64)")
    conc.add_argument("--parsers", type=int, default=max(1, (os.cpu_count() or 2) - 1), help="parser processes (default cores-1)")
    conc.add_argument("--batch-pages", type=int, default=25, help="pages per database transaction at most (default 25)")
    conc.add_argument("--flush-interval", type=float, default=1.0, help="seconds between database flushes (default 1)")
    pol = p.add_argument_group("politeness")
    pol.add_argument("--min-delay", type=float, default=3.0, help="minimum seconds between requests to one host (default 3, +0-25%% jitter)")
    pol.add_argument("--max-crawl-delay", type=float, default=30.0, help="cap for a robots.txt Crawl-delay (default 30)")
    pol.add_argument("--no-redis", action="store_true", help="don't coordinate host cooldowns with other scrapers through redis")
    brd = p.add_argument_group("breadth")
    brd.add_argument("--internal-links", type=int, default=3, help="same-host links enqueued per page, shallowest first (default 3)")
    brd.add_argument("--external-hosts", type=int, default=30, help="other hosts enqueued per page, one shallowest link each (default 30)")
    misc = p.add_argument_group("misc")
    misc.add_argument("--blocklist", default="blocklist.db", help="local blocklist sqlite file (default ./blocklist.db; skipped if missing)")
    misc.add_argument("--claim-batch", type=int, default=200, help="urls claimed from url_queue at a time (default 200)")
    misc.add_argument("--db-watchdog", type=float, default=120.0, help="abort (and retry) a database flush running longer than this many seconds, and complain when no flush completed for this long while work waits (default 120)")
    misc.add_argument("--parse-timeout", type=int, default=60, help="seconds before a page's parse is abandoned (default 60)")
    misc.add_argument("--dump", help="write one JSON line per page (url, title, lang, status, text length, link count) to this file")
    misc.add_argument("--verbose", "-v", action="store_true", help="print every log message")
    misc.add_argument("--progress-interval", type=float, default=10.0)
    args = p.parse_args(argv)
    if args.url and args.file:
        p.error("Cannot use both --file and --url")
    if args.nodb and not (args.url or args.file or args.seed_file):
        p.error("--nodb needs --url, --file or --seed-file")
    if args.follow and not args.nodb and not (args.url or args.file):
        p.error("--follow with a database needs -u or -f (queue mode already follows links through url_queue)")
    return args


def read_url_file(path):
    urls = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip().split(",")[0].strip()
            if line and not line.startswith("#"):
                urls.append(line)
    return urls


def apply_session_env():
    """Connections made through utils.get_conn() (create_database check, queue_has_claims) get the same safety timeouts and name
    as ours, through libpq's environment variables (explicit values in the environment win)"""
    from crawl_lib.db import SESSION_OPTIONS
    current = os.environ.get("PGOPTIONS", "")
    if "lock_timeout" not in current:
        os.environ["PGOPTIONS"] = (current + " " + SESSION_OPTIONS).strip()
    os.environ.setdefault("PGAPPNAME", "fast_crawl")


def rss_mb(pid):
    try:
        with open(f"/proc/{pid}/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1e6
    except (OSError, ValueError, IndexError):
        return 0.0


def _noop(i):
    return i


class NullWriter:
    """--nodb: same interface as AsyncWriter, writes nothing"""

    def __init__(self, crawler):
        self.c = crawler
        self.logs = 0
        self.stats = Counter()

    def submit(self, item):
        self.logs += len(item.get("logs", ()))
        if self.c.args.verbose:
            for m in item.get("logs", ()):
                print("LOG", m)

    async def wait_capacity(self):
        pass

    async def run(self):
        pass

    async def flush_now(self):
        pass

    async def stop(self, timeout=None):
        pass

    def pending(self):
        return 0


class DaemonExecutor(Executor):
    """A single daemon worker thread. Unlike ThreadPoolExecutor a hung thread can be abandoned (replace the executor) and never blocks exit"""

    def __init__(self, name):
        self._q = queue.SimpleQueue()
        self._thread = threading.Thread(target=self._work, name=name, daemon=True)
        self._thread.start()

    def _work(self):
        while True:
            job = self._q.get()
            if job is None:
                return
            fut, fn = job
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                fut.set_result(fn())
            except BaseException as e:
                fut.set_exception(e)

    def submit(self, fn, *args, **kwargs):
        fut = Future()
        self._q.put((fut, functools.partial(fn, *args, **kwargs)))
        return fut

    def shutdown(self, wait=True, **kwargs):
        self._q.put(None)


def _swallow(fut):
    if not fut.cancelled():
        fut.exception()  # an abandoned (watchdog-timed-out) write finishing later is not an unretrieved-exception warning


class AsyncWriter:
    """Collects items from the fetch tasks and commits them in batches from one thread.
    Watchdog: a flush that runs longer than db_watchdog seconds is aborted (connection torn down, the server rolls back and drops its
    locks) and retried; if the writer thread does not come back either it is abandoned and a new thread + connection take over"""

    MAX_BUFFER = 400
    ABORT_GRACE = 15

    def __init__(self, crawler, db):
        self.c = crawler
        self.db = db
        self.items = []
        self.exec = DaemonExecutor("db-writer")
        self.last_flush = time.monotonic()
        self.last_ok = time.monotonic()      # last time a flush completed (or nothing was waiting to be flushed)
        self.flush_started = None
        self.stopping = False
        self.deadline = None                  # monotonic: give up flushing at shutdown
        self.flush_timeout = getattr(crawler.args, "db_watchdog", 120.0)
        self.flush_lock = asyncio.Lock()
        self.stats = Counter()
        self.consecutive_aborts = 0

    def submit(self, item):
        self.items.append(item)

    def pending(self):
        return len(self.items)

    async def wait_capacity(self):
        while len(self.items) > self.MAX_BUFFER:
            await asyncio.sleep(0.05)

    async def run(self):
        a = self.c.args
        while not self.stopping:
            await asyncio.sleep(0.2)
            if not self.items and self.flush_started is None:
                self.last_ok = time.monotonic()
            pages = sum(len(i.get("pages", ())) for i in self.items)
            if self.items and (pages >= a.batch_pages or time.monotonic() - self.last_flush >= a.flush_interval):
                await self.flush_now()

    def abort_flush(self):
        """Tear the writer's connection down (safe from the event loop thread: never blocks)"""
        self.stats["watchdog_aborts"] += 1
        self.db.abort()

    def replace_thread(self):
        """The writer thread did not return after abort: abandon it, continue with a fresh thread and connection"""
        from crawl_lib.db import DbWriter
        old = self.db
        self.exec.shutdown(wait=False)
        self.exec = DaemonExecutor("db-writer")
        new = DbWriter(old.dsn)
        new.maps, new.claims = old.maps, old.claims
        self.db = self.c.db = new
        self.stats["writer_replaced"] += 1
        self.c.hung = True

    async def _write(self, batch):
        """db.write on the writer thread, with the watchdog. Raises FlushAborted when the watchdog had to abort it"""
        from crawl_lib.db import FlushAborted
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(self.exec, self.db.write, batch)
        fut.add_done_callback(_swallow)
        self.flush_started = t0 = time.monotonic()
        try:
            timeout = self.flush_timeout
            if self.deadline is not None:
                timeout = max(0.5, min(timeout, self.deadline - time.monotonic()))
            done, _ = await asyncio.wait({fut}, timeout=timeout)
            if not done:
                self.c.note(f"WATCHDOG: a database flush has been running for {time.monotonic() - t0:.0f}s (limit {timeout:.0f}s): "
                            f"aborting it (connection closed, the server rolls the transaction back)")
                self.abort_flush()
                done, _ = await asyncio.wait({fut}, timeout=min(self.ABORT_GRACE, max(1.0, timeout)))
                if not done:
                    self.c.note("WATCHDOG: the writer thread is still stuck after the abort: abandoning it, new thread and connection")
                    self.replace_thread()
                raise FlushAborted(f"flush exceeded {timeout:.0f}s")
            return fut.result()
        finally:
            self.flush_started = None

    async def flush_now(self):
        from crawl_lib.db import Batch, FlushAborted
        async with self.flush_lock:
            while self.items:
                if self.deadline is not None and time.monotonic() >= self.deadline:
                    break
                # at most batch_pages pages per transaction, everything else (logs, seen, enqueue) rides along
                taken, pages = [], 0
                while self.items and pages < max(1, self.c.args.batch_pages):
                    item = self.items.pop(0)
                    taken.append(item)
                    pages += len(item.get("pages", ()))
                batch = Batch()
                for item in taken:
                    batch.add_item(item)
                t0 = time.monotonic()
                try:
                    stats = await self._write(batch)
                    self.stats["flushes"] += 1
                    self.stats["rows"] += stats.get("rows", 0)
                    self.stats["write_seconds"] += time.monotonic() - t0
                    self.consecutive_aborts = 0
                    self._committed(taken)
                except FlushAborted as e:
                    self.consecutive_aborts += 1
                    print(f"DB batch of {len(taken)} items aborted by the watchdog ({e!r}); attempt {self.consecutive_aborts}", file=sys.stderr, flush=True)
                    if self.consecutive_aborts < 3:
                        self.items[:0] = taken  # retry the same batch on a fresh connection
                        await asyncio.sleep(1.0)
                    else:
                        self.consecutive_aborts = 0
                        for item in taken:
                            await self._write_single(item)
                except Exception as e:
                    print(f"DB batch of {len(taken)} items failed ({e!r}), retrying one by one", file=sys.stderr, flush=True)
                    for item in taken:
                        await self._write_single(item)
                self.last_flush = self.last_ok = time.monotonic()

    async def _write_single(self, item):
        from crawl_lib.db import Batch
        batch = Batch()
        batch.add_item(item)
        try:
            await self._write(batch)
            self._committed([item])
        except Exception as e:
            self.c.stats["db_errors"] += 1
            print(f"DB write failed for {item.get('refs_from') or item.get('done')}: {e!r}", file=sys.stderr, flush=True)
            # a page that can't be stored must not come back forever: log it and finish its claim
            rescue = Batch()
            urls = list(item.get("done", ()))
            rescue.done.update(urls)
            rescue.logs.append(f"Error Unhandled exception {e!r} {(urls or ['?'])[0]}")
            try:
                await self._write(rescue)
                self._committed([{"done": urls}])
            except Exception:
                pass  # claim stays: it's handed back when we exit, or times out

    def _committed(self, items):
        for item in items:
            for u in item.get("done", ()):
                self.c.claimed_open.discard(u)

    async def stop(self, timeout=None):
        """Final flush. timeout: give up (leaving the rest unflushed: its claims are handed back) after this many seconds"""
        self.stopping = True
        if timeout is not None:
            self.deadline = time.monotonic() + timeout
        try:
            await asyncio.wait_for(self.flush_now(), timeout + 2 if timeout is not None else None)
        except asyncio.TimeoutError:
            self.c.hung = True
            self.c.note("WARNING: final flush did not finish in time; unflushed work is handed back")
        self.exec.shutdown(wait=False)


class Crawler:
    def __init__(self, args):
        import utils
        from crawl_lib.frontier import Frontier
        from crawl_lib import urls as urlhelp
        self.utils = utils
        self.urlhelp = urlhelp
        self.args = args
        self.frontier = Frontier(args.max_pages_per_host, args.min_delay, args.max_crawl_delay)
        self.stats = Counter()
        self.accepted = Counter()      # host key -> urls this process queued (frontier or db queue)
        self.claimed_open = set()      # claimed from url_queue, not finished yet
        self.retry_later = set()       # claimed urls left claimed on purpose (rate limited): the claim timing out is their retry delay
        self.foreign_hosts = set()     # hosts that served a non-English page this run
        # English-only unless INDEX_LANGUAGES allows other languages: then foreign-looking urls are skipped before fetching too
        self.english_only = os.environ.get("INDEX_LANGUAGES", "en").strip().lower() in ("en", "en-us", "en-gb")
        self.seen_mem = set()          # --nodb --follow: urls already queued/crawled
        self.active = set()
        self.started = 0
        self.stop_requested = False
        self.stop_reason = ""
        self.t_start = time.monotonic()
        self.wake = asyncio.Event()
        self.db_mode = not args.nodb
        self.db = None                 # DbWriter: the writer thread's connection, nothing else may touch it
        self.queue = None              # QueueDb: its own connection for seeds, claims, hand-backs, the progress estimate
        self.seed_pending = []         # seeds enqueued but not claimed yet: claimed claim_batch at a time
        self.writer = None
        self.redis = None
        self.blocklist = None
        self.dns_cache = {}
        self.pool = None
        self.parse_sem = asyncio.Semaphore(max(2, args.parsers * 2))
        self.session = None
        self.robots = None
        self.queue_empty = False       # last claim returned nothing
        self.hung = False              # a database thread was abandoned (watchdog): the process must not wait for it at exit
        self.dump = open(args.dump, "w", encoding="utf-8") if args.dump else None
        self.web_text_server = os.getenv("WEB_TEXT_STORAGE_SERVER_ADDRESS")
        self.proxy = utils.SCRAPER_PROXY
        self.not_queue_sourced = bool(args.url or args.file)  # these urls have no claims: never touch url_queue rows for them
        self.headers = {"User-Agent": utils.USER_AGENT, "From": "crawler@stultus.rip",
                        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"}
        if self.proxy:
            self.headers.update(utils._PROXY_HEADERS)
        self.loop = asyncio.get_running_loop()

    # ------------------------------------------------------------------ setup / teardown
    def note(self, msg):
        print(msg, flush=True)

    async def setup(self):
        import aiohttp
        import multiprocessing
        from crawl_lib.blocklist import Blocklist, refresh_from_server
        from crawl_lib.robots import RobotsCache
        from crawl_lib.parse import worker_init
        utils = self.utils
        a = self.args
        self.loop.set_default_executor(ThreadPoolExecutor(32, thread_name_prefix="net"))

        # blocklist
        if self.db_mode:
            try:
                await asyncio.to_thread(refresh_from_server, a.blocklist, os.getenv("DATABASE_URL"))
            except Exception as e:
                self.note(f"WARNING: blocklist server unreachable ({e!r}); using the local {a.blocklist} if there is one")
        self.blocklist = Blocklist(a.blocklist)
        if len(self.blocklist):
            self.note(f"blocklist: {len(self.blocklist)} domains from {a.blocklist}")
        else:
            self.note(f"WARNING: no blocklist ({a.blocklist} missing or empty): crawling without one. Build one: python -m crawl_lib.blocklist build")

        # database
        if self.db_mode:
            from crawl_lib.db import DbWriter, QueueDb
            utils.NODB = False
            apply_session_env()
            await asyncio.to_thread(utils.create_database)  # read-only check unless STULTUS_ALLOW_DDL=1
            self.db = DbWriter()
            await asyncio.to_thread(self.db.connect)
            self.queue = QueueDb(claims=self.db.claims)
            self.note(f"queue protocol: {'claims (claimed_at)' if self.db.claims else 'claimless (pop = DELETE, unfinished urls re-inserted, explicit ids)'}")
            self.writer = AsyncWriter(self, self.db)
            await self.init_redis()
        else:
            utils.NODB = True
            self.writer = NullWriter(self)

        # network
        connector = aiohttp.TCPConnector(limit=a.fetchers + 16, limit_per_host=2, ttl_dns_cache=600)
        self.session = aiohttp.ClientSession(connector=connector, cookie_jar=aiohttp.DummyCookieJar(), auto_decompress=True)
        self.robots = RobotsCache(self.fetch_robots_bytes, utils.USER_AGENT)

        # parser processes (spawn: no forked copies of this process' sockets and threads)
        ctx = multiprocessing.get_context("spawn")
        self.pool = ProcessPoolExecutor(max_workers=a.parsers, mp_context=ctx, initializer=worker_init, max_tasks_per_child=300)
        # warm the workers up now (importing nltk/trafilatura takes a few seconds each) instead of during the first pages
        await asyncio.gather(*[self.loop.run_in_executor(self.pool, _noop, i) for i in range(a.parsers)])

    def rebuild_pool(self):
        import multiprocessing
        from crawl_lib.parse import worker_init
        try:
            self.pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        self.pool = ProcessPoolExecutor(max_workers=self.args.parsers, mp_context=multiprocessing.get_context("spawn"),
                                        initializer=worker_init, max_tasks_per_child=300)

    async def init_redis(self):
        if self.args.no_redis:
            self.note("redis: disabled (--no-redis), using in-process per-host politeness only")
            return
        database_url = urlparse(os.getenv("DATABASE_URL") or "")
        if not database_url.hostname:
            return
        try:
            import redis.asyncio as aioredis
            r = aioredis.Redis(host=database_url.hostname, port=6379, db=0, password=unquote(database_url.password or ""),
                               decode_responses=True, socket_connect_timeout=2, socket_timeout=2)
            await r.ping()
            self.redis = r
            self.note(f"redis: connected to {database_url.hostname}:6379, host cooldowns shared with other scrapers")
        except Exception as e:
            self.note(f"WARNING: redis unreachable ({e!r}); using in-process per-host politeness only")

    async def close(self):
        if self.session is not None:
            try:
                await asyncio.wait_for(self.session.close(), 3)
            except Exception:
                pass
        if self.redis is not None:
            try:
                await self.redis.aclose()
            except Exception:
                pass
        if self.pool is not None:
            self.pool.shutdown(wait=False, cancel_futures=True)
        if self.dump:
            self.dump.close()

    # ------------------------------------------------------------------ network helpers
    async def fetch_robots_bytes(self, robots_url):
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=10, sock_connect=5)
        if self.proxy:
            req = dict(url=f"{self.proxy}/fetch", params={"url": robots_url})
        else:
            req = dict(url=robots_url)
        async with self.session.get(headers=self.headers, timeout=timeout, **req) as resp:
            if resp.status != 200:
                return resp.status, b""
            body = bytearray()
            async for chunk in resp.content.iter_chunked(16 * 1024):
                body += chunk
                if len(body) >= 512 * 1024:
                    break
            return 200, bytes(body)

    async def host_public(self, host):
        """utils.is_public_host(), async and cached for 10 minutes"""
        hit = self.dns_cache.get(host)
        now = time.monotonic()
        if hit and hit[1] > now:
            return hit[0]
        try:
            infos = await asyncio.wait_for(self.loop.getaddrinfo(host, None, type=socket.SOCK_STREAM), 10)
            ok = all(ipaddress.ip_address(i[4][0].split("%", 1)[0]).is_global for i in infos)
        except (socket.gaierror, UnicodeError, asyncio.TimeoutError, OSError, ValueError):
            ok = False
        if len(self.dns_cache) > 100000:
            self.dns_cache.clear()
        self.dns_cache[host] = (ok, now + 600)
        return ok

    async def fetch_html(self, url, logs):
        """
        Async twin of utils.get_page_html(). Returns (kind, url, content, x_robots, charset)
        kind: 'ok' | 'redirected' (url = target) | 'rate_limited' | 'not_scrapable' | 'failed'
        Same-host redirects (http->https, example.com->www.example.com) are followed, each hop checked; other hosts are returned as 'redirected'
        """
        import aiohttp
        urlhelp = self.urlhelp
        cur = url
        hops = 0
        timeout = aiohttp.ClientTimeout(total=30, sock_connect=5, sock_read=10)
        max_bytes = self.utils.MAX_PAGE_BYTES
        while True:
            if self.proxy:
                req = dict(url=f"{self.proxy}/fetch", params={"url": cur})
            else:
                req = dict(url=cur)
            try:
                async with self.session.get(headers=self.headers, timeout=timeout, allow_redirects=False, **req) as resp:
                    status = resp.status
                    if status in REDIRECT_STATUSES and "Location" in resp.headers:
                        location = resp.headers.get("Location", "")
                        if not location or hops >= REDIRECT_LIMIT:
                            logs.append(f"Error Empty redirect or too many redirects {cur}")
                            return "not_scrapable", cur, None, "", None
                        target = urljoin(cur, location)
                        if urlhelp.host_key(urlhelp.host_of(target)) != urlhelp.host_key(urlhelp.host_of(cur)):
                            return "redirected", target, None, "", None
                        if not target.startswith(("http://", "https://")):
                            return "not_scrapable", cur, None, "", None
                        cur = target
                        hops += 1
                        redirect_hop = True
                    else:
                        redirect_hop = False
                        content_type = resp.headers.get("Content-Type", "").lower()
                        if status in (401, 403):
                            logs.append(f"Error HTTP {status} {cur}")
                            return "failed", cur, None, "", None
                        if status == 429:
                            logs.append(f"Error Rate limited {cur}")
                            return "rate_limited", cur, None, "", None
                        if not content_type.startswith("text/html"):
                            logs.append(f"Error Invalid data type {cur}")
                            return ("not_scrapable" if status < 400 else "failed"), cur, None, "", None
                        if status >= 400:
                            # an error page isn't content (the old scraper indexed these), and it isn't final either: no 'seen' mark
                            logs.append(f"Error HTTP {status} {cur}")
                            return "failed", cur, None, "", None
                        declared = resp.headers.get("Content-Length")
                        if declared and declared.isdigit() and int(declared) > max_bytes * 8:
                            logs.append(f"Error Page too large {cur}")
                            return "not_scrapable", cur, None, "", None
                        content = bytearray()
                        async for chunk in resp.content.iter_chunked(64 * 1024):
                            content += chunk
                            if len(content) > max_bytes:
                                logs.append(f"Error Page too large {cur}")
                                return "not_scrapable", cur, None, "", None
                        return "ok", cur, bytes(content), resp.headers.get("X-Robots-Tag", ""), resp.charset
            except asyncio.TimeoutError:
                logs.append(f"Error Total timeout exceeded {cur}")
                return "failed", cur, None, "", None
            except aiohttp.ClientConnectorError as e:
                if self.proxy and not isinstance(e, aiohttp.ClientConnectorDNSError):
                    raise ProxyDown(str(e))  # couldn't reach our own proxy, not the page
                logs.append(f"Error HTTP error fetching: {cur} : {e}")
                return "failed", cur, None, "", None
            except (aiohttp.ClientError, ValueError, UnicodeError, OSError) as e:
                logs.append(f"Error HTTP error fetching: {cur} : {e}")
                return "failed", cur, None, "", None

            if redirect_hop:
                if not await self.host_public(urlhelp.host_of(cur)):
                    logs.append(f"Error Redirect to a non-public address {cur}")
                    return "failed", cur, None, "", None
                entry = await self.robots.get(cur)
                if not entry.allowed(self.utils.USER_AGENT, cur):
                    logs.append(f"Blocked by robots.txt {cur}")
                    return "not_scrapable", cur, None, "", None

    # ------------------------------------------------------------------ redis
    async def redis_claim(self, st):
        """True if we may fetch from the host now (no other scraper touched it within the cooldown)"""
        if self.redis is None:
            return True
        try:
            return bool(await self.redis.set(f"domain:{st.fetch_host}", 1, nx=True, ex=max(1, math.ceil(st.delay))))
        except Exception as e:
            self.redis_down(e)
            return True

    async def redis_touch(self, st, seconds=None):
        if self.redis is None:
            return
        try:
            await self.redis.set(f"domain:{st.fetch_host}", 1, ex=int(seconds or max(1, math.ceil(st.delay))))
        except Exception as e:
            self.redis_down(e)

    def redis_down(self, e):
        if self.redis is not None:
            self.note(f"WARNING: redis failed ({e!r}); continuing with in-process politeness only")
            self.redis = None

    # ------------------------------------------------------------------ queue intake
    def add_url(self, url, claimed):
        """Put a url into the frontier. claimed: it came from url_queue and has a claim to finish. Returns True if accepted"""
        if claimed:
            self.claimed_open.add(url)
        reject = None
        if len(url) > self.utils.MAX_URL_LENGTH or not url.startswith(("http://", "https://")):
            reject = "bad_url"
        elif self.urlhelp.looks_non_html(url):
            reject = "non_html_extension"
        elif self.english_only and (self.urlhelp.looks_foreign(url) or self.urlhelp.host_key(self.urlhelp.host_of(url)) in self.foreign_hosts):
            reject = "foreign"
        elif not self.frontier.add(url, time.monotonic()):
            reject = "host_cap"
        if reject:
            self.stats["dropped_" + reject] += 1
            if claimed:
                self.writer.submit({"done": [url]})
            return False
        self.accepted[self.frontier.state(url).key] += 1
        return True

    async def queue_call(self, fn, *args, timeout=90, queue=None):
        """A url_queue call on its own connection, off the event loop, bounded: when it hangs its connection is torn down (the server rolls it back)"""
        try:
            return await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout)
        except asyncio.TimeoutError:
            self.hung = True
            self.note(f"WATCHDOG: url_queue call {getattr(fn, '__name__', fn)} took over {timeout:.0f}s: aborting its connection")
            (queue or self.queue).abort()
            raise

    async def claim_some(self):
        """Next urls for the frontier, at most about claim_batch of them: seeds first (a chunk of the seed list at a time), then the queue.
        Returns None when only seeds were looked at and none were left to claim (call again at once)"""
        a = self.args
        low_water = max(a.fetchers * 2, a.claim_batch // 2)
        size = max(min(a.claim_batch, 50), max(a.claim_batch, low_water + 1) - self.frontier.queued)  # the frontier never holds much more than claim_batch popped urls
        size = min(size, a.claim_batch)
        if self.seed_pending:
            chunk, self.seed_pending = self.seed_pending[:size], self.seed_pending[size:]
            urls = await self.queue_call(self.queue.claim_urls, chunk)
            self.stats["seeds_claimed"] += len(urls)
            return urls or None
        if self.not_queue_sourced:
            return []
        return await self.queue_call(self.queue.claim, size)

    async def loader(self):
        """Keep the frontier stocked from url_queue (claim protocol: utils.get_next_urls / finish_claims semantics), a bounded number of urls at a time"""
        a = self.args
        low_water = max(a.fetchers * 2, a.claim_batch // 2)
        while not self.stop_requested:
            if a.max_pages and self.started >= a.max_pages:
                return
            if self.frontier.queued < low_water:
                if self.not_queue_sourced and not self.seed_pending:
                    return  # -u / -f with --seed-file: only the seeds come from the queue
                try:
                    urls = await self.claim_some()
                except Exception as e:
                    print(f"claiming urls failed: {e!r}", file=sys.stderr, flush=True)
                    await asyncio.sleep(3)
                    continue
                if urls is None:
                    continue
                if not self.seed_pending:
                    self.queue_empty = not urls
                if self.stop_requested:
                    self.claimed_open.update(urls)  # claimed while shutting down: handed straight back
                    return
                for u in urls:
                    self.add_url(u, claimed=True)
                self.stats["claimed"] += len(urls)
                self.wake.set()
                if not urls:
                    await asyncio.sleep(2)
                continue
            await asyncio.sleep(0.25)

    # ------------------------------------------------------------------ one page
    def selected_links(self, own_key, candidates):
        """From the per-host shallow link candidates of a page, pick what to enqueue: a few internal, one link for each of several other hosts"""
        a = self.args
        cap = a.max_pages_per_host
        internal, external = [], []
        for host, links in candidates.items():
            key = self.urlhelp.host_key(host)
            if self.accepted[key] >= cap * 2:
                continue
            if self.blocklist is not None and self.blocklist.is_blocked(host):
                continue
            if self.english_only and (key in self.foreign_hosts or self.urlhelp.looks_foreign(links[0])):
                continue
            if key == own_key:
                internal.extend(links[:max(0, cap * 2 - self.accepted[key])])
            else:
                external.append(links[0])
        external.sort(key=self.urlhelp.path_depth)
        picked = [u for u in internal[:a.internal_links] + external[:a.external_hosts] if u not in self.claimed_open]  # popped from a claimless queue = in flight: not in url_queue or urls yet
        for u in picked:
            self.accepted[self.urlhelp.host_key(self.urlhelp.host_of(u))] += 1
        return picked

    def enqueue_found(self, item, urls):
        """Discovered links: db mode -> url_queue (in the writer's transaction); --nodb --follow -> straight into the frontier"""
        if not urls:
            return
        if self.db_mode and not self.args.no_enqueue:
            item["enqueue"] = list(item.get("enqueue", ())) + urls
        if self.args.follow:
            for u in urls:
                if u not in self.seen_mem:
                    self.seen_mem.add(u)
                    self.add_url(u, claimed=False)
            self.wake.set()

    async def run_one(self, st, url):
        item = {"logs": [], "done": [], "seen": [], "pages": []}
        state = {"released": False}
        orig = url
        try:
            outcome = await self.process(st, url, item, state)
        except asyncio.CancelledError:
            raise
        except ProxyDown as e:
            outcome = "error"
            self.stats["proxy_down"] += 1
            item = {"logs": [f"Error Proxy unreachable {url}"]}
            self.retry_later.add(orig)
            self.claimed_open.discard(orig)
        except Exception as e:
            outcome = "error"
            self.stats["errors"] += 1
            item = {"logs": [f"Error Unhandled exception {e!r} {url}"], "done": [orig]}
            if self.args.verbose:
                import traceback
                traceback.print_exc()
        finally:
            if not state["released"]:
                self.release_host(st, state, counted=True)
        if outcome == "requeued":
            return
        if self.not_queue_sourced:
            item["done"] = []  # these urls weren't claimed from url_queue, the same rows may belong to another scraper
        self.writer.submit(item)
        await self.writer.wait_capacity()

    def release_host(self, st, state, counted=True, delay=None):
        if state["released"]:
            return
        state["released"] = True
        if delay is None:
            delay = st.delay * (1 + random.random() * 0.25)
        self.frontier.complete(st, time.monotonic(), counted=counted, delay=delay)
        self.wake.set()

    async def process(self, st, url, item, state):
        a = self.args
        logs = item["logs"]
        host = self.urlhelp.host_of(url)
        stats = self.stats

        if st.blocked is None:
            st.blocked = self.blocklist is not None and self.blocklist.is_blocked(host)
        if st.blocked:
            logs.append(f"Misc URL found in domain blocklist, skipping {url}")
            item["seen"].append(url)
            item["done"].append(url)
            stats["blocked"] += 1
            self.release_host(st, state, counted=True, delay=0)
            return "blocked"

        if self.english_only and self.urlhelp.host_key(host) in self.foreign_hosts:
            # queued before the host served a non-English page: don't fetch it (not recorded as seen, a later English-only run may want it)
            item["done"].append(url)
            stats["dropped_foreign"] += 1
            self.release_host(st, state, counted=True, delay=0)
            return "foreign"

        if st.public is None:
            st.public = await self.host_public(host)
        if not st.public:
            logs.append(f"Error Not a public address {url}")
            item["done"].append(url)
            stats["errors"] += 1
            self.release_host(st, state, counted=True, delay=0)
            return "not_public"

        if not await self.redis_claim(st):
            # another scraper (or our previous request) touched this host in the last seconds
            self.started -= 1
            self.frontier.requeue(st, url, time.monotonic() + 1 + random.random() * 2)
            state["released"] = True
            self.wake.set()
            return "requeued"

        entry = await self.robots.get(url)
        if entry.delay:
            st.delay = min(max(entry.delay, a.min_delay), a.max_crawl_delay)
        if not entry.allowed(self.utils.USER_AGENT, url):
            logs.append(f"Blocked by robots.txt {url}")
            item["seen"].append(url)
            item["done"].append(url)
            stats["robots_blocked"] += 1
            self.release_host(st, state, counted=True)
            await self.redis_touch(st)
            return "robots"

        kind, final_url, content, x_robots, charset = await self.fetch_html(url, logs)
        # the network part is over: start the host's delay clock now, parsing happens off the host's time
        self.release_host(st, state, counted=True)
        await self.redis_touch(st)

        if kind == "redirected":
            # Another host: queue the target so it goes through that host's checks; stop re-queueing this url
            item["seen"].append(url)
            item["done"].append(url)
            target = (self.utils.clean_links([final_url]) or [None])[0]
            if target and not self.urlhelp.looks_non_html(target):
                key = self.urlhelp.host_key(self.urlhelp.host_of(target))
                if self.accepted[key] < a.max_pages_per_host * 2:
                    self.accepted[key] += 1
                    self.enqueue_found(item, [target])
            stats["redirected"] += 1
            return "redirected"
        if kind == "rate_limited":
            stats["rate_limited"] += 1
            self.retry_later.add(url)
            self.claimed_open.discard(url)
            self.frontier.cooldown(st, time.monotonic() + 600)
            # the rest of this host's queue waits with it: those claims stay in place too (they time out = retry later)
            for _, _, u in st.urls:
                self.retry_later.add(u)
                self.claimed_open.discard(u)
            self.frontier.queued -= len(st.urls)
            st.urls.clear()
            await self.redis_touch(st, 600)
            item["done"] = []
            return "rate_limited"
        if kind == "not_scrapable":
            item["seen"].append(url)
            item["done"].append(url)
            stats["skipped_not_scrapable"] += 1
            return "not_scrapable"
        if kind == "failed":
            item["done"].append(url)
            stats["errors"] += 1
            return "failed"

        # ---- parse in the process pool
        stats["downloaded"] += 1
        stats["bytes"] += len(content)
        from crawl_lib.parse import process_page
        job = {"url": url, "final_url": final_url, "html": content, "charset": charset, "x_robots": x_robots,
               "keep_text": bool(self.web_text_server or self.dump), "parse_timeout": a.parse_timeout}
        content = None
        async with self.parse_sem:
            try:
                res = await asyncio.wait_for(self.loop.run_in_executor(self.pool, process_page, job), a.parse_timeout + 15)
            except BrokenProcessPool:
                self.note("WARNING: a parser process died (out of memory?); restarting the pool")
                self.rebuild_pool()
                raise
        job = None

        page_url = res["page_url"]
        own_key = self.urlhelp.host_key(self.urlhelp.host_of(page_url))
        if page_url != url:
            item["seen"].append(url)   # the old url (a same-host redirect) is recorded as seen; the page lives under its final url
        status = res["status"]
        directives = set(res["directives"])
        stats["status_" + status] += 1
        if res["lang"]:
            stats["lang_" + res["lang"]] += 1

        if status == "not_indexed_language":
            logs.append(f"Language Not in English {page_url}")
            logs.append(f"Misc Language {res['lang']} {page_url}")
            item["seen"].append(page_url)
            if self.english_only:
                # A site whose page isn't English: fetch nothing more from it and don't follow its links (they lead to more of the same)
                self.foreign_hosts.add(own_key)
                res["candidates"] = {}
        elif status in ("noindex", "no_text", "short_text"):
            item["seen"].append(page_url)
        elif status == "indexed":
            if res["lang"] and res["lang"] != "en":
                logs.append(f"Misc Language {res['lang']} {page_url}")
            item["pages"].append({"url": page_url, "title": res["title"], "icon": res["icon"], "counts": res["counts"]})
            item["refs_from"] = page_url   # urls_references rows are only written for stored pages (like utils.store)
            item["refs"] = res["links"]
            stats["indexed"] += 1
        if res["external"]:
            item["external"] = res["external"]  # reference_count += 1 for every stored url this page links to on another domain
            item.setdefault("refs_from", None)

        if self.web_text_server and res["text"] and "noarchive" not in directives and status in ("indexed", "noindex", "not_indexed_language"):
            asyncio.create_task(self.send_text(page_url, res))

        # links: choose a few, shallow and spread over many hosts
        self.enqueue_found(item, self.selected_links(own_key, res["candidates"]))

        logs.append(f"Scraped {page_url}")
        item["done"].append(url)
        stats["pages"] += 1
        if self.dump:
            self.dump.write(json.dumps({"url": page_url, "title": res["title"], "lang": res["lang"], "status": status, "reason": res["reason"],
                                        "text_len": res["text_len"], "links": len(res["links"]), "words": len(res["counts"][0]) if res["counts"] else 0,
                                        "text": (res["text"] or "")[:2000], "timings": res["timings"]}) + "\n")
        return "ok"

    async def send_text(self, url, res):
        try:
            await asyncio.to_thread(self.utils.send_page_text, self.web_text_server, url, text=res["text"], title=res["title"])
        except Exception as e:
            self.writer.submit({"logs": [f"Error Web text storage failed {e!r} {url}"]})

    # ------------------------------------------------------------------ main loop
    def request_stop(self, reason):
        if not self.stop_requested:
            self.stop_requested = True
            self.stop_reason = reason
            self.note(f"stopping ({reason}): finishing in-flight pages, flushing, releasing claims")
            self.wake.set()

    def rss_line(self):
        parent = rss_mb(os.getpid())
        kids = [rss_mb(pid) for pid in list(getattr(self.pool, "_processes", {}) or {})]
        return parent, kids

    def done_count(self):
        s = self.stats
        return s["pages"] + s["errors"] + s["skipped_not_scrapable"] + s["robots_blocked"] + s["blocked"] + s["redirected"] + s["rate_limited"]

    async def progress(self):
        a = self.args
        last_t, last_n = time.monotonic(), 0
        while True:
            await asyncio.sleep(a.progress_interval)
            now = time.monotonic()
            s = self.stats
            done = self.done_count()
            elapsed = now - self.t_start
            rate_now = (done - last_n) / (now - last_t) * 60
            last_t, last_n = now, done
            parent, kids = self.rss_line()
            dbq = ""
            if self.db_mode and self.queue is not None:
                # the queue estimate is a courtesy: a short bound and its own connection, the progress line never waits for the database
                try:
                    est = await asyncio.wait_for(asyncio.to_thread(self.queue.queue_estimate), 3)
                except Exception:
                    est = None
                dbq = f" dbq~{'?' if est is None else est}"
            if self.db_mode and self.writer is not None:
                age = now - self.writer.last_ok
                if age > a.db_watchdog and (self.writer.pending() or self.writer.flush_started is not None):
                    self.note(f"WATCHDOG: no database flush completed for {age:.0f}s with {self.writer.pending()} items waiting: aborting the writer's connection")
                    self.writer.abort_flush()
                    self.writer.last_ok = now  # complain again after another full interval
                dbq += f" lastflush={now - self.writer.last_ok:.0f}s"
            skipped = s["skipped_not_scrapable"] + s["robots_blocked"] + s["blocked"] + s["redirected"]
            self.note(f"[{elapsed:5.0f}s] done={done} stored={s['indexed']} parsed={s['pages']} skipped={skipped} err={s['errors']} | "
                      f"{rate_now:.0f}/min now, {done / max(elapsed, 1) * 60:.0f}/min avg | active={len(self.active)} frontier={self.frontier.queued} "
                      f"hosts={len(self.frontier.hosts)}{dbq} wq={self.writer.pending()} | rss={parent + sum(kids):.0f}MB (main {parent:.0f} + {len(kids)} parsers {sum(kids):.0f})")

    def task_done(self, task):
        self.active.discard(task)
        self.wake.set()

    async def run(self):
        a = self.args
        loop = self.loop
        for sig, name in ((signal.SIGINT, "SIGINT"), (signal.SIGTERM, "SIGTERM")):
            loop.add_signal_handler(sig, self.request_stop, name)

        writer_task = asyncio.create_task(self.writer.run())
        progress_task = asyncio.create_task(self.progress())
        loader_task = None
        self.writer.submit({"logs": ["Started scraping"]})

        # ---- initial urls
        seeds = self.utils.clean_links(read_url_file(a.seed_file)) if a.seed_file else []
        if a.url or a.file:
            for u in ([a.url] if a.url else read_url_file(a.file)):
                u = u.strip()
                if u:
                    self.seen_mem.add(u)
                    self.add_url(u, claimed=False)
        if seeds:
            if self.db_mode:
                # all seeds go into url_queue (one explicit-id insert per 2000), but only claim_batch at a time are popped into memory:
                # a hard kill loses the popped urls only, the rest of the seeds are still queued
                n = await self.queue_call(self.queue.enqueue, seeds, timeout=300)
                self.seed_pending = list(dict.fromkeys(seeds))
                self.note(f"seeds: {len(seeds)} given, {n} newly queued ({len(seeds) - n} already stored or queued); claiming them first, {a.claim_batch} at a time")
            else:
                for u in seeds:
                    self.seen_mem.add(u)
                    self.add_url(u, claimed=False)
        if self.db_mode and (not self.not_queue_sourced or self.seed_pending):
            loader_task = asyncio.create_task(self.loader())
        self.note(f"crawling: fetchers={a.fetchers} parsers={a.parsers} per-host cap={a.max_pages_per_host} min-delay={a.min_delay}s "
                  f"mode={'nodb' if a.nodb else 'database'} initial urls={self.frontier.queued} seeds waiting={len(self.seed_pending)}")

        # ---- dispatch loop
        idle_since = None
        while True:
            if self.stop_requested:
                break
            if a.max_pages and self.started >= a.max_pages:
                self.request_stop("max pages reached")
                break
            if a.max_time and time.monotonic() - self.t_start >= a.max_time:
                self.request_stop("max time reached")
                break
            now = time.monotonic()
            while len(self.active) < a.fetchers and not (a.max_pages and self.started >= a.max_pages):
                got = self.frontier.pop_ready(now)
                if not got:
                    break
                st, url = got
                self.started += 1
                task = loop.create_task(self.run_one(st, url))
                self.active.add(task)
                task.add_done_callback(self.task_done)
            for u in self.frontier.drain_dropped():
                self.stats["dropped_host_cap"] += 1
                if u in self.claimed_open:
                    self.writer.submit({"done": [u]})

            # nothing left anywhere?
            if not self.active and self.frontier.queued == 0 and self.frontier.next_wakeup() is None and self.writer.pending() == 0:
                if loader_task is None or loader_task.done() or (a.exit_when_empty and self.queue_empty):
                    if idle_since is None:
                        idle_since = time.monotonic()
                    if time.monotonic() - idle_since > 1.0:
                        self.request_stop("nothing left to crawl")
                        break
            else:
                idle_since = None

            nxt = self.frontier.next_wakeup()
            timeout = 0.5 if nxt is None else min(max(nxt - time.monotonic(), 0.005), 0.5)
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), timeout)
            except asyncio.TimeoutError:
                pass

        # ---- shutdown: let in-flight pages finish (they're mid-parse), then flush and hand back claims. Every wait is bounded; after a
        # SIGINT/SIGTERM the whole thing takes ~20 s at most even with a dead database (a hard-exit timer backs that up)
        urgent = self.stop_reason in ("SIGINT", "SIGTERM")
        if urgent:
            hard_exit = threading.Timer(22.0, self.hard_exit)
            hard_exit.daemon = True
            hard_exit.start()
        t_stop = time.monotonic()
        stage = (lambda name: self.note(f"shutdown +{time.monotonic() - t_stop:.1f}s: {name}")) if urgent else (lambda name: None)
        if self.active:
            grace = 5 if urgent else 120
            done, pending = await asyncio.wait(set(self.active), timeout=grace)
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.wait(pending, timeout=1)
        stage('in-flight pages done')
        if loader_task:
            loader_task.cancel()
            await asyncio.wait({loader_task}, timeout=1)
        stage('loader stopped')
        progress_task.cancel()
        await asyncio.wait({progress_task}, timeout=1)
        stage('progress stopped')
        for u in self.frontier.drain_dropped():
            if u in self.claimed_open:
                self.writer.submit({"done": [u]})
        self.writer.submit({"logs": ["Finished scraping"]})
        await self.writer.stop(timeout=4 if urgent else 120)
        stage('final flush over')
        if self.db_mode:
            await self.release_claims(timeout=4 if urgent else 60)
        stage('claims released')
        writer_task.cancel()
        await asyncio.wait({writer_task}, timeout=1)
        stage('writer stopped')
        self.stop_pool()
        self.report()

    def hard_exit(self):
        print("WATCHDOG: shutdown did not finish in time: exiting now (unfinished claims time out / were not handed back)", file=sys.stderr, flush=True)
        os._exit(1)

    def stop_pool(self):
        """Don't wait for parser processes (one may be stuck in a parse): ask them to stop, then end whatever is left"""
        try:
            procs = list((getattr(self.pool, "_processes", None) or {}).values())
            self.pool.shutdown(wait=False, cancel_futures=True)
            for p in procs:
                if p.is_alive():
                    p.terminate()
        except Exception:
            pass

    async def release_claims(self, timeout=60):
        """Hand back every claim we still hold (queued but never started, or cut off mid-flight); rate-limited ones stay claimed on purpose"""
        if self.not_queue_sourced and not self.stats["seeds_claimed"]:
            return
        if self.db is not None and not self.db.claims:
            # claimless queue (no claimed_at): popped urls are gone from it, so everything unfinished, rate-limited ones too, goes back in
            release = list(self.claimed_open | self.retry_later)
        else:
            release = [u for u in self.claimed_open if u not in self.retry_later]
        if not release:
            return
        try:
            from crawl_lib.db import QueueDb
            fresh = QueueDb(claims=self.db.claims if self.db is not None else None)  # its own connection: the shared one may be stuck in a hung call
            try:
                await self.queue_call(fresh.release, release, timeout=timeout, queue=fresh)
            finally:
                fresh.close()
            self.note(f"released {len(release)} unfinished claims back to url_queue")
        except Exception as e:
            print(f"could not hand back {len(release)} claims (they time out in {self.utils.CLAIM_TIMEOUT}): {e!r}", file=sys.stderr)

    def report(self):
        s = self.stats
        elapsed = time.monotonic() - self.t_start
        hosts_fetched = sum(1 for h in self.frontier.hosts.values() if h.fetched)
        done = self.done_count()
        langs = {k[5:]: v for k, v in s.items() if k.startswith("lang_")}
        self.note("---- summary ----")
        self.note(f"elapsed {elapsed:.1f}s, urls processed {done} ({done / max(elapsed, 1e-9) * 60:.0f}/min), pages parsed {s['pages']} ({s['pages'] / max(elapsed, 1e-9) * 60:.0f}/min), stored {s['indexed']}")
        self.note(f"distinct hosts fetched from: {hosts_fetched}; downloaded {s['bytes'] / 1e6:.1f} MB; robots.txt fetched {self.robots.fetched} (errors {self.robots.errors})")
        self.note("outcomes: " + ", ".join(f"{k}={v}" for k, v in sorted(s.items()) if k != "bytes" and not k.startswith("lang_")))
        if langs:
            self.note("languages: " + ", ".join(f"{k}={v}" for k, v in sorted(langs.items(), key=lambda kv: -kv[1])[:12]))
        if self.db_mode:
            w = self.writer.stats
            self.note(f"db: {w['flushes']} commits, {w['rows']} posting rows, {w['write_seconds']:.1f}s in writes")
        try:
            import resource
            ru_c = resource.getrusage(resource.RUSAGE_CHILDREN)
            self.note(f"peak RSS: main {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MB, largest parser {ru_c.ru_maxrss / 1024:.0f} MB; "
                      f"cpu seconds: main {time.process_time():.1f}, parsers {ru_c.ru_utime + ru_c.ru_stime:.1f}")
        except Exception:
            pass


async def amain(args):
    crawler = Crawler(args)
    try:
        await crawler.setup()
        await crawler.run()
    finally:
        await crawler.close()
        if crawler.db_mode and (crawler.hung or crawler.stop_reason in ("SIGINT", "SIGTERM")):
            # a database call may still be blocked in a worker thread: asyncio.run() would wait for it at exit. Everything is flushed/handed back by now
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(0)


def main(argv=None):
    load_dotenv(os.path.join(ROOT, ".env"))
    args = parse_args(argv)
    asyncio.run(amain(args))


if __name__ == "__main__":
    main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)  # abandoned (watchdog-timed-out) database threads must not keep the process alive
