import utils

conn = utils.get_conn()
cur = conn.cursor()

cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_word_urls_word_id ON word_urls(word_id);
    CREATE INDEX IF NOT EXISTS idx_word_urls_url_id ON word_urls(url_id);

    CREATE INDEX IF NOT EXISTS idx_bigram_urls_bigram_id ON bigram_urls(bigram_id);
    CREATE INDEX IF NOT EXISTS idx_bigram_urls_url_id ON bigram_urls(url_id);

    CREATE INDEX IF NOT EXISTS idx_trigram_urls_trigram_id ON trigram_urls(trigram_id);
    CREATE INDEX IF NOT EXISTS idx_trigram_urls_url_id ON trigram_urls(url_id);

    CREATE INDEX IF NOT EXISTS idx_prefix_urls_prefix_id ON prefix_urls(prefix_id);
    CREATE INDEX IF NOT EXISTS idx_prefix_urls_url_id ON prefix_urls(url_id);

    CREATE INDEX IF NOT EXISTS idx_words_word ON words(word);
    CREATE INDEX IF NOT EXISTS idx_bigrams_bigram ON bigrams(bigram);
    CREATE INDEX IF NOT EXISTS idx_trigrams_trigram ON trigrams(trigram);
    CREATE INDEX IF NOT EXISTS idx_prefixes_prefix ON prefixes(prefix);

    CREATE INDEX IF NOT EXISTS idx_url_token_counts_url_id ON url_token_counts(url_id);
""")

conn.commit()
cur.close()
conn.close()
print("Indexes created successfully")