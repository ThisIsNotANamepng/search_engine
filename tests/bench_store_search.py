"""
Times utils.store() and search.search() against the Postgres in DATABASE_URL

    DATABASE_URL=postgres://user:pass@host:5432/db python tests/bench_store_search.py
    python tests/bench_store_search.py -n 100 -q "blugold football" -q "climate change"
    python tests/bench_store_search.py --commit    # include the commit in store's time

Runs -n rounds, each one searches every query once then stores one page, printing each try's time, then a summary
Store: stores the texts in tests/texts/wikipedia/ as pages at https://bench.invalid/<n>, timing the whole store() call (tokenizing included)
Search: times the whole search() call. With --commit, the bench pages stored in earlier rounds are in the index for later searches

By default store's transactions are rolled back instead of committed, so nothing is written and it's safe to run against the real index, but the commit (WAL flush) isn't timed
--commit really commits, then deletes the bench pages again at the end. The new words they added stay in the words/prefixes tables
Each stored page burns one id from urls_id_seq either way
"""

import argparse
import glob
import os
import statistics
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import utils
os.environ["SEARCH_CACHE_SIZE"] = "0"  # search.py caches results, so repeats would time the cache instead of Postgres. Read at import
import search

BENCH_URL = "https://bench.invalid/"
QUERIES = ["blugold football", "climate change", "history of the internet", "how do airplanes fly"]


def report(name, times):
    ms = sorted(t * 1000 for t in times)
    p95 = ms[min(len(ms) - 1, round(0.95 * (len(ms) - 1)))]
    print(f"{name:<40} median {statistics.median(ms):8.1f} ms   p95 {p95:8.1f} ms   max {ms[-1]:8.1f} ms   (n={len(ms)})")


def delete_bench_pages():
    conn = utils.get_conn()
    cur = conn.cursor()
    ids = f"(SELECT id FROM urls WHERE url LIKE '{BENCH_URL}%')"
    for table in ("word_urls", "bigram_urls", "trigram_urls", "prefix_urls"):
        cur.execute(f"DELETE FROM {table} WHERE url_id IN {ids};")
    cur.execute(f"DELETE FROM urls WHERE url LIKE '{BENCH_URL}%';")
    conn.commit()
    conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-n", "--rounds", type=int, default=20, help="rounds of (each search once, then one store), default 20")
    parser.add_argument("-q", "--query", action="append", help="search query, repeatable (default: a few built in ones)")
    parser.add_argument("--commit", action="store_true", help="really commit stores (then clean up) so the commit is timed")
    args = parser.parse_args()

    if not os.getenv("DATABASE_URL"):
        sys.exit("Set DATABASE_URL")

    texts = []
    for path in sorted(glob.glob(os.path.join(REPO, "tests", "texts", "wikipedia", "*.txt"))):
        with open(path, encoding="utf-8") as f:
            texts.append((os.path.basename(path)[:-4], f.read()))

    if not args.commit:
        # store() commits then close()s, and close() on our reusable connection rolls back, so skipping the commit throws the page away
        utils._ReusableConnection.commit = lambda self: None

    # Warm up: first connection, bigram/trigram id maps, first plans. Not timed
    search.search(QUERIES[0])
    utils.store([texts[0][1], [], texts[0][0], None], BENCH_URL + "warmup")

    queries = args.query or QUERIES
    search_times = {q: [] for q in queries}
    store_times = []
    store_name = "store" + (" (committed)" if args.commit else " (rolled back)")
    try:
        # Alternate: every round searches each query once, then stores one page
        for i in range(args.rounds):
            for query in queries:
                start = time.perf_counter()
                search.search(query)
                search_times[query].append(time.perf_counter() - start)
                print(f"[{i + 1}] search {query!r}: {search_times[query][-1] * 1000:.1f} ms")

            title, text = texts[i % len(texts)]
            start = time.perf_counter()
            utils.store([text, [], title, None], f"{BENCH_URL}{i}")
            store_times.append(time.perf_counter() - start)
            print(f"[{i + 1}] {store_name} {title!r}: {store_times[-1] * 1000:.1f} ms")

        print()
        for query in queries:
            report(f"search {query!r}", search_times[query])
        report(store_name, store_times)
    finally:
        if args.commit:
            delete_bench_pages()


if __name__ == "__main__":
    main()
