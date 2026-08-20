#!/usr/bin/env python3
"""The card's three 2026-08-11 changes, exercised against the REAL app.js source.

  1. ONE hedged sponsorship chip instead of up to three plus a "+2 more".
  2. The match meter as a traffic light: green at 70 and up, amber 40 to 69, red below 40.
  3. Hours in the relative date, but ONLY where the row carries a clock.

Every function under test is a closure inside app.js's IIFE, so they are lifted out by source
text the way scripts/feed_parity.py and scripts/test_filter_memory.py already do. Lifting rather
than re-typing is the point: a copy here would be a second implementation free to drift from
the one that ships.

    python scripts/test_card_meta.py        (needs node on PATH)

The two cases worth the file's existence are the ones that would fail SILENTLY rather than
loudly. The "New" badge used to be decided by comparing relTime()'s output to the literal string
"Today", which stops matching the moment relTime can answer "3h ago" — no error, the badge just
disappears from every fresh card. And a clock-bearing date is the only shape that may show
hours; if a bare posting date ever did, the product would be inventing a time of day that does
not exist anywhere in the corpus.
"""
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from feed_parity import js_function, js_json      # reuse the lifters, don't re-type them

import core

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(APP, "static", "app.js"), encoding="utf-8").read()

FAILS = []


def check(name, cond, extra=""):
    if not cond:
        FAILS.append(name)
    print("  %s %-58s %s" % ("ok " if cond else "FAIL", name, extra))


# ----------------------------------------------------------------------------------
# 1. The chip. Key chosen server-side by core.sponsor_likely, label looked up in app.js.
# ----------------------------------------------------------------------------------
print("\nONE HEDGED CHIP")

LABELS = js_json(SRC, "SPONSOR_LIKELY_LABELS")
check("app.js SPONSOR_LIKELY_LABELS == core's", LABELS == dict(core.SPONSOR_LIKELY_LABELS),
      repr(LABELS))

# First Solar is the reported case: h1b | green_card | h1b1 read as three chips.
CHIP_CASES = [
    (("h1b", "green_card", "h1b1"), "h1b", "First Solar, the reported case"),
    (("green_card",), "sponsor", "PERM only"),
    (("e3",), "sponsor", "E-3 only, nationality gated"),
    (("h1b1",), "sponsor", "H-1B1 only, nationality gated"),
    (("stem_opt",), "stem_opt", "E-Verify only"),
    (("green_card", "stem_opt"), "sponsor", "a filing outranks mere enrolment"),
    (("h1b", "stem_opt"), "h1b", "h1b outranks everything"),
    ((), "", "no record means no chip"),
]
for tags, want, why in CHIP_CASES:
    got = core.sponsor_likely(tags)
    check("sponsor_likely(%s)" % ",".join(tags or ("-",)), got == want, "%-9r %s" % (got, why))

for key in core.SPONSOR_LIKELY_LABELS:
    check("label for %r ends in 'Likely'" % key,
          core.SPONSOR_LIKELY_LABELS[key].endswith("Likely"),
          core.SPONSOR_LIKELY_LABELS[key])

# The old three-chip machinery must be GONE, not merely unreachable. A dead rankVisa() next to
# chips that are no longer ranked is worse than no comment at all.
check("rankVisa() deleted", "function rankVisa" not in SRC)
check("_wantVisa cache deleted", "_wantVisa = visaWanted()" not in SRC)
check('"+N more" chip deleted', "vt-more" not in SRC)
check("cardHTML reads j.visa_likely", "j.visa_likely" in SRC)
# The FULL list must survive: the visa filter narrows on it, and narrowing on the single chip
# would hide every green-card employer whose chip reads H-1B.
check("visaHit still filters on the full j.visa list",
      "var have = j.visa || []" in js_function(SRC, "visaHit"))

# ----------------------------------------------------------------------------------
# 2 and 3. The meter and the dates, run for real under node.
# ----------------------------------------------------------------------------------
TODAY = datetime.date.today()


def iso(days):
    return (TODAY - datetime.timedelta(days=days)).isoformat()


def stamp(hours):
    """A clock-bearing value `hours` in the past, in the UTC that app.js assumes."""
    t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
    return t.strftime("%Y-%m-%d %H:%M")


