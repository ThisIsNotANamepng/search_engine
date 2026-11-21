"""
Single-parse page extraction, plus the CPU-heavy per-page job that fast_crawl.py runs in a process pool.

extract_page() has the same contract as utils.extract_data_from_html() (which now delegates to it):
    [combined_text, links, title, icon_link, directives]
but parses the HTML once with lxml (the old version parsed with BeautifulSoup and again inside trafilatura, ~130 ms
vs ~10 ms for the parse alone on a Raspberry Pi) and hands the same tree to trafilatura.

Importing this module must stay cheap and must not import utils at module level (utils imports this lazily).
"""

import inspect
import os
import signal
import time
from urllib.parse import urljoin

import lxml.html
from lxml import etree
import trafilatura

_SKIP_LINK_RELS = {"nofollow", "sponsored", "ugc"}
# Pages bigger than this skip trafilatura's slow fallback extractors (a 7 MB cnn.com homepage took 5 s with them, 3.9 s without)
BIG_PAGE_BYTES = 1_000_000
MAX_LINKS_PER_PAGE = 5000  # cleaned links kept per page (references + enqueue candidates); a link farm shouldn't flood the db

_utils = None


def _u():
    global _utils
    if _utils is None:
        import utils
        _utils = utils
    return _utils


def _load_tree(body, charset=None):
    """bytes/str -> lxml tree (or None if there's nothing parseable). Uses trafilatura's loader, which sniffs the encoding"""
    if isinstance(body, (bytes, bytearray)) and charset and charset.lower().replace("_", "-") not in ("utf-8", "utf8"):
        try:
            body = bytes(body).decode(charset, errors="replace")
        except LookupError:
            pass
    if isinstance(body, bytearray):
        body = bytes(body)
    try:
        return trafilatura.load_html(body)
    except Exception:
        return None


def _icon_href(tree):
    """First <link> that bs4's old four-step search would have found, priority order kept: 'shortcut icon', 'icon', exact 'icon', contains 'icon'"""
    best, best_rank = None, 99
    for link in tree.iter("link"):
        rel = link.get("rel")
        if not rel:
            continue
        rel = rel.lower()
        tokens = rel.split()
        if " ".join(tokens) == "shortcut icon":
            rank = 0
        elif "icon" in tokens:
            rank = 1
        elif any("icon" in t for t in tokens):
            rank = 3
        else:
            continue
        if rank < best_rank:
            best, best_rank = link, rank
            if rank == 0:
                break
    return best


def extract_page(body, url, x_robots_tag="", charset=None, fast=None):
    """
    Same contract as utils.extract_data_from_html:
        [combined_text, links, title, icon_link, directives]
    body may be bytes (preferred; charset = the Content-Type charset, if any) or str.
    fast: skip trafilatura's fallback extractors. None = only for pages over BIG_PAGE_BYTES
    """
    utils = _u()
    tree = _load_tree(body, charset)
    if tree is None:
        return [None, [], url, None, utils.robots_directives([x_robots_tag])]

    title_el = tree.find(".//title")
    title = url if title_el is None else (title_el.text if len(title_el) == 0 else "".join(title_el.itertext()))
    if title:
        title = " ".join(title.split()) or url  # "\n   Rust   \n" -> "Rust"; titles are shown as one line

    icon_link = None
    icon_el = _icon_href(tree)
    if icon_el is not None:
        href = icon_el.get("href")
        icon_link = urljoin(url, href) if href else None

    meta_robots = [m.get("content") or "" for m in tree.iter("meta") if (m.get("name") or "").lower() in ("robots", utils.ROBOTS_NAME)]
    directives = utils.robots_directives([x_robots_tag, *meta_robots])

    links = []
    if "nofollow" not in directives:
        for a in tree.xpath("//a[@href][not(ancestor::head) and not(ancestor::noscript)]"):
            rel = a.get("rel")
            if rel and _SKIP_LINK_RELS & {r.lower() for r in rel.split()}:
                continue
            try:
                links.append(urljoin(url, a.get("href")))
            except ValueError:  # e.g. http://[broken ipv6 (the old code crashed the whole page on these)
                continue

    if fast is None:
        fast = (len(body) if body is not None else 0) > BIG_PAGE_BYTES
    # trafilatura works on (a copy of) the tree we hand it; everything we need from the tree has been read above.
    # Comments and metadata aren't indexed, so don't pay for them
    try:
        combined_text = trafilatura.extract(tree, fast=fast, include_comments=False)
    except Exception:
        combined_text = None

    return [combined_text, links, title, icon_link, directives]


# ---------------------------------------------------------------- process pool job

_gate = None          # tokenizer.language_gate if the tokenizer has one
_tokenize = None
_tok_kwargs_lang = None
_base_cache = {}
_sigalrm_ok = hasattr(signal, "SIGALRM")


class ParseTimeout(Exception):
    pass


def _on_alarm(signum, frame):
    raise ParseTimeout("parse timed out")


