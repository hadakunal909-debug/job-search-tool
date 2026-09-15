"""ADP Workforce Now's public career-center listing and description API.

The career page is a JavaScript shell. Its own recruitment bundle calls this API;
no applicant account or browser session is required. Keep tenant and center IDs:
discarding either can silently select another employer or an internal career site.
"""
import re
from html import unescape
from urllib.parse import parse_qs, urlencode, urlparse

from bs4 import BeautifulSoup
from scraper.infosys import PartialScrapeError

HOSTS = {"workforcenow.adp.com", "workforcenow.cloud.adp.com"}
PAGE = "/mascsr/default/mdf/recruitment/recruitment.html"
API = "/mascsr/default/careercenter/public/events/staffing/v1/"


def board_parts(url):
    p = urlparse(unescape(url))
    if p.hostname not in HOSTS or p.path != PAGE:
        raise ValueError("Not an ADP Workforce Now career page")
    q = parse_qs(p.query)
    cid, center = q.get("cid", [""])[0], q.get("ccId", [""])[0]
    if not re.fullmatch(r"[A-Za-z0-9-]{8,80}", cid) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", center):
        raise ValueError("ADP tenant and career center are required")
    return p.hostname, cid, center


def board_url(url):
    host, cid, center = board_parts(url)
    return "https://" + host + PAGE + "?" + urlencode({"cid": cid, "ccId": center})


def request_json(url, endpoint, params=None):
    import scraper
    host, cid, center = board_parts(url)
    query = {"cid": cid, "ccId": center, "locale": "en_US"}
    query.update(params or {})
    # ADPPORTAL is scoped across ADP hosts. A previous employer's cookie makes
    # other tenants return 500; this public API identifies the tenant in cid.
    response = scraper.SESSION.get("https://" + host + API + endpoint,
                                   params=query, headers=dict(scraper.HEADERS, Cookie=""), timeout=20)
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict):
        raise ValueError("ADP returned a non-object response")
    return value


def external_id(job):
    fields = (job.get("customFieldGroup") or {}).get("stringFields", [])
    return next((str(f.get("stringValue", "")) for f in fields
                 if (f.get("nameCode") or {}).get("codeValue") == "ExternalJobID"), "")


def parse_job(job, url):
    jid = external_id(job)
    if not jid or not re.fullmatch(r"\d+", jid) or not job.get("requisitionTitle"):
        raise ValueError("ADP posting lacks its public job ID or title")
    locations = []
    for loc in job.get("requisitionLocations") or []:
        label = (loc.get("nameCode") or {}).get("shortName")
        if not label:
            address = loc.get("address") or {}
            label = ", ".join(str(v) for v in [address.get("cityName"),
                (address.get("countrySubdivisionLevel1") or {}).get("codeValue"),
                (address.get("countryCode") or {}).get("codeValue")] if v)
        if label and label.strip() not in locations:
            locations.append(label.strip())
    return {"title": job["requisitionTitle"].strip(), "url": board_url(url) + "&jobId=" + jid,
            "location": " | ".join(locations), "found_date": (job.get("postDate") or "")[:10],
            "source": "adp", "jd": BeautifulSoup(job.get("requisitionDescription") or "", "html.parser").get_text(" ", strip=True)}


def listing_page(url, offset=0, page_size=100):
    data = request_json(url, "job-requisitions", {"$skip": offset, "$top": page_size})
    jobs, total = data.get("jobRequisitions"), (data.get("meta") or {}).get("totalNumber")
    if not isinstance(jobs, list) or isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise ValueError("ADP listing schema or total is missing")
    return jobs, total


def scrape_adp(url):
    rows, seen, offset = [], set(), 0
    try:
        for _ in range(100):
            jobs, total = listing_page(url, offset)
            page = [parse_job(j, url) for j in jobs]
            fresh = 0
            for row in page:
                if row["url"] in seen:
                    continue
                seen.add(row["url"])
                rows.append(row)
                fresh += 1
            if jobs and not fresh:
                raise ValueError("ADP repeated a page")
            if len(rows) >= total:
                return rows
            if not jobs:
                raise ValueError("ADP ended before the advertised total")
            offset += len(jobs)
        raise ValueError("ADP exceeded the 100-page safety cap")
    except Exception as exc:
        raise PartialScrapeError("ADP listing incomplete: " + str(exc)[:160], rows) from exc


def owns_url(board, posting):
    """ADP tenants share a path; their cid/ccId query values establish ownership."""
    try:
        return board_url(board) == board_url(posting)
    except ValueError:
        return False


def detail_jd(url):
    jid = parse_qs(urlparse(url).query).get("jobId", [""])[0]
    if not re.fullmatch(r"\d+", jid):
        return "", ""
    data = request_json(url, "job-requisitions/" + jid)
    jobs = data.get("jobRequisitions") if "jobRequisitions" in data else [data]
    if len(jobs) != 1 or external_id(jobs[0]) != jid:
        return "", ""
    row = jobs[0]
    return BeautifulSoup(row.get("requisitionDescription") or "", "html.parser").get_text("\n", strip=True), (row.get("postDate") or "")[:10]


def identity_text(url):
    """Public employer welcome text for human identity review before adoption."""
    data = request_json(url, "content-links/career-center")
    return " ".join(BeautifulSoup((r.get("linkTypeCode") or {}).get("longName") or "",
                                  "html.parser").get_text(" ", strip=True)
                    for r in data.get("contentLinks") or [])
