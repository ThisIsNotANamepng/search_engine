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
from typing import List, Tuple
import argparse
import trafilatura

# TODO: TIMEOUT_LENGTH should be passed to the utils.mark_domain() function because it has a hardcoded 10 seconds cooldown which is passed to the redis db when adding a domain
TIMEOUT_TIME = 10  # seconds to wait for fetching a page before skipping
LOCAL_QUEUE_LENGTH = 100 # number of URLs to hold locally for scraping

WEB_TEXT_STORAGE_SERVER_ADDRESS = os.getenv("WEB_TEXT_STORAGE_SERVER_ADDRESS")

parser = argparse.ArgumentParser()

parser.add_argument("-u", "--url", help="Single URL to scrape")
parser.add_argument("-f", "--file", help="URL file to scrape")

## TODO: Make --nodb also not use the blocklist and web text storage. Or add an option to just test some combination of those dbs
parser.add_argument("-ndb", "--nodb", action='store_true', help="Don't use redis or postgres db")

args = parser.parse_args()

if args.nodb and not (args.url or args.file):
    raise("--nodb option must be used with --url or --file flags")
elif args.url and args.file:
    raise("Cannot use both --file and --url flags at the same time")

# Communicates the nodb flag to utils.py for all of the log() functions because I'm lazy
utils.NODB = args.nodb

if not args.nodb: utils.create_database()

total_scraped = 0

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

timed = time.time()
start = timed

# It iterates through the local_queue, scraping when the url is free to be scraped
# If a url is in the local_queue and cannot be scraped, it is added to queue_return_to_db, which is sent back to be added to the db queue when local_queue is spent (empty)
local_queue = []
queue_return_to_db = []
prev_base_domain = ""
page_not_in_english = False
first_run = True

while True:
    
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
            utils.enqueue_urls(queue_return_to_db)

            local_queue = utils.get_next_urls(LOCAL_QUEUE_LENGTH)
        
        first_run = False

    # ------------- Finds the next good url to scrape

    # This loop is for going through the local_queue that we downloaded from the queue in the database until we find a url that is not of the same domain as the site scraped in the last iteration of this scraper and not in the redis db (thus not scraped in the past 10 seconds)
    # TODO: This doesn't need to be two separate if/else statements, make them instead if ___ and ___ and ____ in one statement
    # TODO: Add checking if the url is in the local cached sqlite3 db here too, it currently happens later but that's dumb
    for i in local_queue:
        base_domain = urlparse(i).hostname
        url = ""

        if base_domain != prev_base_domain:
            # Case 1: Base domain of the current url is NOT the same as the one scraped in the previous iteration. Thus, we check if the domain is in the redis db

            # For debugging, we're not using any databases
            if args.nodb:
                url = i
                prev_base_domain = base_domain
                local_queue.remove(i)
                break

            # Checks the redis db
            next_url_is_free = utils.domain_free_for_scraping(base_domain, redis_client)

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
            utils.log("Misc URL found in malicious domain blocklist, exiting")
            continue


    # ------------- Scraping
    
    utils.log(f"Starting scraping {url}")
    utils.debug_print("")
    url_scraping_timer = time.time()

    if not args.nodb:
        ## TODO: is_domain_blocked checks if the url is in the main postgres db table for opted-out domains, domain_free_for_scraping checks if the domain is in the redis db, we should talk about whether we need two of them, or if we should just add the opted-out domains to the redis db
        ## TODO: This should be added to the main check statements above in the loop finding the next good url
        if utils.is_domain_blocked(utils.get_base_domain(url)):
            utils.info_print(f"Domain is blocked, skipping {url}")
            continue

    # timeout is the network timeout for page fetching
    ## TODO: I don't think this timeout works, we need to investigate and potentially fix it

    ## TODO: Right here is where we split this chain of function calls into a series of function calls in this file
    ## Right now functions are called in a chain by utils.store(), I want each required function to be called in this file  in a new line. This makes it clearer for debugging
    #links_to_scrape = utils.store(url, timeout=TIMEOUT_TIME)

    raw_html = utils.get_page_html(url)

    if raw_html == utils.RATE_LIMITED:
        if not args.nodb:
            utils.enqueue_url(url)  # requeue for later
            redis_client.set(f"domain:{base_domain}", 1, ex=600)  # 5-min cooldown
        utils.info_print(f"Rate limited, re-queued with 10 minute cooldown {url}")
        continue

    if raw_html is False:
        utils.info_print(f"Fetch failed (blocked/timeout/non-HTML), skipping {url}")
        continue

    page_data = utils.extract_data_from_html(raw_html, url) #[combined_text, links, title, icon_link]
    page_data[0] = trafilatura.extract(page_data[0])

    # Check for english language
    if utils.determine_language(page_data[0]) != 'en':
        # TODO: Add something here to the logs logging which pages are in what language so we can measure how much of the web is in what language
        utils.log(f"Language Not in English {url}")
        utils.info_print("Language not in English, skipping")

        ## TODO: Change this and the store() function so that we can pass only links and nothing else
        ## TODO: Doesn't this store pages if it's not English? Why is this store command here? It should just pass the links but we haven't implemented that right?
        utils.store(page_data, url)

        #return [links, False]
        page_not_in_english = True
        utils.debug_print("Detected language")

        utils.debug_print("Sending to web text storage server")
        utils.send_page_text(WEB_TEXT_STORAGE_SERVER_ADDRESS, url, text=page_data[0], title=page_data[2])


    else:

        utils.debug_print("Sending to web text storage server")
        utils.store(page_data, url)
        utils.send_page_text(WEB_TEXT_STORAGE_SERVER_ADDRESS, url, text=page_data[0], title=page_data[2])


    total_links = 0

    links_to_add_to_queue = []
    # Clean, deduplicate and filter links in bulk for performance

    seen = set()
    cleaned = []

    # Raw_links should be cleaned by the cleaning function in utils.py, so I'm taking the cleaning stuff out here
    ## TODO: Move the cleaning of the links to a function call here
    cleaned = page_data[1]
    
    # Batch add url references after cleaning
    if cleaned and len(cleaned) > 1:
        print(len(cleaned))
        current_domain = utils.get_base_domain(url)

        if len(cleaned) > 1:
            external_links = [link for link in cleaned if utils.get_base_domain(link) != current_domain]
        elif utils.get_base_domain(cleaned[0]) != current_domain:
            external_links = cleaned[0]

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

        links_to_add_to_queue = utils.clean_links(links_to_add_to_queue)
        utils.enqueue_urls(links_to_add_to_queue)

        # Add url to the cooldown redis db
        utils.mark_domain(base_domain, redis_client)

    utils.log(f"Scraped {url}")
    total_scraped += 1


    if page_not_in_english:
        utils.info_print(f"Stored links for {url}, total time taken: {str(time.time()-url_scraping_timer)}")
        page_not_in_english = False
    else:
        utils.info_print(f"Scraped {url}, total time taken: {str(time.time()-url_scraping_timer)}")

    start=time.time()

utils.log("Finished scraping")
