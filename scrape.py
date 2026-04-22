"""
The scraper file, run to begin scraping
"""

import scraper
import time
from urllib.parse import urlparse
import os
import redis
from typing import List, Tuple

TIMEOUT_TIME = 10  # seconds to wait for fetching a page before skipping
LOCAL_QUEUE_LENGTH = 60 # number of URLs to hold locally for scraping

scraper.create_database()

total_scraped = 0

# This is legacy from running on a single machine, we can probably delete this now that we have a shared queue
# Seed URLs into DB-backed queue (skip those already in DB)

with open("seed_urls.csv", "r") as f:
    for line in f:
        url = line.strip()
        if url and not scraper.exists(url, 'url'):
            scraper.enqueue_url(url)


scraper.log("Started scraping")

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

"""
# Redis db testing
start=time.time()
print(scraper.domain_free_for_scraping("example.com", redis_client))
scraper.mark_domain("example.com", redis_client)
print(scraper.domain_free_for_scraping("example.com", redis_client))

print(time.time()-start)
"""

timed = time.time()
start = timed

# It iterates through the local_queue, scrpaing when the url is free to be scraped
# If a url is in the local_queue and cannot be scraped, it is added to queue_return_to_db, which is sent back to be added to the db queue when local_queue is spent (empty)
local_queue = []
queue_return_to_db = []
prev_base_domain = ""
page_not_in_english = False

