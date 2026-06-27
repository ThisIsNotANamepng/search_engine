# Indexing Methodology

The internet is fucking big.

The internet is also really random. It is a network of unstructured data. Therefore, the process of turning it into structured data (IE a search index) requires a lot of format checking. Before you start checking the text for things to store in your index, you need to check if the data you are checking is actually text and not an .mp3 file

Some websites have too much data to handle

Some don't want you to crawl them

Some are written in languages that the index aren't prepared for

And you need to check for all of it

## Checklist

Here's a checklist of all of the things we do when scraping

- [x] Check for primarily English language
- [x] Network Timeout - only load a url from a domain every 10 seconds (10 second request delay per domain)
- [x] Check for html only (no files)
- [ ] Scraper responsiveness - if the scraper hasn't done anything in the last 10 seconds, reboot it, do health checks, and report the last url that it tried to scrape
- [x] Check the robots.txt to see whether we are allowed to scrape
    - [ ] Plus Crawl-Delay
- [ ] Log http errors
    - [ ] Stop scraping domain and log in the dashboard if its a 403, 429 responses
    - [ ] Log any other http error
- [ ] Check for empty pages (no words)

## Code Process

A diagram of how the process of scraping works through the various functions

Run the main loop in scrape.py

1. Main loop gets next urls to scrape from queue using `utils.get_next_urls`, returns urls not to scrape to queue with `utils.enqueue_urls`
2. Main loop checks url with `utils.domain_free_for_scraping`
3. Main loop checks url with `utils.check_url_in_blocklist`
4. Main loop extracts links from url with `utils.store`
5. `utils.store` calls `utils.get_main_text` which gets the html of the given page
6. `utils.get_main_text` checks of the scraper is allowed by the domain's `robots.txt`
7. `utils.get_main_text` checks for 401/403 http codes, and 423 rate limited code
8. `utils.get_main_text` checks if the response is html (not a pdf, etc)
9. `utils.get_main_text` passes html to `utils.extract_data_from_html` which converts to bs4 soup
10. `utils.extract_data_from_html` extracts the page text, links, favicon link, and page title and retuns them
11. `utils.get_main_text` returns the value of `utils.extract_data_from_html` back to `utils.store`
12. `utils.store` checks for English in the text
13. `utils.store` tokenizes, cleans, and stores in database
14. Main loop stores links from step 4 in the queue
15. Restart

It seems that scrape.py only deals with links, its at the link layer. utils.py deals with links and html and the db