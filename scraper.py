#!/usr/bin/env python3
"""
Personal job scraper
--------------------
Visits the career boards you list in SOURCES, pulls the postings, keeps only the
entry-level ones (and, if you give it H1B data, only companies that have sponsored
before), and appends anything NEW to jobs.csv.

Run it:           python scraper.py
Minimum install:  pip install requests beautifulsoup4 lxml
Only if you add a Workday/JS source:  pip install playwright  &&  playwright install chromium
"""

import csv
import json
import os
import sys
import time
import random
import re
import datetime
from urllib.parse import urljoin, urlparse, parse_qs

# Windows terminals default to cp1252 and crash when printing characters that some job
# titles contain (em dashes, non-breaking hyphens, accents). Force UTF-8 stdout so a
# stray character can never abort a scrape mid-run.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import requests
from bs4 import BeautifulSoup

import core   # for required_years() — JD experience parsing

import db   # storage layer: Supabase if configured, else local jobs.csv


# ============================================================
# CONFIG  — this is the part you edit
# ============================================================

# Each entry: (board_url, ats_type, company_name)
# Find a company's board from its careers-page URL (slug = the LAST path segment):
#     https://job-boards.greenhouse.io/SLUG     -> "greenhouse"
#     https://jobs.lever.co/SLUG                -> "lever"
#     https://jobs.ashbyhq.com/SLUG             -> "ashby"
#     https://jobs.smartrecruiters.com/SLUG     -> "smartrecruiters"
#     ...myworkdayjobs.com/...                  -> "workday"  (JS-rendered, needs Playwright)
# The first four read a public JSON API (fast + reliable). Workday needs Playwright.
ATS_BOARDS = [
    # Verified 2026-05-30: each had >=3 entry-level US matches at scrape time.
    # The trailing number is roughly how many it had then; it changes daily.
    # All are well-known H1B sponsors. Prune any company you're not interested in.
    # ---- Greenhouse ----
    ("https://job-boards.greenhouse.io/samsara",     "greenhouse", "Samsara"),      # ~29
    ("https://job-boards.greenhouse.io/stripe",      "greenhouse", "Stripe"),       # ~20
    ("https://job-boards.greenhouse.io/verkada",     "greenhouse", "Verkada"),      # ~17
    ("https://job-boards.greenhouse.io/brex",        "greenhouse", "Brex"),         # ~15
    ("https://job-boards.greenhouse.io/datadog",     "greenhouse", "Datadog"),      # ~12
    ("https://job-boards.greenhouse.io/instacart",   "greenhouse", "Instacart"),    # ~10
    ("https://job-boards.greenhouse.io/sofi",        "greenhouse", "SoFi"),         # ~8
    ("https://job-boards.greenhouse.io/scaleai",     "greenhouse", "Scale AI"),     # ~8
    ("https://job-boards.greenhouse.io/airbnb",      "greenhouse", "Airbnb"),       # ~7
    ("https://job-boards.greenhouse.io/databricks",  "greenhouse", "Databricks"),   # ~6
    ("https://job-boards.greenhouse.io/twilio",      "greenhouse", "Twilio"),       # ~6
    ("https://job-boards.greenhouse.io/robinhood",   "greenhouse", "Robinhood"),    # ~5
    ("https://job-boards.greenhouse.io/toast",       "greenhouse", "Toast"),        # ~5
    ("https://job-boards.greenhouse.io/checkr",      "greenhouse", "Checkr"),       # ~5
    ("https://job-boards.greenhouse.io/affirm",      "greenhouse", "Affirm"),       # ~4
    ("https://job-boards.greenhouse.io/flexport",    "greenhouse", "Flexport"),     # ~4
    ("https://job-boards.greenhouse.io/mongodb",     "greenhouse", "MongoDB"),      # ~3
    ("https://job-boards.greenhouse.io/okta",        "greenhouse", "Okta"),         # ~3
    # ---- Lever ----
    ("https://jobs.lever.co/palantir",               "lever", "Palantir"),          # ~17
    # ---- Ashby ----
    ("https://jobs.ashbyhq.com/ramp",                "ashby", "Ramp"),              # ~16
    ("https://jobs.ashbyhq.com/notion",              "ashby", "Notion"),            # ~8
    ("https://jobs.ashbyhq.com/vanta",               "ashby", "Vanta"),             # ~4
    ("https://jobs.ashbyhq.com/replit",              "ashby", "Replit"),            # ~4
    ("https://jobs.ashbyhq.com/cursor",              "ashby", "Cursor"),            # ~3
    # ---- SmartRecruiters ----
    ("https://jobs.smartrecruiters.com/AveryDennison", "smartrecruiters", "Avery Dennison"),  # ~8
    ("https://jobs.smartrecruiters.com/Experian",      "smartrecruiters", "Experian"),        # ~6

    # Workday boards need Playwright (pip install playwright && playwright install chromium):
    # ("https://acme.wd1.myworkdayjobs.com/careers", "workday", "Acme"),
]

