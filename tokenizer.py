import nltk
import string

from nltk.util import bigrams
from nltk.util import trigrams

nltk.download('punkt', quiet=True)

def tokenize_bigrams(text):
    """
    Tokenizes the input text into bigrams

    Parameters:
    text (str): The input text to be tokenized

    Returns:
    list: A list of bigram tuples
    """

    grams=[]

    for i in text:
        grams.append(list(bigrams(i)))

    return grams[1:]

def tokenize_trigrams(text):
    """
    Tokenizes the input text into trigrams
    
    Paramenters:
    text (list): The input text to be tokenized
    
    Returns:
    list: A list of trigram tuples
    """

    grams=[]

    for i in text:
        grams.append(list(trigrams(i)))

    return grams[1:]

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

    return [item for item in prefixes if len(item) == n]

def clean(text):
    """
    Cleans the input text by removing punctuation and converting to lowercase

    ##TODO: Can probably remove stopwords, need to talk about that

    Parameters:
    text (str): The input text to be cleaned

    Returns:
    cleaned text (list): The cleaned text split into words
    """

    text = text.lower()
    text = text.translate(str.maketrans('', '', string.punctuation))
    return text.split()

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
    prefixes = tokenize_prefixes(cleaned_text, 3)

    return [wordgrams, bigrams, trigrams, prefixes]

#print(tokenize_bigrams(clean("How do I hack a website")))
#print(tokenize_bigrams(clean("I love programming in python")))

