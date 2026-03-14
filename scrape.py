"""
The scraper file, run to begin scraping
"""

import asyncio
import scraper
import time
from urllib.parse import urlparse
import os
import redis
from typing import List, Tuple, Optional

TIMEOUT_TIME = 10  # seconds to wait for fetching a page before skipping
LOCAL_QUEUE_LENGTH = 60  # number of URLs to hold locally for scraping
MAX_CONCURRENT_SCRAPERS = 1  # Async concurrency bound


def parse_redis_credentials() -> Tuple[str, str]:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL not set")

    redis_address = database_url[database_url.index("@") + 1:]
    redis_address = redis_address[:redis_address.index(":")]

    redis_password = database_url[::-1]
    redis_password = redis_password[redis_password.index("@") + 1:]
    redis_password = redis_password[:redis_password.index(":")][::-1]
    return redis_address, redis_password


def build_redis_client() -> redis.Redis:
    host, password = parse_redis_credentials()
    return redis.Redis(
        host=host,
        port=6379,
        db=0,
        password=password,
        decode_responses=True,
    )


async def load_seeds():
    # preserve existing behavior: skip seeds already in urls table
    with open("seed_urls.csv", "r") as f:
        for line in f:
            url = line.strip()
            if not url:
                continue
            if not await asyncio.to_thread(scraper.exists, url, "url"):
                await asyncio.to_thread(scraper.enqueue_url, url)


async def async_store_url(url: str) -> Optional[List[str]]:
    # Run the heavy blocking store logic in a threadpool
    return await asyncio.to_thread(scraper.store, url, timeout=TIMEOUT_TIME)


async def async_enqueue_urls(urls: List[str]):
    if not urls:
        return
    await asyncio.to_thread(scraper.enqueue_urls, urls)


async def async_filter_new_urls(urls: List[str]) -> List[str]:
    return await asyncio.to_thread(scraper.filter_new_urls, urls)


async def async_update_reference_counts(cleaned: List[str]):
    if not cleaned or len(cleaned) == 0:
        return
    conn = scraper.get_conn()
    cur = conn.cursor()
    cur.execute(
        """
            UPDATE urls
            SET reference_count = reference_count + 1
            WHERE url = ANY(%s);
        """,
        (cleaned,),
    )
    conn.commit()
    cur.close()
    conn.close()


async def async_get_next_urls(n: int) -> Optional[List[str]]:
    return await asyncio.to_thread(scraper.get_next_urls, n)


async def async_main():
    scraper.create_database()
    await load_seeds()
    scraper.log("Started scraping")

    redis_client = build_redis_client()

    total_scraped = 0
    local_queue: List[str] = []
    queue_return_to_db: List[str] = []
    prev_base_domain = ""
    page_not_in_english = False
    timed = time.time()
    start = timed

    while True:
        if len(local_queue) == 0:
            scraper.info_print("Reloading local queue")
            if queue_return_to_db:
                await async_enqueue_urls(queue_return_to_db)
                queue_return_to_db.clear()

            next_urls = await async_get_next_urls(LOCAL_QUEUE_LENGTH)
            if not next_urls:
                # If nothing in queue, sleep and retry.
                await asyncio.sleep(1)
                continue
            local_queue = next_urls

        url = ""
        for candidate in list(local_queue):
            base_domain = urlparse(candidate).hostname
            if not base_domain:
                local_queue.remove(candidate)
                continue

            if base_domain != prev_base_domain:
                next_url_is_free = scraper.domain_free_for_scraping(base_domain, redis_client)
                if next_url_is_free:
                    url = candidate
                    prev_base_domain = base_domain
                    local_queue.remove(candidate)
                    break
                else:
                    local_queue.remove(candidate)
                    queue_return_to_db.append(candidate)
                prev_base_domain = base_domain
            else:
                local_queue.remove(candidate)
                queue_return_to_db.append(candidate)

        if url == "":
            await asyncio.sleep(0.2)
            continue

        scraper.log(f"Starting scraping {url}")
        scraper.debug_print("")
        big_start = time.time()

        links_to_scrape = await async_store_url(url)

        cleaned = []
        if isinstance(links_to_scrape, list) and len(links_to_scrape) == 2 and links_to_scrape[1] is False:
            # store returned [links, False] for non-English pages
            cleaned = links_to_scrape[0] if isinstance(links_to_scrape[0], list) else []
            page_not_in_english = True
        elif isinstance(links_to_scrape, list):
            cleaned = links_to_scrape

        if cleaned:
            await async_update_reference_counts(cleaned)
            links_to_add_to_queue = await async_filter_new_urls(cleaned)
        else:
            links_to_add_to_queue = []

        if links_to_add_to_queue:
            await async_enqueue_urls(links_to_add_to_queue)

        scraper.log(f"Scraped {url}")
        total_scraped += 1

        base_domain = urlparse(url).hostname or ""
        scraper.mark_domain(base_domain, redis_client)

        if page_not_in_english:
            scraper.info_print(f"Stored links for {url}, total time taken: {str(time.time() - big_start)}")
            page_not_in_english = False
        else:
            scraper.info_print(f"Scraped {url}, total time taken: {str(time.time() - big_start)}")

        start = time.time()

    scraper.log("Finished scraping")


if __name__ == "__main__":
    asyncio.run(async_main())
