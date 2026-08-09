#!/usr/bin/env python3
"""Delete job rows the CURRENT title filter would no longer accept.

scraper.MAX_AGE_DAYS + prune_stale.py handle jobs that got OLD. This handles jobs that were
never on target: every time INCLUDE/EXCLUDE is tightened, the rows the old filter let in stay
in the table until they happen to age out, so the feed keeps showing roles the scraper would
now refuse. This is the cleanup half of a filter change, exactly as prune_stale.py is the
cleanup half of the freshness policy.

    python scripts/prune_offtarget.py                 # dry run
    python scripts/prune_offtarget.py --apply         # actually delete
    python scripts/prune_offtarget.py --include-flagged --apply   # also drop saved/applied/hidden

The filter is rebuilt the way scraper.main() does it — base INCLUDE **plus resume_terms()** —
because that is what actually ran when these rows were admitted. Replaying with the base list
alone over-reports badly: it blamed a keyword change for 924 rows when the true figure was 450,
and claimed 57 good rows would be lost when the real answer was 1.

Rows you have liked, applied to or hidden are protected unless --include-flagged, same contract
as prune_stale.py and the admin delete: never silently bin something a user is tracking.
"""
import argparse
import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import db
import scraper


def _score(row):
    s = str(row.get("match_score") or "")
    return int(s) if s.isdigit() else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    ap.add_argument("--include-flagged", action="store_true",
                    help="also delete jobs you've liked / applied to / hidden")
    ap.add_argument("--min-score", type=int, default=45,
                    help="report how many doomed rows score at/above this (default 45, the "
                         "feed's default match floor) — the number that decides if a filter "
                         "change is a good trade")
    a = ap.parse_args()

    extra = scraper.resume_terms()
    inc = scraper._make_matcher(tuple(scraper.INCLUDE) + tuple(extra))
    exc = scraper._EXCLUDE_RE

    rows = db.load_jobs(include_jd=False)
    total = len(rows)
    doomed_rows, reasons = [], collections.Counter()
    for r in rows:
        t = r.get("title") or ""
        if not r.get("url"):
            continue
        hit = exc.search(t)
        if hit:
            doomed_rows.append(r)
            reasons["excluded: " + hit.group(0).lower()] += 1
        elif not inc.search(t):
            doomed_rows.append(r)
            reasons["no INCLUDE match"] += 1

    flagged = db.all_flagged_urls()
    protected = [r for r in doomed_rows if r["url"] in flagged]
    keep_flagged = not a.include_flagged
    doomed = [r for r in doomed_rows if not (keep_flagged and r["url"] in flagged)]
    urls = [r["url"] for r in doomed]

    print("rows in table        : %d" % total)
    print("résumé-driven terms  : %d (folded into the filter, as main() does)" % len(extra))
    print("off-target           : %d (%.1f%%)" % (len(doomed_rows), 100.0 * len(doomed_rows) / max(total, 1)))
    print("  saved/applied/hidden among them: %d  (%s)"
          % (len(protected), "WILL BE DELETED" if a.include_flagged else "protected"))
    print("to delete            : %d" % len(urls))
    print("would remain         : %d" % (total - len(urls)))

    high = [r for r in doomed if _score(r) >= a.min_score]
    print("\nof those, scoring >=%d : %d   <-- if this is not tiny, STOP and re-check the filter"
          % (a.min_score, len(high)))
    for r in sorted(high, key=lambda x: -_score(x))[:10]:
        print("   %3d  %-44s %s" % (_score(r), (r.get("title") or "")[:44], r.get("company")))

    if reasons:
        print("\nwhy they fail:")
        for why, n in reasons.most_common(12):
            print("   %-34s %6d" % (why, n))
    if doomed:
        print("\nbiggest contributors:")
        for c, n in collections.Counter((r.get("company") or "?") for r in doomed).most_common(10):
            print("   %-38s %6d" % (c[:38], n))
        print("\nsample:")
        for r in doomed[:6]:
            print("   %3d  %-32s %s" % (_score(r), (r.get("company") or "?")[:32],
                                        (r.get("title") or "")[:46]))

    if not a.apply:
        print("\nDRY RUN — nothing deleted. Re-run with --apply.")
        return
    if not urls:
        print("\nnothing to do.")
        return

    def prog(done, tot):
        if done % 500 == 0 or done == tot:
            print("   deleted %d/%d" % (done, tot), flush=True)

    print("\ndeleting...")
    n = db.delete_urls(urls, progress=prog)
    left = db.table_count(db.TABLE)
    print("\ndeleted %d row(s). table now holds %s (was %d)." % (n, left, total))


if __name__ == "__main__":
    main()
