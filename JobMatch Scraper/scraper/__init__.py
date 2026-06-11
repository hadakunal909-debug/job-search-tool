#!/usr/bin/env python3
"""
Personal job scraper
--------------------
Visits the career boards you list in SOURCES, pulls the postings, keeps only the
entry-level ones (and, if you give it H1B data, only companies that have sponsored
before), and appends anything NEW to jobs.csv.

Run it:           python -m scraper
Minimum install:  pip install requests beautifulsoup4 lxml
Only if you add a Workday/JS source:  pip install playwright  &&  playwright install chromium
"""

import csv
import json
import os
import sys
import time
import random
import socket
import ipaddress
import concurrent.futures
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
    # --- Added 2026-06-06: probe of the Level-I DOL list (8 confirmed; slugs verified) ---
    ("https://job-boards.greenhouse.io/purestorage",            "greenhouse", "Pure Storage"),
    ("https://job-boards.greenhouse.io/peopletech",             "greenhouse", "People Tech Group"),
    ("https://jobs.lever.co/softworld",                         "lever", "Softworld Technologies"),
    ("https://jobs.smartrecruiters.com/HarvardUniversity",      "smartrecruiters", "Harvard University"),
    ("https://jobs.smartrecruiters.com/SriTechSolutionsINC",    "smartrecruiters", "Sri Tech Solutions"),
    ("https://jobs.smartrecruiters.com/TechTammina",            "smartrecruiters", "Tech Tammina"),
    ("https://jobs.smartrecruiters.com/FederalSoftSystemsINC",  "smartrecruiters", "Federal Soft Systems"),
    ("https://jobs.smartrecruiters.com/SkilltuneTechnologiesINC", "smartrecruiters", "Skilltune Technologies"),
    # --- Added 2026-06-10: probe of the user's H1B LCA list (board names verified) ---
    ("https://job-boards.greenhouse.io/byd",                    "greenhouse", "BYD America"),
    ("https://jobs.ashbyhq.com/deel",                           "ashby", "Deel"),
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
    # --- Added 2026-06-06: universities/hospitals on Workday (tenant URL supplied by user) ---
    ("https://wisconsin.wd1.myworkdayjobs.com/UW_Comprehensives",    "workday", "University of Wisconsin System"),
    ("https://danafarber.wd5.myworkdayjobs.com/dana-farber",         "workday", "Dana-Farber Cancer Institute"),
    ("https://northeastern.wd1.myworkdayjobs.com/careers",           "workday", "Northeastern University"),
    ("https://rit.wd12.myworkdayjobs.com/careers",                   "workday", "Rochester Institute of Technology"),
    ("https://wd5.myworkdaysite.com/recruiting/uw/UWHires",          "workday", "University of Washington"),
    ("https://mastercard.wd1.myworkdayjobs.com/corporatecareers",    "workday", "Mastercard"),
    # --- Added 2026-06-08: careers.labcorp.com is a Phenom front-end, but its apply
    # links go to Workday (labcorp.wd1) — so we read the full 1600-job board directly. ---
    ("https://labcorp.wd1.myworkdayjobs.com/External",               "workday", "Labcorp"),
    # --- Added 2026-06-10: more Phenom front-ends resolved to their Workday boards
    # via detect_phenom (counts at add time: Danaher 1401, Baker Hughes 728, SWA 52). ---
    ("https://danaher.wd1.myworkdayjobs.com/DanaherJobs",            "workday", "Danaher"),
    ("https://bakerhughes.wd5.myworkdayjobs.com/BakerHughes",        "workday", "Baker Hughes"),
    ("https://swa.wd1.myworkdayjobs.com/external",                   "workday", "Southwest Airlines"),
    # --- Added 2026-06-10 (user's H1B LCA list): Cisco via detect_phenom's Workday
    # redirect (962 jobs); UChicago tenant found by direct CXS probe (394; cap-exempt). ---
    ("https://cisco.wd5.myworkdayjobs.com/Cisco_Careers",            "workday", "Cisco"),
    ("https://uchicago.wd5.myworkdayjobs.com/External",              "workday", "University of Chicago"),
    # Found by detect_linked_ats on flowserve.com/en/careers (site really is "applied").
    ("https://flowserve.wd1.myworkdayjobs.com/applied",              "workday", "Flowserve"),
]

# Phenom People career sites that are Phenom-NATIVE (apply links don't go to Workday —
# those get added as Workday boards instead; see detect_phenom).
PHENOM_BOARDS = [
    # Actalent: huge engineering/sciences staffing firm, heavy H1B sponsor (~5k postings;
    # scrape_phenom caps at 3000 — the title filter keeps only on-target PM/analyst roles).
    ("https://careers.actalentservices.com", "phenom", "Actalent"),
]

# Oracle Cloud Recruiting (ORC) career sites — public recruitingCEJobRequisitions API.
# The careers URL embeds the site number: .../CandidateExperience/en/sites/{CX_...}.
ORACLE_BOARDS = [
    # Oracle itself: ~1400 postings, long-time top-20 H1B sponsor.
    ("https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_45001",
     "oracle", "Oracle"),
    # --- Added 2026-06-11 via detect_linked_ats on each company's careers page ---
    # EXL: analytics/operations consulting, steady H1B sponsor (~3k postings).
    ("https://fa-ewjt-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_2",
     "oracle", "EXL Service"),
    # Providence: large nonprofit health system (~1.9k postings; cap-exempt employer).
    ("https://evac.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1",
     "oracle", "Providence"),
]

# iCIMS "Career Sites" (powered by Jibe) expose a public /api/jobs JSON feed at the
# career-site origin. board_url = the careers domain. Add any iCIMS/Jibe employer here
# (the "➕ Add board" view auto-detects these from a pasted careers.<company>.com link).
JIBE_BOARDS = [
    ("https://careers.hrblock.com", "jibe", "H&R Block"),
    # Mount Sinai (Icahn School of Medicine + hospital system): cap-exempt sponsor,
    # ~1.8k postings; apply links go to its Oracle site but the Jibe feed is cleaner.
    ("https://careers.mountsinai.org", "jibe", "Mount Sinai"),
]

