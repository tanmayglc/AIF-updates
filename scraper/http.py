"""A deliberately slow, robots.txt-aware HTTP client.

Both regulators run modest infrastructure, so every request goes through:

* a robots.txt check (cached per host, fail-open only on a 4xx "no robots file")
* a per-host delay of at least ``REQUEST_DELAY_SECONDS`` plus jitter, raised to
  the advertised ``Crawl-delay`` when robots.txt specifies one
* bounded retries with linear backoff for transient failures
"""

import logging
import random
import time
from urllib.parse import urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import requests

from . import config

log = logging.getLogger(__name__)


class Fetcher:
    def __init__(self, user_agent=config.USER_AGENT, obey_robots=True):
        self.user_agent = user_agent
        self.obey_robots = obey_robots
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept-Language": "en-IN,en;q=0.9",
            }
        )
        self._robots = {}       # host -> RobotFileParser or None
        self._crawl_delay = {}  # host -> float
        self._last_request = {}  # host -> monotonic timestamp
        self.request_count = 0

    # -- robots.txt ---------------------------------------------------------

    def _robots_for(self, url):
        parts = urlparse(url)
        host = parts.netloc
        if host in self._robots:
            return self._robots[host]

        robots_url = urlunparse((parts.scheme, host, "/robots.txt", "", "", ""))
        parser = None
        try:
            resp = self.session.get(robots_url, timeout=config.REQUEST_TIMEOUT_SECONDS)
            if resp.status_code == 200:
                parser = RobotFileParser()
                parser.parse(resp.text.splitlines())
                log.info("robots.txt loaded for %s", host)
            elif 400 <= resp.status_code < 500:
                # No robots.txt published: RFC 9309 says everything is allowed.
                log.info("no robots.txt for %s (HTTP %s), assuming allowed",
                         host, resp.status_code)
            else:
                # 5xx means "stay out" under RFC 9309. Be conservative.
                parser = RobotFileParser()
                parser.disallow_all = True
                log.warning("robots.txt for %s returned HTTP %s, treating as "
                            "disallow-all", host, resp.status_code)
        except requests.RequestException as exc:
            parser = RobotFileParser()
            parser.disallow_all = True
            log.warning("could not fetch robots.txt for %s (%s), treating as "
                        "disallow-all", host, exc)

        self._robots[host] = parser
        if parser is not None:
            try:
                delay = parser.crawl_delay(self.user_agent) or parser.crawl_delay("*")
            except Exception:
                delay = None
            if delay:
                self._crawl_delay[host] = float(delay)
                log.info("robots.txt Crawl-delay for %s: %ss", host, delay)
        return parser

    def allowed(self, url):
        if not self.obey_robots:
            return True
        parser = self._robots_for(url)
        if parser is None:
            return True
        return parser.can_fetch(self.user_agent, url)

    # -- throttling ---------------------------------------------------------

    def _wait_turn(self, url):
        host = urlparse(url).netloc
        delay = max(config.REQUEST_DELAY_SECONDS, self._crawl_delay.get(host, 0))
        delay += random.uniform(0, config.REQUEST_JITTER_SECONDS)
        last = self._last_request.get(host)
        if last is not None:
            remaining = delay - (time.monotonic() - last)
            if remaining > 0:
                time.sleep(remaining)
        self._last_request[host] = time.monotonic()

    # -- requests -----------------------------------------------------------

    def get(self, url, params=None, headers=None, required=True):
        """GET a URL, or return None when it is blocked or keeps failing.

        ``required=False`` downgrades failures to a warning; the caller decides
        whether a missing source is fatal.
        """
        if not self.allowed(url):
            log.warning("robots.txt disallows %s - skipping", url)
            return None

        last_error = None
        for attempt in range(1, config.MAX_RETRIES + 1):
            self._wait_turn(url)
            try:
                self.request_count += 1
                resp = self.session.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=config.REQUEST_TIMEOUT_SECONDS,
                )
                if resp.status_code == 200:
                    return resp
                last_error = "HTTP %s" % resp.status_code
                # 4xx other than 429 will not improve on retry.
                if 400 <= resp.status_code < 500 and resp.status_code != 429:
                    break
            except requests.RequestException as exc:
                last_error = repr(exc)

            if attempt < config.MAX_RETRIES:
                backoff = config.RETRY_BACKOFF_SECONDS * attempt
                log.warning("%s -> %s (attempt %s/%s), retrying in %ss",
                            url, last_error, attempt, config.MAX_RETRIES, backoff)
                time.sleep(backoff)

        level = log.error if required else log.warning
        level("giving up on %s: %s", url, last_error)
        return None
