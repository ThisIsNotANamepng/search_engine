"""In-process, read-only memory index over the `urls` table (used by search.py).

Holds, for every searchable page (total_tokens IS NOT NULL):
  * numeric arrays indexed by url id: total_tokens, reference_count, a validity mask,
    plus N and avgdl (so BM25 needs no `SELECT avg(total_tokens)` and no join with `urls`),
  * url and title strings (needed to build result rows and the title/host boost),
  * an inverted index title-word -> ids and host-word -> ids (the "nav" candidates: pages whose
    title or hostname names the query although their body postings do not).

It never writes to the database. A daemon thread loads it once and then refreshes it every
SEARCH_MEMINDEX_REFRESH seconds: one cheap numeric SELECT (id, total_tokens, reference_count)
plus a SELECT of url/title only for ids it has not seen yet (titles/urls do not change).
Needs numpy; without it `available()` is False and search.py keeps using SQL for everything.
"""
import os
import re
import threading
import time
from types import SimpleNamespace

try:
    import numpy as np
except ImportError:  # pragma: no cover - optional dependency
    np = None

REFRESH_SECONDS = float(os.environ.get("SEARCH_MEMINDEX_REFRESH", "180"))
STRING_CHUNK = 20000

_NUM_SQL = ("SELECT id, total_tokens, COALESCE(reference_count, 0) FROM urls "
            "WHERE total_tokens IS NOT NULL")
_STR_SQL = "SELECT id, url, title FROM urls WHERE id = ANY(%s)"

_TITLE_TOKEN = re.compile(r"[0-9a-z]+")
_HOST_SPLIT = re.compile(r"[^0-9a-z\u0080-\uffff]+")
_URL_HOST = re.compile(r"^[a-z][a-z0-9+.-]*://([^/?#]*)([^?#]*)")
_SIMPLE = re.compile(r"[0-9a-z]+")
_EMPTY = None


def available():
    return np is not None


def title_tokens(title):
    """Maximal [0-9a-z] runs of the lower-cased title (what a whole-word match can hit)."""
    return set(_TITLE_TOKEN.findall((title or "").lower()))


def host_and_depth_tokens(url):
    """(host tokens, path depth) with the same semantics as search._host_and_depth + title_boost."""
    m = _URL_HOST.match((url or "").lower())
    if not m:
        return set(), 99
    host = m.group(1).rsplit("@", 1)[-1].split(":")[0]
    depth = len([p for p in m.group(2).split("/") if p])
    return set(_HOST_SPLIT.split(host)) - {""}, min(depth, 99)


def host_tokens(url):
    return host_and_depth_tokens(url)[0]


def word_in_text(word, text):
    """Whole-word match (same as search._word_in_text)."""
    if not word.isascii():
        return word in text
    return re.search(r"(?<![0-9a-z])" + re.escape(word) + r"(?![0-9a-z])", text) is not None


