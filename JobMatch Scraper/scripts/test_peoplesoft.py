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

# Shared installations must keep the institutional SiteId throughout a run.
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
USG = "https://careers.hprod.onehcm.usg.edu/psc/careers/CAREERS/HRMS/c/HRS_HRAM_FL.HRS_CG_SEARCH_FL.GBL"
check("detect keeps institution SiteId", scraper.detect_board(USG + "?FOCUS=Applicant&SiteId=03000")[0], USG + "?SiteId=03000")
check("different campus has different canonical board", scraper.detect_board(USG + "?SiteId=30000")[0], USG + "?SiteId=30000")
check("default SiteId keeps existing canonical boards", scraper.detect_board(FSU + "?SiteId=1")[0], FSU)
check("conflicting SiteId is rejected", scraper.detect_board(USG + "?SiteId=03000&SiteId=30000"), None)
check("non-numeric SiteId is rejected", scraper.detect_board(USG + "?SiteId=unknown"), None)

class FakeResponse:
    status_code = 200
    text = GRID + " 2 jobs found"

with patch.object(scraper, "_safe_get", return_value=FakeResponse()) as get:
    scoped_rows = scraper.scrape_peoplesoft(USG + "?SiteId=03000")
    check("scoped board still produces public jobs", len(scoped_rows), 2)
    check("guest and listing requests keep institution", [parse_qs(urlparse(c.args[0]).query).get("SiteId") for c in get.call_args_list], [["03000"], ["03000"]])
    check("every posting link keeps institution", [parse_qs(urlparse(r["url"]).query).get("SiteId") for r in scoped_rows], [["03000"], ["03000"]])

class IncompleteResponse:
    status_code = 200
    text = GRID + " 3 jobs found"

from scraper.infosys import PartialScrapeError
with patch.object(scraper, "_safe_get", return_value=IncompleteResponse()), patch.object(scraper, "_safe_form_post", return_value=IncompleteResponse()):
    try:
        scraper.scrape_peoplesoft(USG + "?SiteId=03000")
        check("incomplete known total is not a successful run", True, False)
    except PartialScrapeError as exc:
        check("incomplete known total retains readable jobs", len(exc.rows), 2)

# The current New Mexico/Texas pages explicitly cap the unfiltered result grid
# at 500. Their own exhaustive salary checkboxes produce smaller complete grids.
from types import SimpleNamespace
import requests


def grid(ids, total, facets='', cap=None, employer=''):
    rows = ''.join("<span id='SCH_JOB_TITLE$%s'>Project Analyst %s</span>"
                   "<span id='HRS_APP_JBSCH_I_HRS_JOB_OPENING_ID$%s'>%s</span>"
                   "<span id='TX_TAM_JSCH_WRK_TX_TAM_AGENCY_NAME$%s'>%s</span>"
                   % (i, jid, i, jid, i, employer) for i, jid in enumerate(ids))
    return rows + '<b>%s</b> jobs found.' % total + (
        ' Only the first <b>%s</b> jobs can be displayed.' % cap if cap else '') + facets


def facet(heading, options):
    return '<div class="facet_group_box"><h2>'+heading+'</h2>' + ''.join(
        '<input type="checkbox" id="PTS_SELECT$%s" title="%s (%s)">' % (i,label,count)
        for i,label,count in options) + '</div>'


def response(text):
    return SimpleNamespace(status_code=200,text=text)


parts = facet('Maximum Salary Rate', [(0,'Low',250),(1,'High',252)])
initial = grid([1],502,parts,500)
low = grid(range(1,251),250,facet('Maximum Salary Rate',[(40,'Low',250),(41,'High',252)]))
high = grid(range(251,503),252,facet('Maximum Salary Rate',[(70,'Low',250),(71,'High',252)]))
with patch.object(scraper,'_safe_get',return_value=response(initial)), patch.object(scraper,'_safe_form_post',side_effect=[
        response('<FIELD><![CDATA['+low+']]></FIELD>'),response(initial),
        response('<FIELD><![CDATA['+high+']]></FIELD>'),response(initial)]) as post, patch.object(scraper.time,'sleep'):
    complete = scraper.scrape_peoplesoft(FSU)
    check('server cap recovered by exhaustive public facets',len(complete),502)
    check('partition union includes final posting',parse_qs(urlparse(complete[-1]['url']).query)['JobOpeningId'],['502'])
    check('facet actions follow current IDs including reset',[c.args[1]['ICAction'] for c in post.call_args_list],
          ['PTS_SELECT$0','PTS_SELECT$40','PTS_SELECT$1','PTS_SELECT$71'])
    check('reset submits the unchecked public control',post.call_args_list[1].args[1]['PTS_SELECT$chk$40'],'N')
    check('interaction state is carried across partitions',[c.args[1]['ICStateNum'] for c in post.call_args_list],['2','3','4','5'])

overlap = facet('Salary',[(0,'Low',2),(1,'High',2)])
initial_overlap = grid([1],4,overlap,2)
with patch.object(scraper,'_safe_get',return_value=response(initial_overlap)), patch.object(scraper,'_safe_form_post',side_effect=[
        response(grid([1,2],2,overlap)),response(initial_overlap),
        response(grid([2,3],2,overlap)),response(initial_overlap)]), patch.object(scraper.time,'sleep'):
    try:
        scraper.scrape_peoplesoft(FSU)
        check('overlapping facets cannot declare completeness',True,False)
    except PartialScrapeError as exc:
        check('overlapping facet union remains partial',len(exc.rows),3)

