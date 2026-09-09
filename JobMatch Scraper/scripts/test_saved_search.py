"""The saved-search round trip through the real app: POST /prefs, then confirm the feed's
controls come back pre-set and the first-paint count reflects them. No credentials.
"""
import os, sys, re, json
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)
os.chdir(APP)
import web, db, core

USER = (db.list_users() or [{}])[0].get("username")
web.app.config["TESTING"] = True
fails = []
STORE = {}


# The denominator moved into its own span so it can be hidden when it equals the numerator
# (an unfiltered feed reading "24,918 of 24,918 jobs" is noise). This still pins what the test
# actually cares about: the numerator alone in #count, because app.js parses it back out as an
# integer, and the total beside it.
COUNT_RE = r'<span id="count">(\d+)</span>(?:<span[^>]*>)? of (\d+)'


def check(name, cond, extra=""):
    if not cond:
        fails.append(name)
    print("  %s %s%s" % ("ok " if cond else "FAIL", name, ("  " + extra) if extra else ""))


# Keep this test off the real database: profiles live in memory for the duration.
db.get_profile = lambda u: dict(STORE)
db.save_profile = lambda u, payload: (STORE.update(payload), (True, ""))[1]


# Cookie-authenticated writes now require a CSRF token (web.py _require_csrf), exactly as a real
# browser sends one — from a hidden _csrf field on forms, or an X-CSRF-Token header from fetch().
# Seed the same value both ways so these tests exercise the path the app actually uses.
CSRF = "test-token"


def client():
    c = web.app.test_client()
    with c.session_transaction() as s:
        s["user"] = USER
        s["_csrf"] = CSRF
    return c


print("=" * 78)
print("with no saved prefs the feed ships the documented defaults")
print("=" * 78)
body = client().get("/").data.decode("utf-8", "replace")
# DERIVED, not hardcoded. This said value="45" while DEFAULT_PREFS["min"] was 50, so it
# cannot have passed for some time -- the suite is db-tier and nothing runs it. A test that
# freezes a calibration constant fails the next time the constant is calibrated, which is
# exactly what it should NOT be sensitive to. The default is 0 as of 2026-09-08.
_want_min = str(core.DEFAULT_PREFS["min"])
check("min slider at the documented default (%s)" % _want_min,
      'id="min"' in body and ('value="%s"' % _want_min) in body)
check("30-day window selected", '<option value="30" selected>Past 30 Days' in body)
check("agencies hidden by default", 'id="hideagency" checked' in body)
check("Save-as-default button present", 'id="saveprefs"' in body)
m = re.search(COUNT_RE, body)
base_count = int(m.group(1)) if m else -1
print("     first-paint count:", m.groups() if m else "not found")

print()
print("=" * 78)
print("POST /prefs saves a search")
print("=" * 78)
c = client()
r = c.post("/prefs", headers={"X-CSRF-Token": CSRF}, json={"min": 35, "loc": "boston", "remote": False, "minsal": 0,
                           "hideagency": True, "everify": False, "hidenospon": True,
                           "exp": "5", "intern": "no", "date": "90", "sort": "newest"})
check("200", r.status_code == 200)
d = r.get_json()
check("ok true", d.get("ok") is True, str(d)[:120])
saved = d.get("prefs") or {}
check("loc saved", saved.get("loc") == "boston")
check("min saved", saved.get("min") == 35)
check("exp saved", saved.get("exp") == "5")
check("date saved", saved.get("date") == "90")
check("sort saved", saved.get("sort") == "newest")
check("stored on the profile", "search_prefs" in STORE)

