# Scraping Methodology

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
