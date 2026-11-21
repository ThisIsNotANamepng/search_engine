"""
The scraper file, run to begin scraping

Args:
    -u, --url
        The scraper will scrape only the given url

    -f, --url-file
        The scraper will only scrape the urls in a given filepath. The file will be structured as a text file with a url on each line

    -n, --no-db
        The scraper will not store the results of the scrape in the database. If using this option, --url or --file must also be supplied (or the scraper can't contact the database for the scraping queue)

Environment variables:
    DATABASE_URL
        A postgressql styled string containing the user, db name, ip address, and password for the index postgres database
    
    DEBUG
        For debugging, prints as it goes through the steps of scraping

        Possible values:
            1 - Prints for each print statement
            2 - Prints for each print statement and includes timing
            3 - Prints for each print statement and includes timing and saves data to timing.csv
"""

import utils
import time
from urllib.parse import urlparse
import os
import redis
import psycopg2
import requests
import argparse
import atexit
import signal
import sys

LOCAL_QUEUE_LENGTH = 100 # number of URLs to hold locally for scraping

WEB_TEXT_STORAGE_SERVER_ADDRESS = os.getenv("WEB_TEXT_STORAGE_SERVER_ADDRESS")

parser = argparse.ArgumentParser()

parser.add_argument("-u", "--url", help="Single URL to scrape")
parser.add_argument("-f", "--file", help="URL file to scrape")

## TODO: Make --nodb also not use the blocklist and web text storage. Or add an option to just test some combination of those dbs
parser.add_argument("-ndb", "--nodb", action='store_true', help="Don't use redis or postgres db")

args = parser.parse_args()

if args.nodb and not (args.url or args.file):
    raise ("--nodb option must be used with --url or --file flags")
elif args.url and args.file:
    raise ("Cannot use both --file and --url flags at the same time")

# Communicates the nodb flag to utils.py for all of the log() functions because I'm lazy
utils.NODB = args.nodb

if not args.nodb: utils.create_database()


utils.log("Started scraping")

if not args.nodb:
    redis_address = os.getenv("DATABASE_URL")
    redis_address = redis_address[redis_address.index("@")+1:] # Get the IP of the DB server, the redis server will be on the same machine
    redis_address = redis_address[0:redis_address.index(":")]

    redis_password = os.getenv("DATABASE_URL")[::-1]
    redis_password = redis_password[redis_password.index("@")+1:]
    redis_password = redis_password[0:redis_password.index(":")][::-1]

    redis_client = redis.Redis(
        host=redis_address,
        port=6379,
        db=0,
        password=redis_password,
        decode_responses=True  # makes strings instead of bytes
    )


# It iterates through the local_queue, scraping when the url is free to be scraped
# If a url is in the local_queue and cannot be scraped, it is added to queue_return_to_db, which is handed back to the db queue when local_queue is spent (empty)
local_queue = []
queue_return_to_db = []
claimed = []  # urls this scraper claimed with get_next_urls, still marked claimed in the db queue
retry_later = []  # claimed urls left claimed on purpose (rate limited, or one of our own servers was down): the claim timing out is their retry delay
url = ""
prev_base_domain = ""
page_not_in_english = False
first_run = True

def finish_claims(release):
    # End our claims: urls in release go back to the db queue, the rest of what we claimed is done and leaves it
    global claimed, retry_later
    if args.nodb or args.url or args.file:
        return  # these urls didn't come from the db queue, so there are no claims (and the rows may be another scraper's)
    utils.finish_claims(done=set(claimed) - set(release) - set(retry_later), release=release)
    claimed, retry_later = [], []

def send_page_text(url, page_data):
    # The text archive is a copy for research; if it's down, keep crawling
    try:
        utils.send_page_text(WEB_TEXT_STORAGE_SERVER_ADDRESS, url, text=page_data[0], title=page_data[2])
    except Exception as e:
        utils.log(f"Error Web text storage failed {e!r} {url}")

# On any exit, hand the urls we still hold back to the db queue (if we're killed instead, the claims time out)
def on_exit():
    utils.close_all_connections()  # roll back anything SIGTERM cut off, otherwise its locks block handing back the same urls
    release = local_queue + queue_return_to_db + ([url] if url else [])
    try:
        if url and not (args.nodb or args.url or args.file):
            utils.enqueue_urls([url])  # the current url may be a same-host redirect target, which isn't in the db queue
        finish_claims(release)
    except Exception as e:
        utils.failure_print(f"Could not hand back {len(release)} urls: {e}")

