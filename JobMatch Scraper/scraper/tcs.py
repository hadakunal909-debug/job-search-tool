"""Tata Consultancy Services' public iBegin US search and detail APIs.

The official US careers page links to BOARD. Country selection is stored in an
anonymous session, so use a dedicated session rather than the global cookie jar.
The site's Angular JobSearchService uses ten results per numbered page; no
candidate login is required for search or descriptions.
"""
import re
import threading
import time
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from scraper.infosys import PartialScrapeError

HOST = "ibegin.tcsapps.com"
BASE = "https://" + HOST + "/candidate/"
BOARD = BASE + "?geography=US&language=EN"
COMPANY = "Tata Consultancy Services"
MAX_PAGES = 1000
_local = threading.local()


def _session():
    """An anonymous US session per worker, refreshed after ten minutes."""
    cached = getattr(_local, "context", None)
    if cached and time.monotonic() - cached[0] < 600:
        return cached[1]
    if cached:
        cached[1].close()
        del _local.context
    import scraper
    session = scraper._make_session()
    try:
        response = session.get(BOARD, headers=scraper.HEADERS, timeout=25,
                               allow_redirects=False)
        response.raise_for_status()
        body = BeautifulSoup(response.text, "html.parser").find("body")
        if body is None or body.get("data-country") != "US" or body.get("data-country-id") != "230":
            raise ValueError("TCS did not establish the public US careers context")
    except Exception:
        session.close()
        raise
    _local.context = (time.monotonic(), session)
    return session


def _request(endpoint, payload):
    if endpoint not in ("jobs/searchJ", "job/desc", "job/desc/walkin"):
        raise ValueError("Unsupported TCS public endpoint")
    response = _session().post(BASE + "api/v1/" + endpoint, json=payload,
                               timeout=25, allow_redirects=False)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or data.get("result") != "Y" or not isinstance(data.get("data"), dict):
        raise ValueError("TCS public API did not return a successful response")
    return data["data"]


def listing_page(page=1):
    payload = {key: None for key in (
        "jobTitle", "jobCity", "jobFunction", "jobExperience", "jobSkill",
        "jobTitleOrder", "jobCityOrder", "jobFunctionOrder", "jobExperienceOrder", "applyByOrder")}
    payload.update(pageNumber=str(page), userText="", regular=True, walkin=True)
    data = _request("jobs/searchJ", payload)
    jobs, total = data.get("jobs"), data.get("totalJobs")
    if not isinstance(jobs, list) or isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise ValueError("TCS listing schema or total is missing")
    return jobs, total


def _row(job):
    if not isinstance(job, dict):
        raise ValueError("TCS listing is not an object")
    jid, title, location = job.get("id"), job.get("jobTitle"), job.get("location")
    if (not isinstance(jid, str) or not re.fullmatch(r"\d+[JW]", jid, re.I)
            or not isinstance(title, str) or not title.strip()
            or not isinstance(location, str) or not location.strip()):
        raise ValueError("TCS listing lacks an ID, title or location")
    return {"url": BASE + "jobs/" + jid.upper() + "?geography=US&language=EN",
            "title": title.strip(), "company": COMPANY,
            "location": location.strip(), "source": "tcs"}


def scrape_tcs(board_url=BOARD):
    if urlparse(board_url).hostname != HOST:
        raise ValueError("Not the official TCS iBegin portal")
    rows, seen, expected = [], set(), 0
    try:
        for page in range(1, MAX_PAGES + 1):
            jobs, total = listing_page(page)
            expected = max(expected, total)
            fresh = 0
            for job in jobs:
                row = _row(job)
                if row["url"] in seen:
                    continue
                seen.add(row["url"])
                rows.append(row)
                fresh += 1
            if len(rows) >= expected:
                return rows
            if not fresh:
                raise ValueError("TCS search repeated or ended before its advertised total")
        raise ValueError("TCS search exceeded the %d-page safety cap" % MAX_PAGES)
    except Exception as exc:
        raise PartialScrapeError("TCS listing incomplete: " + str(exc), rows) from exc


def detail_jd(url):
    """Return description and no posting date: applyby is a deadline, not a date posted."""
    parsed = urlparse(url)
    match = re.fullmatch(r"/candidate/jobs/(\d+)([JW])/?", parsed.path, re.I)
    if parsed.hostname != HOST or not match:
        return "", ""
    endpoint = "job/desc/walkin" if match[2].upper() == "W" else "job/desc"
    data = _request(endpoint, {"jobId": match[1]})
    if str(data.get("jobId")) != match[1] or data.get("country") != "United States":
        return "", ""
    parts = []
    for label, key in (("", "description"), ("Qualifications", "qualifications"),
                       ("Experience", "experience"), ("Desired skills", "skilldetail"),
                       ("Additional information", "additionalInfo")):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            clean = BeautifulSoup(value, "html.parser").get_text("\n", strip=True)
            parts.append((label + ": " if label else "") + clean)
    salary = [str(data[key]).strip() for key in ("minSalary", "maxSalary")
              if data.get(key) is not None and str(data[key]).strip()]
    if salary:
        parts.append("Salary range: " + " - ".join(salary) + " per year")
    return "\n\n".join(parts), ""
