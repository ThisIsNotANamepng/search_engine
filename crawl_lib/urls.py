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
