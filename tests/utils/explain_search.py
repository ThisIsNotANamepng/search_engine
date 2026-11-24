"""
Shows where a search spends its time: runs both stages of search.py's fast search for a query under
EXPLAIN (ANALYZE, BUFFERS) and prints the plans, with the same session settings the head uses.

    DATABASE_URL=... python tests/utils/explain_search.py how to grow tomatoes

It runs the real (read-only) queries, so it takes as long as the search does; Ctrl-C cancels.
SEARCH_STAGE1 / SEARCH_RESCORE_K are read from the environment like search.py does, so set them the same as the head's.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import psycopg2

import search
import tokenizer


def query_groups(query):
    # Same tokens as search.search()
    tokenized = tokenizer.tokenize_all(query)
    names = ("word", "bigram", "trigram", "prefix")
    groups = [(name, sorted(set(vals))) for name, vals in zip(names, tokenized) if vals]
    return groups


def explain(cur, title, sql, params):
    print(f"\n===== {title}")
    started = time.time()
    cur.execute("EXPLAIN (ANALYZE, BUFFERS) " + sql, params)
    for (line,) in cur.fetchall():
        print(line)
    print(f"----- {title}: {time.time() - started:.1f}s")


def main():
    query = " ".join(sys.argv[1:])
    if not query:
        sys.exit(__doc__)
    groups = query_groups(query)
    for name, vals in groups:
        print(f"{name:8} {len(vals):3}  {' '.join(vals)}")
    print(f"stage 1 uses {','.join(search.STAGE1_BRANCHES)}, re-scores the top {search.RESCORE_K}")

    options = search._session_options()
    conn = psycopg2.connect(os.environ["DATABASE_URL"], **({"options": options} if options else {}))
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT version();")
    print(cur.fetchone()[0])

    g = dict(groups)
    core = [n for n in search.STAGE1_BRANCHES if n in g]
    if not core:
        sys.exit("the query has no tokens for stage 1")
    stage1 = search._STAGE1_SQL.format(
        branches="\n                UNION ALL".join(search._BRANCHES[n] for n in core), k=search.RESCORE_K,
        total_tokens=search._TOTAL_TOKENS, ref_score=search._REF_SCORE)
    stage1_params = tuple(g[n] for n in core)
    explain(cur, "stage 1 (pick candidates)", stage1, stage1_params)

    cur.execute(stage1, stage1_params)  # again for the candidates; mostly cached by now
    cand = [r[0] for r in cur.fetchall()]
    print(f"\n{len(cand)} candidates")
    if not cand:
        return

    stage2 = search._SQL_TEMPLATE.format(
        branches="\n            UNION ALL".join(search._BRANCHES_FOR_CANDIDATES[n] for n, _ in groups),
        total_tokens=search._TOTAL_TOKENS, ref_score=search._REF_SCORE)
    stage2_params = []
    for _, vals in groups:
        stage2_params += [vals, cand]
    explain(cur, "stage 2 (re-score candidates)", stage2, tuple(stage2_params))
    conn.close()


if __name__ == "__main__":
    main()
