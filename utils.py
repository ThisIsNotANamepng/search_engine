"""
Functions for scraping

"""

import os
import time
import requests
import socket
import string
import itertools
import ipaddress
import weakref
import functools
import queue
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin
from urllib.robotparser import RobotFileParser
import tldextract
import tokenizer
import psycopg2
from psycopg2 import sql
import psycopg2.extras as extras
from psycopg2.extras import execute_values
from langdetect import detect
from langdetect.lang_detect_exception import LangDetectException
import csv
from typing import Optional
import sqlite3
import zlib
import trafilatura

USER_AGENT = "StultusSearchEngine/1.0 (+https://stultus.rip; crawler@stultus.rip)"
RATE_LIMITED = "rate_limited"
REDIRECTED = "redirected"
NOT_SCRAPABLE = "not_scrapable"
# Entry point of the proxy chain (proxy A). Proxy A forwards to proxy B
# (configured on A via PROXY_UPSTREAM), which then makes the real outbound
# request. Unset to fetch directly without a proxy.
SCRAPER_PROXY = os.environ.get("SCRAPER_PROXY", "").rstrip("/")
SCRAPER_PROXY_PASSWORD = os.environ.get("SCRAPER_PROXY_PASSWORD", "")
_PROXY_HEADERS = {"X-Proxy-Auth": SCRAPER_PROXY_PASSWORD} if SCRAPER_PROXY_PASSWORD else {}

_BIGRAM_ID_MAP = None
_TRIGRAM_ID_MAP = None
DEBUG_BREAKPOINT_TIMER = time.time()
# Look for the DEBUG shell varialbe and set a global variable to True or false base don whether it was passed
DEBUG = "DEBUG" in os.environ
LEVEL = os.environ["DEBUG"] if DEBUG else False
# When True, suppress all external database access (set once from scrape.py's --nodb flag).
# Like DEBUG above, this is a module-level switch so call sites don't have to thread a flag.
NODB = False
URL = ""

# Chunk size for sending page text to the web text storage server
WEB_STORAGE_CHUNK_SIZE = 64 * 1024

# Postgres columns have length limits, we set them in creating the database and we only send data reduced to the maximum length so we don't crash on the postgres end
# How long a scraper may hold a claimed url before another scraper can claim it. Also the retry delay for rate-limited urls
CLAIM_TIMEOUT = "30 minutes"
MAX_URL_LENGTH = 2048  # urls.url, url_queue.url, urls_references.*, icon_link
MAX_TITLE_LENGTH = 128  # urls.title
MAX_TOKEN_LENGTH = 64  # words.word, prefixes.prefix

MAX_PAGE_BYTES = 5 * 1024 * 1024  # pages bigger than this (after decompression) are skipped