while True:
    
    # Iterates through the queue until it finds a domain which hasn't been scraped in the last 10 seconds (with the redis db)
    # Holds a local queue of 30 urls so it doesn't need to interact with the db as much

    if len(local_queue) == 0:
        scraper.info_print("Reloading local queue")
        #print("Returning to queue:", queue_return_to_db)
        scraper.enqueue_urls(queue_return_to_db)
        #print("Queue to return", queue_return_to_db)
        #print("Enqueued")
        local_queue = scraper.get_next_urls(LOCAL_QUEUE_LENGTH)
        #print("Adding to lcal queue:")
        #print(local_queue)
        #print(local_queue)

    for i in local_queue:
        #print(i, len(local_queue))
        #print(local_queue)
        base_domain = urlparse(i).hostname
        #print(f"Domain {base_domain}, Previous Domain {prev_base_domain}")
        url = ""
        #print(1)

        if base_domain != prev_base_domain:
            # Case 1: base domain of the current url is NOT the same as the one scraped in the previous iteration

            next_url_is_free = scraper.domain_free_for_scraping(base_domain, redis_client)
            #print(2)

            if next_url_is_free:
                #print(f"Not in redis: {base_domain}")
                # Case 3: domain is not the sme as the previous, also not in the redis db
                url = i
                #print("Scraping", url)
                prev_base_domain = base_domain
                local_queue.remove(i)  # Need to remove link because we break the loop so it starts at the same url when it restarts the loop
                #print(3)
                break
                
            else:
                local_queue.remove(i)
                #print("Adding to return_to_queue", i)
                queue_return_to_db.append(i)
                #print(4)

            prev_base_domain = base_domain
        else:
            # Case 2: Base domain is the same as the previous base domain
            local_queue.remove(i)
            #print("Adding to return_to_queue", i)

            queue_return_to_db.append(i)
        
    """
    # Legacy from when we asked the db for the next url one at a time
    url = scraper.pop_next_url()


    if url is None:
        # either rotated due to domain-balancing or queue empty
        if scraper.queue_size() == 0:
            break
        else:
            continue
    """
    if url == "": continue  # If the local_queue is 0 and the loop above ends but the url isn't valid (not in redis and not prev_base_domain) the invalid url will be use for the scraping code below, thus we only assign url a value if it passes all checks

    scraper.log(f"Starting scraping {url}")
    scraper.debug_print("")
    big_start = time.time()

    if scraper.is_domain_blocked(scraper.get_base_domain(url)):
        scraper.info_print(f"Domain is blocked, skipping {url}")
        continue

    #try: # I took this try statement out, see note at the `except` ending
    # enforce a network/read timeout for page fetch and parsing
    ## TODO: I don't think this timeout works, we need to fix it
    links_to_scrape = scraper.store(url, timeout=TIMEOUT_TIME)

    if links_to_scrape == scraper.RATE_LIMITED:
        scraper.enqueue_url(url)
        redis_client.set(f"domain:{base_domain}", 1, ex=300)
        scraper.info_print(f"Rate limited, re-queued {url} with 5-min cooldown")
        continue
    #print("first links_to_scrape", links_to_scrape)


    ## TODO: Right now if a page isn't in English we still store the links in that page (they probably are unlikely to also be in English) so we need to talk abaout whether we still want to queue those links
    if links_to_scrape != None and len(links_to_scrape) > 1 and not links_to_scrape[1]:
        # links_to_scrape is a list of two [links, False] if the page wasn't in English, we set the variable back to the links and make a flag saying that the page wasn't in English, usung that flag in the final print at the end of the loop
        links_to_scrape = links_to_scrape[0]
        page_not_in_english = True

    total_links=0

    links_to_add_to_queue = []
    # Clean, deduplicate and filter links in bulk for performance
    #print("links_to_scrape:", links_to_scrape)
    #raw_links = [i for i in links_to_scrape if "mailto:" not in i]
    #print("raw_links", raw_links)


    seen = set()
    cleaned = []

    #print("HERE raw_links:", raw_links)

    """
    for link in raw_links:
        print("link:", link)
        # Get rid of ?post=data and #section data
        total_links += 1
        clean_link = link.split('?', 1)[0]
        clean_link = link.split('#', 1)[0]
        if clean_link not in seen:
            seen.add(clean_link)
            cleaned.append(clean_link)
    """

    # Raw_links should be cleaned by the cleaning function in scraper.py, so I'm taking the cleaning stuff out here
    cleaned = links_to_scrape

    #print("CLEANED", cleaned)
    
    # Batch add url references after cleaning
    if cleaned and len(cleaned) > 1:
        print(len(cleaned))
        current_domain = scraper.get_base_domain(url)

        if len(cleaned) > 1:
            external_links = [link for link in cleaned if scraper.get_base_domain(link) != current_domain]
        elif scraper.get_base_domain(cleaned[0]) != current_domain:
            external_links = cleaned[0]

        print(external_links)

        if external_links:
            conn = scraper.get_conn()
            cur = conn.cursor()

            cur.execute("""
                UPDATE urls
                SET reference_count = reference_count + 1
                WHERE url = ANY(%s);
            """, (external_links,))

            conn.commit()
            cur.close()
            conn.close()

    #print("cleaned:", cleaned)


    # filter_new_urls checks both the queue and stored urls in one go
    links_to_add_to_queue = scraper.filter_new_urls(cleaned)

    #print("Adding links-to_add_to_queue:", links_to_add_to_queue)
    scraper.enqueue_urls(links_to_add_to_queue)

    scraper.log(f"Scraped {url}")
    total_scraped += 1

    # With this in, I can't find errors so I'm taking out the try: phrase. This will mean that scrapers can crash and stop scraping, but we can set up K3 to restart them and report the errors
    #except Exception as e:
    #    scraper.log(f"Error scraping {url}: {e}")
    #    scraper.failure_print(e)

    # Add url to the cooldown redis db
    scraper.mark_domain(base_domain, redis_client)


    #if total_scraped % 10 == 0:
    #    print(f"Scraped {total_scraped} pages. {scraper.queue_size()} URLs left in queue")
    #print(f"Scraped {total_scraped} pages. Total time to scrape:", time.time()-start)

    if page_not_in_english:
        scraper.info_print(f"Stored links for {url}, total time taken: {str(time.time()-big_start)}")
        page_not_in_english = False
    else:
        scraper.info_print(f"Scraped {url}, total time taken: {str(time.time()-big_start)}")

    start=time.time()

scraper.log("Finished scraping")