# Amazon's own portal (amazon.jobs): its job search is allowed by robots.txt and has a
# public JSON feed, so scrape_amazon() can read it (no browser needed).
AMAZON = [
    ("https://www.amazon.jobs/en/search?country=USA&loc_query=United+States", "amazon", "Amazon"),
]

# More sponsor companies found (by find_boards.py) to have a scrapeable ATS board:
EXTRA_BOARDS = [
    ("https://job-boards.greenhouse.io/lyft",        "greenhouse", "Lyft"),
    ("https://job-boards.greenhouse.io/pinterest",   "greenhouse", "Pinterest"),
    ("https://job-boards.greenhouse.io/block",       "greenhouse", "Block"),
    ("https://job-boards.greenhouse.io/dropbox",     "greenhouse", "Dropbox"),
    ("https://job-boards.greenhouse.io/linkedin",    "greenhouse", "LinkedIn"),
    ("https://jobs.ashbyhq.com/snowflake",           "ashby", "Snowflake"),
    ("https://jobs.smartrecruiters.com/ServiceNow",  "smartrecruiters", "ServiceNow"),
    ("https://jobs.smartrecruiters.com/Visa",        "smartrecruiters", "Visa"),
    ("https://jobs.smartrecruiters.com/Uber",        "smartrecruiters", "Uber"),
    ("https://jobs.smartrecruiters.com/ByteDance",   "smartrecruiters", "ByteDance"),
    # --- Added 2026-06-01: find_boards.py probe of the DOL sponsor list (20 hits) ---
    ("https://job-boards.greenhouse.io/anaplan",            "greenhouse", "Anaplan"),
    ("https://job-boards.greenhouse.io/celonis",            "greenhouse", "Celonis"),
    ("https://job-boards.greenhouse.io/aurorainnovation",   "greenhouse", "Aurora Innovation"),
    ("https://job-boards.greenhouse.io/alixpartners",       "greenhouse", "AlixPartners"),
    ("https://job-boards.greenhouse.io/worldquant",         "greenhouse", "WorldQuant"),
    ("https://job-boards.greenhouse.io/newrelic",           "greenhouse", "New Relic"),
    ("https://job-boards.greenhouse.io/bitgo",              "greenhouse", "BitGo"),
    ("https://job-boards.greenhouse.io/netcracker",         "greenhouse", "Netcracker"),
    ("https://job-boards.greenhouse.io/maymobility",        "greenhouse", "May Mobility"),
    ("https://job-boards.greenhouse.io/yipitdata",          "greenhouse", "YipitData"),
    ("https://job-boards.greenhouse.io/marqeta",            "greenhouse", "Marqeta"),
    ("https://job-boards.greenhouse.io/enova",              "greenhouse", "Enova"),
    ("https://job-boards.greenhouse.io/chargepoint",        "greenhouse", "ChargePoint"),
    ("https://job-boards.greenhouse.io/samsungresearchamerica", "greenhouse", "Samsung Research America"),
    ("https://jobs.lever.co/saviynt",                       "lever", "Saviynt"),
    ("https://jobs.lever.co/weride",                        "lever", "WeRide"),
    ("https://jobs.smartrecruiters.com/IrisSoftware",       "smartrecruiters", "Iris Software"),
    ("https://jobs.smartrecruiters.com/LegendBiotech",      "smartrecruiters", "Legend Biotech"),
    ("https://jobs.smartrecruiters.com/TurnerConstruction", "smartrecruiters", "Turner Construction"),
    ("https://jobs.smartrecruiters.com/UniversityofSouthFlorida", "smartrecruiters", "University of South Florida"),
    # --- Added 2026-06-02: recovered via corrected slugs (find_boards' name-guess missed these) ---
    ("https://job-boards.greenhouse.io/digitalocean98",     "greenhouse", "DigitalOcean"),
    ("https://jobs.smartrecruiters.com/clarivateanalytics", "smartrecruiters", "Clarivate"),
]

