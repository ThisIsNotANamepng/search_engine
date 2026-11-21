"""Cheap, dependency-free URL helpers: host keys, depth, and skipping URLs that obviously aren't HTML."""

from urllib.parse import urlsplit

# Extensions of things that are never an HTML page worth fetching. Checked on cleaned urls (no ?query / #fragment)
NON_HTML_EXTENSIONS = frozenset("""
jpg jpeg png gif svg webp ico bmp tif tiff avif heic psd raw
mp4 m4v mp3 avi mov wmv flv mkv webm ogg ogv oga wav flac m4a aac mpg mpeg 3gp
zip gz tgz tar rar 7z bz2 xz zst lz iso dmg img exe msi apk deb rpm bin jar war whl egg
pdf doc docx xls xlsx ppt pptx odt ods odp rtf epub mobi ps
css js mjs map json xml rss atom woff woff2 ttf eot otf swf torrent csv tsv txt log sql
""".split())


def path_extension(url):
    """Lowercase extension of the last path segment ('' if none). Works on cleaned urls and on raw ones."""
    path = url.split("?", 1)[0].split("#", 1)[0]
    scheme_end = path.find("://")
    start = path.find("/", scheme_end + 3) if scheme_end != -1 else path.find("/")
    if start == -1:
        return ""
    last = path[start:].rsplit("/", 1)[-1]
    dot = last.rfind(".")
    if dot == -1:
        return ""
    return last[dot + 1:].lower()


def looks_non_html(url):
    return path_extension(url) in NON_HTML_EXTENSIONS


def host_of(url):
    """Lowercase hostname ('' if none)"""
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def host_key(host):
    """Politeness / per-host-cap key: www.example.com and example.com are the same server for our purposes"""
    return host[4:] if host.startswith("www.") else host


def path_depth(url):
    """Number of path segments (homepage = 0). Used to prefer homepage / shallow urls."""
    scheme_end = url.find("://")
    start = url.find("/", scheme_end + 3 if scheme_end != -1 else 0)
    if start == -1:
        return 0
    return url.count("/", start)  # '/a/b' -> 2


def host_suffixes(host):
    """example.co.uk -> example.co.uk, co.uk (every dot-suffix with 2+ labels, plus a single-label host as is)"""
    parts = host.split(".")
    if len(parts) < 2:
        return [host]
    return [".".join(parts[i:]) for i in range(len(parts) - 1)]


# ---- Skipping obviously non-English sites before fetching them (the language gate still checks every page that is fetched)
# Country-code TLDs whose sites are overwhelmingly not in English. Generic-use ccTLDs (.io .co .ai .me .tv .fm .ly .gg .to .sh ...)
# (and .is .la .ly, mostly used as generic domains) and English-speaking countries are deliberately absent.
FOREIGN_TLDS = frozenset("""
ru su by kz ua cn jp kr tw hk mo th vn id kh mm de at fr be lu es pt it pl cz sk hu ro bg rs hr si ba mk al gr tr
nl se no fi dk ee lv lt br ar mx cl pe ve uy py bo ec cr pa do gt hn sv ni cu ir iq sa ae qa kw om bh jo sy lb il eg
ma dz tn az am ge uz tm kg tj mn np bd lk pk af md
""".split())
# Second-level registries under those TLDs (com.br, co.jp, ...) are covered because the last label decides.
# Subdomains naming a language (de.wikipedia.org, fr.example.com, pt-br.example.com) and path prefixes (/de/, /fr-fr/)
FOREIGN_LANG_CODES = frozenset("""
de fr es it pt nl ru ja zh ko pl sv no nb nn da fi cs sk hu ro bg el tr uk ar he fa hi id vi th ms ca eu gl hr sr sl
lt lv et is sq mk bs az ka hy kk uz be bn ta te ur mr gu kn ml si ne km lo my mn
""".split())


# Language codes that are also common English words/abbreviations in hostnames and paths (my.site.com, it.uwec.edu, /id/, /no/):
# never treated as a language marker. The language gate still catches those pages after fetching
_AMBIGUOUS_CODES = frozenset("my it is no id ca ms hi be am as at in me us so to or on an do go he".split())


def _lang_label(label):
    base = label.split("-", 1)[0].split("_", 1)[0]
    if label == base and base in _AMBIGUOUS_CODES:
        return False
    return base in FOREIGN_LANG_CODES and (len(label) == 2 or (len(label) == 5 and label[2] in "-_"))


def looks_foreign(url):
    """True if the url is very likely a non-English page: a non-English country TLD, a language subdomain or a language path prefix"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    labels = host.split(".")
    if len(labels) >= 2 and labels[-1] in FOREIGN_TLDS:
        return True
    if len(labels) >= 3 and _lang_label(labels[0]):
        return True
    first = parts.path.lstrip("/").split("/", 1)[0].lower()
    return bool(first) and _lang_label(first)
