# SEBI AIF & IFSCA Fund Management Tracker

A static site that tracks regulatory updates on **SEBI Alternative Investment
Funds** and **IFSCA fund management**. A Python scraper collects titles, dates
and links from both regulators every six hours; a dependency-free single-page
frontend reads the resulting JSON.

The tracker stores **only titles, dates, document types and URLs**. It never
copies document text — every entry links back to the regulator's own page or PDF.

```
.
├── scraper/              # the scraper package
│   ├── config.py         # sources, keyword filters, crawl settings
│   ├── http.py           # robots.txt-aware, rate-limited HTTP client
│   ├── store.py          # normalisation, dedup, the JSON store
│   ├── sebi.py           # SEBI: RSS + AIF category + section listings
│   ├── ifsca.py          # IFSCA: DataTables JSON endpoints
│   └── run.py            # entry point
├── data/news.json        # the committed dataset (history lives here)
├── site/                 # what GitHub Pages publishes
│   ├── index.html
│   ├── styles.css
│   └── app.js
└── .github/workflows/update.yml
```

## Quick start

```bash
pip install -r requirements.txt
python -m scraper.run
python -m http.server --directory site 8000   # then open http://localhost:8000
```

`python -m scraper.run` writes `data/news.json` and, for local preview, copies
it to `site/data/news.json` (gitignored — the workflow stages its own copy at
deploy time).

Useful flags:

| Flag | Effect |
| --- | --- |
| `--only sebi` / `--only ifsca` | Scrape one regulator |
| `--dry-run` | Scrape and report, write nothing |
| `--delay 5` | Raise the minimum gap between requests to a host |
| `--output PATH` | Write somewhere other than `data/news.json` |
| `--no-site-copy` | Skip the `site/data/` preview copy (used in CI) |
| `-v` | Debug logging |

A full run makes about 28 requests and takes roughly three minutes, almost all
of it spent deliberately waiting between requests.

## Where the data comes from

### SEBI

SEBI publishes an RSS feed, but it only carries the most recent ~50 items across
the whole organisation, so three sources are combined:

| Source | URL |
| --- | --- |
| RSS feed | `https://www.sebi.gov.in/sebirss.xml` |
| AIF category listing | `HomeAction.do?doListingAll=yes&cid=25` |
| Circulars | `HomeAction.do?doListing=yes&sid=1&ssid=7&smid=0` |
| Consultation papers | `HomeAction.do?doListing=yes&sid=4&ssid=38&smid=35` |
| Press releases | `HomeAction.do?doListing=yes&sid=6&ssid=23&smid=0` |

The `sid` / `ssid` / `smid` values are SEBI's section identifiers; they can be
re-derived from the `<select name="ssid">` and `<select name="smid">` dropdowns
on any listing page if SEBI ever renumbers them.

Two details worth knowing:

- **Listings show only 25 rows.** Paging deeper needs a session-bound POST that
  SEBI's edge blocks (HTTP 530). Instead, each section is queried through the
  site's own `&search=` parameter with a handful of AIF terms, which reaches
  material going back to the 1990s without hammering the site.
- **`cid=25` is SEBI's own "Alternative Investment Funds (AIFs)" category.** It
  spans every document type and carries an explicit Type column, so it is the
  one SEBI source that bypasses the keyword filter.

Everything else from SEBI is filtered against the AIF keyword list in
`config.py` (`alternative investment fund`, `AIF`, `angel fund`, `accredited
investor`, `placement memorandum`, and so on). Each stored item records which
keywords matched, so filter decisions are auditable.

### IFSCA

`ifsca.gov.in` renders its listing tables client-side: the HTML ships an empty
`<table>` and a DataTables initialiser that calls a JSON endpoint. Rather than
run a headless browser, the scraper calls the same endpoints the page does.

| Section | Endpoint | `EncryptedId` |
| --- | --- | --- |
| Circulars | `/Legal/GetLegalData` | `wF6kttc1JR8=` |
| Notifications | `/Legal/GetLegalData` | `zcGvy-Iqfcg=` |
| Consultation papers | `/ReportPublication/GetReportPublicationData` | `sKCVtbX6J9o=` |
| Press releases | `/PressRelease/GetLegalData` | `MEdJSLhva0M=` |

Each row carries a `PhotoFileID` / `PhotoFileName` pair, which IFSCA's own
frontend turns into its View and Download links. The View URL
(`/CommonDirect/GetFileView?id=…&fileName=…`) is stable per document and is used
as the canonical link and the dedup key.

IFSCA also regulates banking, insurance and aircraft leasing, so its items are
filtered against the fund-management keyword list — deliberately broader than
the SEBI one, because IFSCA titles are less standardised.

> If either site is restructured, these are the things to re-check: the
> `EncryptedId` values above, the `sid`/`ssid`/`smid` numbers, and whether
> `#sample_1` is still the id of SEBI's listing table. The endpoints were
> confirmed against the live sites in September 2026.

## Being a good citizen

- **robots.txt is fetched and honoured per host.** SEBI's allows everything
  outside `/js` and `/css`; IFSCA publishes none, which RFC 9309 reads as
  "allowed". A robots.txt that returns 5xx or cannot be fetched is treated as
  disallow-all, not as permission.
