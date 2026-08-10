"""core.prefs_match (the EMAIL filter) must agree with web.py::_filter_rows (the FEED filter)
on every filter they share, or the digest promises jobs the feed won't show.

Rather than refactor _filter_rows (the grouping branch rewrites it), this asserts agreement
across the real corpus for many prefs combinations.
"""
import os, sys, itertools
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)
os.chdir(APP)
import web, core

fails = []


def check(name, cond, extra=""):
    if not cond:
        fails.append(name)
    print("  %s %s%s" % ("ok " if cond else "FAIL", name, ("  " + extra) if extra else ""))


print("=" * 78)
print("normalize_prefs: untrusted input must always yield a complete valid dict")
print("=" * 78)
cases = [
    (None, "None"), ({}, "empty"), ("", "empty string"), ("not json", "garbage string"),
    ('{"min": 60, "loc": "Boston"}', "json string"),
    ({"min": "abc"}, "non-numeric min"), ({"min": -5}, "negative min"),
    ({"min": 9999}, "absurd min"), ({"exp": "nonsense"}, "invalid choice"),
    ({"date": "30"}, "valid choice"), ({"hideagency": "off"}, "bool from string"),
    ({"remote": 1}, "bool from int"), ({"loc": "x" * 500}, "overlong loc"),
    ({"alerts": "hourly"}, "unsupported alert cadence"),
    ({"unknown_key": "x"}, "unknown key"),
]
for raw, label in cases:
    p = core.normalize_prefs(raw)
    ok = (set(p) == set(core.DEFAULT_PREFS)
          and isinstance(p["min"], int) and 0 <= p["min"] <= 100
          and p["exp"] in ("any", "2", "5", "senior")
          and p["alerts"] in ("off", "daily")
          and isinstance(p["hideagency"], bool) and len(p["loc"]) <= 80)
    check("normalize(%s)" % label, ok, "min=%r exp=%r alerts=%r" % (p["min"], p["exp"], p["alerts"]))
check("json string parsed", core.normalize_prefs('{"min": 60, "loc": "Boston"}')["min"] == 60)
check("invalid choice falls back", core.normalize_prefs({"exp": "nonsense"})["exp"] == "any")
check("absurd min clamped", core.normalize_prefs({"min": 9999})["min"] == 100)
check("hideagency 'off' -> False", core.normalize_prefs({"hideagency": "off"})["hideagency"] is False)

print()
print("=" * 78)
print("prefs_match vs _filter_rows over the real corpus")
print("=" * 78)
# A RÉSUMÉ, not "". user_scores now suppresses every score when the viewer has none — a feed of
# confident percentages computed against the scraper's own resume.txt was somebody else's
# number. But an all-zero corpus would make the min=30/45/60 cases below match nothing on both
# sides and pass VACUOUSLY, which is the one way this grid can lie.
RESUME = ("Project manager and business analyst. Six years running software delivery, "
          "stakeholder reporting, agile ceremonies, SQL and Python data analysis, "
          "vendor management, process improvement, budgets and roadmaps.")
rows = web.ranked_rows("prefs-test", RESUME)
scored = sum(1 for r in rows if r.get("score"))
check("the corpus is actually scored (else every min case below is vacuous)",
      scored > 100, "%d of %d rows scored" % (scored, len(rows)))
print("  corpus rows: %d" % len(rows))

grid = []
for loc in ("", "boston", "MA", "remote"):
    for remote in (False, True):
        for hideagency in (False, True):
            grid.append({"loc": loc, "remote": remote, "hideagency": hideagency})
for minv in (0, 30, 45, 60):
    grid.append({"min": minv})
for minsal in (0, 80000, 130000):
    grid.append({"minsal": minsal})
for exp in ("any", "2", "5", "senior"):
    grid.append({"exp": exp})
for intern in ("any", "only", "no"):
    grid.append({"intern": intern})
for tag in core.VISA_TAGS:
    grid.append({"visatags": tag})
grid.append({"visatags": "h1b,green_card"})
grid.append({"visatags": "stem_opt,e3,h1b1"})
grid.append({"hidenospon": True})
grid.append({"loc": "boston", "min": 40, "hideagency": True, "exp": "5", "intern": "no"})
grid.append({"loc": "CA", "minsal": 100000, "hidenospon": True, "min": 35})

