#!/usr/bin/env python3
"""What does the DESCRIPTION path actually admit? Measured on raw board output. No database.

scripts/calibrate_pm_rule.py grades the rule against postings we already STORE, which means it
can only ever see titles the filter already accepted. That blind spot cost real precision: the
first two-tier rule looked fine there and, run against live boards, rescued Ramp's
"Account Manager | Commercial", "Senior Product Marketing Manager", "Channel Partner Manager"
and "Director, Product Design" -- sales, marketing and design, none of them the job. Sales and
marketing titles are not in the corpus to sample, so nothing stored could have revealed it.

This closes that hole. It sweeps the boards that hand over descriptions for free, replays the
real admission chain in main()'s order, and prints what the description path adds and WHAT IT
IS. Read the samples; the percentage on its own is what fooled the earlier pass.

    python scripts/measure_jd_admission.py --per-ats 6
    python scripts/measure_jd_admission.py --ats lever --all --show 40

Read-only by construction: like dump_titles.py it calls scrape_all() and judges rows in memory,
so it never reaches db.add_jobs.
"""
import argparse
import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core
import scraper

# The branches whose list payload carries the description (see _listing_jd in scraper).
FREE_TEXT_ATS = ("ashby", "lever", "jibe", "pinpoint")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ats", default=",".join(FREE_TEXT_ATS))
    ap.add_argument("--per-ats", type=int, default=6)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--show", type=int, default=25, help="rescued examples to print")
    a = ap.parse_args()

    want = {s.strip() for s in a.ats.split(",") if s.strip()}
    by = collections.defaultdict(list)
    for entry in scraper.SOURCES:
        if entry[1] in want:
            by[entry[1]].append(entry)
    srcs = []
    for ats in sorted(by):
        entries = sorted(by[ats], key=lambda e: e[0])
        srcs.extend(entries if a.all else entries[:a.per_ats])
    if not srcs:
        sys.exit("no boards matched --ats %r" % a.ats)
    print("sweeping %d board(s): %s\n"
          % (len(srcs), ", ".join("%s %d" % (k, len(v)) for k, v in sorted(by.items()))))

    rows = scraper.scrape_all(srcs, workers=a.workers)

    stats = collections.Counter()
    rescued, vetoed = [], []
    for r in rows:
        t = r.get("title") or ""
        # main()'s order: US gate is a veto on the title path, so apply it the same way here.
        if (not scraper.is_us_location(r.get("location") or "")
                or scraper.title_says_non_us(t)):
            stats["non-US"] += 1
            continue
        keep, why = scraper.title_verdict(t)
        if keep:
            stats["kept on title"] += 1
            continue
        if why.startswith("off-target"):
            stats["EXCLUDE veto"] += 1
            continue
        jd = (r.get("jd") or "").strip()
        if len(jd) < core._MIN_JD_CHARS:
            stats["no usable description"] += 1
            continue
        anc, sup, veto = core.pm_signal(jd)
        if core.reads_like_pm(jd):
            stats["RESCUED on description"] += 1
            rescued.append((anc, sup, veto, t, r.get("company")))
        elif veto >= core.PM_MAX_VETO and anc >= core.PM_MIN_ANCHORS:
            # Would have been admitted but for the veto tier. The most useful list in the file:
            # if these read like delivery roles, the veto list is too aggressive.
            stats["vetoed as another function"] += 1
            vetoed.append((anc, sup, veto, t, r.get("company")))
        else:
            stats["still dropped"] += 1

    print("")
    for k, v in stats.most_common():
        print("   %-28s %6d" % (k, v))
    base = stats["kept on title"]
    print("\n   description path adds %+.1f%% on top of the title filter"
          % (100.0 * stats["RESCUED on description"] / max(base, 1)))

    for label, items in (("RESCUED -- read these", rescued),
                         ("VETOED as sales/marketing/design", vetoed)):
        print("\n%s (%d):" % (label, len(items)))
        for anc, sup, veto, t, c in sorted(items, key=lambda x: -x[0])[:a.show]:
            print("   a=%-2d s=%-2d v=%-2d %-50s %s" % (anc, sup, veto, t[:50], (c or "")[:22]))


if __name__ == "__main__":
    main()
