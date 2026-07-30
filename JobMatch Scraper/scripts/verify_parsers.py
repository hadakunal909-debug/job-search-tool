import os, sys, collections
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for ln in open(".env", encoding="utf-8"):
    ln = ln.strip()
    if ln and not ln.startswith("#") and "=" in ln:
        k, v = ln.split("=", 1); os.environ.setdefault(k.strip(), v.strip())
import core, db

rows = db.load_jobs()
n = len(rows)
print("=" * 72)
print("parse_location over %d rows" % n)
print("=" * 72)
st = metro = city = remote = 0
unparsed = collections.Counter()
state_ct = collections.Counter()
metro_ct = collections.Counter()
for r in rows:
    L = r.get("location") or ""
    p = core.parse_location(L, r.get("jd") or "")
    if p["state"]:
        st += 1; state_ct[p["state"]] += 1
    elif L.strip():
        unparsed[L.strip()] += 1
    if p["metro"]:
        metro += 1; metro_ct[p["metro"]] += 1
    if p["city"]:
        city += 1
    if p["remote"]:
        remote += 1
print("  state resolved : %6d (%.1f%%)   [gate: >=87%%]" % (st, 100 * st / n))
print("  metro resolved : %6d (%.1f%%)" % (metro, 100 * metro / n))
print("  city resolved  : %6d (%.1f%%)" % (city, 100 * city / n))
print("  remote flagged : %6d (%.1f%%)   [was 718 on the raw string alone]" % (remote, 100 * remote / n))
print("  top states:", ", ".join("%s=%d" % kv for kv in state_ct.most_common(12)))
print("  top metros:")
for k, v in metro_ct.most_common(12):
    print("      %5d  %s" % (v, k))
print("  MA=%d  Boston metro=%d" % (state_ct.get("MA", 0), metro_ct.get("Boston, MA", 0)))
print("\n  top 20 UNPARSED location strings:")
for k, v in unparsed.most_common(20):
    print("      %5d  %r" % (v, k[:70]))

print()
print("=" * 72)
print("parse_salary over JD-bearing rows")
print("=" * 72)
withjd = [r for r in rows if (r.get("jd") or "").strip()]
yr = hr = 0
samples = []
mins = []
for r in withjd:
    s = core.parse_salary(r["jd"])
    if s["period"] == "year":
        yr += 1; mins.append(s["min"])
        if len(samples) < 10:
            samples.append("%-46s %s" % (r["title"][:46], core.salary_label(s["min"], s["max"], s["period"])))
    elif s["period"] == "hour":
        hr += 1
        if len(samples) < 14:
            samples.append("%-46s %s" % (r["title"][:46], core.salary_label(s["min"], s["max"], s["period"])))
t = len(withjd)
print("  JD-bearing rows: %d" % t)
print("  annual range   : %6d (%.1f%%)   [gate: >=31%%]" % (yr, 100 * yr / t))
print("  hourly range   : %6d (%.1f%%)" % (hr, 100 * hr / t))
print("  ANY pay        : %6d (%.1f%% of JD-bearing, %.1f%% of all jobs)"
      % (yr + hr, 100 * (yr + hr) / t, 100 * (yr + hr) / n))
if mins:
    mins.sort()
    print("  annual min: p10=$%s  median=$%s  p90=$%s"
          % (format(mins[len(mins) // 10], ","), format(mins[len(mins) // 2], ","),
             format(mins[int(len(mins) * .9)], ",")))
print("  samples:")
for s in samples:
    print("      ", s)

print()
print("=" * 72)
print("unit checks")
print("=" * 72)
cases = [
    ("Seattle, Washington, USA", "WA", "Seattle, WA", False),
    ("Seattle, WA", "WA", "Seattle, WA", False),
    ("Santa Clara,CA", "CA", "San Francisco Bay Area, CA", False),
    ("US, CA, Santa Clara", "CA", "San Francisco Bay Area, CA", False),
    ("New York City, New York", "NY", "New York, NY", False),
    ("Washington, D.C., US", "DC", "Washington, DC", False),
    ("Boston, MA (Remote)", "MA", "Boston, MA", True),
    ("Cambridge, Massachusetts", "MA", "Boston, MA", False),
    ("United States", "", "", False),
    ("Remote - US", "", "", True),
    ("", "", "", False),
]
bad = 0
for raw, want_st, want_metro, want_rem in cases:
    p = core.parse_location(raw)
    ok = p["state"] == want_st and p["metro"] == want_metro and p["remote"] == want_rem
    if not ok:
        bad += 1
    print("  %s %-28r -> state=%-3s metro=%-28r remote=%s" %
          ("ok " if ok else "FAIL", raw, p["state"] or "-", p["metro"], p["remote"]))

print()
sal_cases = [
    ("The base pay range for this role is $120,000 - $150,000 per year.", 120000, 150000, "year"),
    ("Compensation: $95K-$115K plus equity", 95000, 115000, "year"),
    ("Pay rate $25.00 - $35.50 per hour", 25, 36, "hour"),
    ("We raised $120,000,000 in Series C", None, None, ""),
    ("Bonus up to $5,000 annually", None, None, ""),
    ("no pay info here at all", None, None, ""),
]
for jd, wmin, wmax, wper in sal_cases:
    s = core.parse_salary(jd)
    ok = s["min"] == wmin and s["max"] == wmax and s["period"] == wper
    if not ok:
        bad += 1
    print("  %s %-52r -> %s" % ("ok " if ok else "FAIL", jd[:52],
                                core.salary_label(s["min"], s["max"], s["period"]) or "(none)"))
print("\n%s" % ("ALL UNIT CHECKS PASS" if bad == 0 else "%d UNIT CHECK(S) FAILED" % bad))

print()
print("-- JD remote negation guard --")
for jd, want in [("This is a fully remote position.", True),
                 ("This is not a remote position.", False),
                 ("No telecommuting is available for this role.", False),
                 ("Remote-first company with offices in Boston.", True),
                 ("Candidates cannot work from home.", False)]:
    got = core._jd_says_remote(jd)
    print("  %s %-52r -> %s" % ("ok " if got == want else "FAIL", jd[:52], got))
