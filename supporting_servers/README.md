# Supporting Servers

Scrapers need three additional servers running on the head node besides the database

## Blocklist

Flask server `serve_blocklist.py` serves up a file `blocklist.db` for scrapers to download and cache. 

The script `update_local_blocklist.py` downloads Steven Black's host file and the postgres `blocked_domains` table (opt-outs) into one sqlite3 database for use by scrapers, so they never ask postgres whether a domain is blocked. It needs `DATABASE_URL` set. This should be run every 10 minutes or so; a new opt-out reaches scrapers within about an hour plus that interval

## Proxy

The way we have our scrapers set up internally, a proxy is run on the head node and another copy on the cloud proxy server. It's a vibe coded go file `proxy.go`. How it works, what happens when it's down, and its settings: [docs/docs/proxy.md](../docs/docs/proxy.md)

To set up the cloud proxy on a new VPS, copy `deploy/vps/install_proxy.sh` and `proxy.go` to it and run:

```bash
sudo PROXY_PASSWORD=<PROXY_PASSWORD from db01:/etc/stultus/proxy.env> ./install_proxy.sh <vps_ip_or_name> <head_public_ip>
```

Then point the head's proxy at it, on db01 from the repo (the script prints the exact commands, including copying its certificate):

```bash
sudo ./deploy/db01/connect_vps_proxy.sh <vps_ip_or_name> /tmp/vps-proxy.crt
```

## Site Text Storage

In addition to storing the tokenized text in the search index, we also store a copy of the text of all indexed websites for research purposes.

The file to run is `web_storage_server.py`

## Batcher

Unlike the others, `batcher.py` runs on each crawler box, not the head node: the node agent starts it from the crawler image when `RUN_BATCHER=1` is in `/etc/stultus/crawler.env`. The box's scrapers send it their postgres writes (pages, logs, seen and queued urls, reference counts) on `127.0.0.1:8004`, and every 5 seconds it writes all of them to postgres together instead of every scraper sending its own small transactions. See the top of `batcher.py` for how it handles postgres being down
