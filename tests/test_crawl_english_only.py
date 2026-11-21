"""English-only crawling: foreign-looking urls are skipped before they're fetched"""

from crawl_lib.urls import looks_foreign


def test_foreign_tlds_and_language_markers():
    for u in ("https://yandex.ru", "https://www.spiegel.de/politik", "https://example.co.jp", "https://globo.com.br",
              "https://de.wikipedia.org/wiki/Berlin", "https://pt-br.example.com", "https://example.com/fr/",
              "https://example.com/de-de/produkte", "https://example.com/zh_CN/docs"):
        assert looks_foreign(u), u


def test_english_and_generic_urls_pass():
    for u in ("https://en.wikipedia.org/wiki/Main_Page", "https://www.bbc.co.uk/news", "https://github.io",
              "https://example.io", "https://example.co", "https://www.abc.net.au", "https://www.cbc.ca",
              "https://example.com/de", "https://example.com/it-support", "https://example.com/news/fr",
              "https://www.uwec.edu", "https://my.example.com", "https://it.uwec.edu", "https://example.com/id/123", "https://example.com/no/", "https://example.ai", "https://site.me/about"):
        # /de alone is a path prefix of 2 letters, treated as a language marker; the rest are English/generic
        if u.endswith("/de"):
            assert looks_foreign(u)
        else:
            assert not looks_foreign(u), u
