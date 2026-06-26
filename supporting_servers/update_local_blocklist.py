"""
This script downloads the blocklist of unpalatable domains (fake news/gambling scams/adware/malware sites) and formats it into a sqlite database for the scrapers to download and reference in their scraping

The script downloads Steve Black's host file, formats it into a sqlite db, and serves


"""

import requests, sqlite3, time

HOSTS_URL = "https://raw.githubusercontent.com/StevenBlack/hosts/master/alternates/fakenews/hosts"

response = requests.get(HOSTS_URL)

with open("hosts.txt", "wb") as f:
    f.write(response.content)

with open ("cleaned_hosts.txt", "w") as cleaned:
    cleaned.write("")

with open("hosts.txt", "r") as hosts:
    with open ("cleaned_hosts.txt", "a") as cleaned:
        for i in hosts.readlines():

            if i[0] == "#":
                continue

            if len(i) > 4:
                cleaned.write(i.split()[1]+"\n")

with open("blocklist.db", "w") as f:
    f.write("")

conn = sqlite3.connect("blocklist.db")
cursor = conn.cursor()
cursor.execute("CREATE TABLE blocklist (domain TEXT);")
cursor.execute("CREATE TABLE last_updated (last_updated TEXT);")
with open("cleaned_hosts.txt", "r") as f:
    for i in f.readlines():
        domain = i.strip()
        if not domain:
            continue
        cursor.execute("INSERT INTO blocklist VALUES (?)", (domain,))
cursor.execute("INSERT INTO last_updated VALUES (?)", (time.time(),))
conn.commit()
conn.close()