class MemIndex:
    def __init__(self):
        self.lock = threading.Lock()
        self.urls = []          # id -> url (None = unknown)
        self.titles = []        # id -> title
        self.depth = bytearray()  # id -> path depth (capped at 99)
        self.title_ix = {}      # token -> id (single doc) | list of ids
        self.host_ix = {}
        self._arrs = {}         # (kind, token) -> (length, np.int32 array): query-time cache
        self._slow = {}         # non-plain word -> title ids (linear scan, memoised)
        self._blob_cache = None  # (n_pages, joined lower-cased titles, offsets)
        self.snap = None        # SimpleNamespace(M, valid, tt, ref, depth, n_docs, avgdl)
        self.loaded_at = 0.0
        self.error = None

    # -- building ----------------------------------------------------------
    @staticmethod
    def _add(ix, token, i):
        cur = ix.get(token)
        if cur is None:
            ix[token] = i
        elif type(cur) is int:
            ix[token] = [cur, i]
        else:
            cur.append(i)

    def add_strings(self, rows):
        """rows: (id, url, title). Extends the string tables and the token indexes."""
        with self.lock:
            for i, url, title in rows:
                if i >= len(self.urls):
                    grow = i + 1 - len(self.urls)
                    self.urls.extend([None] * grow)
                    self.titles.extend([None] * grow)
                    self.depth.extend(b"\x63" * grow)
                if self.urls[i] is not None:
                    continue
                self.urls[i] = url or ""
                self.titles[i] = title or ""
                for t in title_tokens(title):
                    self._add(self.title_ix, t, i)
                htoks, depth = host_and_depth_tokens(url)
                self.depth[i] = depth
                for t in htoks:
                    self._add(self.host_ix, t, i)

    def set_numbers(self, ids, tts, refs):
        """Atomically publish the numeric arrays (ids/tts/refs: parallel sequences)."""
        ids = np.asarray(ids, dtype=np.int64)
        M = int(ids.max()) + 1 if ids.size else 1
        valid = np.zeros(M, dtype=bool)
        tt = np.zeros(M, dtype=np.int32)
        ref = np.zeros(M, dtype=np.int32)
        valid[ids] = True
        tt[ids] = np.asarray(tts, dtype=np.int64)
        ref[ids] = np.asarray(refs, dtype=np.int64)
        depth = np.full(M, 99, dtype=np.uint8)
        have = np.frombuffer(bytes(self.depth[:M]), dtype=np.uint8)
        depth[:have.size] = have
        n_docs = int(ids.size)
        avgdl = float(tt[valid].mean()) if n_docs else 1.0
        self.snap = SimpleNamespace(M=M, valid=valid, tt=tt, ref=ref, depth=depth, n_docs=max(n_docs, 1),
                                    avgdl=max(avgdl, 1.0))
        self.loaded_at = time.time()

    def refresh(self, run):
        """run(fn) executes fn(cursor) on a pooled connection (read-only SELECTs only)."""
        def numbers(cur):
            cur.execute(_NUM_SQL)
            return cur.fetchall()
        rows = run(numbers)
        ids = [r[0] for r in rows]
        have = len(self.urls)
        missing = [i for i in ids if i >= have or self.urls[i] is None]
        for s in range(0, len(missing), STRING_CHUNK):
            chunk = missing[s:s + STRING_CHUNK]

            def strings(cur, chunk=chunk):
                cur.execute(_STR_SQL, (chunk,))
                return cur.fetchall()
            self.add_strings(run(strings))
        self.set_numbers(ids, [r[1] for r in rows], [r[2] for r in rows])

    # -- querying ----------------------------------------------------------
    def _arr(self, kind, token):
        """Ids whose title (kind 't') / hostname (kind 'h') contains `token`, as a cached int32 array."""
        ix = self.title_ix if kind == "t" else self.host_ix
        v = ix.get(token)
        if v is None:
            return _no_ids()
        n = 1 if type(v) is int else len(v)
        key = (kind, token)
        hit = self._arrs.get(key)
        if hit is not None and hit[0] == n:
            return hit[1]
        a = np.sort(np.array([v] if type(v) is int else v, dtype=np.int32))
        if len(self._arrs) > 20000:
            self._arrs.clear()
        self._arrs[key] = (n, a)
        return a

    @staticmethod
    def _member(ids, a):
        """Boolean mask: which of `ids` occur in the sorted array `a`."""
        pos = np.minimum(np.searchsorted(a, ids), a.size - 1)
        return a[pos] == ids

    def hits(self, words, ids):
        """For each page id: how many of `words` appear in its title (whole word) / its hostname.
        Same semantics as title_boost()."""
        ids = np.asarray(ids)
        t = np.zeros(ids.size, dtype=np.int16)
        h = np.zeros(ids.size, dtype=np.int16)
        for w in words:
            if _SIMPLE.fullmatch(w):
                a = self._arr("t", w)
                if a.size:
                    t += self._member(ids, a)
            else:  # non-ascii / punctuation: substring or regex semantics, checked on these pages only
                for j, i in enumerate(ids.tolist()):
                    title = self.titles[i] if i < len(self.titles) else None
                    if title and word_in_text(w, title.lower()):
                        t[j] += 1
            a = self._arr("h", w)
            if a.size:
                h += self._member(ids, a)
        return t, h

    def _slow_title_ids(self, w):
        """Ids of pages whose lower-cased title contains the non-plain word `w` (substring for non-ASCII,
        whole-word regex for ASCII with punctuation). Non-ASCII words use one C-speed str.find sweep over
        a joined-title blob; results are memoised until new pages arrive."""
        key = (w, len(self.urls))
        hit = self._slow.get(key)
        if hit is not None:
            return hit
        if w.isascii():
            pat = re.compile(r"(?<![0-9a-z])" + re.escape(w) + r"(?![0-9a-z])")
            ids = [i for i, t in enumerate(self.titles) if t and pat.search(t.lower())]
        else:
            blob, starts = self._blob()
            ids, pos = [], blob.find(w)
            while pos != -1:
                j = int(np.searchsorted(starts, pos, side="right")) - 1
                if not ids or ids[-1] != j:
                    ids.append(j)
                pos = blob.find(w, pos + 1)
        hit = np.asarray(ids, dtype=np.int32)
        if len(self._slow) > 500:
            self._slow.clear()
        self._slow[key] = hit
        return hit

    def _blob(self):
        """(lower-cased titles joined by NUL, start offset of each id), rebuilt when pages were added."""
        b = self._blob_cache
        if b is None or b[0] != len(self.urls):
            titles = [(t or "").lower() for t in self.titles]
            lens = np.fromiter((len(t) + 1 for t in titles), dtype=np.int64, count=len(titles))
            starts = np.concatenate(([0], np.cumsum(lens)[:-1]))
            b = (len(self.urls), "\x00".join(titles), starts)
            self._blob_cache = b
        return b[1], b[2]

    def nav(self, words, exclude=(), k=100):
        """Pages whose title or hostname contains >= half of `words` as whole words, most-referenced
        first, at most k. Returns (ids, title_hits, host_hits) arrays (mirrors score_nav's test)."""
        snap = self.snap
        none = (np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int16), np.zeros(0, dtype=np.int16))
        if snap is None or not words:
            return none
        need = max(1, len(words) / 2.0)
        t_parts, h_parts = [], []
        for w in words:
            if _SIMPLE.fullmatch(w):
                a = self._arr("t", w)
                if a.size:
                    t_parts.append(a)
            else:      # non-plain word: substring/regex semantics -> scan titles once per word, memoised
                t_parts.append(self._slow_title_ids(w))
            a = self._arr("h", w)
            if a.size:
                h_parts.append(a)
        ok = []
        for parts in (t_parts, h_parts):
            if parts:
                u, c = np.unique(np.concatenate(parts), return_counts=True)
                ok.append(u[c >= need])
        if not ok:
            return none
        cand = np.unique(np.concatenate(ok)).astype(np.int64)
        cand = cand[cand < snap.M]
        cand = cand[snap.valid[cand]]
        if len(exclude) and cand.size:
            ex = np.sort(np.fromiter(exclude, dtype=np.int64, count=len(exclude)))
            cand = cand[~self._member(cand, ex)]
        if not cand.size:
            return none
        order = np.lexsort((cand, -snap.ref[cand].astype(np.int64)))[:k]   # ref DESC, id
        cand = cand[order]
        t, h = self.hits(words, cand)
        return cand, t, h

    def nav_candidates(self, words, exclude=(), k=100):
        """List form of nav(): just the ids."""
        return self.nav(words, exclude, k)[0].tolist()

    def top_title_tokens(self, n):
        """The n most common title words (alphabetic, >= 3 chars): a free proxy for popular query words."""
        import heapq
        cnt = ((1 if type(v) is int else len(v), t) for t, v in list(self.title_ix.items())
               if len(t) >= 3 and t.isalpha())
        return [t for _, t in heapq.nlargest(n, cnt)]

    @property
    def ready(self):
        return self.snap is not None

    def row(self, i):
        """(url, title) for an id, or None if the strings are not loaded."""
        if i < len(self.urls) and self.urls[i] is not None:
            return self.urls[i], self.titles[i]
        return None


