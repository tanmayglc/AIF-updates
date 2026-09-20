"""Fetching and extracting the text of a tracked document.

Both regulators ultimately publish PDFs, by different routes:

* **SEBI** serves an HTML shell whose body is almost empty - the real circular
  sits in an ``<iframe>`` pointing at ``/sebi_data/attachdocs/...pdf``. A few
  older pages and most press releases do carry inline text, so inline is used
  when there is enough of it and the attachment is the fallback.
* **IFSCA** rows already carry a direct PDF link (``download_url``).

Extracted text is used only to produce a summary and is never stored.
"""

import io
import logging
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

# Enough of a document to summarise well without sending a whole master
# circular to a model.
MAX_PAGES = 8
MAX_CHARS = 14000

# Below this, an HTML page is assumed to be a shell around an attachment.
MIN_INLINE_CHARS = 900

_PDF_MAGIC = b"%PDF"


def _clean(text):
    if not text:
        return ""
    # PDF extraction leaves hard-wrapped lines and doubled spaces behind.
    text = text.replace("­", "")
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _pdf_text(blob):
    try:
        from pypdf import PdfReader
    except ImportError:
        log.error("pypdf is not installed - cannot read PDFs")
        return ""

    try:
        reader = PdfReader(io.BytesIO(blob))
    except Exception as exc:
        log.warning("could not open PDF (%s)", exc)
        return ""

    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")
        except Exception:
            log.warning("PDF is encrypted - skipping")
            return ""

    parts = []
    for page in reader.pages[:MAX_PAGES]:
        try:
            parts.append(page.extract_text() or "")
        except Exception as exc:
            log.debug("page extraction failed: %s", exc)
    return _clean("\n".join(parts))


def _html_text(html):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        tag.decompose()
    main = soup.find(id="main-content") or soup.body or soup
    return _clean(main.get_text("\n", strip=True))


def _attachment_url(html, page_url):
    """Find the PDF a SEBI page embeds, if any."""
    soup = BeautifulSoup(html, "html.parser")
    for frame in soup.find_all("iframe", src=True):
        src = frame["src"]
        # The viewer is linked as ../../../web/?file=https://...pdf
        if "file=" in src:
            src = src.split("file=", 1)[1]
        if ".pdf" in src.lower():
            return urljoin(page_url, src)

    match = re.search(r'["\'](\S*?attachdocs/[^"\']+?\.pdf)["\']', html, re.I)
    if match:
        return urljoin(page_url, match.group(1))
    return None


def fetch_text(item, fetcher):
    """Return the plain text of an item's document, or "" if unavailable.

    Network failures are never fatal: a document that cannot be read simply
    does not get a summary, and the next run will try again.
    """
    url = item.get("download_url") or item.get("url")
    if not url:
        return ""

    resp = fetcher.get(url, required=False)
    if resp is None:
        return ""

    content_type = (resp.headers.get("content-type") or "").lower()
    blob = resp.content

    # IFSCA serves PDFs as application/octet-stream, so sniff the magic bytes
    # rather than trusting the header.
    if blob[:4] == _PDF_MAGIC or "pdf" in content_type:
        return _pdf_text(blob)[:MAX_CHARS]

    if "html" not in content_type and blob[:1] not in (b"<", b"\n", b" "):
        log.debug("%s: unhandled content type %r", url, content_type)
        return ""

    html = resp.text
    inline = _html_text(html)
    if len(inline) >= MIN_INLINE_CHARS:
        return inline[:MAX_CHARS]

    attachment = _attachment_url(html, resp.url or url)
    if attachment:
        # Stay on the regulator's own domain.
        if urlparse(attachment).netloc and \
                urlparse(attachment).netloc != urlparse(url).netloc:
            log.debug("skipping off-site attachment %s", attachment)
            return inline[:MAX_CHARS]
        sub = fetcher.get(attachment, required=False)
        if sub is not None and sub.content[:4] == _PDF_MAGIC:
            text = _pdf_text(sub.content)
            if text:
                return text[:MAX_CHARS]

    return inline[:MAX_CHARS]
