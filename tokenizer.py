"""
Tokenizer for the search engine index and for search queries (multilingual, Unicode-aware).

Multilingual design
-------------------
* Pure-ASCII text takes a fast path that is byte-for-byte the original English tokenizer
  (lowercase, delete ASCII punctuation, keep [a-z]+ words, drop English stop words).
* Any other text: delete ASCII punctuation and U+2018/U+2019 (so "don't" -> "dont", as before),
  NFKC-normalize, casefold ("Straße" -> "strasse"), then split into runs. Every other
  non-letter (Unicode punctuation, symbols, emoji, spaces) is a separator. Letters keep their
  combining marks (Devanagari, Arabic, ...). Tokens containing digits are dropped, like before.
* CJK (Han, Hiragana, Katakana, Hangul) runs become overlapping character bigrams, as in Lucene's
  CJKBigramFilter; a run of one character is a unigram. "Python编程语言" -> python, 编程, 程语, 语言.
* Unsegmentable scripts (Thai, Lao, Khmer, Myanmar, Tibetan) have no word separators, so runs of
  them are dropped instead of being indexed as giant run-on words.
* Stop words: English list (STOP_WORDS) for ASCII words as always. The stop words of every other
  language in nltk are applied ONLY to words containing a non-ASCII character, so a foreign list can
  never remove an ASCII word English would keep ('die' stays). Optional clean(text, lang=...)
  / tokenize_all(text, lang=...) additionally applies that language's list to ASCII words (e.g.
  German 'und'), for callers that ran language_gate() and know the page language.
* Words are not length-filtered here (the storer already drops words over 64 chars); this is
  consistent for ASCII and Unicode text.
* The bigram/trigram tables only hold a-z, so non-ASCII n-grams are dropped by the storer.
  Words and prefixes (VARCHAR(64)) carry the Unicode text.
* language_gate(text) decides whether a page should be indexed at all; see its docstring.
  Policy comes from env INDEX_LANGUAGES ('all' by default, or e.g. 'en,de,fr').
"""
import math
import os
import re
import unicodedata

import regex

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
# Same as above plus curly apostrophes, so "don’t" -> "dont" just like "don't"
_UNICODE_PUNCT_TABLE = str.maketrans('', '', string.punctuation + '\u2018\u2019')

# ---- multilingual stop words -------------------------------------------------------------
def _norm_word(w):
    return unicodedata.normalize('NFKC', w).casefold().translate(_UNICODE_PUNCT_TABLE)

_LANG_STOP = {}
for _fid in stopwords.fileids():
    if _fid.startswith('english'):
        continue
    _LANG_STOP[_fid] = frozenset(x for x in (_norm_word(w) for w in stopwords.words(_fid)) if x)
# Applied only to words containing non-ASCII characters (see module docstring)
_FOREIGN_STOP_NON_ASCII = frozenset(w for ws in _LANG_STOP.values() for w in ws if not w.isascii())

# langdetect code -> nltk stopword list name
_LANGDETECT_TO_NLTK = {
    'sq': 'albanian', 'ar': 'arabic', 'az': 'azerbaijani', 'eu': 'basque', 'bn': 'bengali',
    'ca': 'catalan', 'zh-cn': 'chinese', 'zh-tw': 'chinese', 'zh': 'chinese', 'da': 'danish',
    'nl': 'dutch', 'fi': 'finnish', 'fr': 'french', 'de': 'german', 'el': 'greek',
    'he': 'hebrew', 'hu': 'hungarian', 'id': 'indonesian', 'it': 'italian', 'kk': 'kazakh',
    'ne': 'nepali', 'no': 'norwegian', 'pt': 'portuguese', 'ro': 'romanian', 'ru': 'russian',
    'sl': 'slovene', 'es': 'spanish', 'sv': 'swedish', 'tg': 'tajik', 'ta': 'tamil',
    'tr': 'turkish', 'uz': 'uzbek', 'be': 'belarusian',
}

# ---- Unicode regexes ---------------------------------------------------------------------
_CJK = r'\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}\u30fc\u3005'
_UNSEG = r'\p{Script=Thai}\p{Script=Lao}\p{Script=Khmer}\p{Script=Myanmar}\p{Script=Tibetan}'
_TOKEN_RE = regex.compile(
    r'(?P<cjk>[' + _CJK + r']+)'
    r'|(?P<unseg>[' + _UNSEG + r']+)'
    r'|(?P<word>(?:[^' + _CJK + _UNSEG + r'\W]|\p{M})+)'
)
_DIGIT_RE = regex.compile(r'\p{N}|_')
_LETTER_RE = regex.compile(r'\p{L}')
_UNSEG_CHAR_RE = regex.compile('[' + _UNSEG + ']')
_LATIN_CHAR_RE = regex.compile(r'\p{Script=Latin}')

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

