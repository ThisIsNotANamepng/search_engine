"""
PostgreSQL-backed scraper module (ported from sqlite3_scraper.py).

Usage:
 - Set `DATABASE_URL` environment variable (or PGHOST/PGUSER/PGPASSWORD/PGDATABASE/PGPORT).
 - Install dependency: `psycopg2-binary`.

This file mirrors the original SQLite implementation but uses psycopg2/Postgres.
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

def get_conn():
    """Return a new psycopg2 connection using `DATABASE_URL` or PG_* env vars."""
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        return psycopg2.connect(database_url)
    else:
        print("Failed to connect to server")

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
    #print("Starting storing")
    m=time.time()

    content = get_main_text(url, timeout=timeout)

    #print(f"Getting main text: {time.time()-m}")
    m=time.time()

    if content != False:
        # The url contains real text to scrape
        text = content[0]
        links = content[1]
    else:
        # Url is a file format which cannot be scraped
        return

    #print(f"Spliting text: {time.time()-m}")
    m=time.time()


    # Check for english language
    if detect(text) != 'en':
        log(f"Language Not in English {url}")
        return links

    #print(f"Detecting language: {time.time()-m}")
    m=time.time()


    tokens = tokenizer.tokenize_all(text) #TODO: I want to see how expensive it is to check if each token is in the database already, you'd have to search all of the tokens to find if it's in the database

    #print(f"Geeting tokens: {time.time()-m}")
    m=time.time()

    if not text:
        log(f"Error Failed to retrieve page text {url}")
        return links
        

    # tokens: [words, bigrams, trigrams, prefixes]
    words = set(tokens[0]) if tokens and len(tokens) > 0 else set()
    bigrams = set(tokens[1]) if tokens and len(tokens) > 1 else set()
    trigrams = set(tokens[2]) if tokens and len(tokens) > 2 else set()
    prefixes = set(tokens[3]) if tokens and len(tokens) > 3 else set()

    #print(f"Splitting into words, bigrams, trigrams, prefixes: {time.time()-m}")
    m=time.time()


    conn = get_conn()
    cur = conn.cursor()

    #print(f"Getting SQL connection and cursor: {time.time()-m}")
    m=time.time()


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

    # Fetch ids for all tokens in bulk
    def fetch_id_map(column, table, items):
        if not items:
            return {}
        cur.execute(sql.SQL("SELECT id, {col} FROM {tbl} WHERE {col} = ANY(%s);").format(
            col=sql.Identifier(column), tbl=sql.Identifier(table)
        ), (list(items),))
        rows = cur.fetchall()
        return {val: id for (id, val) in rows}
    
    #print(f"Putting tokens into database: {time.time()-m}")
    m=time.time()


    word_map = fetch_id_map('word', 'words', list(words))
    bigram_map = fetch_id_map('bigram', 'bigrams', list(bigrams))
    trigram_map = fetch_id_map('trigram', 'trigrams', list(trigrams))
    prefix_map = fetch_id_map('prefix', 'prefixes', list(prefixes))

    #print(f"Making token maps: {time.time()-m}")
    m=time.time()


    # Prepare mapping inserts and bulk insert them
    word_url_pairs = [(word_map[w], url_id) for w in words if w in word_map]
    bigram_url_pairs = [(bigram_map[b], url_id) for b in bigrams if b in bigram_map]
    trigram_url_pairs = [(trigram_map[t], url_id) for t in trigrams if t in trigram_map]
    prefix_url_pairs = [(prefix_map[p], url_id) for p in prefixes if p in prefix_map]

    #print(f"Making inserts: {time.time()-m}")
    m=time.time()

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

    #print(f"SQL executing token_urls: {time.time()-m}")
    m=time.time()


    conn.commit()
    cur.close()
    conn.close()

    #print(f"Committing and closing sql connection: {time.time()-m}")
    m=time.time()

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