atexit.register(on_exit)
# Turn the node agent's SIGTERM into a normal exit so atexit runs (python is PID 1 in the container, which ignores SIGTERM without a handler)
# While claiming urls from the db the exit is deferred: they're claimed in the db before they land in local_queue, so exiting in between would leave them stuck until the claim times out
claiming_urls = False
stop_requested = False

def handle_sigterm(signum, frame):
    global stop_requested
    stop_requested = True
    if not claiming_urls:
        sys.exit(0)

signal.signal(signal.SIGTERM, handle_sigterm)

while True:
    url = ""
    try:

        # ------------- Creates a local queue of possible urls to scrape

        # Iterates through the queue until it finds a domain which hasn't been scraped in the last 10 seconds (with the redis db)
        # Holds a local queue of 30 urls so it doesn't need to interact with the db as much

        if len(local_queue) == 0:
            utils.info_print("Reloading local queue")

            # If a URL or file was supplied, scrape only those (regardless of whether the db is enabled), then quit once spent
            if args.url or args.file:
                if first_run:
                    if args.url:
                        local_queue = [args.url]
                    else:
                        with open(args.file, "r") as f:
                            local_queue = f.read().splitlines()
                else:
                    break

            else:
                finish_claims(queue_return_to_db)
                queue_return_to_db = []

                claiming_urls = True
                try:
                    local_queue = utils.get_next_urls(LOCAL_QUEUE_LENGTH)
                    claimed = list(local_queue)
                finally:
                    claiming_urls = False
                if stop_requested:
                    sys.exit(0)
                if not local_queue:
                    utils.info_print("Queue is empty, waiting")
                    time.sleep(5)

            first_run = False

        # ------------- Finds the next good url to scrape

        # This loop is for going through the local_queue that we downloaded from the queue in the database until we find a url that is not of the same domain as the site scraped in the last iteration of this scraper and not in the redis db (thus not scraped in the past 10 seconds)
        # TODO: This doesn't need to be two separate if/else statements, make them instead if ___ and ___ and ____ in one statement
        # TODO: Add checking if the url is in the local cached sqlite3 db here too, it currently happens later but that's dumb
        url = ""
        for i in list(local_queue):
            base_domain = urlparse(i).hostname

            if base_domain != prev_base_domain:
                # Case 1: Base domain of the current url is NOT the same as the one scraped in the previous iteration. Thus, we check if the domain is in the redis db

                # For debugging, we're not using any databases
                if args.nodb:
                    url = i
                    prev_base_domain = base_domain
                    local_queue.remove(i)
                    break

                # Checks the redis db
                next_url_is_free = utils.claim_domain(base_domain, redis_client)

                if next_url_is_free:
                    # Case 3: Domain is NOT the same as the previously scraped domain, also NOT in the redis db. Thus, we break this loop and scrape
                    url = i
                    prev_base_domain = base_domain
                    local_queue.remove(i)  # Need to remove link because we break the loop so it starts at the same url when it restarts the loop
                    break
                
                else:
                    # Case 4: Domain NOT the same as the previously scraped domain but IS in the redis db. Thus, we don't scrape 
                    local_queue.remove(i)
                    queue_return_to_db.append(i)

                prev_base_domain = base_domain
            else:
                # Case 2: Base domain IS the same as the previous base domain. Thus, we don't scrape
                local_queue.remove(i)
                #print("Adding to return_to_queue", i)

                queue_return_to_db.append(i)
    
        if url == "": continue # If the local_queue is 0 and the loop above ends but the url isn't valid (not in redis and not prev_base_domain) the invalid url will be use for the scraping code below, thus we only assign url a value if it passes all checks
    

        if not args.nodb:
            if utils.check_url_in_blocklist(url):
                utils.log(f"Misc URL found in domain blocklist, skipping {url}")
                continue


        # ------------- Scraping

        utils.log(f"Starting scraping {url}")
        utils.debug_print("")
        url_scraping_timer = time.time()
        page_not_in_english = False

        raw_html, final_url, x_robots_tag = utils.get_page_html(url)

        if raw_html == utils.REDIRECTED:
            # Redirect to another host: queue the target so it goes through that host's checks, and stop re-queueing this url
            if not args.nodb:
                utils.mark_url_seen(url)
                utils.enqueue_urls(utils.filter_new_urls(utils.clean_links([final_url])))
            utils.info_print(f"Redirects to another host, queued {final_url} for {url}")
            continue

        if raw_html == utils.RATE_LIMITED:
            if not args.nodb:
                retry_later.append(url)  # stays claimed, so it's retried once the claim times out
                redis_client.set(f"domain:{base_domain}", 1, ex=600)  # 10 minute cooldown
            utils.info_print(f"Rate limited, retrying after {utils.CLAIM_TIMEOUT} with 10 minute domain cooldown {url}")
            continue

        if raw_html is False:
            utils.info_print(f"Fetch failed (blocked/timeout/non-HTML), skipping {url}")
            continue

        # A same-host redirect was followed: index the page under where it actually lives, cleaned like every stored url
        page_url = (utils.clean_links([final_url]) or [url])[0]
        if page_url != url:
            if not args.nodb:
                utils.mark_url_seen(url)
            url = page_url

        page_data = utils.extract_data_from_html(raw_html, final_url, x_robots_tag) #[combined_text, links, title, icon_link, directives], links already empty for nofollow
        # The site's robots meta tags / X-Robots-Tag: noindex = keep it out of the index, noarchive = don't keep a copy of its text
        directives = page_data[4]

        if page_data[0]:
            # Check for english language
            if utils.determine_language(page_data[0]) != 'en':
                # TODO: Add something here to the logs logging which pages are in what language so we can measure how much of the web is in what language
                utils.log(f"Language Not in English {url}")
                utils.info_print("Language not in English, skipping")

                # Not indexed, only recorded as seen so other pages linking here don't queue it again
                utils.mark_url_seen(url)

                #return [links, False]
                page_not_in_english = True
                utils.debug_print("Detected language")

                if "noarchive" not in directives:
                    utils.debug_print("Sending to web text storage server")
                    send_page_text(url, page_data)


            else:

                if "noindex" in directives:
                    utils.info_print(f"Page asks not to be indexed (noindex), not storing {url}")
                    if not args.nodb:
                        utils.mark_url_seen(url)  # so other pages linking here don't queue it again
                else:
                    utils.store(page_data, url)
                if "noarchive" not in directives:
                    utils.debug_print("Sending to web text storage server")
                    send_page_text(url, page_data)

        else:
            utils.info_print(f"No text found, keeping links only {url}")

        # Clean first: stored and queued urls are in cleaned form (no ?query, #fragment or trailing /), so raw links wouldn't match them
        cleaned = utils.clean_links(page_data[1])

        # Batch add url references after cleaning
        if cleaned and not args.nodb:
            current_domain = utils.get_base_domain(url)

            external_links = [link for link in cleaned if utils.get_base_domain(link) != current_domain]

            if external_links:

                # TODO: Move this to utils
                conn = utils.get_conn()
                cur = conn.cursor()

                cur.execute("""
                    UPDATE urls
                    SET reference_count = reference_count + 1
                    WHERE url = ANY(%s);
                """, (external_links,))

                conn.commit()
                cur.close()
                conn.close()

        if not args.nodb:
            # filter_new_urls checks both the queue and stored urls in one go
            links_to_add_to_queue = utils.filter_new_urls(cleaned)
            utils.enqueue_urls(links_to_add_to_queue)

            # Add url to the cooldown redis db
            utils.mark_domain(base_domain, redis_client)

        utils.log(f"Scraped {url}")


        if page_not_in_english:
            utils.info_print(f"Stored links for {url}, total time taken: {str(time.time()-url_scraping_timer)}")
            page_not_in_english = False
        else:
            utils.info_print(f"Scraped {url}, total time taken: {str(time.time()-url_scraping_timer)}")

        url = ""  # done with it; nothing to requeue on exit

    except Exception as e:
        # One bad page must not kill the scraper (and lose its local queue); log it, skip that url (not requeued, so a page that always fails can't loop forever), keep going
        utils.failure_print(f"Error scraping {url}: {e!r}")
        utils.log(f"Error Unhandled exception {e!r} {url}")
        # Page errors are caught inside utils, so these mean our own postgres, blocklist server or proxy is down: not the page's fault, retry it later
        if url and isinstance(e, (psycopg2.OperationalError, psycopg2.InterfaceError, requests.RequestException)):
            retry_later.append(url)
        time.sleep(1)  # don't spin if a dependency (db, redis) is down

utils.log("Finished scraping")