# Employers whose OWN site blocks server-side scraping (e.g. Tesla sits behind Akamai's
# bot wall — every request from a script or even headless Chrome gets 403/429). We pull
# their US postings from the Adzuna aggregator API instead. board_url is "adzuna:<Company>"
# — we search that name and keep only exact-employer matches. DORMANT until you set a free
# Adzuna key (no credit card): https://developer.adzuna.com  ->  ADZUNA_APP_ID / ADZUNA_APP_KEY
# (export them as env vars, or add them as GitHub Actions secrets for the scheduled run).
ADZUNA_BOARDS = [
    ("adzuna:Tesla", "adzuna", "Tesla"),
    # Google: their careers site's robots.txt explicitly Disallows the jobs-results
    # pages, so we do NOT scrape it directly — the aggregator is the sanctioned route.
    ("adzuna:Google", "adzuna", "Google"),
    # --- Added 2026-06-11: sponsors from the user's H1B LCA list whose own career
    # sites expose NO public feed (custom portals / SuccessFactors / bot-walled).
    # The aggregator is the only way to scrape them; each costs ~1 API call per run.
    ("adzuna:IBM",                  "adzuna", "IBM"),
    ("adzuna:CGI",                  "adzuna", "CGI"),
    ("adzuna:Mphasis",              "adzuna", "Mphasis"),
    ("adzuna:HCLTech",              "adzuna", "HCL America"),
    ("adzuna:Tech Mahindra",        "adzuna", "Tech Mahindra"),
    ("adzuna:Coforge",              "adzuna", "Coforge"),
    ("adzuna:Brillio",              "adzuna", "Brillio"),
    ("adzuna:L&T Technology Services", "adzuna", "L&T Technology Services"),
    ("adzuna:Persistent Systems",   "adzuna", "Persistent Systems"),
    ("adzuna:Birlasoft",            "adzuna", "Birlasoft"),
    ("adzuna:Hexaware Technologies", "adzuna", "Hexaware"),
    ("adzuna:Nagarro",              "adzuna", "Nagarro"),
    ("adzuna:Cyient",               "adzuna", "Cyient"),
    ("adzuna:HTC Global Services",  "adzuna", "HTC Global Services"),
    ("adzuna:Movate",               "adzuna", "Movate"),
    ("adzuna:Atos",                 "adzuna", "Atos Syntel"),
    ("adzuna:Tencent",              "adzuna", "Tencent America"),
    ("adzuna:Munich Re",            "adzuna", "Munich Re America"),
    ("adzuna:Cornerstone OnDemand", "adzuna", "Cornerstone OnDemand"),
    ("adzuna:Holtec International", "adzuna", "Holtec International"),
    ("adzuna:Hendrickson",          "adzuna", "Hendrickson"),
    ("adzuna:Saama Technologies",   "adzuna", "Saama Technologies"),
    ("adzuna:CAST Software",        "adzuna", "Cast Software"),
    ("adzuna:Ideagen",              "adzuna", "Ideagen"),
    ("adzuna:Elemica",              "adzuna", "Elemica"),
    ("adzuna:Fortanix",             "adzuna", "Fortanix"),
    ("adzuna:Tigo Energy",          "adzuna", "Tigo Energy"),
]

# Everything scrapeable: Amazon + boards + Workday + iCIMS/Jibe + Oracle + Phenom + Adzuna.
# (Amazon-only: SOURCES = AMAZON   |   boards only: SOURCES = ATS_BOARDS + EXTRA_BOARDS)
SOURCES = (AMAZON + ATS_BOARDS + EXTRA_BOARDS + WORKDAY_BOARDS + JIBE_BOARDS
           + ORACLE_BOARDS + PHENOM_BOARDS + ADZUNA_BOARDS)

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


def _make_session():
    """One shared HTTP session for every fetch: connection pooling (keep-alive per ATS
    host — much faster than a new TLS handshake per request) + automatic retries with
    backoff on transient failures (connection resets, 429 rate-limits, 5xx). Without
    this, a single blip loses a whole board for the run. POST is retried too — our only
    POSTs are Workday CXS searches, which are read-only queries, so replay is safe."""
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    retry = Retry(total=3, connect=3, read=2, backoff_factor=0.5,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset({"GET", "POST", "HEAD"}),
                  respect_retry_after_header=True)
    s = requests.Session()
    adapter = HTTPAdapter(max_retries=retry, pool_connections=16, pool_maxsize=16)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


SESSION = _make_session()


# ============================================================
# FETCHERS  — turn a board URL into job rows
# Greenhouse / Lever / Ashby / SmartRecruiters each expose a public JSON API,
# which is far more stable than scraping HTML and returns clean locations. Only
# Workday is JavaScript-rendered and still needs a real browser (Playwright).
# ============================================================

def _get_json(url, params=None):
    r = SESSION.get(url, headers=HEADERS, params=params, timeout=25)
    r.raise_for_status()
    return r.json()


# ---- SSRF / abuse guards for URLs that come from a USER (added boards, JSON-LD pages) ----
# The fixed-host scrapers (Greenhouse/Lever/Ashby/SmartRecruiters/Workday/Amazon/Adzuna) hit
# hard-coded API hosts and don't need this. But the Jibe and JSON-LD scrapers fetch a
# user-supplied domain — both at "➕ Add board" time AND on every scheduled scrape (custom
# boards). Without a guard, someone could point those at http://169.254.169.254/ (cloud
# metadata), http://localhost, or an internal IP and use OUR server as a proxy.
_MAX_FETCH_BYTES   = 5 * 1024 * 1024                       # cap one user-URL fetch at 5 MB
_BLOCKED_HOSTNAMES = {"localhost", "metadata", "metadata.google.internal"}


