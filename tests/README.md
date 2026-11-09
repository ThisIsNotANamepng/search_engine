# Tests

## Unit Tests

`test_store_and_search.py` stores pages into a real Postgres and searches them. It empties the index tables, so point it at a throwaway database (it only reads `TEST_DATABASE_URL`, never `DATABASE_URL`, and is skipped without it):

```bash
TEST_DATABASE_URL="postgres://postgres:password@localhost:5432/stultus_test" python -m pytest tests/test_store_and_search.py
```

## Development tests

Development tests aren't for testing the main code, its for experimenting with certain parts of the code

### `development_searching_quality.py`

Uses a diverse local dataset of Wikipedia articles for testing different search algorithms and tokens and weights. Includes a sqlite3 database as a mock up as the production database