print()
print("=" * 78)
print("the feed comes back with those controls pre-set")
print("=" * 78)
body = client().get("/").data.decode("utf-8", "replace")
# The Location control was removed from the feed. The PREF still round-trips — it is what
# the daily email digest narrows on — so what is asserted is that it survived the save,
# which "loc saved" above already proves, and that the feed no longer renders a control
# for it. Asserting the absence is the point: a stale #loc left behind would keep
# narrowing the feed with nothing on screen able to clear it.
check("no loc control on the feed", 'id="loc"' not in body)
check("min slider restored", 'id="min" class="ranged" type="range" min="0" max="75" step="1" value="35"' in body)
check("date=90 selected", '<option value="90" selected>Past 90 Days' in body)
check("exp=5 selected", '<option value="5" selected>' in body)
# #intern is a hidden value carrier behind two "Job type" checkboxes now, so the option has
# no user-visible label left to assert. The value round-tripping is the actual contract.
check("intern=no selected", '<option value="no" selected>' in body)
check("sort=newest selected", '<option value="newest" selected>' in body)
check("hidenospon checked", 'id="hidenospon" checked' in body)
check("hideagency still checked", 'id="hideagency" checked' in body)
m2 = re.search(COUNT_RE, body)
new_count = int(m2.group(1)) if m2 else -1
print("     first-paint count:", m2.groups() if m2 else "not found")
check("count reflects the saved search", new_count != base_count and new_count > 0,
      "%d -> %d" % (base_count, new_count))

print()
print("=" * 78)
print("count matches what /api/feed returns for the same saved search")
print("=" * 78)
prefs = core.normalize_prefs(STORE.get("search_prefs"))
qs = "&".join("%s=%s" % (k, v) for k, v in web._prefs_as_params(prefs).items())
api = client().get("/api/feed?" + qs).get_json()
check("first paint == api total", new_count == api.get("total"),
      "paint=%d api=%d" % (new_count, api.get("total", -1)))

print()
print("=" * 78)
print("garbage from a browser can't poison the saved search")
print("=" * 78)
r = client().post("/prefs", headers={"X-CSRF-Token": CSRF}, json={"min": "9999", "exp": "'; drop table jobs;--",
                                  "loc": "x" * 900, "date": "banana", "alerts": "hourly"})
p = (r.get_json() or {}).get("prefs") or {}
check("min clamped", p.get("min") == 100, repr(p.get("min")))
check("exp rejected", p.get("exp") == "any", repr(p.get("exp")))
check("loc truncated", len(p.get("loc", "")) <= 80, "len=%d" % len(p.get("loc", "")))
check("date rejected", p.get("date") == "30", repr(p.get("date")))
check("alerts rejected", p.get("alerts") == "off", repr(p.get("alerts")))

print()
print("=" * 78)
print("profile page alert controls round-trip into search_prefs")
print("=" * 78)
STORE.clear()
c = client()
r = c.post("/profile", data={"_csrf": CSRF, "alerts": "daily", "alert_min": "55",
                            "email": "me@example.test"})
check("redirects", r.status_code in (302, 303))
sp = core.normalize_prefs(STORE.get("search_prefs"))
check("alerts=daily stored", sp.get("alerts") == "daily", repr(sp.get("alerts")))
check("alert_min stored", sp.get("alert_min") == 55, repr(sp.get("alert_min")))
body = client().get("/profile").data.decode("utf-8", "replace")
check("Daily selected in the form", '<option value="daily" selected>Daily Digest' in body)
check("alert_min shown", 'value="55"' in body)

print()
print("=" * 78)
print("saving a profile must not clobber an existing saved search")
print("=" * 78)
STORE.clear()
# min_scale DECLARED: without it this reads as a pre-migration profile and the scale bump
# resets min to the default before the check can see whether a profile save preserved it.
# The thing under test is the profile save, not the migration.
STORE["search_prefs"] = {"alerts": "daily", "min": 33, "loc": "boston",
                         "min_scale": core.MIN_SCALE}
client().post("/profile", data={"_csrf": CSRF, "alerts": "daily", "email": "me@example.test"})
sp = core.normalize_prefs(STORE.get("search_prefs"))
check("loc survived a profile save", sp.get("loc") == "boston", repr(sp.get("loc")))
check("min survived a profile save", sp.get("min") == 33, repr(sp.get("min")))

print("\n%s" % ("ALL SAVED-SEARCH APP CHECKS PASS" if not fails
                else "%d FAILED: %s" % (len(fails), fails)))
