import os, sys, datetime
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core

TODAY = datetime.date(2026, 7, 29)
fails = []


def check(name, got, want):
    ok = got == want
    if not ok:
        fails.append(name)
    print("  %s %-52s got=%r want=%r" % ("ok " if ok else "FAIL", name, got, want))


def keys(tl):
    return [i["key"] for i in tl["items"]]


print("=" * 78)
print("empty profile -> nothing to show")
print("=" * 78)
tl = core.visa_timeline({}, TODAY)
check("has_data", tl["has_data"], False)
check("items", tl["items"], [])
check("alert", core.visa_alert(tl), None)

print("\n" + "=" * 78)
print("on post-completion OPT, EAD expires in 200 days, STEM eligible")
print("=" * 78)
prof = {"opt_type": "opt", "opt_start_date": "2026-02-01",
        "opt_end_date": (TODAY + datetime.timedelta(days=200)).isoformat(),
        "stem_eligible": "Yes", "unemployment_days_used": "12"}
tl = core.visa_timeline(prof, TODAY)
for i in tl["items"]:
    print("     %-46s %s  in %4d d  [%s]" % (i["label"], i["date"], i["days"], i["severity"]))
print("     unemployment:", tl["unemployment"])
check("opt_end present", "opt_end" in keys(tl), True)
check("stem window OPENS (not closes)",
      [i["label"] for i in tl["items"] if i["key"] == "stem_window"][0],
      "STEM extension filing window opens")
check("stem opens 90d before EAD",
      [i["days"] for i in tl["items"] if i["key"] == "stem_window"][0], 110)
check("unemployment limit is 90 on OPT", tl["unemployment"]["limit"], 90)
check("unemployment left", tl["unemployment"]["left"], 78)
check("no alert yet (all >90d, 78 days left)", core.visa_alert(tl), None)

print("\n" + "=" * 78)
print("EAD expires in 45 days -> STEM window is OPEN and closing")
print("=" * 78)
prof2 = dict(prof, opt_end_date=(TODAY + datetime.timedelta(days=45)).isoformat())
tl2 = core.visa_timeline(prof2, TODAY)
for i in tl2["items"]:
    print("     %-46s %s  in %4d d  [%s]" % (i["label"], i["date"], i["days"], i["severity"]))
check("stem window CLOSES",
      [i["label"] for i in tl2["items"] if i["key"] == "stem_window"][0],
      "STEM extension filing window closes")
check("opt_end severity soon (45d)",
      [i["severity"] for i in tl2["items"] if i["key"] == "opt_end"][0], "soon")
a = core.visa_alert(tl2)
check("alert fires", bool(a), True)
check("alert is the nearest deadline", a["days"], 45)

print("\n" + "=" * 78)
print("EAD expires in 10 days -> urgent")
print("=" * 78)
tl3 = core.visa_timeline(dict(prof, opt_end_date=(TODAY + datetime.timedelta(days=10)).isoformat()), TODAY)
check("severity urgent", [i["severity"] for i in tl3["items"] if i["key"] == "opt_end"][0], "urgent")
check("alert urgent", core.visa_alert(tl3)["severity"], "urgent")

print("\n" + "=" * 78)
print("already on the STEM extension -> no STEM window, 150-day allowance")
print("=" * 78)
tl4 = core.visa_timeline({"opt_type": "stem", "opt_end_date": "2028-01-15",
                          "stem_eligible": "Yes", "unemployment_days_used": "100"}, TODAY)
print("     ", [i["label"] for i in tl4["items"]])
check("no stem_window", "stem_window" in keys(tl4), False)
check("label says STEM OPT",
      [i["label"] for i in tl4["items"] if i["key"] == "opt_end"][0], "STEM OPT EAD expires")
check("limit 150", tl4["unemployment"]["limit"], 150)
check("left 50", tl4["unemployment"]["left"], 50)

print("\n" + "=" * 78)
print("unemployment nearly exhausted -> that becomes the alert")
print("=" * 78)
tl5 = core.visa_timeline({"opt_type": "opt", "opt_end_date": "2027-06-01",
                          "unemployment_days_used": "80"}, TODAY)
print("     unemployment:", tl5["unemployment"])
a5 = core.visa_alert(tl5)
check("left 10", tl5["unemployment"]["left"], 10)
check("severity urgent", tl5["unemployment"]["severity"], "urgent")
check("alert is unemployment", a5["key"], "unemployment")
check("alert label", a5["label"], "10 of 90 unemployment days left")

print("\n" + "=" * 78)
print("over the unemployment limit -> 'past', never a negative day count in the label")
print("=" * 78)
tl6 = core.visa_timeline({"opt_type": "opt", "opt_end_date": "2027-06-01",
                          "unemployment_days_used": "95"}, TODAY)
check("left is negative internally", tl6["unemployment"]["left"], -5)
check("severity past", tl6["unemployment"]["severity"], "past")
check("label clamps at 0", core.visa_alert(tl6)["label"], "0 of 90 unemployment days left")

print("\n" + "=" * 78)
print("expired EAD -> negative days, severity past")
print("=" * 78)
tl7 = core.visa_timeline({"opt_type": "opt", "opt_end_date": "2026-06-01"}, TODAY)
check("days negative", [i["days"] for i in tl7["items"] if i["key"] == "opt_end"][0], -58)
check("severity past", [i["severity"] for i in tl7["items"] if i["key"] == "opt_end"][0], "past")

print("\n" + "=" * 78)
print("H-1B registration anchor rolls to next year once March passes")
print("=" * 78)
check("July 2026 -> Mar 2027", core.next_h1b_registration(datetime.date(2026, 7, 29)),
      datetime.date(2027, 3, 1))
check("Jan 2026 -> Mar 2026", core.next_h1b_registration(datetime.date(2026, 1, 5)),
      datetime.date(2026, 3, 1))
check("Mar 1 itself -> today", core.next_h1b_registration(datetime.date(2026, 3, 1)),
      datetime.date(2026, 3, 1))

print("\n" + "=" * 78)
print("garbage input must never raise")
print("=" * 78)
for bad in [{"opt_end_date": "not a date"}, {"opt_end_date": None},
            {"unemployment_days_used": "abc"}, {"unemployment_days_used": -5},
            {"opt_end_date": "2026-13-99"}, {"stem_eligible": "maybe"},
            {"opt_end_date": "2027-01-01T00:00:00Z"}]:
    try:
        r = core.visa_timeline(bad, TODAY)
        core.visa_alert(r)
        print("  ok  %-44r -> has_data=%s items=%d" % (bad, r["has_data"], len(r["items"])))
    except Exception as e:
        fails.append("raised on %r" % bad)
        print("  FAIL %-44r raised %s" % (bad, e))

print("\n" + ("ALL VISA CHECKS PASS" if not fails else "%d FAILED: %s" % (len(fails), fails)))
# Exit non-zero, or a failure is invisible to CI. This file printed FAILED and exited 0 from the
# day it was added until 2026-08-20, so `set -e` in python-tests.yml never saw a thing.
if fails:
    raise SystemExit(1)
