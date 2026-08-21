#!/usr/bin/env python3
"""/api/feed's rate limit must bite on loops and be invisible to real use.

This route was the one unguarded way to saturate the worker pool, needing no stolen token and no
malice: measured at 173 ms per request with a search term, 3.4x any other route, so one person
typing occupies most of a worker and a runaway fetch in a stale tab occupies all of them.
_ext_rate_limit never covered it -- it returns early on anything outside /api/ext/.

A limiter is only worth having if it cannot fire on legitimate use, so the assertions here are
symmetrical: the paced case must pass AND the burst case must trip. Testing only the trip would
pass just as well with the cap set to 1, which would break the feed for everybody.

The pacing is not arbitrary. app.js debounces search, location and the match slider at 250 ms
(debouncedRender) and pages behind a button, so 4 requests/second is the fastest a real browser
can go -- and only while someone types without ever pausing.

    python scripts/test_feed_ratelimit.py

No network and no database: get_jobs is stubbed and the corpus is inline.
"""
import os
import sys
import time
import atexit
import shutil
import tempfile

os.environ.setdefault("EV_OFF", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core
import db
import web
import analytics

FAILS = []


def check(name, cond, extra=""):
    if not cond:
        FAILS.append(name)
    print("  %s %-52s %s" % ("ok " if cond else "FAIL", name, extra))


JOBS = [{"url": "https://b.example/%d" % i,
         "title": ["Project Manager", "Data Analyst", "Program Manager"][i % 3] + " %d" % i,
         "company": ["Acme", "Globex", "Initech"][i % 3], "location": "Boston, MA",
         "found_date": "2026-08-18", "match_score": 40 + (i % 30), "is_active": True,
         "jd_terms": '{"w":{"python":1.0,"sql":0.5},"n":0}', "first_seen": "2026-08-18"}
        for i in range(200)]

web.get_jobs = lambda: JOBS
web._jobs_cache["rows"], web._jobs_cache["fp"] = JOBS, (len(JOBS), "2026-08-18")
web._session_dead = lambda u: ""
web._needs_onboarding = lambda u: False
web.current_profile = lambda: "python sql project management delivery"
web.user_statuses = lambda u: {}
web._user_prefs = lambda u: dict(core.DEFAULT_PREFS, min=0)
web._snapshot_write = lambda *a, **k: None
web._snapshot_touch = lambda *a, **k: None
# tempfile, not a path in the repo: this suite used to leave a score_cache directory behind
# next to itself, which then showed up as untracked junk in git status.
_TMP = tempfile.mkdtemp(prefix="jm_rl_")
web._SCORES_DIR = _TMP
atexit.register(lambda: shutil.rmtree(_TMP, ignore_errors=True))
db.using_supabase = lambda: False
db.get_profile = lambda u: {}
db._upsert = lambda *a, **k: None
analytics.emit = lambda *a, **k: None

FEED = "/api/feed?offset=0&limit=60&min=0"


def client(user):
    c = web.app.test_client()
    with c.session_transaction() as s:
        s["user"] = user
    return c


def main():
    # The limiter no-ops under app.testing (see _feed_rate_limit), so this suite must never
    # set it -- otherwise every assertion below would pass against a disabled limiter.
    assert not web.app.testing, ("this suite must not set TESTING: the limiter is gated on "
                                 "it, so the whole file would pass vacuously")
    burst_cap, burst_win = web._FEED_TIERS[0]
    sustained_cap, sustained_win = web._FEED_TIERS[-1]
    print("=" * 78)
    print("tiers: %d per %ds (burst), %d per %ds (sustained)"
          % (burst_cap, burst_win, sustained_cap, sustained_win))
    print("=" * 78)

    # --- the half that matters most: real use must never see a 429 -------------------------
    web._ext_hits.clear()
    c = client("paced@test")
    codes = []
    for _ in range(12):
        codes.append(c.get(FEED).status_code)
        time.sleep(0.26)                 # app.js's own debounce interval
    check("12 debounced requests over ~3s all succeed", set(codes) == {200},
          "saw %s" % sorted(set(codes)))
    check("...and the client's 4/sec ceiling is under the cap",
          4 * burst_win <= burst_cap, "4 x %ds = %d, cap %d" % (burst_win, 4 * burst_win, burst_cap))

    # --- and the half that makes it worth having ------------------------------------------
    web._ext_hits.clear()
    c = client("loop@test")
    codes = [c.get(FEED).status_code for _ in range(burst_cap + 10)]
    check("an unpaced loop is stopped", 429 in codes,
          "first 429 at request %s" % (codes.index(429) + 1 if 429 in codes else "never"))
    check("...at exactly the cap, not before",
          429 in codes and codes.index(429) == burst_cap,
          "tripped after %d" % (codes.index(429) if 429 in codes else -1))

    # --- the 429 must not read as an empty corpus -----------------------------------------
    r = c.get(FEED)
    body = r.get_json() or {}
    check("the 429 says it is a limit, not 'no jobs'", body.get("limited") is True, repr(body)[:70])
    check("...carries the keys app.js reads", "rows" in body and "total" in body)
    check("...and a usable Retry-After", (r.headers.get("Retry-After") or "").isdigit(),
          repr(r.headers.get("Retry-After")))
    check("app.js reads the 429 explicitly rather than r.json()",
          b"r.status === 429" in open(os.path.join(
              os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
              "static", "app.js"), "rb").read(),
          "otherwise a limit renders as 'No jobs match these filters'")

    # --- blast radius ---------------------------------------------------------------------
    check("one user does not throttle another", client("other@test").get(FEED).status_code == 200)
    check("the feed PAGE is never limited", client("loop@test").get("/").status_code == 200)
    check("a rejected call is not counted, so retry time cannot run away",
          len([t for t in web._ext_hits.get(("feed", "loop@test"), ())]) <= burst_cap + 1,
          "%d recorded" % len(web._ext_hits.get(("feed", "loop@test"), ())))

    # --- the shared counter must not have broken the extension limiter --------------------
    web._ext_hits.clear()
    hit = web._rate_hit(("x", "k"), ((2, 60),))
    hit2 = web._rate_hit(("x", "k"), ((2, 60),))
    hit3 = web._rate_hit(("x", "k"), ((2, 60),))
    check("_rate_hit allows exactly `cap` calls", hit is None and hit2 is None and hit3 is not None)
    check("...and reports a retry window", hit3 and hit3[0] > 0 and hit3[1] == 2, repr(hit3))

    print()
    if FAILS:
        print("FAILED: %s" % FAILS)
        return 1
    print("ALL FEED RATE-LIMIT CHECKS PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
