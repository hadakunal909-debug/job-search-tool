"""SLICING MUST NOT CHANGE WHAT IS STORED.

main() sweeps in slices when SCRAPE_SLICE is set, writing and releasing each slice instead of
holding the whole corpus for one final db.add_jobs(). That exists because two cPanel runs were
SIGKILLed on 2026-08-24 having banked nothing -- the second after a COMPLETE 48.4-minute sweep.

The risk the split introduces is a DIFFERENT CORPUS, not a crash: per-slice re-initialisation of
an accumulator that should span the run (the tally, the fingerprint list, cross-slice URL dedupe)
would silently admit or drop rows. So this asserts on the ROWS ACTUALLY WRITTEN -- the union
across every add_jobs call -- rather than on a log line or a return value.
"""
import os, sys, json

os.environ["EV_OFF"] = "1"          # analytics reads this once at import (analytics.py:33)
os.environ["DISCOVER_LIMIT"] = "0"  # else main() probes ~60 real employers over the network
os.environ.setdefault("DB_REQUIRE", "")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scraper
from scraper import db as _db

# A SOURCES entry is the tuple (url, ats_type, company).
BOARDS = [("https://x.test/%02d" % i, "greenhouse", "Co%02d" % i) for i in range(10)]

def _postings(entry):
    """Three postings per board: one plainly ours, one plainly not, one non-US."""
    c = entry[2]
    return [
        {"title": "Software Engineer", "company": c, "location": "Boston, MA",
         "url": "https://x.test/%s/1" % c, "found_date": "", "jd": ""},
        {"title": "Line Cook", "company": c, "location": "Boston, MA",
         "url": "https://x.test/%s/2" % c, "found_date": "", "jd": ""},
        {"title": "Data Analyst", "company": c, "location": "Bengaluru, India",
         "url": "https://x.test/%s/3" % c, "found_date": "", "jd": ""},
    ]

def run(slice_size, budget=0):
    """Run main() with the network and the database stubbed; return the rows it wrote."""
    written, jds, budgets, jd_calls, arms = [], {}, [], [], []

    def fake_scrape_all(sources, workers=None, progress=None, board_results=None, budget_min=None):
        budgets.append(budget_min)
        out = []
        for e in sources:
            rows = _postings(e)
            out.extend(rows)
            if board_results is not None:
                board_results.append({"entry": e, "company": e[2], "ok": True,
                                      "skipped": False, "err": None, "secs": 0.1,
                                      **scraper._pack_urls(r["url"] for r in rows)})
            if progress:
                progress(len(board_results or []), len(sources), len(out))
        return out

    # Count the JD-lookup calls and the budget armings SEPARATELY. main() calls
    # fill_missing_jds once per SLICE and must arm its budget once per RUN -- that asymmetry is
    # the whole of the per-slice bug, and it is only visible from up here. The function's own
    # behaviour is covered by test_jd_lookup_order.py; this pins the WIRING.
    def counting_fill(*a, **k):
        jd_calls.append(len(a[0]) if a else 0)
        return (0, 0)

    stubs = {
        "scrape_all": fake_scrape_all,
        "custom_sources": lambda *a, **k: [],
        "load_sponsors": lambda *a, **k: set(),
        "fill_missing_jds": counting_fill,
        "reconcile_closed": lambda *a, **k: (0, 0),
        "save_board_health": lambda *a, **k: None,
        "truncation_report": lambda *a, **k: "",
    }
    dbstubs = {
        "add_jobs": lambda rows: written.append([dict(r) for r in rows]),
        "update_jds": lambda m: jds.update(m),
        "existing_urls": lambda *a, **k: set(),
        "blocked_company_keys": lambda *a, **k: set(),
        "load_jobs": lambda *a, **k: [],
        "prune_old_jobs": lambda *a, **k: 0,
        "stale_urls": lambda *a, **k: [],
        "set_scrape_status": lambda *a, **k: None,
        "has_remote_db": lambda: True,
        "backend_name": lambda: "stub",
    }
    old = {k: getattr(scraper, k, None) for k in stubs}
    oldb = {k: getattr(_db, k, None) for k in dbstubs}
    old_sources = scraper.SOURCES
    old_budget = scraper.SCRAPE_BUDGET_MIN
    old_slice = scraper.SCRAPE_SLICE
    old_rotate = scraper.SCRAPE_ROTATE
    try:
        for k, v in stubs.items():
            setattr(scraper, k, v)
        for k, v in dbstubs.items():
            setattr(_db, k, v)
        scraper.SOURCES = list(BOARDS)
        scraper.SCRAPE_BUDGET_MIN = budget
        scraper.SCRAPE_SLICE = slice_size
        scraper.SCRAPE_ROTATE = 0          # rotation would reorder the list, not the SET
        _real_reset = scraper.reset_jd_lookup_budget

        def counting_reset():
            arms.append(1)
            _real_reset()
        scraper.reset_jd_lookup_budget = counting_reset
        try:
            scraper.main()
        finally:
            scraper.reset_jd_lookup_budget = _real_reset
    finally:
        for k, v in old.items():
            if v is not None:
                setattr(scraper, k, v)
        for k, v in oldb.items():
            if v is not None:
                setattr(_db, k, v)
        scraper.SOURCES = old_sources
        scraper.SCRAPE_BUDGET_MIN = old_budget
        scraper.SCRAPE_SLICE = old_slice
        scraper.SCRAPE_ROTATE = old_rotate
    return written, budgets, jd_calls, arms