def _no_ids():
    global _EMPTY
    if _EMPTY is None:
        _EMPTY = np.zeros(0, dtype=np.int32)
    return _EMPTY


# ---- background loader -------------------------------------------------------

_index = MemIndex()
_state = {"pid": None, "thread": None}
_state_lock = threading.Lock()


def get():
    """The shared index if it has been loaded, else None."""
    return _index if _index.ready else None


def start(run, interval=None, on_ready=None):
    """Start (once per process) the daemon thread that loads and refreshes the index.
    `on_ready()` (optional) is called once, in that thread, after the first successful load."""
    if np is None:
        return False
    interval = REFRESH_SECONDS if interval is None else interval
    with _state_lock:
        if _state["pid"] == os.getpid() and _state["thread"] is not None and _state["thread"].is_alive():
            return True
        stop = threading.Event()
        target = _index

        def loop():
            first = True
            while not stop.is_set():
                try:
                    target.refresh(run)
                    target.error = None
                    wait = interval
                    if first and on_ready is not None:
                        first = False
                        try:
                            on_ready()
                        except Exception:
                            pass
                except Exception as e:  # keep serving from the previous snapshot
                    target.error = repr(e)
                    wait = min(interval, 30.0)
                stop.wait(wait)

        th = threading.Thread(target=loop, name="search-memindex", daemon=True)
        _state.update(pid=os.getpid(), thread=th, stop=stop)
        th.start()
    return True


def reset():
    """Test helper: forget everything (and ask the loader thread to stop)."""
    global _index
    with _state_lock:
        if _state.get("stop") is not None:
            _state["stop"].set()
        _state.update(pid=None, thread=None)
    _index = MemIndex()
    return _index