# Workday companies via the CXS JSON API. Each URL is the company's myworkdayjobs site
# (the data-center subdomain — wd1/wd5/wd12/etc. — must be looked up per company).
WORKDAY_BOARDS = [
    ("https://salesforce.wd12.myworkdayjobs.com/External_Career_Site", "workday", "Salesforce"),
    ("https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite", "workday", "Nvidia"),
    # --- Added 2026-06-01 (wave 2): DOL sponsors confirmed on Workday CXS (tenant looked up + probed) ---
    ("https://rockwellautomation.wd1.myworkdayjobs.com/External_Rockwell_Automation", "workday", "Rockwell Automation"),
    ("https://edwards.wd5.myworkdayjobs.com/edwardscareers",          "workday", "Edwards Lifesciences"),
    ("https://cmegroup.wd1.myworkdayjobs.com/cme_careers",            "workday", "CME Group"),
    ("https://toyota.wd503.myworkdayjobs.com/TMNA",                   "workday", "Toyota Motor North America"),
    ("https://spgi.wd5.myworkdayjobs.com/SPGI_Careers",               "workday", "S&P Global"),
    ("https://globalfoundries.wd1.myworkdayjobs.com/External",        "workday", "GlobalFoundries"),
    # --- Added 2026-06-02 (wave 2b): recovered from the false-negative recheck ---
    ("https://ciena.wd5.myworkdayjobs.com/Careers",                  "workday", "Ciena"),
    ("https://q2ebanking.wd5.myworkdayjobs.com/Q2",                  "workday", "Q2"),
    ("https://guidewire.wd5.myworkdayjobs.com/external",             "workday", "Guidewire"),
]

# Everything scrapeable: Amazon + 26 original boards + extras + Workday companies.
# (Amazon-only: SOURCES = AMAZON   |   boards only: SOURCES = ATS_BOARDS + EXTRA_BOARDS)
SOURCES = AMAZON + ATS_BOARDS + EXTRA_BOARDS + WORKDAY_BOARDS

OUTPUT_CSV    = "jobs.csv"        # master list; only new jobs get appended
LOG_NOTE_FILE = "log.txt"         # the scheduler writes run output here (see README)
SPONSORS_FILE = "sponsors.txt"    # OPTIONAL: one employer name per line (DOL H1B data)
RESUME_FILE   = "resume.txt"      # résumé-driven scraping reads this to tune the search

# Keep a posting only if its TITLE matches one of these (whole word/phrase, not
# substring). Specific phrases keep precision: "program manager" matches, but
# "Experiential Programs Manager" (plural 'programs') does NOT — exactly what we want.
INCLUDE = (
    # Targeted role titles (specific phrases, no broad single words).
    "project manager", "program manager", "project coordinator",
    "program coordinator", "operations coordinator", "operations manager",
    "project analyst", "business analyst", "operations analyst", "data analyst",
    "project specialist", "program specialist", "project associate",
    "operations associate", "scrum master", "project management",
    "program management", "pmo", "implementation",
    # Early-career / new-grad markers (program-style roles; low noise).
    "entry level", "entry-level", "graduate", "new grad", "early career",
    "rotation program", "rotational program", "trainee", "apprentice",
)
# ...but drop it if the title ALSO matches any of these.
EXCLUDE = (
    # Seniority markers
    "senior", "sr", "lead", "principal", "staff", "head", "director",
    "vp", "vice president", "chief", "ii", "iii", "iv", "expert", "architect",
    # Clearly off-target functions for a PM/analyst/ops search. Word boundaries
    # mean "engineer" drops "Software Engineer" but NOT "Engineering Program
    # Manager". Comment any of these back in if you DO want that function.
    "engineer", "developer", "designer", "scientist", "counsel", "attorney",
    "physician", "nurse", "account executive", "sales development", "sdr",
)

# If sponsors.txt is loaded: True = DROP companies not on the list; False = keep
# everything and just FLAG sponsor status (yes/no). We default to flag-only so a
# good employer that simply isn't in your data file never gets thrown away.
REQUIRE_SPONSOR = False

# Keep only jobs located in the USA. Set to False to keep every location.
US_ONLY = True

# Print a KEEP/drop line (with the reason) for every scraped title. Great for
# tuning the filter on ONE board, but noisy across many — so it's off by default.
# Flip to True (ideally with just one board in SOURCES) to see why titles drop.
VERBOSE = False

# Drop a job if its description requires MORE than this many years of experience.
# Only enforced where the scraper actually has the JD text (e.g. Amazon). 3 = keep 1-3 yr.
MAX_YEARS = 3

# Be polite: random pause between sources, and a normal browser User-Agent.
MIN_DELAY, MAX_DELAY = 2, 5
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/124.0 Safari/537.36",
           "Accept": "application/json, text/plain, */*"}


# ============================================================
# FETCHERS  — turn a board URL into job rows
# Greenhouse / Lever / Ashby / SmartRecruiters each expose a public JSON API,
# which is far more stable than scraping HTML and returns clean locations. Only
# Workday is JavaScript-rendered and still needs a real browser (Playwright).
# ============================================================

def _get_json(url, params=None):
    r = requests.get(url, headers=HEADERS, params=params, timeout=25)
    r.raise_for_status()
    return r.json()


def _slug(board_url):
    """The board slug is the last path segment of the career-board URL:
       https://job-boards.greenhouse.io/boulevard     -> 'boulevard'
       https://jobs.lever.co/spotify                  -> 'spotify'
       https://jobs.ashbyhq.com/openai                -> 'openai'
       https://jobs.smartrecruiters.com/AveryDennison -> 'AveryDennison'"""
    return board_url.rstrip("/").split("/")[-1]