def is_http_url(url):
    """Cheap (no DNS): True only for an http/https URL. Use it to keep dangerous-scheme
    links (javascript:, data:, file:) out of anything we store or render as a link."""
    try:
        return urlparse(url or "").scheme.lower() in ("http", "https")
    except Exception:
        return False


def _ip_is_public(addr):
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or
                ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def public_http_url(url):
    """The URL if it's http(s) AND its host resolves only to PUBLIC IPs, else None — so a
    user URL can't make us reach loopback / private ranges / link-local cloud metadata."""
    if not is_http_url(url):
        return None
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if not host or host in _BLOCKED_HOSTNAMES:
        return None
    try:
        infos = socket.getaddrinfo(host, p.port or (443 if p.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except Exception:
        return None
    addrs = {i[4][0] for i in infos}
    return url if addrs and all(_ip_is_public(a) for a in addrs) else None


def _safe_get(url, headers=None, timeout=20, params=None):
    """requests.get hardened for USER-SUPPLIED URLs: rejects non-public targets (SSRF),
    won't auto-follow a redirect into a private host, and caps the body size. Raises
    ValueError when the URL/target is disallowed. Returns a normal requests.Response."""
    headers = headers or HEADERS
    hops = 0
    while True:
        if not public_http_url(url):
            raise ValueError("blocked non-public URL: %s" % url)
        r = SESSION.get(url, headers=headers, params=params, timeout=timeout,
                        allow_redirects=False, stream=True)
        if r.is_redirect and hops < 3:                     # re-validate each redirect target
            nxt = urljoin(url, r.headers.get("location", ""))
            r.close()
            url, params, hops = nxt, None, hops + 1
            continue
        break
    total, chunks = 0, []
    for chunk in r.iter_content(8192):
        total += len(chunk)
        if total > _MAX_FETCH_BYTES:
            r.close()
            raise ValueError("response exceeds %d bytes" % _MAX_FETCH_BYTES)
        chunks.append(chunk)
    r._content = b"".join(chunks)
    r._content_consumed = True
    return r


def _safe_post(url, body, headers=None, timeout=20):
    """JSON POST hardened for USER-SUPPLIED origins (same checks as _safe_get): the
    target must resolve to public IPs, redirects are not followed, and the response
    body is size-capped. Raises ValueError when the URL/target is disallowed."""
    headers = dict(headers or HEADERS)
    headers.setdefault("Content-Type", "application/json")
    if not public_http_url(url):
        raise ValueError("blocked non-public URL: %s" % url)
    r = SESSION.post(url, headers=headers, data=json.dumps(body), timeout=timeout,
                     allow_redirects=False, stream=True)
    total, chunks = 0, []
    for chunk in r.iter_content(8192):
        total += len(chunk)
        if total > _MAX_FETCH_BYTES:
            r.close()
            raise ValueError("response exceeds %d bytes" % _MAX_FETCH_BYTES)
        chunks.append(chunk)
    r._content = b"".join(chunks)
    r._content_consumed = True
    return r


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


def _workday_parts(board_url):
    """(host, tenant, site) for either Workday URL format:
       {tenant}.{dc}.myworkdayjobs.com/[locale/]{site}
       {dc}.myworkdaysite.com/recruiting/{tenant}/{site}"""
    p = urlparse(board_url)
    host = p.netloc
    segs = [x for x in p.path.split("/") if x and x.lower() not in _LOCALES]
    if "myworkdaysite.com" in host:
        if segs and segs[0].lower() == "recruiting":
            segs = segs[1:]
        tenant = segs[0] if segs else ""
        site = segs[1] if len(segs) > 1 else ""
    else:
        tenant = host.split(".")[0]
        site = segs[0] if segs else ""
    return host, tenant, site


# Workday caps each page at 20 results (asking for more returns HTTP 400). We walk the
# WHOLE board with an empty search and let main()'s title/US filter decide what to keep.
# MAX_JOBS is just a safety stop so a giant tenant can't page forever (3000 = 150 pages).
WORKDAY_PAGE_LIMIT = 20
WORKDAY_MAX_JOBS = 3000


def _workday_loc_from_path(path):
    """Multi-location postings report locationsText as a bare count ('3 Locations'),
    which the US filter can't read. The job's externalPath embeds the primary city, e.g.
       /job/OFallon-Missouri/Manager--Project---Change-Management_R-279079
    so recover a readable 'OFallon Missouri' from the segment right after 'job'."""
    segs = [s for s in (path or "").split("/") if s]
    if "job" in segs:
        i = segs.index("job")
        if i + 1 < len(segs):
            return segs[i + 1].replace("-", " ").strip()
    return ""


def scrape_workday(board_url):
    """Workday via its public CXS JSON API (no browser needed). Pulls the ENTIRE board
    by paging through every posting with an empty search; main()'s title + US filter
    then trims it down. (The old approach ran a few keyword searches 2 pages deep, which
    MISSED on-target roles that Workday's relevance ranked lower — e.g. a 'Manager,
    Project & Change Management' sitting at result #48 of 1000+.) board_url is the
    company's Workday site in either format:
       https://salesforce.wd12.myworkdayjobs.com/External_Career_Site
       https://wd5.myworkdaysite.com/recruiting/uw/UWHires"""
    host, tenant, site = _workday_parts(board_url)
    cxs = "https://%s/wday/cxs/%s/%s/jobs" % (host, tenant, site)
    job_base = ("https://%s/en-US/recruiting/%s/%s" % (host, tenant, site)
                if "myworkdaysite.com" in host else "https://%s/%s" % (host, site))
    hdr = dict(HEADERS); hdr["Content-Type"] = "application/json"
    seen, rows, offset, total = set(), [], 0, None
    while offset < WORKDAY_MAX_JOBS:
        r = SESSION.post(cxs, headers=hdr, timeout=25, data=json.dumps(
            {"appliedFacets": {}, "limit": WORKDAY_PAGE_LIMIT, "offset": offset,
             "searchText": ""}))
        if r.status_code != 200:
            break
        body = r.json()
        jp = body.get("jobPostings", [])
        if not jp:
            break
        if total is None:                                # only the FIRST page reports the
            total = body.get("total") or 0               # real count; later pages send 0
        for j in jp:
            path = j.get("externalPath") or ""
            if not path or path in seen:
                continue
            seen.add(path)
            loc = j.get("locationsText") or ""
            if not loc or re.search(r"\d+\s+location|multiple", loc, re.I):
                loc = _workday_loc_from_path(path) or loc    # 'N Locations' -> city from URL
            rows.append({
                "title": (j.get("title") or "").strip(),
                "url": job_base + path,
                "location": loc,
                "found_date": _workday_date(j.get("postedOn")),
            })
        offset += len(jp)
        if total and offset >= total:                    # read the whole board
            break
        time.sleep(random.uniform(0.1, 0.25))
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
        for _ in range(2):                       # up to 2 pages per term
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


def _jibe_date(s):
    """'2026-06-05T21:49:00+0000' -> '2026-06-05' (best-effort)."""
    try:
        return datetime.datetime.strptime((s or "")[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
    except Exception:
        return ""


def scrape_jibe(board_url):
    """iCIMS 'Career Sites' (powered by Jibe) via their public /api/jobs JSON feed.
    board_url is the career-site origin, e.g. https://careers.hrblock.com or
    https://<client>.jibeapply.com . Pages through ?limit=100&page=N until done.
    Lots of big US employers on iCIMS expose this — a custom careers domain is fine."""
    p = urlparse(board_url)
    base = "%s://%s" % (p.scheme or "https", p.netloc)
    rows, seen, page = [], set(), 1
    while page <= 20:                                 # 100/page -> up to 2000 postings
        try:
            r = _safe_get("%s/api/jobs?limit=100&page=%d" % (base, page), timeout=25)
        except ValueError:
            break                                     # non-public host -> refuse (SSRF guard)
        if r.status_code != 200:
            break
        d = r.json()
        jobs = d.get("jobs") or []
        if not jobs:
            break
        for w in jobs:
            j = w.get("data", w) or {}
            url = j.get("apply_url") or ""
            if not url or url in seen:
                continue
            seen.add(url)
            loc = j.get("full_location") or j.get("location_name") or ""
            ctry = j.get("country") or ""
            if ctry and ctry.lower() not in loc.lower():
                loc = (loc + ", " + ctry).strip(", ")
            rows.append({
                "title": (j.get("title") or "").strip(),
                "url": url,
                "location": loc,
                "found_date": _jibe_date(j.get("posted_date") or j.get("create_date")),
            })
        total = d.get("totalCount") or d.get("count") or 0
        if len(jobs) < 100 or page * 100 >= total:
            break
        page += 1
        time.sleep(random.uniform(0.3, 0.7))
    return rows


def scrape_adzuna(board_url):
    """US postings for ONE employer via the Adzuna aggregator API — used for companies
    whose own careers site blocks scraping (Tesla = Akamai bot-wall, returns 403/429 to
    any script or headless browser). board_url is 'adzuna:<Company>'; we search that name
    and keep only rows whose employer matches it (Adzuna's keyword search is broad).

    DORMANT unless a free Adzuna key is configured (no credit card needed):
        register at https://developer.adzuna.com  ->  set ADZUNA_APP_ID + ADZUNA_APP_KEY
        (env vars locally, or GitHub Actions secrets for the scheduled scrape).
    Returns [] (contributes nothing) when the key isn't set, so it never breaks a run."""
    app_id  = os.environ.get("ADZUNA_APP_ID")
    app_key = os.environ.get("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        return []
    company = board_url.split(":", 1)[1] if ":" in board_url else board_url
    target  = _norm_name(company)
    rows, seen = [], set()
    for page in range(1, 6):                          # up to 5 pages x 50 = 250 results
        try:
            data = _get_json(
                "https://api.adzuna.com/v1/api/jobs/us/search/%d" % page,
                params={"app_id": app_id, "app_key": app_key,
                        # company= returns ONLY this employer (keyword `what` searches
                        # mentions — for Google that found 0 of its 3.8k listings);
                        # what_or biases the 250-result page budget toward our roles.
                        "company": company,
                        "what_or": "project program analyst coordinator operations implementation scrum",
                        "results_per_page": 50, "content-type": "application/json"})
        except Exception:
            break
        results = data.get("results", [])
        if not results:
            break
        for j in results:
            co = ((j.get("company") or {}).get("display_name") or "").strip()
            con = _norm_name(co)
            if not con or not (con == target or con.startswith(target + " ")):
                continue                              # skip recruiters / unrelated keyword hits
            url = j.get("redirect_url") or ""
            if not url or url in seen:
                continue
            seen.add(url)
            rows.append({
                "title": (j.get("title") or "").strip(),
                "url": url,
                "location": ((j.get("location") or {}).get("display_name") or ""),
                "found_date": (j.get("created") or "")[:10],
            })
        if len(results) < 50 or page * 50 >= data.get("count", 0):
            break
        time.sleep(random.uniform(0.3, 0.7))
    return rows


def _sub(board_url):
    """First DNS label, e.g. https://bunq.recruitee.com -> 'bunq'."""
    return urlparse(board_url).netloc.split(".")[0]


def scrape_recruitee(board_url):
    """Recruitee public API: https://{slug}.recruitee.com/api/offers/"""
    d = _get_json("https://%s.recruitee.com/api/offers/" % _sub(board_url))
    rows = []
    for o in (d.get("offers", []) if isinstance(d, dict) else []):
        loc = o.get("location") or ", ".join(
            x for x in (o.get("city"), o.get("country")) if x)
        rows.append({"title": (o.get("title") or "").strip(),
                     "url": o.get("careers_url") or o.get("careers_apply_url") or "",
                     "location": loc,
                     "found_date": (o.get("published_at") or "")[:10]})
    return [r for r in rows if r["url"]]


def scrape_breezy(board_url):
    """Breezy public JSON: https://{slug}.breezy.hr/json"""
    d = _get_json("https://%s.breezy.hr/json" % _sub(board_url))
    rows = []
    for j in (d if isinstance(d, list) else []):
        loc = j.get("location") or {}
        if isinstance(loc, dict):
            def _nm(v):
                return v.get("name") if isinstance(v, dict) else v
            loc = ", ".join(str(x) for x in
                            (_nm(loc.get("city")), _nm(loc.get("state")), _nm(loc.get("country"))) if x)
        rows.append({"title": (j.get("name") or "").strip(),
                     "url": j.get("url") or "",
                     "location": loc,
                     "found_date": (j.get("published_date") or "")[:10]})
    return [r for r in rows if r["url"]]


def scrape_personio(board_url):
    """Personio XML feed: https://{slug}.jobs.personio.com/xml"""
    import xml.etree.ElementTree as ET
    slug = _sub(board_url)
    try:
        r = SESSION.get("https://%s.jobs.personio.com/xml" % slug, headers=HEADERS, timeout=20)
        root = ET.fromstring(r.content)
    except Exception:
        return []
    rows = []
    for pos in root.findall(".//position"):
        def t(tag):
            e = pos.find(tag)
            return (e.text or "").strip() if e is not None and e.text else ""
        offices = [t("office")] + [o.text for o in pos.findall(".//additionalOffices/office") if o.text]
        jid = t("id")
        rows.append({"title": t("name"),
                     "url": "https://%s.jobs.personio.com/job/%s" % (slug, jid),
                     "location": ", ".join(x for x in offices if x),
                     "found_date": (t("createdAt") or "")[:10]})
    return [r for r in rows if r["title"] and r["url"]]


def scrape_jsonld(board_url):
    """Generic: pull schema.org JobPosting items embedded in a careers page (the same
    structured data Google for Jobs reads). Works on many custom sites; best-effort."""
    try:
        r = _safe_get(board_url)
        soup = BeautifulSoup(r.text, "lxml")
    except Exception:
        return []

    def _addr(node):
        a = (node or {}).get("address") or {}
        if not isinstance(a, dict):
            return ""
        ctry = a.get("addressCountry")
        ctry = ctry.get("name") if isinstance(ctry, dict) else ctry
        return ", ".join(str(x) for x in
                         (a.get("addressLocality"), a.get("addressRegion"), ctry) if x)

    rows = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for it in list(items):
            if isinstance(it, dict) and isinstance(it.get("@graph"), list):
                items += it["@graph"]
        for it in items:
            if not isinstance(it, dict):
                continue
            typ = it.get("@type")
            if typ != "JobPosting" and not (isinstance(typ, list) and "JobPosting" in typ):
                continue
            jl = it.get("jobLocation")
            loc = ("; ".join(_addr(x) for x in jl if _addr(x)) if isinstance(jl, list)
                   else _addr(jl))
            rows.append({"title": (it.get("title") or "").strip(),
                         "url": it.get("url") or board_url,
                         "location": loc,
                         "found_date": (str(it.get("datePosted") or ""))[:10]})
    # de-dupe by url; keep only http(s) links (a malicious page could embed a
    # "url": "javascript:..." in its JobPosting JSON, which we'd later render as a link)
    seen, out = set(), []
    for r in rows:
        if r["title"] and is_http_url(r["url"]) and r["url"] not in seen:
            seen.add(r["url"]); out.append(r)
    return out


# ---- Phenom People (careers.<company>.com sites used by many Fortune-500 sponsors) ----
def _phenom_body(offset, size):
    """The POST /widgets body Phenom career sites send for their own job search."""
    return {"lang": "en_us", "deviceType": "desktop", "country": "us",
            "pageName": "search-results", "ddoKey": "refineSearch", "sortBy": "Most recent",
            "subsearch": "", "from": offset, "jobs": True, "counts": True,
            "all_fields": ["category", "country", "state", "city"], "size": size,
            "clearAll": False, "jdsource": "facets", "isSliderEnable": False,
            "pageId": "page-search-results", "siteType": "external", "keywords": "",
            "global": True, "selected_fields": {}, "locationData": {}}


def scrape_phenom(board_url):
    """Phenom People career sites (careers.<company>.com / jobs.<company>.com) via the
    public POST /widgets JSON their own search uses. board_url is the careers origin.
    NOTE: many Phenom tenants are a front-end for Workday — their applyUrl points at
    myworkdayjobs — and detect_phenom() returns the Workday board instead in that case
    (better data + JD support). This scraper is for tenants that are Phenom-native."""
    p = urlparse(board_url)
    base = "%s://%s" % (p.scheme or "https", p.netloc)
    rows, seen, offset, total = [], set(), 0, None
    while offset < 3000:
        try:
            r = _safe_post(base + "/widgets", _phenom_body(offset, 100), timeout=25)
        except ValueError:
            break                                     # non-public host -> refuse (SSRF guard)
        if r.status_code != 200:
            break
        d = (r.json() or {}).get("refineSearch") or {}
        if total is None:
            total = d.get("totalHits") or 0
        jobs = (d.get("data") or {}).get("jobs") or []
        if not jobs:
            break
        for j in jobs:
            url = j.get("applyUrl") or ""
            if not is_http_url(url):                  # native tenants: build the job-page link
                jid = j.get("jobId") or ""
                url = "%s/us/en/job/%s" % (base, jid) if jid else ""
            if not url or url in seen:
                continue
            seen.add(url)
            loc = ", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x) \
                  or (j.get("cityState") or "")
            rows.append({"title": (j.get("title") or "").strip(), "url": url,
                         "location": loc,
                         "found_date": (str(j.get("postedDate") or j.get("dateCreated") or ""))[:10]})
        offset += len(jobs)
        if total and offset >= total:
            break
        time.sleep(random.uniform(0.2, 0.5))
    return rows


# ---- Oracle Cloud Recruiting (ORC) — {tenant}.oraclecloud.com career sites ----
def _oracle_parts(board_url):
    """(origin, site_number) from an ORC careers URL, e.g.
       https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_45001/..."""
    p = urlparse(board_url)
    m = re.search(r"/sites/([A-Za-z0-9_]+)", p.path)
    return "%s://%s" % (p.scheme or "https", p.netloc), (m.group(1) if m else "CX_1")


ORACLE_PAGE = 200
ORACLE_MAX_JOBS = 3000


def scrape_oracle(board_url):
    """Oracle Cloud Recruiting career sites via the public recruitingCEJobRequisitions
    REST API (the same one the site's own search calls — no auth). Unlocks employers
    on Oracle HCM (lots of banks/pharma/industrials) that no other ATS feed covers.
    Bonus: the list response carries the JD text, so score_jobs needs no detail calls."""
    origin, site = _oracle_parts(board_url)
    if not urlparse(origin).netloc.lower().endswith(".oraclecloud.com"):
        return []                  # a stored 'oracle' board must really be an Oracle host
    rows, seen, offset, total = [], set(), 0, None
    while offset < ORACLE_MAX_JOBS:
        try:
            d = _get_json(origin + "/hcmRestApi/resources/latest/recruitingCEJobRequisitions",
                          params={"onlyData": "true",
                                  "expand": "requisitionList.secondaryLocations",
                                  "finder": "findReqs;siteNumber=%s,limit=%d,offset=%d,sortBy=POSTING_DATES_DESC"
                                            % (site, ORACLE_PAGE, offset)})
        except Exception:
            break
        items = d.get("items") or []
        reqs = (items[0].get("requisitionList") or []) if items else []
        if not reqs:
            break
        if total is None:
            total = items[0].get("TotalJobsCount") or 0
        for q in reqs:
            jid = str(q.get("Id") or "")
            if not jid or jid in seen:
                continue
            seen.add(jid)
            locs = [q.get("PrimaryLocation") or ""]
            for s in (q.get("secondaryLocations") or []):
                locs.append((s.get("Name") if isinstance(s, dict) else str(s)) or "")
            rows.append({"title": (q.get("Title") or "").strip(),
                         "url": "%s/hcmUI/CandidateExperience/en/sites/%s/job/%s"
                                % (origin, site, jid),
                         "location": "; ".join(x for x in locs if x),
                         "found_date": (str(q.get("PostedDate") or ""))[:10]})
        offset += len(reqs)
        if total and offset >= total:
            break
        time.sleep(random.uniform(0.2, 0.5))
    return rows


# ---- Workable — apply.workable.com/{slug} ----
def _workable_slug(board_url):
    p = urlparse(board_url)
    host = p.netloc.lower()
    segs = [s for s in p.path.split("/") if s]
    if host.endswith(".workable.com") and host not in ("apply.workable.com", "www.workable.com"):
        return host.split(".")[0]                     # {slug}.workable.com vanity host
    return segs[0] if segs else ""                    # apply.workable.com/{slug}


def scrape_workable(board_url):
    """Workable via the public v3 jobs search the apply.workable.com pages call (POST,
    paged by a nextPage token). The older v1 'widget' endpoint often returns an empty
    list even for live tenants — don't use it."""
    slug = _workable_slug(board_url)
    if not slug:
        return []
    api = "https://apply.workable.com/api/v3/accounts/%s/jobs" % slug
    hdr = dict(HEADERS); hdr["Content-Type"] = "application/json"
    rows, seen, token = [], set(), ""
    for _ in range(30):                               # 10/page -> up to 300 postings
        body = {"query": "", "department": [], "location": [],
                "remote": [], "workplace": [], "worktype": []}
        if token:
            body["token"] = token
        r = SESSION.post(api, headers=hdr, timeout=20, data=json.dumps(body))
        if r.status_code != 200:
            break
        d = r.json()
        for j in d.get("results") or []:
            sc = j.get("shortcode") or ""
            if not sc or sc in seen:
                continue
            seen.add(sc)
            loc = j.get("location") or {}
            location = ", ".join(x for x in (loc.get("city"), loc.get("region"),
                                             loc.get("country")) if x)
            if j.get("remote"):
                location = (location + " (Remote)").strip()
            rows.append({"title": (j.get("title") or "").strip(),
                         "url": "https://apply.workable.com/%s/j/%s/" % (slug, sc),
                         "location": location})
        token = d.get("nextPage") or ""
        if not token:
            break
        time.sleep(random.uniform(0.2, 0.4))
    return rows


SCRAPERS = {
    "greenhouse": scrape_greenhouse,
    "lever": scrape_lever,
    "ashby": scrape_ashby,
    "smartrecruiters": scrape_smartrecruiters,
    "amazon": scrape_amazon,
    "workday": scrape_workday,
    "jibe": scrape_jibe,
    "recruitee": scrape_recruitee,
    "breezy": scrape_breezy,
    "personio": scrape_personio,
    "jsonld": scrape_jsonld,
    "adzuna": scrape_adzuna,
    "phenom": scrape_phenom,
    "oracle": scrape_oracle,
    "workable": scrape_workable,
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

    if "recruitee.com" in host:
        sub = host.split(".")[0]
        return ("https://%s.recruitee.com" % sub, "recruitee", _name_from(sub))

    if "breezy.hr" in host:
        sub = host.split(".")[0]
        return ("https://%s.breezy.hr" % sub, "breezy", _name_from(sub))

    if "personio.com" in host:
        sub = host.split(".")[0]
        return ("https://%s.jobs.personio.com" % sub, "personio", _name_from(sub))

    if "myworkdayjobs.com" in host or "myworkdaysite.com" in host:
        _h, tenant, site = _workday_parts(url)
        if tenant and site:
            norm = ("https://%s/recruiting/%s/%s" % (_h, tenant, site)
                    if "myworkdaysite.com" in host else "https://%s/%s" % (_h, site))
            return (norm, "workday", _name_from(tenant))

    if host.endswith(".oraclecloud.com") and "/sites/" in p.path:
        origin, site = _oracle_parts(url)
        return ("%s/hcmUI/CandidateExperience/en/sites/%s" % (origin, site),
                "oracle", _name_from(host.split(".")[0]))

    if host.endswith("workable.com"):
        slug = _workable_slug(url)
        if slug:
            return ("https://apply.workable.com/%s" % slug, "workable", _name_from(slug))

    return None


def detect_jibe(url):
    """Network probe for iCIMS 'Career Sites' (Jibe). These run on custom domains
    (careers.<company>.com) or *.jibeapply.com and can't be told apart from any other
    site by URL alone — but they all expose GET /api/jobs JSON. Returns
    (origin_url, 'jibe', name) if that feed is present, else None. Used as a fallback
    when detect_board() doesn't match one of the fixed ATS hosts."""
    url = (url or "").strip()
    if not url:
        return None
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    p = urlparse(url)
    base = "%s://%s" % (p.scheme, p.netloc)
    try:
        r = _safe_get(base + "/api/jobs?limit=1", timeout=10)
        if r.status_code == 200 and "json" in r.headers.get("content-type", "").lower():
            d = r.json()
            if isinstance(d, dict) and "jobs" in d and ("totalCount" in d or "count" in d):
                name = ""
                jobs = d.get("jobs") or []
                if jobs:
                    data = jobs[0].get("data", {}) or {}
                    name = data.get("brand") or data.get("hiring_organization") or ""
                if not name:                          # fall back to the domain label
                    host = p.netloc.split(":")[0]
                    parts = [x for x in host.split(".") if x not in ("www", "careers", "jobs")]
                    name = _name_from(parts[0]) if parts else host
                return (base, "jibe", name)
    except Exception:
        return None
    return None


def detect_phenom(url):
    """Network probe for Phenom People career sites (careers.<company>.com style) —
    custom domains can't be told apart by URL, but they all answer the public
    POST /widgets job-search JSON. When the tenant's apply links point at Workday
    (Phenom is often just the front-end — Labcorp, Southwest), return the WORKDAY
    board behind it instead: richer data and JD support. Else (origin,'phenom',name)."""
    url = (url or "").strip()
    if not url:
        return None
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    p = urlparse(url)
    base = "%s://%s" % (p.scheme, p.netloc)
    try:
        r = _safe_post(base + "/widgets", _phenom_body(0, 1), timeout=12)
        if r.status_code != 200 or "json" not in r.headers.get("content-type", "").lower():
            return None
        d = (r.json() or {}).get("refineSearch") or {}
        if not isinstance(d.get("totalHits"), int):
            return None
        jobs = (d.get("data") or {}).get("jobs") or []
        apply_url = (jobs[0].get("applyUrl") or "") if jobs else ""
        if "myworkdayjobs.com" in apply_url or "myworkdaysite.com" in apply_url:
            wd = detect_board(apply_url)
            if wd:
                return wd
        host = p.netloc.split(":")[0]
        parts = [x for x in host.split(".")
                 if x not in ("www", "careers", "jobs", "career", "mycareer")]
        return (base, "phenom", _name_from(parts[0]) if parts else host)
    except Exception:
        return None


# URLs of scrapeable ATS platforms as they appear inside a company careers PAGE.
# detect_linked_ats() fetches the page and follows the first of these it finds.
_ATS_LINK_RE = re.compile(
    r"""https?://(?:
        (?:job-boards|boards)\.greenhouse\.io/[A-Za-z0-9_-]+
      | boards\.greenhouse\.io/embed/job_board\?for=[A-Za-z0-9_-]+
      | jobs\.lever\.co/[A-Za-z0-9_-]+
      | jobs\.ashbyhq\.com/[A-Za-z0-9_-]+
      | jobs\.smartrecruiters\.com/[A-Za-z0-9_-]+
      | [a-z0-9-]+\.wd\d+\.myworkdayjobs\.com/[A-Za-z0-9_/-]+
      | wd\d+\.myworkdaysite\.com/recruiting/[A-Za-z0-9_/-]+
      | [a-z0-9-]+\.jibeapply\.com
      | apply\.workable\.com/[A-Za-z0-9_-]+
      | [a-z0-9-]+\.recruitee\.com
      | [a-z0-9-]+\.breezy\.hr
      | [a-z0-9-]+\.jobs\.personio\.com
      | [a-z0-9.-]+\.oraclecloud\.com/hcmUI/CandidateExperience[A-Za-z0-9_/.-]*/sites/[A-Za-z0-9_]+
    )""", re.X | re.I)


def detect_linked_ats(url):
    """Follow-the-link detect: fetch a company CAREERS PAGE and look for a link to a
    scrapeable ATS inside its HTML (lots of corporate sites are a marketing page whose
    'View jobs' button goes to Greenhouse/Workday/etc.). Returns the same
    (board_url, ats_type, name) tuple as detect_board, or None."""
    url = (url or "").strip()
    if not url:
        return None
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    try:
        r = _safe_get(url, timeout=15)
        if r.status_code != 200:
            return None
        html_text = r.text
    except Exception:
        return None
    seen = set()
    for m in _ATS_LINK_RE.finditer(html_text):
        cand = m.group(0)
        if cand in seen:
            continue
        seen.add(cand)
        det = detect_board(cand)
        if det:
            return det
        if "jibeapply.com" in cand.lower():
            det = detect_jibe(cand)
            if det:
                return det
    return None


def detect_jsonld(url):
    """Last-resort generic detect: if a page embeds schema.org JobPosting structured
    data, we can read it. Returns (url, 'jsonld', name) when >=1 posting is found."""
    url = (url or "").strip()
    if not url:
        return None
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    try:
        if scrape_jsonld(url):
            host = urlparse(url).netloc.split(":")[0]
            parts = [x for x in host.split(".") if x not in ("www", "careers", "jobs", "job")]
            return (url, "jsonld", _name_from(parts[0]) if parts else host)
    except Exception:
        return None
    return None


def probe_board(board_url, ats_type):
    """Hit the board's API and return how many postings it exposes right now
    (0 = reachable but empty; None = couldn't read it). Used to validate before saving."""
    try:
        slug = board_url.rstrip("/").split("/")[-1]
        if ats_type == "greenhouse":
            r = SESSION.get("https://boards-api.greenhouse.io/v1/boards/%s/jobs" % slug,
                            headers=HEADERS, timeout=10)
            return len(r.json().get("jobs", [])) if r.status_code == 200 else None
        if ats_type == "lever":
            r = SESSION.get("https://api.lever.co/v0/postings/%s?mode=json" % slug,
                            headers=HEADERS, timeout=10)
            d = r.json() if r.status_code == 200 else None
            return len(d) if isinstance(d, list) else None
        if ats_type == "ashby":
            r = SESSION.get("https://api.ashbyhq.com/posting-api/job-board/%s" % slug,
                            headers=HEADERS, timeout=10)
            return len(r.json().get("jobs", [])) if r.status_code == 200 else None
        if ats_type == "smartrecruiters":
            r = SESSION.get("https://api.smartrecruiters.com/v1/companies/%s/postings?limit=1" % slug,
                            headers=HEADERS, timeout=10)
            return r.json().get("totalFound") if r.status_code == 200 else None
        if ats_type == "workday":
            host, tenant, site = _workday_parts(board_url)
            cxs = "https://%s/wday/cxs/%s/%s/jobs" % (host, tenant, site)
            hdr = dict(HEADERS); hdr["Content-Type"] = "application/json"
            r = SESSION.post(cxs, headers=hdr, timeout=12, data=json.dumps(
                {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}))
            return r.json().get("total") if r.status_code == 200 else None
        if ats_type == "jibe":
            p = urlparse(board_url)
            base = "%s://%s" % (p.scheme, p.netloc)
            r = _safe_get(base + "/api/jobs?limit=1", timeout=12)
            if r.status_code == 200:
                d = r.json()
                return d.get("totalCount") or d.get("count") or len(d.get("jobs", []))
            return None
        if ats_type == "phenom":
            p = urlparse(board_url)
            r = _safe_post("%s://%s/widgets" % (p.scheme or "https", p.netloc),
                           _phenom_body(0, 1), timeout=12)
            if r.status_code == 200:
                return ((r.json() or {}).get("refineSearch") or {}).get("totalHits")
            return None
        if ats_type == "oracle":
            origin, site = _oracle_parts(board_url)
            d = _get_json(origin + "/hcmRestApi/resources/latest/recruitingCEJobRequisitions",
                          params={"onlyData": "true",
                                  "finder": "findReqs;siteNumber=%s,limit=1,offset=0" % site})
            items = d.get("items") or []
            return items[0].get("TotalJobsCount") if items else None
        if ats_type == "workable":
            return len(scrape_workable(board_url))
        if ats_type == "recruitee":
            return len(scrape_recruitee(board_url))
        if ats_type == "breezy":
            return len(scrape_breezy(board_url))
        if ats_type == "personio":
            return len(scrape_personio(board_url))
        if ats_type == "jsonld":
            return len(scrape_jsonld(board_url))
        if ats_type == "adzuna":
            return len(scrape_adzuna(board_url))
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
    if re.search(r"\b\d+\s+locations?\b|multiple locations?", low):
        return True                                 # bare 'N Locations' count -> unknown, keep
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

def scrape_all(sources, workers=8):
    """Scrape boards CONCURRENTLY (each is an independent host) so the whole run takes
    a few minutes, not ~30. One bad source never stops the run."""
    def _one(entry):
        url, ats_type, company = entry
        fn = SCRAPERS.get(ats_type)
        if fn is None:
            return company, None, "unknown ats_type '%s'" % ats_type
        try:
            time.sleep(random.uniform(0, 1.0))          # small stagger so we don't burst one API
            rows = fn(url)
            for r in rows:
                r["company"] = company
            return company, rows, None
        except Exception as e:
            return company, None, str(e)

    all_jobs = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for company, rows, err in ex.map(_one, sources):     # results come back in source order
            if err is not None:
                print(f"  FAIL {company:<26} {err}")
            elif rows is None:
                print(f"  SKIP {company:<26}")
            else:
                all_jobs.extend(rows)
                print(f"  OK   {company:<26} {len(rows):>3} postings")
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
    tally = {"already known": 0, "senior/off-target title": 0,
             "no matching role keyword": 0, "non-US location": 0}
    for j in scraped:
        if j["url"] in seen:
            tally["already known"] += 1
            continue                       # already in jobs.csv from a past run
        keep, why = title_verdict(j["title"])
        if not keep:
            tally["senior/off-target title" if why.startswith("looks senior")
                  else "no matching role keyword"] += 1
        elif US_ONLY and not is_us_location(j.get("location", "")):
            keep, why = False, "non-US location (%s)" % (j.get("location") or "n/a")
            tally["non-US location"] += 1
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
    try:                                  # breadcrumb so notify.py can email just this run's new jobs
        json.dump(kept, open("last_new_jobs.json", "w", encoding="utf-8"))
    except Exception:
        pass

    dropped = ", ".join("%d %s" % (n, k) for k, n in tally.items() if n)
    print(f"\nScanned {len(scraped)} postings ({dropped or 'nothing dropped'}).")
    print(f"{len(kept)} NEW matching job(s):")
    for j in kept:
        flag = "" if j["sponsors_h1b"] != "yes" else "  [sponsors H1B]"
        print(f"  - {j['title']} - {j['company']} ({j['location'] or 'n/a'}){flag}")
        print(f"    {j['url']}")
    if kept:
        where = "Supabase" if db.using_supabase() else OUTPUT_CSV
        print(f"\nSaved to {where}. Run `python -m scraper.score_jobs` next to score them.")
    else:
        print("Nothing new this run.")


if __name__ == "__main__":
    main()
