"""
In-memory crawl frontier: per-host queues with a politeness clock.

Rules enforced here (single-threaded, called only from the event loop):
  * at most one request in flight per host
  * a host is not handed out again until its delay has passed (delay = max(min_delay, robots Crawl-delay), set per host)
  * at most max_per_host pages per host per run; urls beyond that are rejected / dropped (returned in .dropped)
  * within a host the shallowest url goes first (homepage before /a/b/c)
"""

import heapq
import itertools

from crawl_lib.urls import host_key, host_of, path_depth


class HostState:
    __slots__ = ("key", "fetch_host", "urls", "next_time", "inflight", "fetched", "accepted", "delay", "blocked", "public", "in_heap")

    def __init__(self, key, fetch_host, delay):
        self.key = key
        self.fetch_host = fetch_host  # the hostname as it appears in urls (redis cooldown key, same as scrape.py)
        self.urls = []                # heap of (depth, seq, url)
        self.next_time = 0.0
        self.inflight = False
        self.fetched = 0              # pages attempted (counts toward the cap)
        self.accepted = 0             # urls this process queued for the host (frontier or db queue); caps enqueueing
        self.delay = delay
        self.blocked = None           # blocklist verdict, None = not checked yet
        self.public = None            # True/False once resolved
        self.in_heap = False


class Frontier:
    def __init__(self, max_per_host=4, min_delay=3.0, max_delay=30.0):
        self.max_per_host = max_per_host
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.hosts = {}
        self._heap = []
        self._seq = itertools.count()
        self.queued = 0
        self.dropped = []   # urls rejected after being accepted (host hit its cap): caller finishes their claims

    def state(self, url_or_host, create=True):
        host = host_of(url_or_host) if "/" in url_or_host else url_or_host
        key = host_key(host)
        st = self.hosts.get(key)
        if st is None and create:
            st = self.hosts[key] = HostState(key, host, self.min_delay)
        return st

    def is_full(self, st):
        return (st.fetched + len(st.urls) + (1 if st.inflight else 0)) >= self.max_per_host

    def add(self, url, now):
        """Queue a url. Returns True if accepted, False if its host is full / the url has no host"""
        if not host_of(url):
            return False
        st = self.state(url)
        if self.is_full(st):
            return False
        heapq.heappush(st.urls, (path_depth(url), next(self._seq), url))
        self.queued += 1
        st.accepted += 1
        if not st.in_heap and not st.inflight:
            self._push(st, max(st.next_time, now))
        return True

    def _push(self, st, when):
        st.in_heap = True
        heapq.heappush(self._heap, (when, next(self._seq), st.key))

    def pop_ready(self, now):
        """(state, url) for a host that's free and due, or None"""
        heap = self._heap
        while heap and heap[0][0] <= now:
            _, _, key = heapq.heappop(heap)
            st = self.hosts[key]
            st.in_heap = False
            if st.inflight or not st.urls:
                continue
            if st.fetched >= self.max_per_host:
                self._drop_all(st)
                continue
            _, _, url = heapq.heappop(st.urls)
            self.queued -= 1
            st.inflight = True
            return st, url
        return None

    def next_wakeup(self):
        return self._heap[0][0] if self._heap else None

    def complete(self, st, now, counted=True, delay=None):
        """The in-flight request finished (network part): start the host's delay clock (delay=0 when nothing was requested from the host)"""
        st.inflight = False
        if counted:
            st.fetched += 1
        st.next_time = now + (st.delay if delay is None else delay)
        self._reschedule(st)

    def requeue(self, st, url, when):
        """Couldn't use the slot (another crawler touched the host a moment ago): put the url back, try again at `when`"""
        st.inflight = False
        heapq.heappush(st.urls, (path_depth(url), next(self._seq), url))
        self.queued += 1
        st.next_time = when
        self._reschedule(st)

    def cooldown(self, st, until):
        st.next_time = max(st.next_time, until)

    def _reschedule(self, st):
        if not st.urls:
            return
        if st.fetched >= self.max_per_host:
            self._drop_all(st)
        elif not st.in_heap:
            self._push(st, st.next_time)

    def _drop_all(self, st):
        self.dropped.extend(u for _, _, u in st.urls)
        self.queued -= len(st.urls)
        st.urls.clear()

    def drain_dropped(self):
        out, self.dropped = self.dropped, []
        return out

    def pending_urls(self):
        """Every url still queued (not started)"""
        return [u for st in self.hosts.values() for _, _, u in st.urls]
