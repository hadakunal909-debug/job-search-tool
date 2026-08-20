#!/usr/bin/env python3
"""Measure core.reads_like_pm against real stored postings, and sweep its two thresholds.

The rule decides whether a posting whose TITLE says nothing useful should still be kept, so how
it errs is the whole risk of description-based admission. This scores it against postings we
already hold, in three buckets:

    pos / recall    the title IS a delivery role (core.ROLE_FAMILIES 'deliver' group, less
                    `consultant`). The rule should fire. Every miss is a badly-titled job the
                    description path would fail to rescue -- this is the number to maximise.
    neg / tech%     the title is engineering or data. A weaker signal than it looks: an
                    engineering JD with an unmatched title is a job this feed WANTS, so a hit
                    here is mostly harmless. Watch it for drift, don't optimise it to zero.
    amb / amb-fire  neither of the above -- no role family, or one of the business/ops ones.
                    NOT an error rate, a PREVIEW: the closest thing in the corpus to the
                    postings the rule will really judge, since it only ever runs on titles
                    INCLUDE rejected. Read the samples rather than the percentage.

What must never get in is retail, clinical and trades, and EXCLUDE vetoes those before this
rule is consulted at all, which is why no bucket here measures them.

Read-only. Needs the proxy, as documented in docs/SESSION_HANDOFF_PROMPT.md:

    DB_PROXY_SECRET="$(tr -d '\\r\\n' < .db_proxy_secret)" \\
    DB_PROXY_URL="https://stemjobs1.astrochakra.co/api/db" DB_REQUIRE=proxy \\
    python scripts/calibrate_pm_rule.py --sample 300

IT SAMPLES ON PURPOSE. Reading the jd column for the whole corpus is ~128 MB and was the largest
item on the egress bill (db.py:504); 300 rows a side is ~3 MB and enough to separate a 5% false
positive rate from a 30% one. --sample is per side, not total.
"""
import argparse
import collections
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core
import db

# The 'deliver' group MINUS `consultant`. Implementation/solutions-consultant JDs are largely
# pre-sales and customer-onboarding work, so grading the rule as wrong for not calling them
# project management measures the wrong thing and understates recall.
DELIVER = {k for k, _lab, grp, _p in core.ROLE_FAMILIES
           if grp == "deliver" and k != "consultant"}
TECHNICAL = {k for k, _lab, grp, _p in core.ROLE_FAMILIES if grp in ("eng", "data")}


