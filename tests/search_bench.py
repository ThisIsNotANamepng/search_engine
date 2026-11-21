#!/usr/bin/env python
"""Ranking + latency harness for the live index (read-only).

    set -a; . ./.env; set +a
    NLTK_DATA=~/nltk_data python tests/search_bench.py            # top 5 of each + latencies + stages
    python tests/search_bench.py --quiet                          # latencies only
    python tests/search_bench.py -q "linux kernel" -q wikipedia   # custom queries
    python tests/search_bench.py --repeat 5                       # more samples per query
    python tests/search_bench.py --sql                            # force the old SQL-only path (no memory index)
    python tests/search_bench.py --modes bm25,legacy              # also time the legacy formula
    python tests/search_bench.py --save top10.json                # save top-10 per query ...
    python tests/search_bench.py --compare top10.json             # ... and later report overlap@10 / #1 changes

Latency columns: cold = every in-process cache empty (result, df/posting, correction; the memory index
stays loaded, it is start-up state); df-warm = only the result LRU is empty; cached = result LRU hit.
The stage table is the per-stage breakdown from search.search_debug(timings=...) in ms: median over the
queries (and the worst query) for the cold and df-warm runs.
"""
import argparse
import json
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

STAGES = ["tokenize", "result_cache", "postings_cache", "postings_db", "score", "nav", "corrections",
          "finalize", "sql_path", "total_ms", "db_ms", "round_trips"]


def pct(values, p):
    values = sorted(values)
    if not values:
        return float("nan")
    return values[min(len(values) - 1, int(round(p / 100.0 * (len(values) - 1))))]


def clear_caches():
    if hasattr(search, "clear_caches"):
        search.clear_caches()
    else:                                   # older search.py
        search._cache.clear()
        search._df_cache.clear()
        search._corr_cache.clear()


def timed(q, mode, limit=5):
    t = time.perf_counter()
    tm = {}
    try:
        rows = search.search_debug(q, limit, 0, mode=mode, timings=tm)
    except Exception as e:                  # a DB timeout must not abort the whole run
        print(f"   ! {q!r} [{mode}] failed: {type(e).__name__}", file=sys.stderr)
        return None, (time.perf_counter() - t) * 1000, tm
    return rows, (time.perf_counter() - t) * 1000, tm


def show(rows, mode):
    if not rows:
        print("    (no results)")
    for i, d in enumerate(rows or [], 1):
        extra = ""
        if mode == "bm25":
            extra = f" [{d['kind']}] bm25={d['bm25']:.2f} title×{d['title_boost']:.2f} pop×{d['pop_boost']:.2f}"
        print(f"    {i}. {d['score']:7.2f}  {d['url'][:62]:62s} {(d['title'] or '').strip().replace(chr(10), ' ')[:36]}{extra}")


def wait_for_memindex(timeout=120):
    if not getattr(search, "FAST", False):
        return None
    search.warmup()
    import search_memindex
    t0 = time.time()
    while search_memindex.get() is None and time.time() - t0 < timeout:
        time.sleep(0.1)
    return search_memindex.get() is not None and time.time() - t0