mismatch_total = 0
worst = None
for i, override in enumerate(grid):
    prefs = core.normalize_prefs(dict({"min": 0, "hideagency": False, "date": "any"}, **override))
    # Feed side: same prefs, expressed as query args. date=any so the two are comparable
    # (prefs_match deliberately has no date filter — every digest candidate is brand new).
    params = web._prefs_as_params(prefs)
    params["date"] = "any"
    # Same reason as `date` above, and the same treatment: verifiedonly is FEED-ONLY. Every
    # digest candidate is a job we just discovered, so its date has not been confirmed yet —
    # applying this to the email would silently empty it rather than filter it. Neutralised
    # here so the grid still compares the filters the two sides genuinely share.
    params["verifiedonly"] = ""
    feed_set = {r["url"] for r, _st in web._filter_rows(rows, {}, params)}
    mail_set = {r["url"] for r in rows if core.prefs_match(r, prefs)}
    diff = feed_set ^ mail_set
    mismatch_total += len(diff)
    if diff and (worst is None or len(diff) > worst[1]):
        worst = (override, len(diff), list(diff)[:3])

print("  %d prefs combinations compared" % len(grid))
check("zero disagreements", mismatch_total == 0,
      "" if mismatch_total == 0 else "total differing urls: %d, worst: %r" % (mismatch_total, worst))

# Without this the visa half of the grid is a no-op: with no index every row gets (), every
# visa case matches nothing on BOTH sides, and "zero disagreements" passes while testing
# nothing. Fail loudly instead of passing quietly.
_vidx = core.load_visa_tags()
check("visa index is loaded (else the visa cases above are vacuous)", len(_vidx) > 1000,
      "%d employers" % len(_vidx))
_tagged = {t for r in rows for t in (r.get("visa") or ())}
check("corpus actually carries visa tags", len(_tagged) >= 3, "present: %s" % sorted(_tagged))

print()
print("=" * 78)
print("visatags normalization")
print("=" * 78)
check("junk dropped", core.normalize_prefs({"visatags": "nonsense,h1b"})["visatags"] == "h1b")
check("order canonicalized",
      core.normalize_prefs({"visatags": "e3,h1b"})["visatags"] == "h1b,e3")
check("duplicates collapsed",
      core.normalize_prefs({"visatags": "h1b,h1b"})["visatags"] == "h1b")
_mig = core.normalize_prefs({"everify": True})
check("legacy everify migrates to stem_opt", _mig["visatags"] == "stem_opt")
check("...and clears everify, so unticking STEM-OPT sticks", _mig["everify"] is False)
check("normalize_prefs keeps the full key set",
      set(core.normalize_prefs({})) == set(core.DEFAULT_PREFS))

print()
print("=" * 78)
print("prefs_match specifics the feed can't express")
print("=" * 78)
# Any row will do — every check below overrides score= explicitly. Don't require a scored one:
# that made this line raise StopIteration the moment scores could legitimately all be zero.
sample = next((r for r in rows if r.get("score")), rows[0])
p = core.normalize_prefs({"min": 10, "alert_min": 90})
check("alert_min overrides min for email",
      core.prefs_match(dict(sample, score=50), p) is False)
p2 = core.normalize_prefs({"min": 10})
check("min applies when alert_min is 0",
      core.prefs_match(dict(sample, score=50), p2) is True)
check("closed rows never emailed",
      core.prefs_match(dict(sample, score=99, closed=True), p2) is False)

print()
print("=" * 78)
print("digest_row builds the shape prefs_match needs, from a RAW db job")
print("=" * 78)
import db
raw = next(j for j in db.load_jobs() if (j.get("jd") or "").strip() and j.get("location"))
dr = core.digest_row(raw, 55, core.load_everify(), core.load_visa_tags())
for k in ("title", "company", "url", "location", "score", "loc_state", "loc_metro", "remote",
          "salary_min", "salary_period", "salary_label", "sponsors_h1b", "sponsor_jd",
          "agency", "cap_exempt", "everify", "visa", "exp_years", "intern", "closed"):
    check("digest_row has %s" % k, k in dr)
check("digest_row is prefs_match-compatible",
      isinstance(core.prefs_match(dr, core.normalize_prefs({"min": 0})), bool))
print("     sample: %s | %s | state=%r pay=%r" %
      (dr["title"][:40], dr["company"][:20], dr["loc_state"], dr["salary_label"]))

print("\n%s" % ("ALL PREFS CHECKS PASS" if not fails else "%d FAILED: %s" % (len(fails), fails)))
