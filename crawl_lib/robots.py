"""Async robots.txt cache: one fetch per origin per day, Crawl-delay honored (capped)."""

import time
from urllib.robotparser import RobotFileParser
from urllib.parse import urlsplit

import aiohttp
import asyncio

ROBOTS_TTL = 86400          # a site's robots.txt is refetched at most once a day
ROBOTS_ERROR_TTL = 600      # network error: crawl permissively (like utils.allowed_by_robots) but retry in 10 minutes, not on every page
MAX_ROBOTS_BYTES = 512 * 1024
MAX_CRAWL_DELAY = 30.0


class RobotsEntry:
    __slots__ = ("parser", "delay", "expires")

    def __init__(self, parser, delay, expires):
        self.parser = parser   # None = no robots.txt (everything allowed)
        self.delay = delay     # seconds from Crawl-delay, or None
        self.expires = expires

    def allowed(self, user_agent, url):
        return self.parser is None or self.parser.can_fetch(user_agent, url)


class RobotsCache:
    def __init__(self, fetch_bytes, user_agent, max_entries=50000):
        """fetch_bytes: async (robots_url) -> (status, body_bytes). Raises on network errors"""
        self._fetch = fetch_bytes
        self.user_agent = user_agent
        self._cache = {}
        self._max = max_entries
        self.fetched = 0
        self.errors = 0

    async def get(self, url):
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        entry = self._cache.get(origin)
        now = time.monotonic()
        if entry is not None and entry.expires > now:
            return entry
        try:
            status, body = await self._fetch(origin + "/robots.txt")
            self.fetched += 1
            if status != 200:
                entry = RobotsEntry(None, None, now + ROBOTS_TTL)
            else:
                rp = RobotFileParser()
                rp.parse(body.decode("utf-8", errors="replace").splitlines())
                delay = rp.crawl_delay(self.user_agent)
                entry = RobotsEntry(rp, float(delay) if delay is not None else None, now + ROBOTS_TTL)
        except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeError, ValueError, OSError):
            self.errors += 1
            entry = RobotsEntry(None, None, now + ROBOTS_ERROR_TTL)
        if len(self._cache) >= self._max:
            self._cache.clear()
        self._cache[origin] = entry
        return entry
