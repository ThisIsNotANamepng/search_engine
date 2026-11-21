"""
Search lab: index 1000 real pages into a local Postgres and compare search/tokenizing variants on them

    tests/search_lab/db.sh up
    python tests/search_lab/lab.py build baseline
    python tests/search_lab/lab.py search "blugold" baseline myvariant
    python tests/search_lab/lab.py quality baseline myvariant
    python tests/search_lab/lab.py speed baseline myvariant

See tests/README.md for the data layout and how to write a variant
"""

import argparse
import importlib
import json
import os
import sqlite3
import statistics
import sys
import time
from pathlib import Path

LAB = Path(__file__).resolve().parent
sys.path.insert(0, str(LAB))                # variants/
sys.path.insert(0, str(LAB.parent.parent))  # utils.py, search.py, tokenizer.py

import psycopg2
from psycopg2 import sql

import add_search_indexes
import tokenizer
import utils

DATA = Path(os.getenv("LAB_DATA", LAB / "data"))
# Hardcoded and never read from the environment, so the lab can't touch the real index
SERVER = "postgres://postgres:lab@127.0.0.1:5433/"
PAGES = 1000
TOP_N = 10
SPEED_REPEATS = 5

PRODUCTION_TOKENIZE = tokenizer.tokenize_all


def use(name):
    """Load a variant, and point utils.store/search.search at its index and its tokenizer"""
    variant = importlib.import_module("variants." + name)
    utils.close_all_connections()  # get_conn() would otherwise reuse the previous variant's connection
    os.environ["DATABASE_URL"] = SERVER + "lab_" + getattr(variant, "INDEX", name)
    tokenizer.tokenize_all = getattr(variant, "tokenize_all", PRODUCTION_TOKENIZE)
    return variant


def build(name):
    variant = use(name)
    if hasattr(variant, "INDEX"):
        sys.exit(f"{name} searches the '{variant.INDEX}' index, build that one instead")

    index_db = next(DATA.glob("*.db"), None)
    if not index_db:
        sys.exit(f"No .db file in {DATA}, put the downloaded pages there (or set LAB_DATA)")
    files = {p.stem: p for p in DATA.rglob("*") if p.is_file()}

    admin = psycopg2.connect(SERVER + "postgres")
    admin.autocommit = True
    database = sql.Identifier("lab_" + name)
    admin.cursor().execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE);").format(database))
    admin.cursor().execute(sql.SQL("CREATE DATABASE {};").format(database))
    admin.close()
    utils.create_database()

    stored = 0
    # Ordered so every build indexes the same pages
    for page_id, url, title in sqlite3.connect(index_db).execute("SELECT id, url, title FROM pages ORDER BY id;"):
        path = files.get(page_id)
        if not path:
            continue
        body = path.read_text(errors="replace")
        # Raw html goes through the same extraction as a scrape; anything else is already the page's text
        content = utils.extract_data_from_html(body, url) if path.suffix == ".html" else [body, [], title, None]
        if not content[0]:
            continue
        utils.store(content, url)
        stored += 1
        if stored % 100 == 0:
            print(stored, "pages stored")
        if stored == PAGES:
            break

    if not stored:
        sys.exit(f"Stored nothing: no file under {DATA} is named after an id in {index_db.name}'s pages table")
    # The token indexes production search runs on; create_database() doesn't make them
    for table in add_search_indexes.INDEXES:
        add_search_indexes.build(table)
    print(f"Built lab_{name} from {stored} pages")


def show(names, query):
    for name in names:
        variant = use(name)
        print(f"\n== {name}: {query}")
        for rank, row in enumerate(variant.search(query)[:TOP_N], 1):
            print(f"{rank:>2}. {row[2]:<10.4f} {row[1]}\n    {row[0]}")


def expected_results():
    expected = json.loads((LAB / "expected.json").read_text())
    if not expected:
        sys.exit("expected.json is empty, add some queries first (see tests/README.md)")
    return expected


def quality(names):
    expected = expected_results()
    for name in names:
        variant = use(name)
        passed = 0
        print(f"\n== {name}")
        for query, urls in expected.items():
            got = [row[0] for row in variant.search(query)[:TOP_N]]
            missing = [url for url in urls if url not in got]
            passed += not missing
            print("PASS" if not missing else "FAIL", query)
            for url in missing:
                print("       missing", url)
        print(f"{name}: {passed}/{len(expected)} queries have every expected page in the top {TOP_N}")


def speed(names):
    expected = expected_results()
    for name in names:
        variant = use(name)
        times = []
        for _ in range(SPEED_REPEATS):
            for query in expected:
                start = time.perf_counter()
                variant.search(query)
                times.append((time.perf_counter() - start) * 1000)
        times.sort()
        p95 = times[int(len(times) * 0.95)]
        print(f"{name}: median {statistics.median(times):.1f} ms, p95 {p95:.1f} ms, slowest {times[-1]:.1f} ms ({len(times)} searches)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    modes = parser.add_subparsers(dest="mode", required=True)
    modes.add_parser("build").add_argument("variant", nargs="?", default="baseline")
    search_mode = modes.add_parser("search")
    search_mode.add_argument("query")
    for mode in (search_mode, modes.add_parser("quality"), modes.add_parser("speed")):
        mode.add_argument("variants", nargs="*", default=["baseline"])
    args = parser.parse_args()

    try:
        if args.mode == "build":
            build(args.variant)
        elif args.mode == "search":
            show(args.variants, args.query)
        elif args.mode == "quality":
            quality(args.variants)
        else:
            speed(args.variants)
    except psycopg2.OperationalError as error:
        sys.exit(f"{error}\nIs the lab database up (tests/search_lab/db.sh up) and the variant built (lab.py build <variant>)?")
