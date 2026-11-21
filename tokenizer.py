import math

import nltk
import string
from collections import Counter

from nltk.util import bigrams
from nltk.util import trigrams
from nltk.corpus import stopwords

# Already downloaded when building the container, theoretically shouldn't need any more changes because the dockerfile also sets the env variable pointing to the data
#nltk.download('punkt', quiet=True)
#nltk.download('stopwords', quiet=True)

STOP_WORDS = list(stopwords.words('english'))
# O(1) membership for clean(); STOP_WORDS stays a list for anything importing it
_STOP_WORDS_SET = frozenset(STOP_WORDS)
_PUNCTUATION_TABLE = str.maketrans('', '', string.punctuation)

def tokenize_bigrams(text):
    """
    Tokenizes the input text into bigrams

    Parameters:
    text (str): The input text to be tokenized

    Returns:
    list: A list of bigram tuples
    """

    # Slicing unique words gives exactly the set nltk's bigrams() would, without a
    # Python-level tuple/join per character pair.
    output_list = set()
    for word in set(text):
        for i in range(len(word) - 1):
            output_list.add(word[i:i + 2])

    return output_list

def tokenize_trigrams(text):
    """
    Tokenizes the input text into trigrams
    
    Paramenters:
    text (list): The input text to be tokenized
    
    Returns:
    list: A list of trigram tuples
    """

    output_list = set()
    for word in set(text):
        for i in range(len(word) - 2):
            output_list.add(word[i:i + 3])

    return output_list

def tokenize_prefixes(text, n):
    """
    Tokenizes the input text into prefixes of length n

    Parameters:
    text (str): The input text to be tokenized
    n (int): The length of the prefixes

    Returns:
    list: A list of prefix strings
    """

    prefixes = []

    for word in text:
        if len(word) >= n:
            prefixes.append(word[:n])
        else:
            prefixes.append(word)

    toreturn = list(set([item for item in prefixes if len(item) == n]))

    return toreturn

def is_all_lowercase(input_string):
    for char in input_string:
        if not ('a' <= char <= 'z'):
            return False
    return True

def clean(text):
    """
    Cleans the input text by removing punctuation and converting to lowercase, adn removes stop words

    Parameters:
    text (str): The input text to be cleaned

    Returns:
    cleaned text (list): The cleaned text split into words
    """

    # Punctuation removal only deletes characters (none are whitespace), so
    # translating the whole text and then splitting is identical to splitting first.
    # After lower(), an ASCII-only alphabetic word is exactly [a-z]+.
    stop = _STOP_WORDS_SET
    return [w for w in text.lower().translate(_PUNCTUATION_TABLE).split()
            if w.isascii() and w.isalpha() and w not in stop]

def tokenize_all(text):
    """
    Tokenizes the input text into unigrams, bigrams, trigrams, and prefixes

    Parameters:
    text (str): The input text to be tokenized

    Returns:
    list[wordgrams, bigrams, trigrams, prefixes]: A list containing lists of unigrams, bigrams, trigrams, and prefixes
    """

    cleaned_text = clean(text)

    wordgrams = cleaned_text
    bigrams = tokenize_bigrams(cleaned_text)
    trigrams = tokenize_trigrams(cleaned_text)
    prefixes = list({word[:math.ceil(len(word) / 3)] for word in set(cleaned_text)})

    return [wordgrams, bigrams, trigrams, prefixes]

def tokenize_all_counts(text):
    """
    Same tokens as tokenize_all(), but counts how many times each one appears on the page

    A bigram/trigram is counted once per place it appears inside a word, and a prefix is
    counted once per word that starts with it, so "the theme" gives th: 2, the: 2, he: 2.
    The keys of each Counter are exactly the tokens tokenize_all() returns.

    Parameters:
    text (str): The input text to be tokenized

    Returns:
    list[Counter, Counter, Counter, Counter]: token -> count for words, bigrams, trigrams, prefixes
    """

    cleaned_text = clean(text)
    word_counts = Counter(cleaned_text)

    bigram_counts = Counter()
    trigram_counts = Counter()
    prefix_counts = Counter()

    # Work per unique word and multiply by how often the word appears, instead of re-slicing repeated words
    for word, n in word_counts.items():
        for i in range(len(word) - 1):
            bigram_counts[word[i:i + 2]] += n
        for i in range(len(word) - 2):
            trigram_counts[word[i:i + 3]] += n
        prefix_counts[word[:math.ceil(len(word) / 3)]] += n

    return [word_counts, bigram_counts, trigram_counts, prefix_counts]

          #print(tokenize_bigrams(clean("How do I hack a website")))
#print(tokenize_bigrams(clean("I love programming in python")))

