import math

import nltk
import string

from nltk.util import bigrams
from nltk.util import trigrams
from nltk.corpus import stopwords

# Already downloaded when building the container, theoretically shouldn't need any more changes because the dockerfile also sets the env variable pointing to the data
#nltk.download('punkt', quiet=True)
#nltk.download('stopwords', quiet=True)

STOP_WORDS = list(stopwords.words('english'))

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

        #grams.append(list(bigrams(i)))
        grams.append(list(bigrams(i)))
        ## TODO UPNEXT Where I left off, I want to make this for all tokenizers return a list of strings instead of tuples and bullcrap for easier use in main.py

    #print(grams[1:])

    flat_list = []
    for i in grams:
        for j in i:
            flat_list.append(''.join(j))
    output_list = set(flat_list)

    return output_list

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

    flat_list = []
    for i in grams:
        for j in i:
            flat_list.append(''.join(j))
    output_list = set(flat_list)

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

    text = text.lower()
    cleaned_text = []

    for i in text.split():
        if is_all_lowercase(i):
            cleaned_word = i.translate(str.maketrans('', '', string.punctuation))
            cleaned_text.append(cleaned_word)        
    
    return [item for item in cleaned_text if item not in STOP_WORDS]

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
    prefixes = []
    for word in cleaned_text:
        n = math.ceil(len(word) / 3)
        if n > 0:
            prefixes.append(word[:n])
    prefixes = list(set(prefixes))

    return [wordgrams, bigrams, trigrams, prefixes]

          #print(tokenize_bigrams(clean("How do I hack a website")))
#print(tokenize_bigrams(clean("I love programming in python")))

