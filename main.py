# Search Engine

import tokenizer
import scraper
import time


def search(query):
    """Run search against the Postgres DB via `scraper.get_conn()`.

    Uses array parameters with `ANY(%s)` so empty token groups are handled safely.
    """
    conn = scraper.get_conn()
    cur = conn.cursor()

    # Fetch weights by type to avoid relying on ordering
    cur.execute("SELECT weight FROM weights WHERE type = %s;", ("word",))
    word_weight = cur.fetchone()[0]
    cur.execute("SELECT weight FROM weights WHERE type = %s;", ("bigram",))
    bigram_weight = cur.fetchone()[0]
    cur.execute("SELECT weight FROM weights WHERE type = %s;", ("trigram",))
    trigram_weight = cur.fetchone()[0]
    cur.execute("SELECT weight FROM weights WHERE type = %s;", ("prefix",))
    prefix_weight = cur.fetchone()[0]

    tokenized = tokenizer.tokenize_all(query)

    words = list(tokenized[0]) if tokenized and len(tokenized) > 0 else []
    bigrams = list(tokenized[1]) if tokenized and len(tokenized) > 1 else []
    trigrams = list(tokenized[2]) if tokenized and len(tokenized) > 2 else []
    prefixes = list(tokenized[3]) if tokenized and len(tokenized) > 3 else []

    # I changed it to rate based on number of token occurences divided by total tokens on url, we might want to change this in the future
    sql_query = """
    WITH scores AS (
        -- Words
        SELECT url_id,
            COUNT(*) * %s / COUNT(url_id) AS score
        FROM word_urls
        WHERE word_id IN (
            SELECT id FROM words WHERE word = ANY(%s)
        )
        GROUP BY url_id

        UNION ALL

        -- Bigrams
        SELECT url_id,
            COUNT(*) * %s / COUNT(url_id) AS score
        FROM bigram_urls
        WHERE bigram_id IN (
            SELECT id FROM bigrams WHERE bigram = ANY(%s)
        )
        GROUP BY url_id

        UNION ALL

        -- Trigrams
        SELECT url_id,
            COUNT(*) * %s / COUNT(url_id) AS score
        FROM trigram_urls
        WHERE trigram_id IN (
            SELECT id FROM trigrams WHERE trigram = ANY(%s)
        )
        GROUP BY url_id

        UNION ALL

        -- Prefixes
        SELECT url_id,
            COUNT(*) * %s / COUNT(url_id) AS score
        FROM prefix_urls
        WHERE prefix_id IN (
            SELECT id FROM prefixes WHERE prefix = ANY(%s)
        )
        GROUP BY url_id
    )

    SELECT urls.url,
        SUM(score) AS score
    FROM scores
    JOIN urls ON urls.id = scores.url_id
    GROUP BY urls.id, urls.url
    ORDER BY score DESC
    LIMIT 10;
    """

    params = (
        word_weight, words,
        bigram_weight, bigrams,
        trigram_weight, trigrams,
        prefix_weight, prefixes,
    )

    print("Searching....", query)
    start = time.time()
    cur.execute(sql_query, params)
    print(time.time()-start)
    results = cur.fetchall()

    print(results)

    cur.close()
    conn.close()


# Example usage:
# query = input("Search query: ")
query = "university"
search(query)
