"""
Functions for scraping

"""

import os
import time
import requests
import socket
from bs4 import BeautifulSoup
from bs4.element import Comment
from urllib.parse import urlparse, urljoin
from urllib.robotparser import RobotFileParser
import tldextract
import tokenizer
import psycopg2
from psycopg2 import sql
import psycopg2.extras as extras
from psycopg2.extras import execute_values
from langdetect import detect
import signal

USER_AGENT = "StultusSearchEngine/1.0 (+https://github.com/ThisIsNotANamepng/search_engine; hagenjj4111@uwec.edu)"
DEBUG_BREAKPOINT_TIMER = time.time()

# I don't think these are necessary for now (for debug printing)
BREAKPOINTS = [
    "Sent to the database",
    "Get back from the database"
    "Tokenizing",
    "Tokenized"
]


def debug_print(text):
    """
    Print system stats when DEBUG is set 
    
    DEBUG = 1 Prints for each print statement
    DEBUG = 2 Prints for each print statement and includes timing

    TODO:
        - All debug print (info_print and failure_print too) should have a option, maybe DEBUG=3 to write all of the debug stuff to a file log as well as printing
    """

    global DEBUG_BREAKPOINT_TIMER

    def format_time(timestamp):
        return f"{timestamp:.4f}"

    if "DEBUG" in os.environ:

        if text == "":
            # Just wants to reset the timer, functions to track the timing for aren't next to each other, need to reset the time in the debug_breakpoint_timer to zero before starting new function
            DEBUG_BREAKPOINT_TIMER = time.time()
            return
        
        level = os.environ["DEBUG"]

        if level == "1":
            print(text)

        elif level == "2":

            print(format_time(time.time()-DEBUG_BREAKPOINT_TIMER), text)

            DEBUG_BREAKPOINT_TIMER = time.time()

def info_print(text):
    # For debugging and printing statements which notify what's happening, not that need to be timed

    if "DEBUG" in os.environ:
        print('\033[2;34;43m '+text+' \033[0;0m')

        #print(text)
        
def failure_print(text):
    # For when things fail to the point of breaking and don't need timing, just debug logs

    print(text)

