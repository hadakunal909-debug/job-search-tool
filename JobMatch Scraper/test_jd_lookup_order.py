"""THE SWEEP MUST BUY DESCRIPTIONS FOR THE JOBS IT KEEPS BEFORE THE ONES IT REJECTS.

fill_missing_jds exists to fetch a description the boards did not hand over. It was built to
RESCUE postings the title filter had dropped, and until 2026-09-02 that was all it did -- a row
that passed the title filter and had no description was skipped outright (`if keep: continue`)
and left for score_jobs to fetch on some later run.

What that cost, from the live cron log of the 2026-09-01 20:00 run, one slice of twenty-one:

    JD lookup: fetching 1200 description(s) for title-rejected postings
    JD lookup: 1200 of 1200 returned a usable description.
    add_jobs (105 rows)
    Stored 4 description(s) that arrived with the listing.

1,200 descriptions bought for postings that were discarded, 4 for the 105 rows kept. Measured the
same day, 404 of the 2,274 rows added (17.8%) landed with no description at all.

Two properties are asserted here and each one is a bug that shipped:

  ORDER   a kept row is fetched before a title-rejected one, even when the rejected ones come
          first in the slice and even when there are more of them than the budget allows.
  SPAN    the request ceiling and the clock are the RUN's, not the slice's. fill_missing_jds is
          called once per slice; both ceilings used to be re-derived on every call, so
          SCRAPE_SLICE=100 over 2,032 boards turned "1,200 fetches, 3 minutes" into 21 x 1,200
          and 63 minutes. test_scrape_slice.py froze exactly this lesson for SCRAPE_BUDGET_MIN;
          this pair was added after the slicing and never got it.

Plus the Phenom-by-id path, which is the difference between reading Actalent and not: for those
tenants the url we STORE is an apply app that renders no description for any job, so asking by
the url is a guaranteed-empty round trip no matter how much budget it is given.

The network is poisoned rather than mocked-permissively: an unexpected fetch is itself a failure.
"""
import os, sys

os.environ["EV_OFF"] = "1"
os.environ.setdefault("DB_REQUIRE", "")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import core
import scraper
from scraper import score_jobs

fails = []
JD = "We are hiring a program manager. " * 30          # comfortably over _MIN_JD_CHARS


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if detail and not cond else ""))
    if not cond:
        fails.append(name + ((": " + detail) if detail else ""))


def posting(n, title, **extra):
    row = {"title": title, "company": "Co%d" % n, "location": "Boston, MA",
           "url": "https://x.test/job/%d" % n, "found_date": "", "jd": ""}
    row.update(extra)
    return row


class Calls(object):
    """Records every fetch the pass makes, by the route it took."""

    def __init__(self):
        self.detail, self.phenom = [], []

    def install(self, jd=JD):
        def _detail(url):
            self.detail.append(url)
            return url, jd, ""

        def _by_id(origin, jid):
            self.phenom.append((origin, jid))
            return jd
        score_jobs.detail_jd = _detail
        score_jobs.phenom_jd_by_id = _by_id


_real = (score_jobs.detail_jd, score_jobs.phenom_jd_by_id)


def run(scraped, seen=(), budget=None, per_kept=None, reset=True, cutoff="", long_cutoff=""):
    """One fill_missing_jds pass with the network recorded. Returns (calls, tried, got)."""
    calls = Calls()
    calls.install()
    old_b, old_k = scraper.JD_LOOKUP_BUDGET, scraper.JD_LOOKUP_PER_KEPT_BOARD
    if budget is not None:
        scraper.JD_LOOKUP_BUDGET = budget
    if per_kept is not None:
        scraper.JD_LOOKUP_PER_KEPT_BOARD = per_kept
    try:
        if reset:
            scraper.reset_jd_lookup_budget()
        tried, got = scraper.fill_missing_jds(scraped, set(seen), None, cutoff,
                                             long_cutoff or cutoff)
    finally:
        scraper.JD_LOOKUP_BUDGET, scraper.JD_LOOKUP_PER_KEPT_BOARD = old_b, old_k
        score_jobs.detail_jd, score_jobs.phenom_jd_by_id = _real
    return calls, tried, got