- **Requests are throttled**: at least 2.5 s between requests to the same host,
  plus up to 1 s of jitter, raised further if robots.txt advertises a
  `Crawl-delay`. Retries use linear backoff and give up after three attempts.
- **A descriptive User-Agent** identifies the tracker.
- **No document text is stored or republished** — only the metadata needed to
  find and link the original.

If you fork this, please keep the delays and put your own contact details in
`USER_AGENT` in `config.py`.

## The data file

`data/news.json` is the committed dataset and the history. Items are
deduplicated by canonical URL (case-normalised host, `;jsessionid=` and tracking
parameters stripped), and an item that drops off a regulator's listing **stays**
in the file with its original `first_seen`.

```jsonc
{
  "schema_version": 1,
  "generated_at": "2026-09-20T11:07:51+00:00",
  "counts": { "total": 168, "by_regulator": {...}, "by_doc_type": {...} },
  "date_range": { "earliest": "1996-02-13", "latest": "2026-09-18" },
  "sources": { "ok": [...], "failed": [] },
  "items": [
    {
      "id": "c6a91a44458eacd8",      // sha1 of the canonical URL, truncated
      "title": "Relaxation in timeline ... for Angel Funds",
      "date": "2026-09-07",           // ISO, or null when unparseable
      "date_display": "07 Sep 2026",
      "regulator": "SEBI",            // SEBI | IFSCA
      "doc_type": "Circular",
      "url": "https://www.sebi.gov.in/legal/circulars/...",
      "source_page": "https://www.sebi.gov.in/...",   // the listing it came from
      "source_name": "SEBI Circulars",
      "matched_keywords": ["accredited investor", "angel fund"],
      "first_seen": "2026-09-20T11:07:51+00:00",   // when the tracker first saw it
      "last_seen":  "2026-09-20T11:07:51+00:00"    // last run that observed it listed
    }
  ]
}
```

`last_seen` moves on every run, so the file is rewritten every time even when
nothing has happened. To keep that out of the commit history, the scraper
compares a fingerprint of each item with `last_seen` excluded and reports a
`changed` flag; CI only commits when that flag is true. `generated_at` therefore
means *when the dataset last changed*, which is what the site shows as "last
updated" — the workflow's run history is the record of how recently it checked.

Dates are parsed from four different regulator formats (`Sep 07, 2026`,
`17 Sep, 2026 +0530`, `18/09/2026`, ISO). An unrecognised format stores `null`
rather than a guess, and the frontend shows the item as undated instead of
inventing a date for it.

## The frontend

`site/` is three static files with no dependencies and no build step.

- Filter by regulator, document type and period; counts on each chip reflect the
  other active filters.
- Multi-term search across titles, document types and matched keywords, with the
  match highlighted.
- Sort by newest, oldest, title, or recently added to the tracker.
- Items added in the last seven days are flagged `new`.
- Light and dark themes follow the system setting; the layout works down to
  phone width.

Opening `site/index.html` straight off disk will fail the `fetch` because of
the `file://` origin — serve the folder over HTTP instead.

## Deployment

`.github/workflows/update.yml` runs every six hours (and on demand via
**Actions → Run workflow**):

1. **scrape** — installs dependencies, runs the scraper, and commits
   `data/news.json` only when an item was actually added or changed (see the
   `changed` flag above), so a quiet day leaves no commit. It rebases before
   pushing in case a previous run landed mid-scrape, and writes a run summary.
2. **deploy** — checks out the commit the scrape job produced, copies
   `data/news.json` into `site/data/`, and publishes `site/` to GitHub Pages.

If every source fails, the scraper exits non-zero and the workflow fails rather
than publishing an empty file. A partial failure is recorded in
`sources.failed` and shown in the site footer.

Two guards keep a quiet tracker from becoming a dead one:

- **Keepalive.** GitHub disables cron workflows after 60 days without repository
  activity. Since a quiet run deliberately commits nothing, the scrape job makes
  an empty commit if the last commit is 45+ days old.
- **Health check.** A scraper that has silently stopped finding anything looks
  exactly like a quiet week. The workflow annotates the run with a warning when
  any source failed, or when the newest tracked item is more than 60 days old —
  which usually means a regulator restructured its site and the selectors or
  endpoints in `config.py` need re-checking.

One-time repository setup:

1. **Settings → Pages → Source: GitHub Actions.**
2. **Settings → Actions → General → Workflow permissions: Read and write**
   (the data commit needs it).

Note that GitHub disables scheduled workflows in repositories with no activity
for 60 days.

## Caveats

- Keyword filtering is title-only, because that is all the listings expose.
  An AIF circular with an unusual title can be missed; widen the list in
  `config.py` if you find one. False positives are visible via each item's
  `matched_keywords`.
- SEBI's search index lags its listings slightly, which is why each section is
  also fetched unfiltered — new items show up on the first pass either way.
- This is an unofficial tracker. Treat `sebi.gov.in` and `ifsca.gov.in` as the
  authoritative sources.
