# Tests

## Unit Tests

`test_store_and_search.py` stores pages into a real Postgres and searches them. It empties the index tables, so point it at a throwaway database (it only reads `TEST_DATABASE_URL`, never `DATABASE_URL`, and is skipped without it):

```bash
TEST_DATABASE_URL="postgres://postgres:password@localhost:5432/stultus_test" python -m pytest tests/test_store_and_search.py
```

## Search lab

`search_lab/` indexes 1000 real pages from production into a throwaway Postgres (a podman container) so we can compare searching algorithms and tokenizers against each other: by eye, by a list of results that should come up, and by speed.

### Setup

1. Put the downloaded pages in `tests/search_lab/data/` (gitignored), or point `LAB_DATA` at them. The lab expects one sqlite3 `.db` file with a `pages` table (`id`, `url`, `title`), and one file per page named `<id>.html` (or `<id>.txt` for already extracted text) anywhere under that folder. `.html` files go through the same `utils.extract_data_from_html` as a real scrape
2. Start the database and build the index. Pages are stored with the real `utils.store`, the first 1000 by id

```bash
tests/search_lab/db.sh up                       # down to stop it, reset to delete it and every index
python tests/search_lab/lab.py build baseline
```

The lab only ever connects to `127.0.0.1:5433`, it ignores `DATABASE_URL`, so it can't touch the real index.

### Using it

```bash
python tests/search_lab/lab.py search "blugold football" baseline myvariant   # top 10 of each, to look at
python tests/search_lab/lab.py quality baseline myvariant                     # checks expected.json
python tests/search_lab/lab.py speed baseline myvariant                       # median/p95 over the expected.json queries
```

`expected.json` is the list of results that should come up. Each query maps to the urls which must be in its top 10:

```json
{
    "blugold football": ["https://www.uwec.edu/athletics/football"]
}
```

With 1000 pages the speed numbers only compare variants with each other, they say nothing about production speed.

### Writing a variant

A variant is a file in `search_lab/variants/`, its filename is its name. `baseline.py` is the production code.

- `search(query)` (required) returns rows best first, each starting with `(url, title, score)` like `search.search` does. Inside it, `utils.get_conn()` is connected to the variant's index
- `tokenize_all(text)` (optional) replaces `tokenizer.tokenize_all` for both storing and searching. It needs to return the same `[words, bigrams, trigrams, prefixes]` shape. Run `lab.py build myvariant` to index the pages with it
- `INDEX = "baseline"` (optional) searches another variant's index instead of building its own, for when only the searching changed