def _clean_unicode(text, extra_stop=None):
    foreign = _FOREIGN_STOP_NON_ASCII
    stop = _STOP_WORDS_SET
    out = []
    append = out.append
    # Same whitespace split as the ASCII path; only tokens with non-ASCII characters need the regex.
    for w in text.translate(_UNICODE_PUNCT_TABLE).split():
        if w.isascii():
            w = w.lower()
            if w.isalpha() and w not in stop and not (extra_stop and w in extra_stop):
                append(w)
            continue
        for m in _TOKEN_RE.finditer(unicodedata.normalize('NFKC', w).casefold()):
            kind = m.lastgroup
            w2 = m.group()
            if kind == 'cjk':
                if len(w2) == 1:
                    if w2 not in foreign:
                        append(w2)
                else:
                    for i in range(len(w2) - 1):
                        b2 = w2[i:i + 2]
                        if b2 not in foreign:
                            append(b2)
            elif kind == 'word':
                if _DIGIT_RE.search(w2) or not _LETTER_RE.search(w2):
                    continue
                if w2.isascii():
                    if w2 in stop or (extra_stop and w2 in extra_stop):
                        continue
                elif w2 in foreign:
                    continue
                append(w2)
            # 'unseg' runs are dropped
    return out

def clean(text, lang=None):
    """
    Cleans the input text: lowercases/casefolds, removes punctuation, drops numbers and stop words,
    and splits it into words (CJK as overlapping character bigrams, Thai-like scripts dropped).
    Pure-ASCII text is handled exactly like the original English-only tokenizer.

    Parameters:
    text (str): The input text to be cleaned
    lang (str|None): optional langdetect code of the text ('de', 'fr', ...); also applies that
        language's stop words to ASCII words. Default None keeps the English-only behavior.

    Returns:
    cleaned text (list): The cleaned text split into words
    """

    # Punctuation removal only deletes characters (none are whitespace), so
    # translating the whole text and then splitting is identical to splitting first.
    # After lower(), an ASCII-only alphabetic word is exactly [a-z]+.
    extra = None
    if lang and not lang.lower().startswith('en'):
        extra = _LANG_STOP.get(_LANGDETECT_TO_NLTK.get(lang.lower()) or _LANGDETECT_TO_NLTK.get(lang.lower().split('-')[0], ''))
    if text.isascii() and not extra:
        stop = _STOP_WORDS_SET
        return [w for w in text.lower().translate(_PUNCTUATION_TABLE).split()
                if w.isascii() and w.isalpha() and w not in stop]
    return _clean_unicode(text, extra)

def tokenize_all(text, lang=None):
    """
    Tokenizes the input text into unigrams, bigrams, trigrams, and prefixes

    Parameters:
    text (str): The input text to be tokenized

    Returns:
    list[wordgrams, bigrams, trigrams, prefixes]: A list containing lists of unigrams, bigrams, trigrams, and prefixes
    """

    cleaned_text = clean(text, lang)

    wordgrams = cleaned_text
    bigrams = tokenize_bigrams(cleaned_text)
    trigrams = tokenize_trigrams(cleaned_text)
    prefixes = list({word[:math.ceil(len(word) / 3)] for word in set(cleaned_text)})

    return [wordgrams, bigrams, trigrams, prefixes]

def tokenize_all_counts(text, lang=None):
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

    cleaned_text = clean(text, lang)
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


# ---- language gate -----------------------------------------------------------------------
_GATE_SCRIPT_SAMPLE = 20000
_GATE_LANG_SAMPLE = 2000

def _langdetect(sample):
    from langdetect import DetectorFactory, detect
    DetectorFactory.seed = 0
    try:
        return detect(sample)
    except Exception:
        return 'unknown'

def language_gate(text):
    """
    Decides whether a page's main text should be indexed.

    Returns (should_index, lang, reason):
      * more than 50% of the letters (first 20k chars) in Thai/Lao/Khmer/Myanmar/Tibetan
        -> (False, 'unknown'-or-detected, 'unsupported_script')
      * no letters at all -> (False, 'unknown', 'no_letters')
      * env INDEX_LANGUAGES='all' (default): index everything else, lang from langdetect
        (first ~2000 chars, seed 0, 'unknown' if it fails)
      * INDEX_LANGUAGES='en,de,fr': index only those (compared on the part before '-', so zh-cn
        matches 'zh'). 'unknown' passes only if most letters are Latin.
    """
    sample = text[:_GATE_SCRIPT_SAMPLE]
    letters = len(_LETTER_RE.findall(sample))
    if letters == 0:
        return False, 'unknown', 'no_letters'
    if sample.isascii():
        unseg = 0
        latin = letters
    else:
        unseg = len(_UNSEG_CHAR_RE.findall(sample))
        latin = len(_LATIN_CHAR_RE.findall(sample))
    if unseg * 2 > letters:
        return False, 'unknown', 'unsupported_script'

    lang = _langdetect(text[:_GATE_LANG_SAMPLE])

    policy = os.environ.get('INDEX_LANGUAGES', 'all').strip().lower()
    if policy in ('', 'all', '*'):
        return True, lang, 'all_languages'
    allowed = {x.strip() for x in policy.split(',') if x.strip()}
    if lang == 'unknown':
        if latin * 2 > letters:
            return True, lang, 'unknown_latin'
        return False, lang, 'language_not_allowed'
    if lang.lower() in allowed or lang.lower().split('-')[0] in allowed:
        return True, lang, 'allowed_language'
    return False, lang, 'language_not_allowed'
