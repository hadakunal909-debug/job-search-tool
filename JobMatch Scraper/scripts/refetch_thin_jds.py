#!/usr/bin/env python3
"""refetch_thin_jds.py — repair job rows whose stored JD is junk rather than absent.

audit_jd_coverage.py only looks at rows where `jd` is EMPTY. It cannot see the other failure:
a row that HAS a description which is really a JavaScript loading shell — "Loading … Sorry to
interrupt CSS Error Refresh" (Salesforce Lightning), "You need to enable JavaScript to run this
app." (JobDiva), or just the page title and nav. Those rows score 0 and read as blank in the
feed, and because score_jobs is incremental — it queues a fetch only when `jd` is empty — a junk
value is STICKY. Nothing ever tries again.

Measured 2026-08-17: 2,154 rows (9.8% of the corpus) were in that state, and every one scored 0.

This is deliberately NOT a change to score_jobs' queue. Treating every thin JD as missing would
make each run spend its whole fetch budget retrying the ~2,100 rows whose host genuinely cannot
be read server-side, which is the trap audit_jd_coverage.py's BLOCKED class exists to prevent.
Run this instead, per host, once an extractor for that host actually works.

    python scripts/refetch_thin_jds.py                        # dry run, all hosts
    python scripts/refetch_thin_jds.py --host michaelpage.com
    python scripts/refetch_thin_jds.py --host michaelpage.com --apply
    python scripts/refetch_thin_jds.py --under 400 --min-gain 3
"""
import os
import sys
import argparse
import collections
import concurrent.futures
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import db
from scraper.score_jobs import detail_jd

# Below this many characters a stored JD is assumed to be a shell rather than a description.
# 400 is comfortably under the shortest real JD seen (Michael Page's genuine ones run 1,500+)
# and comfortably over the longest shell (Salesforce's is ~46, JobDiva's ~63).
THIN_UNDER = 400


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", action="append", default=[],
                    help="only this host suffix (repeatable)")
    ap.add_argument("--under", type=int, default=THIN_UNDER)
    ap.add_argument("--min-gain", type=float, default=3.0,
                    help="only write when the new JD is this many times longer")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    rows = db.load_jobs(["url", "jd", "company", "title"])
    thin = [r for r in rows
            if (r.get("jd") or "").strip() and len(r["jd"]) < a.under and r.get("url")]
    if a.host:
        thin = [r for r in thin
                if any(urlparse(r["url"]).netloc.endswith(h) for h in a.host)]
    if a.limit:
        thin = thin[:a.limit]

    by_host = collections.Counter(urlparse(r["url"]).netloc for r in thin)
    print("rows in table   : %d" % len(rows))
    print("thin (<%d chars): %d" % (a.under, len(thin)))
    for h, n in by_host.most_common(12):
        print("   %-40s %d" % (h, n))
    if not thin:
        return 0

    def work(r):
        try:
            return r, (detail_jd(r["url"])[1] or "")
        except Exception:
            return r, ""

    fixed, still = {}, collections.Counter()
    print("\nre-fetching %d row(s) with %d workers..." % (len(thin), a.workers), flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=a.workers) as ex:
        for i, (r, jd) in enumerate(ex.map(work, thin), 1):
            old = len(r["jd"])
            # A gain multiple, not an absolute length: it is what distinguishes "the extractor
            # now works" from "we re-read the same shell and it is 46 characters again".
            if jd and len(jd) >= old * a.min_gain and len(jd) >= 400:
                fixed[r["url"]] = jd
            else:
                still[urlparse(r["url"]).netloc] += 1
            if i % 200 == 0:
                print("   ... %d/%d" % (i, len(thin)), flush=True)

    print("\nrecovered  : %d" % len(fixed))
    print("still thin : %d" % sum(still.values()))
    for h, n in still.most_common(10):
        print("   %-40s %d  (host needs an extractor)" % (h, n))
    if fixed:
        avg = sum(len(v) for v in fixed.values()) // len(fixed)
        print("\naverage recovered length: %d chars" % avg)
        for u, jd in list(fixed.items())[:3]:
            print("   %s\n      %s..." % (u[-58:], jd[:100]))

    if not a.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return 0
    if fixed:
        db.update_jds(fixed)
        print("\nwrote %d JD(s)." % len(fixed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
