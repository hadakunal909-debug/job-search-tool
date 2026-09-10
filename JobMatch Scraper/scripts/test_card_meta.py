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
# app.js WITHOUT its line comments. Several checks below ask "does a card still render X", and
# the answer must not be yes because the comment above the branch explains what X used to be --
# which is exactly what happened: this file documents the labels and the clause it removed, and
# a grep over the raw source read that prose as the feature. Comments are the record of a
# decision; only code is the decision.
CODE = "\n".join(re.sub(r"//.*$", "", ln) for ln in SRC.splitlines())

FAILS = []


def check(name, cond, extra=""):
    if not cond:
        FAILS.append(name)
    print("  %s %-58s %s" % ("ok " if cond else "FAIL", name, extra))


# ----------------------------------------------------------------------------------
# 1. The chip. The KEY is still chosen server-side by core.sponsor_likely; since 2026-08-31 the
#    card no longer looks a LABEL up from it, so there is no client-side table to compare.
# ----------------------------------------------------------------------------------
print("\nONE CHIP, AND IT NAMES NO ROUTE")

# The label table is gone from app.js, not merely unused. A copy left behind would be a second
# vocabulary free to drift from core's, and a silent one, because nothing would read it.
check("app.js no longer mirrors SPONSOR_LIKELY_LABELS",
      "SPONSOR_LIKELY_LABELS =" not in SRC,
      "the card renders two fixed strings; /job and /company name the routes")
check("the two strings are the whole vocabulary",
      "Sponsorship likely" in SRC and "Sponsorship unlikely" in SRC,
      "one question, answered or not answered")
check("no route name survives on a card",
      not any(x in CODE for x in ("H-1B Likely", "Sponsor Likely", "STEM-OPT Likely")),
      "which route an employer filed for is a fact about the EMPLOYER, not this posting")
# The third state is SILENCE. core.py's own rule is "no route shown means no record, not a
# refusal", so a card with neither a verdict nor a filing record must render no chip at all --
# 2,020 rows in the live 30-day feed, which would otherwise be told "unlikely" on no evidence.
check("no record renders no chip", core.sponsor_likely(()) == "",
      "absence is not a refusal; the card stays quiet and /job says so in words")

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

print("\nTHE STAR, which replaced ', top sponsor to FY2025'")
# Gated on BOTH halves. j.strength alone would star every employer with a filing history, and
# vtop alone would star every card -- the mark means "top H-1B sponsor", which is one specific
# claim about volume, not a decoration for having any record at all.
check("the star is gated on h1b AND a high strength",
      'vtop === "h1b" && j.strength === "high"' in SRC,
      "~30% of cards; the other 70% carry the chip without it")
check("the star is a mark, not a sentence",
      "\\u2605" in CODE and ", top sponsor to " not in CODE,
      "the clause doubled the chip's width on 30% of cards to carry one bit")
# A mark nobody can decode is decoration. The legend prints the sentence once, above the grid,
# in the include BOTH /feed and /company render -- not in a tooltip on 8,000 cards.
GRID = open(os.path.join(APP, "templates", "_feedgrid.html"), encoding="utf-8").read()
check("the grid prints the star's legend once",
      "sponkey" in GRID and "&#9733;" in GRID and "sponsor_data_through()" in GRID,
      "and it quotes the same window the chip's tooltip does")

# The old multi-chip machinery must be GONE, not merely unreachable. A dead rankVisa() next to
# chips that are no longer ranked is worse than no comment at all.
check("rankVisa() deleted", "function rankVisa" not in SRC)
check("_wantVisa cache deleted", "_wantVisa = visaWanted()" not in SRC)
check('"+N more" chip deleted', "vt-more" not in SRC)
check("the chip priority list deleted", "_chip(" not in SRC and "CARD_CHIP_MAX" not in SRC,
      "capping eleven chips at three was the previous answer; one chip needs no ranking")
