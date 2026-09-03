#!/usr/bin/env python3
"""Read every would-add board's OWN postings and report what a human needs to judge it.

STAGE 3.5, between probe and adopt. `adopt_everify_boards --dry-run` answers "is this a real
board for this employer". It does NOT answer the question the owner actually decides on: are
the surviving titles genuine HQ roles, or one store-level title repeated?

    python scripts/review_candidates.py --csv discovered_board_probe.csv
    python scripts/review_candidates.py --csv discovered_adoption.csv --out review.csv
    python scripts/review_candidates.py --csv discovered_adoption.csv --all   # incl. would-not-add

WRITES NOTHING but its own CSV, same guarantee as the rest of the chain.

WHY IT IS NOT adopt's YIELD CHECK. Two reasons, and both are a board that shipped:

  * `relevance_yield` is gated at YIELD_CHECK_MIN_POSTINGS = 500, so a smaller board is never
    sampled at all -- Drury Hotels kept 1 of 429 and no gate ever looked. This samples every
    board it is given, whatever its size.
  * `relevance_yield` auto-rejects only at ZERO survivors, which makes the verdict turn on
    luck. Domino's sampled 0 of 1,000 on 2026-08-31 and was auto-killed; the same board
    sampled 1 of 1,000 on 09-02 -- one row -- and graded would-add with 24,663 postings
    behind it.

CONCENTRATION BEATS RATIO, which is why `top_title_share` is here and why `kept_pct` alone is a
trap in both directions. EoS Fitness kept 7.6% and PDS Health 5.2% -- healthier ratios than
boards worth keeping -- but 99% and 95% of those survivors were ONE repeated title. Tapestry
kept 21 of 1,987 (1.1%) and every one was a distinct HQ role, so it was a keep.

AND `distinct_titles` CAN BE FOOLED BY A CITY SUFFIX. Quince reads 15 distinct because
"3PL Fulfillment Operations Manager - Dallas, TX" and "- Ontario, California" are different
strings. `titles` carries the head of the list for exactly this reason: read it, don't just
read the count.
"""
import argparse
import collections
import concurrent.futures
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import scraper
from scraper import find_everify_boards as feb

REPORT = "candidate_review.csv"

COLS = ["employer", "ats_type", "board_url", "h1b_filings", "job_count", "fetched", "kept",
        "kept_pct", "us_kept", "distinct_titles", "top_title_share", "top_title", "titles"]

# adopt writes "would-add" on a dry run and "yes" on a real one. Accept both: the whole point is
# to run this BEFORE adopting, but a post-hoc review of a batch already added is just as valid.
ADD_VERDICTS = ("yes", "would-add")


def judge(rec):
    """Fetch one board and attach the numbers a human judges it on."""
    fn = scraper.SCRAPERS.get(rec.get("ats_type") or "")
    rows = []
    if fn:
        try:
            rows = fn(rec["board_url"]) or []
        except Exception as e:
            rec["titles"] = "FETCH FAILED: %s" % type(e).__name__
    kept = [r for r in rows if scraper.title_verdict(r.get("title", ""))[0]]
    # The US gate is applied SEPARATELY from the title filter, never folded into it: a board
    # whose survivors are all real but all foreign is a different decision from one with no
    # survivors, and collapsing the two hides which is which.
    us = [r for r in kept if scraper.is_us_location(r.get("location", ""))]
    counts = collections.Counter((r.get("title") or "").strip() for r in kept)
    rec["fetched"] = len(rows)
    rec["kept"] = len(kept)
    rec["kept_pct"] = round(100.0 * len(kept) / len(rows), 1) if rows else ""
    rec["us_kept"] = len(us)
    rec["distinct_titles"] = len(counts)
    rec["top_title"], top_n = (counts.most_common(1)[0] if counts else ("", 0))
    rec["top_title_share"] = round(100.0 * top_n / len(kept), 1) if kept else ""
    rec.setdefault("titles", " | ".join(t for t, _ in counts.most_common(6)))
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True, help="a probe or adopt CSV: needs board_url + ats_type")
    ap.add_argument("--out", default=REPORT)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--all", action="store_true",
                    help="include rows adopt would NOT add (verifying an identity hold)")
    a = ap.parse_args()

    if not os.path.exists(a.csv):
        raise SystemExit("no such file: %s" % a.csv)
    with open(a.csv, encoding="utf-8-sig", newline="") as f:
        rows = [r for r in csv.DictReader(f)
                if (r.get("board_url") or "").strip()
                and (a.all or "added" not in r
                     or (r.get("added") or "").strip().lower() in ADD_VERDICTS)]
    if not rows:
        raise SystemExit("nothing to sample in %s (no board_url, or none would be added)" % a.csv)
    print("sampling %d board(s) with %d workers..." % (len(rows), a.workers), flush=True)

    orig = scraper.SESSION
    scraper.SESSION = feb._fast_session()
    try:
        with concurrent.futures.ThreadPoolExecutor(a.workers) as ex:
            done = list(ex.map(judge, rows))
    finally:
        scraper.SESSION = orig

    done.sort(key=lambda r: -(r["us_kept"] or 0))
    with open(a.out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(done)

    print("%-34s %6s %6s %6s %8s %7s  %s" %
          ("employer", "fetch", "kept", "us", "distinct", "top%", "sample titles"))
    for r in done:
        print("%-34.34s %6s %6s %6s %8s %7s  %.70s" %
              (r["employer"], r["fetched"], r["kept"], r["us_kept"], r["distinct_titles"],
               r["top_title_share"], r.get("titles", "")))
    zero = [r["employer"] for r in done if not r["us_kept"]]
    if zero:
        print("\ncontributing NOTHING today (%d): %s" % (len(zero), ", ".join(sorted(zero))))
    print("\nwrote %s" % a.out)


if __name__ == "__main__":
    main()
