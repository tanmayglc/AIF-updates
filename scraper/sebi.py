"""SEBI scraper.

Three complementary sources, because no single one is sufficient:

1. ``sebirss.xml`` - the official RSS feed. Fast and covers every document type,
   but only the most recent ~50 items across all of SEBI.
2. The "Alternative Investment Funds (AIFs)" category listing
   (``doListingAll=yes&cid=25``) - SEBI's own AIF classification, with an
   explicit Type column. Highest signal, so it bypasses the keyword filter.
3. The Circulars / Consultation Papers / Press Releases section listings, each
   queried through the site's own ``search=`` box with a handful of AIF terms.
   The listings only render 25 rows per view without a session-bound POST, so
   searching is how older items are reached without paging aggressively.

Everything from (1) and (3) is then filtered by the AIF keyword list in
config.py. Only titles, dates and links are stored - never document text.
"""

import logging
import xml.etree.ElementTree as ET
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from . import config
from .store import make_item

log = logging.getLogger(__name__)

REGULATOR = "SEBI"


def doc_type_from_url(url, fallback=None):
    path = (url or "").lower()
    for fragment, label in config.SEBI_URL_TYPE_MAP:
        if fragment in path:
            return label
    return fallback or "Other"


def _refine_report_type(doc_type, title):
    """SEBI files consultation papers under the generic "Reports" section."""
    if doc_type == "Report" and "consultation" in (title or "").lower():
        return "Consultation Paper"
    return doc_type


def _keep(title, keyword_filter):
    """Return (keep?, matched patterns) for an item title."""
    matched = config.match_keywords(title, config.SEBI_KEYWORD_RES)
    if not keyword_filter:
        return True, matched
    return bool(matched), matched


# --- Source 1: RSS ----------------------------------------------------------

def scrape_rss(fetcher):
    items = []
    resp = fetcher.get(config.SEBI_RSS_URL, required=False)
    if resp is None:
        return items, False

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as exc:
        log.error("SEBI RSS is not valid XML: %s", exc)
        return items, False

    for node in root.iterfind(".//item"):
        title = (node.findtext("title") or "").strip()
        link = (node.findtext("link") or "").strip()
        pub_date = (node.findtext("pubDate") or "").strip()
        if not link:
            continue
        keep, matched = _keep(title, keyword_filter=True)
        if not keep:
            continue
        doc_type = _refine_report_type(doc_type_from_url(link), title)
        item = make_item(
            title=title,
            raw_date=pub_date,
            regulator=REGULATOR,
            doc_type=doc_type,
            url=link,
            source_page=config.SEBI_RSS_URL,
            source_name="SEBI RSS feed",
            matched_keywords=matched,
        )
        if item:
            items.append(item)

    log.info("SEBI RSS: %s AIF-related items", len(items))
    return items, True


# --- Sources 2 and 3: HTML listings ----------------------------------------

def _parse_listing(html, source, search_term):
    """Pull rows out of the ``#sample_1`` listing table.

    Column layout varies by section, so columns are identified by content:
    the first cell is the date, the last cell holds the linked title, and a
    middle cell may carry SEBI's own type label or a press-release number.
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="sample_1")
    if table is None:
        # A search that matches nothing renders a page with no table at all,
        # which is an ordinary outcome rather than a scraping failure.
        level = log.debug if search_term else log.warning
        level("%s (search=%r): no listing table found", source["name"], search_term)
        return []

    headers = [th.get_text(" ", strip=True).lower()
               for th in table.find_all("th")]
    type_index = headers.index("type") if "type" in headers else None

    rows = []
    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 2:
            continue  # header row
        link = tr.find("a", href=True)
        if link is None:
            continue

        url = urljoin(config.SEBI_BASE, link["href"])
        title = link.get_text(" ", strip=True) or cells[-1].get_text(" ", strip=True)
        raw_date = cells[0].get_text(" ", strip=True)

        doc_type = None
        if type_index is not None and type_index < len(cells):
            label = cells[type_index].get_text(" ", strip=True).lower()
            doc_type = config.SEBI_TYPE_LABEL_MAP.get(label)
        doc_type = doc_type or doc_type_from_url(url, source.get("default_doc_type"))
        doc_type = _refine_report_type(doc_type, title)

        keep, matched = _keep(title, source.get("keyword_filter", True))
        if not keep:
            continue

        item = make_item(
            title=title,
            raw_date=raw_date,
            regulator=REGULATOR,
            doc_type=doc_type,
            url=url,
            source_page=source.get("landing"),
            source_name=source["name"],
            matched_keywords=matched,
        )
        if item:
            rows.append(item)
    return rows


def scrape_category(fetcher):
    """SEBI's pre-classified AIF category listing."""
    source = dict(config.SEBI_AIF_CATEGORY)
    params = dict(source["params"])
    landing = config.SEBI_LISTING_URL + "?" + "&".join(
        f"{k}={v}" for k, v in params.items())
    source["landing"] = landing

    resp = fetcher.get(config.SEBI_LISTING_URL, params=params, required=False)
    if resp is None:
        return [], False
    items = _parse_listing(resp.text, source, "")
    log.info("%s: %s items", source["name"], len(items))
    return items, True


def scrape_sections(fetcher):
    items = []
    ok_names, failed_names = [], []

    for section in config.SEBI_SECTIONS:
        base_params = dict(section["params"])
        landing = config.SEBI_LISTING_URL + "?" + "&".join(
            f"{k}={v}" for k, v in base_params.items())
        source = dict(section, landing=landing)

        section_items = []
        section_ok = False
        for term in config.SEBI_SEARCH_TERMS:
            params = dict(base_params)
            if term:
                params["search"] = term
            resp = fetcher.get(config.SEBI_LISTING_URL, params=params, required=False)
            if resp is None:
                continue
            section_ok = True
            section_items.extend(_parse_listing(resp.text, source, term))

        items.extend(section_items)
        log.info("%s: %s matching items across %s queries",
                 section["name"], len(section_items), len(config.SEBI_SEARCH_TERMS))
        (ok_names if section_ok else failed_names).append(section["name"])

    return items, ok_names, failed_names


def scrape(fetcher):
    """Run every SEBI source. Returns (items, ok_source_names, failed_names)."""
    items = []
    ok, failed = [], []

    rss_items, rss_ok = scrape_rss(fetcher)
    items.extend(rss_items)
    (ok if rss_ok else failed).append("SEBI RSS feed")

    cat_items, cat_ok = scrape_category(fetcher)
    items.extend(cat_items)
    (ok if cat_ok else failed).append(config.SEBI_AIF_CATEGORY["name"])

    section_items, section_ok, section_failed = scrape_sections(fetcher)
    items.extend(section_items)
    ok.extend(section_ok)
    failed.extend(section_failed)

    return items, ok, failed
