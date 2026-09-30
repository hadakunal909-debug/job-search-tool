"""Dayforce's public external career boards and server-rendered posting details.

The application's search API requires its normal anonymous CSRF cookie/header
pair. Obtain it from the public endpoint in the same session; never use an
applicant account. Keep both the client namespace and career-site code so a
shared Dayforce host cannot silently select another employer's board.
"""
import json
import re
import threading
from functools import wraps
from html import unescape
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from scraper.infosys import PartialScrapeError

HOST = "jobs.dayforcehcm.com"
MAX_PAGES = 100
_SESSION_LOCK = threading.RLock()


def _public_session(fn):
    """Keep the shared session's anonymous CSRF cookie stable for a full board."""
    @wraps(fn)
    def locked(*args, **kwargs):
        with _SESSION_LOCK:
            return fn(*args, **kwargs)
    return locked


def board_parts(url):
    p = urlparse(unescape(url))
    if p.scheme not in ("https", "http") or p.hostname != HOST or p.username or p.password or p.port:
        raise ValueError("Not a public Dayforce career board")
    parts = p.path.strip("/").split("/")
    language = "en-US"
    if parts and re.fullmatch(r"[a-z]{2}-[A-Za-z]{2,4}", parts[0]):
        language = parts.pop(0)
    if len(parts) not in (2, 4) or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", s) for s in parts[:2]):
        raise ValueError("Dayforce client and career site are required")
    if len(parts) == 4 and (parts[2] != "jobs" or not re.fullmatch(r"[0-9]+", parts[3])):
        raise ValueError("Not a public Dayforce posting")
    return language, parts[0], parts[1]


def board_url(url):
    language, client, site = board_parts(url)
    return "https://" + HOST + "/" + language + "/" + client + "/" + site


def owns_url(board, posting):
    try:
        return tuple(s.lower() for s in board_parts(board)[1:]) == tuple(s.lower() for s in board_parts(posting)[1:])
    except ValueError:
        return False


@_public_session
def page_data(url):
    import scraper
    board_parts(url)
    response = scraper.SESSION.get(url, timeout=20)
    response.raise_for_status()
    if not owns_url(url, response.url):
        raise ValueError("Dayforce redirect changed the employer board")
    script = BeautifulSoup(response.text, "html.parser").select_one("script#__NEXT_DATA__")
    if script is None:
        raise ValueError("Dayforce public page data is absent")
    data = json.loads(script.string or script.get_text())
    query = data.get("query") or {}
    _, client, site = board_parts(url)
    if str(query.get("clientNamespace", "")).lower() != client.lower() or str(query.get("careerSiteXRefCode", "")).lower() != site.lower():
        raise ValueError("Dayforce page belongs to a different employer board")
    return data


def site_info(data, url):
    _, client, site = board_parts(url)
    props = (data.get("props") or {}).get("pageProps") or {}
    queries = (props.get("dehydratedState") or {}).get("queries") or []
    for query in queries:
        key = query.get("queryKey") or []
        if key and key[0] == "site-info":
            state = query.get("state") or {}
            info = state.get("data") or {}
            if (state.get("status") != "success" or info.get("isDisabled") is True
                    or str(info.get("clientNamespace", "")).lower() != client.lower()
                    or str(info.get("jobBoardCode", "")).lower() != site.lower()
                    or type(info.get("jobBoardId")) is not int or info["jobBoardId"] <= 0):
                raise ValueError("Dayforce external board context is invalid")
            return info
    raise ValueError("Dayforce board context is absent")


@_public_session
def anonymous_csrf():
    import scraper
    response = scraper.SESSION.get("https://" + HOST + "/api/auth/csrf", timeout=20)
    response.raise_for_status()
    data = response.json()
    token = data.get("csrfToken") if isinstance(data, dict) else None
    if not isinstance(token, str) or not re.fullmatch(r"[a-fA-F0-9]{32,128}", token):
        raise ValueError("Dayforce anonymous CSRF context is absent")
    return token


@_public_session
def listing_page(url, info, token, offset=0):
    import scraper
    language, client, site = board_parts(url)
    response = scraper.SESSION.post("https://" + HOST + "/api/geo/" + client + "/jobposting/search",
        json={"clientNamespace": client, "jobBoardCode": site, "cultureCode": language,
              "distanceUnit": 1, "paginationStart": offset},
        headers={"X-CSRF-TOKEN": token, "Referer": board_url(url)}, timeout=20)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("Dayforce returned a non-object listing")
    jobs, total = data.get("jobPostings"), data.get("maxCount")
    if (not isinstance(jobs, list) or type(total) is not int or total < 0
            or type(data.get("offset")) is not int or data["offset"] != offset
            or type(data.get("count")) is not int or data["count"] != len(jobs)):
        raise ValueError("Dayforce listing schema, total or offset is invalid")
    return jobs, total


