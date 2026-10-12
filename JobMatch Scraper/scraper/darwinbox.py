"""Darwinbox's public main career portal and anonymous listing interface.

The official application exposes /ms/candidateapi/getCompanyConfig and
job/alljobs with page, limit and companyId. Only the observed external main
portal is supported; applicant accounts and alternate group portals are outside
this adapter. Keep the tenant host, configured subdomain, public careers marker
and advertised total together so a readable response cannot hide a wrong board
or truncated listing. Descriptions arrive with the public listing itself.
"""
import datetime
from html import unescape
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from scraper.infosys import PartialScrapeError

PAGE_SIZE = 10
MAX_PAGES = 100
_ID = re.compile(r"[A-Za-z0-9_-]{1,100}")
_PATH = "/ms/candidatev2/main/careers/"


def board_url(url):
    p = urlparse(unescape(url))
    host = p.hostname or ""
    if (p.scheme != "https" or p.username or p.password or p.port
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}\.darwinbox\.com", host)
            or p.fragment):
        raise ValueError("Not a public Darwinbox tenant")
    if p.path.rstrip("/") not in (_PATH + "allJobs", _PATH + "home"):
        match = re.fullmatch(re.escape(_PATH) + r"jobDetails/([A-Za-z0-9_-]{1,100})/?", p.path)
        if not match:
            raise ValueError("Only the public Darwinbox main career portal is supported")
    return "https://" + host + _PATH + "allJobs"


def _json(url, endpoint, payload=None, params=None):
    import scraper
    url = board_url(url)
    origin = "https://" + urlparse(url).hostname
    target = origin + "/ms/candidateapi/" + endpoint
    headers = {"Accept": "application/json, text/plain, */*",
               "Accept-Language": "en", "Referer": url}
    if payload is None:
        response = scraper.SESSION.get(target, params=params, headers=headers,
                                       timeout=25, allow_redirects=False)
    else:
        response = scraper.SESSION.post(target, json=payload, headers=headers,
                                        timeout=25, allow_redirects=False)
    response.raise_for_status()
    final = urlparse(response.url)
    if (response.status_code != 200 or final.scheme != "https"
            or final.netloc != urlparse(url).netloc
            or final.path != "/ms/candidateapi/" + endpoint):
        raise ValueError("Darwinbox public API redirect or tenant changed")
    data = response.json()
    if not isinstance(data, dict) or data.get("status") != "success":
        raise ValueError("Darwinbox public API did not report success")
    return data


def company_info(url):
    url = board_url(url)
    data = _json(url, "getCompanyConfig", params={"is_preview": "false", "companyId": "main"})
    message = data.get("message")
    company = message.get("company") if isinstance(message, dict) else None
    if (not isinstance(company, dict) or company.get("recruitment_enabled") is not True
            or company.get("new_careers") is not True or company.get("is_preview") is not False
            or company.get("subdomain") != urlparse(url).hostname.split(".")[0]
            or not isinstance(company.get("company_name"), str) or not company["company_name"].strip()
            or not isinstance(company.get("id"), str) or not _ID.fullmatch(company["id"])
            or type(company.get("allJobsCount")) is not int or company["allJobsCount"] < 0):
        raise ValueError("Darwinbox public employer context is invalid")
    return company


def listing_page(url, page=1):
    data = _json(url, "job/alljobs", payload={"page": page, "limit": PAGE_SIZE, "companyId": "main"})
    jobs, total = data.get("data"), data.get("job_counts")
    if not isinstance(jobs, list) or type(total) is not int or total < 0:
        raise ValueError("Darwinbox listing schema or total is invalid")
    return jobs, total


def _public_job(job):
    if (not isinstance(job, dict) or not isinstance(job.get("id"), str)
            or not _ID.fullmatch(job["id"]) or not isinstance(job.get("title"), str)
            or not job["title"].strip() or not isinstance(job.get("jd"), str)
            or not job["jd"].strip()):
        raise ValueError("Darwinbox public posting identity or description is invalid")
    posting = job.get("job_posting")
    careers = posting.get("careers") if isinstance(posting, dict) else None
    if (not isinstance(careers, dict) or type(careers.get("from")) not in (int, float)
            or careers["from"] <= 0 or careers.get("source", "careers") != "careers"):
        raise ValueError("Darwinbox posting is outside the public careers scope")
    return job


def _date(job):
    timestamp = job.get("posted_on")
    if type(timestamp) in (int, float) and timestamp > 0:
        return datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc).date().isoformat()
    created = job.get("created_on")
    if isinstance(created, str):
        try:
            return datetime.date.fromisoformat(created[:10]).isoformat()
        except ValueError:
            pass
    return ""


def parse_job(job, url):
    job = _public_job(job)
    location = job.get("officelocation_show_arr")
    if not isinstance(location, str):
        raise ValueError("Darwinbox posting location is invalid")
    jd = BeautifulSoup(unescape(job["jd"]), "html.parser").get_text("\n", strip=True)
    if not jd:
        raise ValueError("Darwinbox posting description is empty after decoding")
    row = {"title": unescape(job["title"]).strip(),
            "url": board_url(url).rsplit("/", 1)[0] + "/jobDetails/" + job["id"],
            "location": unescape(location).strip(),
            "jd": jd,
            "source": "darwinbox"}
    date = _date(job)
    if date:
        row["found_date"] = date
    return row


def scrape_darwinbox(url):
    rows, seen = [], set()
    try:
        url = board_url(url)
        expected = company_info(url)["allJobsCount"]
        for page in range(1, MAX_PAGES + 1):
            jobs, total = listing_page(url, page)
            if total != expected:
                raise ValueError("Darwinbox advertised total changed during pagination")
            fresh = 0
            for job in jobs:
                row = parse_job(job, url)
                if row["url"] in seen:
                    continue
                seen.add(row["url"])
                rows.append(row)
                fresh += 1
            if len(rows) > total:
                raise ValueError("Darwinbox returned more postings than advertised")
            if jobs and not fresh:
                raise ValueError("Darwinbox repeated a page")
            if len(rows) == total:
                return rows
            if not jobs:
                raise ValueError("Darwinbox ended before its advertised total")
        raise ValueError("Darwinbox exceeded its page safety cap")
    except Exception as exc:
        raise PartialScrapeError("Darwinbox listing incomplete: " + str(exc)[:160], rows) from exc


def identity_text(url):
    return company_info(url)["company_name"].strip()


def detail_jd(url, expected_title=None):
    """Require the exact public ID, and the supplied title when available."""
    board = board_url(url)
    match = re.fullmatch(re.escape(_PATH) + r"jobDetails/([A-Za-z0-9_-]{1,100})/?", urlparse(url).path)
    if not match:
        return "", ""
    company_info(board)
    data = _json(board, "job/" + match[1], params={"companyId": "main"})
    message = data.get("message")
    jobs = message.get("job") if isinstance(message, dict) else None
    if not isinstance(jobs, list) or len(jobs) != 1:
        return "", ""
    job = _public_job(jobs[0])
    if (job["id"] != match[1] or (expected_title is not None
            and unescape(job["title"]).strip() != expected_title.strip())):
        return "", ""
    row = parse_job(job, board)
    return row["jd"], row["found_date"]
