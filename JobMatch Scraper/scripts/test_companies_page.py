#!/usr/bin/env python3
"""The /companies directory, through Flask's real test client.

Every assertion here is aimed at something that would fail QUIETLY:

  * The `?c=` link must carry the CORPUS spelling, not the registry one. /company filters on
    db.block_key, which does NOT strip legal suffixes, so a card linking "Accenture" lands on a
    page that finds nothing while the corpus stores "Accenture LLP". Six employers differ that
    way in live data. Nothing about the card looks wrong — only the destination is empty.
  * A careers URL must be absent or absolute. The build script's resolution ladder deliberately
    refuses to guess `<domain>/careers`, and a future edit that "helpfully" synthesised one
    would ship links that 404.
  * A boards row with ats_type='direct' must stay OUT of scraper.custom_sources() while still
    reaching the directory. That single fact is what makes the apply-direct flow safe without a
    guard in the scrape path, so it is frozen here rather than left to a comment.
  * The page must NOT ship #feed, or app.js would try to initialise a card grid on it.
  * page_view must fire exactly once per GET.

    python scripts/test_companies_page.py
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core
import db
import web
import analytics

USER = "companies@test"

# The corpus. Accenture is the regression fixture: companies.json calls it "Accenture", the
# corpus calls it "Accenture LLP", and db.block_key does not reconcile the two.
JOBS = [
    {"url": "https://x.test/1", "title": "Consultant", "company": "Accenture LLP",
     "location": "Boston, MA", "found_date": "2026-08-04", "match_score": 60, "is_active": True},
    {"url": "https://x.test/2", "title": "Analyst", "company": "Accenture LLP",
     "location": "Chicago, IL", "found_date": "2026-08-04", "match_score": 55, "is_active": True},
    # is_active None is an un-migrated row and must COUNT as open; only an explicit False is closed.
    {"url": "https://x.test/3", "title": "SDE", "company": "Corpusonly Inc",
     "location": "Seattle, WA", "found_date": "2026-08-03", "match_score": 70, "is_active": None},
    {"url": "https://x.test/4", "title": "Gone", "company": "Accenture LLP",
     "location": "Austin, TX", "found_date": "2026-07-01", "match_score": 20, "is_active": False},
]

web.get_jobs = lambda: [dict(j) for j in JOBS]
web._session_dead = lambda u: ""
web._needs_onboarding = lambda u: False
web.current_profile = lambda: ""
web.user_statuses = lambda u: {}
web.sponsor_counts = lambda: {"accenture": 11703}
web.sponsor_years = lambda: {}
web.visa_index = lambda: {}
db.using_supabase = lambda: False
db.get_brain_company = lambda dom: {}
db.list_brain_companies = lambda: {}
web._rows_cache.clear()

# A boards table holding one real board and one apply-direct row.
BOARDS = [
    {"url": "https://job-boards.greenhouse.io/newco", "ats_type": "greenhouse",
     "company": "Newco", "added_by": USER},
    {"url": "https://directonly.test/careers", "ats_type": "direct",
     "company": "Directonly Ltd", "added_by": USER},
]
db.list_boards = lambda: [dict(b) for b in BOARDS]

# Drop the caches these stubs invalidate.
web._co_stats_cache["data"] = None
web._boards_cache["data"] = None

EMITS = []
analytics.emit = lambda user, sid, name, **props: EMITS.append((name, props))
web.analytics = analytics

FAILS = []


def check(name, cond, extra=""):
    if not cond:
        FAILS.append(name)
    print("  %s %-62s %s" % ("ok " if cond else "FAIL", name, extra))


def client():
    c = web.app.test_client()
    with c.session_transaction() as s:
        s["user"] = USER
    return c


def get(path):
    del EMITS[:]
    return client().get(path)


print("=" * 92)
print("ROUTE + MARKUP")
print("=" * 92)
r = get("/companies")
check("/companies is 200", r.status_code == 200, str(r.status_code))
body = r.data.decode("utf-8", "replace")
check("ships the data block", 'id="codata"' in body)
check("ships the meta block", 'id="cometa"' in body)
check("ships the grid container", 'id="companies"' in body)
# app.js early-returns without #feed; if this page ever grew one it would try to run the feed.
check("does NOT ship #feed, so app.js cannot initialise here", 'id="feed"' not in body)
check("does not load app.js", "app.js" not in body)
check("loads companies.js", "companies.js" in body)

r302 = client().get("/careers")
check("/careers is a 301 to /companies",
      r302.status_code == 301 and r302.headers.get("Location", "").endswith("/companies"),
      "%s -> %s" % (r302.status_code, r302.headers.get("Location")))

print()
print("EVENTS")
names = [n for n, _ in EMITS]
# ONE, not two. _ev_page_view (an after_request hook) already reports every HTML 200 with
# ep=<endpoint>, so a route that also emits its own page_view double-counts itself -- and
# inflated usage numbers are a mistake this app has already made twice.
check("page_view fires exactly once", names.count("page_view") == 1, str(names))
check("page_view identifies the endpoint",
      any(p.get("ep") == "companies" for n, p in EMITS if n == "page_view"),
      str([p for n, p in EMITS if n == "page_view"]))

print()
print("=" * 92)
print("THE PAYLOAD")
print("=" * 92)
blob = json.loads(re.search(r'<script id="codata" type="application/json">(.*?)</script>',
                            body, re.S).group(1))
meta = json.loads(re.search(r'<script id="cometa" type="application/json">(.*?)</script>',
                            body, re.S).group(1))
check("rows parse", isinstance(blob, list) and len(blob) > 0, "%d rows" % len(blob))
check("every row has 9 fields", all(len(r) == 9 for r in blob))
check("sectors list is non-empty", len(meta.get("sectors") or []) > 0,
      "%d sectors" % len(meta.get("sectors") or []))
check("no duplicate or empty sector",
      len(set(meta["sectors"])) == len(meta["sectors"]) and all(meta["sectors"]))
check("sector indexes are in range",
      all(-1 <= r[1] < len(meta["sectors"]) for r in blob))

by_name = {r[0]: r for r in blob}

# The ladder must never invent a URL. "" or absolute http(s), nothing else -- and a prefix code
# must resolve against a prefix the page actually shipped.
bad = []
for r in blob:
    v = r[2]
    if not v:
        continue
    if "|" in v and v.split("|", 1)[0] in (meta.get("prefix") or {}):
        continue
    if not v.startswith("http"):
        bad.append((r[0], v))
check("every careers value is empty, a known prefix code, or absolute http",
      not bad, str(bad[:3]))

print()
print("=" * 92)
print("THE ?c= SPELLING  (the bug that is invisible by eye)")
print("=" * 92)
acc = by_name.get("Accenture")
check("Accenture is in the directory", acc is not None)
if acc:
    check("its open-role count comes from the corpus, closed row excluded",
          acc[7] == 2, "live=%s" % acc[7])
    check("it links as the CORPUS spelling, not the registry one",
          acc[8] == "Accenture LLP", "?c=%s" % acc[8])
    # And prove the link actually resolves, which is the whole point.
    rc = client().get("/company?c=" + acc[8].replace(" ", "%20"))
    check("that link renders a page with a non-zero role count",
          rc.status_code == 200 and b"0 open roles" not in rc.data, str(rc.status_code))

co = by_name.get("Corpusonly Inc")
check("a corpus-only employer still reaches the directory", co is not None)
if co:
    check("an is_active=None row counts as open", co[7] == 1, "live=%s" % co[7])

print()
print("=" * 92)
print("APPLY-DIRECT  (ats_type='direct')")
print("=" * 92)
import scraper
custom = scraper.custom_sources() or []
check("'direct' is not a scraper key", "direct" not in scraper.SCRAPERS)
check("the direct row is INVISIBLE to custom_sources, so no scrape guard is needed",
      not any(c[2] == "Directonly Ltd" for c in custom),
      "custom_sources -> %s" % [c[2] for c in custom])
check("the real board IS visible to custom_sources",
      any(c[2] == "Newco" for c in custom))
check("the direct company still reaches the directory",
      "Directonly Ltd" in by_name)
if "Directonly Ltd" in by_name:
    check("and it carries the pasted URL as its careers link",
          by_name["Directonly Ltd"][2] == "https://directonly.test/careers",
          by_name["Directonly Ltd"][2])

print()
print("=" * 92)
print("/company ENRICHMENT")
print("=" * 92)
links = web._company_links("Accenture")
check("_company_links returns a linkedin url for a known name",
      links["linkedin"].startswith("https://www.linkedin.com/jobs/search/"))
unknown = web._company_links("Nobody Has Heard Of This Ltd")
check("an unknown name still gets a linkedin url",
      unknown["linkedin"].startswith("https://www.linkedin.com/jobs/search/") and
      unknown["careers"] == "")
rc = client().get("/company?c=Directonly%20Ltd")
check("a ZERO-role employer still renders its page", rc.status_code == 200, str(rc.status_code))
cbody = rc.data.decode("utf-8", "replace")
check("and shows the LinkedIn link", "linkedin.com/jobs/search" in cbody)
check("and links back to the directory", 'href="/companies"' in cbody)

print()
print("=" * 92)
print("THE SECTOR MAP")
print("=" * 92)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_companies as B

check("SECTORS has no duplicate", len(set(B.SECTORS)) == len(B.SECTORS))
check("every CURATED value is a real sector",
      all(v in B.SECTORS for v in B.CURATED.values()),
      str(sorted({v for v in B.CURATED.values() if v not in B.SECTORS})[:3]))
# The rule layer must beat keywords, or a university whose name contains "systems" would be
# filed under IT services and lose its cap-exempt section.
s, src = B._sector("Emory University", core.norm_company("Emory University"), True, False)
check("a cap-exempt university resolves by RULE, not keyword",
      (s, src) == ("Universities & Research", "rule"), "%s / %s" % (s, src))
s, src = B._sector("Mount Sinai Hospital", core.norm_company("Mount Sinai Hospital"), True, False)
check("a cap-exempt hospital splits into its own sector",
      (s, src) == ("Hospitals & Health Systems", "rule"), "%s / %s" % (s, src))
s, src = B._sector("Amazon", "amazon", False, False)
check("CURATED wins over keywords", src == "curated", "%s / %s" % (s, src))
s, src = B._sector("Zzz Unclassifiable Qqq", "zzz unclassifiable qqq", False, False)
check("an unmatched name is Unsorted, not force-fitted",
      (s, src) == (B.UNSORTED, "unsorted"), "%s / %s" % (s, src))

print()
if FAILS:
    print("FAILURES (%d):" % len(FAILS))
    for f in FAILS:
        print("   %s" % f)
    raise SystemExit(1)
print("ALL COMPANIES PAGE CHECKS PASS")