def urls(batches):
    return sorted(r["url"] for b in batches for r in b)

fails = []

one, one_budgets, one_jd, one_arms = run(0)          # single pass, the old behaviour
many, many_budgets, many_jd, many_arms = run(3)   # 10 boards in slices of 3 -> 4 slices
# THE BUDGET MUST SPAN THE RUN, NOT RESET PER SLICE. Left unhandled, slicing would have turned
# one 22-minute deadline into six of them -- and in CI that is six times the step timeout.
_, budgeted, _jd2, _arms2 = run(3, budget=22)
# An EXHAUSTED budget must stop the run STARTING slices -- and must still leave what it already
# swept in the database. This is the assertion that would actually have failed before the fix:
# a per-slice budget can never be exhausted, so it could never stop anything.
spent, spent_budgets, _jd3, _arms3 = run(3, budget=1e-9)

if len(one) > 1:
    fails.append("SCRAPE_SLICE=0 should write once, wrote %d times" % len(one))
if len(many) < 2:
    fails.append("SCRAPE_SLICE=3 over 10 boards should write more than once, wrote %d" % len(many))
if urls(one) != urls(many):
    a, b = set(urls(one)), set(urls(many))
    fails.append("sliced run stored a DIFFERENT corpus: only-unsliced=%s only-sliced=%s"
                 % (sorted(a - b)[:5], sorted(b - a)[:5]))
if len(urls(many)) != len(set(urls(many))):
    fails.append("sliced run wrote the same url twice")
# the whole point: work is banked as it goes, not at the end
if len(many) < 4:
    fails.append("expected one write per slice (4), got %d" % len(many))
if any(b is not None for b in one_budgets + many_budgets):
    fails.append("SCRAPE_BUDGET_MIN=0 should pass no deadline, passed %r" % (many_budgets,))
if len(budgeted) < 2:
    fails.append("expected several budgeted slices, got %r" % (budgeted,))
elif not all(isinstance(b, float) for b in budgeted):
    fails.append("a budgeted slice got a non-numeric deadline: %r" % (budgeted,))
elif budgeted != sorted(budgeted, reverse=True):
    fails.append("the budget must be SPENT DOWN across slices, got %r" % (budgeted,))
elif budgeted[0] > 22:
    fails.append("first slice got more than the whole budget: %r" % (budgeted,))
if len(spent_budgets) >= 4:
    fails.append("an exhausted budget started every slice anyway (%d) -- it is still per-slice"
                 % len(spent_budgets))
if len(spent) != len(spent_budgets):
    fails.append("a slice that ran did not get written: %d swept, %d written"
                 % (len(spent_budgets), len(spent)))
