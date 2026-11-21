import os, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import tokenizer as T

def test_german():
    assert T.clean("Die Straße ist groß") == ["die", "strasse", "ist", "gross"]  # ß casefolds; ASCII 'die' kept
    assert T.clean("Über Mädchen für schön") == ["mädchen", "schön"]  # über/für are German stopwords
    assert T.clean("Die und der Haus", "de") == ["haus"]  # lang hint applies German list to ASCII words

def test_french():
    assert T.clean("Où est la façade déjà élégante") == ["où", "est", "la", "façade", "déjà", "élégante"]
    assert "façade" in T.clean("La façade")
    assert T.clean("CAFÉ") == ["café"]

def test_russian_greek():
    assert T.clean("Привет, мир! И это тест") == ["привет", "мир", "тест"] or "привет" in T.clean("Привет, мир")
    assert "и" not in T.clean("Привет и мир")
    assert T.clean("Ελληνικά κείμενο") == ["ελληνικά", "κείμενο"]

def test_arabic_hindi_marks_kept():
    assert T.clean("مرحبا بالعالم") == ["مرحبا", "بالعالم"]
    assert T.clean("नमस्ते दुनिया") == ["नमस्ते", "दुनिया"]  # virama/matras intact

def test_cjk_bigrams():
    assert T.clean("你好，世界") == ["你好", "世界"]
    assert T.clean("世") == ["世"]
    assert T.clean("한국어 문장") == ["한국", "국어", "문장"]
    assert T.clean("日本語です") == ["日本", "本語", "語で", "です"]

def test_mixed_english_japanese():
    assert T.clean("Python编程语言") == ["python", "编程", "程语", "语言"]
    assert T.clean("I love 日本語 programming") == ["love", "日本", "本語", "programming"]

def test_thai_dropped():
    assert T.clean("ภาษาไทย") == []
    assert T.clean("ภาษาไทย hello world") == ["hello", "world"]

def test_punct_emoji_numbers_empty():
    for s in ["", "   ", "😀😀!!!", "...", "—«»“”", "123 4567", "١٢٣ ४५६"]:
        assert T.clean(s) == [], s
    assert T.clean("abc123 x") == ["x"]
    assert T.clean("don't don’t") == ["dont", "dont"]
    assert T.clean("wait… later") == ["wait", "later"]

def test_shapes_and_prefix_rule():
    w, b, t, p = T.tokenize_all("Привет мир héllo")
    assert isinstance(w, list) and isinstance(b, set) and isinstance(t, set) and isinstance(p, list)
    assert "при" not in p and "пр" in p and "привет"[:2] in p  # ceil(6/3)=2
    wc, bc, tc, pc = T.tokenize_all_counts("мир мир")
    assert wc["мир"] == 2 and pc["м"] == 2
    assert "the" in T.STOP_WORDS and isinstance(T.STOP_WORDS, list)

def test_gate():
    os.environ.pop("INDEX_LANGUAGES", None)
    en = "The quick brown fox jumps over the lazy dog and keeps running through the forest. " * 5
    de = "Der schnelle braune Fuchs springt über den faulen Hund und läuft durch den Wald. " * 5
    ok, lang, why = T.language_gate(en); assert ok and lang == "en", (ok, lang, why)
    assert T.language_gate(de)[:2] == (True, "de")
    ok, lang, why = T.language_gate("ภาษาไทยเป็นภาษาที่ใช้ในประเทศไทย " * 20 + "hello"); assert not ok and why == "unsupported_script"
    assert T.language_gate("") [0] is False and T.language_gate("😀 123")[0] is False
    os.environ["INDEX_LANGUAGES"] = "en"
    try:
        assert T.language_gate(en)[0] is True
        assert T.language_gate(de)[0] is False
        os.environ["INDEX_LANGUAGES"] = "en,de"
        assert T.language_gate(de)[0] is True
        os.environ["INDEX_LANGUAGES"] = "zh"
        assert T.language_gate("今天天气很好，我们一起去公园玩吧。" * 10)[0] is True
    finally:
        os.environ.pop("INDEX_LANGUAGES", None)
    assert T.language_gate(de) == T.language_gate(de)  # deterministic
