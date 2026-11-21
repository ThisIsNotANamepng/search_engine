#!/usr/bin/env python
"""Ranking + latency harness: legacy formula vs BM25 on the live index (read-only).

    set -a; . ./.env; set +a
    NLTK_DATA=~/nltk_data python tests/search_bench.py            # top 5 of each + latencies
    python tests/search_bench.py --quiet                          # latencies only
    python tests/search_bench.py -q "linux kernel" -q wikipedia   # custom queries
    python tests/search_bench.py --repeat 5                       # more samples per query
"""
import argparse
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import search  # noqa: E402

QUERIES = [
    "python", "linux kernel", "free software", "postgresql", "rust programming language",
    "bbc news", "raspberry pi", "web crawler", "hacker news", "wikipedia", "mozilla", "apache",
    "go programming", "node.js", "uwec", "eau claire",
    # multi-word / natural
    "open source operating system", "how to learn javascript", "debian linux distribution",
    "electronic frontier foundation privacy", "search engine", "machine learning",
    # typos / partial
    "kernal", "postgre", "pyhton", "wikipedai",
    # foreign / CJK
    "东京大学", "日本語 ニュース", "программирование", "noticias", "linux 内核",
    # single common/rare
    "the", "software", "zzzqxj",
]


def pct(values, p):
    values = sorted(values)
    if not values:
        return float("nan")
    return values[min(len(values) - 1, int(round(p / 100.0 * (len(values) - 1))))]


def clear_caches():
    search._cache.clear()
    search._df_cache.clear()
    search._corr_cache.clear()


def timed(q, mode, limit=5):
    t = time.perf_counter()
    rows = search.search_debug(q, limit, 0, mode=mode)
    return rows, (time.perf_counter() - t) * 1000


def show(rows, mode):
    if not rows:
        print("    (no results)")
    for i, d in enumerate(rows, 1):
        extra = ""
        if mode == "bm25":
            extra = f" [{d['kind']}] bm25={d['bm25']:.2f} title×{d['title_boost']:.2f} pop×{d['pop_boost']:.2f}"
        print(f"    {i}. {d['score']:7.2f}  {d['url'][:62]:62s} {(d['title'] or '').strip().replace(chr(10), ' ')[:36]}{extra}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-q", "--query", action="append", help="custom query (repeatable)")
    ap.add_argument("--quiet", action="store_true", help="skip result listings")
    ap.add_argument("--repeat", type=int, default=3, help="warm samples per query")
    ap.add_argument("--modes", default="legacy,bm25")
    args = ap.parse_args()
    queries = args.query or QUERIES
    modes = args.modes.split(",")

    if not os.environ.get("DATABASE_URL"):
        sys.exit("DATABASE_URL not set (source .env)")

    # warm up the pool / stats so connection setup isn't counted as query latency
    search.search("python", 1, 0, mode="bm25")

    lat = {m: {"cold": [], "warm": []} for m in modes}
    for q in queries:
        if not args.quiet:
            print(f"\n=== {q!r}")
        for m in modes:
            clear_caches()
            rows, cold = timed(q, m)
            lat[m]["cold"].append(cold)
            warm = []
            for _ in range(max(1, args.repeat)):
                _, w = timed(q, m)       # served by the LRU cache
                warm.append(w)
            lat[m]["warm"].append(statistics.median(warm))
            if not args.quiet:
                print(f"  {m:6s} cold {cold:6.1f} ms, cached {statistics.median(warm):.3f} ms")
                show(rows, m)

    # Cold with warm *internal* caches (df/stats) but a new page of results: the common production case
    for m in modes:
        lat[m]["semi"] = []
    for q in queries:
        for m in modes:
            search._cache.clear()          # result cache only
            _, t = timed(q, m)
            lat[m]["semi"].append(t)

    print("\n=== latency (ms) over", len(queries), "queries")
    print(f"{'mode':8s}{'cold p50':>10s}{'cold p95':>10s}{'cold max':>10s}{'df-warm p50':>13s}{'df-warm p95':>13s}{'cached p50':>12s}{'cached p95':>12s}")
    for m in modes:
        c, s, w = lat[m]["cold"], lat[m]["semi"], lat[m]["warm"]
        print(f"{m:8s}{pct(c, 50):10.1f}{pct(c, 95):10.1f}{max(c):10.1f}{pct(s, 50):13.1f}{pct(s, 95):13.1f}{pct(w, 50):12.3f}{pct(w, 95):12.3f}")
    print("cold = result, df and correction caches empty; df-warm = only the result LRU cache empty; cached = LRU hit")


if __name__ == "__main__":
    main()
