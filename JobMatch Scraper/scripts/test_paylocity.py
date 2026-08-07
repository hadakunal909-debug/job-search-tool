"""Paylocity Recruiting parser checks — no network, so CI can run them.

Confirmed live against West Cary Group on 2026-08-07: 3 postings, real published dates, and a
2,347-char description off the posting page. What can silently rot is the parsing — the shape
of the embedded pageData blob, and which URL shapes resolve to a board — so that is what's
pinned here. The slug pattern gets its own case because the first version of that regex was
too loose and matched straight through a closing quote into the surrounding markup.
"""
import os, sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scraper

fails, ran = [], []


def check(name, got, want):
    ok = got == want
    ran.append(name)
    print("%-4s %-54s %s" % ("ok" if ok else "FAIL", name,
                             "" if ok else "got %r, want %r" % (got, want)))
    if not ok:
        fails.append(name)


GUID = "155dc82e-5369-4654-bc29-7289091fe518"
BOARD = "https://recruiting.paylocity.com/recruiting/jobs/All/%s/West-Cary-Group-LLC" % GUID

# Trimmed to the fields the scraper reads, in the shape the live page ships them.
PAGE = ('<script>window.pageData = {"Departments":["All Departments"],"Jobs":['
        '{"JobId":4104790,"JobTitle":"Project Manager","LocationName":"","PublishedDate":'
        '"2026-08-07T13:12:56-05:00","Description":"","JobLocation":{"City":null,"State":null,'
        '"Country":"USA"},"IsRemote":true},'
        '{"JobId":4377172,"JobTitle":"Senior Copywriter","LocationName":"","PublishedDate":'
        '"2026-08-03T12:14:40-05:00","JobLocation":{"City":"Richmond","State":"VA",'
        '"Country":"USA"},"IsRemote":false},'
        '{"JobId":999,"JobTitle":"Berlin Role","LocationName":"","PublishedDate":'
        '"2026-08-01T00:00:00-05:00","JobLocation":{"City":"Berlin","State":null,'
        '"Country":"DEU"},"IsRemote":false}]};</script>')

data = scraper._paylocity_pagedata(PAGE)
check("pageData parses", [j["JobId"] for j in data.get("Jobs", [])], [4104790, 4377172, 999])
check("pageData on a page without it", scraper._paylocity_pagedata("<html/>"), {})
check("pageData on truncated json", scraper._paylocity_pagedata("window.pageData = {\"Jobs\":["), {})

jobs = data["Jobs"]
check("location: remote with null city", scraper._paylocity_location(jobs[0]), "Remote")
check("location: city + state", scraper._paylocity_location(jobs[1]), "Richmond, VA")
check("location: LocationName wins", scraper._paylocity_location(
    {"LocationName": "Hybrid Remote", "IsRemote": True}), "Hybrid Remote")
check("location: nothing known", scraper._paylocity_location({}), "")

# --- URL shapes ---
check("board url built from guid + slug",
      scraper._paylocity_board_url(GUID, "West-Cary-Group-LLC"), BOARD)
check("detect: list url needs no network", scraper.detect_paylocity(BOARD),
      (BOARD, "paylocity", "West Cary Group LLC"))
check("detect: list url without slug",
      (scraper.detect_paylocity("https://recruiting.paylocity.com/recruiting/jobs/All/%s" % GUID)
       or ("", "", ""))[0],
      "https://recruiting.paylocity.com/recruiting/jobs/All/%s" % GUID)
check("detect: another host is not Paylocity",
      scraper.detect_paylocity("https://example.com/recruiting/jobs/All/%s/x" % GUID), None)
check("detect: paylocity home is not a board",
      scraper.detect_paylocity("https://recruiting.paylocity.com/"), None)

# The slug group must stop at the quote when this regex is run over raw HTML, not run on
# through the rest of the tag (the bug that shipped a board URL with markup glued to it).
HTML = ('<a href="/recruiting/jobs/All/%s/West-Cary-Group-LLC" class="breadcrumb-link" '
        'title="All Jobs">All Jobs</a>' % GUID)
m = scraper._PAYLOCITY_ALL_RE.search(HTML)
check("slug stops at the closing quote", (m.group(1), m.group(2)), (GUID, "West-Cary-Group-LLC"))

check("paylocity is wired into SCRAPERS",
      scraper.SCRAPERS.get("paylocity"), scraper.scrape_paylocity)
check("board is in SOURCES", any(a == "paylocity" for _u, a, _c in scraper.SOURCES), True)
check("scrape refuses a non-Paylocity url", scraper.scrape_paylocity("https://example.com"), [])

# --- jr_id is a referral id, not part of the posting's identity ---
check("jr_id dropped by canonical_url",
      scraper.canonical_url("https://recruiting.paylocity.com/Recruiting/Jobs/Details/4104790"
                            "?jr_id=6a76288a67a1ad0bc53c90c6"),
      "https://recruiting.paylocity.com/Recruiting/Jobs/Details/4104790")
check("a real query param still survives",
      scraper.canonical_url("https://example.com/jobs?id=42&jr_id=abc"),
      "https://example.com/jobs?id=42")

print("\n%s" % ("All %d Paylocity checks passed." % len(ran) if not fails
                else "%d of %d FAILED: %s" % (len(fails), len(ran), ", ".join(fails))))
sys.exit(1 if fails else 0)