# ---------------------------------------------------------------------------------------------
# The titles this suite leans on, verified against the real filter rather than assumed. A test
# that silently mislabels its own fixtures asserts nothing -- if "Line Cook" ever became a KEEP,
# every ordering check below would still pass while testing the opposite thing.
# ---------------------------------------------------------------------------------------------
# "Coordinator II" is the canonical rescue case -- no keyword matches it, nothing vetoes it,
# and core.pm_title_gate lets the description have a vote. Verified below rather than assumed.
KEPT_TITLE, REJECTED_TITLE = "Program Manager", "Coordinator II"
print("fixture sanity")
_k, _kw = scraper.title_verdict(KEPT_TITLE)
_r, _rw = scraper.title_verdict(REJECTED_TITLE)
check("the KEEP fixture really is kept", _k is True, "%r -> %s" % (KEPT_TITLE, _kw))
check("the REJECT fixture really is rejected", _r is False, "%r -> %s" % (REJECTED_TITLE, _rw))
check("the REJECT fixture is rescuable, not vetoed",
      _r is False and not _rw.startswith("off-target") and core.pm_title_gate(REJECTED_TITLE),
      "%r -> %s, pm_title_gate=%s" % (REJECTED_TITLE, _rw, core.pm_title_gate(REJECTED_TITLE)))

# ---------------------------------------------------------------------------------------------
print("\norder: kept rows come first")

# The rejected rows are deliberately FIRST in the slice and outnumber the budget, so a pass that
# simply walks `scraped` in order spends everything before it reaches the kept row.
mixed = [posting(i, REJECTED_TITLE) for i in range(5)] + [posting(99, KEPT_TITLE)]
calls, tried, got = run(mixed, budget=2)
check("the budget was spent on 2 fetches", tried == 2, "tried=%d" % tried)
check("the kept row was fetched", "https://x.test/job/99" in calls.detail,
      "fetched %r" % (calls.detail,))
check("it was fetched FIRST", calls.detail[:1] == ["https://x.test/job/99"],
      "fetched %r" % (calls.detail,))
check("the description landed on the row", (mixed[-1].get("jd") or "") == JD)
check("usable count matches", got == 2, "got=%d" % got)

# A kept row already carrying a description must not be re-fetched.
have = [posting(1, KEPT_TITLE, jd=JD), posting(2, KEPT_TITLE)]
calls, tried, _ = run(have, budget=10)
check("a row that already has a description is not re-fetched",
      calls.detail == ["https://x.test/job/2"], "fetched %r" % (calls.detail,))

# A row already in the corpus is never re-judged, kept title or not.
calls, tried, _ = run([posting(1, KEPT_TITLE)], seen={"https://x.test/job/1"}, budget=10)
check("a row already in `seen` is skipped", tried == 0 and not calls.detail,
      "tried=%d fetched=%r" % (tried, calls.detail))

# ---------------------------------------------------------------------------------------------
print("\nspan: the budget is the RUN's, not the slice's")

def slice_of(base, n=3):
    """Fresh rows every time -- fill_missing_jds sets j["jd"] IN PLACE, so a reused fixture
    arrives already satisfied and the next pass reports 0 for the wrong reason."""
    return [posting(base + i, KEPT_TITLE) for i in range(n)]

calls_a, tried_a, _ = run(slice_of(0), budget=4)                  # arms the run
calls_b, tried_b, _ = run(slice_of(10), budget=4, reset=False)    # same run, next slice
check("the first slice spent its share", tried_a == 3, "tried=%d" % tried_a)
check("the second slice got only what was LEFT", tried_b == 1,
      "tried=%d -- a per-slice budget would have allowed 3" % tried_b)
check("the run total never exceeds the ceiling", tried_a + tried_b == 4,
      "%d + %d" % (tried_a, tried_b))

calls_c, tried_c, _ = run(slice_of(20), budget=4, reset=False)    # still the same run
check("a spent budget stops later slices entirely", tried_c == 0 and not calls_c.detail,
      "tried=%d fetched=%r" % (tried_c, calls_c.detail))

calls_d, tried_d, _ = run(slice_of(30), budget=4)                 # new run
check("resetting re-arms the budget", tried_d == 3, "tried=%d" % tried_d)