class CSVTracker:
    def __init__(self, filename: str = "timing.csv"):
        self.filename = filename
        self.columns = [
            "url",
            "downloaded_text",
            "detected_language",
            "tokenized",
            "cleaned_and_split",
            "added_new_tokens",
            "made_token_maps",
            "made_insert_statements",
            "execute_insert_statements",
            "committed_and_closed",
            "total"
        ]
        self._ensure_file_exists()

    def _ensure_file_exists(self):
        """Create CSV with headers if it doesn't exist."""
        if not os.path.exists(self.filename):
            with open(self.filename, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=self.columns)
                writer.writeheader()

    def _read_all(self) -> list[dict]:
        """Read all rows from CSV."""
        with open(self.filename, 'r', newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            return list(reader)

    def _write_all(self, rows: list[dict]):
        """Write all rows to CSV."""
        with open(self.filename, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=self.columns)
            writer.writeheader()
            writer.writerows(rows)

    def update(self, url: str, column: str, value) -> bool:
        """
        Update a specific column for a given URL.
        Creates new row if URL doesn't exist.

        Args:
            url: The URL to update (primary key)
            column: Column name to update
            value: Value to set

        Returns:
            True if successful, False if column invalid
        """
        if column not in self.columns:
            print(f"Error: Invalid column '{column}'. Valid columns: {self.columns}")
            return False

        rows = self._read_all()

        # Find existing row or create new one
        row_found = False
        for row in rows:
            if row.get("url") == url:
                row[column] = value
                row_found = True
                break

        if not row_found:
            # Create new row with URL and empty values
            new_row = {col: "" for col in self.columns}
            new_row["url"] = url
            new_row[column] = value
            rows.append(new_row)

        self._write_all(rows)
        return True

    def get(self, url: str) -> Optional[dict]:
        """Get a row by URL."""
        rows = self._read_all()
        for row in rows:
            if row.get("url") == url:
                return row
        return None

    def get_all(self) -> list[dict]:
        """Get all rows."""
        return self._read_all()

TIMING_TRACKER = CSVTracker("timing.csv") if LEVEL == "3" else ""

def debug_print(text):
    """
    Print system stats when DEBUG is set 
    
    DEBUG = 1 Prints for each print statement
    DEBUG = 2 Prints for each print statement and includes timing
    DEBUG = 3 Prints for each print statement and includes timing and saves data to timing.csv

    TODO:
        - All debug print (info_print and failure_print too) should have a option, maybe DEBUG=3 to write all of the debug stuff to a file log as well as printing
    """

    global DEBUG_BREAKPOINT_TIMER
    global DEBUG
    global LEVEL

    def format_time(timestamp):
        return f"{timestamp:.4f}"

    if DEBUG:

        if text == "":
            # Just wants to reset the timer, functions to track the timing for aren't next to each other, need to reset the time in the debug_breakpoint_timer to zero before starting new function
            DEBUG_BREAKPOINT_TIMER = time.time()
            return
        
        if LEVEL == "1":
            print(text)

        elif LEVEL == "2":

            print(format_time(time.time()-DEBUG_BREAKPOINT_TIMER), text)

            DEBUG_BREAKPOINT_TIMER = time.time()

        elif LEVEL == "3":
            global TIMING_TRACKER
            global URL

            print(format_time(time.time()-DEBUG_BREAKPOINT_TIMER), text)
            timing = format_time(time.time()-DEBUG_BREAKPOINT_TIMER)

            # From the debug message that's passed we can derive a unique key for the columns in the debug csv file
            if "Visi" == text[0:4]:
                # "Visited site, got main text and links"
                code = "downloaded_text"
            elif "Dete" == text[0:4]:
                # "Detected language"
                code = "detected_language"
            elif "Toke" == text[0:4]:
                # "Tokenized"
                code = "tokenized"
            elif "Clea" == text[0:4]:
                # "Cleaned links and split tokens into words, bigrams, trigrams, prefixes"
                code = "cleaned_and_split"
            elif "Adde" == text[0:4]:
                # "Added new, unseen tokens to the database"
                code = "added_new_tokens"
            elif "Made t" == text[0:6]:
                # "Made token maps"
                code = "made_token_maps"
            elif "Made i" == text[0:6]:
                # "Made insert stementes for <token>_url tables"
                code = "made_insert_statements"
            elif "Exec" == text[0:4]:
                # "Executed insert statements, stored url.id_token.id pairs"
                code = "execute_insert_statements"
            elif "Comm" == text[0:4]:
                # "Committed and closed sql connection"
                code = "committed_and_closed"
            else:
                # Not one of the timed steps (e.g. "Sending to web text storage server"): just printed above
                code = None

            if code:
                TIMING_TRACKER.update(URL, code, timing)

            DEBUG_BREAKPOINT_TIMER = time.time()

def info_print(text):
    # For debugging and printing statements which notify what's happening, not that need to be timed

    if "DEBUG" in os.environ:
        print('\033[2;34;43m '+text+' \033[0;0m')

        #print(text)
        
def failure_print(text):
    # For when things fail to the point of breaking and don't need timing, just debug logs

    if "DEBUG" in os.environ:
        print('\033[30;41m ' + "Fatal: " + str(text) + ' \033[0m')
    else:
        print(text)

class _ReusableConnection(psycopg2.extensions.connection):
    """
    A connection whose close() rolls back and keeps it for the next get_conn() instead of disconnecting.
    Opening a postgres connection costs a new server process and an auth round trip, and a scrape used ~10 per page.
    At most one is kept idle; a get_conn() while it's in use (nested or another thread) opens its own, so transactions are never shared.
    """
    def close(self):
        if not self.closed and _IDLE_CONNS.qsize() < 1:
            try:
                self.rollback()  # leave it like a fresh connection (also clears an aborted transaction)
                _IDLE_CONNS.put(self)
                return
            except psycopg2.Error:
                pass
        super().close()

_IDLE_CONNS = queue.SimpleQueue()

def get_conn():
    """Return a psycopg2 connection using `DATABASE_URL`, reusing the idle one if there is one. Call close() when done with it"""
    while True:
        try:
            conn = _IDLE_CONNS.get_nowait()
        except queue.Empty:
            break
        if not conn.closed:
            return conn
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        conn = psycopg2.connect(database_url, connection_factory=_ReusableConnection)
        _OPEN_CONNS.add(conn)
        return conn
    else:
        failure_print("No DATABASE_URL set, not connecting to server")

# Every connection get_conn() opened that's still alive, so close_all_connections() can find ones a caller never closed
_OPEN_CONNS = weakref.WeakSet()

def close_all_connections():
    """
    Really close every open connection (idle ones too), rolling back anything uncommitted.
    Used on exit: a transaction cut off by SIGTERM (and still referenced, e.g. by the exception) keeps its row locks until the process ends, so the final requeue of the same urls would wait on it forever
    """
    for conn in list(_OPEN_CONNS):
        psycopg2.extensions.connection.close(conn)  # the base close, not _ReusableConnection's which would keep it

def create_database():
    conn = get_conn()
    cur = conn.cursor()

    # create sequences for stable id generation and tables with id defaults
    cur.execute("""
    CREATE SEQUENCE IF NOT EXISTS words_id_seq;
    CREATE SEQUENCE IF NOT EXISTS bigrams_id_seq;
    CREATE SEQUENCE IF NOT EXISTS trigrams_id_seq;
    CREATE SEQUENCE IF NOT EXISTS prefixes_id_seq;
    CREATE SEQUENCE IF NOT EXISTS urls_id_seq;

    CREATE TABLE IF NOT EXISTS words (
        word VARCHAR(64) NOT NULL PRIMARY KEY,
        id INT NOT NULL DEFAULT nextval('words_id_seq')
    );
    CREATE TABLE IF NOT EXISTS bigrams (
        bigram CHAR(2) PRIMARY KEY,
        id INT NOT NULL DEFAULT nextval('bigrams_id_seq')
    );
    CREATE TABLE IF NOT EXISTS trigrams (
        trigram CHAR(3) PRIMARY KEY,
        id INT NOT NULL DEFAULT nextval('trigrams_id_seq')
    );
    CREATE TABLE IF NOT EXISTS prefixes (
        prefix VARCHAR(64) NOT NULL PRIMARY KEY,
        id INT NOT NULL DEFAULT nextval('prefixes_id_seq')
    );
    CREATE TABLE IF NOT EXISTS urls (
        url VARCHAR(2048) NOT NULL UNIQUE,
        icon_link VARCHAR(2048),
        title VARCHAR(128),
        id INT NOT NULL DEFAULT nextval('urls_id_seq') PRIMARY KEY,
        reference_count INT NOT NULL DEFAULT 1,
        -- Sum of the counts of every word, bigram, trigram and prefix stored for this page.
        -- NULL for pages stored before this column existed.
        total_tokens INT
    );
    CREATE TABLE IF NOT EXISTS urls_references (
        url VARCHAR(2048) NOT NULL,
        referenced_domain VARCHAR(2048) NOT NULL,
        PRIMARY KEY (url, referenced_domain)
    );
                
    CREATE TABLE IF NOT EXISTS url_token_counts (
        url_id INT PRIMARY KEY REFERENCES urls(id),
        word_count INT NOT NULL DEFAULT 1,
        bigram_count INT NOT NULL DEFAULT 0,
        trigram_count INT NOT NULL DEFAULT 0,
        prefix_count INT NOT NULL DEFAULT 0
    );
                

    CREATE TABLE IF NOT EXISTS url_queue (
        id SERIAL PRIMARY KEY,
        url VARCHAR(2048) UNIQUE NOT NULL,
        enqueued_at TIMESTAMP DEFAULT now(),
        claimed_at TIMESTAMP  -- set while a scraper holds the url, see get_next_urls()
    );
    -- Older databases: add the column only if missing, since ALTER TABLE locks the whole queue even when there's nothing to do and every scraper runs this on startup
    DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'url_queue' AND column_name = 'claimed_at') THEN
            ALTER TABLE url_queue ADD COLUMN claimed_at TIMESTAMP;
        END IF;
    END $$;
    -- Claiming skips the ~100k claimed rows at the front of the queue, releasing expired claims finds them without a scan
    CREATE INDEX IF NOT EXISTS url_queue_unclaimed_idx ON url_queue (id) WHERE claimed_at IS NULL;
    CREATE INDEX IF NOT EXISTS url_queue_claimed_idx ON url_queue (claimed_at) WHERE claimed_at IS NOT NULL;

    CREATE TABLE IF NOT EXISTS logs (
        id SERIAL PRIMARY KEY,
        ts TIMESTAMP DEFAULT now(),
        ip VARCHAR(64),
        message TEXT
    );



    -- count = how many times the token appears on the page (rows from before this column existed default to 1).
    -- Existing databases get these columns from add_count_columns.py, not from here.
    CREATE TABLE IF NOT EXISTS bigram_urls (bigram_id INT NOT NULL, url_id INT NOT NULL, count INT NOT NULL DEFAULT 1);
    CREATE TABLE IF NOT EXISTS trigram_urls (trigram_id INT NOT NULL, url_id INT NOT NULL, count INT NOT NULL DEFAULT 1);
    CREATE TABLE IF NOT EXISTS prefix_urls (prefix_id INT NOT NULL, url_id INT NOT NULL, count INT NOT NULL DEFAULT 1);
    CREATE TABLE IF NOT EXISTS word_urls (word_id INT NOT NULL, url_id INT NOT NULL, count INT NOT NULL DEFAULT 1);

    CREATE TABLE IF NOT EXISTS blocked_domains (
        domain VARCHAR(512) PRIMARY KEY,
        reason VARCHAR(128),
        blocked_at TIMESTAMP DEFAULT now()
    );

    -- These all speed up requests for stats from frontend/app.py and probably other places in the future
    -- text_pattern_ops lets "message LIKE 'Sc%'" prefix scans use an index.
    CREATE INDEX IF NOT EXISTS logs_message_pattern_idx
        ON logs (message text_pattern_ops);
    -- Partial indexes for the scrape-only queries: last-N by id, per-minute and per-day counts by ts, and active-scraper grouping by ip
    CREATE INDEX IF NOT EXISTS logs_scraped_id_idx
        ON logs (id DESC) WHERE message LIKE 'Scraped%';
    CREATE INDEX IF NOT EXISTS logs_scraped_ts_idx
        ON logs (ts) WHERE message LIKE 'Scraped%';
    CREATE INDEX IF NOT EXISTS logs_scraped_ip_ts_idx
        ON logs (ip, ts) WHERE message LIKE 'Scraped%';
    -- Functional index matching the unique-domains regex in the dashboard
    CREATE INDEX IF NOT EXISTS urls_domain_idx
        ON urls ((SUBSTRING(url FROM '^(?:https?://)?([^/]+)')));

    -- Token -> page lookups (search) and page -> tokens (store() replacing a re-crawled page's postings)
    CREATE INDEX IF NOT EXISTS idx_word_urls_word_id ON word_urls(word_id);
    CREATE INDEX IF NOT EXISTS idx_word_urls_url_id ON word_urls(url_id);
    CREATE INDEX IF NOT EXISTS idx_bigram_urls_bigram_id ON bigram_urls(bigram_id);
    CREATE INDEX IF NOT EXISTS idx_bigram_urls_url_id ON bigram_urls(url_id);
    CREATE INDEX IF NOT EXISTS idx_trigram_urls_trigram_id ON trigram_urls(trigram_id);
    CREATE INDEX IF NOT EXISTS idx_trigram_urls_url_id ON trigram_urls(url_id);
    CREATE INDEX IF NOT EXISTS idx_prefix_urls_prefix_id ON prefix_urls(prefix_id);
    CREATE INDEX IF NOT EXISTS idx_prefix_urls_url_id ON prefix_urls(url_id);
    """)


    conn.commit()
    cur.close()
    conn.close()

    populate_static_ngrams()

def populate_static_ngrams():
    """Pre-populate all the bigrams/trigrams so store() doesn't have to re-insert every time"""
    chars = string.ascii_lowercase
    conn = get_conn()
    cur = conn.cursor()

    # AI MADE (this cur.execute line)
    # Skip if already populated. This runs on EVERY scraper startup, and postgres
    # consumes a sequence number for each attempted row even when ON CONFLICT DO
    # NOTHING inserts nothing — enough scraper restarts overflowed the INT id
    # columns fleet-wide ("integer out of range"). Only insert on a fresh database.
    cur.execute("SELECT (SELECT COUNT(*) FROM bigrams) >= %s AND (SELECT COUNT(*) FROM trigrams) >= %s;", (len(chars) ** 2, len(chars) ** 3))
    if cur.fetchone()[0]:
        cur.close()
        conn.close()
        return

    # bigram crap
    bigram_values = [(''.join(b),) for b in itertools.product(chars, repeat=2)]

    extras.execute_values(cur, "INSERT INTO bigrams (bigram) VALUES %s ON CONFLICT (bigram) DO NOTHING;", bigram_values)

    # trigram crap
    trigram_values = [(''.join(stupidthinghole),) for stupidthinghole in itertools.product(chars, repeat=3)]
    extras.execute_values(cur, "INSERT INTO trigrams (trigram) VALUES %s ON CONFLICT (trigram) DO NOTHING;", trigram_values)

    conn.commit()
    cur.close()
    conn.close()

def load_static_token_maps():
    global _BIGRAM_ID_MAP, _TRIGRAM_ID_MAP
    if _BIGRAM_ID_MAP is not None:
        return
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT bigram, id FROM bigrams;")
    _BIGRAM_ID_MAP = {row[0].strip(): row[1] for row in cur.fetchall()}
    cur.execute("SELECT trigram, id FROM trigrams;")
    _TRIGRAM_ID_MAP = {row[0].strip(): row[1] for row in cur.fetchall()}
    cur.close()
    conn.close()

# ----- HTML text extraction utilities
# Robots meta / X-Robots-Tag directives aimed at us by name (<meta name="stultussearchengine">, "stultussearchengine: noindex")
ROBOTS_NAME = USER_AGENT.split("/")[0].lower()
# Directives that carry a "name: value" themselves, so their colon isn't a crawler name prefix
_VALUED_DIRECTIVES = ("unavailable_after", "max-snippet", "max-image-preview", "max-video-preview")
# Links marked with any of these aren't followed or counted as references
_SKIP_LINK_RELS = {"nofollow", "sponsored", "ugc"}

def robots_directives(values):
    """
    Returns the set of directives (noindex, nofollow, noarchive, ...) in robots meta contents / X-Robots-Tag values that apply to us

    In "googlebot: noindex, nofollow" both directives are for googlebot only: a "name:" prefix applies until the next one
    """
    directives = set()
    for value in values:
        target = None  # None = every crawler
        for part in value.lower().split(","):
            name, colon, rest = part.partition(":")
            if colon and name.strip() not in _VALUED_DIRECTIVES:
                target, part = name.strip(), rest
            if part.strip() and target in (None, ROBOTS_NAME):
                directives.add(part.strip())
    if "none" in directives:
        directives |= {"noindex", "nofollow"}
    return directives

def extract_data_from_html(body, url, x_robots_tag=""):
    """
    Takes in the bs4 soup and returns the text, links, title, and favicon link from the page

    Args:
        body : string
            The html of a page
        url : string
            The url of the page
        x_robots_tag : string
            The page's X-Robots-Tag header, combined with its robots meta tags

    Returns:
        list
            [combined_text, links, title, icon_link, directives]
            combined_text : string
                The main text of the page (menus, footers etc. left out by trafilatura), None if there is none
            links : list
                All of the links on the page, as defined by <a> tags
            title : string
                The title of the page, as defined by the <title> tag
            icon_link : string
                The url to the favicon of the page
            directives : set
                The robots directives that apply to us (noindex, nofollow, noarchive, ...), see robots_directives()
            Links are [] for a nofollow page and leave out rel="nofollow"/"sponsored"/"ugc" links
    """

    # Use lxml parser for speed
    soup = BeautifulSoup(body, "lxml")
    title = soup.find('title')

    if title == None:
        title = url
        #print("Found none title")
    else:
        title = title.string
        #print("Found title:,", title)

    icon_link = (
        soup.find('link', rel='shortcut icon') or
        soup.find('link', rel='icon') or
        soup.find('link', rel=lambda x: x and x.lower() == 'icon') or
        soup.find('link', rel=lambda x: x and 'icon' in x.lower())
    )

    if icon_link:
        href = icon_link.get('href')
        if href:
            # Convert to absolute URL
            icon_link = urljoin(url, href)
        else:
            # If there's no link we still send a None variables to the database
            icon_link = None

    # Read before <head> and <meta> are dropped below
    meta_robots = [m.get("content") or "" for m in soup.find_all("meta") if (m.get("name") or "").lower() in ("robots", ROBOTS_NAME)]
    directives = robots_directives([x_robots_tag, *meta_robots])

    # Links inside these aren't followed
    for tag in soup(["script", "style", "head", "meta", "noscript"]):
        tag.decompose()

    # Relative to the page itself, so href="page2" on /blog/post resolves to /blog/page2, not /page2
    links = [] if "nofollow" in directives else [
        urljoin(url, a["href"]) for a in soup.find_all("a", href=True)
        if not _SKIP_LINK_RELS & {r.lower() for r in a.get("rel", [])}
    ]

    combined_text = trafilatura.extract(body)

    return [combined_text, links, title, icon_link, directives]

def determine_language(text):
    """
    Determines the language of a given text using langdetect

    Args:
        text : string
            The text to determine

    Returns:
        string
            The two character code (https://en.wikipedia.org/wiki/List_of_ISO_639_language_codes),
            or 'unknown' if the text has no detectable language features (e.g. only
            numbers/symbols) — langdetect raises on those instead of returning a code

    TODO:
        - Determine how long this process takes with long documents, and if it's a noticeable slowdown experiment with only passing a part of the page for detection
    """

    # THis bullshit crashes sometimes, usually because the text is too short or some other bullcrap
    try:
        return detect(text)
    except LangDetectException:
        return 'unknown'

def allowed_by_robots(url, user_agent):
    """
    Uses the robots.txt of the site and the user agent of the scraper to determine whether the scraper is allowed to scrape the page

    Args:
        url : string
            The url of the page you are scraping
        user_agent : string
            The user agent defined in the constant at the top of utils.py

    Returns:
        boolean
            Allowed to scrape if True, not allowed if False
    """

    parsed = urlparse(url)
    robots_url = urljoin(f"{parsed.scheme}://{parsed.netloc}", "robots.txt")

    try:
        # The day number is part of the cache key, so each site's robots.txt is fetched at most once a day per scraper instead of once per page
        rp = _robots_parser(robots_url, user_agent, int(time.time() // 86400))
    except Exception:
        # Network errors/timeouts or an unexpected parsing error -> be permissive so we don't block scraping (not cached, so the next page retries)
        return True

    # No robots.txt (not found or inaccessible) -> assume allowed
    return rp is None or rp.can_fetch(user_agent, url)

@functools.lru_cache(maxsize=10000)
def _robots_parser(robots_url, user_agent, day):
    """Fetch and parse one site's robots.txt. Returns None if the site has none (cached); raises on network errors (lru_cache doesn't cache exceptions)"""
    if SCRAPER_PROXY:
        resp = requests.get(f"{SCRAPER_PROXY}/fetch", params={"url": robots_url}, headers={"User-Agent": user_agent, **_PROXY_HEADERS}, timeout=5)
    else:
        resp = requests.get(robots_url, headers={"User-Agent": user_agent}, timeout=5)
    if resp.status_code != 200:
        return None
    rp = RobotFileParser()
    # rp.parse expects an iterable of lines
    rp.parse(resp.text.splitlines())
    return rp

def is_public_host(url):
    """
    True if the url's host resolves only to public internet addresses.
    Stops the crawler from following links or redirects into our own network (db01, localhost, 10.x, ...) and indexing internal pages.
    shortcut: the host is resolved again for the real request (DNS rebinding could slip past), fine while our internal services aren't high value, pin the resolved IP if that changes
    """
    host = urlparse(url).hostname
    if not host:
        return False
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except (socket.gaierror, UnicodeError):
        return False
    return all(ipaddress.ip_address(a.split("%", 1)[0]).is_global for a in addresses)  # split drops an IPv6 zone like %eth0

def get_page_html(url):
    """
    Download the html of a page

    Same-host redirects (http -> https, /old -> /new) are followed here, each hop checked against robots.txt.
    Redirects to another host aren't followed: they're returned as REDIRECTED so the scraper queues the target, which then gets that host's blocklist, robots.txt and cooldown checks like any other url

    Args:
        url : string
            The url of the page you are scraping

    Returns:
        (result, final_url, x_robots_tag)
            result is one of:
                bytes         the raw html of the page
                RATE_LIMITED  the site answered 429
                REDIRECTED    the page redirects to another host, final_url is the target
                NOT_SCRAPABLE the site's answer won't change on a retry (robots.txt, not html, too large, redirect loop), so the scraper records the url as seen
                False         the page can't be scraped right now (error status, network error, not a public address)
            final_url is the url the result came from, after any redirects (not cleaned, so relative links resolve against it)
            x_robots_tag is the page's X-Robots-Tag header ("" if none), for extract_data_from_html
    """

    if not is_public_host(url):
        log(f"Error Not a public address {url}")
        return False, url, ""

    if not allowed_by_robots(url, USER_AGENT):
        log(f"Blocked by robots.txt {url}")
        info_print("Not allowed by robots.txt")
        return NOT_SCRAPABLE, url, ""

    headers = {
        "User-Agent": USER_AGENT,
        "From": "crawler@stultus.rip"
    }

    def fetch(target):
        # One hop: redirects are followed below so every target is checked first
        # stream=True: only the headers are read here, so status and Content-Type are checked before any body is downloaded
        if SCRAPER_PROXY:
            return requests.get(f"{SCRAPER_PROXY}/fetch", params={"url": target}, headers={**headers, **_PROXY_HEADERS}, timeout=(5, 10), allow_redirects=False, stream=True)
        return requests.get(target, headers=headers, timeout=(5, 10), allow_redirects=False, stream=True)

    request = None
    try:
        request = fetch(url)
        redirect_hops = 0
        while request.is_redirect:
            location = request.headers.get("Location", "")
            if not location or redirect_hops >= 5:
                log(f"Error Empty redirect or too many redirects {url}")
                return NOT_SCRAPABLE, url, ""
            target = urljoin(url, location)  # relative Location -> absolute target url
            if urlparse(target).hostname != urlparse(url).hostname:
                return REDIRECTED, target, ""
            url = target
            request.close()
            if not is_public_host(url):
                log(f"Error Redirect to a non-public address {url}")
                return False, url, ""
            if not allowed_by_robots(url, USER_AGENT):
                log(f"Blocked by robots.txt {url}")
                return NOT_SCRAPABLE, url, ""
            request = fetch(url)
            redirect_hops += 1

        content_type = request.headers.get("Content-Type", "").lower()

        # Skip just this page: one protected url (/admin, a bot challenge) shouldn't block the whole host
        if request.status_code in (401, 403):
            log(f"Error HTTP {request.status_code} {url}")
            return False, url, ""

        if request.status_code == 429:
            log(f"Error Rate limited {url}")
            return RATE_LIMITED, url, ""

        # Only allow HTML content
        if not (
            content_type.startswith("text/html")
        ):
            log(f"Error Invalid data type {url}")
            # An error status isn't final: our proxy's own 502s look like this too
            return (NOT_SCRAPABLE if request.status_code < 400 else False), url, ""

        # Read at most MAX_PAGE_BYTES; a huge page would otherwise be loaded into memory whole
        content = bytearray()
        for chunk in request.iter_content(chunk_size=64 * 1024):
            content += chunk
            if len(content) > MAX_PAGE_BYTES:
                log(f"Error Page too large {url}")
                return NOT_SCRAPABLE, url, ""
        content = bytes(content)

    except TimeoutError:
        log(f"Error Total timeout exceeded {url}")
        return False, url, ""
    except requests.exceptions.RequestException as e:
        if SCRAPER_PROXY and isinstance(e, requests.exceptions.ConnectionError):
            raise  # couldn't reach our own proxy, not the page: scrape.py retries the url later
        log(f"Error HTTP error fetching: {url} : {e}")
        return False, url, ""
    finally:
        if request is not None:
            request.close()

    return content, url, request.headers.get("X-Robots-Tag", "")

def log(message):
    """
    Logs the message in the database logs

    Args:
        message : string
            The message for the logs
        
    Returns:
        Nothing

    TODO:
        - Change the logs table to add a column for error/success/misc
        - Add a table for success/error rate, an integer in the table should be increase by one for each success/error
        - Add a column to the table for the url so we don't have to parse all message to find those pertaining to a url
    
    Notes:
        Logs must be in this format:
        - Errors: Error {error message} {url}
        - Scraped: Scraped {url}
        - Misc: Misc {message} {url}
    """

    if NODB:
        debug_print(message)
        return

    try:
        log_db(message)
    except Exception:
        ## TODO: This message isn't compliant with the proper debug_print formatting, fix that and make the formatting guidelines more clear
        debug_print("FAILURE COULDN'T WRITE TO DATABASE")
        pass

def get_base_domain(url):
    """
    Return the base domain name of a given url

    Args:
        url : string
            A url of a page

    Returns:
        string
            The base domain of the given url (minus the https:// and the /subpage)

    TODO:
        - Add to this doc whether the domain is returned with a / on the end
    """

    if "://" not in url:
        url = "http://" + url
    host = urlparse(url).hostname
    if not host:
        return ""
    ext = tldextract.extract(host)
    if ext.top_domain_under_public_suffix:
        return ext.top_domain_under_public_suffix
    return host

def clean_links(raw_links):
    """
    Takes in raw links and cleans them for storage in the index. Removes ? post parameter and # things and non-http links and any trailing slashes /

    Args:
        raw_links : list[string]
            A list of strings which are raw links from the internet
    
    Returns:
        raw_links : list[strings]
            A list of strings which are clean links
    """

    # Clean links, this is copied from scrape.py, should probably have a shred function but it's 10:55pm and I'm tired
    # This above comment caused issues. The cleaning here is for cleaning links to add to the scraping, while cleaning in scrape.py is for references so it just need to be the base domains
    cleaned = []
    seen = set()  # same contents as cleaned; a set so checking for duplicates doesn't rescan the list (O(n^2) on pages with thousands of links)

    for link in raw_links:
        clean_link = link.split('?', 1)[0] #gets rid of anything after a ?
        clean_link = clean_link.split('#', 1)[0] #gets rid of anything after a #

        slash_index = clean_link.rfind("/") #finds where the last "/" is in a string and returns the index of that
        clean_link_length = len(clean_link)-1 #returns max index value of the link
        if slash_index == clean_link_length:
            clean_link = clean_link[:-1] #gets rid of the "/" if a link ends with it

        # Scheme and host are case-insensitive, so HTTPS://Example.com/a and https://example.com/a are one url (the path is case-sensitive, left alone)
        scheme_end = clean_link.find("://")
        if scheme_end != -1:
            host_end = clean_link.find("/", scheme_end + 3)
            if host_end == -1:
                host_end = len(clean_link)
            clean_link = clean_link[:host_end].lower() + clean_link[host_end:]
            
        if clean_link not in seen:
            # Gets rid of links that aren't http(s) and are longer than the max url length (as dictated by the postgres length limits)
            if clean_link[0:4] == "http" and len(clean_link) <= MAX_URL_LENGTH:
                cleaned.append(clean_link)
                seen.add(clean_link)

    return cleaned


def store(content, url):
    """
    Stores the data (text, links, title, icon_link) of a given page in the index database
    """
    global URL
    URL = url

    # No database available (e.g. --nodb debugging run): nothing below this point can run
    # without a connection, so bail out before any DB access.
    if NODB: return

    # If the url is longer than we can store fail storing the whole thing, there's no point in storing page information is we can't store the primary key which connects the information (the url)
    if len(url) > MAX_URL_LENGTH:
        info_print(f"URL longer than {MAX_URL_LENGTH} chars, not storing {url[:80]}...")
        return

    load_static_token_maps()

    debug_print("Visited site, got main text and links")

    text, links, title, icon_link = content[:4]
    
    # Making sure there is text at all. I chose 4 arbitrarily
    if text != None and len(text) < 4:
        mark_url_seen(url)  # so other pages linking here don't queue it again
        return [links, False]
    
    """
    # MOVED THIS TO SCRAPE.PY  
    # Check for english language
    if detect(text) != 'en':
        # TODO: Add something here to the logs logging which pages are in what language so we can measure how much of the web is in what language
        log(f"Language Not in English {url}")
        info_print("Language not in English, skipping")

        return [links, False]

    debug_print("Detected language")
    """

    # Tokenize the text of the page, counting how many times each token appears
    tokens = tokenizer.tokenize_all_counts(text)
    debug_print("Tokenized")

    # A clean list of the links in the scraped page
    links = clean_links(links)

    # tokens: [word_counts, bigram_counts, trigram_counts, prefix_counts], each a Counter of token -> times it appears on the page
    word_counts = {w: n for w, n in tokens[0].items() if 0 < len(w) <= MAX_TOKEN_LENGTH}
    bigram_counts = tokens[1]
    trigram_counts = tokens[2]
    prefix_counts = {p: n for p, n in tokens[3].items() if 0 < len(p) <= MAX_TOKEN_LENGTH}

    # word_list keeps repeats so url_token_counts.word_count is the same as before
    word_list = [w for w, n in word_counts.items() for _ in range(n)]
    words = set(word_counts)
    bigrams = set(bigram_counts)
    trigrams = set(trigram_counts)
    prefixes = set(prefix_counts)
    debug_print("Cleaned links and split tokens into words, bigrams, trigrams, prefixes")


    conn = get_conn()
    cur = conn.cursor()

    #debug_print("Getting SQL connection and cursor:")

    # Cut down to title and url lengths. Probably shouldn't store the icon link if it's concatenated and thus doesn't work but it's 11:44pm and I'm tired
    if title:
        title = title[:MAX_TITLE_LENGTH]
    if icon_link and len(icon_link) > MAX_URL_LENGTH:
        icon_link = None

    # Every word, bigram, trigram and prefix occurrence stored for this page (only tokens that get a row below are counted)
    total_tokens = (sum(word_counts.values())
                    + sum(n for b, n in bigram_counts.items() if b in _BIGRAM_ID_MAP)
                    + sum(n for t, n in trigram_counts.items() if t in _TRIGRAM_ID_MAP)
                    + sum(prefix_counts.values()))

    # Upsert the URL and get its id. Use RETURNING id when inserting; else SELECT.
    cur.execute("INSERT INTO urls (url, title, icon_link, total_tokens) VALUES (%s, %s, %s, %s) ON CONFLICT (url) DO NOTHING RETURNING id;", (url, title, icon_link, total_tokens))
    row = cur.fetchone()
    if row:
        url_id = row[0]
    else:
        # Already stored (re-crawl or two scrapers on the same url): the UPDATE locks the row so
        # concurrent stores of this url run one at a time, and refreshes total_tokens for the new scrape
        cur.execute("UPDATE urls SET total_tokens = %s WHERE url = %s RETURNING id;", (total_tokens, url))
        url_id = cur.fetchone()[0]
        for table in ("word_urls", "bigram_urls", "trigram_urls", "prefix_urls"):
            cur.execute(sql.SQL("DELETE FROM {} WHERE url_id = %s;").format(sql.Identifier(table)), (url_id,))


    cur.execute("""
        INSERT INTO url_token_counts (url_id, word_count, bigram_count, trigram_count, prefix_count)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (url_id)
        DO UPDATE SET
            word_count = EXCLUDED.word_count,
            bigram_count = EXCLUDED.bigram_count,
            trigram_count = EXCLUDED.trigram_count,
            prefix_count = EXCLUDED.prefix_count;
    """, (
        url_id,
        len(word_list),
        len(bigrams),
        len(trigrams),
        len(prefixes)
    ))
    
    # Bulk insert new words/prefixes (bigrams/trigrams are pre-populated).
    # Only rows that don't exist yet: postgres burns an id for every attempted row even when ON CONFLICT skips it, which overflows the INT ids.
    # ORDER BY: every scraper locks new tokens in the same order, otherwise two scrapers inserting the same new words deadlock.
    for column, table, tokens in (("word", "words", words), ("prefix", "prefixes", prefixes)):
        if tokens:
            cur.execute(sql.SQL("""
                INSERT INTO {tbl} ({col})
                SELECT t FROM unnest(%s::varchar[]) AS t
                WHERE NOT EXISTS (SELECT 1 FROM {tbl} WHERE {col} = t)
                ORDER BY t
                ON CONFLICT ({col}) DO NOTHING;
            """).format(col=sql.Identifier(column), tbl=sql.Identifier(table)), (list(tokens),))

    if links:
        extra_vals = [(url, l) for l in links]
        extras.execute_values(cur, "INSERT INTO urls_references (url, referenced_domain) VALUES %s ON CONFLICT DO NOTHING;", extra_vals, template=None)


    # Fetch ids for all tokens in bulk
    def fetch_id_map(column, table, items):
        if not items:
            return {}
        cur.execute(sql.SQL("SELECT id, {col} FROM {tbl} WHERE {col} = ANY(%s);").format(
            col=sql.Identifier(column), tbl=sql.Identifier(table)
        ), (list(items),))
        rows = cur.fetchall()
        return {val: id for (id, val) in rows}
    
    debug_print("Added new, unseen tokens to the database")

    word_map = fetch_id_map('word', 'words', list(words))
    bigram_map = _BIGRAM_ID_MAP
    trigram_map = _TRIGRAM_ID_MAP
    prefix_map = fetch_id_map('prefix', 'prefixes', list(prefixes))

    debug_print("Made token maps")

    # Prepare mapping inserts (token id, url id, times the token appears on the page) and bulk insert them
    word_url_pairs = [(word_map[w], url_id, n) for w, n in word_counts.items() if w in word_map]
    bigram_url_pairs = [(bigram_map[b], url_id, n) for b, n in bigram_counts.items() if b in bigram_map]
    trigram_url_pairs = [(trigram_map[t], url_id, n) for t, n in trigram_counts.items() if t in trigram_map]
    prefix_url_pairs = [(prefix_map[p], url_id, n) for p, n in prefix_counts.items() if p in prefix_map]

    debug_print("Made insert stementes for <token>_url tables")

    if word_url_pairs:
        extras.execute_values(cur,
            "INSERT INTO word_urls (word_id, url_id, count) VALUES %s;",
            word_url_pairs)

    if bigram_url_pairs:
        extras.execute_values(cur,
            "INSERT INTO bigram_urls (bigram_id, url_id, count) VALUES %s;",
            bigram_url_pairs)

    if trigram_url_pairs:
        extras.execute_values(cur,
            "INSERT INTO trigram_urls (trigram_id, url_id, count) VALUES %s;",
            trigram_url_pairs)

    if prefix_url_pairs:
        extras.execute_values(cur,
            "INSERT INTO prefix_urls (prefix_id, url_id, count) VALUES %s;",
            prefix_url_pairs)

    debug_print("Executed insert statements, stored url.id_token.id pairs")

    conn.commit()
    cur.close()
    conn.close()

    debug_print("Committed and closed sql connection")

    return links

def mark_url_seen(url):
    """Record a url without indexing its text (no tokens, so it never shows up in search), so filter_new_urls() won't queue it again"""
    if NODB or len(url) > MAX_URL_LENGTH:
        return
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO urls (url) VALUES (%s) ON CONFLICT (url) DO NOTHING;", (url,))
    conn.commit()
    cur.close()
    conn.close()

# Queue and logging helpers (Postgres-backed)
def get_next_urls(num_urls):
    """
    Claims the next urls in the queue ([] if empty) for this scraper

    Claimed urls stay in url_queue with claimed_at set, so filter_new_urls() and enqueue's ON CONFLICT still see them and no other scraper queues or claims them again.
    finish_claims() deletes or hands them back; a claim never finished (scraper killed) is handed back after CLAIM_TIMEOUT
    """

    conn = get_conn()
    cur = conn.cursor()

    # SKIP LOCKED: scrapers releasing the same expired rows don't wait on (or deadlock with) each other
    cur.execute("""
        UPDATE url_queue SET claimed_at = NULL WHERE id IN (
            SELECT id FROM url_queue WHERE claimed_at < now() - %s::interval FOR UPDATE SKIP LOCKED
        );
    """, (CLAIM_TIMEOUT,))
    # SKIP LOCKED in one statement so concurrent scrapers never claim the same rows
    cur.execute("""
        UPDATE url_queue SET claimed_at = now() WHERE id IN (
            SELECT id FROM url_queue WHERE claimed_at IS NULL ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED
        ) RETURNING url;
    """, (num_urls,))
    urls = [row[0] for row in cur.fetchall()]

    conn.commit()
    cur.close()
    conn.close()

    return urls

def finish_claims(done=(), release=()):
    """
    Ends this scraper's claims on urls from get_next_urls(): done urls leave the queue, released ones go back for any scraper to claim
    """
    if not done and not release:
        return

    conn = get_conn()
    cur = conn.cursor()
    if done:
        cur.execute("DELETE FROM url_queue WHERE url = ANY(%s);", (list(done),))
    if release:
        # An UPDATE, not a re-insert: re-inserting burns a SERIAL id per url, and cooldown skips hand back a lot of urls
        cur.execute("UPDATE url_queue SET claimed_at = NULL WHERE url = ANY(%s);", (list(release),))
    conn.commit()
    cur.close()
    conn.close()

def get_host_ip():
    """Return the host machine IP address. Try local hostname first, then fallback to external lookup."""
    try:
        host_ip = socket.gethostbyname(socket.gethostname())
        if host_ip and not host_ip.startswith("127."):
            return host_ip
    except Exception:
        pass
    return "unknown"


def log_db(message):
    """Insert a log message into the `logs` table with timestamp and host IP."""
    #start=time.time()
    conn = get_conn()
    cur = conn.cursor()
    ip = get_host_ip()
    cur.execute("INSERT INTO logs (ip, message) VALUES (%s, %s);", (ip, message))
    conn.commit()
    cur.close()
    conn.close()
    #print("DB Log time:", time.time()-start)

def enqueue_urls(urls):
    if not urls:
        return

    conn = get_conn()
    cur = conn.cursor()

    query = """
        INSERT INTO url_queue (url)
        VALUES %s
        ON CONFLICT (url) DO NOTHING;
    """

    # execute_values handles building the bulk values list efficiently
    execute_values(cur, query, [(u,) for u in urls])

    conn.commit()
    cur.close()
    conn.close()


def filter_new_urls(urls):
    """
    Return a list of URLs from `urls` that are not present in the
    `url_queue` table and not already stored in `urls` table.

    This performs two bulk lookups (one against `url_queue`, one against
    `urls`) and preserves the input order while removing duplicates.
    """

    if not urls:
        return []

    # preserve order and deduplicate
    unique = list(dict.fromkeys(urls))

    conn = get_conn()
    cur = conn.cursor()

    # fetch queued URLs present in input
    cur.execute("SELECT url FROM url_queue WHERE url = ANY(%s);", (unique,))
    queued_rows = cur.fetchall()
    queued = set(r[0] for r in queued_rows)

    # fetch already-stored URLs present in input
    cur.execute("SELECT url FROM urls WHERE url = ANY(%s);", (unique,))
    stored_rows = cur.fetchall()
    stored = set(r[0] for r in stored_rows)

    cur.close()
    conn.close()

    result = [u for u in unique if u not in queued and u not in stored]
    return result


# ------------ Redis functions

def mark_domain(domain, redis_client):
    # Called after a scrape finishes: restart the 10 second cooldown so it also counts from the end of a slow scrape
    redis_client.set(f"domain:{domain}", 1, ex=10)

def claim_domain(domain, redis_client):
    # Atomically claims the domain for 10 seconds (SET NX): True if it was free and is now ours, False if another scraper touched it in the last 10 seconds
    # Check and claim must be one step, otherwise scrapers that check at the same time all see it free
    return bool(redis_client.set(f"domain:{domain}", 1, nx=True, ex=10))

def _refresh_blocklist_if_stale():
    """
    If the local blocklist.db is missing or was downloaded more than an hour
    ago, re-download it from db01 (port 8002/blocklist.db)

    The age is the local file's, not its last_updated row: if update_local_blocklist.py
    stops running, a fresh download would still look stale and be fetched again for every url
    """

    try:
        if time.time() - os.path.getmtime("blocklist.db") < 3600:
            return
    except OSError:
        pass  # no copy yet

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        failure_print("Cannot refresh blocklist: DATABASE_URL not set")
        return
    host = urlparse(database_url).hostname
    if not host:
        failure_print("Cannot refresh blocklist: no host in DATABASE_URL")
        return

    download_url = f"http://{host}:8002/blocklist.db"
    tmp_path = "blocklist.db.tmp"
    try:
        with requests.get(download_url, stream=True, timeout=30) as resp:
            resp.raise_for_status()
            with open(tmp_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
    except requests.RequestException as e:
        if not os.path.exists("blocklist.db"):
            raise  # no copy at all: scraping unchecked isn't allowed, scrape.py retries the url later
        failure_print(f"Could not refresh blocklist, using the old copy: {e!r}")
        os.utime("blocklist.db", (time.time() - 3300,) * 2)  # try again in 5 minutes, not on every url
        return
    os.replace(tmp_path, "blocklist.db")

def check_url_in_blocklist(url):
    """
    Takes url, checks if its exact host or base domain is in blocklist.db (Steven Black's hosts + postgres blocked_domains)

    Exact host catches the hosts file's subdomain entries, base domain catches opt-outs (example.com also covers www.example.com)

    Returns True if blocked (cannot scrape), False if not
    """

    domains = (urlparse(url).hostname, get_base_domain(url))

    _refresh_blocklist_if_stale()

    conn = sqlite3.connect("blocklist.db")
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM blocklist WHERE domain IN (?, ?)", domains)
    result = cursor.fetchone()
    conn.close()

    return(result is not None)

# ================= Functions for sending data to the web text storage server
def _gzip_chunks(source, gzip_level=6):
    
    #Yield gzip-compressed chunks from source, an iterable of str/bytes pieces. Compresses incrementally so the full compressed body is never held at once
    
    compressor = zlib.compressobj(gzip_level, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    for piece in source:
        if isinstance(piece, str):
            piece = piece.encode("utf-8")
        out = compressor.compress(piece)
        if out:
            yield out
    tail = compressor.flush()
    if tail:
        yield tail

def send_page_text(server, url, text=None, path=None, title=None, gzip_level=6, chunk_size=WEB_STORAGE_CHUNK_SIZE, timeout=60):
    """
    Send one page's text to the storage server streamed with gzip

    Provide exactly one of:
        text: a str (or bytes) already in memory, or
        path:  a path to a file on disk to stream from (best for very large text, since it never loads the whole file into memory)

    """

    if (text is None) == (path is None):
        raise ValueError("pass exactly one of `text` or `path`")

    def source():
        if path is not None:
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk
        else:
            data = text.encode("utf-8") if isinstance(text, str) else text
            for i in range(0, len(data), chunk_size):
                yield data[i:i + chunk_size]

    params = {"url": url}
    if title is not None:
        params["title"] = title

    resp = requests.post(
        server.rstrip("/") + "/store",
        params=params,
        data=_gzip_chunks(source(), gzip_level),  # generator => streamed upload
        headers={
            "Content-Encoding": "gzip",
            "Content-Type": "text/plain; charset=utf-8",
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.text


if not SCRAPER_PROXY:
    info_print("Not using scraper proxy")
