"""New tokenizer must give EXACTLY the old tokens/counts on pure-ASCII text."""
import sys, pathlib, glob
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
import tokenizer as new
import _tokenizer_reference as old

def _files():
    fs = sorted(glob.glob(str(ROOT / "tests/*.txt"))) + sorted(glob.glob(str(ROOT / "tests/texts/**/*.txt"), recursive=True))
    assert fs
    return fs

def test_ascii_identical():
    n = 0
    for f in _files():
        text = open(f, encoding="utf-8", errors="replace").read()
        if not text.isascii():
            # real file (curly quotes, accents...) must index at least as many words as before
            assert len(new.clean(text)) >= 0.99 * len(old.clean(text)), f
            # compare on the ASCII-only projection (the non-ASCII originals go through the Unicode path)
            text = text.encode("ascii", "ignore").decode()
        o, w = old.tokenize_all(text), new.tokenize_all(text)
        assert o[0] == w[0], f
        assert o[1] == w[1] and o[2] == w[2], f
        assert sorted(o[3]) == sorted(w[3]), f
        oc, wc = old.tokenize_all_counts(text), new.tokenize_all_counts(text)
        assert oc == wc, f
        n += 1
    assert n > 100

def test_ascii_with_foreign_stopword_unchanged():
    assert new.clean("die der und la le the cat") == old.clean("die der und la le the cat")
    assert "die" in new.clean("die cat")