def classify(title):
    """'pos' | 'neg' | 'amb' -- see the module docstring for what each bucket is for."""
    fams = set(core.roles_for_title(title or ""))
    if fams & DELIVER:
        return "pos"
    if fams & TECHNICAL:
        return "neg"
    return "amb"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=250, help="rows per side (default 250)")
    ap.add_argument("--seed", type=int, default=20260820, help="so a re-run is comparable")
    ap.add_argument("--show", type=int, default=8, help="example errors to print per side")
    a = ap.parse_args()

    print("backend: %s" % db.backend_name())
    index = db.load_jobs(include_jd=False, cols="url,title,company") or []
    print("corpus : %d rows (titles only)" % len(index))

    buckets = collections.defaultdict(list)
    for r in index:
        if r.get("url"):
            buckets[classify(r.get("title"))].append(r)
    print("buckets : %d delivery-titled, %d technical-titled, %d no-role-family"
          % (len(buckets["pos"]), len(buckets["neg"]), len(buckets["amb"])))

    rng = random.Random(a.seed)
    picked = {}
    for side in ("pos", "neg", "amb"):
        pool = buckets[side]
        picked[side] = rng.sample(pool, min(a.sample, len(pool)))

    urls = [r["url"] for side in picked for r in picked[side]]
    print("fetching %d description(s)...\n" % len(urls))
    rows = {r.get("url"): r for r in db.load_jobs_by_urls(urls, include_jd=True)}

    # (anchors, support, side, title, company) for every row that actually had a usable JD.
    graded = []
    thin = collections.Counter()
    vetoed = collections.Counter()
    for side in ("pos", "neg", "amb"):
        for r in picked[side]:
            jd = (rows.get(r["url"], {}) or {}).get("jd") or ""
            if len(jd.strip()) < core._MIN_JD_CHARS:
                thin[side] += 1
                continue
            anc, sup, veto = core.pm_signal(jd)
            if veto >= core.PM_MAX_VETO:
                # Vetoed as a different function entirely (sales / marketing / design). Counted
                # separately: folding it into the sweep would make every threshold row look
                # better for a reason that has nothing to do with the thresholds.
                vetoed[side] += 1
                continue
            graded.append((anc, sup, side, r.get("title") or "", r.get("company") or ""))

    n = {s: sum(1 for g in graded if g[2] == s) for s in ("pos", "neg", "amb")}
    print("usable descriptions: %d delivery, %d technical, %d ambiguous"
          % (n["pos"], n["neg"], n["amb"]))
    print("skipped as thin/empty: %d, %d, %d" % (thin["pos"], thin["neg"], thin["amb"]))
    print("vetoed as another function: %d, %d, %d"
          % (vetoed["pos"], vetoed["neg"], vetoed["amb"]))
    if not n["pos"] or not n["neg"]:
        sys.exit("not enough usable descriptions on one side to calibrate")

    # --- the sweep. Both gates matter, so vary both. ---
    print("\n%-9s %-8s %8s %8s %8s %8s"
          % ("anchors", "points", "recall", "tech%", "amb-fire", "spread"))
    best = None
    for ma in (1, 2, 3, 4):
        for mp in (4, 6, 8, 10, 12, 14):
            if mp < core.PM_ANCHOR_WEIGHT * ma:
                continue                    # points gate unreachable-below-anchors: no-op
            hit = {"pos": 0, "neg": 0, "amb": 0}
            for anc, sup, side, _t, _c in graded:
                if anc >= ma and core.pm_points(anc, sup) >= mp:
                    hit[side] += 1
            recall = 100.0 * hit["pos"] / n["pos"]
            fp = 100.0 * hit["neg"] / n["neg"]
            amb = 100.0 * hit["amb"] / max(n["amb"], 1)
            mark = "  <-- shipped" if (ma == core.PM_MIN_ANCHORS
                                       and mp == core.PM_MIN_POINTS) else ""
            print("%-9d %-8d %7.1f%% %7.1f%% %7.1f%% %8.1f%s"
                  % (ma, mp, recall, fp, amb, recall - fp, mark))
            if best is None or (recall - fp) > best[0]:
                best = (recall - fp, ma, mp, recall, fp)
    print("\nbest spread: anchors>=%d points>=%d  (recall %.1f%%, false positives %.1f%%)"
          % (best[1], best[2], best[3], best[4]))

    # --- the errors, at the SHIPPED thresholds. The rates above do not show composition, and
    # composition is what says whether an error rate is acceptable: a false positive that is
    # genuinely a delivery role wearing an engineering title is not the same defect as a
    # hardware JD that happened to say "milestone" twice.
    def fires(anc, sup):
        return (anc >= core.PM_MIN_ANCHORS
                and core.pm_points(anc, sup) >= core.PM_MIN_POINTS)

    for label, side, want in (
            ("MISSED delivery roles", "pos", False),
            ("TECHNICAL titles the rule claims", "neg", True),
            ("AMBIGUOUS titles the rule claims -- READ THESE, they are the preview", "amb", True)):
        bad = [g for g in graded if g[2] == side and fires(g[0], g[1]) is want]
        print("\n%s: %d" % (label, len(bad)))
        for anc, sup, _s, title, company in sorted(
                bad, key=lambda g: -core.pm_points(g[0], g[1]))[:a.show]:
            print("   a=%-2d s=%-2d p=%-3d %-44s %s"
                  % (anc, sup, core.pm_points(anc, sup), title[:44], company[:24]))


if __name__ == "__main__":
    main()