# and the run's own summary file must describe the WHOLE run, not the last slice
try:
    saved = json.load(open("last_new_jobs.json", encoding="utf-8"))
    if len(saved) != len(urls(many)):
        fails.append("last_new_jobs.json holds %d row(s), the run stored %d"
                     % (len(saved), len(urls(many))))
except Exception as e:
    fails.append("last_new_jobs.json unreadable: %s" % e)

# THE DESCRIPTION BUDGET IS ARMED ONCE PER RUN, NOT ONCE PER SLICE.
#
# fill_missing_jds is called per slice -- that part is correct and expected -- but until
# 2026-09-02 it also re-derived its request ceiling and its clock on every one of those calls,
# so SCRAPE_SLICE=100 over 2,032 boards turned "1,200 fetches, 3 minutes" into 21 x 1,200 and
# 63 minutes. That phase then ate the run: the 2026-09-01 20:00 cron got through two slices of
# twenty-one before being SIGKILLed. Same lesson as SCRAPE_BUDGET_MIN above, one phase later.
if len(many_jd) != 4:
    fails.append("the JD lookup ran %d time(s) across 4 slices -- it is called per slice"
                 % len(many_jd))
if len(many_arms) != 1:
    fails.append("the JD budget was armed %d time(s) in one run; it must be armed exactly once"
                 % len(many_arms))
if len(one_arms) != 1:
    fails.append("an unsliced run armed the JD budget %d time(s)" % len(one_arms))

# --- board_results is the accumulator SLICING NEVER BOUNDED -------------------------------
# SCRAPE_SLICE bounds the postings held WITHIN a slice; board_results spans the whole run, so
# whatever it keeps per URL is multiplied by every URL on every board -- 518,504 of them across
# 2,213 boards on the 2026-09-09 cron run. Keeping a set of strings there cost 97 MB against a
# margin of roughly 20 MB, which is why the same sweep banked fine on 09-09 and came back
# rc=137 on 09-10. It holds ONE joined string per board now (measured: 51 MB), and this is the
# guard on that -- the shape is easy to revert to a set by accident, and nothing else would say.
_probe = ["https://boards.test/co/job/%d" % i for i in range(500)]
_packed = scraper._pack_urls(_probe)

if set(_packed) != {"n", "urlblob"}:
    fails.append("_pack_urls returned keys %r; board_results entries must carry exactly "
                 "n + urlblob" % (sorted(_packed),))
if not isinstance(_packed.get("urlblob"), str):
    fails.append("urlblob is %s, not a string -- the per-URL object overhead is back"
                 % type(_packed.get("urlblob")).__name__)
if _packed.get("n") != len(_probe):
    fails.append("n is %r for %d urls" % (_packed.get("n"), len(_probe)))

# Round trip: reconcile_closed rebuilds the set from this and must get back exactly what the
# board returned, scheme-stripped -- that normalisation is what reconcile always applied itself.
_want = {scraper._norm_url(u) for u in _probe}
_got = scraper._unpack_urls(_packed)
if _got != _want:
    fails.append("_unpack_urls lost %d and invented %d url(s)"
                 % (len(_want - _got), len(_got - _want)))
if scraper._unpack_urls({"urlblob": ""}) or scraper._unpack_urls({}):
    fails.append("an empty board must unpack to an empty set, not {''}")

# The REAL producer's shape is guarded in test_board_health.py, which drives scrape_all itself.
# Asserting it against fake_scrape_all here would only assert that this file calls _pack_urls.

print("unsliced: %d write(s), %d row(s)" % (len(one), len(urls(one))))
print("sliced:   %d write(s), %d row(s)" % (len(many), len(urls(many))))
print("budget spans the run: deadlines handed to the slices = %r" % ([round(b, 4) for b in budgeted],))
print("JD budget:           armed %d time(s), lookup ran %d time(s) over 4 slices"
      % (len(many_arms), len(many_jd)))
print("budget exhausted:    %d of 4 slice(s) started, %d write(s)" % (len(spent_budgets), len(spent)))
if fails:
    print("\nFAIL")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("\nok - slicing stores exactly what one pass stores, and banks it per slice")