def _load_components():
    """Import and warm everything slow (nltk stopwords, langdetect profiles, public suffix list)"""
    global _gate, _tokenize, _tok_kwargs_lang
    utils = _u()
    import tokenizer
    from langdetect import DetectorFactory
    DetectorFactory.seed = 0  # langdetect is randomized by default: same page, same answer
    _gate = getattr(tokenizer, "language_gate", None)
    _tokenize = tokenizer.tokenize_all_counts
    try:
        params = inspect.signature(_tokenize).parameters
        _tok_kwargs_lang = next((n for n in ("lang", "language") if n in params), None)
    except (TypeError, ValueError):
        _tok_kwargs_lang = None
    utils.determine_language("warm up the language detector with an english sentence")
    _tokenize("warm up the tokenizer")
    utils.get_base_domain("https://warm.up.example.co.uk")  # loads the public suffix list


def worker_init():
    """Runs once in each pool process: ignore Ctrl-C (the parent shuts the pool down), install the parse timeout, preload everything"""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    if _sigalrm_ok:
        signal.signal(signal.SIGALRM, _on_alarm)
    _load_components()


def _base_domain(host_url):
    from urllib.parse import urlsplit
    try:
        host = urlsplit(host_url).hostname or ""
    except ValueError:
        return ""
    base = _base_cache.get(host)
    if base is None:
        base = _u().get_base_domain(host_url)
        if len(_base_cache) > 50000:
            _base_cache.clear()
        _base_cache[host] = base
    return base


def language_decision(text):
    """(should_index, lang, reason). Uses tokenizer.language_gate when it exists, otherwise the old rule: index only English"""
    if _gate is not None:
        should_index, lang, reason = _gate(text)
        return bool(should_index), lang, reason
    lang = _u().determine_language(text)
    return lang == "en", lang, "" if lang == "en" else "not english"


def process_page(job):
    """
    Everything CPU-heavy about one downloaded page. job: dict(url, final_url, html, charset, x_robots, keep_text, parse_timeout)
    Returns a plain dict (picklable, no big objects):
        page_url      the url to store the page under (cleaned final url)
        status        'indexed' | 'not_indexed_language' | 'noindex' | 'no_text' | 'short_text'
        lang, reason  from the language gate (None when there was no text)
        title, icon, directives(list), text (only if job['keep_text']), text_len
        counts        (word_counts, bigram_counts, trigram_counts, prefix_counts) as dicts, only when status == 'indexed'
        links         every cleaned link on the page (kept for urls_references / reference_count)
        external      distinct cleaned links to another base domain
        candidates    {host: [shallowest urls]} to enqueue (non-HTML extensions removed)
        timings       seconds spent: extract, language, tokenize
    """
    from crawl_lib import urls as urlhelp
    utils = _u()
    t0 = time.perf_counter()
    use_alarm = _sigalrm_ok and job.get("parse_timeout") and signal.getsignal(signal.SIGALRM) is _on_alarm
    if use_alarm:
        signal.alarm(int(job["parse_timeout"]))
    try:
        if _tokenize is None:
            _load_components()  # called directly (tests), not through the pool
        url, final_url = job["url"], job["final_url"]
        text, links, title, icon, directives = extract_page(job["html"], final_url, job.get("x_robots", ""), job.get("charset"))
        t1 = time.perf_counter()

        page_url = (utils.clean_links([final_url]) or [url])[0]
        cleaned = utils.clean_links(links)[:MAX_LINKS_PER_PAGE]
        base = _base_domain(page_url)
        external = []
        by_host = {}
        for link in cleaned:
            if _base_domain(link) != base:
                external.append(link)
            if not urlhelp.looks_non_html(link):
                by_host.setdefault(urlhelp.host_of(link), []).append(link)
        candidates = {}
        for host, host_links in by_host.items():
            if host:
                host_links.sort(key=urlhelp.path_depth)
                candidates[host] = host_links[:6]

        out = {"page_url": page_url, "status": "no_text", "lang": None, "reason": None, "title": title, "icon": icon,
               "directives": sorted(directives), "text": None, "text_len": 0, "counts": None, "links": cleaned,
               "external": external, "candidates": candidates, "timings": [t1 - t0, 0.0, 0.0]}
        if not text:
            return out
        out["text_len"] = len(text)
        if job.get("keep_text"):
            out["text"] = text

        should_index, lang, reason = language_decision(text)
        t2 = time.perf_counter()
        out["lang"], out["reason"] = lang, reason
        out["timings"][1] = t2 - t1
        if not should_index:
            out["status"] = "not_indexed_language"
        elif "noindex" in directives:
            out["status"] = "noindex"
        elif len(text) < 4:
            out["status"] = "short_text"
        else:
            kwargs = {_tok_kwargs_lang: lang} if _tok_kwargs_lang else {}
            tokens = _tokenize(text, **kwargs)
            max_tok = utils.MAX_TOKEN_LENGTH
            out["counts"] = (
                {w: n for w, n in tokens[0].items() if 0 < len(w) <= max_tok},
                dict(tokens[1]),
                dict(tokens[2]),
                {p: n for p, n in tokens[3].items() if 0 < len(p) <= max_tok},
            )
            out["status"] = "indexed"
            out["timings"][2] = time.perf_counter() - t2
        return out
    finally:
        if use_alarm:
            signal.alarm(0)
