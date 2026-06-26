# Supporting Servers

Scrapers need three additional servers running on the head node besides the database

## Blocklist

Flask server `serve_blocklist.py` serves up a file `blocklist.db` for scrapers to download and cache. 

The script `update_local_blocklist.py` downloads Steven Black's host file and converts it to a sqlite3 database for use by scrapers. This should be run every 10 minutes or so

## Proxy

The way we have our scrapers set up internally, a proxy is run on the head node and another copy on the cloud proxy server. It's a vibe coded go file `proxy.go`

I'll put the commands to run the proxies here later

## Site Text Storage

In addition to storing the tokenized text in the search index, we also store a copy of the text of all indexed websites for research purposes.

The file to run is `web_storage_server.py`