with patch.object(scraper,'_safe_get',return_value=response(grid([1],3))), patch.object(scraper,'_safe_form_post',side_effect=requests.Timeout('offline')):
    try:
        scraper.scrape_peoplesoft(FSU)
        check('transport failure must retain partial rows',True,False)
    except PartialScrapeError as exc:
        check('transport failure retains prior public rows',len(exc.rows),1)

DPS = 'https://erphcmprd.cpa.texas.gov/psc/candidateportal' + scraper.PEOPLESOFT_GBL
SCOPE = DPS + '?Company=Texas+Department+of+Public+Safety'
check('canonical detection preserves explicit Company scope',scraper.detect_board(SCOPE)[0],SCOPE)
check('canonical detection preserves Company and SiteId',scraper.detect_board(SCOPE+'&SiteId=2')[0],
      DPS+'?SiteId=2&Company=Texas+Department+of+Public+Safety')
for query in ('?Company=', '?Company=DPS&Company=Other'):
    check('blank/conflicting Company selector is rejected '+query,scraper.detect_board(DPS+query),None)
company_facet = facet('Company',[(2,'TEXAS DEPARTMENT OF PUBLIC SAFETY',2),(3,'OTHER AGENCY',5)])
with patch.object(scraper,'_safe_get',return_value=response(grid([1],7,company_facet))), patch.object(scraper,'_safe_form_post',return_value=response(
        grid([8,9],2,company_facet,employer='TEXAS DEPARTMENT OF PUBLIC SAFETY'))) as post, patch.object(scraper.time,'sleep'):
    selected = scraper.scrape_peoplesoft(SCOPE)
    check('Company selector retrieves only its public facet',len(selected),2)
    check('Company selector retains exact registered employer',[r['company'] for r in selected],['Texas Department of Public Safety']*2)
    check('Company selector invokes observed checkbox',post.call_args.args[1]['ICAction'],'PTS_SELECT$2')

for label,text in [('missing facet',grid([1],7)),('wrong public company',grid([1],7,facet('Company',[(0,'OTHER AGENCY',7)])))]:
    with patch.object(scraper,'_safe_get',return_value=response(text)), patch.object(scraper,'_safe_form_post') as post:
        try:
            scraper.scrape_peoplesoft(SCOPE)
            check(label+' must not fall back statewide',True,False)
        except PartialScrapeError as exc:
            check(label+' cannot emit other agencies',exc.rows,[])
            check(label+' sends no guessed checkbox action',post.call_count,0)

with patch.object(scraper,'_safe_get',return_value=response(grid([1],7,company_facet))), patch.object(scraper,'_safe_form_post',return_value=response(
        grid([1,2,3],7,company_facet,employer='OTHER AGENCY'))), patch.object(scraper.time,'sleep'):
    try:
        scraper.scrape_peoplesoft(SCOPE)
        check('ignored Company filter must fail its advertised count',True,False)
    except PartialScrapeError as exc:
        check('ignored Company filter cannot fall back statewide',exc.rows,[])

for unavailable in (SimpleNamespace(status_code=403,text='Access denied'),response('<html>Sign In</html>')):
    with patch.object(scraper,'_safe_get',return_value=unavailable):
        try:
            scraper.scrape_peoplesoft(FSU)
            check('unavailable guest grid cannot be a healthy empty board',True,False)
        except PartialScrapeError as exc:
            check('unavailable guest grid remains a failed empty read',exc.rows,[])

with patch.object(scraper,'_safe_get',return_value=response(grid([1],7,company_facet))), patch.object(scraper,'_safe_form_post',return_value=response(
        grid([8,9],2,company_facet,employer='OTHER AGENCY'))), patch.object(scraper.time,'sleep'):
    try:
        scraper.scrape_peoplesoft(SCOPE)
        check('wrong returned employer must fail selected scope',True,False)
    except PartialScrapeError as exc:
        check('wrong returned employer is never admitted',exc.rows,[])

with patch.object(scraper,'scrape_peoplesoft',return_value=selected) as scoped_scrape:
    check('probe counts the complete selected Company',scraper.probe_board(SCOPE+'&SiteId=2','peoplesoft'),2)
    check('probe preserves Company and SiteId unchanged',scoped_scrape.call_args.args,(SCOPE+'&SiteId=2',))
with patch.object(scraper,'scrape_peoplesoft',side_effect=PartialScrapeError('scope incomplete',selected)):
    check('probe cannot present partial scope as a healthy count',scraper.probe_board(SCOPE,'peoplesoft'),None)
with patch.object(scraper,'scrape_peoplesoft',return_value=[]):
    check('probe preserves a complete explicitly empty board',scraper.probe_board(SCOPE,'peoplesoft'),0)

print("\n%s" % ("All %d PeopleSoft checks passed." % len(ran)
                if not fails else "%d of %d FAILED: %s" % (len(fails), len(ran), ", ".join(fails))))
sys.exit(1 if fails else 0)
