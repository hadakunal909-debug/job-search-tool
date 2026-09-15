"""Paycom's public career portal API, using its own anonymous visitor context.

Visitor tokens remain in memory, are restricted to the portal's service host,
and expire from our cache after five minutes. No candidate login is used.
"""
import json
import re
import threading
import time
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from scraper.infosys import PartialScrapeError

HOSTS = {"www.paycomonline.net", "paycomonline.net"}
SERVICE = "https://portal-applicant-tracking.us-cent.paycomonline.net/"
_contexts = {}
_lock = threading.Lock()


def board_url(url):
    p = urlparse(url)
    match = re.match(r"^/v4/ats/web.php/portal/([A-Fa-f0-9]{32})(?:/|$)", p.path)
    if p.hostname not in HOSTS or not match:
        raise ValueError("Not a Paycom career portal")
    return "https://www.paycomonline.net/v4/ats/web.php/portal/" + match[1].upper() + "/career-page"


def parse_context(html):
    match = re.search(r"var\s+configsFromHost\s*=\s*", html)
    if not match:
        raise ValueError("Paycom visitor context is absent")
    data, _ = json.JSONDecoder().raw_decode(html[match.end():])
    config = json.loads(data.get("libConfig", "{}"))
    if config.get("atsPortalMantleServiceUrl") != SERVICE:
        raise ValueError("Unrecognized Paycom service host")
    token = data.get("sessionJWT")
    if not isinstance(token, str) or not token:
        raise ValueError("Paycom visitor token is absent")
    return token


def context(url):
    import scraper
    board = board_url(url)
    with _lock:
        cached = _contexts.get(board)
        if cached and time.monotonic() - cached[0] < 300:
            return cached[1]
    response = scraper.SESSION.get(board, timeout=20)
    response.raise_for_status()
    token = parse_context(response.text)
    with _lock:
        # Bound memory even when a worker visits thousands of employers.
        if len(_contexts) >= 64:
            _contexts.pop(next(iter(_contexts)))
        _contexts[board] = (time.monotonic(), token)
    return token


def request_json(url, endpoint, payload=None):
    import scraper
    headers = {"Authorization": context(url), "Locale": "en-US"}
    target = SERVICE + "api/ats/" + endpoint
    if payload is None:
        response = scraper.SESSION.get(target, headers=headers, timeout=20)
    else:
        response = scraper.SESSION.post(target, headers=headers, json=payload, timeout=20)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("Paycom response is not an object")
    return data


def listing_page(url, offset=0, page_size=100):
    filters = {k: [] for k in ("workEnvironments", "positionTypes", "educationLevels", "categories", "travelTypes", "shiftTypes", "otherFilters")}
    filters.update(distanceFrom=0, keywordSearchText="", location="", sortOption="")
    data = request_json(url, "job-posting-previews/search", {"skip": offset, "take": page_size, "filtersForQuery": filters})
    jobs, total = data.get("jobPostingPreviews"), data.get("jobPostingPreviewsCount")
    if not isinstance(jobs, list) or isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise ValueError("Paycom listing schema or count is missing")
    return jobs, total


def scrape_paycom(url):
    rows, seen, offset = [], set(), 0
    board = board_url(url).removesuffix("/career-page")
    try:
        for _ in range(100):
            jobs, total = listing_page(url, offset)
            fresh = 0
            for job in jobs:
                jid = str(job.get("jobId", ""))
                if not re.fullmatch(r"\d+", jid) or not job.get("jobTitle"):
                    raise ValueError("Paycom posting lacks a public ID or title")
                if jid in seen:
                    continue
                seen.add(jid)
                fresh += 1
                loc = job.get("locations") or ""
                if job.get("remoteType") and "remote" not in loc.lower():
                    loc = (loc + " | " + job["remoteType"]).strip(" |")
                rows.append({"url": board + "/jobs/" + jid, "title": job["jobTitle"],
                             "location": loc, "source": "paycom"})
            if len(rows) >= total:
                return rows
            if not fresh:
                raise ValueError("Paycom page repeated or ended before its advertised total")
            offset += len(jobs)
        raise ValueError("Paycom exceeded the 100-page safety cap")
    except Exception as exc:
        raise PartialScrapeError("Paycom listing incomplete: " + type(exc).__name__, rows) from exc


def detail_jd(url):
    match = re.search(r"/jobs/(\d+)$", urlparse(url).path)
    if not match:
        return "", ""
    data = request_json(url, "job-postings/" + match[1]).get("jobPosting") or {}
    if str(data.get("jobId", "")) != match[1]:
        return "", ""
    text = "\n\n".join(BeautifulSoup(data.get(k) or "", "html.parser").get_text("\n", strip=True)
                         for k in ("description", "qualifications"))
    return text.strip(), ""


def identity_text(url):
    data = request_json(url, "company-name")
    return data.get("companyName") or ""