def stage_table(label, tms):
    tms = [t for t in tms if t and t.get("path") not in (None, "cache")]
    if not tms:
        return
    print(f"\n--- stage breakdown, {label} (ms; median / worst over {len(tms)} queries) ---")
    for s in STAGES:
        vals = [t[s] for t in tms if s in t]
        if vals:
            print(f"  {s:15s}{statistics.median(vals):10.2f}{max(vals):10.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-q", "--query", action="append", help="custom query (repeatable)")
    ap.add_argument("--quiet", action="store_true", help="skip result listings")
    ap.add_argument("--repeat", type=int, default=3, help="cached samples per query")
    ap.add_argument("--modes", default="bm25")
    ap.add_argument("--sql", action="store_true", help="disable the memory index (old SQL path)")
    ap.add_argument("--save", help="write top-10 urls per query to this JSON file")
    ap.add_argument("--compare", help="compare top-10 with a file written by --save")
    args = ap.parse_args()
    queries = args.query or QUERIES
    modes = args.modes.split(",")

    if not os.environ.get("DATABASE_URL"):
        sys.exit("DATABASE_URL not set (source .env)")
    if args.sql:
        search.FAST = False
    else:
        ready = wait_for_memindex()
        print(f"memory index: {'loaded' if ready is not False and ready is not None else 'NOT loaded'}"
              + (f" (after {ready:.1f}s)" if ready else ""))

    search.search("python", 1, 0, mode="bm25")   # open the pool so connection setup isn't counted

    lat = {m: {"cold": [], "warm": [], "semi": [], "cold_tm": [], "semi_tm": []} for m in modes}
    tops = {}
    for q in queries:
        if not args.quiet:
            print(f"\n=== {q!r}")
        for m in modes:
            clear_caches()
            rows, cold, tm = timed(q, m)
            if rows is not None:
                lat[m]["cold"].append(cold)
                lat[m]["cold_tm"].append(tm)
            warm = []
            for _ in range(max(1, args.repeat)):
                _, w, _ = timed(q, m)       # served by the LRU cache
                warm.append(w)
            lat[m]["warm"].append(statistics.median(warm))
            if m == "bm25" and rows is not None:
                top, _, _ = timed(q, m, limit=10)
                if top is not None:
                    tops[q] = [d["url"] for d in top]
            if not args.quiet:
                print(f"  {m:6s} cold {cold:6.1f} ms, cached {statistics.median(warm):.3f} ms")
                show(rows, m)

    # Only the result cache cleared: df / posting / stats / correction caches warm (the common production
    # case). Prime every cache first (the cold pass above cleared them query by query).
    clear_caches()
    for q in queries:
        for m in modes:
            timed(q, m)
    for q in queries:
        for m in modes:
            search._cache.clear()
            rows, t, tm = timed(q, m)
            if rows is not None:
                lat[m]["semi"].append(t)
                lat[m]["semi_tm"].append(tm)

    print("\n=== latency (ms) over", len(queries), "queries")
    print(f"{'mode':8s}{'cold p50':>10s}{'cold p95':>10s}{'cold max':>10s}{'df-warm p50':>13s}{'df-warm p95':>13s}{'df-warm max':>13s}{'cached p50':>12s}{'cached p95':>12s}")
    for m in modes:
        c, s, w = lat[m]["cold"], lat[m]["semi"], lat[m]["warm"]
        print(f"{m:8s}{pct(c, 50):10.1f}{pct(c, 95):10.1f}{max(c or [0]):10.1f}{pct(s, 50):13.1f}{pct(s, 95):13.1f}{max(s or [0]):13.1f}{pct(w, 50):12.3f}{pct(w, 95):12.3f}")
    print("cold = result, df/posting and correction caches empty; df-warm = only the result LRU cache empty; cached = LRU hit")
    for m in modes:
        if m == "bm25":
            stage_table("cold", lat[m]["cold_tm"])
            stage_table("df-warm", lat[m]["semi_tm"])
        worst = sorted(zip(lat[m]["semi"], queries), reverse=True)[:3]
        print(f"worst df-warm [{m}]: " + ", ".join(f"{q!r} {t:.0f} ms" for t, q in worst))

    if args.save:
        json.dump(tops, open(args.save, "w"), indent=1)
        print("saved top-10 urls to", args.save)
    if args.compare:
        base = json.load(open(args.compare))
        ovs, diffs = [], []
        for q, urls in tops.items():
            old = base.get(q)
            if old is None:
                continue
            ov = len(set(urls) & set(old)) / max(1, len(old)) if old else 1.0
            ovs.append(ov)
            if urls[:1] != old[:1]:
                diffs.append(q)
        print(f"\nranking vs {args.compare}: mean overlap@10 = {statistics.mean(ovs):.3f} over {len(ovs)} queries; "
              f"#1 changed for {len(diffs)}: {diffs}")


if __name__ == "__main__":
    main()
