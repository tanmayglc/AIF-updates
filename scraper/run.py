"""Entry point: scrape both regulators and update data/news.json.

    python -m scraper.run                 # scrape everything
    python -m scraper.run --only sebi     # one regulator
    python -m scraper.run --dry-run       # scrape but do not write

Exit codes: 0 on success, 1 if every configured source failed (so a scheduled
run fails loudly rather than silently committing an empty result).
"""

import argparse
import logging
import os
import shutil
import sys

from . import config, documents, ifsca, sebi, store
from .http import Fetcher
from .summarize import Summariser

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUTPUT = os.path.join(REPO_ROOT, "data", "news.json")
SITE_COPY = os.path.join(REPO_ROOT, "site", "data", "news.json")

log = logging.getLogger("scraper")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", choices=["sebi", "ifsca"],
                        help="scrape a single regulator")
    parser.add_argument("--output", default=DEFAULT_OUTPUT,
                        help="path to news.json (default: data/news.json)")
    parser.add_argument("--no-site-copy", action="store_true",
                        help="skip copying the JSON into site/data/ for local preview")
    parser.add_argument("--dry-run", action="store_true",
                        help="scrape and report, but do not write any file")
    parser.add_argument("--summarise", "--summarize", dest="summarise",
                        action="store_true",
                        help="fetch and summarise documents that have no summary yet")
    parser.add_argument("--summary-limit", type=int, default=40,
                        help="maximum documents to summarise in one run "
                             "(default: 40, keeps free-tier rate limits happy)")
    parser.add_argument("--resummarise", action="store_true",
                        help="regenerate summaries that already exist")
    parser.add_argument("--ignore-robots", action="store_true",
                        help="skip robots.txt checks (not recommended)")
    parser.add_argument("--delay", type=float, default=config.REQUEST_DELAY_SECONDS,
                        help="minimum seconds between requests to a host "
                             f"(default: {config.REQUEST_DELAY_SECONDS})")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def summarise_pending(items, fetcher, limit, redo=False):
    """Summarise up to ``limit`` documents that have no summary yet.

    Deliberately incremental: a summary is generated once and kept, so the
    initial backfill spreads over several scheduled runs rather than fetching
    every document at once. Returns how many summaries were added.
    """
    summariser = Summariser()
    if not summariser.available:
        log.warning("summaries skipped - %s", summariser.describe())
        return 0

    pending = [i for i in items if redo or not i.get("summary")]
    if not pending:
        log.info("summaries: nothing pending")
        return 0

    # Newest first, so the front page fills in before the archive.
    pending = store.sort_items(pending)[:limit]
    log.info("summaries: %s pending this run via %s (%s total without one)",
             len(pending), summariser.describe(),
             sum(1 for i in items if not i.get("summary")))

    added = 0
    for index, item in enumerate(pending, start=1):
        text = documents.fetch_text(item, fetcher)
        if not text:
            log.debug("no extractable text for %s", item["url"])
            continue

        summary = summariser.summarise(item, text)
        if not summary:
            continue

        item["summary"] = summary
        item["summary_model"] = "%s/%s" % (summariser.provider, summariser.model)
        item["summary_at"] = store.now_iso()
        added += 1
        log.info("  [%s/%s] %s", index, len(pending), item["title"][:70])

    log.info("summaries: %s added (%s model calls, %s failures)",
             added, summariser.calls, summariser.failures)
    return added


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    config.REQUEST_DELAY_SECONDS = args.delay

    fetcher = Fetcher(obey_robots=not args.ignore_robots)
    scraped, ok_sources, failed_sources = [], [], []

    if args.only in (None, "sebi"):
        items, ok, failed = sebi.scrape(fetcher)
        scraped.extend(items)
        ok_sources.extend(ok)
        failed_sources.extend(failed)

    if args.only in (None, "ifsca"):
        items, ok, failed = ifsca.scrape(fetcher)
        scraped.extend(items)
        ok_sources.extend(ok)
        failed_sources.extend(failed)

    log.info("%s raw items from %s requests (%s sources ok, %s failed)",
             len(scraped), fetcher.request_count, len(ok_sources), len(failed_sources))

    if not ok_sources:
        log.error("every source failed - not touching %s", args.output)
        return 1

    existing = store.load(args.output)
    items, stats = store.merge(existing, scraped)
    log.info("store: %(added)s new, %(updated)s updated, %(unchanged)s unchanged, "
             "%(total)s total", stats)

    if failed_sources:
        log.warning("sources that failed this run: %s", ", ".join(failed_sources))

    # Runs before the CI signal below: a run that adds nothing but summaries is
    # still a run worth committing.
    stats["summarised"] = 0
    if args.summarise:
        stats["summarised"] = summarise_pending(
            items, fetcher, args.summary_limit, redo=args.resummarise)
        if stats["summarised"]:
            stats["changed"] = True

    # CI uses this to decide whether to commit: the file is rewritten on every
    # run (last_seen moves), but only a substantive change is worth a commit.
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as fh:
            fh.write("changed=%s\n" % ("true" if stats["changed"] else "false"))
            fh.write("added=%s\n" % stats["added"])
            fh.write("total=%s\n" % stats["total"])
            fh.write("summarised=%s\n" % stats["summarised"])

    payload = store.build_payload(items, ok_sources, failed_sources)

    if args.dry_run:
        log.info("dry run - nothing written")
        for item in items[:10]:
            log.info("  %s  %-18s %-18s %s", item.get("date") or "????-??-??",
                     item["regulator"], item["doc_type"], item["title"][:80])
        return 0

    store.save(args.output, payload)

    if not args.no_site_copy:
        os.makedirs(os.path.dirname(SITE_COPY), exist_ok=True)
        shutil.copyfile(args.output, SITE_COPY)
        log.info("copied to %s for local preview", SITE_COPY)

    return 0


if __name__ == "__main__":
    sys.exit(main())
