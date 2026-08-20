"""Exercise the per-user digest without sending any mail: SMTP is monkeypatched so a real
send would be visible as a failure, and every path runs in dry-run.
"""
import os, sys, json
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)
os.chdir(APP)
import core, db
from scraper import notify

fails = []
SENT = []


def check(name, cond, extra=""):
    if not cond:
        fails.append(name)
    print("  %s %s%s" % ("ok " if cond else "FAIL", name, ("  " + extra) if extra else ""))


# Hard guard: if anything tries to actually send, record it instead of hitting a network.
def _fake_send(cfg, to, subject, html):
    SENT.append({"to": to, "subject": subject, "html": html})


notify._send = _fake_send

# The digest reads whatever the LAST SCRAPE happened to leave in last_new_jobs.json. That made
# this file's results depend on ambient state: a scrape that turned up two California jobs made
# the Boston/remote users below match nothing and the run "fail" with no code change. Point the
# module at our own fixture instead, so the test asserts behaviour rather than yesterday's scrape.
FIXTURE = os.path.join(os.environ.get("TEMP") or "/tmp", "_test_notify_new_jobs.json")
json.dump([
    {"found_date": "2026-08-07", "title": "Program Manager", "company": "Acme Health",
     "location": "Boston, MA", "url": "https://example.test/j/1", "sponsors_h1b": "yes"},
    {"found_date": "2026-08-07", "title": "Technical Program Manager", "company": "Globex",
     "location": "Remote, United States", "url": "https://example.test/j/2", "sponsors_h1b": "yes"},
], open(FIXTURE, "w", encoding="utf-8"))
notify.NEW_FILE = FIXTURE

real_get_profile = db.get_profile
real_list_users = db.list_users
real_profile_text = db.profile_text


def fake_users(users, profiles, texts):
    db.list_users = lambda: [{"username": u} for u in users]
    db.get_profile = lambda u: dict(profiles.get(u) or {})
    db.profile_text = lambda u: texts.get(u, "")


PM_RESUME = open("resume.txt", encoding="utf-8").read()

print("=" * 78)
print("recipients(): opt-in required, and an email is required")
print("=" * 78)
fake_users(
    ["opted_in", "no_email", "opted_out"],
    {"opted_in": {"email": "a@example.test", "search_prefs": {"alerts": "daily", "min": 0}},
     "no_email": {"search_prefs": {"alerts": "daily"}},
     "opted_out": {"email": "c@example.test", "search_prefs": {"alerts": "off"}}},
    {"opted_in": PM_RESUME})
people = notify.recipients()
names = sorted(p[0] for p in people)
check("only the opted-in user with an email", names == ["opted_in"], "got %r" % names)

print()
print("=" * 78)
print("dry run builds a per-user digest and sends NOTHING")
print("=" * 78)
os.environ.pop("ALERT_TO", None)
os.environ["SMTP_HOST"] = "smtp.invalid.test"
os.environ["SMTP_USER"] = "bot@example.test"
os.environ["SMTP_PASS"] = "x"
os.environ["ALERT_DRY_RUN"] = "1"     # the flag under test — fully configured SMTP, still no send
notify.main()
check("nothing sent in dry run", SENT == [], "SENT=%d" % len(SENT))
os.environ.pop("ALERT_DRY_RUN", None)

print()
print("=" * 78)
print("two users with DIFFERENT saved searches get different digests")
print("=" * 78)
fake_users(
    ["boston_pm", "remote_only"],
    {"boston_pm": {"email": "b@example.test",
                   "search_prefs": {"alerts": "daily", "min": 0, "loc": "MA",
                                    "hideagency": True}},
     "remote_only": {"email": "r@example.test",
                     "search_prefs": {"alerts": "daily", "min": 0, "remote": True}}},
    {"boston_pm": PM_RESUME, "remote_only": PM_RESUME})
SENT.clear()
os.environ.pop("ALERT_DRY_RUN", None)
notify.main()                       # real path now, but _send is patched
check("one email per user", len(SENT) == 2, "sent=%d" % len(SENT))
if len(SENT) == 2:
    a, b = SENT[0], SENT[1]
    check("different recipients", a["to"] != b["to"], "%s vs %s" % (a["to"], b["to"]))
    check("different content", a["html"] != b["html"])
    check("MA digest mentions its saved location", "MA" in a["html"])
    check("remote digest says remote", "Remote" in b["html"] or "remote" in b["html"])
    for s in SENT:
        check("subject is specific (%s)" % s["to"], "new match" in s["subject"])
        check("body links jobs (%s)" % s["to"], "<a href=" in s["html"])
        check("body explains the filter (%s)" % s["to"], "saved search" in s["html"])
        check("body says how to stop (%s)" % s["to"], "alerts to Off" in s["html"])
    print("     subjects: %s" % [s["subject"] for s in SENT])

print()
print("=" * 78)
print("a user whose search matches nothing gets NO email (not an empty one)")
print("=" * 78)
fake_users(["picky"],
           {"picky": {"email": "p@example.test",
                      "search_prefs": {"alerts": "daily", "min": 99, "loc": "zzznowhere"}}},
           {"picky": PM_RESUME})
SENT.clear()
notify.main()
check("no empty digest sent", SENT == [], "sent=%d" % len(SENT))

print()
print("=" * 78)
print("legacy ALERT_TO still works when nobody has opted in")
print("=" * 78)
fake_users(["nobody"], {"nobody": {"email": "n@example.test", "search_prefs": {"alerts": "off"}}}, {})
os.environ["ALERT_TO"] = "legacy@example.test"
SENT.clear()
notify.main()
check("legacy digest sent", len(SENT) == 1, "sent=%d" % len(SENT))
if SENT:
    check("to the legacy address", SENT[0]["to"] == "legacy@example.test")
os.environ.pop("ALERT_TO", None)

print()
print("=" * 78)
print("no SMTP config -> dormant, and no new jobs -> quiet")
print("=" * 78)
for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS"):
    os.environ.pop(k, None)
SENT.clear()
notify.main()
check("dormant without SMTP", SENT == [])

notify.NEW_FILE = "does_not_exist.json"
os.environ["SMTP_HOST"] = "smtp.invalid.test"
os.environ["SMTP_USER"] = "bot@example.test"
os.environ["SMTP_PASS"] = "x"
SENT.clear()
notify.main()
check("quiet with no new jobs", SENT == [])

db.get_profile, db.list_users, db.profile_text = real_get_profile, real_list_users, real_profile_text
print("\n%s" % ("ALL DIGEST CHECKS PASS" if not fails else "%d FAILED: %s" % (len(fails), fails)))
# Exit non-zero, or a failure is invisible to CI. This file printed FAILED and exited 0 from the
# day it was added until 2026-08-20, so `set -e` in python-tests.yml never saw a thing.
if fails:
    raise SystemExit(1)
