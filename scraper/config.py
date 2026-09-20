"""Source definitions, keyword filters and crawl settings.

Everything that is likely to need tuning lives here so the scrapers themselves
stay generic. Endpoints were confirmed against the live sites in Sep 2026;
see README.md for how they were discovered and how to re-check them.
"""

import re

# --- Crawl politeness -------------------------------------------------------

USER_AGENT = (
    "reg-tracker/1.0 (+https://github.com/; static regulatory-update tracker; "
    "contact via repository issues)"
)

# Minimum seconds between two requests to the same host. Overridden upwards if
# robots.txt advertises a larger Crawl-delay.
REQUEST_DELAY_SECONDS = 2.5

# Extra random jitter (0..JITTER) added to every delay.
REQUEST_JITTER_SECONDS = 1.0

REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5


# --- Keyword filters --------------------------------------------------------

# Each entry is (display label, regex). The label is what ends up in an item's
# ``matched_keywords`` and is shown as a chip in the UI, so it stays readable
# even when the pattern behind it is not. Matching is case-insensitive and runs
# against the item title.

# SEBI publishes across the whole securities market, so its feeds are filtered
# down to AIF-related material.
SEBI_AIF_KEYWORDS = [
    ("alternative investment fund", r"alternative investment funds?"),
    ("AIF", r"\baifs?\b"),
    ("angel fund", r"angel funds?"),
    ("venture capital fund", r"venture capital funds?"),
    ("VCF", r"\bvcfs?\b"),
    ("fund of funds", r"funds? of funds?"),
    ("large value fund", r"large value funds?"),
    ("LVF", r"\blvfs?\b"),
    ("accredited investor", r"accredited investors?"),
    ("special situation fund", r"special situation funds?"),
    ("CDMDF", r"corporate debt market development fund|\bcdmdf\b"),
    ("co-investment", r"co-?investment"),
    ("placement memorandum", r"placement memorandum|\bppms?\b"),
]

# IFSCA is a much smaller regulator but still covers banking, insurance,
# capital markets and aircraft leasing. Filtered down to fund-management
# material. Deliberately broader than the SEBI list because IFSCA titles are
# less standardised.
IFSCA_FUND_KEYWORDS = [
    ("fund management", r"fund manage"),
    ("FME", r"\bfmes?\b"),
    ("fund", r"\bfunds?\b"),
    ("scheme", r"\bschemes?\b"),
    ("alternative investment", r"alternative investment"),
    ("AIF", r"\baifs?\b"),
    ("portfolio management", r"portfolio manage"),
    ("asset management", r"asset manage"),
    ("venture capital", r"venture capital"),
    ("family investment fund", r"family investment|\bfifs?\b"),
    ("angel", r"\bangel\b"),
    ("investment adviser", r"investment advis"),
    ("placement memorandum", r"placement memorandum|\bppms?\b"),
    ("ETF", r"\betfs?\b|exchange traded funds?"),
    ("custodian", r"custodian"),
    ("distributor", r"distributor"),
    ("accredited investor", r"accredited investors?"),
]


def _compile(entries):
    return [(label, re.compile(pattern, re.IGNORECASE)) for label, pattern in entries]


SEBI_KEYWORD_RES = _compile(SEBI_AIF_KEYWORDS)
IFSCA_KEYWORD_RES = _compile(IFSCA_FUND_KEYWORDS)


def match_keywords(text, compiled):
    """Return the display labels of every keyword that matched ``text``."""
    if not text:
        return []
    return [label for label, regex in compiled if regex.search(text)]


# --- SEBI -------------------------------------------------------------------

SEBI_BASE = "https://www.sebi.gov.in"
SEBI_RSS_URL = f"{SEBI_BASE}/sebirss.xml"

# SEBI's listing servlet. sid = section, ssid = sub-section, smid = sub-sub.
# Values enumerated from the <select name="ssid"> / <select name="smid"> on the
# listing pages themselves.
SEBI_LISTING_URL = f"{SEBI_BASE}/sebiweb/home/HomeAction.do"

# The site also exposes a pre-filtered "Alternative Investment Funds (AIFs)"
# category that spans every document type and carries an explicit Type column.
# This is the highest-signal SEBI source, so it is never keyword-filtered.
SEBI_AIF_CATEGORY = {
    "name": "SEBI AIF category listing",
    "params": {"doListingAll": "yes", "cid": "25"},
    "has_type_column": True,
    "keyword_filter": False,
    "default_doc_type": None,
}

