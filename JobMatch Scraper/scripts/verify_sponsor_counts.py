import os, sys, collections
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for ln in open(".env", encoding="utf-8"):
    ln = ln.strip()
    if ln and not ln.startswith("#") and "=" in ln:
        k, v = ln.split("=", 1); os.environ.setdefault(k.strip(), v.strip())
import core, db

counts = core.load_sponsor_counts()
print("sponsor_counts.json loaded:", len(counts), "keys")

print("\n-- named assertions --")
for name, want in [("Amazon", "high"), ("Cognizant Technology Solutions", "high"),
                   ("Infosys", "high"), ("Deloitte", "high"), ("Google", "high"),
                   ("Northeastern University", None), ("Actalent", None),
                   ("Zzzz Nonexistent Company Qqq", "")]:
    tier, n = core.sponsor_strength(name, counts)
    ok = "ok " if (want is None or tier == want) else "FAIL"
    print("  %s %-32s -> %-6s %8s   (want %s)" % (ok, name, tier or "''", format(n, ","), want))

rows = db.load_jobs()
companies = collections.Counter((r.get("company") or "").strip() for r in rows if (r.get("company") or "").strip())
resolved = tiers = 0
tier_ct = collections.Counter()
jobs_covered = 0
unresolved = []
for c, njobs in companies.items():
    tier, n = core.sponsor_strength(c, counts)
    tier_ct[tier or "none"] += 1
    if n > 0:
        resolved += 1
        jobs_covered += njobs
    else:
        unresolved.append((njobs, c))

print("\n-- corpus coverage --")
print("  distinct companies: %d" % len(companies))
print("  with a filing count: %d (%.1f%%)" % (resolved, 100 * resolved / len(companies)))
print("  JOBS whose company has a count: %d / %d (%.1f%%)"
      % (jobs_covered, len(rows), 100 * jobs_covered / len(rows)))
print("  tier spread across companies:", dict(tier_ct))
print("\n-- biggest unresolved employers (by job count) --")
for njobs, c in sorted(unresolved, reverse=True)[:15]:
    print("   %5d jobs  %s" % (njobs, c))
