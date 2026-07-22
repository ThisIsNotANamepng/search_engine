# Stultus Development Docs

Stultus is a really big project, and as we're all college students, we don't have time to learn the entire codebase. Therefore, we must strive to make our docs as useful and as comprehensive as possible

There's a lot to add, and it's going to take a while to reach critical mass of code covered in these docs, but I'm working on it

## Code Structure Overview and Process Workflow

As I've continued to add new features to this project, I keep adding one-off scripts and random things that need to be run to enable a single feature or system, and it's too much to remember and communicate in a few sentences. Thus, here I will give an overview of all of the subsystems within Stultus and point to further documentation for those systems

### Stultus Components

- [Scrapers](/scraping): Web scrapers
- [Postgres Database](/databases): The search index and URL scraping queue
- [Redis Database](/databases): Database which checks whether a given domain has been scraped within the last 10 seconds
- [Web text Storage Server](/databases): Stores the text from all of the pages crawled by the scrapers
- [Frontend](/frontend): The web server for searching, the sysadmin dashboard for monitoring the system, and the index dashboard for stats on what we've crawled
- [Proxy Server](/proxy): The server which proxies traffic from the scrapers to a proxy server
- [Blocklist Server](/databases): An automatically generated blocklist which is checked before crawling a page


``` mermaid
graph TB

    %% Main scraping loop
    A[Init] --> B[Get next URLs]
    B --> RC{Recently scraped?}
    RC -->|no| BL{Blocklisted?}
    BL -->|no| REQ[Fetch page]
    REQ --> TOK{Tokenize}
    TOK --> NU[Extract new URLs]
    NU --> B

    %% Supporting servers
    PG[("Postgres<br/>index + url_queue")]
    RD[("Redis<br/>10s domain cooldown")]
    BLK[(Blocklist server)]
    PX[Proxy server]
    NET((Internet))
    WTS[(Web text storage)]

    B  -.->|pull url_queue| PG
    RC <-.->|check domain| RD
    BL <-.->|check host| BLK
    REQ -->|route request| PX --> NET
    NET -->|HTML response| REQ
    TOK -.->|tokens| PG
    TOK -.->|page text| WTS
    NU -.->|enqueue| PG

    class PG,RD,BLK,PX,NET,WTS service
```