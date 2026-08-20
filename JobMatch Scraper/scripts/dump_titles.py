#!/usr/bin/env python3
"""Sweep boards and write EVERY posting's title with the filter's verdict on it. No database.

The keep rule drops ~213,000 postings a run (see the "Scanned N postings" line in any run log)
and keeps no record of WHICH, so until now every "why isn't this job in the feed?" question was
answered by hand -- that is how the Disney "Manager, Projects" case was found, and it is why
commit 027dff7 had to build a throwaway harness to measure three candidate fixes.

This is that harness, kept. It writes one TSV row per posting so a candidate keyword can be
scored offline in milliseconds, against real titles, instead of argued about:

    python scripts/dump_titles.py --per-ats 3 --out titles.tsv     # quick, ~1-3 min
    python scripts/dump_titles.py --all --out titles_full.tsv      # every board, ~20 min
    python scripts/dump_titles.py --ats workday,greenhouse --all --out wd.tsv

WHY IT DOES NOT GO THROUGH scraper.main(): main() ends in db.add_jobs(), so measuring with it
would write to production. This calls scrape_all() and judges the rows in memory, so it is
read-only by construction -- it opens no database connection at all.

THE US GATE IS APPLIED HERE, not left to the reader. In main() that gate only runs on titles
that already passed the keep rule, so a rejected row never gets one; a measurement that forgets
to re-apply it counts postings in Bangalore as wins. The `us` column is the answer, and
--us-only (the default) drops the rest, matching what a real run would have stored.
"""
import argparse
import collections
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import scraper


def pick_sources(per_ats, ats_filter, take_all):
    """Boards to sweep, stratified by ats_type so a sample is not 400 Greenhouse boards.

    Sorted for determinism: two runs of the same arguments must sweep the same boards, or a
    before/after comparison is measuring the sample rather than the change.
    """
    by_ats = collections.OrderedDict()
    for entry in scraper.SOURCES:
        url, ats = entry[0], entry[1]
        if ats_filter and ats not in ats_filter:
            continue
        by_ats.setdefault(ats, []).append(entry)
    out = []
    for ats, entries in by_ats.items():
        entries = sorted(entries, key=lambda e: e[0])
        out.extend(entries if take_all else entries[:per_ats])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="titles.tsv", help="TSV to write (default titles.tsv)")
    ap.add_argument("--per-ats", type=int, default=3,
                    help="boards per ats_type when not --all (default 3)")
    ap.add_argument("--all", action="store_true", help="every board in SOURCES")
    ap.add_argument("--ats", default="", help="comma-separated ats_types to restrict to")
    ap.add_argument("--workers", type=int, default=8, help="concurrent boards (default 8)")
    ap.add_argument("--budget-min", type=float, default=0,
                    help="stop starting boards after this many minutes (0 = no limit)")
    ap.add_argument("--keep-non-us", action="store_true",
                    help="also write postings the US gate would have dropped")
    a = ap.parse_args()

    ats_filter = {s.strip() for s in a.ats.split(",") if s.strip()}
    sources = pick_sources(a.per_ats, ats_filter, a.all)
    if not sources:
        sys.exit("no boards matched --ats %r" % a.ats)
    kinds = collections.Counter(e[1] for e in sources)
    print("sweeping %d board(s) across %d ats_type(s): %s"
          % (len(sources), len(kinds), ", ".join("%s %d" % kv for kv in kinds.most_common())))

    t0 = time.monotonic()
    rows = scraper.scrape_all(sources, workers=a.workers,
                              budget_min=a.budget_min or None)
    print("\nscraped %d posting(s) in %.1f min" % (len(rows), (time.monotonic() - t0) / 60.0))

    # Which board each row came from, so the dump can say "workday" rather than leaving the
    # reader to re-derive it from the URL. scrape_all stamps company but not ats_type.
    host_ats = {}
    for url, ats, _company in sources:
        host_ats[scraper.urlparse(url).netloc.lower()] = ats

    verdicts = collections.Counter()
    written = 0
    with open(a.out, "w", encoding="utf-8", newline="") as fh:
        fh.write("verdict\treason\tus\tats\ttitle\tcompany\tlocation\turl\n")
        for r in rows:
            title = r.get("title") or ""
            loc = r.get("location") or ""
            keep, why = scraper.title_verdict(title)
            us = (scraper.is_us_location(loc)
                  and not scraper.title_says_non_us(title))
            verdicts["%s / %s" % ("keep" if keep else "drop", "US" if us else "non-US")] += 1
            if not us and not a.keep_non_us:
                continue
            fh.write("\t".join(scraper._dump_field(v) for v in (
                "keep" if keep else "drop", why, "y" if us else "n",
                host_ats.get(scraper.urlparse(r.get("url") or "").netloc.lower(), "?"),
                title, r.get("company"), loc, r.get("url"))) + "\n")
            written += 1

    print("\nwrote %d row(s) to %s" % (written, a.out))
    for k, n in verdicts.most_common():
        print("   %-18s %7d" % (k, n))


if __name__ == "__main__":
    main()
