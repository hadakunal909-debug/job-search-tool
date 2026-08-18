#!/usr/bin/env python3
"""build_score_calibration.py — rebuild the raw-score -> display-score curve.

WHY A CURVE EXISTS AT ALL. core.score_against answers "what share of THIS job description's
weighted keywords does the resume contain". That is a real number, but it is structurally
bounded: a full JD names far more terms than any one resume carries, so across 22,424 scored
rows nothing ever exceeded 69 and the mode sat at 30-39. A default floor of 45 therefore sat at
the 94th percentile -- it admitted 5.7% of the corpus, which is how a pipeline ingesting
300-1,800 rows a day showed 30-40 of them, and why moving the floor five points changed the
answer by a factor of three.

WHAT THIS WRITES. score_calibration.json: anchor points mapping raw coverage to the percentile
it occupies in the corpus, pinned to 0 at the bottom so a job sharing no keywords reads 0 rather
than "16% match". core.calibrate_score interpolates between them.

WHY THE ANCHORS ARE FROZEN rather than recomputed on every render: a live percentile keeps
exactly 30% of the feed above any given floor forever, so a genuinely good week and a genuinely
bad one look identical and the number can never say "there is nothing for you today". Rebuild
deliberately -- after a resume change, or a large shift in what the scraper collects.

    python scripts/build_score_calibration.py                 # read the corpus, write the file
    python scripts/build_score_calibration.py --dry-run       # print the curve, write nothing

Reads through db.py, so it works on all three transports (local Postgres, the HTTPS proxy, or
Supabase). Writes nothing to the database.
"""
import argparse
import bisect
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core
import db

# The raw values to place an anchor at, up to where the percentile stops being informative.
# Dense where the corpus is: 20-45 holds ~70% of it.
RAW_POINTS = [0, 5, 10, 15, 20, 25, 30, 35, 40, 45]

# Above raw 45 the percentile is already 94 and hits 100 by 55, so continuing it would map every
# strong match to the same number -- and the feed SORTS on this, so the best rows would arrive
# tied and unordered. Measured on a live account before this tail existed: the top 40 rows all
# read 100. Spread by hand instead, so near-perfect matches can still be ranked against each
# other. Raw 75 is above the highest score ever observed (74), which is what keeps 100 rare.
TAIL = [(50, 96), (55, 97), (60, 98), (65, 99), (75, 100)]


def raw_scores():
    """Every non-null match_score in the corpus."""
    rows = db._fetch_all(db.TABLE, {"select": "match_score", "order": "url"})
    return sorted(r["match_score"] for r in rows if r.get("match_score") is not None)


def curve(vals):
    """[(raw, display)] — display is the percentile `raw` occupies, 0 pinned to 0.

    Monotone by construction: bisect_right is non-decreasing in its argument, so the y values
    can only rise. core.load_score_anchors rejects a non-monotone file, and this is why it can
    afford to.
    """
    n = len(vals) or 1
    out = []
    for raw in RAW_POINTS:
        pct = 0.0 if raw <= 0 else 100.0 * bisect.bisect_right(vals, raw) / n
        out.append((raw, int(round(pct))))
    # Enforce strict non-decrease even if two adjacent points tie after rounding.
    for i in range(1, len(out)):
        if out[i][1] < out[i - 1][1]:
            out[i] = (out[i][0], out[i - 1][1])
    # Never let the measured part run above where the hand-spread tail starts, or the join
    # would be a step down and the curve would stop being monotone.
    ceiling = TAIL[0][1] - 1
    out = [(x, min(y, ceiling)) for x, y in out]
    return out + list(TAIL)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print the curve, write nothing")
    ap.add_argument("--out", default=core.SCORE_CALIBRATION_PATH)
    a = ap.parse_args()

    print("reading match_score from %s ..." % db.backend_name())
    vals = raw_scores()
    if len(vals) < 500:
        print("REFUSING: only %d scored rows. A curve built from that would be noise, and it "
              "would silently reshape every score in the feed." % len(vals))
        return 1
    pts = curve(vals)

    print("\n  %d scored rows, min %d, max %d\n" % (len(vals), vals[0], vals[-1]))
    print("  raw -> shown   share of corpus at or above")
    n = len(vals)
    for raw, shown in pts:
        above = 100.0 * (n - bisect.bisect_right(vals, raw)) / n
        print("  %4d -> %4d   %5.1f%%" % (raw, shown, above))

    floor = core.DEFAULT_PREFS["min"]
    need = next((r for r, s in pts if s >= floor), None)
    if need is not None:
        above = 100.0 * (n - bisect.bisect_right(vals, need)) / n
        print("\n  the default floor of %d admits roughly the top %.0f%% of the corpus "
              "(raw >= %d)" % (floor, above, need))

    if a.dry_run:
        print("\n  DRY RUN — nothing written.")
        return 0
    payload = {"built_from_rows": len(vals), "anchors": [[r, s] for r, s in pts],
               "note": "raw match_score -> displayed match %. See core.calibrate_score."}
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    print("\n  wrote %s" % a.out)
    print("  DEPLOY IT: the web app reads this file at boot, so a rebuilt curve that stays on "
          "this machine changes nothing live.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
