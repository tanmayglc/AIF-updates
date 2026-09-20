"""Normalisation, deduplication and the on-disk JSON store.

The store is append-only in spirit: an item that disappears from a regulator's
listing stays in ``data/news.json`` with its original ``first_seen``. Only the
mutable fields (title, date, doc_type, last_seen) are refreshed on a re-scrape.
"""

import hashlib
import json
import logging
import os
import re
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# Query parameters that vary between requests but identify the same document.
_VOLATILE_QUERY_KEYS = {"_", "draw", "jsessionid", "utm_source", "utm_medium",
                        "utm_campaign", "utm_term", "utm_content"}

_MONTHS = {m.lower(): i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# --- URL handling -----------------------------------------------------------

def canonical_url(url):
    """Normalise a URL so the same document always hashes to the same id."""
    if not url:
        return ""
    url = url.strip()
    parts = urlparse(url)
    scheme = (parts.scheme or "https").lower()
    netloc = parts.netloc.lower()
    # SEBI occasionally appends ;jsessionid=... to paths.
    path = re.sub(r";jsessionid=[^/?]*", "", parts.path, flags=re.IGNORECASE)
    if path.endswith("/") and len(path) > 1:
        path = path.rstrip("/")
    query = urlencode(
        [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
         if k.lower() not in _VOLATILE_QUERY_KEYS]
    )
    return urlunparse((scheme, netloc, path, "", query, ""))


def item_id(url):
    return hashlib.sha1(canonical_url(url).encode("utf-8")).hexdigest()[:16]


# --- Text and date handling -------------------------------------------------

def clean_title(title):
    """Collapse whitespace and drop the boilerplate SEBI appends to titles."""
    if not title:
        return ""
    title = re.sub(r"\s+", " ", title).strip()
    title = re.sub(
        r"\s*Click here to (provide|submit) your comments\.?\s*$",
        "", title, flags=re.IGNORECASE,
    )
    return title.strip(" -–—")


def parse_date(raw):
    """Parse the date formats the two regulators use into ``YYYY-MM-DD``.

    Returns None rather than guessing when the format is unrecognised, so the
    frontend can show the item without inventing a date for it.
    """
    if not raw:
        return None
    text = re.sub(r"\s+", " ", str(raw)).strip().rstrip(",")
    # Drop a trailing timezone offset, e.g. "17 Sep, 2026 +0530".
    text = re.sub(r"\s*[+-]\d{4}$", "", text).strip()

    # "Sep 07, 2026" / "Sep 7 2026"
    m = re.match(r"^([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})$", text)
    if m:
        month = _MONTHS.get(m.group(1)[:3].lower())
        if month:
            return _safe_date(int(m.group(3)), month, int(m.group(2)))

    # "17 Sep, 2026" / "17 September 2026"
    m = re.match(r"^(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})$", text)
    if m:
        month = _MONTHS.get(m.group(2)[:3].lower())
        if month:
            return _safe_date(int(m.group(3)), month, int(m.group(1)))

    # "18/09/2026" (IFSCA, day first) and "18-09-2026"
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$", text)
    if m:
        return _safe_date(int(m.group(3)), int(m.group(2)), int(m.group(1)))

    # Already ISO.
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        return _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    log.debug("unparsed date: %r", raw)
    return None


def _safe_date(year, month, day):
    try:
        return datetime(year, month, day).strftime("%Y-%m-%d")
    except ValueError:
        return None


def display_date(iso):
    if not iso:
        return ""
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%d %b %Y")
    except ValueError:
        return iso


def make_item(title, raw_date, regulator, doc_type, url, source_page=None,
              source_name=None, matched_keywords=None):
    """Build a normalised record, or None if it is unusable."""
    url = (url or "").strip()
    title = clean_title(title)
    if not url or not title:
        return None
    iso = parse_date(raw_date)
    return {
        "id": item_id(url),
        "title": title,
        "date": iso,
        "date_display": display_date(iso),
        "regulator": regulator,
        "doc_type": doc_type or "Other",
        "url": url,
        "source_page": source_page,
        "source_name": source_name,
        "matched_keywords": sorted(set(matched_keywords or [])),
    }


# --- The store --------------------------------------------------------------

# Fields that change on every run without the underlying document changing.
# They are excluded from change detection so a scheduled run that finds nothing
# new does not produce a commit.
_VOLATILE_ITEM_FIELDS = ("last_seen",)


def fingerprint(item):
    """A stable hash of everything about an item that is worth committing."""
    payload = {k: v for k, v in item.items() if k not in _VOLATILE_ITEM_FIELDS}
    return hashlib.sha1(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def load(path):
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError) as exc:
        log.error("could not read %s (%s) - starting from an empty store", path, exc)
        return []
    if isinstance(payload, dict):
        return payload.get("items", [])
    if isinstance(payload, list):  # tolerate a bare list from an older run
        return payload
    return []


def merge(existing, scraped, seen_at=None):
    """Merge freshly scraped items into the stored history.

    Deduplication is by canonical URL. Returns ``(items, stats)``.
    """
    seen_at = seen_at or now_iso()
    by_id = {}
    for item in existing:
        key = item.get("id") or item_id(item.get("url", ""))
        item["id"] = key
        by_id[key] = item

    # The same document legitimately shows up in several feeds (e.g. an AIF
    # circular appears in the RSS feed, the AIF category listing and the
    # circulars search), and those feeds disagree about which listing it "came
    # from". Change detection therefore compares each item against a snapshot
    # taken before the merge rather than counting writes, so a document being
    # rewritten by a later feed with the same final value is not a change.
    before = {key: fingerprint(item) for key, item in by_id.items()}

    for fresh in scraped:
        key = fresh["id"]
        current = by_id.get(key)
        if current is None:
            fresh["first_seen"] = seen_at
            fresh["last_seen"] = seen_at
            by_id[key] = fresh
            continue

        # Refresh the mutable fields, but never lose a date or a keyword hit
        # we already had if this pass could not determine one.
        for field in ("title", "doc_type", "source_page", "source_name",
                      "download_url"):
            value = fresh.get(field)
            if value:
                current[field] = value
        if fresh.get("date"):
            current["date"] = fresh["date"]
            current["date_display"] = fresh["date_display"]
        current["matched_keywords"] = sorted(
            set(current.get("matched_keywords") or [])
            | set(fresh.get("matched_keywords") or []))

        current.setdefault("first_seen", seen_at)
        current["last_seen"] = seen_at

    items = sort_items(by_id.values())
    added = sum(1 for i in items if i["id"] not in before)
    updated = sum(1 for i in items
                  if i["id"] in before and fingerprint(i) != before[i["id"]])
    stats = {
        "added": added,
        "updated": updated,
        "unchanged": len(items) - added - updated,
        "total": len(items),
        "changed": bool(added or updated),
    }
    return items, stats


def sort_items(items):
    """Newest first. Undated items sink to the bottom, then sort by title."""
    return sorted(
        items,
        key=lambda i: (i.get("date") or "0000-00-00", i.get("title", "")),
        reverse=True,
    )


def build_payload(items, sources_ok, sources_failed):
    dated = [i["date"] for i in items if i.get("date")]
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now_iso(),
        "counts": {
            "total": len(items),
            "by_regulator": dict(Counter(i["regulator"] for i in items)),
            "by_doc_type": dict(Counter(i["doc_type"] for i in items)),
        },
        "date_range": {
            "earliest": min(dated) if dated else None,
            "latest": max(dated) if dated else None,
        },
        "sources": {"ok": sources_ok, "failed": sources_failed},
        "items": items,
    }


def save(path, payload):
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=False)
        fh.write("\n")
    os.replace(tmp, path)
    log.info("wrote %s (%s items)", path, payload["counts"]["total"])
