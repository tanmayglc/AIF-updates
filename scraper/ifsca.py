"""IFSCA scraper.

ifsca.gov.in renders its Circular / Notification / Consultation Paper / Press
Release tables client-side: the HTML ships an empty ``<table>`` and a DataTables
initialiser that calls a JSON endpoint. Scraping the rendered HTML would need a
headless browser, so the scraper talks to the same JSON endpoints the page does
(discovered by watching the page's own network traffic - see README.md).

Each row carries a ``PhotoFileID`` / ``PhotoFileName`` pair, which is what the
site turns into its View and Download links. The View URL is stable per document
and is used as the canonical link and dedupe key.
"""

import logging
from urllib.parse import urlencode

from . import config
from .store import make_item

log = logging.getLogger(__name__)

REGULATOR = "IFSCA"

VIEW_URL = f"{config.IFSCA_BASE}/CommonDirect/GetFileView"
DOWNLOAD_URL = f"{config.IFSCA_BASE}/CommonDirect/DownloadFile"

_AJAX_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}


def _document_url(row, title_name):
    """Rebuild the site's own "View" link for a row.

    ``title_name`` is the section label the site passes ("Legal" for circulars,
    notifications and press releases, "Report and Publication" for the reports
    section); it is part of the URL the site itself links to.
    """
    file_id = row.get("PhotoFileID")
    file_name = row.get("PhotoFileName")
    if not file_id or not file_name:
        return None
    query = urlencode({"id": file_id, "fileName": file_name, "TitleName": title_name})
    return f"{VIEW_URL}?{query}"


def _datatables_params(source, page, page_size):
    """The minimum subset of DataTables parameters the endpoint accepts."""
    return {
        "draw": page,
        "start": (page - 1) * page_size,
        "length": page_size,
        "order[0][column]": 0,
        "order[0][dir]": "desc",
        "PageNumber": page,
        "PageSize": page_size,
        "SearchText": "",
        "EncryptedId": source["encrypted_id"],
        "DateFrom": "",
        "DateTo": "",
        "AIlistType": "",
    }


def _fetch_page(fetcher, source, page):
    """Return (rows, total_records, ok)."""
    resp = fetcher.get(
        source["endpoint"],
        params=_datatables_params(source, page, config.IFSCA_PAGE_SIZE),
        headers=dict(_AJAX_HEADERS, Referer=source["landing"]),
        required=False,
    )
    if resp is None:
        return [], None, False

    try:
        payload = resp.json()
    except ValueError:
        log.error("%s page %s: response was not JSON", source["name"], page)
        return [], None, False

    data = payload.get("data") or {}
    rows = data.get(source["list_key"]) or []
    total = None
    if rows:
        pagination = rows[0].get("PaginationRequest") or {}
        total = pagination.get("TotalRecord")
    return rows, total, True


def scrape_source(fetcher, source):
    """Page through one IFSCA listing, filtering to fund-management items."""
    items = []
    seen_urls = set()
    ok = False

    for page in range(1, config.IFSCA_MAX_PAGES + 1):
        rows, total, page_ok = _fetch_page(fetcher, source, page)
        ok = ok or page_ok
        if not rows:
            break

        for row in rows:
            title = row.get("Title") or ""
            url = _document_url(row, source["title_name"])
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            matched = config.match_keywords(title, config.IFSCA_KEYWORD_RES)
            if not matched:
                continue

            item = make_item(
                title=title,
                raw_date=row.get("PublishDate"),
                regulator=REGULATOR,
                doc_type=source["doc_type"],
                url=url,
                source_page=source["landing"],
                source_name=source["name"],
                matched_keywords=matched,
            )
            if item:
                # Filenames contain spaces and smart quotes, so this is
                # percent-encoded rather than concatenated.
                item["download_url"] = DOWNLOAD_URL + "?" + urlencode(
                    {"id": row["PhotoFileID"], "fileName": row["PhotoFileName"]}
                )
                items.append(item)

        if len(rows) < config.IFSCA_PAGE_SIZE:
            break  # last page
        if total is not None and page * config.IFSCA_PAGE_SIZE >= total:
            break

    log.info("%s: %s fund-management items", source["name"], len(items))
    return items, ok


def scrape(fetcher):
    """Run every IFSCA source. Returns (items, ok_source_names, failed_names)."""
    items = []
    ok, failed = [], []
    for source in config.IFSCA_SOURCES:
        source_items, source_ok = scrape_source(fetcher, source)
        items.extend(source_items)
        (ok if source_ok else failed).append(source["name"])
    return items, ok, failed
