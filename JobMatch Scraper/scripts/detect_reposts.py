"""
detect_reposts.py — which employers keep re-posting the same role?

    python scripts/detect_reposts.py                  # top 25 clusters
    python scripts/detect_reposts.py --top 60 --min-urls 3
    python scripts/detect_reposts.py --window 45 --company stripe
    python scripts/detect_reposts.py --json out.json
    python scripts/detect_reposts.py --write         # publish the map the feed badges from

READ-ONLY unless you pass --write, and even then the only write is one KV row holding the cluster
map. No posting is ever closed, hidden, deleted or re-scored by this file — see scraper/reposts.py
for why a repost is information about the EMPLOYER rather than a defect in the feed, and why acting
on it automatically would be wrong. The card says "posted N times" and the reader decides.

The clustering lives in scraper.reposts so it can be unit-tested without a database
(test_reposts.py). This file is only the query and the report.
"""

import argparse
import collections
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core
import db
from scraper import reposts

# The KV row the feed reads. Same pattern as close_dead_jds' jd_host_verdicts: a precomputed
# measurement in a KV blob, not a new column — DDL cannot go through the HMAC proxy, so a schema
# change means someone pasting SQL into a console, and this needs neither.
REPOST_KEY = "repost_clusters"


def _today():
    return datetime.date.today().isoformat()


def _rows():
    """Every stored posting, with only the columns the clustering reads.

    include_jd=False matters: descriptions are the bulk of the table and pulling them here would
    turn a cheap report into the kind of read that put us over the free-tier egress budget once
    already.
    """
    # `location` is NOT optional here, and leaving it out silently disabled the location half of
    # the cluster key: every row normalised to "" and Walmart's 106 store-level internships still
    # read as 106 reposts. The clustering has no way to tell an absent column from a blank field.
    return db.load_jobs(include_jd=False,
                        cols="url,title,company,location,first_seen,found_date,is_active") or []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=25, help="clusters to print (default 25)")
    ap.add_argument("--window", type=int, default=reposts.DEFAULT_WINDOW_DAYS,
                    help="days all sightings must fall within (default %d)"
                         % reposts.DEFAULT_WINDOW_DAYS)
    # 3, not the module's theoretical minimum of 2. Measured against the live 25,180-row corpus:
    #   min 2 -> 1,845 clusters / 4,815 postings (19% of the feed)
    #   min 3 ->   528 clusters / 2,181 postings (8.7%)
    #   min 5 ->   122 clusters /   863 postings (3.4%)
    # Two sightings is a coincidence often enough that 19% of the feed gets flagged, and a signal
    # that fires on a fifth of the corpus is not one anybody can act on. Three is where the list
    # becomes a list worth reading.
    ap.add_argument("--min-urls", type=int, default=3,
                    help="distinct URLs before a cluster counts (default 3)")
    ap.add_argument("--company", default="", help="substring filter on the employer name")
    ap.add_argument("--include-closed", action="store_true",
                    help="also cluster rows already marked is_active=false")
    ap.add_argument("--json", default="", help="write the full result to this file")
    ap.add_argument("--write", action="store_true",
                    help="publish the cluster map to the %s KV row so the feed can badge cards"
                         % REPOST_KEY)
    args = ap.parse_args()

    rows = _rows()
    print("read %d row(s) from the corpus" % len(rows))
    if not args.include_closed:
        # A closed row is a posting that ENDED, which is exactly what a repost cycle looks like
        # from the outside, so including them by default would inflate every count. Off by
        # default, available when the question is "how long has this been going on".
        before = len(rows)
        rows = [r for r in rows if r.get("is_active") is not False]
        print("  (dropped %d closed row(s); --include-closed to keep them)" % (before - len(rows)))
    if args.company:
        want = args.company.lower()
        rows = [r for r in rows if want in (r.get("company") or "").lower()]
        print("  (filtered to %d row(s) matching company=%r)" % (len(rows), args.company))

    clusters = reposts.find_reposts(rows, window_days=args.window, min_urls=args.min_urls)
    total_rows = sum(c["count"] for c in clusters)
    print("\n%d repost cluster(s), covering %d posting(s) — window %dd, min %d URL(s)\n"
          % (len(clusters), total_rows, args.window, args.min_urls))

    for c in clusters[:args.top]:
        span = "same day" if c["span_days"] == 0 else "%dd apart" % c["span_days"]
        print("%2dx  %-34s %-52s %s" % (c["count"], (c["company"] or "?")[:34],
                                        c["title"][:52], span))
        if len(c["titles"]) > 1:
            print("        also as: %s" % "; ".join(t[:60] for t in c["titles"][1:4]))
        if c["dates"]:
            print("        seen: %s" % ", ".join(c["dates"][:6])
                  + (" …" if len(c["dates"]) > 6 else ""))
        print("        %s" % c["urls"][0][:110])

    if clusters[args.top:]:
        print("\n… %d more (use --top)" % len(clusters[args.top:]))

    # Which employers do it most. This is the number worth acting on: one repeatedly-reposted role
    # is a coincidence, an employer with fifteen of them is a pattern.
    worst = collections.Counter()
    for c in clusters:
        worst[c["company"] or "?"] += 1
    if worst:
        print("\nemployers with the most repost clusters:")
        for name, n in worst.most_common(12):
            print("  %-40s %d" % (name[:40], n))
        # Staffing agencies dominate this list by construction — Actalent alone held 167 of the 528
        # clusters at min-urls=3 — because re-advertising the same role for different clients IS
        # their product. Worth knowing, but not the same finding as a direct employer sitting on a
        # req nobody fills.
        print("  (staffing agencies re-advertise by design — read them differently from a direct"
              " employer)")

    # Aggregator relists are a DIFFERENT problem (fingerprint_duplicate's), so say when a cluster
    # is one, rather than quietly counting it as employer behaviour.
    agg = [c for c in clusters if any(core.is_aggregator_url(u) for u in c["urls"])]
    if agg:
        print("\nnote: %d cluster(s) include an aggregator URL — those may be relists rather than"
              "\n      the employer re-posting. See scraper.fingerprint_duplicate." % len(agg))

    if args.write:
        # Only clusters at or above the threshold are published, so the badge and this report
        # always agree about what counts as a repost.
        cmap = reposts.cluster_map(clusters)
        db.put_kv(REPOST_KEY, {"clusters": cmap, "built": _today(),
                               "window_days": args.window, "min_urls": args.min_urls,
                               "rows_scanned": len(rows)})
        print("\npublished %d cluster key(s) to the %s KV row — the feed badges from this."
              % (len(cmap), REPOST_KEY))
        print("workers cache it for their lifetime, so a running app picks it up on next restart.")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(clusters, fh, indent=1, ensure_ascii=False)
        print("\nwrote %s (%d cluster(s))" % (args.json, len(clusters)))


if __name__ == "__main__":
    main()
