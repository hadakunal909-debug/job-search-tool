#!/usr/bin/env python3
"""Delete job rows older than N days.

Postings that have been up for a month are almost always filled, and on the Supabase free
tier every dead row costs space and slows the feed. This is the cleanup half of the freshness
policy; the other half is scraper.MAX_AGE_DAYS, which stops stale postings being saved in the
first place. Without that gate this script would just delete rows the next scrape re-adds.

Age is row_age_date: posted_verified > found_date > first_seen — the same precedence the feed
filters and sorts by, so "older than 30 days" means the same thing here as it does on screen.

    python scripts/prune_stale.py                 # dry run, 30 days
    python scripts/prune_stale.py --days 45       # dry run, 45 days
    python scripts/prune_stale.py --apply         # actually delete
    python scripts/prune_stale.py --apply --include-flagged   # also drop saved/applied/hidden
"""
import argparse
import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import db


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    ap.add_argument("--include-flagged", action="store_true",
                    help="also delete jobs you've liked / applied to / hidden")
    # Defaults to db.AGE_LONG_DAYS so a hand-run purge cannot quietly undo the exemption the
    # scrape's intake gate applied. --long-days 0 holds every source to --days.
    ap.add_argument("--long-days", type=int, default=db.AGE_LONG_DAYS,
                    help="window for long-lived boards (%s); 0 = same as --days"
                         % ", ".join(sorted(db.LONG_LIVED_HOSTS)))
    a = ap.parse_args()

    total = len(db.existing_urls())
    stale, cutoff = db.stale_urls(a.days, long_days=a.long_days)
    flagged = db.all_flagged_urls()
    protected = [u for u in stale if u in flagged]
    doomed = stale if a.include_flagged else [u for u in stale if u not in flagged]

    print("rows in table      : %d" % total)
    print("cutoff             : %s  (older than %d days)" % (cutoff, a.days))
    if a.long_days and a.long_days != a.days:
        print("long-lived cutoff  : %d days for %s"
              % (a.long_days, ", ".join(sorted(db.LONG_LIVED_HOSTS))))
    print("stale              : %d" % len(stale))
    print("  saved/applied/hidden among them: %d  (%s)"
          % (len(protected), "WILL BE DELETED" if a.include_flagged else "protected"))
    print("to delete          : %d" % len(doomed))
    print("would remain       : %d" % (total - len(doomed)))

    if doomed:
        rows = {r["url"]: r for r in db.load_jobs() if r.get("url")}
        by_co = collections.Counter((rows.get(u, {}).get("company") or "?") for u in doomed)
        print("\nbiggest contributors:")
        for c, n in by_co.most_common(10):
            print("   %-38s %6d" % (c[:38], n))
        print("\nsample:")
        for u in doomed[:5]:
            r = rows.get(u, {})
            print("   %-10s %-34s %s" % (db.row_age_date(r), (r.get("company") or "?")[:34],
                                         (r.get("title") or "")[:46]))

    if not a.apply:
        print("\nDRY RUN — nothing deleted. Re-run with --apply.")
        return

    if not doomed:
        print("\nnothing to do.")
        return

    def prog(done, tot):
        if done % 2000 == 0 or done == tot:
            print("   deleted %d/%d" % (done, tot), flush=True)

    print("\ndeleting...")
    n = db.delete_urls(doomed, progress=prog)
    left = len(db.existing_urls())
    print("\ndeleted %d row(s). table now holds %d (was %d)." % (n, left, total))


if __name__ == "__main__":
    main()