check("cardHTML reads j.visa_likely", "j.visa_likely" in SRC)
# Every chip below the old cap had to LAND somewhere, and these are the two that had no
# job-page equivalent. Trimming the card without moving them would have deleted them.
JOBHTML = open(os.path.join(APP, "templates", "job.html"), encoding="utf-8").read()
# WHAT RENDERS, with the Jinja comments taken out. A {# ... #} block emits nothing, and the
# comments in this template deliberately QUOTE the copy they replaced so the next reader can see
# what was removed and why -- which made an assertion that a phrase is absent match the very
# note explaining its absence. Any check about what a reader SEES has to run against this.
JOBBODY = re.sub(r"\{#.*?#\}", " ", JOBHTML, flags=re.S)
# A CHIP OR A COLUMN -- what is asserted is that the fact still reaches this page, not which
# shape it takes. CLAUDE.md's own rule is that a fact gets a column and a verdict gets a chip,
# and on 2026-09-05 pay, workplace and years moved into the At a Glance grid as fixed tracks
# where they can be compared down the page. They carry data-fact="<name>" there. Pinning
# `class="pay` would have frozen the chip shape and made following that rule a test failure.
for _cls in ("pay", "rem", "intl", "exp", "cx", "agency", "repost", "jdadmit"):
    check("/job still renders %r, as a chip or as a column" % _cls,
          'class="%s' % _cls in JOBHTML or 'data-fact="%s"' % _cls in JOBHTML,
          "the card dropped it; the detail page is where it went")
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
process.stdout.write(JSON.stringify(out));
"""

# THE SHIM IS GONE WITH THE BRANCH. This used to text-lift cardHTML's repost branch out of
# app.js and evaluate it under node, because the threshold and the agency guard were arithmetic
# worth running. The chip moved to templates/job.html on 2026-08-31 when the card was cut to one
# chip, and a Jinja `{%- if row.repost > 2 and not row.agency %}` has nothing to evaluate: the
# condition IS the assertion. It is checked against the template source below instead, which is
# the same guarantee by a cheaper route -- and REPOST_CASES stays as the table of what the rule
# is supposed to decide, now read by a Python mirror rather than by node.


def run_js():
    fns = "\n".join(js_function(SRC, f) for f in
                    ("parseRowDate", "hasClock", "daysAgo", "relTime", "scoreRing"))
    src = DRIVER % {
        "fns": fns,
        "dates": json.dumps([c[0] for c in DATE_CASES]),
        "news": json.dumps([c[0] for c in NEW_CASES]),
        "scores": json.dumps([c[0] for c in RING_CASES]),
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
      "the card carries a tint, and a ring drawn straight onto it loses its own edge")
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

print("\nTHE REPOST BADGE, a count and not a verdict — now on /job")
# The Jinja condition, mirrored in Python. Same table, same decisions; what changed is only
# which file states the rule. A drift in either number fails here.
_M = re.search(r"\{%-\s*if row\.repost > (\d+) and not row\.agency\s*%\}", JOBHTML)
check("the branch exists and is still (count > N and not agency)", _M is not None,
      "if this moved, update the regex rather than retyping the rule")
_floor = int(_M.group(1)) if _M else -1
for (n, agency, want, why) in REPOST_CASES:
    label = "repost=%-4s agency=%-5s" % (n, agency)
    fires = (n is not None and n > _floor and not agency)
    check(label + (" -> no badge" if want is None else " -> %r" % want),
          fires == (want is not None), why)
check("the threshold matches detect_reposts' published default", _floor == 2,
      "the badge and scripts/detect_reposts.py must agree on what a repost IS")
# The wording matters as much as the threshold: we can prove the count, not the motive.
_row = next((l for l in JOBHTML.splitlines() if 'class="repost"' in l), "")
check("the badge states a count, not a judgement",
      _row and "ghost" not in _row.lower() and "fake" not in _row.lower(),
      "we can prove N postings; we cannot prove why")
check("the tooltip says where the number comes from",
      "different URLs" in _row and "90 days" in _row,
      "otherwise 'Posted 5x' is unfalsifiable")

print("\nTHE 'MATCHED ON DESCRIPTION' CHIP, added 2026-08-20 — now on /job")
# Wording and provenance, asserted against the template it now lives in. There is no
# arithmetic here — a plain `{%- if row.jd_admit %}` — so there was never anything to evaluate,
# only claims to hold still.
check("the chip exists and reads as provenance",
      "row.jd_admit" in JOBHTML and "Matched on description" in JOBHTML,
      "the whole point of the wider net is that you can see which rule admitted a row")
check("its tooltip explains the rule rather than asserting quality",
      "description reads like" in JOBHTML and "matched none of our role" in JOBHTML,
      "'matched on description' alone tells the reader nothing they can act on")
# The CHIP moved; the FIELD stays. rolesMatch() reads j.jd_admit to decide which role
# families a title-less row may answer (see core.roles_match), so asserting the field were gone
# would demand deleting a filter twin feed_parity.py checks.
check("the card no longer renders it", "matched on description" not in CODE,
      "it moved rather than being duplicated; two copies is one place for the wording to drift")
check("but the FILTER still reads the flag", "j.jd_admit" in CODE,
      "rolesMatch needs it; only the chip was on the card")
check("web.py derives the flag instead of reading a column",
      "_admitted_on_description" in open(
          os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web.py"),
          encoding="utf-8").read(),
      "a stamped column would need a hand-run migration and could drift from the filter")

# ---------------------------------------------------------------------------------------------
# THE EXPERIENCE INK, AND THE FOURTH STATE THAT MAKES IT HONEST
# ---------------------------------------------------------------------------------------------
# The card reads exp_max_years, a STORED column; /job reads the description LIVE. When the column
# is stale the two contradict each other in the same session -- a card saying "years not stated"
# over a page that just printed "5+ years required". Measured on the live corpus before the
# 2026-09-03 re-derive: 21.5% of the 39,459 rows holding a description.
#
# A re-derive fixes the stale ones. What it CANNOT fix is a row fetched since the last scoring
# run, and for those "years not stated" is not a stale answer, it is a FALSE CLAIM: it asserts
# something about the employer's posting when the only fact we hold is about our own pipeline.
# The two flags that separate the cases already exist and already drive the score ring.
# A FIGURE OR NOTHING. The card briefly carried the three no-answer states in words -- "years
# not stated", "not read yet", "unknown" -- and on a feed where most rows are one of them that
# is a column of cards explaining what the app does not know. The owner asked for silence
# instead, so the distinction has to live on /job, where it is one posting and there is room.
for _gone in ("years not stated", "years not read yet", "years unknown"):
    check("the card does not print %r" % _gone, _gone not in CODE,
          "a missing figure already reads as 'no figure'")
# SAID ONCE, ABOVE THE DESCRIPTION, rather than repeated into four rows. The claim this check
# defends is unchanged and still binding: "the employer did not say" and "we have not read it"
# are opposite facts and the page must not print the wrong one. What moved on 2026-09-05 is
# where it is said. The four-row block spent a sentence per empty row explaining what the
# posting did not contain, and the owner asked for a dash instead; the distinction those
# sentences carried survives as one .jdstate line on the description itself.
check("...and /job still names which of the three it is",
      "asks.verdict == 'not-a-posting'" in JOBHTML and "not has_jd" in JOBHTML
      and "rather than a posting" in JOBHTML and "No description stored yet" in JOBHTML,
      "'the employer did not say' and 'we have not read it' are opposite facts, and only one "
      "of them changes on its own")
check("...and an empty fact is a dash, not a sentence about the posting",
      "&mdash;" in JOBBODY and "Not stated in this posting" not in JOBBODY
      and "no separate required-skills" not in JOBBODY
      and "Nothing listed as preferred" not in JOBBODY,
      "the owner's instruction 2026-09-05: if it is not there, leave it blank")
check("the inferred case survives, because it is an ANSWER",
      "senior role" in CODE,
      "silence there means a job vanishing from '0 to 2 Years' with nothing to say why")
# INK, NOT A CHIP. CLAUDE.md: one blue and one chip, walked back twice already.
CSS = open(os.path.join(APP, "static", "style.css"), encoding="utf-8").read()
check("the experience is ink in the fact grid, not a second chip",
      "cexp" in CODE and "cexp" in CSS,
      "reusing .exp / .exp-hi would give it the /job chip's background by stylesheet accident")

print()
print("THE FACT GRID -- a chip is a verdict, a fact is a column (2026-09-03)")
# THE THREE FIELDS THE CARD WAS ALREADY BEING HANDED AND NEVER DREW. web.py::_build_row has
# shipped salary_label, remote and exp_level in the feed JSON the whole time; cardHTML read
# none of them, so the server paid to compute and serialise three facts per row for nobody. A
# grep for salary_label in app.js returned 0 hits the day this was written.
for _field in ("j.salary_label", "j.remote"):
    check("the card draws %s" % _field, _field in CODE,
          "it is already on the row and already over the wire")
check("...through one cell builder", "function factCell(" in CODE,
      "a top-level function, because feed_parity.py and test_filter_memory.py lift by source "
      "text and a var would break both")
check("the four fact cells are still addressable",
      all(("'%s'" % k) in CODE for k in ("cf-loc", "cf-rem", "cf-pay", "cf-exp")),
      "the classes outlived the emoji they used to carry; the ordering check below needs them")
check("...and no emoji is left on a card or in the description",
      ".cfi{" not in CSS and "jsi" not in CSS
      and not re.search(r'jdh\[data-sec="\w+"\]::before', CSS),
      "removed 2026-09-03 at the owner's direction")
# FIXED TRACKS ARE THE WHOLE FEATURE. Collapse the empty cells and pay stops sitting under pay,
# which is the only reason a uniform grid beats the 330px auto-fill one it replaced -- there,
# every card was its own width and nothing lined up with anything.
check("the fact tracks are fixed and equal",
      ".cfacts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))" in CSS,
      "equal halves in a uniform grid put pay at the same offset in all three columns")
# Scoped to the FEED's own rules: /companies has its own auto-fill grid (.codir) and is not
# making a claim about shared offsets.
check("the feed is a fixed three-column grid, not auto-fill",
      ".feedgrid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr))" in CSS
      and not re.search(r"#feed\s*\{[^}]*grid-template-columns", CSS),
      "auto-fill sizes cards to the viewport, so the offsets stop being shared -- and an #feed "
      "rule beats .feedgrid on ID specificity no matter where it sits in the file")
# ORDER IS BY COVERAGE. location 98% / years 91% / pay 41% / remote 17%, so the two near-universal
# facts take line one and the 52% of cards carrying exactly two read as one line, not a diagonal.
_order = [CODE.index("'cf-%s'" % k) for k in ("loc", "exp", "pay", "rem")]
check("fact cells are ordered by how often they are populated", _order == sorted(_order),
      "place, years, pay, remote -- see the measured coverage in cardHTML")
check("an unknown fact renders an empty cell, not a placeholder",
      "cfact-blank" in CODE and "cfact-blank" in CSS)
for _gone in ("Not stated", "Salary not stated", "Not specified"):
    check("the grid does not print %r" % _gone, _gone not in CODE,
          "a blank cell already reads as 'no figure' -- the same rule the years ink follows")
# NO SENIORITY CELL. exp_level is core.exp_level_for(exp_years): the same number the years cell
# prints, bucketed. Two columns saying "Entry Level" and "0+ yrs" is one fact twice.
check("exp_level is still not drawn on the card", "j.exp_level" not in CODE,
      "it is the years cell restated, and it would spend a track to say nothing new")

print()
print("THE VERDICT COLUMN -- our claim, kept apart from the employer's")
check("the score label reuses the ring's own thresholds",
      "function matchLabel(" in CODE
      and all(s in CODE for s in ("'Strong Match'", "'Good Match'", "'Low Match'")))
check("...and is absolute, never a percentile",
      "s >= 70 ?" in CODE and "percentile" not in CODE.lower(),
      "CLAUDE.md: the feed SORTS on this number, so a relative scale saturates")
check("...and says nothing when there is no ring",
      "!HAS_RESUME || (j && (j.jd_unavailable || j.score_pending))" in CODE,
      "a label under a dashed placeholder would name a number that is not there")
check("the verdict is its own element, separable from the posting's facts",
      "'<div class=\"cardverdict\">'" in CODE and ".cardverdict{" in CSS,
      "it was a ruled side column while the feed was full-width rows and is the top-right "
      "corner at three to a line; what must survive either way is that it is ONE element "
      "holding everything that depends on who is asking")
check("the chip cap survived the redesign",
      CODE.count('class="spon"') == 1 and CODE.count('class="nospon"') == 1,
      "one hedged sponsorship verdict, still, plus Closed")

print()
print("A SENIOR TITLE VETOES AN ENTRY LEVEL -- found by using the feed, 2026-09-08")
# Filtering the real feed for "Entry Level / Associate" with the Product Manager chip on
# returned 716 rows and 86 of them (12.0%) were senior-TITLED. _build_row derived the level
# from the YEARS band, so a Senior Product Manager whose description says "2+ years" became
# "entry" and the title was never consulted. After the veto: 623 rows, 2 senior-titled (0.3%),
# and both of those are the "Senior ... Associate" coin flip title_level declines to judge.
#
# ONE RULE IN CORE, because there were two derivations of this field: /job called
# core.level_for (description text) while the card and the filter used the years band, and
# level_for's docstring claims to be "the one definition every surface reads".
for _t, _y, _want in (("Senior Product Manager, Web Application Platform", 2, "senior"),
                      ("Director, Product Operations", 3, "senior"),
                      ("Staff Product Manager", 1, "senior"),
                      # WAS "senior" UNTIL 2026-09-10, and only because title_level had no word
                      # for the middle rung: "ii" sat in _TITLE_SENIOR_LEVEL_RE with iii-vi. It
                      # is the weakest numeral in that set (71.5% against 81.1%) and 1,183
                      # active rows -- 3.0% of the corpus -- were senior for no other reason,
                      # Project Manager II and Program Manager II among them. What this row was
                      # protecting is intact: mid implies 3 years, so the posting still fails a
                      # "0 to 2 Years" ceiling. It now passes "3 to 5 Years", which it should.
                      ("Product Manager II", 2, "mid"),
                      # ...and the years still win when they are HIGHER than the rung.
                      ("Product Manager II", 8, "senior"),
                      # the veto only ever moves a level UP, so these are untouched
                      ("Associate Product Manager", 2, "entry"),
                      ("Product Manager", 1, "entry"),
                      ("Product Manager Intern", None, "entry")):
    _got, _src = core.level_from_exp(_y, "stated" if _y is not None else "", _t)
    check("level_from_exp(%r, %s) -> %s" % (_t[:34], _y, _want), _got == _want, _got)

# The coin flip stays a coin flip: at Capital One and the banks "Senior Associate" IS the
# early-career rung, so title_level returns "" and the years are left to decide.
check("Senior Associate is NOT vetoed -- it is a genuine coin flip",
      core.level_from_exp(1, "stated", "Senior Associate, Product Manager - Credit Management")[0]
      == "entry")
check("the veto never invents an entry role, only ever raises to senior",
      core.senior_title_veto("", "Senior Product Manager") == ""
      and core.senior_title_veto("senior", "Associate Product Manager") == "senior")

# level_for -- the /job path -- must apply the SAME veto, or the two surfaces disagree again.
check("level_for applies the veto too",
      core.level_for("We are looking for someone with 2+ years of experience building products.",
                     "Senior Product Manager")[0] == "senior")

print()
if FAILS:
    print("FAILURES (%d): %s" % (len(FAILS), "; ".join(FAILS)))
    raise SystemExit(1)
print("ALL CARD META CHECKS PASS")