# An exhausted CLOCK stops the pass without touching the network, the same way the count does.
scraper.reset_jd_lookup_budget()
scraper._JD_RUN["deadline"] = 0.0                                 # monotonic() is always > 0
calls_e, tried_e, _ = run(slice_of(40), budget=10, reset=False)
check("an expired run deadline fetches nothing",
      tried_e == 0 and not calls_e.detail, "tried=%d" % tried_e)

# ---------------------------------------------------------------------------------------------
print("\nphenom: asked by id, never by the apply url")

ph = [posting(1, KEPT_TITLE, _phenom_jid="JP-1", _phenom_origin="https://careers.t.test")]
calls, tried, got = run(ph, budget=10)
check("the phenom row went through phenom_jd_by_id",
      calls.phenom == [("https://careers.t.test", "JP-1")], "phenom=%r" % (calls.phenom,))
check("its apply url was never fetched", not calls.detail, "fetched %r" % (calls.detail,))
check("the description landed on the row", (ph[0].get("jd") or "") == JD)

# A row without the pair still takes the ordinary route -- the branch must not swallow everything.
mix = [posting(1, KEPT_TITLE, _phenom_jid="JP-2", _phenom_origin="https://careers.t.test"),
       posting(2, KEPT_TITLE)]
calls, tried, _ = run(mix, budget=10)
check("a non-phenom row still uses detail_jd",
      calls.detail == ["https://x.test/job/2"] and len(calls.phenom) == 1,
      "detail=%r phenom=%r" % (calls.detail, calls.phenom))

# ---------------------------------------------------------------------------------------------
print("\ngates: no fetch is spent on a row the keep loop is about to drop")

# Freshness. The keep loop drops these on `posted < age_cutoff`; buying a description first is
# pure waste, and the cutoff has to be the SAME expression the loop uses.
old_row = posting(1, KEPT_TITLE, found_date="2020-01-01")
new_row = posting(2, KEPT_TITLE, found_date="2099-01-01")
calls, tried, _ = run([old_row, new_row], budget=10, cutoff="2026-08-02")
check("a posting past the age cutoff is not fetched",
      calls.detail == ["https://x.test/job/2"], "fetched %r" % (calls.detail,))

# No cutoff configured -> no freshness filtering, or a manual run would silently skip rows.
calls2 = Calls(); calls2.install()
try:
    scraper.reset_jd_lookup_budget()
    old_row["jd"] = ""
    scraper.fill_missing_jds([old_row], set(), None)
finally:
    score_jobs.detail_jd, score_jobs.phenom_jd_by_id = _real
check("with no cutoff passed, an old posting IS fetched",
      calls2.detail == ["https://x.test/job/1"], "fetched %r" % (calls2.detail,))

# Non-US rows are dropped by the keep loop too.
calls, tried, _ = run([posting(1, KEPT_TITLE, location="Bengaluru, India"),
                       posting(2, KEPT_TITLE)], budget=10)
check("a non-US posting is not fetched",
      calls.detail == ["https://x.test/job/2"], "fetched %r" % (calls.detail,))

# A shell shorter than _MIN_JD_CHARS must never be stored as a description.
row = posting(1, KEPT_TITLE)
calls = Calls(); calls.install(jd="Loading ... Sorry to interrupt")
try:
    scraper.reset_jd_lookup_budget()
    _t, g = scraper.fill_missing_jds([row], set(), None, "", "")
finally:
    score_jobs.detail_jd, score_jobs.phenom_jd_by_id = _real
check("a sub-threshold shell is not stored", not row.get("jd") and g == 0,
      "jd=%r got=%d" % (row.get("jd"), g))

# ---------------------------------------------------------------------------------------------
print("\nper-employer ceiling")

flood = [posting(i, KEPT_TITLE) for i in range(20)]
for r in flood:
    r["company"] = "OneBigTenant"
calls, tried, _ = run(flood, budget=100, per_kept=5)
check("one employer cannot take the whole keep budget", tried == 5, "tried=%d" % tried)

if fails:
    print("\nFAIL (%d)" % len(fails))
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("\nok - kept rows are bought first, the budget spans the run, phenom is asked by id")
