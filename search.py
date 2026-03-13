# Search Engine:


#Aaron's attribute normalization functions
'''
prefix_score = ceil(len(prefix_matches)/3)
trigram_score = trigram_matches {on the page}
bigram_score = bigram_matches {on the page}
where word_score = len(word)/2
'''

#Aaron's Search:
'''search_output = ((prefix_score + bigram_score + trigram_score + word_score + word_count) / word_count) * reference_count'''

# relevance = sum_of_tokens_of_all_attributes + word_count / word_count
# popularity = reference_count
# search_output = relevance * popularity




#Original Time Taken b4 Aaron:
#   0.22978639602661133 sec
#AFTER
#   0.058481693267822266

import tokenizer
import scraper
import time
import math


def search(query):
    start = time.time()
    """Run search against the Postgres DB via `scraper.get_conn()`.

    Uses array parameters with `ANY(%s)` so empty token groups are handled safely.
    """
    conn = scraper.get_conn()
    cur = conn.cursor()

    tokenized = tokenizer.tokenize_all(query)

    words    = list(tokenized[0]) if tokenized and len(tokenized) > 0 and tokenized[0] else []
    bigrams  = list(tokenized[1]) if tokenized and len(tokenized) > 1 and tokenized[1] else []
    trigrams = list(tokenized[2]) if tokenized and len(tokenized) > 2 and tokenized[2] else []
    prefixes = list(tokenized[3]) if tokenized and len(tokenized) > 3 and tokenized[3] else []


    #start = time.time()
    #print("Time taken:", time.time() - start)

    sql_query = """
        WITH
        word_matches AS (
            SELECT wu.url_id,
                SUM(LENGTH(w.word) / 2.0) AS word_score -- bias toward longer words, but that is intentional because they probably show up less. this also normalizes word length to bigrams and trigrams which are naturally also biased toward longer words since they will create more bigrams/trigrams
            FROM word_urls wu
            JOIN words w ON w.id = wu.word_id
            WHERE w.word = ANY(%s)
            GROUP BY wu.url_id
        ),

        bigram_matches AS (
            SELECT bu.url_id,
                COUNT(*) * 1.0 AS bigram_score  -- 1x weight
            FROM bigram_urls bu
            JOIN bigrams b ON b.id = bu.bigram_id
            WHERE b.bigram = ANY(%s)
            GROUP BY bu.url_id
        ),

        trigram_matches AS (
            SELECT tu.url_id,
                COUNT(*) * 1.5 AS trigram_score  -- 1.5x weight
            FROM trigram_urls tu
            JOIN trigrams t ON t.id = tu.trigram_id
            WHERE t.trigram = ANY(%s)
            GROUP BY tu.url_id
        ),

        prefix_matches AS (
            SELECT 
                pu.url_id,
                SUM(
                    CASE
                        WHEN LENGTH(p.prefix) %% 3 = 0
                            THEN CEIL(LENGTH(p.prefix) / 3.0) + 1
                        ELSE
                            CEIL(LENGTH(p.prefix) / 3.0)
                    END
                ) AS prefix_score
            FROM prefix_urls pu
            JOIN prefixes p ON p.id = pu.prefix_id
            WHERE p.prefix = ANY(%s)
            GROUP BY pu.url_id
        ),

        combined AS (
            SELECT
                u.id AS url_id,
                COALESCE(wm.word_score, 0) AS word_score,
                COALESCE(bm.bigram_score, 0) AS bigram_score,
                COALESCE(tm.trigram_score, 0) AS trigram_score,
                COALESCE(pm.prefix_score, 0) AS prefix_score,
                utc.word_count AS word_count,
                u.reference_count AS reference_count  -- adjust if your column name differs
            FROM urls u
            JOIN url_token_counts utc ON utc.url_id = u.id
            LEFT JOIN word_matches wm ON wm.url_id = u.id
            LEFT JOIN bigram_matches bm ON bm.url_id = u.id
            LEFT JOIN trigram_matches tm ON tm.url_id = u.id
            LEFT JOIN prefix_matches pm ON pm.url_id = u.id
        )

        SELECT
            u.url,
            (
                (
                    prefix_score +
                    bigram_score +
                    trigram_score +
                    word_score +
                    word_count
                ) / word_count
            ) * c.reference_count AS search_output
        FROM combined c
        JOIN urls u ON u.id = c.url_id
        WHERE
            word_score > 0
            OR bigram_score > 0
            OR trigram_score > 0
            OR prefix_score > 0
        ORDER BY search_output DESC
        LIMIT 10;
        """

    params = (
        words    or [''],
        bigrams  or [''],
        trigrams or [''],
        prefixes or [''],
    )

    print("Searching....", query)
    start = time.time()
    print("")
    print("words:", words)
    print("bigrams:", bigrams)
    print("trigrams:", trigrams)
    print("prefixes:", prefixes)
    #print("params:", params)
    print("param count:", len(params))
    cur.execute(sql_query, params)
    print("")
    print("Time taken:", time.time() - start)
    results = cur.fetchall()

    print(results)

    cur.close()
    conn.close()


# Example usage:
# query = input("Search query: ")
query = "best way to cook chicken"
search(query)
