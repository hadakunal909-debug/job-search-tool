#!/usr/bin/env python3
"""Are the dates we never check actually right?

verify_dates only queues a row when nobody stated its date — a bare YYYY-MM-DD is trusted on
sight and never questioned. That is ~9,800 rows, half the corpus, and the trust is inherited
rather than earned:

  * Greenhouse falls back to `updated_at` when `first_published` is absent — a MODIFICATION
    date, which moves every time the employer edits the posting.
  * Oracle, iCIMS, Ashby, SmartRecruiters, Lever, JobDiva each hand us a different field and
    nothing has ever compared them against an independent reading.

This samples those rows per source, asks the same lookup service verify_dates uses, and reports
how far apart the two are. It answers "which sources can we trust", which is a prerequisite for
deciding whether the feed's Confirmed-posting-date filter is telling the truth.

    python scripts/audit_dates.py                       # 10 per source, ~9 sources
    python scripts/audit_dates.py --per-source 25
    python scripts/audit_dates.py --sources greenhouse,lever
    python scripts/audit_dates.py --list                # what's out there, no API calls

READ-ONLY. There is deliberately no --apply: this measures, it never rewrites a date. Acting on
what it finds is a separate, deliberate change, because "the service disagrees" is not the same
as "the service is right" — see the caveat printed with the results.

It reuses verify_dates' own dispatch gate and client, so it obeys the same ~54 req/min ceiling
and cannot get the shared IP rate-limited by racing the real verifier.
"""
import argparse
import collections
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import db
from scraper import verify_dates as vd

# Host fragment -> the field that host's scraper reads, so the report names the SUSPECT rather
# than just the domain. The comment is what makes a disagreement actionable.
SOURCES = [
    ("greenhouse", "greenhouse.io",       "first_published, FALLING BACK TO updated_at"),
    ("oracle",     "oraclecloud.com",     "PostedDate"),
    ("icims",      "icims.com",           "posted_date / create_date"),
    ("ashby",      "ashbyhq.com",         "publishedAt"),
    ("smartrec",   "smartrecruiters.com", "releasedDate"),
    ("lever",      "lever.co",            "createdAt (epoch ms)"),
    ("jobdiva",    "jobdiva.com",         "postDate (epoch ms)"),
    ("amazon",     "amazon.jobs",         "posted_date — newly trusted, so worth confirming"),
]


def classify(url):
    for name, frag, _ in SOURCES:
        if frag in (url or ""):
            return name
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-source", type=int, default=10, help="rows to sample per source")
    ap.add_argument("--sources", default="", help="comma-separated subset (default: all)")
    ap.add_argument("--list", action="store_true", help="show the population and exit")
    ap.add_argument("--max-calls", type=int, default=300, help="hard ceiling on API calls")
    a = ap.parse_args()

    want = {s.strip() for s in a.sources.split(",") if s.strip()}
    rows = db.load_jobs(include_jd=False)
    # The blind-trusted population: a bare ISO date, never queued, never confirmed.
    blind = [r for r in rows
             if vd._is_clean_api_date(r.get("found_date")) and not r.get("posted_verified")]
    pool = collections.defaultdict(list)
    for r in blind:
        s = classify(r.get("url"))
        if s and (not want or s in want):
            pool[s].append(r)

    print("corpus %d rows | never-questioned bare-ISO dates: %d (%.1f%%)"
          % (len(rows), len(blind), 100.0 * len(blind) / max(len(rows), 1)))
    print("\n%-10s %7s  %s" % ("source", "rows", "date field we take on trust"))
    print("-" * 78)
    for name, _frag, field in SOURCES:
        if name in pool:
            print("%-10s %7d  %s" % (name, len(pool[name]), field))
    other = len(blind) - sum(len(v) for v in pool.values())
    if other and not want:
        print("%-10s %7d  (hosts with no rule here)" % ("other", other))
    if a.list:
        return

    picks = []
    for name in sorted(pool):
        rs = sorted(pool[name], key=lambda r: r.get("found_date") or "", reverse=True)
        step = max(1, len(rs) // max(a.per_source, 1))       # spread across the date range
        picks += [(name, r) for r in rs[::step][:a.per_source]]
    if len(picks) > a.max_calls:
        picks = picks[:a.max_calls]
    if not picks:
        print("\nnothing to sample.")
        return

    mins = len(picks) * vd.SPACING / 60.0
    print("\nsampling %d rows across %d sources — ~%.1f min at the service's ~54/min ceiling"
          % (len(picks), len(set(n for n, _ in picks)), mins))
    print("(reusing verify_dates' dispatch gate, so this cannot outrun the real verifier)\n")

    res = collections.defaultdict(list)
    unanswered = collections.Counter()
    t0 = time.time()
    for i, (name, r) in enumerate(picks, 1):
        vd._gate()
        date, conf, note = vd.check(r["url"])
        stored = (r.get("found_date") or "")[:10]
        if not date or conf not in vd.MIN_CONFIDENCE:
            unanswered[name] += 1
        else:
            try:
                import datetime
                gap = (datetime.date.fromisoformat(date)
                       - datetime.date.fromisoformat(stored)).days
            except Exception:
                continue
            res[name].append((gap, stored, date, r))
        if i % 25 == 0:
            print("   %d/%d (%.1f min)" % (i, len(picks), (time.time() - t0) / 60.0))

    print("\n" + "=" * 78)
    print("HOW FAR OFF IS EACH SOURCE?")
    print("=" * 78)
    print("gap = service date minus stored date, in days. Negative means the stored date is")
    print("NEWER than the truth, i.e. the posting is older than the feed claims.\n")
    print("%-10s %6s %7s %8s %8s %8s   %s"
          % ("source", "n", "exact", "within3", "median", "worst", "no answer"))
    print("-" * 78)
    for name in sorted(res):
        g = [x[0] for x in res[name]]
        if not g:
            continue
        exact = sum(1 for x in g if x == 0)
        near = sum(1 for x in g if abs(x) <= 3)
        worst = max(g, key=abs)
        print("%-10s %6d %6d%% %7d%% %8s %8s   %d"
              % (name, len(g), 100 * exact // len(g), 100 * near // len(g),
                 "%+d" % int(statistics.median(g)), "%+d" % worst, unanswered[name]))
    for name in sorted(unanswered):
        if name not in res:
            print("%-10s %6s %6s %7s %8s %8s   %d"
                  % (name, "-", "-", "-", "-", "-", unanswered[name]))

    print("\n" + "=" * 78)
    print("THE WORST DISAGREEMENTS")
    print("=" * 78)
    flat = sorted((x for v in res.values() for x in v), key=lambda x: -abs(x[0]))
    for gap, stored, date, r in flat[:15]:
        print("  %+5d  %-9s stored %s -> service %s  %-28s %s"
              % (gap, classify(r["url"]), stored, date,
                 (r.get("company") or "?")[:28], (r.get("title") or "")[:34]))

    print("\n" + "=" * 78)
    print("READING THIS")
    print("=" * 78)
    print("A high 'exact' means that source's date field is genuinely the posting date and the")
    print("blind trust is earned. A wide median, or a consistent sign, means it is measuring")
    print("something else — a modification date drifts LATER, an aggregator ingest date drifts")
    print("EARLIER than the employer's own publication.")
    print()
    print("CAVEAT before acting: the service reads the live ATS page, so for a source that")
    print("renders its date on that page the two are not independent, and agreement proves")
    print("less than it looks. Disagreement is the informative direction here.")
    print()
    print("Nothing was written. This script has no --apply.")


if __name__ == "__main__":
    main()
