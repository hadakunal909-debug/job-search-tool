"""Tata Consultancy Services' public iBegin US search and detail APIs.

The legacy board URL now opens the public Angular app at /candidate/next/.
Its search and description APIs require an anonymous session and the normal
public CSRF handshake. No candidate login is required. Keep the existing posting
URLs so the upgrade cannot duplicate jobs already stored by the old adapter.
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
NEXT = BASE + "next/"
SEARCH = NEXT + "en-US/jobs/search"
API = NEXT + "api/"
PAGE_SIZE = 50
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
        session.headers.update(dict(scraper.HEADERS, Referer=SEARCH,
                                    Origin="https://" + HOST))
        response = session.get(SEARCH, timeout=25,
                               allow_redirects=False)
        response.raise_for_status()
        # This is the app's own anonymous bootstrap, not a login. The public
        # browser interceptor copies this response header on search POSTs.
        response = session.get(API + "candidate", timeout=25, allow_redirects=False)
        response.raise_for_status()
        _csrf(session, response)
        response = session.get(API + "en-US/landing/init", timeout=25, allow_redirects=False)
        response.raise_for_status()
        _csrf(session, response)
        data = _payload(response)
        country = data.get("currentCountry") or {}
        if (country.get("geoCode") != "US" or country.get("id") != 230 or
                not session.headers.get("NEXT-XSRF-TOKEN")):
            raise ValueError("TCS did not establish the public US careers context")
    except Exception:
        session.close()
        raise
    _local.context = (time.monotonic(), session)
    return session


def _csrf(session, response):
    token = response.headers.get("X-NEXT-XSRF-TOKEN")
    if token:
        session.headers["NEXT-XSRF-TOKEN"] = token


def _payload(response):
    data = response.json()
    if (not isinstance(data, dict) or data.get("result") is not True or
            not isinstance(data.get("payload"), dict)):
        raise ValueError("TCS public API did not return a successful response")
    return data["payload"]


def _request(endpoint, payload):
    if endpoint not in ("jobs/searchJ", "job/desc", "job/desc/walkin"):
        raise ValueError("Unsupported TCS public endpoint")
    session = _session()
    if endpoint == "jobs/searchJ":
        response = session.post(API + "en-US/search/jobs", json=payload,
                                timeout=25, allow_redirects=False)
    else:
        jid = payload.get("jobId")
        if not isinstance(jid, str) or not re.fullmatch(r"\d+", jid):
            raise ValueError("Invalid TCS description ID")
        response = session.get(API + "en-US/" + endpoint + "/" + jid,
                               timeout=25, allow_redirects=False)
    response.raise_for_status()
    _csrf(session, response)
    return _payload(response)


def listing_page(page=1):
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ValueError("TCS request pages are numbered from one")
    payload = {"includeRegularJob": True, "includeWalkIn": True,
               "searchTerms": [], "jobLocationFilters": [], "jobDomainFilters": [],
               "experienceLevelFilters": [], "skillFilters": [],
               "relevancySortOrder": None, "recentSortOrder": "desc",
               "lastDateToApplySortOrder": None, "page": page, "resultsPerPage": PAGE_SIZE}
    data = _request("jobs/searchJ", payload)
    jobs, total, number = data.get("content"), data.get("totalElements"), data.get("number")
    if (not isinstance(jobs, list) or isinstance(total, bool) or
            not isinstance(total, int) or total < 0 or len(jobs) > total or
            isinstance(number, bool) or not isinstance(number, int) or number != page - 1):
        raise ValueError("TCS listing schema or total is missing")
    return jobs, total


def _row(job):
    if not isinstance(job, dict):
        raise ValueError("TCS listing is not an object")
    jid, title, location = job.get("id"), job.get("title") or job.get("jobTitle"), job.get("location")
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
    rows, seen, expected, initial_total = [], set(), 0, None

    def collect(jobs):
        fresh = 0
        for job in jobs:
            row = _row(job)
            if row["url"] in seen:
                continue
            seen.add(row["url"])
            rows.append(row)
            fresh += 1
        return fresh

    try:
        for page in range(1, MAX_PAGES + 1):
            jobs, total = listing_page(page)
            if initial_total is None:
                initial_total = total
            expected = max(expected, total)
            fresh = collect(jobs)
            if total < expected or len(rows) > total:
                raise ValueError("TCS search count changed or unique rows exceeded its total")
            if len(rows) == total:
                return rows
            if not fresh:
                # A newly published job shifts recent-sort page boundaries. The
                # API total increasing is evidence for exactly that case; check
                # the head once for the new IDs, then enforce the same total.
                # Do not mask static duplicates, missing pages or ongoing churn.
                if expected > initial_total and expected - len(rows) <= PAGE_SIZE:
                    head, head_total = listing_page(1)
                    expected = max(expected, head_total)
                    collect(head)
                    if head_total < expected or len(rows) > head_total:
                        raise ValueError("TCS refreshed count changed or unique rows exceeded its total")
                    if len(rows) == head_total:
                        return rows
                raise ValueError("TCS search repeated or ended before its advertised total")
        raise ValueError("TCS search exceeded the %d-page safety cap" % MAX_PAGES)
    except Exception as exc:
        raise PartialScrapeError("TCS listing incomplete: " + str(exc), rows) from exc


def detail_jd(url):
    """Return description and no posting date: applyby is a deadline, not a date posted."""
    parsed = urlparse(url)
    match = re.fullmatch(r"/candidate/(?:next/en-US/)?jobs/(\d+)([JW])/?", parsed.path, re.I)
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
