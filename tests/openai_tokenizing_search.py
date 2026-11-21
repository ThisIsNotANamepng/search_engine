import tiktoken, sys
import os
import math
from collections import Counter

texts = []
tokenized_texts = []
titles = []

enc = tiktoken.get_encoding("o200k_base")

for i in os.listdir("texts/wikipedia"):
    filepath = os.path.join("texts/wikipedia/", i)
    with open(filepath, "r") as file:
        tokenized_texts.append(enc.encode(file.read()))
        titles.append(i)

query = "Frankenstein monster created by scientist"
if len(sys.argv) == 2: query = sys.argv[1]

encoded_query = enc.encode(query)

# Build document frequency for IDF calculation
num_docs = len(tokenized_texts)
doc_freq = Counter()

for tokens in tokenized_texts:
    doc_freq.update(set(tokens))

# Calculate IDF for each token in query
query_tokens = set(encoded_query)
idf = {}
for token in query_tokens:
    idf[token] = math.log((num_docs + 1) / (doc_freq.get(token, 0) + 1)) + 1

def compute_tfidf_vector(tokens, idf_dict):
    tf = Counter(tokens)
    vector = {}
    for token, weight in idf_dict.items():
        vector[token] = tf.get(token, 0) * weight
    return vector

def cosine_similarity(vec1, vec2):
    # Only compute for tokens present in both (optimization)
    common_tokens = set(vec1.keys()) & set(vec2.keys())

    dot_product = sum(vec1[t] * vec2[t] for t in common_tokens)
    norm1 = math.sqrt(sum(v ** 2 for v in vec1.values()))
    norm2 = math.sqrt(sum(v ** 2 for v in vec2.values()))

    if norm1 == 0 or norm2 == 0:
        return 0.0
    return dot_product / (norm1 * norm2)

# Compute query vector once
query_vector = compute_tfidf_vector(encoded_query, idf)

matched = ""
top_score = -1

for idx, tokenized_text in enumerate(tokenized_texts):
    text_vector = compute_tfidf_vector(tokenized_text, idf)
    score = cosine_similarity(query_vector, text_vector)

    if score > top_score:
        top_score = score
        matched = titles[idx]

print(f"Best match: {matched}")
print(f"Similarity score: {top_score:.4f}")