def parse_job(job, url, info):
    _, client, _ = board_parts(url)
    if (not isinstance(job, dict) or type(job.get("jobPostingId")) is not int or job["jobPostingId"] <= 0
            or not isinstance(job.get("jobTitle"), str) or not job["jobTitle"].strip()
            or str(job.get("clientNamespace", "")).lower() != client.lower()
            or job.get("jobBoardId") != info["jobBoardId"] or job.get("isInternal") is True):
        raise ValueError("Dayforce posting identity or external scope is invalid")
    locations = []
    for loc in job.get("postingLocations") or []:
        if not isinstance(loc, dict):
            raise ValueError("Dayforce posting location is invalid")
        label = loc.get("formattedAddress") or ", ".join(str(v) for v in
            [loc.get("cityName"), loc.get("stateCode"), loc.get("isoCountryCode")] if v)
        if label and label not in locations:
            locations.append(label)
    return {"title": job["jobTitle"].strip(), "url": board_url(url) + "/jobs/" + str(job["jobPostingId"]),
            "location": " | ".join(locations), "source": "dayforce",
            "found_date": str(job.get("postingStartTimestampUTC") or "")[:10]}


@_public_session
def scrape_dayforce(url):
    rows, seen, offset, expected = [], set(), 0, None
    try:
        url = board_url(url)
        info = site_info(page_data(url), url)
        token = anonymous_csrf()
        for _ in range(MAX_PAGES):
            jobs, total = listing_page(url, info, token, offset)
            if expected is None:
                expected = total
            elif total != expected:
                raise ValueError("Dayforce total changed during pagination")
            fresh = 0
            for job in jobs:
                row = parse_job(job, url, info)
                if row["url"] in seen:
                    continue
                seen.add(row["url"])
                rows.append(row)
                fresh += 1
            if len(rows) > total:
                raise ValueError("Dayforce returned more postings than advertised")
            if jobs and not fresh:
                raise ValueError("Dayforce repeated a page")
            if len(rows) == total:
                return rows
            if not jobs:
                raise ValueError("Dayforce ended before its advertised total")
            offset += len(jobs)
        raise ValueError("Dayforce exceeded its page safety cap")
    except Exception as exc:
        raise PartialScrapeError("Dayforce listing incomplete: " + str(exc)[:160], rows) from exc


@_public_session
def probe_dayforce(url):
    url = board_url(url)
    info = site_info(page_data(url), url)
    jobs, total = listing_page(url, info, anonymous_csrf())
    for job in jobs:
        parse_job(job, url, info)
    if not jobs and total:
        raise ValueError("Dayforce search omitted its advertised postings")
    return total


def detail_jd(url):
    board_parts(url)
    match = re.search(r"/jobs/([0-9]+)/?$", urlparse(url).path)
    if not match:
        return "", ""
    data = page_data(url)
    site_info(data, url)
    props = (data.get("props") or {}).get("pageProps") or {}
    job = props.get("jobData")
    if (not isinstance(job, dict) or str(job.get("jobPostingId")) != match[1]
            or str((data.get("query") or {}).get("id", "")) != match[1]
            or job.get("isInternal") is not False or job.get("postingStatus") != 1):
        return "", ""
    content = job.get("jobPostingContent")
    if not isinstance(content, dict) or not content.get("jobDescription"):
        return "", ""
    html = "\n".join(str(content.get(k) or "") for k in
        ["jobDescriptionHeader", "jobDescription", "jobDescriptionFooter"])
    return BeautifulSoup(html, "html.parser").get_text("\n", strip=True), str(job.get("postingStartTimestampUTC") or "")[:10]


def identity_text(url):
    info = site_info(page_data(board_url(url)), url)
    employer = info.get("candidateCorrespondenceClientName")
    if isinstance(employer, str) and employer.strip():
        return employer.strip()
    links = (info.get("header") or {}).get("headerLinks") or []
    return " | ".join(re.sub(r"\s+Home$", "", str(link.get("pageLinkName") or ""), flags=re.I)
        for link in links if str(link.get("link") or "").startswith("https://"))