# Deliberately includes a FUTURE stamp: these are written by whichever machine ran the scrape,
# with a naive datetime.now(), so a row really can sit ahead of the reader's clock.
DATE_CASES = [
    (stamp(0.1), "Just now", "clock, minutes old"),
    (stamp(3), "3h ago", "clock, hours old"),
    (stamp(23), "23h ago", "clock, just inside a day"),
    (stamp(30), "Yesterday", "clock, past a day, falls back to days"),
    (stamp(-2), "Just now", "clock in the FUTURE clamps, never 'in 2h'"),
    (iso(0), "Today", "bare date stays a day, hours are not invented"),
    (iso(1), "Yesterday", "bare date"),
    (iso(3), "3d ago", "bare date"),
    (iso(14), "2w ago", "bare date"),
    ("", "", "empty"),
]
NEW_CASES = [
    (stamp(3), True, "clock, today"),
    (stamp(30), False, "clock, yesterday"),
    (iso(0), True, "bare date, today"),
    (iso(1), False, "bare date, yesterday"),
    ("", False, "no date at all"),
]
RING_CASES = [(100, "ring-strong"), (70, "ring-strong"), (69, "ring-good"),
              (45, "ring-good"), (40, "ring-good"), (39, "ring-low"), (0, "ring-low")]

# 4. The repost badge. Both numbers here are product decisions that would otherwise live only in a
# comment: the floor of 3 (measured — badging at 2 flags a fifth of the feed) and the agency
# suppression. (count, agency, expected text or None, why).
# Expectations are the RAW html the branch emits, so "&times;" not "×" — the browser renders it as
# × and that is what the user reads, but this test compares strings.
REPOST_CASES = [
    (5, False, "Posted 5&times;", "a real repost badges"),
    (3, False, "Posted 3&times;", "3 is the floor, matching detect_reposts' --min-urls default"),
    (2, False, None, "2 is a coincidence often enough that badging it flags 19% of the feed"),
    (0, False, None, "the overwhelming majority of rows"),
    (None, False, None, "field absent on a row cached before the feature -> no badge, no crash"),
    (9, True, None, "an AGENCY re-advertises by design; the Agency chip already says that"),
]

DRIVER = """
%(fns)s
const out = {rel: [], isnew: [], ring: [], pill: null};
for (const s of %(dates)s) out.rel.push(relTime(s));
// The badge asks daysAgo() for a NUMBER. This mirrors cardHTML's own expression.
for (const s of %(news)s) { const d = daysAgo(s); out.isnew.push(d !== null && d <= 0); }
for (const n of %(scores)s) {
  const m = /score-ring (ring-[a-z]+)/.exec(scoreRing(n));
  out.ring.push(m ? m[1] : null);
}
out.pill = scoreRing(72).indexOf('fill="var(--match-pill)"') >= 0;
out.weight800 = scoreRing(72).indexOf('font-weight="800"') >= 0;
out.repost = [];
for (const c of %(reposts)s) out.repost.push(REPOST_BADGE({repost: c[0], agency: c[1]}));
process.stdout.write(JSON.stringify(out));
"""

# cardHTML is 90 lines and needs a whole card's worth of data, so rather than lift it and stub its
# world, lift JUST its badge branch — condition and template — out of app.js by text. A change to
# either the threshold or the agency guard then fails here instead of shipping silently.
_REPOST_RE = re.compile(
    r"if \(j\.repost > 2 && !j\.agency\)\s*\n\s*badges \+= (.*?);\s*\n\s*if \(j\.closed\)", re.S)


def repost_shim():
    m = _REPOST_RE.search(SRC)
    if not m:
        raise SystemExit("could not find cardHTML's repost badge branch in app.js — did it move? "
                         "This shim is text-lifted on purpose; update the regex, do not retype "
                         "the badge.")
    return ("function H(x){return String(x);}\n"
            "function REPOST_BADGE(j){ if (j.repost > 2 && !j.agency) return %s; return null; }"
            % m.group(1))


def run_js():
    fns = "\n".join(js_function(SRC, f) for f in
                    ("parseRowDate", "hasClock", "daysAgo", "relTime", "scoreRing"))
    src = DRIVER % {
        "fns": fns + "\n" + repost_shim(),
        "dates": json.dumps([c[0] for c in DATE_CASES]),
        "news": json.dumps([c[0] for c in NEW_CASES]),
        "scores": json.dumps([c[0] for c in RING_CASES]),
        "reposts": json.dumps([[c[0], c[1]] for c in REPOST_CASES]),
    }
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False,
                                     encoding="utf-8", dir=APP) as fh:
        fh.write(src)
        path = fh.name
    try:
        p = subprocess.run(["node", path], capture_output=True, text=True, encoding="utf-8")
        if p.returncode != 0:
            raise SystemExit("node failed:\n" + (p.stderr or "")[:1500])
        return json.loads(p.stdout)
    finally:
        os.unlink(path)