def fetch_dynamic(url):
    """For JavaScript-rendered boards (Workday, etc.). Needs Playwright installed."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError(
            "This source needs a browser. Install it once:\n"
            "    pip install playwright && playwright install chromium"
        )
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=HEADERS["User-Agent"])
        page.goto(url, wait_until="networkidle", timeout=30000)
        # If the board lazy-loads, scroll until the count stops growing:
        prev = -1
        while True:
            cards = page.locator("[data-automation-id='jobTitle'], .job-card").count()
            if cards == prev:
                break
            prev = cards
            page.mouse.wheel(0, 12000)
            page.wait_for_timeout(1200)
        html = page.content()
        browser.close()
    return BeautifulSoup(html, "lxml")


# ============================================================
# SCRAPERS  — one per ATS. Each takes a board_url and returns
# [{title, url, location}, ...]. The first four hit a public JSON API;
# Workday has no open API, so it is rendered with Playwright.
# ============================================================

def scrape_greenhouse(board_url):
    data = _get_json("https://boards-api.greenhouse.io/v1/boards/%s/jobs" % _slug(board_url))
    return [{
        "title": (j.get("title") or "").strip(),
        "url": j.get("absolute_url", ""),
        "location": (j.get("location") or {}).get("name", ""),
    } for j in data.get("jobs", [])]


def scrape_lever(board_url):
    data = _get_json("https://api.lever.co/v0/postings/%s?mode=json" % _slug(board_url))
    rows = []
    for j in data:
        cats = j.get("categories") or {}
        loc = cats.get("location") or ""
        # Lever returns the country separately; fold a non-US one into the location
        # string so the US filter can see (and drop) it.
        country = (j.get("country") or "").upper()
        if country and country != "US" and country not in loc.upper():
            loc = ("%s, %s" % (loc, country)).strip(", ")
        rows.append({
            "title": (j.get("text") or "").strip(),
            "url": j.get("hostedUrl", ""),
            "location": loc,
        })
    return rows


def scrape_ashby(board_url):
    data = _get_json("https://api.ashbyhq.com/posting-api/job-board/%s" % _slug(board_url))
    return [{
        "title": (j.get("title") or "").strip(),
        "url": j.get("jobUrl", ""),
        "location": j.get("location") or "",
    } for j in data.get("jobs", []) if j.get("isListed", True)]


def scrape_smartrecruiters(board_url):
    slug = _slug(board_url)
    rows, offset = [], 0
    while True:
        data = _get_json(
            "https://api.smartrecruiters.com/v1/companies/%s/postings?limit=100&offset=%d"
            % (slug, offset))
        batch = data.get("content", [])
        for j in batch:
            loc = j.get("location") or {}
            location = loc.get("fullLocation") or ", ".join(
                x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x)
            if loc.get("remote"):
                location = (location + " (Remote)").strip()
            rows.append({
                "title": (j.get("name") or "").strip(),
                "url": "https://jobs.smartrecruiters.com/%s/%s" % (slug, j.get("id", "")),
                "location": location,
            })
        offset += len(batch)
        if not batch or offset >= data.get("totalFound", 0) or offset >= 1000:
            break
    return rows


WORKDAY_QUERIES = (
    "program manager", "project manager", "project coordinator",
    "program coordinator", "business analyst", "operations analyst",
)


def _workday_date(posted_on):
    """Workday gives 'Posted 5 Days Ago' / 'Posted Today' -> turn into a date."""
    s = (posted_on or "").lower()
    if "today" in s:
        days = 0
    elif "yesterday" in s:
        days = 1
    else:
        m = re.search(r"(\d+)", s)
        days = int(m.group(1)) if m else 0
    return (datetime.datetime.now() - datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M")


def scrape_workday(board_url):
    """Workday via its public CXS JSON API (no browser needed). board_url is the
    company's myworkdayjobs careers site, e.g.
    https://salesforce.wd12.myworkdayjobs.com/External_Career_Site .
    Queries entry-level role terms (Workday caps results at 20/page)."""
    from urllib.parse import urlparse
    p = urlparse(board_url)
    host = p.netloc                                  # e.g. salesforce.wd12.myworkdayjobs.com
    tenant = host.split(".")[0]
    parts = [x for x in p.path.split("/") if x and x.lower() != "en-us"]
    site = parts[-1] if parts else ""
    cxs = "https://%s/wday/cxs/%s/%s/jobs" % (host, tenant, site)
    hdr = dict(HEADERS); hdr["Content-Type"] = "application/json"
    seen, rows = set(), []
    for term in WORKDAY_QUERIES:
        offset = 0
        for _ in range(3):                           # up to 3 pages (20 each) per term
            r = requests.post(cxs, headers=hdr, timeout=25, data=json.dumps(
                {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": term}))
            if r.status_code != 200:
                break
            jp = r.json().get("jobPostings", [])
            if not jp:
                break
            for j in jp:
                path = j.get("externalPath") or ""
                if not path or path in seen:
                    continue
                seen.add(path)
                rows.append({
                    "title": (j.get("title") or "").strip(),
                    "url": "https://%s/%s%s" % (host, site, path),
                    "location": j.get("locationsText") or "",
                    "found_date": _workday_date(j.get("postedOn")),
                })
            offset += len(jp)
            time.sleep(random.uniform(0.3, 0.7))
    return rows


AMAZON_QUERIES = (
    "program manager", "project manager", "project coordinator",
    "program coordinator", "business analyst", "operations manager",
    "data analyst", "implementation",
)


def scrape_amazon(board_url):
    """Amazon's own portal via its public search.json feed. Queries entry-level role
    terms (US-only); the title filter then trims to genuinely entry-level titles."""
    from urllib.parse import urlparse, parse_qs
    q = parse_qs(urlparse(board_url).query)
    country = (q.get("country") or ["USA"])[0]
    loc = (q.get("loc_query") or ["United States"])[0]
    seen, rows = set(), []
    for term in AMAZON_QUERIES:
        offset = 0
        for _ in range(3):                       # up to 3 pages per term
            data = _get_json("https://www.amazon.jobs/en/search.json", params={
                "base_query": term, "country": country, "loc_query": loc,
                "result_limit": 100, "offset": offset, "sort": "relevant"})
            hits = data.get("jobs", [])
            if not hits:
                break
            for j in hits:
                jid = j.get("id_icims") or j.get("job_path")
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                if core.required_years(j.get("basic_qualifications") or "") > MAX_YEARS:
                    continue                     # wants more experience than entry-level
                row = {
                    "title": (j.get("title") or "").strip(),
                    "url": "https://www.amazon.jobs" + (j.get("job_path") or ""),
                    "location": j.get("normalized_location") or j.get("location") or "",
                }
                try:                             # the real posting date from the JD
                    row["found_date"] = datetime.datetime.strptime(
                        j.get("posted_date", ""), "%B %d, %Y").strftime("%Y-%m-%d %H:%M")
                except Exception:
                    pass
                rows.append(row)
            offset += len(hits)
            time.sleep(random.uniform(0.3, 0.7))
    return rows


SCRAPERS = {
    "greenhouse": scrape_greenhouse,
    "lever": scrape_lever,
    "ashby": scrape_ashby,
    "smartrecruiters": scrape_smartrecruiters,
    "amazon": scrape_amazon,
    "workday": scrape_workday,
}


# ============================================================
# ADD-A-BOARD  — turn a pasted careers link into a scrapeable source
# ============================================================
# Only these 5 ATS platforms expose a public job feed we can read. A plain company
# careers site (Google/Meta-style custom portal, iCIMS, Oracle, Eightfold, Taleo) does
# NOT, so detect_board() returns None for those — the app routes them to careers links.
_LOCALES = {"en-us", "en-gb", "en", "us", "global", "en-us"}


def _name_from(slug):
    s = slug.replace("-", " ").replace("_", " ")
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)   # split camel/Pascal: AveryDennison -> Avery Dennison
    s = re.sub(r"\s+", " ", s).strip()
    return s.title() if (s.islower() or s.isupper()) else s


def detect_board(url):
    """Map a pasted job-board URL to (normalized_board_url, ats_type, suggested_name),
    or None if it isn't one of the scrapeable ATS feeds. The normalized URL is the exact
    form the matching scrape_* function expects."""
    url = (url or "").strip()
    if not url:
        return None
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    p = urlparse(url)
    host = p.netloc.lower()
    segs = [s for s in p.path.split("/") if s]

    if "greenhouse.io" in host:
        slug = (parse_qs(p.query).get("for") or [None])[0]      # embed link: ?for=slug
        if not slug and "boards" in segs:                        # boards-api/v1/boards/<slug>/jobs
            i = segs.index("boards")
            slug = segs[i + 1] if i + 1 < len(segs) else None
        if not slug and segs:
            slug = segs[0]
        if slug:
            return ("https://job-boards.greenhouse.io/%s" % slug, "greenhouse", _name_from(slug))

    if "lever.co" in host and segs:
        return ("https://jobs.lever.co/%s" % segs[0], "lever", _name_from(segs[0]))

    if "ashbyhq.com" in host and segs:
        return ("https://jobs.ashbyhq.com/%s" % segs[0], "ashby", _name_from(segs[0]))

    if "smartrecruiters.com" in host and segs:
        return ("https://jobs.smartrecruiters.com/%s" % segs[0], "smartrecruiters", _name_from(segs[0]))

    if "myworkdayjobs.com" in host:
        tenant = host.split(".")[0]
        site_segs = [s for s in segs if s.lower() not in _LOCALES]
        if site_segs:
            return ("https://%s/%s" % (host, site_segs[0]), "workday", _name_from(tenant))

    return None


def probe_board(board_url, ats_type):
    """Hit the board's API and return how many postings it exposes right now
    (0 = reachable but empty; None = couldn't read it). Used to validate before saving."""
    try:
        slug = board_url.rstrip("/").split("/")[-1]
        if ats_type == "greenhouse":
            r = requests.get("https://boards-api.greenhouse.io/v1/boards/%s/jobs" % slug,
                             headers=HEADERS, timeout=10)
            return len(r.json().get("jobs", [])) if r.status_code == 200 else None
        if ats_type == "lever":
            r = requests.get("https://api.lever.co/v0/postings/%s?mode=json" % slug,
                             headers=HEADERS, timeout=10)
            d = r.json() if r.status_code == 200 else None
            return len(d) if isinstance(d, list) else None
        if ats_type == "ashby":
            r = requests.get("https://api.ashbyhq.com/posting-api/job-board/%s" % slug,
                             headers=HEADERS, timeout=10)
            return len(r.json().get("jobs", [])) if r.status_code == 200 else None
        if ats_type == "smartrecruiters":
            r = requests.get("https://api.smartrecruiters.com/v1/companies/%s/postings?limit=1" % slug,
                             headers=HEADERS, timeout=10)
            return r.json().get("totalFound") if r.status_code == 200 else None
        if ats_type == "workday":
            p = urlparse(board_url)
            host, tenant = p.netloc, p.netloc.split(".")[0]
            parts = [x for x in p.path.split("/") if x and x.lower() not in _LOCALES]
            site = parts[-1] if parts else ""
            cxs = "https://%s/wday/cxs/%s/%s/jobs" % (host, tenant, site)
            hdr = dict(HEADERS); hdr["Content-Type"] = "application/json"
            r = requests.post(cxs, headers=hdr, timeout=12, data=json.dumps(
                {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}))
            return r.json().get("total") if r.status_code == 200 else None
    except Exception:
        return None
    return None


def custom_sources():
    """Boards the user added through the app (stored in db) as (url, ats_type, company)
    tuples, deduped against the built-in SOURCES by URL. Never raises (missing table /
    no network -> just no extra sources), so it can't break a scrape."""
    try:
        boards = db.list_boards()
    except Exception:
        boards = []
    have = {u for u, _, _ in SOURCES}
    out = []
    for b in boards or []:
        u, t, c = b.get("url"), b.get("ats_type"), b.get("company")
        if u and t in SCRAPERS and u not in have:
            out.append((u, t, c or u))
            have.add(u)
    return out


# ============================================================
# FILTER  — entry-level + H1B sponsor
# ============================================================

def _make_matcher(terms):
    """Whole-word / whole-phrase, case-insensitive matcher. Word boundaries stop
    'program' from matching 'programmer' or the plural 'programs', and 'lead' from
    matching 'leadership'."""
    parts = sorted((re.escape(t) for t in terms), key=len, reverse=True)
    return re.compile(r"\b(?:%s)\b" % "|".join(parts), re.IGNORECASE)


_INCLUDE_RE = _make_matcher(INCLUDE)
_EXCLUDE_RE = _make_matcher(EXCLUDE)


# ---- résumé-driven terms: tune the scrape toward YOUR resume (purely additive) ----
# Map a detected résumé skill -> extra role phrases to also look for. These get added
# to the title filter AND the Amazon/Workday search queries, on top of the base lists.
SKILL_TO_TERMS = {
    "project management": ("project manager", "project coordinator"),
    "program management": ("program manager", "program coordinator"),
    "requirements / business analysis": ("business analyst", "business systems analyst"),
    "data analysis": ("data analyst",),
    "reporting & dashboards": ("reporting analyst",),
    "operations": ("operations analyst", "operations coordinator"),
    "agile / scrum": ("scrum master",),
    "process improvement": ("business process analyst",),
    "change management": ("change management",),
    "product & roadmap": ("product manager", "associate product manager"),
    "customer success": ("implementation specialist", "implementation consultant"),
    "vendor & procurement": ("procurement analyst",),
    "quality assurance": ("quality analyst",),
    "budgeting & cost": ("financial analyst",),
}


def resume_terms(path=RESUME_FILE):
    """Extra role phrases derived from YOUR resume's detected skills. They AUGMENT the
    base INCLUDE filter + the Amazon/Workday queries (nothing is removed), so scraping
    leans toward your background. Returns [] if resume.txt is missing/unreadable."""
    if not os.path.exists(path):
        return []
    try:
        text = open(path, encoding="utf-8").read()
    except Exception:
        return []
    terms = set()
    for skill in core.skills_in(text):
        terms.update(SKILL_TO_TERMS.get(skill, ()))
    return sorted(terms)


def title_verdict(title):
    """Judge a posting by its TITLE alone. Returns (keep, reason) so a VERBOSE run
    shows exactly why each title survived or was dropped — makes tuning easy."""
    bad = _EXCLUDE_RE.search(title)
    if bad:
        return False, "looks senior/off-target ('%s')" % bad.group(0)
    good = _INCLUDE_RE.search(title)
    if good:
        return True, "matched '%s'" % good.group(0)
    return False, "no entry-level PM/coordinator/analyst keyword"


def is_entry_level(title):
    """Back-compat: title-only boolean (ignores location)."""
    return title_verdict(title)[0]


US_STATE_ABBR = {"AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC"}
US_STATE_NAMES = {"alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "hawaii", "idaho", "illinois", "indiana", "iowa",
    "kansas", "kentucky", "louisiana", "maine", "maryland", "massachusetts", "michigan",
    "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york", "north carolina",
    "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island",
    "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming"}
# Common non-US signals (countries, regions, and big non-US tech hubs).
NON_US = {"india", "united kingdom", " uk", "canada", "ireland", "germany", "france",
    "spain", "portugal", "netherlands", "poland", "romania", "ukraine", "singapore",
    "australia", "new zealand", "japan", "china", "hong kong", "taiwan", "korea",
    "philippines", "vietnam", "indonesia", "malaysia", "thailand", "brazil", "mexico",
    "argentina", "colombia", "chile", "israel", "uae", "united arab emirates",
    "south africa", "sweden", "norway", "denmark", "finland", "switzerland", "austria",
    "belgium", "italy", "greece", "hungary", "emea", "apac", "latam", "london", "dublin",
    "bangalore", "bengaluru", "hyderabad", "pune", "mumbai", "delhi", "chennai", "gurgaon",
    "gurugram", "noida", "toronto", "vancouver", "montreal", "berlin", "munich",
    "amsterdam", "paris", "madrid", "barcelona", "lisbon", "warsaw", "krakow", "bucharest",
    "tel aviv", "sydney", "melbourne", "sao paulo", "mexico city", "tokyo", "shanghai",
    "beijing", "shenzhen", "seoul", "manila", "kuala lumpur", "jakarta", "bangkok",
    "costa rica", "guatemala", "el salvador", "honduras", "panama", "dominican",
    "guadalajara", "monterrey", "bogota", "medellin", "lima", "cebu"}

_STATE_ABBR_RE = re.compile(r",\s*([A-Za-z]{2})\b")


def is_us_location(loc):
    """Heuristic: True if the location looks US-based. Unknown/blank -> kept."""
    if not loc:
        return True
    low = loc.lower()
    if any(tok in low for tok in NON_US):           # explicit non-US signal -> drop
        return False
    if "united states" in low or "usa" in low or "u.s." in low:
        return True
    m = _STATE_ABBR_RE.search(loc)                  # e.g. "Boston, MA"
    if m and m.group(1).upper() in US_STATE_ABBR:
        return True
    if any(name in low for name in US_STATE_NAMES):
        return True
    if "remote" in low:                             # remote w/ no foreign signal -> US-eligible
        return True
    return False


def load_sponsors(path=SPONSORS_FILE):
    """Read employer names (one per line) from sponsors.txt. Build that file from
    public DOL H1B disclosure data with build_sponsors.py. '#' lines are ignored."""
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        names = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    return names or None


_LEGAL_SUFFIX = re.compile(
    r"\b(?:inc|incorporated|llc|corp|corporation|co|company|ltd|limited|lp|llp|"
    r"plc|gmbh|sa|ag|holdings|group|technologies|technology|labs|usa|us|na)\b")


def _norm_name(s):
    """Normalize a company name for matching: lowercase, strip punctuation and
    common legal suffixes. 'Avery-Dennison Corp.' -> 'avery dennison'."""
    s = re.sub(r"[^a-z0-9 ]+", " ", s.lower())
    s = _LEGAL_SUFFIX.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def build_sponsor_index(names):
    """Pre-normalize the sponsor list once so per-job lookups are fast."""
    return {"raw": names, "norm": {_norm_name(n) for n in names}}


_sponsor_cache = {}

def sponsors_h1b(company, sponsor_index):
    """True if `company` looks like a known H1B sponsor. Normalized exact match is
    the fast path (works even on a huge DOL list); for a SMALL list we also try
    fuzzy matching so e.g. 'Meta' still matches 'Meta Platforms'. Cached per company."""
    if company in _sponsor_cache:
        return _sponsor_cache[company]
    norm = _norm_name(company)
    hit = bool(norm) and norm in sponsor_index["norm"]
    if not hit and norm and len(sponsor_index["raw"]) <= 5000:
        try:
            from rapidfuzz import fuzz
            hit = any(fuzz.token_set_ratio(norm, n) >= 90 for n in sponsor_index["norm"])
        except ImportError:
            hit = any(norm in n or n in norm for n in sponsor_index["norm"])
    _sponsor_cache[company] = hit
    return hit


# ============================================================
# STORAGE  — plain CSV, with cross-run dedup (no database)
# ============================================================

FIELDNAMES = ["found_date", "title", "company", "location", "url", "sponsors_h1b"]

def load_seen_urls(path=OUTPUT_CSV):
    if not os.path.exists(path):
        return set()
    with open(path, newline="", encoding="utf-8") as f:
        return {row["url"] for row in csv.DictReader(f) if row.get("url")}


def append_jobs(rows, path=OUTPUT_CSV):
    new_file = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if new_file:
            writer.writeheader()
        writer.writerows(rows)


# ============================================================
# ORCHESTRATOR  — the scraper itself
# ============================================================

def scrape_all(sources):
    all_jobs = []
    for url, ats_type, company in sources:
        scraper_fn = SCRAPERS.get(ats_type)
        if scraper_fn is None:
            print(f"  SKIP {company:<24} unknown ats_type '{ats_type}'")
            continue
        try:
            rows = scraper_fn(url)
            for r in rows:
                r["company"] = company
            all_jobs.extend(rows)
            print(f"  OK   {company:<24} {len(rows):>3} postings")
        except Exception as e:
            print(f"  FAIL {company:<24} {e}")     # one bad source never stops the run
        time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
    return all_jobs


def main():
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"\n=== Job scrape @ {stamp} ===")

    sponsors = load_sponsors()
    sponsor_index = build_sponsor_index(sponsors) if sponsors else None
    if sponsor_index:
        action = "dropping non-sponsors" if REQUIRE_SPONSOR else "flag only"
        print(f"Loaded {len(sponsors)} sponsor names from {SPONSORS_FILE} ({action}).")
    else:
        print(f"No {SPONSORS_FILE} found — keeping all entry-level jobs, "
              f"sponsor status marked 'unknown'.")

    extra = resume_terms()
    if extra:
        global _INCLUDE_RE, AMAZON_QUERIES, WORKDAY_QUERIES
        _INCLUDE_RE = _make_matcher(tuple(INCLUDE) + tuple(extra))   # broaden the title keep-filter
        AMAZON_QUERIES = tuple(dict.fromkeys(AMAZON_QUERIES + tuple(extra)))     # + Amazon searches
        WORKDAY_QUERIES = tuple(dict.fromkeys(WORKDAY_QUERIES + tuple(extra)))   # + Workday searches
        print("Résumé-driven (%s): also searching %s" % (RESUME_FILE, ", ".join(extra)))
    else:
        print("No %s found — using the base role filter only." % RESUME_FILE)

    seen = db.existing_urls()
    sources = SOURCES + custom_sources()
    if len(sources) > len(SOURCES):
        print("+ %d board(s) added via the app." % (len(sources) - len(SOURCES)))
    scraped = scrape_all(sources)

    kept = []
    for j in scraped:
        if j["url"] in seen:
            continue                       # already in jobs.csv from a past run
        keep, why = title_verdict(j["title"])
        if keep and US_ONLY and not is_us_location(j.get("location", "")):
            keep, why = False, "non-US location (%s)" % (j.get("location") or "n/a")
        if VERBOSE:
            print("  %s %-52s %s" % ("KEEP " if keep else "drop ", j["title"][:52], why))
        if not keep:
            continue
        if sponsor_index:
            sponsored = sponsors_h1b(j["company"], sponsor_index)
            if REQUIRE_SPONSOR and not sponsored:
                continue
            j["sponsors_h1b"] = "yes" if sponsored else "no"
        else:
            j["sponsors_h1b"] = "unknown"
        j.setdefault("found_date", stamp)        # keep the JD's posting date if set
        kept.append({k: j.get(k, "") for k in FIELDNAMES})

    if kept:
        db.add_jobs(kept)

    print(f"\n{len(kept)} NEW matching job(s):")
    for j in kept:
        flag = "" if j["sponsors_h1b"] != "yes" else "  [sponsors H1B]"
        print(f"  - {j['title']} - {j['company']} ({j['location'] or 'n/a'}){flag}")
        print(f"    {j['url']}")
    if kept:
        where = "Supabase" if db.using_supabase() else OUTPUT_CSV
        print(f"\nSaved to {where}. Run `python score_jobs.py` next to score them.")
    else:
        print("Nothing new this run.")


if __name__ == "__main__":
    main()