def get_conn():
    """Return a new psycopg2 connection using `DATABASE_URL` or PG_* env vars."""
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        return psycopg2.connect(database_url)
    else:
        failure_print("Failed to connect to server")

    #return psycopg2.connect(host=host, port=port, user=user, password=password, dbname=dbname)

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
        url VARCHAR(2048) NOT NULL PRIMARY KEY,
        id INT NOT NULL DEFAULT nextval('urls_id_seq')
    );
    CREATE TABLE IF NOT EXISTS urls_references (
        url VARCHAR(2048) NOT NULL PRIMARY KEY,
        referenced_domain VARCHAR(2048) NOT NULL
    );
    CREATE TABLE IF NOT EXISTS total_references (
        domain VARCHAR(2048) NOT NULL PRIMARY KEY,
        total_references INT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS entities (
        entity_text VARCHAR(2048) NOT NULL PRIMARY KEY,
        id INT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS entity_urls (
        url VARCHAR(2048) NOT NULL PRIMARY KEY,
        entity_url INT NOT NULL
    );


    CREATE TABLE IF NOT EXISTS bigram_urls (bigram_id INT NOT NULL, url_id INT NOT NULL);
    CREATE TABLE IF NOT EXISTS trigram_urls (trigram_id INT NOT NULL, url_id INT NOT NULL);
    CREATE TABLE IF NOT EXISTS prefix_urls (prefix_id INT NOT NULL, url_id INT NOT NULL);
    CREATE TABLE IF NOT EXISTS word_urls (word_id INT NOT NULL, url_id INT NOT NULL);

    CREATE TABLE IF NOT EXISTS weights (type TEXT PRIMARY KEY, weight FLOAT NOT NULL);
    """)

    # create queue and logs tables as well
    _extend_create_database_tables(cur)

    conn.commit()
    cur.close()
    conn.close()

    set_default_weights()

def set_default_weights():
    conn = get_conn()
    cur = conn.cursor()

    # Upsert default weights
    cur.execute("""
    INSERT INTO weights (type, weight) VALUES (%s, %s)
    ON CONFLICT (type) DO UPDATE SET weight = EXCLUDED.weight;
    """, ("word", 1.7))
    cur.execute("""
    INSERT INTO weights (type, weight) VALUES (%s, %s)
    ON CONFLICT (type) DO UPDATE SET weight = EXCLUDED.weight;
    """, ("bigram", 1.2))
    cur.execute("""
    INSERT INTO weights (type, weight) VALUES (%s, %s)
    ON CONFLICT (type) DO UPDATE SET weight = EXCLUDED.weight;
    """, ("trigram", 1.3))
    cur.execute("""
    INSERT INTO weights (type, weight) VALUES (%s, %s)
    ON CONFLICT (type) DO UPDATE SET weight = EXCLUDED.weight;
    """, ("prefix", 1.2))

    conn.commit()
    cur.close()
    conn.close()

def exists(text, type_):
    # Keep function for compatibility but prefer upserts/bulk operations
    # Not used anymore

    # Returns true if token has been used in the database, false if it's new

    conn = get_conn()
    cur = conn.cursor()

    if type_ == "word":
        cur.execute("SELECT 1 FROM words WHERE word = %s;", (text,))
    elif type_ == "bigram":
        cur.execute("SELECT 1 FROM bigrams WHERE bigram = %s;", (text,))
    elif type_ == "trigram":
        cur.execute("SELECT 1 FROM trigrams WHERE trigram = %s;", (text,))
    elif type_ == "prefix":
        cur.execute("SELECT 1 FROM prefixes WHERE prefix = %s;", (text,))
    elif type_ == "url":
        cur.execute("SELECT 1 FROM urls WHERE url = %s;", (text,))
    else:
        cur.close()
        conn.close()
        return False

    found = cur.fetchone() is not None

    cur.close()
    conn.close()
    return found

# HTML text extraction utilities
def tag_visible(element):
    if element.parent.name in ["style", "script", "head", "title", "meta", "[document]"]:
        return False
    if isinstance(element, Comment):
        return False
    return True

def text_from_html(body, url):
    # Use lxml parser for speed
    soup = BeautifulSoup(body, "lxml")

    # Remove tags that are not relevant for visible text
    for tag in soup(["script", "style", "head", "meta", "noscript"]):
        tag.decompose()

    # Prepare base URL for relative links
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    texts = []
    links = []

    # Single tree traversal for both text and links
    for element in soup.descendants:
        if element.name == "a" and element.has_attr("href"):
            links.append(urljoin(base_url, element["href"]))
        elif isinstance(element, str):
            # Only collect non-whitespace text
            t = element.strip()
            if t:
                texts.append(t)

    # Join all text fragments into one string
    combined_text = " ".join(texts)

    return combined_text, links

def allowed_by_robots(url, user_agent):
        
    parsed = urlparse(url)
    robots_url = urljoin(f"{parsed.scheme}://{parsed.netloc}", "robots.txt")

    rp = RobotFileParser()
    # Avoid RobotFileParser.read() which uses urllib without a timeout and may block.
    # Instead, fetch robots.txt with `requests` using a timeout and feed the contents
    # to the parser via `rp.parse()`.
    try:
        rp.set_url(robots_url)
        resp = requests.get(robots_url, headers={"User-Agent": user_agent}, timeout=5)
        if resp.status_code != 200:
            # If robots.txt not found or inaccessible, assume allowed
            return True
        # rp.parse expects an iterable of lines
        rp.parse(resp.text.splitlines())
    except requests.exceptions.RequestException:
        # Network errors/timeouts -> be permissive so we don't block scraping
        return True
    except Exception:
        # Any unexpected parsing error, be permissive
        return True

    return rp.can_fetch(user_agent, url)


def get_main_text(url, timeout=None):
    """Fetch URL and extract visible text and links.

    `timeout` is passed to `requests.get` (both connect and read timeout).
    On network errors or timeouts, returns empty text and empty links list.
    """

    def handler(signum, frame):
        log(f"Error URL took too long to download {url}")

    if not allowed_by_robots(url, USER_AGENT):
        log(f"Blocked by robots.txt {url}")
        info_print("Not allowed by robots.txt")
        return "", []

    headers = {
        "User-Agent": USER_AGENT,
        "From": "hagenjj4111@uwec.edu"
    }

    signal.signal(signal.SIGALRM, handler)
    signal.alarm(timeout)

    try:
        r = requests.get(url, headers=headers, timeout=(5, 10))

        content_type = r.headers.get("Content-Type", "").lower()

        # Check for text and pdf only
        if not content_type.startswith("text/") and not content_type == "application/pdf":
            log(f"Error Invalid data type {url}")
            return False
        

        # Check for concerning http error codes

        content = r.content
            
    except TimeoutError:
        log(f"Error Total timeout exceeded {url}")
        return False
    except requests.exceptions.RequestException as e:
        log(f"Error HTTP error fetching: {url} : {e}")
        return "", []
    finally:
        #print(5)
        signal.alarm(0)

    
    return text_from_html(content, url)

def log(message):
    """
    Logs the message in the database logs

    Logs must be in this format:
    - Errors: Error {error message} {url}
    - Scraped: Scraped {url}
    - Misc: Misc {message} {url}

    This is for measuring success/error rate in the dashboard

    TODO:
        - Change the logs table to add a column for error/success/misc
        - Add a table for success/error rate, an integer in the table should be increase by one for each success/error
    """
    # write to local file
    #with open("scraper.log", "a") as f:
    #    f.write(str(time.time()) + ": " + message + "\n")
    # Write to database logs
    try:
        log_db(message)
    except Exception:
        pass


def get_scraped_urls():
    visited = set()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT url FROM urls;")
    rows = cur.fetchall()
    for (url,) in rows:
        visited.add(url)
    cur.close()
    conn.close()
    return visited


def get_base_domain(url):
    if "://" not in url:
        url = "http://" + url
    host = urlparse(url).hostname
    if not host:
        return ""
    ext = tldextract.extract(host)
    if ext.registered_domain:
        return ext.registered_domain
    return host


def store(url, timeout=None):
    """
    Store the page at `url` and return discovered links.

    If `timeout` is provided it is forwarded to HTTP fetch.
    """
    #info_print("Starting storing")

    content = get_main_text(url, timeout=timeout)

    debug_print("Visited site, got main text and links")

    if content != False:
        # The url contains real text to scrape
        text = content[0]
        links = content[1]
    else:
        # Url is a file format which cannot be scraped
        ## TODO: I don't think this completely works, need to test with a abunch of file formats so we don't try to scrape files like images and stuff

        info_print("URL stores a file format we can't scrape")
        return

    # Check for english language
    if detect(text) != 'en':
        # TODO: Add something here to the logs logging which pages are in what language so we can measure how much of the web is in what language
        log(f"Language Not in English {url}")
        info_print("Language not in English, skipping")

        return [links, False]

    debug_print("Detected language")


    tokens = tokenizer.tokenize_all(text) #TODO: I want to see how expensive it is to check if each token is in the database already, you'd have to search all of the tokens to find if it's in the database

    debug_print("Tokenized")

    if not text:
        log(f"Error Failed to retrieve page text {url}")
        failure_print("Error Failed to retrieve page text")
        return links
        

    # Clean links, this is copied from scrape.py, should probably have a shred function but it's 10:55pm and I'm tired
    raw_links = [i for i in links if "mailto:" not in i]
    cleaned = []

    for link in raw_links:
        # Get rid of ?post=data
        clean_link = link.split('?', 1)[0]
        clean_link = link.split('#')[0]
        if clean_link not in cleaned:
            link_domain = get_base_domain(clean_link)
            if clean_link[0:4] == "http" and get_base_domain(url) != link_domain:
                cleaned.append(link_domain)

    # A list of the links in the scraped page
    links = cleaned

    # tokens: [words, bigrams, trigrams, prefixes]
    words = set(tokens[0]) if tokens and len(tokens) > 0 else set()
    bigrams = set(tokens[1]) if tokens and len(tokens) > 1 else set()
    trigrams = set(tokens[2]) if tokens and len(tokens) > 2 else set()
    prefixes = set(tokens[3]) if tokens and len(tokens) > 3 else set()

    debug_print("Cleaned links and split tokens into words, bigrams, trigrams, prefixes")


    conn = get_conn()
    cur = conn.cursor()

    #debug_print("Getting SQL connection and cursor:")

    # Upsert the URL and get its id. Use RETURNING id when inserting; else SELECT.
    cur.execute("INSERT INTO urls (url) VALUES (%s) ON CONFLICT (url) DO NOTHING RETURNING id;", (url,))
    row = cur.fetchone()
    if row:
        url_id = row[0]
    else:
        cur.execute("SELECT id FROM urls WHERE url = %s;", (url,))
        url_id = cur.fetchone()[0]

    # Bulk insert words/bigrams/trigrams/prefixes using execute_values for speed.
    if words:
        extra_vals = [(w,) for w in words]
        extras.execute_values(cur,
            "INSERT INTO words (word) VALUES %s ON CONFLICT (word) DO NOTHING;",
            extra_vals,
            template=None)

    if bigrams:
        extra_vals = [(b,) for b in bigrams]
        extras.execute_values(cur,
            "INSERT INTO bigrams (bigram) VALUES %s ON CONFLICT (bigram) DO NOTHING;",
            extra_vals)

    if trigrams:
        extra_vals = [(t,) for t in trigrams]
        extras.execute_values(cur,
            "INSERT INTO trigrams (trigram) VALUES %s ON CONFLICT (trigram) DO NOTHING;",
            extra_vals)

    if prefixes:
        extra_vals = [(p,) for p in prefixes]
        extras.execute_values(cur,
            "INSERT INTO prefixes (prefix) VALUES %s ON CONFLICT (prefix) DO NOTHING;",
            extra_vals)
        
    if links:
        extra_vals = [(url, l) for l in links]
        extras.execute_values(cur,
            "INSERT INTO urls_references (url, referenced_domain) VALUES %s ON CONFLICT (url) DO NOTHING;",
            extra_vals,
            template=None)


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
    bigram_map = fetch_id_map('bigram', 'bigrams', list(bigrams))
    trigram_map = fetch_id_map('trigram', 'trigrams', list(trigrams))
    prefix_map = fetch_id_map('prefix', 'prefixes', list(prefixes))

    debug_print("Made token maps")

    # Prepare mapping inserts and bulk insert them
    word_url_pairs = [(word_map[w], url_id) for w in words if w in word_map]
    bigram_url_pairs = [(bigram_map[b], url_id) for b in bigrams if b in bigram_map]
    trigram_url_pairs = [(trigram_map[t], url_id) for t in trigrams if t in trigram_map]
    prefix_url_pairs = [(prefix_map[p], url_id) for p in prefixes if p in prefix_map]

    debug_print("Made insert stementes for <token>_url tables")

    if word_url_pairs:
        extras.execute_values(cur,
            "INSERT INTO word_urls (word_id, url_id) VALUES %s;",
            word_url_pairs)

    if bigram_url_pairs:
        extras.execute_values(cur,
            "INSERT INTO bigram_urls (bigram_id, url_id) VALUES %s;",
            bigram_url_pairs)

    if trigram_url_pairs:
        extras.execute_values(cur,
            "INSERT INTO trigram_urls (trigram_id, url_id) VALUES %s;",
            trigram_url_pairs)

    if prefix_url_pairs:
        extras.execute_values(cur,
            "INSERT INTO prefix_urls (prefix_id, url_id) VALUES %s;",
            prefix_url_pairs)

    debug_print("Executed insert statements, stored url.id_token.id pairs")

    conn.commit()
    cur.close()
    conn.close()

    debug_print("Committed and closed sql connection")

    return links


def delete_url(url):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT id FROM urls WHERE url = %s", (url,))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return

    url_id = row[0]

    cur.execute("DELETE FROM word_urls WHERE url_id = %s", (url_id,))
    cur.execute("DELETE FROM bigram_urls WHERE url_id = %s", (url_id,))
    cur.execute("DELETE FROM trigram_urls WHERE url_id = %s", (url_id,))
    cur.execute("DELETE FROM prefix_urls WHERE url_id = %s", (url_id,))

    cur.execute("DELETE FROM urls WHERE id = %s", (url_id,))

    # cleanup orphaned entries
    cur.execute("DELETE FROM words WHERE id NOT IN (SELECT DISTINCT word_id FROM word_urls)")
    cur.execute("DELETE FROM bigrams WHERE id NOT IN (SELECT DISTINCT bigram_id FROM bigram_urls)")
    cur.execute("DELETE FROM trigrams WHERE id NOT IN (SELECT DISTINCT trigram_id FROM trigram_urls)")
    cur.execute("DELETE FROM prefixes WHERE id NOT IN (SELECT DISTINCT prefix_id FROM prefix_urls)")

    conn.commit()
    cur.close()
    conn.close()



# Queue and logging helpers (Postgres-backed)
def enqueue_url(url):
    """Insert a URL into the queue if it's not already present."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO url_queue (url) VALUES (%s) ON CONFLICT (url) DO NOTHING;", (url,))
    conn.commit()
    cur.close()
    conn.close()


def queue_size():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM url_queue;")
    count = cur.fetchone()[0]
    cur.close()
    conn.close()
    return count

def get_next_urls(num_urls):
    # Returns a list of the next urls in the queue, deletes them from the db queue

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT id, url FROM url_queue ORDER BY id LIMIT %s;", (num_urls,))
    rows = cur.fetchall()

    ids = [row[0] for row in rows]
    urls = [row[1] for row in rows]

    # Nothing in the queue
    if not rows:
        cur.close()
        conn.close()
        return None

    cur.execute("DELETE FROM url_queue WHERE id = ANY(%s);", (ids,))
    conn.commit()
    cur.close()
    conn.close()

    return urls

def pop_next_url():
    """
    Pop the next URL from the queue and return it.
    If the queue's first two URLs are from the same base domain, rotate the first URL to the end and return None.
    If the queue is empty, return None.
    """
    conn = get_conn()
    cur = conn.cursor()

    def get_next_urls(conn, cur):
        cur.execute("SELECT id, url FROM url_queue ORDER BY id LIMIT 2;")
        rows = cur.fetchall()
        if not rows:
            cur.close()
            conn.close()
            return None

        if len(rows) == 1:
            row = rows[0]
            cur.execute("DELETE FROM url_queue WHERE id = %s;", (row[0],))
            conn.commit()
            cur.close()
            conn.close()
            return row[1]

        # Two rows: check domain
        first_id, first_url = rows[0]
        
        second_id, second_url = rows[1]

        return(first_id, first_url, second_id, second_url)
       
    def check_for_bad_domain(url):
        try:
            second_domain = get_base_domain(second_url)
            return False
        except:
            delete_from_queue(second_url)
            return True

    data = get_next_urls(conn, cur)
    first_id = data[0]
    first_url = data[1]
    second_id = data[2]
    second_url = data[3]

    while check_for_bad_domain(second_url):
        # check_for_bad_domain() returns False for a good domain
        data = get_next_urls(conn, cur)
        first_id = data[0]
        first_url = data[1]
        second_url = data[3]

    if get_base_domain(first_url) == get_base_domain(second_url):
        # rotate: remove first then reinsert it so it goes to the end
        cur.execute("DELETE FROM url_queue WHERE id = %s;", (first_id,))
        cur.execute("INSERT INTO url_queue (url) VALUES (%s) ON CONFLICT (url) DO NOTHING;", (first_url,))
        conn.commit()
        cur.close()
        conn.close()
        return None

    # otherwise pop the first
    cur.execute("DELETE FROM url_queue WHERE id = %s;", (first_id,))
    conn.commit()
    cur.close()
    conn.close()
    return first_url


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

"""
def enqueue_urls(urls):
    conn = get_conn()
    cur = conn.cursor()
    for u in urls:
        cur.execute("INSERT INTO url_queue (url) VALUES (%s) ON CONFLICT (url) DO NOTHING;", (u,))
    conn.commit()
    cur.close()
    conn.close()
"""
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
    """Return a list of URLs from `urls` that are not present in the
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


def delete_from_queue(url):
    """Delete `url` from the `url_queue` table.

    Returns the number of rows deleted (0 if not present, 1 if removed).
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM url_queue WHERE url = %s;", (url,))
    deleted = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return deleted

# Ensure queue and logs tables are created when creating DB
def _extend_create_database_tables(cur):
    cur.execute("""
    CREATE TABLE IF NOT EXISTS url_queue (
        id SERIAL PRIMARY KEY,
        url VARCHAR(2048) UNIQUE NOT NULL,
        enqueued_at TIMESTAMP DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS logs (
        id SERIAL PRIMARY KEY,
        ts TIMESTAMP DEFAULT now(),
        ip VARCHAR(64),
        message TEXT
    );
    """)


# ------------ Redis functions

def mark_domain(domain, redis_client):
    # Marks domain as scraped

    key = f"domain:{domain}"
    # SET key with NX (only if not exists) and EX (expire)
    redis_client.set(key, 1, nx=True, ex=10)

def domain_free_for_scraping(domain, redis_client):
    # Checks if domain has been scrpaed in the last 10 seconds
    # True for not in db and can be scraped, False for not

    key = f"domain:{domain}"
    return not redis_client.exists(key)