# Search terms pushed through each section's own search box. SEBI only renders
# the first 25 rows per listing without a session-bound POST, so searching is
# how historical items are reached without hammering the site.
SEBI_SEARCH_TERMS = [
    "",  # unfiltered first page, catches brand-new items before the index updates
    "alternative investment",
    "AIF",
    "angel fund",
    "venture capital fund",
]

SEBI_SECTIONS = [
    {
        "name": "SEBI Circulars",
        "params": {"doListing": "yes", "sid": "1", "ssid": "7", "smid": "0"},
        "default_doc_type": "Circular",
        "has_type_column": False,
        "keyword_filter": True,
    },
    {
        "name": "SEBI Consultation Papers",
        # Reports & Statistics > Reports > Reports for Public Comments
        "params": {"doListing": "yes", "sid": "4", "ssid": "38", "smid": "35"},
        "default_doc_type": "Consultation Paper",
        "has_type_column": False,
        "keyword_filter": True,
    },
    {
        "name": "SEBI Press Releases",
        "params": {"doListing": "yes", "sid": "6", "ssid": "23", "smid": "0"},
        "default_doc_type": "Press Release",
        "has_type_column": False,
        "keyword_filter": True,
    },
]

# Maps a SEBI document URL path to a document type. Checked in order.
SEBI_URL_TYPE_MAP = [
    ("/legal/master-circulars/", "Master Circular"),
    ("/legal/circulars/", "Circular"),
    ("/legal/regulations/", "Regulation"),
    ("/legal/guidelines/", "Guideline"),
    ("/legal/general-orders/", "General Order"),
    ("/legal/gazette-notification/", "Gazette Notification"),
    ("/legal/acts/", "Act"),
    ("/legal/rules/", "Rule"),
    ("/media-and-notifications/press-releases/", "Press Release"),
    ("/media-and-notifications/public-notices/", "Public Notice"),
    ("/media-and-notifications/speeches/", "Speech"),
    ("/reports-and-statistics/reports/", "Report"),
    ("/enforcement/orders/", "Order"),
]

# SEBI's own Type column labels, normalised to ours.
SEBI_TYPE_LABEL_MAP = {
    "circulars": "Circular",
    "master circulars": "Master Circular",
    "regulations": "Regulation",
    "guidelines": "Guideline",
    "reports": "Report",
    "press releases": "Press Release",
    "public notices": "Public Notice",
    "orders": "Order",
    "general orders": "General Order",
    "gazette notification": "Gazette Notification",
    "acts": "Act",
    "rules": "Rule",
    "informal guidance": "Informal Guidance",
}


# --- IFSCA ------------------------------------------------------------------

IFSCA_BASE = "https://ifsca.gov.in"

# ifsca.gov.in renders its listing tables client-side from DataTables JSON
# endpoints. The EncryptedId values are the opaque ids used in the public
# listing URLs (e.g. /Legal/Index/wF6kttc1JR8=).
IFSCA_SOURCES = [
    {
        "name": "IFSCA Circulars",
        "endpoint": f"{IFSCA_BASE}/Legal/GetLegalData",
        "encrypted_id": "wF6kttc1JR8=",
        "landing": f"{IFSCA_BASE}/Legal/Index/wF6kttc1JR8=",
        "list_key": "LegalMasterModelList",
        "title_name": "Legal",
        "doc_type": "Circular",
    },
    {
        "name": "IFSCA Notifications",
        "endpoint": f"{IFSCA_BASE}/Legal/GetLegalData",
        "encrypted_id": "zcGvy-Iqfcg=",
        "landing": f"{IFSCA_BASE}/Legal/Index/zcGvy-Iqfcg=",
        "list_key": "LegalMasterModelList",
        "title_name": "Legal",
        "doc_type": "Notification",
    },
    {
        "name": "IFSCA Consultation Papers",
        "endpoint": f"{IFSCA_BASE}/ReportPublication/GetReportPublicationData",
        "encrypted_id": "sKCVtbX6J9o=",
        "landing": f"{IFSCA_BASE}/ReportPublication/index/sKCVtbX6J9o=",
        "list_key": "reportandPublicationModels",
        "title_name": "Report and Publication",
        "doc_type": "Consultation Paper",
    },
    {
        "name": "IFSCA Press Releases",
        "endpoint": f"{IFSCA_BASE}/PressRelease/GetLegalData",
        "encrypted_id": "MEdJSLhva0M=",
        "landing": f"{IFSCA_BASE}/PressRelease/Index/MEdJSLhva0M=",
        "list_key": "LegalMasterModelList",
        "title_name": "Legal",
        "doc_type": "Press Release",
    },
]

# Rows requested per IFSCA endpoint call, and how many pages deep to go.
IFSCA_PAGE_SIZE = 50
IFSCA_MAX_PAGES = 3