got = run_js()

print("\nTHE MATCH METER, green >= 70, amber 40 to 69, red < 40")
for (score, want), g in zip(RING_CASES, got["ring"]):
    check("scoreRing(%d)" % score, g == want, "%-12s" % g)
check("the arc sits on an opaque pill", got["pill"],
      "otherwise a green ring vanishes into the green STEM-OPT card wash")
check("font-weight 800 is gone", not got["weight800"], "the type scale tops out at bold")

print("\nRELATIVE DATES, hours only where the row carries a clock")
for (s, want, why), g in zip(DATE_CASES, got["rel"]):
    check("relTime(%r)" % s, g == want, "%-11r %s" % (g, why))

print("\nTHE 'NEW' BADGE, decided by a day count and not by a string")
for (s, want, why), g in zip(NEW_CASES, got["isnew"]):
    check("New for %r" % s, g == want, "%-6r %s" % (g, why))
check("cardHTML no longer tests relTime() against \"Today\"",
      'relTime(j.date) === "Today"' not in SRC,
      "a string equality standing in for a date computation")

# A bare posting date must NEVER produce an hour count. Hours exist in this corpus only on the
# derived "YYYY-MM-DD HH:MM" shape; on a stated date they would be fabricated.
bare_rel = [g for (s, _w, _y), g in zip(DATE_CASES, got["rel"])
            if s and not re.match(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}", s)]
check("no bare date ever renders hours", not any("h ago" in g or g == "Just now" for g in bare_rel),
      repr(bare_rel))

print("\nTHE REPOST BADGE, a count and not a verdict")
for (n, agency, want, why), g in zip(REPOST_CASES, got["repost"]):
    label = "repost=%-4s agency=%-5s" % (n, agency)
    if want is None:
        check(label + " -> no badge", g is None, why)
    else:
        check(label + " -> %r" % want, g is not None and want in g, why)
# The wording matters as much as the threshold: we can prove the count, not the motive.
badge = next((g for g in got["repost"] if g), "")
check("the badge states a count, not a judgement",
      "ghost" not in badge.lower() and "fake" not in badge.lower(),
      "we can prove N postings; we cannot prove why")
check("the tooltip says where the number comes from",
      "different URLs" in badge and "90 days" in badge, "otherwise 'Posted 5x' is unfalsifiable")
check("the threshold matches detect_reposts' published default",
      "j.repost > 2" in SRC,
      "the badge and scripts/detect_reposts.py must agree on what a repost IS")

print("\nTHE 'MATCHED ON DESCRIPTION' CHIP, added 2026-08-20")
# Asserted against the SOURCE rather than through the shim, because this branch is a plain
# `if (j.jd_admit)` with no arithmetic in it — there is nothing to evaluate, only wording and
# placement to hold still. Placement is the part that bit once already: this chip originally
# sat between the repost branch and `if (j.closed)`, which is exactly the span REPOST_RE above
# anchors on, and the lifted shim silently absorbed it and still passed.
check("the chip exists and reads as provenance",
      "j.jd_admit" in SRC and "matched on description" in SRC,
      "the whole point of the wider net is that you can see which rule admitted a row")
check("its tooltip explains the rule rather than asserting quality",
      "description reads like" in SRC and "matched none of our role" in SRC,
      "'matched on description' alone tells the reader nothing they can act on")
check("it sits BEFORE the repost branch",
      SRC.find("j.jd_admit") < SRC.find("j.repost > 2"),
      "between repost and j.closed it lands inside REPOST_RE's anchor and breaks that test "
      "silently rather than loudly")
check("web.py derives the flag instead of reading a column",
      "_admitted_on_description" in open(
          os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web.py"),
          encoding="utf-8").read(),
      "a stamped column would need a hand-run migration and could drift from the filter")

print()
if FAILS:
    print("FAILURES (%d): %s" % (len(FAILS), "; ".join(FAILS)))
    raise SystemExit(1)
print("ALL CARD META CHECKS PASS")
