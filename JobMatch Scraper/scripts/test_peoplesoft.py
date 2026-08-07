"""PeopleSoft Candidate Gateway parser checks — no network, so CI can run them.

The live-site behaviour these encode (guest cookie, server-rendered 50-row grid, "show more"
returning the WHOLE grown grid) was confirmed against jobs.omni.fsu.edu on 2026-08-07: 207
postings in 4 hops. What can silently rot is the PARSING — field ids, the MM/DD/YYYY dates,
which URL shapes count as a board — so that is what's pinned here.
"""
import os, sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scraper

fails = []
ran = []


def check(name, got, want):
    ok = got == want
    ran.append(name)
    print("%-4s %-52s %s" % ("ok" if ok else "FAIL", name, "" if ok else "got %r, want %r" % (got, want)))
    if not ok:
        fails.append(name)


# One row of the real FSU grid, trimmed to the cells we read (ids are what PeopleSoft emits).
GRID = (
    "<td><span class='ps_box-value' id='SCH_JOB_TITLE$0'>Program Manager, Media &amp; Comms</span></td>"
    "<td><span class='ps_box-value' id='HRS_APP_JBSCH_I_HRS_JOB_OPENING_ID$0'>63214</span></td>"
    "<td><span class='ps_box-value' id='LOCATION$0'>Tallahassee, FL</span></td>"
    "<td><span class='ps_box-value' id='SCH_OPENED$0'>08/07/2026</span></td>"
    "<td><span class='ps_box-value' id='SCH_JOB_TITLE$1'>Business Analyst</span></td>"
    "<td><span class='ps_box-value' id='HRS_APP_JBSCH_I_HRS_JOB_OPENING_ID$1'>62998</span></td>"
    "<td><span class='ps_box-value' id='LOCATION$1'>Panama City, FL</span></td>"
    "<td><span class='ps_box-value' id='SCH_OPENED$1'>7/29/2026</span></td>"
    "<input type='hidden' name='ICStateNum' id='ICStateNum' value='3' />"
    "<input type='hidden' name='ICSID' id='ICSID' value='abc123' />"
)

rows = scraper._ps_rows(GRID)
check("grid -> 2 rows", len(rows), 2)
check("row order follows the $index", [r["job_id"] for r in rows], ["63214", "62998"])
check("entities decoded in titles", rows[0]["title"], "Program Manager, Media & Comms")
check("location read", rows[1]["location"], "Panama City, FL")
check("no rows from empty html", scraper._ps_rows(""), [])

check("ICStateNum + ICSID read", scraper._ps_state(GRID), ("3", "abc123"))
check("state falls forward when absent", scraper._ps_state("<html/>", "7")[0], "8")

check("MM/DD/YYYY -> ISO", scraper._ps_date("08/07/2026"), "2026-08-07")
check("single-digit month/day padded", scraper._ps_date("7/29/2026"), "2026-07-29")
check("junk date -> ''", scraper._ps_date("Open Until Filled"), "")
check("missing date -> ''", scraper._ps_date(None), "")

FSU = "https://jobs.omni.fsu.edu/psc/sprdhr_er/EMPLOYEE/HRMS/c/HRS_HRAM_FL.HRS_CG_SEARCH_FL.GBL"
check("parts from /psc/", scraper._peoplesoft_parts(FSU + "?Page=HRS_APP_SCHJOB_FL"),
      ("https://jobs.omni.fsu.edu", "sprdhr_er"))
check("parts from /psp/", scraper._peoplesoft_parts(FSU.replace("/psc/", "/psp/")),
      ("https://jobs.omni.fsu.edu", "sprdhr_er"))
check("parts from junk", scraper._peoplesoft_parts("https://example.com/jobs"), (None, None))

check("job url is a resolvable deep link",
      scraper._ps_job_url("https://jobs.omni.fsu.edu", "sprdhr_er", "63214"),
      "https://jobs.omni.fsu.edu/psc/sprdhr_er/EMPLOYEE/HRMS/c/HRS_HRAM_FL.HRS_CG_SEARCH_FL.GBL"
      "?Page=HRS_APP_JBPST_FL&Action=U&FOCUS=Applicant&SiteId=1&JobOpeningId=63214&PostingSeq=1")

# ---- detect_board: every shape a user might paste, and the ones that must NOT match ----
for label, url in (("search page", FSU + "?Page=HRS_APP_SCHJOB_FL&Action=U"),
                   ("one posting", FSU + "?Page=HRS_APP_JBPST_FL&Action=U&JobOpeningId=63214"),
                   ("portal servlet", FSU.replace("/psc/", "/psp/"))):
    got = scraper.detect_board(url)
    check("detect: %s -> board" % label, (got or ("", "", ""))[:2], (FSU, "peoplesoft"))
check("detect: acronym host -> uppercase name", (scraper.detect_board(FSU) or ("", "", ""))[2], "FSU")
check("detect: bare host is not a board", scraper.detect_board("https://jobs.omni.fsu.edu/"), None)
check("detect: other PeopleSoft app is not a board",
      scraper.detect_board("https://ps.example.edu/psc/ps/EMPLOYEE/HRMS/c/OTHER_MENU.OTHER.GBL"), None)
check("peoplesoft is wired into SCRAPERS",
      scraper.SCRAPERS.get("peoplesoft"), scraper.scrape_peoplesoft)

print("\n%s" % ("All %d PeopleSoft checks passed." % len(ran)
                if not fails else "%d of %d FAILED: %s" % (len(fails), len(ran), ", ".join(fails))))
sys.exit(1 if fails else 0)
