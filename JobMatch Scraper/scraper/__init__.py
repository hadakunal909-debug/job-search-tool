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
import html
import json
import os
import sys
import time
import random
import socket
import ipaddress
import threading
import concurrent.futures
import re
import datetime
import email.utils      # RFC-822 dates (Aquent's XML feed)
from urllib.parse import (urljoin, urlparse, parse_qs, unquote,
                          urlsplit, urlunsplit, parse_qsl, urlencode)

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
    # SPECULATIVE — currently yields NOTHING, unlike every other entry here.
    #
    # The best of probe_everify_xlsx's 2026-08-09 sweep: of 26k E-Verify employers, 537 cleared
    # the >=500-staff bar, 8 had a scrapeable board, and this was the only one both filing LCAs
    # (242 USCIS approvals; h1b + green_card + stem_opt) and free of a retail flood — Dollar
    # General's board is 88,948 postings.
    #
    # But measured the day it was added: 55 postings, 24 pass the title filter, ZERO US-located.
    # The board is Germany/Singapore/UK/Vietnam; only 5 USA roles exist and none match (two
    # Client Partners, Business Development, Field Marketing, and a "Consultant Developer" the
    # filter has no phrase for). That misses the >=3-US-matches bar stated at the top of this
    # list, so it is kept on the employer's merits rather than on measured yield: a real sponsor
    # with US operations whose board may carry matching roles later, at ~0.5s a run to find out.
    # Drop it if a later sweep still shows zero.
    ("https://job-boards.greenhouse.io/thoughtworks", "greenhouse", "ThoughtWorks"),  # 0 US
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
    ("https://jobs.smartrecruiters.com/LegendBiotech",      "smartrecruiters", "Legend Biotech"),
    ("https://jobs.smartrecruiters.com/TurnerConstruction", "smartrecruiters", "Turner Construction"),
    ("https://jobs.smartrecruiters.com/UniversityofSouthFlorida", "smartrecruiters", "University of South Florida"),
    # --- Added 2026-06-02: recovered via corrected slugs (find_boards' name-guess missed these) ---
    ("https://job-boards.greenhouse.io/digitalocean98",     "greenhouse", "DigitalOcean"),
    ("https://jobs.smartrecruiters.com/clarivateanalytics", "smartrecruiters", "Clarivate"),
    # --- Added 2026-06-06: probe of the Level-I DOL list. The legit big employers
    # are kept; the generic IT-staffing/body-shop entries (People Tech, Softworld,
    # Sri Tech, Tech Tammina, Federal Soft Systems, Skilltune, Iris Software) were
    # REMOVED 2026-06-13 — OPT/H-1B body-shops we don't want to surface or endorse. ---
    ("https://job-boards.greenhouse.io/purestorage",            "greenhouse", "Pure Storage"),
    ("https://jobs.smartrecruiters.com/HarvardUniversity",      "smartrecruiters", "Harvard University"),
    # --- Added 2026-06-10: probe of the user's H1B LCA list (board names verified) ---
    ("https://job-boards.greenhouse.io/byd",                    "greenhouse", "BYD America"),
    ("https://jobs.ashbyhq.com/deel",                           "ashby", "Deel"),
    # --- Added 2026-06-13 via detect_linked_ats on careers.point72.com ---
    ("https://job-boards.greenhouse.io/point72",                "greenhouse", "Point72"),
    # --- Added 2026-06-13 (E-Verify major-employer sweep, find_everify_boards.py;
    # each identity verified by sampling the board's real job titles). ---
    ("https://jobs.smartrecruiters.com/CACI",                   "smartrecruiters", "CACI"),
    ("https://jobs.smartrecruiters.com/AbbVie",                 "smartrecruiters", "AbbVie"),
    ("https://jobs.smartrecruiters.com/Microchip",              "smartrecruiters", "Microchip"),
    ("https://jobs.smartrecruiters.com/NorthwesternMutual",     "smartrecruiters", "Northwestern Mutual"),
    ("https://job-boards.greenhouse.io/coinbase",               "greenhouse", "Coinbase"),
    ("https://job-boards.greenhouse.io/roblox",                 "greenhouse", "Roblox"),
    ("https://job-boards.greenhouse.io/cloudflare",             "greenhouse", "Cloudflare"),
    ("https://job-boards.greenhouse.io/waymo",                  "greenhouse", "Waymo"),
    # --- Added 2026-06-16 (user request): Greenhouse, confirmed via probe_board (~20). ---
    ("https://job-boards.greenhouse.io/calendly",               "greenhouse", "Calendly"),
    # --- Added 2026-06-17 (E-Verify+ list mine, probe_everify_xlsx.py): real companies with a
    # readable board; counts at add time. Consultancies + hourly-retail floods deliberately excluded. ---
    ("https://job-boards.greenhouse.io/scoutmotors",            "greenhouse", "Scout Motors"),       # ~207
    ("https://job-boards.greenhouse.io/okx",                    "greenhouse", "OKX"),                # ~262
    ("https://job-boards.greenhouse.io/zetaglobal",             "greenhouse", "Zeta Global"),        # ~152
    ("https://jobs.ashbyhq.com/benchling",                      "ashby", "Benchling"),               # ~48
    ("https://job-boards.greenhouse.io/zuora",                  "greenhouse", "Zuora"),              # ~38
    ("https://job-boards.greenhouse.io/buildops",               "greenhouse", "BuildOps"),           # ~28
    ("https://job-boards.greenhouse.io/twistbioscience",        "greenhouse", "Twist Bioscience"),   # ~25
    ("https://jobs.smartrecruiters.com/CDKGlobal",              "smartrecruiters", "CDK Global"),    # ~10
    ("https://jobs.smartrecruiters.com/Kenvue",                 "smartrecruiters", "Kenvue"),        # ~7
    ("https://job-boards.greenhouse.io/slideinsurance",         "greenhouse", "Slide Insurance"),    # ~20
    ("https://job-boards.greenhouse.io/rocketems",              "greenhouse", "Rocket EMS"),         # ~27
    ("https://job-boards.greenhouse.io/metroveincenters",       "greenhouse", "Metro Vein Centers"), # ~52
    # --- Added 2026-06-17 (user request): LG Electronics. lge-careers.com is a WordPress
    # front-end whose Apply links go to Greenhouse (slug `lgelectronics`) — so we read the
    # board directly (LG North America, ~103 postings). ---
    ("https://job-boards.greenhouse.io/lgelectronics",          "greenhouse", "LG Electronics"),     # ~103
    # --- Added 2026-06-18 (DOL LCA FY2026-Q2 top H1B sponsors via find_everify_boards;
    # each slug verified by sampling real job titles/locations). Counts at add time. ---
    ("https://job-boards.greenhouse.io/zscaler",                "greenhouse", "Zscaler"),            # ~335
    ("https://job-boards.greenhouse.io/coreweave",              "greenhouse", "CoreWeave"),          # ~272
    ("https://job-boards.greenhouse.io/reddit",                 "greenhouse", "Reddit"),             # ~147
    ("https://job-boards.greenhouse.io/rubrik",                 "greenhouse", "Rubrik"),             # ~113
    ("https://job-boards.greenhouse.io/duolingo",               "greenhouse", "Duolingo"),           # ~60
    ("https://job-boards.greenhouse.io/applovin",               "greenhouse", "AppLovin"),           # ~28
    ("https://jobs.lever.co/zoox",                              "lever", "Zoox"),                    # ~203
    ("https://jobs.smartrecruiters.com/AristaNetworks",         "smartrecruiters", "Arista Networks"), # ~247
    # --- Added 2026-06-18 (LCA FY2026-Q2 web-search wave 2) ---
    ("https://jobs.lever.co/spotify",                           "lever", "Spotify"),                 # ~136
    ("https://job-boards.greenhouse.io/hubspotjobs",            "greenhouse", "HubSpot"),            # ~164
    # --- Added 2026-08-07 (user-supplied link; the ?for= embed URL normalizes to this) ---
    ("https://job-boards.greenhouse.io/netradyne",              "greenhouse", "Netradyne"),          # ~28
    # --- Added 2026-08-07: 64 boards recovered by probing the 771 employers that appear on
    # migratemate.co's product/project pages but weren't in SOURCES. Every one below had its
    # identity CONFIRMED — the board's own reported name matched the company on a whole-string
    # fuzzy check, not the subset check that once let "Charles Schwab" bind to a board called
    # "charles". The unverified/guessed hits from that probe were deliberately NOT added.
    # GE Vernova and AEP came from reading their careers pages by hand: their Workday sites are
    # "Vernova_ExternalSite" / "AEPCareerSite", which no slug guess can reach. ---
    ("https://jobs.smartrecruiters.com/eurofins",               "smartrecruiters", "Eurofins"),      # ~2552
    ("https://gevernova.wd5.myworkdayjobs.com/Vernova_ExternalSite", "workday", "GE Vernova"),       # ~2150
    ("https://job-boards.greenhouse.io/speechify",              "greenhouse", "Speechify"),          # ~1302
    ("https://job-boards.greenhouse.io/coupang",                "greenhouse", "Coupang"),            # ~666
    ("https://jobs.smartrecruiters.com/rrdonnelley",            "smartrecruiters", "RR Donnelley"),  # ~569
    ("https://job-boards.greenhouse.io/anthropic",              "greenhouse", "Anthropic"),          # ~390
    ("https://job-boards.greenhouse.io/braze",                  "greenhouse", "Braze"),              # ~258
    ("https://job-boards.greenhouse.io/veeamsoftware",          "greenhouse", "Veeam Software"),     # ~241
    ("https://job-boards.greenhouse.io/astspacemobile",         "greenhouse", "AST SpaceMobile"),    # ~209
    ("https://job-boards.greenhouse.io/formlabs",               "greenhouse", "Formlabs"),           # ~203
    ("https://job-boards.greenhouse.io/monsterenergy",          "greenhouse", "Monster Energy"),     # ~182
    ("https://jobs.smartrecruiters.com/testingxperts",          "smartrecruiters", "Testingxperts"), # ~179
    ("https://job-boards.greenhouse.io/riotgames",              "greenhouse", "Riot Games"),         # ~167
    ("https://job-boards.greenhouse.io/figma",                  "greenhouse", "Figma"),              # ~167
    ("https://jobs.smartrecruiters.com/accionlabs",             "smartrecruiters", "Accion Labs"),   # ~166
    ("https://aep.wd1.myworkdayjobs.com/AEPCareerSite",         "workday", "American Electric Power"), # ~162
    ("https://jobs.smartrecruiters.com/kpffconsultingengineers", "smartrecruiters", "KPFF Consulting Engineers"), # ~157
    ("https://job-boards.greenhouse.io/gotion",                 "greenhouse", "Gotion"),             # ~157
    ("https://job-boards.greenhouse.io/cannondesign",           "greenhouse", "CannonDesign"),       # ~148
    ("https://job-boards.greenhouse.io/redwoodmaterials",       "greenhouse", "Redwood Materials"),  # ~139
    ("https://jobs.smartrecruiters.com/sodexo",                 "smartrecruiters", "Sodexo"),        # ~126
    ("https://job-boards.greenhouse.io/gusto",                  "greenhouse", "Gusto"),              # ~95
    ("https://jobs.smartrecruiters.com/universityofnotredame",  "smartrecruiters", "University of Notre Dame"), # ~94
    ("https://job-boards.greenhouse.io/fanduel",                "greenhouse", "FanDuel"),            # ~87
    ("https://grantthornton.recruitee.com/",                    "recruitee", "Grant Thornton"),      # ~86
    ("https://job-boards.greenhouse.io/neuralink",              "greenhouse", "Neuralink"),          # ~79
    ("https://job-boards.greenhouse.io/hunterdouglas",          "greenhouse", "Hunter Douglas"),     # ~78
    ("https://job-boards.greenhouse.io/caddellconstruction",    "greenhouse", "Caddell Construction"), # ~61
    ("https://job-boards.greenhouse.io/cpisecurity",            "greenhouse", "CPI Security"),       # ~54
    ("https://job-boards.greenhouse.io/codeandtheory",          "greenhouse", "Code and Theory"),    # ~53
    ("https://job-boards.greenhouse.io/peloton",                "greenhouse", "Peloton"),            # ~52
    ("https://job-boards.greenhouse.io/discord",                "greenhouse", "Discord"),            # ~48
    ("https://job-boards.greenhouse.io/summittherapeutics",     "greenhouse", "Summit Therapeutics"), # ~47
    ("https://job-boards.greenhouse.io/flex",                   "greenhouse", "Flex"),               # ~47
    ("https://job-boards.greenhouse.io/willmengconstruction",   "greenhouse", "Willmeng Construction"), # ~46
    ("https://job-boards.greenhouse.io/project44",              "greenhouse", "project44"),          # ~34
    ("https://job-boards.greenhouse.io/jfrog",                  "greenhouse", "JFrog"),              # ~34
    ("https://job-boards.greenhouse.io/pacificfusion",          "greenhouse", "Pacific Fusion"),     # ~33
    ("https://job-boards.greenhouse.io/clearstreet",            "greenhouse", "Clear Street"),       # ~28
    ("https://job-boards.greenhouse.io/altoslabs",              "greenhouse", "Altos Labs"),         # ~25
    ("https://job-boards.greenhouse.io/waymark",                "greenhouse", "Waymark"),            # ~19
    ("https://jobs.smartrecruiters.com/saintgobain",            "smartrecruiters", "Saint-Gobain"),  # ~18
    ("https://job-boards.greenhouse.io/innovid",                "greenhouse", "Innovid"),            # ~18
    ("https://job-boards.greenhouse.io/collectivehealth",       "greenhouse", "Collective Health"),  # ~15
    ("https://aramark.recruitee.com/",                          "recruitee", "Aramark"),             # ~15
    ("https://job-boards.greenhouse.io/udemy",                  "greenhouse", "Udemy"),              # ~12
    ("https://job-boards.greenhouse.io/siliconranch",           "greenhouse", "Silicon Ranch"),      # ~12
    ("https://job-boards.greenhouse.io/motive",                 "greenhouse", "Motive"),             # ~11
    ("https://job-boards.greenhouse.io/generatebiomedicines",   "greenhouse", "Generate Biomedicines"), # ~11
    ("https://jobs.smartrecruiters.com/bostonmedicalcenter",    "smartrecruiters", "Boston Medical Center"), # ~10
    ("https://job-boards.greenhouse.io/brightcoreenergy",       "greenhouse", "Brightcore Energy"),  # ~9
    ("https://jobs.smartrecruiters.com/servicetitan",           "smartrecruiters", "ServiceTitan"),  # ~8
    ("https://job-boards.greenhouse.io/lattice",                "greenhouse", "Lattice"),            # ~8
    ("https://jobs.smartrecruiters.com/centraprise",            "smartrecruiters", "Centraprise"),   # ~7
    ("https://jobs.smartrecruiters.com/celanese",               "smartrecruiters", "Celanese"),      # ~5
    ("https://jobs.smartrecruiters.com/brookfieldproperties",   "smartrecruiters", "Brookfield Properties"), # ~4
    ("https://job-boards.greenhouse.io/pliancy",                "greenhouse", "Pliancy"),            # ~4
    ("https://jobs.smartrecruiters.com/henryschein",            "smartrecruiters", "Henry Schein"),  # ~3
    ("https://jobs.smartrecruiters.com/athene",                 "smartrecruiters", "Athene"),        # ~1
    ("https://jobs.smartrecruiters.com/sutterhealth",           "smartrecruiters", "Sutter Health"), # ~1
    ("https://jobs.smartrecruiters.com/stifel",                 "smartrecruiters", "Stifel"),        # ~1
    ("https://jobs.smartrecruiters.com/kimberlyclark",          "smartrecruiters", "Kimberly-Clark"), # ~1
    ("https://jobs.smartrecruiters.com/applicantz",             "smartrecruiters", "Applicantz"),    # ~1
    ("https://jobs.smartrecruiters.com/tikehaucapital",         "smartrecruiters", "Tikehau Capital"), # ~1
    # --- Added 2026-08-07: probed from migratemate.co's public sponsor directory
    # (scraper/mine_migratemate.py -> scraper/probe_migratemate.py). Every entry had its
    # IDENTITY VERIFIED: the board reported a matching name, or it was reached by resolving
    # the company's own domain, or the slug is the company name verbatim. Unverified and
    # single-token guesses were NOT added. Dollar General / PetSmart / Ulta were dropped —
    # ~110k frontline retail postings the title filter discards anyway, at real fetch cost. ---
    ("https://davita.wd1.myworkdayjobs.com/DKC_External", "workday", "DaVita"),              # ~3486
    ("https://careers.cintas.com", "successfactors", "Cintas"),                              # ~2359
    ("https://hdox.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1", "oracle", "Quest Diagnostics"), # ~2095
    ("https://fivebelow.wd1.myworkdayjobs.com/fivebelowcareers", "workday", "Five Below"),   # ~2000
    ("https://genpact.wd108.myworkdayjobs.com/External_Careers", "workday", "Genpact"),      # ~2000
    ("https://mmc.wd1.myworkdayjobs.com/MMC", "workday", "Marsh"),                           # ~1976
    ("https://jabil.wd5.myworkdayjobs.com/Jabil_Careers", "workday", "Jabil"),               # ~1901
    ("https://republic.wd5.myworkdayjobs.com/Republic", "workday", "Republic Services"),     # ~1708
    ("https://skechers.wd5.myworkdayjobs.com/One-career-site", "workday", "Skechers"),       # ~1669
    ("https://kbr.wd5.myworkdayjobs.com/KBR_Careers", "workday", "KBR"),                     # ~1640
    ("https://wawa.wd1.myworkdayjobs.com/careers", "workday", "Wawa"),                       # ~1505
    ("https://careers.pruitthealth.com", "phenom", "PruittHealth"),                          # ~1475
    ("https://careers.accentcare.com", "jibe", "AccentCare"),                                # ~1386
    ("https://careers.mastec-civil.com", "jibe", "Mastec Civil"),                            # ~1361
    ("https://ur.wd1.myworkdayjobs.com/URcareers", "workday", "United Rentals"),             # ~1353
    ("https://richemont.wd3.myworkdayjobs.com/richemont", "workday", "Richemont"),           # ~1291
    ("https://careers.orlandohealth.com", "jibe", "Orlando Health"),                         # ~1269
    ("https://careers.rollins.com", "jibe", "Rollins"),                                      # ~1227
    ("https://jobs.smartrecruiters.com/RedBull", "smartrecruiters", "Red Bull"),             # ~1173
    ("https://carrier.wd5.myworkdayjobs.com/jobs", "workday", "Carrier"),                    # ~1171
    ("https://careers.celestica.com", "successfactors", "Celestica"),                        # ~1089
    ("https://jobs.sephora.com", "successfactors", "Sephora"),                               # ~1077
    ("https://sunbeltrentals.wd1.myworkdayjobs.com/sbcareers", "workday", "Sunbelt Rentals"), # ~1042
    ("https://ohiohealth.wd5.myworkdayjobs.com/OhioHealthJobs", "workday", "OhioHealth"),    # ~1009
    ("https://weis.wd108.myworkdayjobs.com/Careers", "workday", "Weis Markets"),             # ~973
    ("https://kone.wd3.myworkdayjobs.com/Careers", "workday", "KONE"),                       # ~962
    ("https://gianteagle.wd503.myworkdayjobs.com/GEExternalcareers", "workday", "Giant Eagle"), # ~866
    ("https://autonation.wd5.myworkdayjobs.com/Careers", "workday", "AutoNation"),           # ~854
    ("https://careers.medpace.com", "jibe", "Medpace"),                                      # ~764
    ("https://jobs.ametek.com", "successfactors", "AMETEK"),                                 # ~739
    ("https://job-boards.greenhouse.io/capco", "greenhouse", "Capco"),                       # ~728
    ("https://aegistherapies.wd1.myworkdayjobs.com/AegisCareers", "workday", "Aegis Therapies"), # ~720
    ("https://synnex.wd5.myworkdayjobs.com/tdsynnexcareers", "workday", "TD SYNNEX"),        # ~711
    ("https://ecolab.wd1.myworkdayjobs.com/Ecolab_External", "workday", "Ecolab"),           # ~704
    ("https://jobs.smartrecruiters.com/PublicStorage", "smartrecruiters", "Public Storage"), # ~676
    ("https://careers.quiktrip.com", "successfactors", "QuikTrip"),                          # ~630
    ("https://fa-ewji-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1", "oracle", "eClerx"), # ~578
    ("https://careers.paychex.com", "jibe", "Paychex"),                                      # ~574
    ("https://careers.andritz.com", "successfactors", "ANDRITZ"),                            # ~571
    ("https://jobs.cemex.com", "successfactors", "CEMEX"),                                   # ~544
    ("https://jobs.lever.co/lyrahealth", "lever", "Lyra Health"),                            # ~526
    ("https://honorhealth.wd12.myworkdayjobs.com/HonorHealth_careers", "workday", "HonorHealth"), # ~513
    ("https://careers.crocs.com", "successfactors", "Crocs"),                                # ~511
    ("https://careers.teradyne.com", "successfactors", "Teradyne"),                          # ~501
    ("https://hckd.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1", "oracle", "Molina Healthcare"), # ~442
    ("https://jobs.lever.co/shieldai", "lever", "Shield AI"),                                # ~433
    ("https://jobs.ferrero.com", "successfactors", "Ferrero"),                               # ~431
    ("https://chenmed.wd1.myworkdayjobs.com/ChenMed", "workday", "ChenMed"),                 # ~417
    ("https://airliquidehr.wd3.myworkdayjobs.com/AirgasExternalCareer", "workday", "Airgas"), # ~412
    ("https://fortrea.wd1.myworkdayjobs.com/Fortrea", "workday", "Fortrea"),                 # ~396
    ("https://unisys.wd5.myworkdayjobs.com/External", "workday", "Unisys"),                  # ~396
    ("https://atriumhospitality.wd5.myworkdayjobs.com/AtriumHospitality", "workday", "Atrium Hospitality"), # ~384
    ("https://jobs.townpump.com", "phenom", "Town Pump"),                                    # ~378
    ("https://jobs.dana.com", "successfactors", "Dana"),                                     # ~368
    ("https://jobs.smartrecruiters.com/Konecranes", "smartrecruiters", "Konecranes"),        # ~365
    ("https://careers.swissport.com", "jibe", "Swissport"),                                  # ~365
    ("https://careers.ucb.com", "phenom", "UCB"),                                            # ~362
    ("https://jobs.gft.com", "successfactors", "GFT"),                                       # ~361
    ("https://sunrun.wd5.myworkdayjobs.com/Sunrun_Careers", "workday", "Sunrun"),            # ~352
    ("https://job-boards.greenhouse.io/olsson", "greenhouse", "Olsson"),                     # ~334
    ("https://jobs.growmark.com", "successfactors", "GROWMARK"),                             # ~329
    ("https://careers.nrgenergy.com", "successfactors", "NRG Energy"),                       # ~324
    ("https://careers.yash.com", "successfactors", "YASH Technologies"),                     # ~311
    ("https://careers.airmethods.com", "jibe", "Air Methods"),                               # ~304
    ("https://job-boards.greenhouse.io/canonical", "greenhouse", "Canonical"),               # ~304
    ("https://jobs.ashbyhq.com/fluidstack", "ashby", "Fluidstack"),                          # ~287
    ("https://job-boards.greenhouse.io/convenientmd", "greenhouse", "ConvenientMD"),         # ~283
    ("https://job-boards.greenhouse.io/revolutionmedicines", "greenhouse", "Revolution Medicines"), # ~270
    ("https://jobs.zs.com", "jibe", "ZS"),                                                   # ~270
    ("https://jobs.kerry.com", "phenom", "Kerry"),                                           # ~264
    ("https://job-boards.greenhouse.io/loenbro", "greenhouse", "Loenbro"),                   # ~259
    ("https://cubesmart.jibeapply.com", "jibe", "CubeSmart"),                                # ~255
    ("https://jobs.arkema.com", "successfactors", "Arkema"),                                 # ~251
    ("https://jobs.smartrecruiters.com/Canva", "smartrecruiters", "Canva"),                  # ~245
    ("https://roberthalf.wd1.myworkdayjobs.com/RobertHalfStaffingCareers", "workday", "Robert Half"), # ~236
    ("https://job-boards.greenhouse.io/natera", "greenhouse", "Natera"),                     # ~226
    ("https://job-boards.greenhouse.io/alphasense", "greenhouse", "AlphaSense"),             # ~225
    ("https://careers.brp.com", "phenom", "BRP"),                                            # ~216
    ("https://republicfinance.jibeapply.com", "jibe", "Republic Finance"),                   # ~216
    ("https://careers.unitedsiteservices.com", "jibe", "United Site Services"),              # ~215
    ("https://jobs.puig.com", "successfactors", "Puig"),                                     # ~213
    ("https://careers.hanger.com", "phenom", "Hanger"),                                      # ~209
    ("https://clarios.wd5.myworkdayjobs.com/clarioscareers", "workday", "Clarios"),          # ~208
    ("https://logitech.wd5.myworkdayjobs.com/Logitech", "workday", "Logitech"),              # ~203
    ("https://careers.technipfmc.com", "successfactors", "TechnipFMC"),                      # ~201
    ("https://job-boards.greenhouse.io/fivetran", "greenhouse", "Fivetran"),                 # ~198
    ("https://job-boards.greenhouse.io/appian", "greenhouse", "Appian"),                     # ~194
    ("https://careers.southwire.com", "successfactors", "Southwire Company"),                # ~194
    ("https://jobs.ashbyhq.com/formenergy", "ashby", "Form Energy"),                         # ~184
    ("https://jobs.lever.co/xsolla", "lever", "Xsolla"),                                     # ~181
    ("https://jobs.smartrecruiters.com/Endava", "smartrecruiters", "Endava"),                # ~178
    ("https://jobs.ashbyhq.com/whoop", "ashby", "Whoop"),                                    # ~175
    ("https://jda.wd5.myworkdayjobs.com/JDA_Careers", "workday", "Blue Yonder"),             # ~173
    ("https://careers.teleflex.com", "successfactors", "Teleflex"),                          # ~173
    ("https://job-boards.greenhouse.io/via", "greenhouse", "via"),                           # ~173
    ("https://jobs.lever.co/Aprio", "lever", "Aprio"),                                       # ~170
    ("https://job-boards.greenhouse.io/asteralabs", "greenhouse", "Astera Labs"),            # ~167
    ("https://careers.incyte.com", "jibe", "Incyte"),                                        # ~167
    ("https://job-boards.greenhouse.io/clickhouse", "greenhouse", "ClickHouse"),             # ~166
    ("https://jobs.barry-callebaut.com", "successfactors", "Barry Callebaut"),               # ~163
    ("https://brambles.wd5.myworkdayjobs.com/Brambles_Careers", "workday", "CHEP"),          # ~163
    ("https://careers.allanmyers.com", "jibe", "Allan Myers"),                               # ~159
    ("https://job-boards.greenhouse.io/workato", "greenhouse", "Workato"),                   # ~159
    ("https://job-boards.greenhouse.io/clarksoneyecare", "greenhouse", "Clarkson Eyecare"),  # ~156
    ("https://job-boards.greenhouse.io/epicgames", "greenhouse", "Epic Games"),              # ~155
    ("https://jobs.smartrecruiters.com/Freshworks", "smartrecruiters", "Freshworks"),        # ~155
    ("https://jobs.smartrecruiters.com/IntegratedDermatology", "smartrecruiters", "Integrated Dermatology"), # ~154
    ("https://jobs.ashbyhq.com/maintainx", "ashby", "MaintainX"),                            # ~154
    ("https://job-boards.greenhouse.io/five9", "greenhouse", "Five9"),                       # ~153
    ("https://job-boards.greenhouse.io/asana", "greenhouse", "Asana"),                       # ~150
    ("https://tarkett.wd3.myworkdayjobs.com/Tarkett_Careers", "workday", "Tarkett"),         # ~150
    ("https://criteo.wd3.myworkdayjobs.com/Criteo_Career_Site", "workday", "Criteo"),        # ~147
    ("https://job-boards.greenhouse.io/oneoncology", "greenhouse", "OneOncology"),           # ~146
    ("https://careers.avalara.com", "jibe", "Avalara"),                                      # ~144
    ("https://job-boards.greenhouse.io/cookunity", "greenhouse", "CookUnity"),               # ~143
    ("https://jobs.lever.co/includedhealth", "lever", "Included Health"),                    # ~143
    ("https://job-boards.greenhouse.io/netskope", "greenhouse", "Netskope"),                 # ~142
    ("https://careers.chobani.com", "successfactors", "Chobani"),                            # ~140
    ("https://job-boards.greenhouse.io/intersystems", "greenhouse", "InterSystems"),         # ~140
    ("https://job-boards.greenhouse.io/scopely", "greenhouse", "Scopely"),                   # ~140
    ("https://jobs.ashbyhq.com/cohere", "ashby", "Cohere"),                                  # ~139
    ("https://jobs.igt.com", "successfactors", "IGT"),                                       # ~138
    ("https://careers.rhimagnesita.com", "successfactors", "RHI Magnesita"),                 # ~138
    ("https://jobs.shamrockfoods.com", "phenom", "Shamrock Foods Company"),                  # ~137
    ("https://job-boards.greenhouse.io/jensenhughes", "greenhouse", "Jensen Hughes"),        # ~136
    ("https://eehb.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_2005", "oracle", "Rockland Trust Company"), # ~134
    ("https://therealreal.wd1.myworkdayjobs.com/Careers", "workday", "The RealReal"),        # ~134
    ("https://jobs.rogers.com", "successfactors", "Rogers"),                                 # ~133
    ("https://careers.belden.com", "successfactors", "Belden"),                              # ~130
    ("https://jobs.biontech.com", "successfactors", "BioNTech"),                             # ~128
    ("https://zendesk.wd1.myworkdayjobs.com/zendesk", "workday", "Zendesk"),                 # ~128
    ("https://careers.cvent.com", "jibe", "Cvent"),                                          # ~126
    ("https://jobs.bekaert.com", "successfactors", "Bekaert"),                               # ~123
    ("https://tt.wd503.myworkdayjobs.com/ThorntonTomasetti", "workday", "Thornton Tomasetti"), # ~121
    ("https://jobs.ashbyhq.com/uipath", "ashby", "UiPath"),                                  # ~121
    ("https://corespaces.jibeapply.com", "jibe", "Core Spaces"),                             # ~120
    ("https://recruiting.ultipro.com/ARH1000ARH/JobBoard/e5051b40-e91f-fa81-f0bf-ed2e9361f690", "ultipro", "Arhaus"), # ~119
    ("https://careers.eidebailly.com", "jibe", "Eide Bailly"),                               # ~118
    ("https://job-boards.greenhouse.io/ixllearning", "greenhouse", "IXL Learning"),          # ~117
    ("https://jobs.ashbyhq.com/skydio", "ashby", "Skydio"),                                  # ~114
    ("https://braunintertec.wd5.myworkdayjobs.com/BraunIntertecCareers", "workday", "Braun Intertec"), # ~113
    ("https://jobs.lever.co/extremenetworks", "lever", "Extreme Networks"),                  # ~112
    ("https://ebwg.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX", "oracle", "ACI Worldwide"), # ~110
    ("https://job-boards.greenhouse.io/fahertybrand", "greenhouse", "Faherty Brand"),        # ~110
    ("https://jobs.ashbyhq.com/etched", "ashby", "Etched"),                                  # ~109
    ("https://job-boards.greenhouse.io/geotab", "greenhouse", "Geotab"),                     # ~108
    ("https://jobs.ashbyhq.com/plaid", "ashby", "Plaid"),                                    # ~106
    ("https://job-boards.greenhouse.io/postman", "greenhouse", "Postman"),                   # ~106
    ("https://job-boards.greenhouse.io/advancedtechnologyservices", "greenhouse", "Advanced Technology Services"), # ~105
    ("https://job-boards.greenhouse.io/freedomcare", "greenhouse", "Freedom Care"),          # ~102
    ("https://job-boards.greenhouse.io/nuro", "greenhouse", "Nuro"),                         # ~102
    ("https://jobs.ashbyhq.com/langchain", "ashby", "LangChain"),                            # ~101
    ("https://job-boards.greenhouse.io/smartsheet", "greenhouse", "Smartsheet"),             # ~101
    ("https://job-boards.greenhouse.io/billiontoone", "greenhouse", "BillionToOne"),         # ~99
    ("https://careers.dentsplysirona.com", "successfactors", "Dentsply Sirona"),             # ~99
    ("https://wd5.myworkdaysite.com/recruiting/conmed/conmed", "workday", "CONMED"),         # ~98
    ("https://boseallaboutme.wd503.myworkdayjobs.com/Bose_Careers", "workday", "Bose"),      # ~97
    ("https://careers.primetals.com", "phenom", "Primetals Technologies"),                   # ~97
    ("https://msci.jibeapply.com", "jibe", "MSCI"),                                          # ~95
    ("https://job-boards.greenhouse.io/taboola", "greenhouse", "Taboola"),                   # ~95
    ("https://ameresco.wd5.myworkdayjobs.com/Ameresco", "workday", "Ameresco"),              # ~94
    ("https://job-boards.greenhouse.io/modernanimal", "greenhouse", "Modern Animal"),        # ~94
    ("https://job-boards.greenhouse.io/fictiv", "greenhouse", "Fictiv"),                     # ~93
    ("https://job-boards.greenhouse.io/justworks", "greenhouse", "Justworks"),               # ~93
    ("https://job-boards.greenhouse.io/onetrust", "greenhouse", "OneTrust"),                 # ~93
    ("https://careers.pacificorp.com", "successfactors", "PacifiCorp"),                      # ~93
    ("https://jobs.ashbyhq.com/socure", "ashby", "Socure"),                                  # ~93
    ("https://job-boards.greenhouse.io/blankstreet", "greenhouse", "Blank Street"),          # ~91
    ("https://jobs.lever.co/pattern", "lever", "Pattern"),                                   # ~90
    ("https://job-boards.greenhouse.io/uberfreight", "greenhouse", "Uber Freight"),          # ~87
    ("https://cordis.jibeapply.com", "jibe", "Cordis"),                                      # ~86
    ("https://job-boards.greenhouse.io/fashionnova", "greenhouse", "Fashion Nova"),          # ~85
    ("https://job-boards.greenhouse.io/apptronik", "greenhouse", "Apptronik"),               # ~84
    ("https://job-boards.greenhouse.io/vercel", "greenhouse", "Vercel"),                     # ~81
    ("https://job-boards.greenhouse.io/knowbe4", "greenhouse", "KnowBe4"),                   # ~80
    ("https://jobs.ashbyhq.com/mercor", "ashby", "MERCOR"),                                  # ~80
    ("https://job-boards.greenhouse.io/standishmanagement", "greenhouse", "Standish Management"), # ~79
    ("https://jobs.lever.co/aircall", "lever", "Aircall"),                                   # ~77
    ("https://blackbaud.wd1.myworkdayjobs.com/ExternalCareers", "workday", "Blackbaud"),     # ~77
    ("https://job-boards.greenhouse.io/crunchyroll", "greenhouse", "Crunchyroll"),           # ~76
    ("https://darktrace.wd3.myworkdayjobs.com/DarktaceExternal", "workday", "Darktrace"),    # ~76
    ("https://jobs.lever.co/mainspringenergy", "lever", "Mainspring Energy"),                # ~76
    ("https://job-boards.greenhouse.io/dynetherapeutics", "greenhouse", "Dyne Therapeutics"), # ~74
    ("https://job-boards.greenhouse.io/pubmatic", "greenhouse", "PubMatic"),                 # ~74
    ("https://job-boards.greenhouse.io/vaynermedia", "greenhouse", "VaynerMedia"),           # ~74
    ("https://job-boards.greenhouse.io/bloomreach", "greenhouse", "Bloomreach"),             # ~73
    ("https://jobs.lever.co/mantisinnovation", "lever", "Mantis Innovation"),                # ~73
    ("https://jobs.ashbyhq.com/delinea", "ashby", "Delinea"),                                # ~72
    ("https://jobs.lever.co/ttecdigital", "lever", "TTEC Digital"),                          # ~72
    ("https://fa-eyau-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1", "oracle", "Argano"), # ~71
    ("https://job-boards.greenhouse.io/cloverhealth", "greenhouse", "Clover Health"),        # ~71
    ("https://job-boards.greenhouse.io/opentable", "greenhouse", "OpenTable"),               # ~71
    ("https://job-boards.greenhouse.io/psiquantum", "greenhouse", "PsiQuantum"),             # ~71
    ("https://alignmenthealthcare.wd12.myworkdayjobs.com/ahc_external", "workday", "Alignment Healthcare"), # ~70
    ("https://job-boards.greenhouse.io/lotusworks", "greenhouse", "LotusWorks"),             # ~70
    ("https://livanova.wd5.myworkdayjobs.com/Search", "workday", "LivaNova"),                # ~70
    ("https://job-boards.greenhouse.io/pmg", "greenhouse", "PMG"),                           # ~70
    ("https://job-boards.greenhouse.io/trace3", "greenhouse", "Trace3"),                     # ~70
    ("https://job-boards.greenhouse.io/chowbus", "greenhouse", "Chowbus"),                   # ~69
    ("https://jobs.ashbyhq.com/commure", "ashby", "Commure"),                                # ~69
    ("https://job-boards.greenhouse.io/cribl", "greenhouse", "Cribl"),                       # ~69
    ("https://job-boards.greenhouse.io/ensono", "greenhouse", "Ensono"),                     # ~69
    ("https://job-boards.greenhouse.io/fireblocks", "greenhouse", "Fireblocks"),             # ~69
    ("https://job-boards.greenhouse.io/lightmatter", "greenhouse", "Lightmatter"),           # ~69
    ("https://job-boards.greenhouse.io/coherehealth", "greenhouse", "Cohere Health"),        # ~68
    ("https://jobs.ashbyhq.com/illumio", "ashby", "Illumio"),                                # ~68
    ("https://jobs.ashbyhq.com/industrious", "ashby", "Industrious"),                        # ~68
    ("https://jobs.lever.co/lendbuzz", "lever", "Lendbuzz"),                                 # ~68
    ("https://jobs.ashbyhq.com/lambda", "ashby", "Lambda"),                                  # ~67
    ("https://job-boards.greenhouse.io/vaco", "greenhouse", "Vaco"),                         # ~67
    ("https://job-boards.greenhouse.io/nexhealth", "greenhouse", "NexHealth"),               # ~65
    ("https://job-boards.greenhouse.io/convera", "greenhouse", "Convera"),                   # ~63
    ("https://jobs.ashbyhq.com/mapbox", "ashby", "Mapbox"),                                  # ~63
    ("https://job-boards.greenhouse.io/vaxcyte", "greenhouse", "Vaxcyte"),                   # ~63
    ("https://job-boards.greenhouse.io/digicert", "greenhouse", "DigiCert"),                 # ~62
    ("https://job-boards.greenhouse.io/guidepointsecurity", "greenhouse", "GuidePoint Security"), # ~62
    ("https://job-boards.greenhouse.io/clinchoice", "greenhouse", "ClinChoice"),             # ~61
    ("https://job-boards.greenhouse.io/faradayfuture", "greenhouse", "Faraday Future"),      # ~61
    ("https://jobs.ashbyhq.com/reflectionai", "ashby", "Reflection AI"),                     # ~61
    ("https://job-boards.greenhouse.io/tatari", "greenhouse", "Tatari"),                     # ~61
    ("https://eisai.wd5.myworkdayjobs.com/eisai", "workday", "Eisai"),                       # ~60
    ("https://jobs.lever.co/payjoy", "lever", "PayJoy"),                                     # ~59
    ("https://jobs.lever.co/sambatv", "lever", "Samba TV"),                                  # ~59
    ("https://job-boards.greenhouse.io/kaseya", "greenhouse", "Kaseya"),                     # ~59
    ("https://careers.aptean.com", "jibe", "Aptean"),                                        # ~58
    ("https://job-boards.greenhouse.io/torcrobotics", "greenhouse", "Torc Robotics"),        # ~58
    ("https://job-boards.greenhouse.io/agilityrobotics", "greenhouse", "Agility Robotics"),  # ~57
    ("https://job-boards.greenhouse.io/gigaenergy", "greenhouse", "Giga Energy"),            # ~56
    ("https://job-boards.greenhouse.io/zocdoc", "greenhouse", "Zocdoc"),                     # ~56
    ("https://jobs.ashbyhq.com/drata", "ashby", "Drata"),                                    # ~55
    ("https://job-boards.greenhouse.io/gallup", "greenhouse", "Gallup"),                     # ~55
    ("https://jobs.smartrecruiters.com/Mirantis", "smartrecruiters", "Mirantis"),            # ~55
    ("https://jobs.ashbyhq.com/writer", "ashby", "WRITER"),                                  # ~55
    ("https://job-boards.greenhouse.io/altruist", "greenhouse", "ALTRUIST"),                 # ~54
    ("https://americanregent.wd1.myworkdayjobs.com/American_Regent_Careers", "workday", "American Regent"), # ~53
    ("https://jobs.smartrecruiters.com/Brainlab", "smartrecruiters", "Brainlab"),            # ~53
    ("https://jobs.ashbyhq.com/instructure", "ashby", "Instructure"),                        # ~53
    ("https://jobs.lever.co/spreetail", "lever", "Spreetail"),                               # ~53
    ("https://careers.allnex.com", "successfactors", "allnex"),                              # ~52
    ("https://ehtl.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX", "oracle", "Resideo"), # ~52
    ("https://job-boards.greenhouse.io/forgen", "greenhouse", "Forgen"),                     # ~51
    ("https://job-boards.greenhouse.io/cargurus", "greenhouse", "CarGurus"),                 # ~50
    ("https://careers.knapp.com", "successfactors", "Knapp"),                                # ~50
    ("https://jobs.power-electronics.com", "successfactors", "POWER ELECTRONICS"),           # ~50
    ("https://jobs.ashbyhq.com/thumbtack", "ashby", "Thumbtack"),                            # ~50
    ("https://jobs.lever.co/acceldata", "lever", "Acceldata"),                               # ~49
    ("https://careers.assistrx.com", "jibe", "AssistRx"),                                    # ~49
    ("https://job-boards.greenhouse.io/fastly", "greenhouse", "Fastly"),                     # ~49
    ("https://jobs.lever.co/lessen", "lever", "Lessen"),                                     # ~49
    ("https://jobs.ashbyhq.com/lumaai", "ashby", "Luma AI"),                                 # ~49
    ("https://job-boards.greenhouse.io/sweetgreen", "greenhouse", "Sweetgreen"),             # ~49
    ("https://yelp.jibeapply.com", "jibe", "Yelp"),                                          # ~49
    ("https://jobs.smartrecruiters.com/Cricut", "smartrecruiters", "Cricut"),                # ~48
    ("https://job-boards.greenhouse.io/moloco", "greenhouse", "Moloco"),                     # ~47
    ("https://job-boards.greenhouse.io/skhynixamerica", "greenhouse", "SK Hynix America"),   # ~47
    ("https://jobs.lever.co/appletreedental", "lever", "Apple Tree Dental"),                 # ~46
    ("https://argenx.wd3.myworkdayjobs.com/External_Careers", "workday", "argenx"),          # ~46
    ("https://job-boards.greenhouse.io/azuritypharmaceuticals", "greenhouse", "Azurity Pharmaceuticals"), # ~46
    ("https://jobs.ashbyhq.com/hopper", "ashby", "Hopper"),                                  # ~46
    ("https://job-boards.greenhouse.io/spire", "greenhouse", "Spire"),                       # ~46
    ("https://job-boards.greenhouse.io/evgspecialtynetwork", "greenhouse", "EVG Specialty Network"), # ~45
    ("https://jobs.ashbyhq.com/meter", "ashby", "Meter"),                                    # ~45
    ("https://jobs.ashbyhq.com/sentilink", "ashby", "SentiLink"),                            # ~45
    ("https://job-boards.greenhouse.io/forter", "greenhouse", "Forter"),                     # ~43
    ("https://jobs.ashbyhq.com/reprally", "ashby", "RepRally"),                              # ~43
    ("https://job-boards.greenhouse.io/simplisafe", "greenhouse", "SimpliSafe"),             # ~43
    ("https://job-boards.greenhouse.io/snorkelai", "greenhouse", "Snorkel AI"),              # ~43
    ("https://job-boards.greenhouse.io/verifone", "greenhouse", "Verifone"),                 # ~43
    ("https://job-boards.greenhouse.io/beyondfinance", "greenhouse", "Beyond Finance"),      # ~42
    ("https://collegeboard.wd1.myworkdayjobs.com/Careers", "workday", "College Board"),      # ~42
    ("https://job-boards.greenhouse.io/devrev", "greenhouse", "DevRev"),                     # ~42
    ("https://job-boards.greenhouse.io/ridgeline", "greenhouse", "Ridgeline"),               # ~42
    ("https://jobs.lever.co/smarsh", "lever", "Smarsh"),                                     # ~42
    ("https://athenahealth.wd1.myworkdayjobs.com/External", "workday", "athenahealth"),      # ~41
    ("https://jobs.lever.co/diamondfoundry", "lever", "Diamond Foundry"),                    # ~41
    ("https://job-boards.greenhouse.io/kiniksapharmaceuticals", "greenhouse", "Kiniksa Pharmaceuticals"), # ~41
    ("https://jobs.ashbyhq.com/planhat", "ashby", "Planhat"),                                # ~41
    ("https://jobs.ashbyhq.com/serval", "ashby", "SERVAL"),                                  # ~41
    ("https://careers.somatus.com", "jibe", "Somatus"),                                      # ~41
    ("https://job-boards.greenhouse.io/clear", "greenhouse", "CLEAR"),                       # ~41
    ("https://jobs.lever.co/avalerehealth", "lever", "Avalere Health"),                      # ~40
    ("https://job-boards.greenhouse.io/gofundme", "greenhouse", "GoFundMe"),                 # ~40
    ("https://jobs.ashbyhq.com/junipersquare", "ashby", "Juniper Square"),                   # ~40
    ("https://kyndryl.wd5.myworkdayjobs.com/KyndrylEarlyCareers", "workday", "Kyndryl"),     # ~40
    ("https://job-boards.greenhouse.io/recordedfuture", "greenhouse", "Recorded Future"),    # ~40
    ("https://job-boards.greenhouse.io/singlestore", "greenhouse", "SingleStore"),           # ~40
    ("https://job-boards.greenhouse.io/tanium", "greenhouse", "Tanium"),                     # ~40
    ("https://job-boards.greenhouse.io/truveta", "greenhouse", "Truveta"),                   # ~40
    ("https://agilent.wd5.myworkdayjobs.com/Agilent_Student_Careers", "workday", "Agilent Technologies"), # ~39
    ("https://job-boards.greenhouse.io/lucidsoftware", "greenhouse", "Lucid Software"),      # ~39
    ("https://jobs.lever.co/reply", "lever", "REPLY"),                                       # ~39
    ("https://jobs.ashbyhq.com/betterup", "ashby", "BetterUp"),                              # ~38
    ("https://job-boards.greenhouse.io/flatironhealth", "greenhouse", "Flatiron Health"),    # ~38
    ("https://jobs.lever.co/innophos", "lever", "Innophos"),                                 # ~38
    ("https://job-boards.greenhouse.io/neo4j", "greenhouse", "Neo4j"),                       # ~38
    ("https://job-boards.greenhouse.io/pendo", "greenhouse", "Pendo"),                       # ~38
    ("https://job-boards.greenhouse.io/avride", "greenhouse", "Avride"),                     # ~37
    ("https://careers.gentherm.com", "successfactors", "Gentherm"),                          # ~37
    ("https://job-boards.greenhouse.io/phdata", "greenhouse", "phData"),                     # ~37
    ("https://jobs.ashbyhq.com/stepful", "ashby", "Stepful"),                                # ~37
    ("https://job-boards.greenhouse.io/doubleverify", "greenhouse", "DoubleVerify"),         # ~36
    ("https://job-boards.greenhouse.io/kikoff", "greenhouse", "Kikoff"),                     # ~36
    ("https://job-boards.greenhouse.io/amplitude", "greenhouse", "Amplitude"),               # ~35
    ("https://jobs.lever.co/cellares", "lever", "Cellares"),                                 # ~35
    ("https://job-boards.greenhouse.io/oportun", "greenhouse", "Oportun"),                   # ~35
    ("https://job-boards.greenhouse.io/vixxo", "greenhouse", "Vixxo"),                       # ~35
    ("https://jobs.lever.co/celerion", "lever", "Celerion"),                                 # ~34
    ("https://job-boards.greenhouse.io/komodohealth", "greenhouse", "Komodo Health"),        # ~34
    ("https://job-boards.greenhouse.io/life360", "greenhouse", "Life360"),                   # ~34
    ("https://job-boards.greenhouse.io/lonestarcircleofcare", "greenhouse", "Lone Star Circle of Care"), # ~34
    ("https://job-boards.greenhouse.io/akunacapital", "greenhouse", "Akuna Capital"),        # ~33
    ("https://jobs.ashbyhq.com/campfire", "ashby", "campfire"),                              # ~33
    ("https://archwellessentials.wd1.myworkdayjobs.com/Careers", "workday", "Freedom Mortgage"), # ~33
    ("https://job-boards.greenhouse.io/maxcessinternational", "greenhouse", "Maxcess International"), # ~33
    ("https://jobs.lever.co/outreach", "lever", "Outreach"),                                 # ~33
    ("https://jobs.lever.co/tutorintelligence", "lever", "Tutor Intelligence"),              # ~33
    ("https://jobs.smartrecruiters.com/Vitol", "smartrecruiters", "Vitol"),                  # ~33
    ("https://job-boards.greenhouse.io/arcesiumllc", "greenhouse", "Arcesium"),              # ~32
    ("https://recruiting.ultipro.com/INT1043EXCUR/JobBoard/ad5e5978-552f-4ef7-90c8-70ebb0a57994", "ultipro", "arrivia"), # ~32
    ("https://job-boards.greenhouse.io/atomicmachines", "greenhouse", "Atomic Machines"),    # ~32
    ("https://jobs.ashbyhq.com/dexmate", "ashby", "Dexmate"),                                # ~32
    ("https://job-boards.greenhouse.io/edgeconnex", "greenhouse", "EdgeConneX"),             # ~32
    ("https://job-boards.greenhouse.io/everlaw", "greenhouse", "Everlaw"),                   # ~32
    ("https://job-boards.greenhouse.io/nexamp", "greenhouse", "Nexamp"),                     # ~32
    ("https://job-boards.greenhouse.io/quinstreet", "greenhouse", "QuinStreet"),             # ~32
    ("https://jobs.ashbyhq.com/safetyculture", "ashby", "SafetyCulture"),                    # ~32
    ("https://job-boards.greenhouse.io/backblaze", "greenhouse", "Backblaze"),               # ~32
    ("https://job-boards.greenhouse.io/bandwidth", "greenhouse", "Bandwidth"),               # ~31
    ("https://careers.certara.com", "jibe", "Certara"),                                      # ~31
    ("https://jobs.ashbyhq.com/retell-ai", "ashby", "Retell AI"),                            # ~31
    ("https://job-boards.greenhouse.io/suvoda", "greenhouse", "Suvoda"),                     # ~31
    ("https://job-boards.greenhouse.io/vianttechnology", "greenhouse", "Viant Technology"),  # ~31
    ("https://job-boards.greenhouse.io/obsidiansecurity", "greenhouse", "Obsidian Security"), # ~30
    ("https://job-boards.greenhouse.io/omadahealth", "greenhouse", "Omada Health"),          # ~30
    ("https://job-boards.greenhouse.io/stockx", "greenhouse", "StockX"),                     # ~30
    ("https://job-boards.greenhouse.io/tia", "greenhouse", "Tia"),                           # ~30
    ("https://jobs.ashbyhq.com/astronomer", "ashby", "Astronomer"),                          # ~29
    ("https://jobs.ashbyhq.com/confluent", "ashby", "Confluent"),                            # ~29
    ("https://job-boards.greenhouse.io/druva", "greenhouse", "Druva"),                       # ~29
    ("https://jobs.smartrecruiters.com/Oetiker", "smartrecruiters", "Oetiker"),              # ~29
    ("https://job-boards.greenhouse.io/onbe", "greenhouse", "onbe"),                         # ~29
    ("https://job-boards.greenhouse.io/reltio", "greenhouse", "Reltio"),                     # ~28
    ("https://careers.dentons.com", "successfactors", "Dentons"),                            # ~27
    ("https://jobs.lever.co/parallelwireless", "lever", "Parallel Wireless"),                # ~27
    ("https://jobs.ashbyhq.com/traba", "ashby", "Traba"),                                    # ~27
    ("https://job-boards.greenhouse.io/cockroachlabs", "greenhouse", "Cockroach Labs"),      # ~26
    ("https://job-boards.greenhouse.io/gruve", "greenhouse", "Gruve"),                       # ~26
    ("https://job-boards.greenhouse.io/iterable", "greenhouse", "Iterable"),                 # ~26
    ("https://job-boards.greenhouse.io/perryellisinternational", "greenhouse", "Perry Ellis International"), # ~26
    ("https://job-boards.greenhouse.io/bayasystems", "greenhouse", "Baya Systems"),          # ~25
    ("https://job-boards.greenhouse.io/beamtherapeutics", "greenhouse", "Beam Therapeutics"), # ~25
    ("https://job-boards.greenhouse.io/cypresscreekrenewables", "greenhouse", "Cypress Creek Renewables"), # ~25
    ("https://jobs.lever.co/equativ", "lever", "Equativ"),                                   # ~25
    ("https://job-boards.greenhouse.io/otter", "greenhouse", "Otter"),                       # ~25
    ("https://jobs.ashbyhq.com/poshmark", "ashby", "Poshmark"),                              # ~25
    ("https://jobs.ashbyhq.com/workos", "ashby", "WorkOS"),                                  # ~25
    ("https://careers.aflac.com", "successfactors", "Aflac"),                                # ~24
    ("https://jobs.ashbyhq.com/cardless", "ashby", "Cardless"),                              # ~24
    ("https://job-boards.greenhouse.io/environmentalscienceassociates", "greenhouse", "Environmental Science Associates"), # ~24
    ("https://jobs.ashbyhq.com/envoy", "ashby", "Envoy"),                                    # ~24
    ("https://jobs.ashbyhq.com/redis", "ashby", "Redis"),                                    # ~24
    ("https://job-boards.greenhouse.io/aftership", "greenhouse", "AfterShip"),               # ~23
    ("https://jobs.lever.co/eliyan", "lever", "Eliyan"),                                     # ~23
    ("https://job-boards.greenhouse.io/khealthcareers", "greenhouse", "K Health"),           # ~23
    ("https://job-boards.greenhouse.io/lusternational", "greenhouse", "Luster National"),    # ~23
    ("https://jobs.lever.co/luxurypresence", "lever", "Luxury Presence"),                    # ~23
    ("https://job-boards.greenhouse.io/platformscience", "greenhouse", "Platform Science"),  # ~23
    ("https://jobs.ashbyhq.com/webai", "ashby", "webAI"),                                    # ~23
    ("https://jobs.lever.co/gridware", "lever", "Gridware"),                                 # ~22
    ("https://job-boards.greenhouse.io/pagerduty", "greenhouse", "PagerDuty"),               # ~22
    ("https://job-boards.greenhouse.io/sonatus", "greenhouse", "Sonatus"),                   # ~22
    ("https://job-boards.greenhouse.io/xairatherapeutics", "greenhouse", "Xaira Therapeutics"), # ~22
    ("https://job-boards.greenhouse.io/dataiku", "greenhouse", "Dataiku"),                   # ~21
    ("https://jobs.lever.co/goodleap", "lever", "GoodLeap"),                                 # ~21
    ("https://job-boards.greenhouse.io/minio", "greenhouse", "MinIO"),                       # ~21
    ("https://job-boards.greenhouse.io/pacvue", "greenhouse", "Pacvue"),                     # ~21
    ("https://jobs.ashbyhq.com/semperis", "ashby", "Semperis"),                              # ~21
    ("https://jobs.ashbyhq.com/strava", "ashby", "Strava"),                                  # ~21
    ("https://jobs.ashbyhq.com/ambiencehealthcare", "ashby", "Ambience Healthcare"),         # ~20
    ("https://jobs.ashbyhq.com/articul8", "ashby", "Articul8"),                              # ~20
    ("https://job-boards.greenhouse.io/mindbody", "greenhouse", "Mindbody"),                 # ~20
    ("https://job-boards.greenhouse.io/newsela", "greenhouse", "Newsela"),                   # ~20
    ("https://job-boards.greenhouse.io/redcellpartners", "greenhouse", "Red Cell Partners"), # ~20
    ("https://job-boards.greenhouse.io/grouppmx", "greenhouse", "Group PMX"),                # ~19
    ("https://jobs.lever.co/jumpcloud", "lever", "JumpCloud"),                               # ~19
    ("https://jobs.ashbyhq.com/parafin", "ashby", "Parafin"),                                # ~19
    ("https://jobs.ashbyhq.com/plasmidsaurus", "ashby", "Plasmidsaurus"),                    # ~19
    ("https://job-boards.greenhouse.io/radar", "greenhouse", "Radar"),                       # ~19
    ("https://careers.trilliumflow.com", "successfactors", "Trillium Flow Technologies"),    # ~19
    ("https://jobs.ashbyhq.com/virtahealth", "ashby", "Virta Health"),                       # ~19
    ("https://job-boards.greenhouse.io/amperity", "greenhouse", "Amperity"),                 # ~18
    ("https://job-boards.greenhouse.io/eikontherapeutics", "greenhouse", "Eikon Therapeutics"), # ~18
    ("https://job-boards.greenhouse.io/eulerity", "greenhouse", "Eulerity"),                 # ~18
    ("https://job-boards.greenhouse.io/ginkgobioworks", "greenhouse", "Ginkgo Bioworks"),    # ~18
    ("https://jobs.ashbyhq.com/gorgias", "ashby", "Gorgias"),                                # ~18
    ("https://jobs.lever.co/hottopic", "lever", "Hot Topic"),                                # ~18
    ("https://jobs.ashbyhq.com/overjet", "ashby", "OVERJET"),                                # ~18
    ("https://jobs.ashbyhq.com/plenful", "ashby", "Plenful"),                                # ~18
    ("https://job-boards.greenhouse.io/roboforce", "greenhouse", "RoboForce"),               # ~18
    ("https://job-boards.greenhouse.io/stubhubinc", "greenhouse", "StubHub"),                # ~18
    ("https://sunpower.breezy.hr", "breezy", "SunPower"),                                    # ~18
    ("https://job-boards.greenhouse.io/axiom", "greenhouse", "Axiom Technologies"),          # ~17
    ("https://job-boards.greenhouse.io/balsambrands", "greenhouse", "Balsam Brands"),        # ~17
    ("https://jobs.lever.co/doxel", "lever", "Doxel"),                                       # ~17
    ("https://job-boards.greenhouse.io/healthverity", "greenhouse", "HealthVerity"),         # ~17
    ("https://job-boards.greenhouse.io/known", "greenhouse", "Known"),                       # ~17
    ("https://jobs.lever.co/mashgin", "lever", "Mashgin"),                                   # ~17
    ("https://jobs.ashbyhq.com/mintlify", "ashby", "Mintlify"),                              # ~17
    ("https://jobs.lever.co/modeln", "lever", "Model N"),                                    # ~17
    ("https://job-boards.greenhouse.io/noahmedical", "greenhouse", "Noah Medical"),          # ~17
    ("https://jobs.ashbyhq.com/semgrep", "ashby", "Semgrep"),                                # ~17
    ("https://job-boards.greenhouse.io/squarespace", "greenhouse", "Squarespace"),           # ~17
    ("https://job-boards.greenhouse.io/thrivemarket", "greenhouse", "Thrive Market"),        # ~17
    ("https://job-boards.greenhouse.io/upgrade", "greenhouse", "Upgrade"),                   # ~17
    ("https://jobs.lever.co/wealthfront", "lever", "Wealthfront"),                           # ~17
    ("https://job-boards.greenhouse.io/array", "greenhouse", "Array Technologies"),          # ~16
    ("https://job-boards.greenhouse.io/bombas", "greenhouse", "Bombas"),                     # ~16
    ("https://job-boards.greenhouse.io/cambridgemobiletelematics", "greenhouse", "Cambridge Mobile Telematics"), # ~16
    ("https://job-boards.greenhouse.io/courierhealth", "greenhouse", "Courier Health"),      # ~16
    ("https://job-boards.greenhouse.io/gatherai", "greenhouse", "Gather AI"),                # ~16
    ("https://jobs.oregontool.com", "successfactors", "Oregon Tool"),                        # ~16
    ("https://job-boards.greenhouse.io/pointdigitalfinance", "greenhouse", "Point Digital Finance"), # ~16
    ("https://job-boards.greenhouse.io/semafor", "greenhouse", "SEMAFOR"),                   # ~16
    ("https://job-boards.greenhouse.io/typeface", "greenhouse", "Typeface"),                 # ~16
    ("https://job-boards.greenhouse.io/ultimagenomics", "greenhouse", "Ultima Genomics"),    # ~16
    ("https://job-boards.greenhouse.io/cogentbiosciences", "greenhouse", "Cogent Biosciences"), # ~15
    ("https://job-boards.greenhouse.io/lunarenergy", "greenhouse", "Lunar Energy"),          # ~15
    ("https://job-boards.greenhouse.io/nextdoor", "greenhouse", "Nextdoor"),                 # ~15
    ("https://jobs.ashbyhq.com/sphere", "ashby", "Sphere"),                                  # ~15
    ("https://jobs.lever.co/zimperium", "lever", "Zimperium"),                               # ~15
    ("https://job-boards.greenhouse.io/crexi", "greenhouse", "Crexi"),                       # ~14
    ("https://jobs.ashbyhq.com/deposco", "ashby", "Deposco"),                                # ~14
    ("https://lower.wd1.myworkdayjobs.com/lower_external_careers", "workday", "Lower"),      # ~14
    ("https://job-boards.greenhouse.io/spinnakersupport", "greenhouse", "Spinnaker Support"), # ~14
    ("https://jobs.lever.co/analyticpartners", "lever", "Analytic Partners"),                # ~13
    ("https://job-boards.greenhouse.io/avantus", "greenhouse", "Avantus"),                   # ~13
    ("https://jobs.ashbyhq.com/centivo", "ashby", "Centivo"),                                # ~13
    ("https://jobs.lever.co/cyngn", "lever", "Cyngn"),                                       # ~13
    ("https://job-boards.greenhouse.io/himarley", "greenhouse", "Hi Marley"),                # ~13
    ("https://job-boards.greenhouse.io/kodiaksolutions", "greenhouse", "Kodiak Solutions"),  # ~13
    ("https://job-boards.greenhouse.io/lendingtree", "greenhouse", "LendingTree"),           # ~13
    ("https://veev.breezy.hr", "breezy", "Lennar"),                                          # ~13
    ("https://job-boards.greenhouse.io/syndigo", "greenhouse", "Syndigo"),                   # ~13
    ("https://job-boards.greenhouse.io/voxmedia", "greenhouse", "Vox Media"),                # ~13
    ("https://jobs.ashbyhq.com/amigo", "ashby", "AMIGO"),                                    # ~12
    ("https://jobs.lever.co/calstart", "lever", "CALSTART"),                                 # ~12
    ("https://job-boards.greenhouse.io/capstoneinvestmentadvisors", "greenhouse", "Capstone Investment Advisors"), # ~12
    ("https://jobs.lever.co/duetti", "lever", "Duetti"),                                     # ~12
    ("https://job-boards.greenhouse.io/juullabs", "greenhouse", "Juul Labs"),                # ~12
    ("https://jobs.smartrecruiters.com/LongbridgeFinancial", "smartrecruiters", "Longbridge Financial"), # ~12
    ("https://job-boards.greenhouse.io/maesa", "greenhouse", "Maesa"),                       # ~12
    ("https://job-boards.greenhouse.io/neptunemedical", "greenhouse", "Neptune Medical"),    # ~12
    ("https://job-boards.greenhouse.io/precisionmedicine", "greenhouse", "Precision Medicine Group"), # ~12
    ("https://job-boards.greenhouse.io/stitchfix", "greenhouse", "Stitch Fix"),              # ~12
    ("https://jobs.ashbyhq.com/stedi", "ashby", "Stedi"),                                    # ~12
    ("https://jobs.ashbyhq.com/substack", "ashby", "Substack"),                              # ~12
    ("https://job-boards.greenhouse.io/taskrabbit", "greenhouse", "Taskrabbit"),             # ~12
    ("https://jobs.lever.co/aeratechnology", "lever", "Aera Technology"),                    # ~11
    ("https://jobs.ashbyhq.com/furtherai", "ashby", "FurtherAI"),                            # ~11
    ("https://job-boards.greenhouse.io/pivotbio", "greenhouse", "Pivot Bio"),                # ~11
    ("https://jobs.lever.co/activecampaign", "lever", "ActiveCampaign"),                     # ~10
    ("https://jobs.smartrecruiters.com/Alnylam", "smartrecruiters", "Alnylam"),              # ~10
    ("https://job-boards.greenhouse.io/avetta", "greenhouse", "Avetta"),                     # ~10
    ("https://job-boards.greenhouse.io/forgebiologics", "greenhouse", "Forge Biologics"),    # ~10
    ("https://job-boards.greenhouse.io/knit", "greenhouse", "Knit"),                         # ~10
    ("https://job-boards.greenhouse.io/koddi", "greenhouse", "Koddi"),                       # ~10
    ("https://job-boards.greenhouse.io/novacredit", "greenhouse", "Nova Credit"),            # ~10
    ("https://jobs.ashbyhq.com/titan", "ashby", "Titan"),                                    # ~10
    ("https://job-boards.greenhouse.io/vestmark", "greenhouse", "Vestmark"),                 # ~10
    ("https://job-boards.greenhouse.io/wasabi", "greenhouse", "Wasabi Technologies"),        # ~10
    ("https://job-boards.greenhouse.io/acuitymd", "greenhouse", "AcuityMD"),                 # ~9
    ("https://jobs.ashbyhq.com/capsule", "ashby", "Capsule"),                                # ~9
    ("https://jobs.ashbyhq.com/jellyfish", "ashby", "Jellyfish"),                            # ~9
    ("https://job-boards.greenhouse.io/locusrobotics", "greenhouse", "Locus Robotics"),      # ~9
    ("https://job-boards.greenhouse.io/pathai", "greenhouse", "PathAI"),                     # ~9
    ("https://job-boards.greenhouse.io/renewedvision", "greenhouse", "Renewed Vision"),      # ~9
    ("https://jobs.smartrecruiters.com/TexasHealthResources", "smartrecruiters", "Texas Health Resources"), # ~9
    ("https://jobs.ashbyhq.com/vesta", "ashby", "Vesta"),                                    # ~9
    ("https://jobs.smartrecruiters.com/RaasInfotek", "smartrecruiters", "Raas Infotek"),     # ~8
    # Todyl retired 2026-08-21: the Ashby board 404s at both casings (/todyl and /Todyl) after
    # returning 8 postings until 2026-08-20, and todyl.com/careers still links only to Ashby --
    # so the board was unpublished, not moved. Re-add if it comes back.
    ("https://job-boards.greenhouse.io/upwork", "greenhouse", "Upwork"),                     # ~8
    ("https://jobs.ashbyhq.com/blissway", "ashby", "BLISSWAY"),                              # ~7
    ("https://jobs.lever.co/disqo", "lever", "Disqo"),                                       # ~7
    ("https://job-boards.greenhouse.io/doximity", "greenhouse", "Doximity"),                 # ~7
    ("https://job-boards.greenhouse.io/magicleap", "greenhouse", "Magic Leap"),              # ~7
    ("https://jobs.ashbyhq.com/nusano", "ashby", "Nusano"),                                  # ~7
    ("https://job-boards.greenhouse.io/shopmonkey", "greenhouse", "Shopmonkey"),             # ~7
    ("https://jobs.lever.co/topazlabs", "lever", "Topaz Labs"),                              # ~7
    ("https://jobs.lever.co/aquabyte", "lever", "Aquabyte"),                                 # ~6
    ("https://jobs.ashbyhq.com/brunswick", "ashby", "Brunswick"),                            # ~6
    ("https://job-boards.greenhouse.io/hginsights", "greenhouse", "HG Insights"),            # ~6
    ("https://jobs.ashbyhq.com/hockeystack", "ashby", "HockeyStack"),                        # ~6
    ("https://jobs.ashbyhq.com/patreon", "ashby", "Patreon"),                                # ~6
    ("https://jobs.lever.co/valkyrietrading", "lever", "Valkyrie Trading"),                  # ~6
    ("https://jobs.smartrecruiters.com/Winsupply", "smartrecruiters", "Winsupply"),          # ~6
    ("https://careers.kula.ai/10xgenomics", "kula", "10x Genomics"),                         # ~31
    ("https://jobs.smartrecruiters.com/ChathamFinancial", "smartrecruiters", "Chatham Financial"), # ~5
    ("https://jobs.ashbyhq.com/eventual", "ashby", "Eventual"),                              # ~5
    ("https://job-boards.greenhouse.io/instabase", "greenhouse", "Instabase"),               # ~5
    ("https://jobs.lever.co/payactiv", "lever", "Payactiv"),                                 # ~5
    ("https://jobs.smartrecruiters.com/PresbyterianHealthcareServices", "smartrecruiters", "Presbyterian Healthcare Services"), # ~5
    ("https://job-boards.greenhouse.io/quanata", "greenhouse", "Quanata"),                   # ~5
    ("https://jobs.ashbyhq.com/quora", "ashby", "Quora"),                                    # ~5
    ("https://job-boards.greenhouse.io/resortpass", "greenhouse", "ResortPass"),             # ~5
    ("https://job-boards.greenhouse.io/invisible", "greenhouse", "Invisible Technologies"),  # ~5
    ("https://jobs.smartrecruiters.com/BAYADAHomeHealthCare", "smartrecruiters", "BAYADA Home Health Care"), # ~4
    ("https://jobs.lever.co/genesis", "lever", "Genesis"),                                   # ~4
    ("https://job-boards.greenhouse.io/runwise", "greenhouse", "Runwise"),                   # ~4
    ("https://job-boards.greenhouse.io/septerna", "greenhouse", "Septerna"),                 # ~4
    ("https://jobs.smartrecruiters.com/Vivint", "smartrecruiters", "Vivint"),                # ~4
    ("https://jobs.lever.co/voltai", "lever", "Voltai"),                                     # ~4
    ("https://jobs.smartrecruiters.com/YoungstownStateUniversity", "smartrecruiters", "Youngstown State University"), # ~4
    ("https://job-boards.greenhouse.io/videoamp", "greenhouse", "VideoAmp"),                 # ~4
    ("https://job-boards.greenhouse.io/fleishmanhillard", "greenhouse", "FleishmanHillard"), # ~3
    ("https://jobs.smartrecruiters.com/MissouriSouthernStateUniversity", "smartrecruiters", "Missouri Southern State University"), # ~3
    ("https://job-boards.greenhouse.io/mobilityware", "greenhouse", "MobilityWare"),         # ~3
    ("https://job-boards.greenhouse.io/owllabs", "greenhouse", "Owl Labs"),                  # ~3
    ("https://jobs.smartrecruiters.com/Synechron", "smartrecruiters", "Synechron"),          # ~3
    ("https://jobs.smartrecruiters.com/VITASHealthcare", "smartrecruiters", "VITAS Healthcare"), # ~3
    ("https://jobs.smartrecruiters.com/Zeeco", "smartrecruiters", "Zeeco"),                  # ~3
    ("https://jobs.smartrecruiters.com/armis", "smartrecruiters", "armis"),                  # ~2
    ("https://jobs.ashbyhq.com/cranston", "ashby", "CRANSTON"),                              # ~2
    ("https://jobs.smartrecruiters.com/FamilyHealthCentersofSanDiego", "smartrecruiters", "Family Health Centers of San Diego"), # ~2
    ("https://job-boards.greenhouse.io/imbue", "greenhouse", "Imbue"),                       # ~2
    ("https://jobs.smartrecruiters.com/KentStateUniversity", "smartrecruiters", "Kent State University"), # ~2
    ("https://jobs.smartrecruiters.com/NorthwesternMedicalCenter", "smartrecruiters", "Northwestern Medical Center"), # ~2
    ("https://jobs.smartrecruiters.com/UniversityofMinnesotaPhysicians", "smartrecruiters", "University of Minnesota Physicians"), # ~2
    ("https://jobs.smartrecruiters.com/AdvancedPhysicalTherapy", "smartrecruiters", "Advanced Physical Therapy"), # ~1
    ("https://jobs.smartrecruiters.com/AlabamaStateUniversity", "smartrecruiters", "Alabama State University"), # ~1
    ("https://jobs.lever.co/anyscale", "lever", "Anyscale"),                                 # ~1
    ("https://jobs.smartrecruiters.com/CEDSystems", "smartrecruiters", "CED Systems"),       # ~1
    ("https://jobs.smartrecruiters.com/ComfortKeepers", "smartrecruiters", "Comfort Keepers"), # ~1
    ("https://jobs.smartrecruiters.com/DGNTechnologies", "smartrecruiters", "DGN Technologies"), # ~1
    ("https://job-boards.greenhouse.io/invoca", "greenhouse", "Invoca"),                     # ~1
    ("https://jobs.smartrecruiters.com/LambWeston", "smartrecruiters", "Lamb Weston"),       # ~1
    ("https://jobs.smartrecruiters.com/MaineHealth", "smartrecruiters", "MaineHealth"),      # ~1
    ("https://jobs.smartrecruiters.com/Masimo", "smartrecruiters", "Masimo"),                # ~1
    ("https://jobs.smartrecruiters.com/My3Tech", "smartrecruiters", "My3Tech"),              # ~1
    ("https://jobs.smartrecruiters.com/Nsight", "smartrecruiters", "Nsight"),                # ~1
    ("https://jobs.smartrecruiters.com/PrimeSourceBuildingProducts", "smartrecruiters", "PrimeSource Building Products"), # ~1
    ("https://jobs.smartrecruiters.com/ProcDNA", "smartrecruiters", "ProcDNA"),              # ~1
    ("https://job-boards.greenhouse.io/resilience", "greenhouse", "Resilience"),             # ~1
    ("https://jobs.smartrecruiters.com/RentTheRunway", "smartrecruiters", "Rent The Runway"), # ~1
    ("https://jobs.smartrecruiters.com/SEGULATechnologies", "smartrecruiters", "SEGULA Technologies"), # ~1
    ("https://jobs.smartrecruiters.com/ShopLC", "smartrecruiters", "Shop LC"),               # ~1
    ("https://jobs.smartrecruiters.com/Wayfair", "smartrecruiters", "Wayfair"),              # ~1
    ("https://jobs.smartrecruiters.com/WaylandBaptistUniversity", "smartrecruiters", "Wayland Baptist University"), # ~1
    ("https://jobs.smartrecruiters.com/WiderCircle", "smartrecruiters", "Wider Circle"),     # ~1
    # --- Added 2026-08-07: probed from migratemate.co's public sponsor directory
    # (scraper/mine_migratemate.py -> scraper/probe_migratemate.py). Every entry had its
    # IDENTITY VERIFIED: the board reported a matching name, or it was reached by resolving
    # the company's own domain, or the slug is the company name verbatim. Unverified and
    # single-token guesses were NOT added. Dollar General / PetSmart / Ulta were dropped —
    # ~110k frontline retail postings the title filter discards anyway, at real fetch cost. ---
    ("https://careers.bureauveritas.com", "successfactors", "Bureau Veritas"),               # ~2048
    ("https://cw.wd1.myworkdayjobs.com/External", "workday", "Cushman & Wakefield"),         # ~2000
    ("https://freseniusmedicalcare.wd3.myworkdayjobs.com/fme", "workday", "Fresenius Medical Care"), # ~2000
    ("https://aspendental.wd1.myworkdayjobs.com/Careers_Aspen_Dental", "workday", "Aspen Dental"), # ~1742
    ("https://careers.quest-global.com", "phenom", "Quest Global"),                          # ~1704
    ("https://cnx.wd1.myworkdayjobs.com/external_global", "workday", "Concentrix"),          # ~1665
    ("https://careers.mcdean.com", "jibe", "M.C. Dean, Inc."),                               # ~1626
    ("https://ssmh.wd5.myworkdayjobs.com/ssmhealth", "workday", "SSM Health"),               # ~1622
    ("https://jobs.smartrecruiters.com/NorthwesternMedicine", "smartrecruiters", "Northwestern Medicine"), # ~1461
    ("https://rbc.wd3.myworkdayjobs.com/RBCGLOBAL1", "workday", "RBC"),                      # ~1388
    ("https://meijer.wd5.myworkdayjobs.com/Meijer_Stores_Hourly", "workday", "Meijer"),      # ~1385
    ("https://careers.lemartec.com", "jibe", "Lemartec"),                                    # ~1360
    ("https://carmax.wd1.myworkdayjobs.com/External", "workday", "CarMax"),                  # ~1209
    ("https://jobs.kuehne-nagel.com", "phenom", "Kuehne+Nagel"),                             # ~1116
    ("https://jobs.aon.com", "jibe", "Aon"),                                                 # ~1050
    ("https://gehc.wd5.myworkdayjobs.com/GEHC_ExternalSite", "workday", "GE HealthCare"),    # ~985
    ("https://conehealth.wd12.myworkdayjobs.com/Cone_Health-Careers", "workday", "Cone Health"), # ~925
    ("https://jobs.zf.com", "successfactors", "ZF"),                                         # ~827
    ("https://jobs.lever.co/gopuff", "lever", "Gopuff"),                                     # ~806
    ("https://dxctechnology.wd1.myworkdayjobs.com/DXCJobs", "workday", "CSC"),               # ~788
    ("https://jobs.ashbyhq.com/openai", "ashby", "OpenAI"),                                  # ~748
    ("https://gsk.wd5.myworkdayjobs.com/GSKCareers", "workday", "GSK"),                      # ~730
    ("https://saks.wd1.myworkdayjobs.com/careers_at_saks", "workday", "Saks Global"),        # ~728
    ("https://jobs.smartrecruiters.com/Equinox", "smartrecruiters", "Equinox"),              # ~674
    ("https://careers.ecslimited.com", "jibe", "ECS Limited"),                               # ~669
    ("https://belron.wd3.myworkdayjobs.com/Safelite_Careers", "workday", "Safelite"),        # ~669
    ("https://jobs.smartrecruiters.com/Wabtec", "smartrecruiters", "Wabtec"),                # ~644
    ("https://jobs.ashbyhq.com/airwallex", "ashby", "Airwallex"),                            # ~634
    ("https://careers.vetcor.com", "jibe", "Vetcor"),                                        # ~617
    ("https://jobs.exxonmobil.com", "successfactors", "ExxonMobil"),                         # ~590
    ("https://careers.hubbell.com", "successfactors", "Hubbell"),                            # ~578
    ("https://sedgwick.wd1.myworkdayjobs.com/Sedgwick", "workday", "Sedgwick"),              # ~551
    ("https://careers.conduent.com", "phenom", "Conduent"),                                  # ~537
    ("https://bdx.wd1.myworkdayjobs.com/EXTERNAL_CAREER_SITE_USA", "workday", "BD"),         # ~518
    ("https://regalrexnord.wd1.myworkdayjobs.com/Careers", "workday", "Regal Rexnord Corporation"), # ~505
    ("https://fa-evly-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1", "oracle", "TriHealth Inc."), # ~504
    ("https://crateandbarrel.wd1.myworkdayjobs.com/CBH", "workday", "Crate and Barrel"),     # ~502
    ("https://gohealthuc.wd12.myworkdayjobs.com/External", "workday", "UPMC"),               # ~440
    ("https://hccz.fa.em3.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_2", "oracle", "Pearson"), # ~439
    ("https://erhk.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1", "oracle", "R+L Carriers"), # ~437
    ("https://jobs.churchilldowns.com", "successfactors", "Churchill Downs Inc."),           # ~409
    ("https://careers.novonordisk.com", "successfactors", "Novo Nordisk, Inc."),             # ~407
    ("https://abcsupply.wd1.myworkdayjobs.com/ABCSupplyCareers", "workday", "ABC Supply Co., Inc."), # ~402
    ("https://quickenloans.wd5.myworkdayjobs.com/rocket_careers", "workday", "Rocket"),      # ~397
    ("https://icf.wd5.myworkdayjobs.com/ICFExternal_Career_Site", "workday", "ICF"),         # ~381
    ("https://oumedicine.wd5.myworkdayjobs.com/OUHealthCareers", "workday", "OU Health"),    # ~381
    ("https://job-boards.greenhouse.io/sumup", "greenhouse", "SumUp"),                       # ~377
    ("https://careers.zimmerbiomet.com", "phenom", "Zimmer Biomet"),                         # ~368
    ("https://ebez.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1", "oracle", "CBIZ"), # ~361
    ("https://jobs.ashbyhq.com/harvey", "ashby", "HARVEY"),                                  # ~361
    ("https://jobs.ashbyhq.com/crusoe", "ashby", "CRUSOE"),                                  # ~360
    ("https://myhrhome.wd1.myworkdayjobs.com/OneMainCareers", "workday", "OneMain Financial"), # ~352
    ("https://jobs.nucor.com", "successfactors", "Nucor Corporation"),                       # ~341
    ("https://travelers.wd5.myworkdayjobs.com/External", "workday", "Travelers"),            # ~341
    ("https://jobs.lear.com", "successfactors", "Lear Corporation"),                         # ~330
    ("https://careers.garmin.com", "jibe", "Garmin"),                                        # ~323
    ("https://jobs.newyorklife.com", "successfactors", "New York Life"),                     # ~321
    ("https://rxo.wd501.myworkdayjobs.com/rxojobs", "workday", "RXO"),                       # ~320
    ("https://elyb.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001", "oracle", "CentraCare"), # ~308
    ("https://jobs.smartrecruiters.com/SanDisk", "smartrecruiters", "SanDisk"),              # ~305
    ("https://jobs.grainger.com", "successfactors", "Grainger"),                             # ~304
    ("https://thrivent.wd5.myworkdayjobs.com/external", "workday", "Thrivent"),              # ~304
    ("https://careers.opentext.com", "phenom", "OpenText"),                                  # ~299
    ("https://careers.willscot.com", "successfactors", "WillScot"),                          # ~297
    ("https://careers.kindermorgan.com", "jibe", "Kinder Morgan"),                           # ~284
    ("https://jobs.nexteraenergy.com", "successfactors", "NextEra Energy"),                  # ~276
    ("https://eewl.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX", "oracle", "Sundt"), # ~260
    ("https://careers.getinge.com", "successfactors", "Getinge"),                            # ~254
    ("https://jobs.entergy.com", "successfactors", "Entergy"),                               # ~253
    ("https://victaulic.wd1.myworkdayjobs.com/victaulic_careers", "workday", "Victaulic"),   # ~236
    ("https://goodyear.wd1.myworkdayjobs.com/GoodyearCareers", "workday", "Goodyear"),       # ~235
    ("https://invesco.wd1.myworkdayjobs.com/IVZ", "workday", "Invesco"),                     # ~234
    ("https://careers.timken.com", "successfactors", "Timken"),                              # ~234
    ("https://jobs.statefarm.com", "jibe", "State Farm"),                                    # ~232
    ("https://careers.appliedmedical.com", "jibe", "Applied Medical"),                       # ~225
    ("https://jobs.constellationenergy.com", "jibe", "Constellation Energy"),                # ~223
    ("https://newbalance.wd1.myworkdayjobs.com/Careers", "workday", "New Balance"),          # ~221
    ("https://wwwinc.wd1.myworkdayjobs.com/WWW", "workday", "Wolverine Worldwide"),          # ~220
    ("https://hdep.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_2003", "oracle", "Digital Realty"), # ~219
    ("https://terex.wd1.myworkdayjobs.com/terexcareers", "workday", "Terex Corporation"),    # ~219
    ("https://job-boards.greenhouse.io/thetradedesk", "greenhouse", "The Trade Desk"),       # ~201
    ("https://careers.straumann.com", "phenom", "Straumann Group"),                          # ~195
    ("https://job-boards.greenhouse.io/42northdental", "greenhouse", "42 North Dental"),     # ~194
    ("https://jobs.smartrecruiters.com/SaxonGlobal", "smartrecruiters", "Saxon Global"),     # ~193
    ("https://careers.syncreon.com", "successfactors", "Syncreon"),                          # ~193
    ("https://job-boards.greenhouse.io/gitlab", "greenhouse", "GitLab"),                     # ~188
    ("https://careers.abskids.com", "phenom", "ABS Kids"),                                   # ~182
    ("https://jobs.enersys.com", "successfactors", "EnerSys"),                               # ~181
    ("https://frostbank.wd5.myworkdayjobs.com/external", "workday", "Frost Bank"),           # ~177
    ("https://sleepnumber.wd5.myworkdayjobs.com/sleepnumber", "workday", "Sleep Number Corporation"), # ~175
    ("https://careers.gates.com", "successfactors", "Gates Corporation"),                    # ~173
    ("https://lazboy.wd1.myworkdayjobs.com/LZBCareers", "workday", "La-Z-Boy"),              # ~173
    ("https://razer.wd3.myworkdayjobs.com/Careers", "workday", "Razer"),                     # ~166
    ("https://job-boards.greenhouse.io/thenewyorktimes", "greenhouse", "The New York Times"), # ~166
    ("https://eisneramper.wd1.myworkdayjobs.com/EisnerAmper_External", "workday", "EisnerAmper"), # ~164
    ("https://lifeworks.wd3.myworkdayjobs.com/External", "workday", "Telus"),                # ~164
    ("https://finastra.wd3.myworkdayjobs.com/FINC", "workday", "Finastra"),                  # ~162
    ("https://job-boards.greenhouse.io/sezzle", "greenhouse", "Sezzle"),                     # ~148
    ("https://workingatbooking.jibeapply.com", "jibe", "Booking Holdings"),                  # ~147
    ("https://formfactor.wd1.myworkdayjobs.com/FFI-Careers", "workday", "FormFactor, Inc."), # ~146
    ("https://job-boards.greenhouse.io/hasbro", "greenhouse", "Hasbro, Inc."),               # ~146
    ("https://xcelenergy.wd1.myworkdayjobs.com/External", "workday", "Xcel Energy"),         # ~146
    ("https://job-boards.greenhouse.io/bozzuto", "greenhouse", "Bozzuto"),                   # ~143
    ("https://job-boards.greenhouse.io/welbehealth", "greenhouse", "WelbeHealth"),           # ~142
    ("https://pru.wd5.myworkdayjobs.com/Careers", "workday", "Prudential"),                  # ~139
    ("https://jobs.smartrecruiters.com/JackLinksProteinSnacks", "smartrecruiters", "Jack Link's Protein Snacks"), # ~136
    ("https://assurant.wd1.myworkdayjobs.com/Assurant_Careers", "workday", "Assurant"),      # ~133
    ("https://job-boards.greenhouse.io/cortland", "greenhouse", "Cortland"),                 # ~133
    ("https://fugro.wd3.myworkdayjobs.com/Careers", "workday", "Fugro"),                     # ~133
    ("https://job-boards.greenhouse.io/ripple", "greenhouse", "Ripple"),                     # ~131
    ("https://centene.wd5.myworkdayjobs.com/centene_external", "workday", "Centene"),        # ~129
    ("https://jobs.ashbyhq.com/decagon", "ashby", "Decagon"),                                # ~125
    ("https://job-boards.greenhouse.io/lilasciences", "greenhouse", "Lila Sciences"),        # ~122
    ("https://jobs.smartrecruiters.com/StanfordMedicineChildrensHealth", "smartrecruiters", "Stanford Medicine Children's Health"), # ~116
    ("https://job-boards.greenhouse.io/wayve", "greenhouse", "Wayve"),                       # ~106
    ("https://job-boards.greenhouse.io/thinkacademyus", "greenhouse", "Think Academy US"),   # ~105
    ("https://careers.andersen.com", "jibe", "Andersen Corporation"),                        # ~104
    ("https://meadhunt.jibeapply.com", "jibe", "Mead & Hunt, Inc."),                         # ~103
    ("https://franklin-electric.pinpointhq.com", "pinpoint", "Franklin Electric"),           # ~99
    ("https://careers.starktech.com", "phenom", "Stark Tech"),                               # ~98
    ("https://job-boards.greenhouse.io/cresta", "greenhouse", "Cresta"),                     # ~97
    ("https://jobs.farmersinsurance.com", "successfactors", "Farmers Insurance Group"),      # ~97
    ("https://job-boards.greenhouse.io/upstart", "greenhouse", "Upstart"),                   # ~97
    ("https://careers.jameshardie.com", "successfactors", "James Hardie"),                   # ~95
    ("https://jobs.lever.co/coupa", "lever", "Coupa"),                                       # ~92
    ("https://jobs.ashbyhq.com/perplexity", "ashby", "Perplexity"),                          # ~92
    ("https://chrobinson.wd5.myworkdayjobs.com/CHRobinson", "workday", "C.H. Robinson"),     # ~90
    ("https://careers.merrick.com", "jibe", "Merrick & Company"),                            # ~89
    ("https://choicehotels.wd5.myworkdayjobs.com/HotelExternal", "workday", "Choice Hotels"), # ~85
    ("https://freddiemac.wd5.myworkdayjobs.com/External", "workday", "Freddie Mac"),         # ~85
    ("https://job-boards.greenhouse.io/orioninnovation", "greenhouse", "Orion Innovation"),  # ~83
    ("https://job-boards.greenhouse.io/motional", "greenhouse", "Motional"),                 # ~81
    ("https://job-boards.greenhouse.io/metropolis", "greenhouse", "Metropolis"),             # ~80
    ("https://careers.dominionenergy.com", "successfactors", "Dominion Energy"),             # ~79
    ("https://job-boards.greenhouse.io/realchemistry", "greenhouse", "Real Chemistry"),      # ~78
    ("https://jobs.lever.co/standtogether", "lever", "Stand Together"),                      # ~78
    ("https://prologis.wd5.myworkdayjobs.com/Prologis_External_Careers", "workday", "Prologis"), # ~77
    ("https://job-boards.greenhouse.io/rockstargames", "greenhouse", "Rockstar Games"),      # ~74
    ("https://jobs.franke.com", "successfactors", "Franke"),                                 # ~71
    ("https://steeleurope.wd3.myworkdayjobs.com/Job_Board", "workday", "thyssenkrupp"),      # ~71
    ("https://jobs.ashbyhq.com/headway", "ashby", "Headway"),                                # ~70
    ("https://careers.siriusxm.com", "jibe", "SiriusXM"),                                    # ~70
    ("https://job-boards.greenhouse.io/faire", "greenhouse", "Faire"),                       # ~66
    ("https://jobs.ashbyhq.com/riveron", "ashby", "Riveron"),                                # ~66
    ("https://unum.wd1.myworkdayjobs.com/External", "workday", "Unum"),                      # ~66
    ("https://job-boards.greenhouse.io/carta", "greenhouse", "Carta"),                       # ~64
    ("https://fmc.wd12.myworkdayjobs.com/FMC", "workday", "FMC Corporation"),                # ~64
    ("https://jobs.peabodyenergy.com", "successfactors", "Peabody Energy"),                  # ~63
    ("https://jobs.bourns.com", "successfactors", "Bourns"),                                 # ~62
    ("https://job-boards.greenhouse.io/blinkhealth", "greenhouse", "Blink Health"),          # ~60
    ("https://careers.keolis.com", "successfactors", "Keolis"),                              # ~60
    ("https://job-boards.greenhouse.io/geniussports", "greenhouse", "Genius Sports"),        # ~58
    ("https://job-boards.greenhouse.io/mercury", "greenhouse", "Mercury"),                   # ~57
    ("https://careers.cambrex.com", "jibe", "Cambrex"),                                      # ~56
    ("https://job-boards.greenhouse.io/getyourguide", "greenhouse", "GetYourGuide"),         # ~53
    ("https://job-boards.greenhouse.io/nintendo", "greenhouse", "Nintendo"),                 # ~52
    ("https://jobs.ashbyhq.com/serverobotics", "ashby", "Serve Robotics"),                   # ~52
    ("https://jobs.lever.co/provectus", "lever", "Provectus"),                               # ~50
    ("https://job-boards.greenhouse.io/futuresecureai", "greenhouse", "Future Secure AI"),   # ~49
    ("https://jobs.ashbyhq.com/carian", "ashby", "CARIAN"),                                  # ~48
    ("https://jobs.ashbyhq.com/sentry", "ashby", "Sentry"),                                  # ~47
    ("https://job-boards.greenhouse.io/tenableinc", "greenhouse", "Tenable"),                # ~46
    ("https://jobs.ashbyhq.com/sereact", "ashby", "Sereact"),                                # ~45
    ("https://job-boards.greenhouse.io/williamblair", "greenhouse", "William Blair & Company"), # ~45
    ("https://job-boards.greenhouse.io/trustpilot", "greenhouse", "Trustpilot"),             # ~43
    ("https://job-boards.greenhouse.io/axle", "greenhouse", "Axle"),                         # ~42
    ("https://jobs.lever.co/protective", "lever", "Protective"),                             # ~42
    ("https://job-boards.greenhouse.io/wing", "greenhouse", "Wing"),                         # ~42
    ("https://jobs.lever.co/metlife", "lever", "MetLife"),                                   # ~39
    ("https://jobs.ashbyhq.com/aerovect", "ashby", "AeroVect"),                              # ~38
    ("https://jobs.lever.co/solarlandscape", "lever", "Solar Landscape"),                    # ~37
    ("https://gentex.wd5.myworkdayjobs.com/Gentex", "workday", "Gentex Corporation"),        # ~35
    ("https://job-boards.greenhouse.io/algolia", "greenhouse", "Algolia"),                   # ~34
    ("https://job-boards.greenhouse.io/workstream", "greenhouse", "Workstream Technologies"), # ~33
    ("https://job-boards.greenhouse.io/freenome", "greenhouse", "Freenome"),                 # ~30
    ("https://job-boards.greenhouse.io/accuweather", "greenhouse", "AccuWeather"),           # ~29
    ("https://jobs.ashbyhq.com/physicalintelligence", "ashby", "Physical Intelligence"),     # ~29
    ("https://jobs.smartrecruiters.com/SoftpathSystemLLC", "smartrecruiters", "Softpath System LLC"), # ~28
    ("https://jobs.lever.co/pivotal", "lever", "Pivotal"),                                   # ~27
    ("https://jobs.hilmarcheese.com", "successfactors", "Hilmar Cheese Company"),            # ~26
    ("https://job-boards.greenhouse.io/mindgruve", "greenhouse", "Mindgruve"),               # ~26
    ("https://jobs.ashbyhq.com/paraform", "ashby", "Paraform"),                              # ~26
    ("https://job-boards.greenhouse.io/jumio", "greenhouse", "Jumio Corporation"),           # ~25
    ("https://job-boards.greenhouse.io/vestwell", "greenhouse", "Vestwell"),                 # ~25
    ("https://job-boards.greenhouse.io/kairospower", "greenhouse", "Kairos Power"),          # ~24
    ("https://job-boards.greenhouse.io/oculartherapeutix", "greenhouse", "Ocular Therapeutix, Inc."), # ~24
    ("https://careers.ofi.com", "successfactors", "OFI"),                                    # ~24
    ("https://job-boards.greenhouse.io/berkadia", "greenhouse", "Berkadia"),                 # ~23
    ("https://jobs.ashbyhq.com/oscilar", "ashby", "Oscilar"),                                # ~23
    ("https://job-boards.greenhouse.io/tebra", "greenhouse", "Tebra"),                       # ~23
    ("https://jobs.ashbyhq.com/versemedical", "ashby", "Verse Medical"),                     # ~23
    ("https://jobs.lever.co/bounteous", "lever", "Bounteous"),                               # ~22
    ("https://job-boards.greenhouse.io/quantifind", "greenhouse", "Quantifind"),             # ~22
    ("https://jobs.ashbyhq.com/centerfield", "ashby", "Centerfield"),                        # ~21
    ("https://careers.wheelsup.com", "jibe", "Wheels Up"),                                   # ~20
    ("https://careers.astellas.com", "successfactors", "Astellas"),                          # ~19
    ("https://jobs.lever.co/snappr", "lever", "Snappr"),                                     # ~19
    ("https://job-boards.greenhouse.io/mill", "greenhouse", "Mill"),                         # ~18
    ("https://bestwestern.wd1.myworkdayjobs.com/careers", "workday", "Best Western"),        # ~17
    ("https://job-boards.greenhouse.io/firstnationalbankofamerica", "greenhouse", "First National Bank of America"), # ~17
    ("https://job-boards.greenhouse.io/groupon", "greenhouse", "Groupon"),                   # ~17
    ("https://job-boards.greenhouse.io/legion", "greenhouse", "Legion"),                     # ~17
    ("https://jobs.xfab.com", "successfactors", "X-FAB"),                                    # ~17
    ("https://jobs.ashbyhq.com/brightwheel", "ashby", "brightwheel"),                        # ~16
    ("https://careers.americanintegrityinsurance.com", "jibe", "American Integrity Insurance Company"), # ~15
    ("https://job-boards.greenhouse.io/auctane", "greenhouse", "Auctane"),                   # ~15
    ("https://job-boards.greenhouse.io/landdesign", "greenhouse", "LandDesign, Inc"),        # ~15
    ("https://job-boards.greenhouse.io/make", "greenhouse", "MAKE"),                         # ~15
    ("https://job-boards.greenhouse.io/mochihealth", "greenhouse", "Mochi Health"),          # ~15
    ("https://job-boards.greenhouse.io/collegetrack", "greenhouse", "College Track"),        # ~14
    ("https://job-boards.greenhouse.io/falconx", "greenhouse", "FalconX"),                   # ~14
    ("https://job-boards.greenhouse.io/starburst", "greenhouse", "Starburst"),               # ~14
    ("https://jobs.ashbyhq.com/artafinance", "ashby", "Arta Finance"),                       # ~13
    ("https://jobs.ashbyhq.com/demandbase", "ashby", "Demandbase"),                          # ~13
    ("https://job-boards.greenhouse.io/hearcom", "greenhouse", "hear.com"),                  # ~13
    ("https://job-boards.greenhouse.io/ownwell", "greenhouse", "Ownwell, Inc."),             # ~13
    ("https://jobs.lever.co/sysdig", "lever", "Sysdig"),                                     # ~13
    ("https://jobs.lever.co/valiantys", "lever", "VALIANTYS"),                               # ~13
    ("https://job-boards.greenhouse.io/goldenstate", "greenhouse", "Golden State"),          # ~12
    ("https://job-boards.greenhouse.io/paystand", "greenhouse", "PayStand"),                 # ~12
    ("https://job-boards.greenhouse.io/madisonenergyinfrastructure", "greenhouse", "Madison Energy Infrastructure"), # ~11
    ("https://job-boards.greenhouse.io/vay", "greenhouse", "Vay"),                           # ~11
    ("https://jobs.smartrecruiters.com/CarilionClinic", "smartrecruiters", "Carilion Clinic"), # ~10
    ("https://jobs.smartrecruiters.com/FamiliaDental", "smartrecruiters", "Familia Dental"), # ~10
    ("https://jobs.ashbyhq.com/glimpse", "ashby", "Glimpse"),                                # ~10
    ("https://jobs.smartrecruiters.com/KatalystHealthcaresLifeSciences", "smartrecruiters", "Katalyst Healthcares & Life Sciences"), # ~10
    ("https://careers.keyence.com", "successfactors", "Keyence"),                            # ~10
    ("https://job-boards.greenhouse.io/pdtpartners", "greenhouse", "PDT Partners"),          # ~10
    ("https://job-boards.greenhouse.io/supernal", "greenhouse", "Supernal"),                 # ~10
    ("https://jobs.ashbyhq.com/zello", "ashby", "Zello"),                                    # ~10
    ("https://job-boards.greenhouse.io/elsevier", "greenhouse", "Elsevier"),                 # ~9
    ("https://job-boards.greenhouse.io/flash", "greenhouse", "Flash"),                       # ~9
    ("https://job-boards.greenhouse.io/skyryse", "greenhouse", "SkyRyse"),                   # ~9
    ("https://jobs.ashbyhq.com/tensec", "ashby", "Tensec"),                                  # ~9
    ("https://jobs.smartrecruiters.com/MastechDigital", "smartrecruiters", "Mastech Digital"), # ~8
    ("https://job-boards.greenhouse.io/ooma", "greenhouse", "Ooma, Inc."),                   # ~8
    ("https://job-boards.greenhouse.io/primemedicine", "greenhouse", "Prime Medicine, Inc."), # ~8
    ("https://ballinger.bamboohr.com", "bamboohr", "Ballinger"),                             # ~7
    ("https://jobs.smartrecruiters.com/HealthPartners", "smartrecruiters", "HealthPartners"), # ~7
    ("https://jobs.smartrecruiters.com/SmartITFrameLLC", "smartrecruiters", "Smart IT Frame LLC"), # ~7
    ("https://jobs.smartrecruiters.com/YamahaMotor", "smartrecruiters", "Yamaha Motor"),     # ~7
    ("https://jobs.smartrecruiters.com/iPivot", "smartrecruiters", "iPivot"),                # ~6
    ("https://jobs.smartrecruiters.com/Lonza", "smartrecruiters", "Lonza"),                  # ~6
    ("https://jobs.smartrecruiters.com/PhoenixCharterAcademyNetwork", "smartrecruiters", "Phoenix Charter Academy Network"), # ~6
    ("https://jobs.smartrecruiters.com/TexasWaterDevelopmentBoard", "smartrecruiters", "Texas Water Development Board"), # ~6
    ("https://job-boards.greenhouse.io/valerahealth", "greenhouse", "Valera Health"),        # ~6
    ("https://job-boards.greenhouse.io/ernestpackagingsolutions", "greenhouse", "Ernest"),   # ~38
    ("https://jobs.smartrecruiters.com/MGMResortsInternational", "smartrecruiters", "MGM Resorts International"), # ~5
    ("https://jobs.ashbyhq.com/tapblaze", "ashby", "TapBlaze"),                              # ~5
    ("https://job-boards.greenhouse.io/akoya", "greenhouse", "Akoya"),                       # ~4
    ("https://jobs.lever.co/gatorbio", "lever", "Gator Bio"),                                # ~4
    ("https://jobs.smartrecruiters.com/iHeartMedia", "smartrecruiters", "iHeartMedia"),      # ~4
    ("https://jobs.lever.co/influ2", "lever", "Influ2"),                                     # ~3
    ("https://jobs.smartrecruiters.com/ServiceLink", "smartrecruiters", "ServiceLink"),      # ~3
    ("https://jobs.smartrecruiters.com/BONITABAYCLUB", "smartrecruiters", "BONITA BAY CLUB"), # ~2
    ("https://jobs.smartrecruiters.com/FirstServiceResidential", "smartrecruiters", "FirstService Residential"), # ~2
    ("https://jobs.smartrecruiters.com/JayesTechLLC", "smartrecruiters", "Jayes Tech LLC"),  # ~2
    ("https://jobs.smartrecruiters.com/KarsunSolutionsLLC", "smartrecruiters", "Karsun Solutions LLC"), # ~2
    ("https://jobs.lever.co/nextech", "lever", "Nextech"),                                   # ~2
    ("https://jobs.smartrecruiters.com/ReveilleTechnologiesInc", "smartrecruiters", "Reveille Technologies,Inc"), # ~2
    ("https://jobs.smartrecruiters.com/Resmed", "smartrecruiters", "Resmed"),                # ~2
    ("https://jobs.smartrecruiters.com/Solovis", "smartrecruiters", "Solovis"),              # ~2
    ("https://jobs.smartrecruiters.com/ArizonaPublicServiceAPS", "smartrecruiters", "Arizona Public Service (APS)"), # ~1
    ("https://jobs.smartrecruiters.com/Arthrex", "smartrecruiters", "Arthrex"),              # ~1
    ("https://jobs.smartrecruiters.com/Avangrid", "smartrecruiters", "Avangrid"),            # ~1
    ("https://jobs.smartrecruiters.com/Canidium", "smartrecruiters", "Canidium"),            # ~1
    ("https://jobs.smartrecruiters.com/COGENTDATASOLUTIONSLLC", "smartrecruiters", "COGENT DATA SOLUTIONS LLC"), # ~1
    ("https://jobs.smartrecruiters.com/CrunchFitness", "smartrecruiters", "Crunch Fitness"), # ~1
    ("https://jobs.smartrecruiters.com/GEICO", "smartrecruiters", "GEICO"),                  # ~1
    ("https://itron.wd5.myworkdayjobs.com/Early_Careers", "workday", "Itron"),               # ~1
    ("https://jobs.smartrecruiters.com/KPIPartners", "smartrecruiters", "KPI Partners"),     # ~1
    ("https://jobs.smartrecruiters.com/MooreVanAllen", "smartrecruiters", "Moore & Van Allen"), # ~1
    ("https://jobs.ashbyhq.com/optimum", "ashby", "Optimum"),                                # ~1
    ("https://job-boards.greenhouse.io/paradigm", "greenhouse", "Paradigm"),                 # ~1
    ("https://job-boards.greenhouse.io/phantomai", "greenhouse", "Phantom AI"),              # ~1
    ("https://jobs.smartrecruiters.com/Qurrent", "smartrecruiters", "Qurrent"),              # ~1
    ("https://jobs.smartrecruiters.com/SaintAlphonsusHealthSystem", "smartrecruiters", "Saint Alphonsus Health System"), # ~1
    ("https://jobs.smartrecruiters.com/UCIrvineHealth", "smartrecruiters", "UC Irvine Health"), # ~1
    # --- Added 2026-08-22 (careers_us.md audit, second tranche; see the note in WORKDAY_BOARDS).
    # The three small boards from that audit. Both slug-guessed ones were confirmed against the
    # ATS's own company name before being labelled here -- greenhouse/fns is "FNS, Inc.
    # Affiliates" and smartrecruiters/IrisSoftware is "IRIS Software", so neither is the slug
    # collision a low-confidence hit usually turns out to be.
    #
    # IRIS Software is a US IT staffing firm and its board is all recruiter roles -- the profile
    # the body-shop guard exists to exclude (it does not match _BODYSHOP_RE, which is why the
    # probe reached it). Here on the same basis as eTeam in JOBDIVA_BOARDS: the user's explicit
    # ask, recorded so it is not mistaken for something the guard approved. ---
    ("https://jobs.ashbyhq.com/sift", "ashby", "Sift Science"),   # ~8 -> 3 on-target US
    ("https://job-boards.greenhouse.io/fns", "greenhouse", "FNS"),   # ~6 -> 0 on-target US (freight/logistics, KR+CA)
    ("https://jobs.smartrecruiters.com/IrisSoftware", "smartrecruiters", "IRIS Software"), # ~7 -> 0 on-target US
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
    # Accenture (2026-06-11): one of the largest H1B sponsors, period. Found behind
    # accenture.com/careers via the detect chain (~2k+ postings).
    ("https://accenture.wd103.myworkdayjobs.com/AccentureCareers",   "workday", "Accenture"),
    # CVS Health (2026-06-12): jobs.cvshealth.com is a Phenom front-end whose apply
    # links go to Workday — detect_phenom resolved it (~16k postings, incl. Aetna).
    ("https://cvshealth.wd1.myworkdayjobs.com/CVS_Health_Careers",   "workday", "CVS Health"),
    # --- Added 2026-06-13: major sponsors found by detect_linked_ats over their public
    # careers pages (each followed a real "view jobs" link to its Workday board). ---
    ("https://adobe.wd5.myworkdayjobs.com/external_experienced",        "workday", "Adobe"),
    ("https://comcast.wd5.myworkdayjobs.com/Comcast_Careers",           "workday", "Comcast"),
    ("https://expedia.wd108.myworkdayjobs.com/search",                  "workday", "Expedia Group"),
    ("https://citi.wd5.myworkdayjobs.com/2",                            "workday", "Citigroup"),
    ("https://ghr.wd1.myworkdayjobs.com/lateral-us",                    "workday", "Bank of America"),
    ("https://amgen.wd1.myworkdayjobs.com/Careers",                     "workday", "Amgen"),
    ("https://rsm.wd1.myworkdayjobs.com/RSMCareers",                    "workday", "RSM US"),
    ("https://asurion.wd5.myworkdayjobs.com/AsurionCareers_US",         "workday", "Asurion"),
    ("https://lendingclub.wd1.myworkdayjobs.com/External",              "workday", "LendingClub"),
    ("https://directv.wd1.myworkdayjobs.com/Careers",                   "workday", "DirecTV"),
    ("https://santander.wd3.myworkdayjobs.com/SantanderCareers",        "workday", "Santander"),
    ("https://sabre.wd1.myworkdayjobs.com/SabreJobs",                   "workday", "Sabre"),
    ("https://cigna.wd5.myworkdayjobs.com/cignacareers",                "workday", "Cigna"),
    ("https://humana.wd5.myworkdayjobs.com/CenterWell_External_Career_Site", "workday", "Humana"),
    ("https://elevancehealth.wd1.myworkdayjobs.com/ANT",               "workday", "Elevance Health"),
    ("https://tmobile.wd1.myworkdayjobs.com/External",                  "workday", "T-Mobile"),
    ("https://iqvia.wd1.myworkdayjobs.com/IQVIA",                       "workday", "IQVIA"),
    ("https://westernunion.wd5.myworkdayjobs.com/WesternUnionJobs",     "workday", "Western Union"),
    ("https://nike.wd1.myworkdayjobs.com/nke",                          "workday", "Nike"),
    # --- Added 2026-06-13 (E-Verify major-employer sweep): found via detect_linked_ats on
    # the company's careers page, or a web-searched Workday tenant verified via probe_board
    # (count shown). Big legit employers — no body-shops. ---
    ("https://globalhr.wd5.myworkdayjobs.com/REC_RTX_Ext_Gateway",      "workday", "RTX"),                  # ~4215
    ("https://geaerospace.wd5.myworkdayjobs.com/GE_ExternalSite",       "workday", "GE Aerospace"),         # ~569
    ("https://boeing.wd1.myworkdayjobs.com/EXTERNAL_CAREERS",           "workday", "Boeing"),               # ~1156
    ("https://stryker.wd1.myworkdayjobs.com/StrykerCareers",            "workday", "Stryker"),              # ~1207
    ("https://abbott.wd5.myworkdayjobs.com/abbottcareers",              "workday", "Abbott"),               # ~2000
    ("https://zoetis.wd5.myworkdayjobs.com/zoetis",                     "workday", "Zoetis"),               # ~111
    ("https://micron.wd1.myworkdayjobs.com/External",                   "workday", "Micron"),               # ~3061
    ("https://kla.wd1.myworkdayjobs.com/Search",                        "workday", "KLA"),                  # ~798
    ("https://usbank.wd1.myworkdayjobs.com/US_Bank_Careers",            "workday", "U.S. Bank"),            # ~1243
    ("https://truist.wd1.myworkdayjobs.com/Careers",                    "workday", "Truist"),               # ~1082
    ("https://td.wd3.myworkdayjobs.com/TD_Bank_Careers",                "workday", "TD Bank"),              # ~1524
    ("https://target.wd5.myworkdayjobs.com/targetcareers",              "workday", "Target"),               # ~2000
    ("https://paloaltonetworks.wd5.myworkdayjobs.com/panwexternalcareers", "workday", "Palo Alto Networks"), # ~1447
    ("https://jj.wd5.myworkdayjobs.com/JJ",                             "workday", "Johnson & Johnson"),    # ~1971
    ("https://pfizer.wd1.myworkdayjobs.com/PfizerCareers",              "workday", "Pfizer"),               # ~683
    ("https://medtronic.wd1.myworkdayjobs.com/medtroniccareers",        "workday", "Medtronic"),            # ~1077
    ("https://ngc.wd1.myworkdayjobs.com/Northrop_Grumman_External_Site", "workday", "Northrop Grumman"),    # ~2884
    ("https://intel.wd1.myworkdayjobs.com/External",                    "workday", "Intel"),                # ~670
    ("https://bristolmyerssquibb.wd5.myworkdayjobs.com/BMS",            "workday", "Bristol Myers Squibb"), # ~823
    ("https://amat.wd1.myworkdayjobs.com/External",                     "workday", "Applied Materials"),    # ~1920
    ("https://capitalone.wd12.myworkdayjobs.com/Capital_One",           "workday", "Capital One"),          # ~1568
    ("https://cat.wd5.myworkdayjobs.com/CaterpillarCareers",            "workday", "Caterpillar"),          # ~976
    ("https://gilead.wd1.myworkdayjobs.com/gileadcareers",              "workday", "Gilead Sciences"),      # ~309
    # --- Added 2026-06-14 (E-Verify sweep, round 2 — web-searched tenants verified via
    # probe_board; Thermo Fisher resolved Phenom->Workday like CVS/Cisco). ---
    ("https://ms.wd5.myworkdayjobs.com/External",                       "workday", "Morgan Stanley"),       # ~1368
    ("https://wf.wd1.myworkdayjobs.com/WellsFargoJobs",                 "workday", "Wells Fargo"),          # ~1836
    ("https://fmr.wd1.myworkdayjobs.com/FidelityCareers",               "workday", "Fidelity Investments"), # ~597
    ("https://disney.wd5.myworkdayjobs.com/disneycareer",               "workday", "Disney"),               # ~636
    ("https://blackrock.wd1.myworkdayjobs.com/BlackRock_Professional",  "workday", "BlackRock"),            # ~392
    ("https://broadcom.wd1.myworkdayjobs.com/External_Career",          "workday", "Broadcom"),             # ~326
    ("https://autodesk.wd1.myworkdayjobs.com/Ext",                      "workday", "Autodesk"),             # ~663
    ("https://paypal.wd1.myworkdayjobs.com/jobs",                       "workday", "PayPal"),               # ~213
    ("https://thermofisher.wd5.myworkdayjobs.com/ThermoFisherCareers",  "workday", "Thermo Fisher Scientific"), # ~2967
    # --- Added 2026-06-16 (user request): confirmed via probe_board. Walmart is on the
    # wd504 data center (wd5 returns nothing); CDW ~200, Walmart ~2000+. ---
    ("https://cdw.wd5.myworkdayjobs.com/Careers",                       "workday", "CDW"),                  # ~200
    ("https://walmart.wd504.myworkdayjobs.com/WalmartExternal",         "workday", "Walmart"),              # ~2000+
    # --- Added 2026-06-17 (E-Verify+ list): Magna, found via www.magna.com/careers -> Workday. ---
    ("https://wd3.myworkdaysite.com/recruiting/magna/Magna",            "workday", "Magna"),                # ~1252
    # --- Added 2026-06-17 (H1B data-hub majors not yet tracked): Marvell + GM, both Workday
    # (their careers SPAs hid the link; tenants found via web search, confirmed via probe_board). ---
    ("https://marvell.wd1.myworkdayjobs.com/MarvellCareers",            "workday", "Marvell"),              # ~719
    ("https://generalmotors.wd5.myworkdayjobs.com/Careers_GM",          "workday", "General Motors"),       # ~880
    # --- Added 2026-06-18 (DOL LCA FY2026-Q2 sponsors via find_everify_boards careers-chain;
    # tenants resolved from each company's own careers page + validated via probe_board). ---
    ("https://nordstrom.wd501.myworkdayjobs.com/nordstrom_careers",     "workday", "Nordstrom"),            # ~1,102
    ("https://ingrammicro.wd5.myworkdayjobs.com/IngramMicro",           "workday", "Ingram Micro"),         # ~552
    ("https://redhat.wd5.myworkdayjobs.com/jobs",                       "workday", "Red Hat"),              # ~291
    ("https://biibhr.wd3.myworkdayjobs.com/external",                   "workday", "Biogen"),               # ~231
    ("https://wd5.myworkdaysite.com/recruiting/chewy/External",         "workday", "Chewy"),                # ~224
    # --- Added 2026-06-18 (LCA FY2026-Q2 deeper probe: tenants found via each company's
    # www careers page + detect chain, validated via probe_board + sampled job content).
    # MSK + Mass General Brigham are cap-exempt (no H1B lottery). ---
    ("https://lilly.wd115.myworkdayjobs.com/LLY",                       "workday", "Eli Lilly"),            # ~776
    ("https://motorolasolutions.wd5.myworkdayjobs.com/Careers",         "workday", "Motorola Solutions"),   # ~924
    ("https://fiserv.wd5.myworkdayjobs.com/EXT",                        "workday", "Fiserv"),               # ~438
    ("https://ffive.wd5.myworkdayjobs.com/f5jobs",                      "workday", "F5"),                   # ~297
    ("https://usaa.wd1.myworkdayjobs.com/USAAJOBSWD",                   "workday", "USAA"),                 # ~183
    ("https://modernatx.wd1.myworkdayjobs.com/M_tx",                    "workday", "Moderna"),              # ~157
    ("https://massgeneralbrigham.wd1.myworkdayjobs.com/MGBExternal",    "workday", "Mass General Brigham"), # ~2000 (cap-exempt)
    ("https://msk.wd108.myworkdayjobs.com/MSKCC_Careers_Primary",       "workday", "Memorial Sloan Kettering"), # ~98 (cap-exempt)
    # --- Added 2026-06-18 (LCA FY2026-Q2 web-search tenant lookup; verified via probe_board /
    # sampled content). Cleveland Clinic / Yale / Cornell are cap-exempt (no H1B lottery). ---
    ("https://ccf.wd1.myworkdayjobs.com/ClevelandClinicCareers",        "workday", "Cleveland Clinic"),    # ~1,984 (cap-exempt)
    ("https://yale.wd1.myworkdayjobs.com/external_career_site",         "workday", "Yale University"),      # ~292 (cap-exempt)
    ("https://cornell.wd1.myworkdayjobs.com/CornellCareerPage",         "workday", "Cornell University"),   # ~143 (cap-exempt)
    ("https://nxp.wd3.myworkdayjobs.com/careers",                       "workday", "NXP Semiconductors"),   # ~664
    ("https://mckesson.wd3.myworkdayjobs.com/External_Careers",         "workday", "McKesson"),             # ~387
    ("https://vrtx.wd501.myworkdayjobs.com/Vertex_Careers",             "workday", "Vertex Pharmaceuticals"), # ~279
    ("https://illumina.wd1.myworkdayjobs.com/illumina-careers",         "workday", "Illumina"),             # ~140
    # --- Added 2026-06-18 (LCA FY2026-Q2 web-search wave 2; Carnegie Mellon + VUMC cap-exempt) ---
    ("https://takeda.wd3.myworkdayjobs.com/External",                   "workday", "Takeda"),               # ~1,623
    ("https://cardinalhealth.wd1.myworkdayjobs.com/EXT",                "workday", "Cardinal Health"),      # ~757
    ("https://cadence.wd1.myworkdayjobs.com/External_Careers",          "workday", "Cadence Design Systems"), # ~640
    ("https://regeneron.wd1.myworkdayjobs.com/Careers",                 "workday", "Regeneron"),            # ~585
    ("https://transunion.wd5.myworkdayjobs.com/TransUnion",             "workday", "TransUnion"),           # ~244
    ("https://vumc.wd1.myworkdayjobs.com/vumccareers",                  "workday", "Vanderbilt University Medical Center"), # ~683 (cap-exempt)
    ("https://cmu.wd5.myworkdayjobs.com/CMU",                           "workday", "Carnegie Mellon University"), # ~167 (cap-exempt)
    # --- Added 2026-06-18 (LCA FY2026-Q2 web-search wave 3; OSU/PSU/Georgetown cap-exempt) ---
    ("https://psu.wd1.myworkdayjobs.com/PSU_Staff",                     "workday", "Penn State University"), # ~1,412 (cap-exempt)
    ("https://osu.wd1.myworkdayjobs.com/OSUCareers",                    "workday", "Ohio State University"), # ~1,067 (cap-exempt)
    ("https://cox.wd1.myworkdayjobs.com/Cox_External_Career_Site_1",    "workday", "Cox Automotive"),       # ~722
    ("https://equifax.wd5.myworkdayjobs.com/External",                  "workday", "Equifax"),              # ~209
    ("https://georgetown.wd1.myworkdayjobs.com/Georgetown_Admin_Careers", "workday", "Georgetown University"), # ~111 (cap-exempt)
    # --- Added 2026-06-18: Snap — careers.snap.com is a SPA but its apply links go to Workday
    # (the public external site is 'snap'). Found while assessing Eightfold (Snap isn't Eightfold). ---
    ("https://snapchat.wd1.myworkdayjobs.com/snap",                     "workday", "Snap"),                 # ~138
    # --- Added 2026-08-12 (grad.jobs H-1B sponsor list -> find_everify_boards). The probe's
    # own careers-chain found only Oregon State; the rest came from a second pass that SUPPLIES
    # the domain instead of deriving it from the name. That rule reduces "X University" to X,
    # which is right for duke/brown/purdue and wrong for every institution whose domain is an
    # abbreviation — iastate.edu, ucsf.edu, ohsu.edu, chop.edu. All cap-exempt (university,
    # university-affiliated hospital, or government lab), so they skip the H1B lottery. ---
    ("https://ochsner.wd1.myworkdayjobs.com/Ochsner",                   "workday", "Ochsner Health"),       # ~1,925 (cap-exempt)
    ("https://chop.wd108.myworkdayjobs.com/CHOPExternalCareers",        "workday", "Children's Hospital of Philadelphia"), # ~286 (cap-exempt)
    ("https://oregonstate.wd501.myworkdayjobs.com/OSU_Careers_Site",    "workday", "Oregon State University"), # ~134 (cap-exempt)
    ("https://isu.wd1.myworkdayjobs.com/IowaStateJobs",                 "workday", "Iowa State University"), # ~73 (cap-exempt)
    # Not a typo and not a truncation: the site name really is "Externa". /External and
    # /ExternalCareers both probe 0 — checked before this went in.
    ("https://bnl.wd1.myworkdayjobs.com/Externa",                       "workday", "Brookhaven National Laboratory"), # ~59 (cap-exempt)
    ("https://hhmi.wd1.myworkdayjobs.com/External",                     "workday", "Howard Hughes Medical Institute"), # ~45 (cap-exempt)
    # --- Added 2026-08-21 (careers_us.md coverage audit). careers_us.md lists 289 sponsors and
    # only 130 were scraped; the 160 that were not went through find_everify_boards, then a
    # second pass that feeds the doc's OWN careers URL into the detect chain instead of guessing
    # careers.<slug>.com. 14 had a real board. These are the 6 that EARN the scrape time.
    #
    # Adopted on measured on-target US rows, not board size -- the two are barely related:
    #
    #     Booz Allen  2,000 postings -> 645 on-target US   32%   <- 72% of the entire haul
    #     Workday       364          ->  89                24%
    #     Milliman      118          ->  20                17%   (ULTIPRO_BOARDS)
    #     JLL         2,000          ->  77                 3.9%
    #     Blackstone    174          ->  13                 7.5%
    #     Alcon         401          ->  13                 3.2%
    #
    # DELIBERATELY NOT ADDED, though all six probe fine and would look like wins in a count of
    # boards: Macy's (oracle, 4,453 -> 16 US rows, 0.5%), Novant Health (jibe, 1,678 -> 9,
    # 0.5%), Stanford Health Care (343 -> 4), AIG (473 -> 3), Frontier Airlines (76 -> 3),
    # RingCentral (72 -> 5). Macy's is retail store staffing and Novant is nursing -- the same
    # flood pattern that got the E-Verify retail giants blocklisted. Bentley University probes
    # 24 postings and yields ZERO on-target, so it is not here either.
    #
    # Cost is bounded: 5 Workday boards x SCRAPE_BOARD_TIMEOUT["workday"] (300s) is a 1,500s
    # worst case, and the honest cost is far less -- these page at the clean median, not
    # Itron's pathological rate. ---
    ("https://bah.wd1.myworkdayjobs.com/BAH_Jobs",                   "workday", "Booz Allen Hamilton"),   # ~2,000 -> 645 on-target US
    ("https://workday.wd5.myworkdayjobs.com/Workday",                "workday", "Workday"),   # ~364 -> 89
    ("https://jll.wd1.myworkdayjobs.com/jllcareers",                 "workday", "Jones Lang LaSalle"),   # ~2,000 -> 77
    ("https://alcon.wd5.myworkdayjobs.com/careers_alcon",            "workday", "Alcon"),   # ~401 -> 13
    ("https://blackstone.wd1.myworkdayjobs.com/Blackstone_Careers",  "workday", "Blackstone"),   # ~174 -> 13
    # --- Added 2026-08-22 (careers_us.md audit, second tranche -- at the user's explicit
    # ask, after the first tranche took only the six boards that paid for themselves).
    # These are the rest of the 14 probeable boards from that audit. Each one is real and
    # each one is a poor trade, so the measured yield is recorded per entry rather than
    # argued here: on-target US rows over postings paged, measured 2026-08-21.
    #
    # Not blocked, so the rows do reach the funnel -- the live blocked_companies table holds
    # only Whataburger and Family Dollar. Macy's is the one to watch: Whataburger was
    # blocked for "title filter keeps 0 of 4,640 postings" and Macy's keeps 16 of 4,450,
    # which is the same shape and not yet the same verdict. ---
    ("https://aig.wd1.myworkdayjobs.com/aig",                                      "workday", "AIG"),   # ~473 -> 3 on-target US (0.6%)
    ("https://stanfordmedicine.wd115.myworkdayjobs.com/SHC_External_Career_Site",  "workday", "Stanford Health Care"),   # ~343 -> 4 (1.2%, cap-exempt)
    ("https://ringcentral.wd1.myworkdayjobs.com/RingCentral_Careers",              "workday", "RingCentral"),   # ~72 -> 5 (6.9%)
    # Bentley University is cap-exempt and runs TWO Workday sites. /staff is the one that pays:
    # 17 postings -> 2 on-target US (Business Systems Analyst, Senior IT Project Manager), a
    # better ratio than anything else in this tranche. /faculty is deliberately NOT here --
    # measured 0 on-target of 24, and a second entry for one employer would be the only
    # duplicate company name in SOURCES.
    ("https://bentley.wd503.myworkdayjobs.com/staff",                              "workday", "Bentley University"), # ~17 -> 2 (cap-exempt)
]

# SAP SuccessFactors "Career Site Builder" sites (jobs.<co>.com / careers.<co>.com with
# /search/?q= + /job/<slug>/<id>/ URLs). Server-rendered HTML — scrape_successfactors
# parses the results table; detect_successfactors() network-probes pasted links.
SF_BOARDS = [
    # SAP America: top-30 H1B sponsor; global board, the US filter keeps US roles.
    ("https://jobs.sap.com",              "successfactors", "SAP"),
    # NTT DATA North America: major H1B sponsor (careers-inc = the US entity site).
    ("https://careers-inc.nttdata.com",   "successfactors", "NTT DATA"),
    # --- Added 2026-06-13: sponsors found on SuccessFactors CSB (probe of sponsors.txt) ---
    ("https://jobs.deere.com",            "successfactors", "John Deere"),
    ("https://jobs.bunge.com",            "successfactors", "Bunge"),
    ("https://jobs.mcdonalds.com",        "successfactors", "McDonald's"),
    ("https://jobs.tenneco.com",          "successfactors", "Tenneco"),
    ("https://careers.westpharma.com",    "successfactors", "West Pharmaceutical"),
    ("https://careers.qorvo.com",         "successfactors", "Qorvo"),
    # --- Added 2026-06-13 (E-Verify major-employer sweep) ---
    ("https://jobs.bostonscientific.com",  "successfactors", "Boston Scientific"),   # ~638
    ("https://jobs.paccar.com",            "successfactors", "Paccar"),              # ~86
    ("https://jobs.netapp.com",            "successfactors", "NetApp"),              # ~271
    # --- Added 2026-06-17 (user request): ENGIE. www.engie-na.com/careers points here;
    # this jobs2web/RMK SF site renders results client-side, so scrape_successfactors
    # falls back to the sitemap path and keeps only US (North America) postings. ---
    ("https://jobs.engie.com",             "successfactors", "Engie"),              # ~82 US
    # --- Added 2026-08-12 (grad.jobs H-1B sponsor list). ORNL is a DOE lab run by UT-Battelle;
    # jobs.ornl.gov is its own board, separate from the Battelle Memorial Institute board
    # already in the boards table (jobs.battelle.org) — different postings, not a duplicate. ---
    ("https://jobs.ornl.gov",              "successfactors", "Oak Ridge National Laboratory"), # ~111 (cap-exempt)
    # --- Added 2026-08-16, replacing Adzuna company boards. These two are the employers that
    # turned out to have a scrapeable board of their own once someone looked: the detect chain in
    # find_everify_boards found both on SuccessFactors CSB, and scripts/probe_adzuna_
    # replacements.py has the full result for the other 64 (Google, Tesla, IBM, KPMG, Deloitte,
    # Cognizant, CBRE, Zoom, Nutanix, Goldman Sachs, Verizon: no public board, still absent).
    #
    # Rows each ACTUALLY contributes, measured 2026-08-16 through the same US + title filters
    # main() applies, against what Adzuna was contributing for the same employer:
    #   Capgemini  161 rows  vs  19 via Adzuna     (from 602 scraped)
    #   EY          18 rows  vs  46 via Adzuna     (from 148 scraped)
    # EY is a DECREASE in count and was kept anyway, because 18 rows with a real description and
    # an apply form beat 46 that link to an aggregator redirect and can never hold a JD. Count is
    # the wrong axis; that was Adzuna's whole problem.
    #
    # Capgemini's board is also what exposed the two-letter-country-code hole in _csb_is_us —
    # 30% of its "US" rows were Morocco, Canada and Argentina. Read that fix before assuming a
    # new CSB tenant's location strings mean what they look like.
    #
    # Two were probed and REJECTED, and both rejections are the same lesson:
    #   PwC        careers.pwc.com answers probe_board with 30 and scrape_successfactors with 0.
    #   Birlasoft  622 postings scraped, 9 US, 0 past the title filter — an India board.
    # A board that costs a full paged walk and yields nothing is worse than no board, because it
    # reads as coverage in this list. Re-probe before adding either back.
    ("https://careers.capgemini.com",      "successfactors", "Capgemini"),          # ~211 rows
    ("https://careers.ey.com",             "successfactors", "EY"),                 # ~19 rows
]

# Phenom People career sites that are Phenom-NATIVE (apply links don't go to Workday —
# those get added as Workday boards instead; see detect_phenom).
PHENOM_BOARDS = [
    # Actalent: huge engineering/sciences staffing firm, heavy H1B sponsor (~5k postings;
    # scrape_phenom caps at 3000 — the title filter keeps only on-target PM/analyst roles).
    ("https://careers.actalentservices.com", "phenom", "Actalent"),
    # BCG: top-tier consulting sponsor; careers.bcg.com is Phenom-native (~900 postings).
    ("https://careers.bcg.com", "phenom", "Boston Consulting Group"),
    # Merck: top pharma H1B sponsor; jobs.merck.com is Phenom-native (~280 US postings).
    ("https://jobs.merck.com", "phenom", "Merck"),
    # --- Added 2026-06-17 (H1B data-hub majors): eBay. jobs.ebayinc.com is Phenom (its apply
    # links point at Workday, but the ebay.wd5 site name isn't exposed, so scrape the Phenom
    # front-end directly — ~591 US postings with titles/locations/dates). ---
    ("https://jobs.ebayinc.com", "phenom", "eBay"),
]

# Avature career portals (<tenant>.avature.net/<portal>/SearchJobs). Server-rendered HTML,
# walked 10/page with an empty search; no public API and no posting dates, so found_date
# falls back to the scrape stamp and the title/US filter trims the board. These tenants are
# often dominated by one job family — the corporate PM/ops/product roles are the keepers.
AVATURE_BOARDS = [
    # --- Added 2026-06-18 (user request): NVA (National Veterinary Associates). ~2,300
    # postings, overwhelmingly clinical (DVM / vet tech / veterinarian) which the title
    # filter drops; the keepers are its corporate project / operations / product roles. ---
    ("https://nva.avature.net/jobs/SearchJobs", "avature", "National Veterinary Associates"),
    # --- Added 2026-06-18 (LCA FY2026-Q2 sponsors on Avature's 'article--result' template,
    # unlocked by generalizing scrape_avature). Bloomberg shows location (US-filtered cleanly);
    # Synopsys omits location on its listing (blank -> kept, so a few non-US roles slip the US
    # filter — acceptable: the title filter still trims, and most Synopsys roles are engineer). ---
    ("https://bloomberg.avature.net/careers/SearchJobs", "avature", "Bloomberg"),    # ~438
    ("https://synopsys.avature.net/careers/SearchJobs", "avature", "Synopsys"),      # ~714
    # --- Added 2026-08-16 (user request). careers.lululemon.com is a vanity host in front of
    # lululemoninc.avature.net; either URL normalizes to the same portal, and the cards link to
    # the careers.lululemon.com form either way, so this uses the host a person would paste.
    # ~1,130 postings, ~92% of them retail floor roles (Educator / Community Specialist) that
    # the title filter drops — the keepers are corporate PM/product/ops out of Vancouver and
    # Seattle. It is also the board that exposed the subtitle-location gap in
    # _avature_location; before that fix its Zurich and Mexico stores read as US. ---
    ("https://careers.lululemon.com/en_US/careers/SearchJobs", "avature", "lululemon"),  # ~1130
    # --- Added 2026-08-21 (user request): Epic Systems (Verona WI — the EHR vendor, and a
    # top-volume H-1B petitioner). Pasted as a /Careers/Register?folderId=… apply link;
    # _avature_base normalizes it. Epic posts EVERGREEN JOB FAMILIES, not requisitions: 49
    # folders, one per role, reposted never — so found_date is the scrape stamp and the row
    # count stays flat instead of churning. 48 are US (the one UK folder is dropped) and 7
    # clear the title filter as measured on 2026-08-21: Software Developer (+ its intern),
    # Project Manager, Infrastructure Engineer, Enterprise Network Engineer, DevOps/SRE,
    # Compensation and Stock Program Manager. The rest is the Verona campus kitchen and
    # facilities. Note the near-misses are real roles the gate declines by design, NOT a
    # coverage bug: Technical Solutions Engineer, Integration Solutions Engineer and the
    # three Systems Administrators want a phrase the keyword list does not carry, and
    # core.admits_on_description cannot rescue them because Avature ships no listing JD
    # (the card's one-line teaser is far too thin to offer as one — see the THIN-jd class).
    # This is the board that forced the third card template and _avature_offset_param. ---
    ("https://epic.avature.net/Careers/SearchJobs", "avature", "Epic Systems"),  # ~49
]

# UKG Pro Recruiting (UltiPro) boards — recruiting[N].ultipro.com/{CO}/JobBoard/{guid}.
# Public LoadSearchResults JSON POST; the list carries a BriefDescription, full JD via the
# OpportunityDetail page (score_jobs.ultipro_detail_jd handles it).
ULTIPRO_BOARDS = [
    # --- Added 2026-06-18 (user request): Starkey (Starkey Hearing, Eden Prairie MN — the
    # "Start Hearing" brand). On the recruiting2 host; ~82 postings, the title filter keeps
    # its corporate project/ops/analyst roles (e.g. Project Manager II - Engineering). ---
    ("https://recruiting2.ultipro.com/STA1003STARK/JobBoard/aa9d7813-93e5-4731-9f45-8ccb56bea5fd",
     "ultipro", "Starkey"),
    # --- Added 2026-08-21 (careers_us.md coverage audit; see the note in WORKDAY_BOARDS for
    # how the six were chosen). Milliman, the actuarial/consulting firm -- a DOL H-1B sponsor
    # that was in careers_us.md and had no board. The only one of the 160 that the ordinary
    # careers-chain probe found on its own; the other five needed the doc's careers URL. ---
    ("https://recruiting2.ultipro.com/MIL1017/JobBoard/f54234e9-dfde-b183-fd20-4fbdb19cba7a",
     "ultipro", "Milliman"),   # ~118 -> 20 on-target US
    # --- Added 2026-08-22 (careers_us.md audit, second tranche; see the note in
    # WORKDAY_BOARDS). Frontier Airlines -- airline ops, so almost nothing clears the filter. ---
    ("https://recruiting2.ultipro.com/FRO1003FTAIR/JobBoard/1efcf859-1b48-4a31-b014-ef62bdcab988",
     "ultipro", "Frontier Airlines"),   # ~76 -> 3 on-target US
]

# JobDiva candidate portals — www1.jobdiva.com/portal/?a=<token>. Public ws.jobdiva.com REST
# API (auth/a -> session token -> job/listall + job/getmore); the list carries the FULL JD
# inline (score_jobs bulk path). board_url is the portal link incl. its ?a=<token>.
JOBDIVA_BOARDS = [
    # --- Added 2026-06-18 (user request): eTeam Inc., an IT/healthcare STAFFING AGENCY
    # (the kind normally excluded as a body-shop — kept here at the user's explicit ask).
    # ~2,088 postings -> ~203 on-target (PM/BA/Data Analyst/Scrum Master); ~72% list the
    # client as "Confidential" (those show company "eTeam"), the rest name the end-client. ---
    ("https://www1.jobdiva.com/portal/?a=svjdnwzkulao5hqo7t0ifgvj8s71sf01d7dtgdstyhdixakxt6ty85zljsdyhgz2",
     "jobdiva", "eTeam"),
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
    # --- Added 2026-06-13 via detect_linked_ats on each company's careers page
    # (identity verified by sampling JDs: e.g. edel/CX_2001 serves Fortinet, which
    # acquired Lacework). JPMorgan + Safeway are top-volume H1B sponsors. ---
    ("https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001",
     "oracle", "JPMorgan Chase"),                              # ~7,000 postings
    ("https://ebcs.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1",
     "oracle", "Arcadis"),
    ("https://edel.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_2001",
     "oracle", "Fortinet"),
    ("https://fa-espx-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1",
     "oracle", "Cummins"),
    ("https://eofd.fa.us6.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001",
     "oracle", "Safeway"),
    # --- Added 2026-06-13 (E-Verify major-employer sweep): Texas Instruments on Oracle (~467). ---
    ("https://edbz.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX",
     "oracle", "Texas Instruments"),
    # --- Added 2026-06-14 (round 2): Dell's Oracle site has jobs (its Workday board was empty). ---
    ("https://iawmqy.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/careers",
     "oracle", "Dell Technologies"),
    # --- Added 2026-06-17 (E-Verify+ list): Warby Parker, found via www.warbyparker.com/careers (~870). ---
    ("https://fa-evdi-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/WarbyParkerCareers",
     "oracle", "Warby Parker"),
    # --- Added 2026-06-17 (user request): American Express. careers.americanexpress.com is a custom
    # domain fronting Oracle ORC; the real origin (egug.fa.us2) is in the page HTML. ~350 postings,
    # top-volume H1B sponsor. (The custom domain does NOT proxy /hcmRestApi, so we point at the origin.) ---
    ("https://egug.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1",
     "oracle", "American Express"),
    # --- Added 2026-06-18 (DOL LCA FY2026-Q2 sponsors via find_everify_boards careers-chain;
    # validated via probe_board). Big hospitality/retail boards — US filter trims the global set. ---
    ("https://ejwl.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX",
     "oracle", "Marriott"),                                   # ~11,713 (global; US-filtered)
    ("https://efet.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1",
     "oracle", "Hilton"),                                     # ~2,856
    ("https://fa-exhh-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/StaplesInc",
     "oracle", "Staples"),                                    # ~943
    # --- Added 2026-06-18 (LCA FY2026-Q2 deeper probe; identity verified by sampling JDs).
    # Mayo + Northwell are cap-exempt health systems (no H1B lottery). ---
    ("https://efds.fa.em5.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1",
     "oracle", "Ford Motor"),                                 # ~848
    ("https://fa-euwp-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1",
     "oracle", "Mayo Clinic"),                                # ~1,317 (cap-exempt)
    ("https://eppr.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_2",
     "oracle", "Northwell Health"),                           # ~1,505 (cap-exempt)
    # --- Added 2026-08-12 (grad.jobs H-1B sponsor list). Mount Sinai's Oracle site turned up
    # in the same sweep and is deliberately NOT here: it is already scraped via Jibe below,
    # which carries inline JDs. ---
    ("https://iazuqy.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1",
     "oracle", "University of California, San Francisco"),    # ~860 (cap-exempt)
    # --- Added 2026-08-22 (careers_us.md audit, second tranche; see the note in
    # WORKDAY_BOARDS). Macy's: the worst trade of the fourteen. 4,450 postings paged for 16
    # on-target US rows (0.4%) because the board is overwhelmingly store staffing -- the same
    # flood shape as the retail E-Verify giants that ended up blocklisted. Kept on request. ---
    ("https://ebwh.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1",
     "oracle", "Macy's"),   # ~4,450 -> 16 on-target US (0.4%)
]

# iCIMS "Career Sites" (powered by Jibe) expose a public /api/jobs JSON feed at the
# career-site origin. board_url = the careers domain. Add any iCIMS/Jibe employer here
# (the "➕ Add board" view auto-detects these from a pasted careers.<company>.com link).
JIBE_BOARDS = [
    ("https://careers.hrblock.com", "jibe", "H&R Block"),
    # Mount Sinai (Icahn School of Medicine + hospital system): cap-exempt sponsor,
    # ~1.8k postings; apply links go to its Oracle site but the Jibe feed is cleaner.
    ("https://careers.mountsinai.org", "jibe", "Mount Sinai"),
    # --- Added 2026-06-13 via detect_jibe over careers pages (the /api/jobs feed
    # carries full inline JDs, so score_jobs gets descriptions for free). ---
    ("https://careers.viasat.com",         "jibe", "Viasat"),
    ("https://careers.docusign.com",       "jibe", "Docusign"),
    # Insight Global: one of the largest US IT-staffing H1B sponsors.
    ("https://careers.insightglobal.com",  "jibe", "Insight Global"),
    # --- Added 2026-06-13 (E-Verify major-employer sweep): iCIMS/Jibe feeds w/ inline JDs. ---
    ("https://careers.amd.com",          "jibe", "AMD"),            # ~1041
    ("https://careers.pepsico.com",      "jibe", "PepsiCo"),        # ~2943
    ("https://careers.generalmills.com", "jibe", "General Mills"),  # ~334
    # --- Added 2026-06-18 (DOL LCA FY2026-Q2 sponsors via find_everify_boards careers-chain) ---
    ("https://careers.keysight.com",     "jibe", "Keysight Technologies"),  # ~548
    ("https://fedexfreight.jibeapply.com", "jibe", "FedEx Freight"),        # ~697
    ("https://careers.rivian.com",       "jibe", "Rivian"),                 # ~562 (deeper probe)
    ("https://careers.emory.edu",        "jibe", "Emory University"),       # ~1,840 (cap-exempt; iCIMS/Jibe)
    # --- Added 2026-08-12 (grad.jobs H-1B sponsor list). OHSU is a public health & science
    # university, so it is cap-exempt as an institution in its own right. ---
    ("https://jobs.ohsu.edu",            "jibe", "Oregon Health & Science University"), # ~533 (cap-exempt)
    # --- Added 2026-08-22 (careers_us.md audit, second tranche; see the note in
    # WORKDAY_BOARDS). Novant Health is cap-exempt, which is the only reason it is worth 1,677
    # postings: the board is overwhelmingly clinical, so 9 rows clear the title filter. ---
    ("https://jobs.novanthealth.org", "jibe", "Novant Health"),   # ~1,677 -> 9 on-target US (0.5%; cap-exempt)
]

# Adzuna (the aggregator API) was REMOVED on 2026-08-16, along with its 40 company boards and
# 27 role-phrase searches. It is recorded here rather than deleted silently because the reasons
# it was added — Tesla behind Akamai, Google's robots.txt disallowing its own results pages —
# are still true, and the next person to hit one of those will reach for the same answer.
#
# What it actually cost, measured on the live corpus the day it came out:
#   * 1,435 rows (6.4% of the feed) but 1,438 of the 3,816 rows with NO job description — 38%
#     of the entire JD backlog from 6% of the jobs. Its search API returns a truncated blurb and
#     its redirect pages block a server-side fetch, so an Adzuna row could never hold a real JD.
#   * Every row linked to adzuna.com/land/ad/..., which is a redirect, not an application form:
#     web._QUEUE_SKIP_HOSTS already refused to queue them for auto-apply, and _dupe_rank already
#     ranked them last. The feed was carrying rows the rest of the app declined to use.
#   * 564 of the 644 employers it contributed reached the feed ONLY through a phrase search, at
#     1-2 rows each, and a large share were staffing resellers rather than sponsors.
#
# The replacement is employer-side, which is this file's whole premise: EY, Capgemini and
# Birlasoft moved to their own SuccessFactors boards (see SF_BOARDS), and J.P. Morgan and
# Centene turned out to be duplicates of boards already in ORACLE_BOARDS / WORKDAY_BOARDS.
# The rest — Google, Tesla, IBM, KPMG, Deloitte, Cognizant, CBRE, Zoom, Nutanix, Goldman Sachs,
# Verizon — were probed against the full detect chain (scripts/probe_adzuna_replacements.py)
# and have no scrapeable public board. They are simply absent, which is honest.

# ---- JobSpy: the market-side sweep (LinkedIn / Indeed / Glassdoor / Google / ZipRecruiter) ----
# Every other source here is EMPLOYER-side: we only see a job if its company is already in
# SOURCES. JobSpy queries the big aggregators by role phrase, so it surfaces employers we have
# never heard of. That is the whole reason it earns its keep.
#
# The cost is that it hands back URLs in two namespaces, and only one of them can be merged with
# what we already hold:
#
#   * ORIGINAL-SOURCE urls merge for free. Google's job_url IS the employer's greenhouse/workday
#     link; Indeed's job_url_direct is the employer's own ATS link. canonical_url() plus main()'s
#     `seen` set already collapse these against the direct scrapers, at zero cost.
#   * AGGREGATOR-NATIVE urls cannot be merged by anything. indeed.com/viewjob?jk=... and
#     job-boards.greenhouse.io/acme/jobs/1 are different strings, so they are different primary
#     keys for one job. Glassdoor and ZipRecruiter NEVER supply a direct link, so every row from
#     them is one of these.
#
# _jobspy_best_url() is the first line of defence (prefer the direct link); main()'s posting
# fingerprint is the second. scripts/jobspy_shadow.py measures both before anything is enabled.
JOBSPY_CALLS = [0]        # selectors actually executed — printed each run, same as ADZUNA_CALLS
JOBSPY_ROWS = [0]         # raw rows returned, before any of main()'s filters
JOBSPY_JDS = {}           # canonical url -> description, harvested for free; see main()

# Empty by default: with JOBSPY_SITES unset there are no jobspy entries in SOURCES, the library
# is never imported, and the run is byte-for-byte what it is today. Enabling is one env var, and
# so is rolling back.
JOBSPY_SITES = [s for s in
                (os.environ.get("JOBSPY_SITES") or "").replace(",", " ").lower().split() if s]
JOBSPY_LOCATION = os.environ.get("JOBSPY_LOCATION") or "United States"
# 100, not the ~1000 the API allows. Measured on Adzuna 2026-08-09: two identical consecutive
# calls overlapped on only 6 of 50 URLs, i.e. the top of a date-sorted aggregator feed turns over
# almost completely between runs. Depth buys far less than breadth here, and every extra page is
# another request against the site most likely to start refusing them.
JOBSPY_RESULTS = int(os.environ.get("JOBSPY_RESULTS") or 100)
# 72h, not 24h: a skipped or failed run would otherwise lose that day's postings permanently.
# Same reasoning as ADZUNA_MAX_DAYS_SEARCH=7. Re-seeing a posting is free — the url key drops it.
JOBSPY_HOURS_OLD = int(os.environ.get("JOBSPY_HOURS_OLD") or 72)
# One extra HTTP request PER JOB, which is both the slowest thing the library does and the
# fastest way to earn a block. Off until the shadow report says what it would buy in direct-URL
# coverage on LinkedIn.
JOBSPY_LINKEDIN_JD = (os.environ.get("JOBSPY_LINKEDIN_JD") or "").lower() in ("1", "true", "yes")

# A SUBSET of the Adzuna phrases rather than a new list — those were tuned against this exact
# title filter, so reusing them keeps the two aggregators comparable. Start narrow: each phrase
# is a separate burst against one IP, and 5 sites x 5 phrases is already 25 selectors.
JOBSPY_PHRASES = [p.strip() for p in (
    os.environ.get("JOBSPY_PHRASES")
    or "project manager|program manager|business analyst|product manager|software engineer"
).split("|") if p.strip()]

# One site per entry, never site_name=["indeed","linkedin"] in a single call: JobSpy collects its
# per-site futures with an unwrapped future.result(), so one site raising takes down the whole
# call and discards the other sites' rows. Split this way, scrape_all's existing per-board
# try/except turns each site failure into one FAIL line and everything else still lands.
JOBSPY_BOARDS = [("jobspy:%s|%s|%s" % (site, phrase, JOBSPY_LOCATION), "jobspy", "JobSpy")
                 for site in JOBSPY_SITES for phrase in JOBSPY_PHRASES]

# Refuse a JobSpy row whose employer appears in NO federal file. Measured on the first live run
# (2026-08-09), against the corpus baseline in brackets:
#
#     no federal record      41%  [10%]
#     h1b (certified LCA)    55%  [80%]
#     E-Verify / STEM OPT     3%  [12%]
#     net H-1B viable        49%  [67%]
#
# Of 44 employers it introduced, 32 had no record: Amigo Construction, Diablo Roofing, Johnny on
# the Spot Environmental, Jones Mobile Home Service. That is structural rather than bad luck — an
# Indeed keyword search returns the long tail of small US employers, and that is exactly the
# population that never files an LCA or enrols in E-Verify. On a tool whose point is sponsorship,
# that dilutes the corpus without adding anything reachable.
#
# SCOPED TO JOBSPY ONLY. The ~1,220 direct boards are employers chosen deliberately, and several
# are cap-exempt universities and hospitals that this test would wrongly drop. Only rows from a
# keyword sweep — where nobody vetted the employer — have to earn their place.
#
# "No record" is NOT "will not sponsor": it means absent from the DOL LCA/PERM, USCIS Data Hub
# and E-Verify files, and a real sponsor can be missing from all three. This deliberately trades
# some genuine sponsors away to stop the long tail flooding the feed. Set to 0 to take everything.
JOBSPY_REQUIRE_VISA_RECORD = (
    (os.environ.get("JOBSPY_REQUIRE_VISA_RECORD") or "1").lower() not in ("0", "false", "no"))

# Meta (metacareers.com): the ONLY source with no public feed AND no aggregator stand-in
# we trust for it — Meta's careers site is a Facebook Relay/GraphQL app, so it's scraped
# by driving a headless browser (Playwright). Kept in its own list because, unlike every
# other source, this one needs `playwright install chromium` to be present to work.
METACAREERS_BOARDS = [
    ("https://www.metacareers.com/jobs/", "metacareers", "Meta"),
]

# PeopleSoft Candidate Gateway — the stock careers portal for universities and hospital
# systems, i.e. the CAP-EXEMPT employers (no H-1B lottery) that matter most here.
PEOPLESOFT_BOARDS = [
    ("https://jobs.omni.fsu.edu/psc/sprdhr_er/EMPLOYEE/HRMS/c/HRS_HRAM_FL.HRS_CG_SEARCH_FL.GBL",
     "peoplesoft", "Florida State University"),                                          # ~207
]

# Paylocity Recruiting — small and mid-size US employers. One board per company, keyed by the
# company GUID in its careers URL.
PAYLOCITY_BOARDS = [
    ("https://recruiting.paylocity.com/recruiting/jobs/All/"
     "155dc82e-5369-4654-bc29-7289091fe518/West-Cary-Group-LLC",
     "paylocity", "West Cary Group"),                                                      # ~3
]

# Michael Page — a recruitment AGENCY rather than an employer, so every row here carries
# "Michael Page" as its company and the real employer is named only in the JD. Read the long
# note above scrape_michaelpage before adding a second agency: it explains why these rows
# read as non-sponsors and why they arrive as one large single-company group.
MICHAELPAGE_BOARDS = [
    ("https://www.michaelpage.com/jobs", "michaelpage", "Michael Page"),   # ~9/page of 30
]

# Aquent — the SECOND recruitment agency, and the same bargain: `company` is "Aquent" on every
# row and the client is named only in the JD. One XML feed is the entire board, so this costs
# one request per sweep; the long note over scrape_aquent has the measurements and the reason
# it needs a bespoke adapter at all.
AQUENT_BOARDS = [
    # 649 posted -> 372 US -> 104 past the title filter -> 78 inside MAX_AGE_DAYS.
    ("https://aquent.com/feeds/jobs.xml", "aquent", "Aquent"),
]
# The literal lives in the tuple above, not here, because scripts/build_docs.py counts SOURCES
# by ast.literal_eval-ing each list in the + chain WITHOUT importing -- a Name in there evaluates
# to nothing and the board count in the generated docs silently stops matching len(SOURCES).
AQUENT_FEED = AQUENT_BOARDS[0][0]

# Y Combinator's Work at a Startup. The company name here is only the board's label -- every row
# scrape_workatastartup returns names the actual startup, and main()'s setdefault leaves it be.
WORKATASTARTUP_BOARDS = [
    ("https://www.workatastartup.com/jobs", "workatastartup", "Y Combinator"),   # ~250 across 10 roles
]

# Eightfold AI. ALWAYS carry ?domain=<company-domain> — the API requires it and deriving it from
# the tenant label ("insight" -> "insight.com") is a guess that silently returns nothing when wrong.
# Roughly a third of tenants answer a flat 403 no matter what headers we send (see
# scrape_eightfold), so a candidate that probes clean is worth adding and one that 403s is not
# worth retrying. Every row arrives with a REAL posting date from t_create, which is rare.
# Digitas (Publicis Groupe). ats_type "digitas" is deliberately NOT emitted by detect_board, so
# this cannot arrive from the add-a-board UI -- the scraper reads a 14 MB sitemap and then fetches
# a page per posting, which is fine for one known board and wrong to let anyone point anywhere.
DIGITAS_BOARDS = [
    ("https://www.digitas.com/en-us/careers", "digitas", "Digitas"),   # 86 rows -> 18 on-target
]

# Jobvite. board_url is the tenant ROOT, not /jobs -- scrape_jobvite appends that, and
# detect_board normalizes a pasted posting link down to this form.
JOBVITE_BOARDS = [
    ("https://jobs.jobvite.com/dwt", "jobvite", "Davis Wright Tremaine"),  # 18 rows -> 5 on-target
]

# Werfen. ats_type "werfen" is deliberately NOT emitted by detect_board, same as "digitas": the
# selectors in scrape_werfen are one site's Drupal view, not an ATS contract, so this must not be
# pointable at an arbitrary host.
WERFEN_BOARDS = [
    ("https://www.werfen.com/en/careers-finder", "werfen", "Werfen"),  # 188 rows -> 32 on-target
]

EIGHTFOLD_BOARDS = [
    ("https://bayer.eightfold.ai/careers?domain=bayer.com", "eightfold", "Bayer"),        # 607
    ("https://insight.eightfold.ai/careers?domain=insight.com", "eightfold", "Insight Enterprises"),  # 183
]

# Everything scrapeable: Amazon + boards + Workday + iCIMS/Jibe + Oracle + Phenom + Avature
# + SuccessFactors + PeopleSoft + Eightfold + Meta + Michael Page + Jobvite + Werfen.
# (Adzuna was in this list until 2026-08-16; see the removal note above EXTRA_BOARDS.)
# (Amazon-only: SOURCES = AMAZON   |   boards only: SOURCES = ATS_BOARDS + EXTRA_BOARDS)
SOURCES = (AMAZON + ATS_BOARDS + EXTRA_BOARDS + WORKDAY_BOARDS + JIBE_BOARDS
           + ORACLE_BOARDS + PHENOM_BOARDS + AVATURE_BOARDS + ULTIPRO_BOARDS + JOBDIVA_BOARDS
           + SF_BOARDS + PEOPLESOFT_BOARDS + PAYLOCITY_BOARDS
           + JOBSPY_BOARDS + METACAREERS_BOARDS + MICHAELPAGE_BOARDS + AQUENT_BOARDS
           + WORKATASTARTUP_BOARDS + EIGHTFOLD_BOARDS + DIGITAS_BOARDS
           + JOBVITE_BOARDS + WERFEN_BOARDS)

OUTPUT_CSV    = "jobs.csv"        # master list; only new jobs get appended
LOG_NOTE_FILE = "log.txt"         # the scheduler writes run output here (see README)
SPONSORS_FILE = "sponsors.txt"    # OPTIONAL: one employer name per line (DOL H1B data)
RESUME_FILE   = "resume.txt"      # résumé-driven scraping reads this to tune the search

# Keep a posting only if its TITLE matches one of these (whole word/phrase, not
# substring). Specific phrases keep precision: "program manager" matches, but
# "Experiential Programs Manager" (plural 'programs') does NOT — exactly what we want.
INCLUDE = (
    # --- Core project / program management ---
    "project manager", "program manager", "project management", "program management",
    "project coordinator", "program coordinator", "project administrator", "program administrator",
    "project specialist", "program specialist", "project analyst", "project associate",
    "project lead", "program lead", "pmo", "scrum master", "agile coach",
    # SAFe's release-train role. Needed explicitly: "release engineer" is in this list but the
    # phrase is "release TRAIN engineer", so it never matched.
    "release train engineer",
    "technical program manager", "technical project manager",
    "portfolio manager", "project portfolio",
    # --- Project controls / scheduling / planning (PM core; previously missing) ---
    "project controls", "project control", "project control analyst", "controls analyst",
    "cost controls", "cost control", "cost analyst",
    "scheduler", "project scheduler", "master scheduler", "planner scheduler",
    "project planner", "planning analyst",
    # --- Product ---
    "product manager", "associate product manager", "product owner",
    "product analyst", "product coordinator", "product operations",
    # "product management" as well as "product manager" — the matcher is whole-word, so the
    # two are different strings, and project/program above already list both forms. Without
    # it, "Product Management - Technical (Ads)" and every "<Level>, Product Management"
    # title missed entirely. NOT "product development": measured, it pulls hardware
    # ("Product Development Engineer, Annapurna Labs Silicon").
    "product management",
    # product strategy family (PM-adjacent; product-scoped so it avoids the
    # marketing "brand/content/media strategist" noise that bare "strategist" pulls).
    "product strategist", "product strategy",
    # --- Coordination / operations / analyst (related domain) ---
    "operations coordinator", "operations manager", "operations analyst",
    "operations specialist", "business operations",
    "operations management",
    # "operations associate" was REMOVED 2026-08-09 after measuring it. At retail and
    # self-storage chains it means shop-floor shift work, not coordination, and it was the sole
    # reason 306 rows were kept: Sephora 112, CubeSmart 111 — 73% of them — with exactly ONE of
    # the 306 clearing the feed's 45% default match floor (median 24). Only 9 rows carrying the
    # phrase also match another keyword, so they survive without it. The near-twin
    # "operations specialist" was measured too and kept: 212 rows, median 27, at Mayo Clinic and
    # Kinder Morgan rather than shop floors. Re-add only with numbers.
    "business analyst", "data analyst",
    "implementation", "implementation manager", "implementation specialist",
    "delivery manager", "engagement manager",
    # ---------------------------------------------------------------------------------------
    # 2026-08-20: the four role families Kunal asked for, MEASURED against a full sweep dump
    # (scripts/dump_titles.py --all: 334,716 postings, 185,753 US, 24,804 kept by the filter as
    # it stood, 107,284 in the drop pool after the EXCLUDE veto). Ranked by
    # scripts/score_title_candidates.py, which credits a phrase only for rows it is the SOLE
    # reason for keeping. Numbers below are that marginal count.
    #
    # FIRST, THE THING WORTH KNOWING: the examples Kunal gave were already working. "Associate
    # Project Manager", "Assistant Product Manager", "Senior Project Manager" and "Project
    # Manager II" all match today, because the matcher is whole-PHRASE and any seniority prefix
    # rides along on "project manager". No keyword was needed for those.
    "chief of staff",              # +60, 43 employers -- exec-ops delivery work
    "delivery lead", "delivery analyst",           # +34, +5 -- spread across 27 employers
    "technical delivery", "agile delivery",        # +1 each; the phrasings, for completeness
    "strategic initiatives", "initiatives manager",   # +30, +2
    "program analyst",             # +23 -- "Program Analyst, Legal Ops", "Operations Program"
    "integration manager",         # +21, 17 employers
    "deployment manager",          # +16 -- network/robotics rollout delivery
    "process improvement", "process analyst",      # +17, +11
    "business transformation", "transformation manager",   # +10, +16 (ERP/finance programmes)
    "change manager", "change management", "change analyst",   # +9, 0, +1
    "requirements analyst", "resource planner", "project support",       # +1, +1, +4
    # Abbreviations. The employers with the most openings write them: Amazon posts "TPM" and
    # "Prog Mgr", Disney "Sr Tech Project Mgr". Exactly the "software dev engineer" case, where
    # one missing abbreviation was dropping 178 postings on a single board.
    "tpm", "project mgr", "program mgr", "prog mgr", "proj mgr", "proj manager", "pgm mgr",
    "epmo", "project management office", "release train",
    # British spelling, and the plural coordinator forms. Nearly zero rows in this dump, kept
    # anyway on the same reasoning as the low-volume programme markers below: they cost nothing
    # and a real one is unambiguously wanted.
    "programme manager", "programme management", "programme coordinator",
    "projects coordinator", "programs coordinator",
    # Singular forms of two phrases already here in the plural. Found by testing a blanket
    # plurals relaxation of the matcher, which is NOT shipped (see below) -- but it surfaced 65
    # Amazon "System Development Engineer" postings being dropped while "systems development"
    # sat in this list. Same class of miss as "software dev engineer", same fix.
    "system development",          # +65, Amazon 63
    "application development",     # +31, AWS delivery consultants
    #
    # THE MISSPELLING QUESTION, ANSWERED WITH NUMBERS. Kunal asked for typo coverage on the
    # theory that we lose jobs to them. 40 candidate misspellings were measured against all
    # 185,753 US postings in the dump, and THIRTY-NINE of them matched nothing at all:
    # porject / proejct / prject / projct / proect manager, prodcut / produt / prodct manager,
    # progarm / progran manager, cordinator / coordinater / coordiator / coordintor, analist /
    # analyist / anaylst, specilist / speclialist, asociate / assistent, buisness / bussiness,
    # opertions / operatons / oprations, enginer / engineeer, devloper / deveploer, sofware /
    # softwre, scrum mater / msater. ATS titles are typed into a form by recruiters and then
    # reused, so they are cleaner than expected. Exactly one earns its place:
    "program manger",              # +3, all Amazon, all real: "Program Manger, AUTA Experience"
    # Bare "manger" was measured too (+14) and REJECTED: it is a real English word and the extra
    # rows were "Sales Manger" and "Contract manger" at Hilton and Five Below. The two-word form
    # keeps the win without the retail.
    #
    # TWO MATCHER RELAXATIONS MEASURED AND NOT SHIPPED, both plausible and both wrong:
    #   plurals (optional trailing s on the thing-word)  +147 rows, but 66 were the Amazon
    #     "System Development" case above, now fixed precisely by two phrases instead of by
    #     loosening all 234. The "Programs Manager" titles this was meant to catch barely exist.
    #   punctuation as a word separator                  +8 rows, ALL EIGHT junk: "Staff
    #     Engineer Systems - Development Lead", "Angular Front- End Developer", "Business
    #     Continuity Program - administrator". A hyphen bridging two unrelated words is not a
    #     phrase. Independently confirms the +0 result 027dff7 got for the same idea.
    # A bare "specialist" was measured as a control: +6,473 rows, 583 employers, led by
    # OneMain 277 and Amazon 272 ("Sales Specialist", "PR Specialist"). That is the number the
    # note above about bare single words is protecting against.
    #
    # MEASURED AND REJECTED. Every one of these looked like an obvious addition and is not; the
    # rows are real, they are just somebody else's job.
    #   project engineer        +349  the biggest candidate in the file and a CONSTRUCTION
    #                                 flood: Actalent 60, M.C. Dean 59, Sundt 15, Lemartec 15,
    #                                 and Amazon's are "Project Engineer, DC Construction".
    #                                 43% from four contractors -- the same shape "trainee" had.
    #   continuous improvement  +70   manufacturing-plant lean roles: Regal Rexnord, Celestica,
    #                                 Danaher, Hubbell. EXCLUDE already names manufacturing.
    #   portfolio management    +41   INVESTMENT management -- Morgan Stanley 11, BlackRock,
    #   portfolio analyst        +8   JPMorgan, Fidelity, "Quantitative Portfolio Analyst".
    #   vendor manager          +31   Amazon retail CATEGORY BUYING ("Sr. Vendor Manager, Canada
    #                                 Fashion", "Toys & Entertainment"), not procurement.
    #   transformation lead     +17   sales-contaminated: Amazon's is "Business Development -
    #                                 Industrial transformation lead".
    #   delivery specialist     +12   "Olympic Power Delivery Specialist", agri co-ops.
    #   program associate       +11   investment-banking and university programmes.
    #   capacity planning        +9   already-adjacent; weak signal either way.
    #   governance analyst       +7   data/identity governance, i.e. security.
    #   operations administrator +7   "Deal Operations Administrator" -- sales ops.
    #   resource manager         +6   "Biochemistry Resource Manager".
    #   bsa                      +6   Bank Secrecy Act / AML, not Business Systems Analyst.
    #   apm                      +5   Application Performance Monitoring. Not Associate Product
    #                                 Manager -- "APM Twitch", "APM Serverless".
    # ---------------------------------------------------------------------------------------
    "supply chain analyst", "logistics analyst", "supply chain manager", "logistics manager",
    # NOTE: bare single words (analyst/coordinator/specialist/associate/consultant) stay OUT —
    # they pulled retail/hourly/clinical noise. Trimmed for focus (2026-06-16): "financial analyst",
    # "marketing manager", and the generic consulting block (consulting/management/strategy/business
    # consultant) — off-PM scatter the user flagged. Re-add any of these to widen the net again.
    # --- Software engineering (added 2026-08-01) ---
    # The whole software/data/infra track. Bare "engineer"/"developer"/"scientist" deliberately
    # stay OUT of this list (they'd pull mechanical/civil/chemical/lab roles); every entry is a
    # software-specific PHRASE instead, and the non-software engineering disciplines are named
    # explicitly in EXCLUDE below so the generic early-career markers ("intern", "new grad")
    # can't drag a Mechanical Engineering Intern in through the side door.
    "software engineer", "software developer", "software engineering", "software development",
    "software development engineer", "sde", "swe", "programmer", "programmer analyst",
    # Amazon writes it ABBREVIATED — "Software Dev Engineer II" — which matches neither
    # "software development engineer" nor the acronym. Whole-word matching meant 178 SDE
    # postings on one board were being dropped as "no matching role keyword", i.e. the
    # single most on-target software title at the employer with the most openings.
    "software dev engineer", "software dev",
    "application engineer", "applications engineer", "application developer",
    "applications developer", "systems analyst", "computer science",
    # web / front-end / back-end / full-stack / mobile
    "web developer", "web development", "web engineer",
    "front end engineer", "front-end engineer", "frontend engineer",
    "front end developer", "front-end developer", "frontend developer",
    "front end software", "frontend software",
    "back end engineer", "back-end engineer", "backend engineer",
    "back end developer", "back-end developer", "backend developer",
    "backend software", "back end software", "back-end software",
    "full stack", "full-stack", "fullstack",
    "ui engineer", "ui developer", "javascript developer", "react developer",
    "mobile engineer", "mobile developer", "mobile application engineer",
    "mobile application developer", "mobile software",
    "ios engineer", "ios developer", "android engineer", "android developer",
    "game developer", "embedded software",
    # language / platform specific
    "java developer", "python developer", "net developer", "dotnet developer",
    "c# developer", "salesforce developer", "sql developer", "etl developer",
    "bi developer", "rpa developer", "api engineer", "integration engineer",
    # AI / ML / data
    "machine learning engineer", "machine learning", "ml engineer", "mlops",
    "ai engineer", "ai/ml engineer", "ai developer", "artificial intelligence",
    "deep learning", "nlp engineer", "computer vision", "prompt engineer",
    "data scientist", "data science", "applied scientist", "machine learning scientist",
    "data engineer", "data engineering", "analytics engineer", "big data",
    "etl engineer", "business intelligence", "bi analyst",
    "database administrator", "dba", "database engineer", "database developer",
    # infra / devops / cloud / QA / security
    "devops", "dev ops", "devsecops", "site reliability", "sre",
    "platform engineer", "infrastructure engineer", "cloud engineer", "cloud developer",
    "cloud support engineer", "systems development", "systems development engineer",
    "systems engineer", "system engineer", "network engineer",
    "release engineer", "build engineer", "automation engineer", "test automation",
    "test engineer", "qa engineer", "qa analyst", "quality assurance engineer",
    "sdet", "software test", "software quality",
    "security engineer", "application security", "kubernetes",
    # --- Early-career / new-grad markers ---
    # This block used to say "low noise". Measured 2026-08-09 and that was wrong: FIVE of these
    # admitted 1,015 rows between them and NOT ONE of those rows cleared the feed's 45% default
    # match floor. Removed, with what they were actually dragging in:
    #   trainee      425 rows, median 20 — Cintas 125, Red Bull 64, and the "Store Manager
    #                Trainee" postings at Safeway / Sephora / Town Pump that read as floor
    #                management. This one keyword was the source of the retail-management noise.
    #   entry level  315 rows, median 13 — Aspen Dental 167, i.e. dental assistants
    #   apprentice   175 rows, median 19 — FedEx Freight 62 (driver apprenticeships)
    #   graduate      59 rows, median 17 — PMG, Mayo Clinic; matches "Graduate Nurse"
    #   entry-level   41 rows, median  0 — Actalent 18, Boeing 7
    # Kept below: the four low-volume program markers (42 rows all told, so they cost nothing and
    # a real "Early Career Program Manager" would want them), plus intern / internship / co-op.
    # Those three look weak on the same metric (661/188/59 rows, 4/3/1 clearing) but they stay
    # DELIBERATELY: they feed the feed's "Internships & co-ops only" filter, internships are
    # OPT/STEM-OPT eligible, and an internship scoring low against a senior PM résumé is expected
    # rather than evidence it is junk.
    "new grad", "early career",
    "rotation program", "rotational program",
    # Internships & co-ops — OPT/STEM-OPT lets the user do these. The matcher is
    # whole-word, so plurals/variants are listed explicitly. The EXCLUDE block still
    # drops eng/clinical/retail/trades interns (incl. the eng/research-intern phrases
    # added to EXCLUDE below, which the word-boundary "engineer"/"scientist" miss).
    "intern", "interns", "internship", "internships",
    "co-op", "co-ops", "coop", "coops", "co op",
    "summer analyst", "summer associate",
)
# ...but drop it if the title ALSO matches any of these.
# Seniority markers, deliberately NOT part of EXCLUDE any more (2026-08-08).
#
# They say "wrong LEVEL", which is a different claim from the rest of EXCLUDE's "wrong
# FIELD" — and because the exclude check runs FIRST and vetoes unconditionally, a single
# seniority word used to override a perfectly on-target role. "Principal Product Manager
# Tech, eShop" at Amazon was dropped on the word "Principal" despite "product manager"
# being one of the résumé's own search terms.
#
# Dropping the veto is safe because the keep rule already requires an INCLUDE match, so a
# genuinely off-target senior title still fails on its own: "Chief Financial Officer"
# matches no role keyword, and "Principal Mechanical Engineer" is still caught by
# "mechanical" below. Measured on Amazon's 4,064 postings: 163 titles were vetoed by a
# seniority word, and only 66 of those become keeps — +2.6% against 2,513 already kept.
#
# The right place to express "too senior for me" is the FEED's experience filter, which
# reads years-required out of the JD and can be changed without a re-scrape. This gate is
# permanent: a title dropped here is never stored, so it can never be reconsidered.
# Re-adding this tuple to EXCLUDE restores the old behaviour exactly.
EXCLUDE_SENIORITY = ("principal", "head", "director", "vp", "vice president", "chief",
                     "iv", "expert")

EXCLUDE = (
    # "architect" stays here rather than in EXCLUDE_SENIORITY: it names a different JOB
    # (solutions/enterprise architect), not a level of the target ones.
    "architect",
    # Clearly off-target functions. NOTE (2026-08-01): "engineer", "developer" and
    # "scientist" USED to be here — they were removed when the software-engineering
    # block was added to INCLUDE, since a bare "engineer" drops "Software Engineer"
    # too. The non-software engineering disciplines are now named explicitly further
    # down instead. Re-add these three words to go back to a PM/analyst-only feed.
    "designer", "counsel", "attorney",
    "physician", "nurse", "account executive", "sales development", "sdr",
    # Trades / retail / hospitality — these sneak in via the early-career markers
    # ("apprentice"/"trainee"/"entry level"): e.g. Tesla's "Apprentice Collision
    # Technician" or Safeway's "Front End Entry Level".
    "technician", "technicien", "mechanic", "machinist", "welder", "electrician",
    "plumber", "detailer", "collision", "culinary", "chef", "barista", "advisor",
    "cashier", "janitor", "custodian",
    # Grocery / retail floor roles (a single big grocery board — Safeway/Albertsons —
    # otherwise floods the feed: 462 "Front End Entry Level" clerks in one run).
    # ("front end" used to be a bare exclude here for the grocery flood, but that also
    # killed "Front End Engineer"/"Front End Developer" — so the grocery-specific
    # phrasings are spelled out instead.)
    "front end entry level", "front end clerk", "front end associate", "front end retail",
    "front end service", "front end supervisor", "front end team member", "front end checker",
    "courtesy clerk", "grocery", "deli", "bakery", "cake decorator",
    "produce", "meat", "seafood", "stocker", "bagger", "checker", "store associate",
    "retail associate", "sales associate", "sales representative", "merchandiser",
    # Employment-type markers, which catch retail-floor postings whatever the title says —
    # Sephora's "Operations Associate - Part Time" arrived under an on-target-looking phrase.
    # Measured 2026-08-09: 128 rows carry part-time (81 of them Sephora), median score 22, and
    # NOT ONE clears the 45% floor. Both spellings are listed because the matcher escapes each
    # term literally, so "part time" would not catch "Part-Time".
    # DELIBERATELY NOT here, both measured and both earning their place: "flex" (38 rows but 3
    # clear the floor — Amazon's "US Flex Business Optimization" and Engie's "Renewables Flex"
    # are real programme names) and "full time" (63 rows, and Cisco's "Operations Analyst II
    # (Full-Time)" scores 53).
    "part time", "part-time", "seasonal",
    "stock clerk", "pharmacy graduate", "pharmacy intern", "warehouse associate",
    # Retail-floor / wireless / loss-prevention / clinical-bedside / academic-lab roles —
    # NOT a PM/analyst/ops/STEM track. These flooded in under the wide net (T-Mobile
    # "Mobile Associate - Retail Sales", Target "Security Specialist"/"Assets Protection",
    # hospital "Patient Care Coordinator", "Postdoctoral Research Associate").
    "mobile associate", "retail sales", "wireless", "security specialist",
    "assets protection", "loss prevention", "patient", "postdoctoral", "postdoc",
    "teller", "phlebotom", "caregiver", "client service associate",
    "surgery", "surgical",          # clinical schedulers/coordinators (bare "scheduler" else slips)
    # Off-domain intern/co-op variants the word-boundary excludes above miss
    # ("scientist" doesn't match "Science"). NOTE: "engineering intern"/"co-op" and
    # "software intern" were REMOVED here on 2026-08-01 — they blocked "Software
    # Engineering Intern", which is now a wanted title. Non-software engineering
    # interns are caught by the discipline block below instead.
    "hardware intern", "research intern", "design intern",
    "laboratory intern", "lab intern",
    "nursing intern", "clinical intern", "medical intern", "pharmacy intern",
    # --- Non-software ENGINEERING disciplines (added 2026-08-01) ---
    # Bare "engineer" is no longer an exclude, so the other engineering fields have to
    # be named. These words never appear in a software/data/infra title, and they also
    # stop the generic early-career markers ("intern", "new grad", "entry level",
    # "trainee", "co-op") from dragging in a Mechanical Engineering Intern.
    "mechanical", "civil", "chemical", "electrical", "structural", "geotechnical",
    "aerospace", "aeronautical", "astronautical", "avionics", "propulsion", "flight",
    "petroleum", "drilling", "geological", "geologist", "mining engineer",
    "metallurgy", "metallurgical", "biomedical", "bioengineering", "biochemical",
    "agricultural", "marine engineer", "nuclear", "hvac", "piping", "welding",
    "thermal", "combustion", "hydraulic", "acoustic", "corrosion",
    "industrial engineer", "industrial engineering",
    "manufacturing engineer", "manufacturing engineering",
    "materials engineer", "materials engineering", "materials science",
    "environmental engineer", "environmental engineering", "environmental science",
    "process engineer", "process engineering", "plant engineer", "facilities engineer",
    "field engineer", "packaging engineer", "sales engineer",
    "transportation engineer", "traffic engineer", "water resources", "wastewater",
    "hardware engineer", "hardware engineering", "rf engineer", "optical engineer",
    "antenna", "asic", "fpga", "vlsi",
    # Non-software "science" fields (bare "science intern" used to be excluded, which
    # also killed "Computer Science Intern" / "Data Science Intern").
    "life science", "political science", "animal science", "food science",
    "health science", "clinical science", "social science",
    # Measured leaks from the first widened run (2026-08-01). Each of these was checked
    # against all 8,094 new titles first and drops ONLY non-software roles — no software
    # title anywhere in that batch matches them. Utility/rail/lab "engineer" titles get
    # in through the generic early-career markers ("entry level", "trainee", "co-op").
    "substation", "distribution engineer", "transmission engineer",
    "passenger engineer", "locomotive",
    # "train engineer" USED to be a bare exclude here, and it silently vetoed
    # "Release Train Engineer" -- a core SAFe/Agile role that core.ROLE_FAMILIES already lists
    # under `scrum`, so the scraper and the feed disagreed about whether it was a job we want.
    # EXCLUDE runs first and vetoes unconditionally, so no INCLUDE entry could rescue it; the
    # rail phrasings are spelled out instead. Exactly the fix used for "front end" above, which
    # was killing "Front End Engineer" for the same reason.
    # Bare "Train Engineer" needs no exclude at all: it matches no INCLUDE phrase, so it is
    # already dropped as "no matching role keyword".
    "passenger train engineer", "freight train engineer", "train engineer trainee",
    "stationary engineer", "operating engineer", "building engineer",
    "manufacturing test engineer", "physical security",
    "medical laboratory", "laboratory scientist", "rfid",
)

# If sponsors.txt is loaded: True = DROP companies not on the list; False = keep
# everything and just FLAG sponsor status (yes/no). We default to flag-only so a
# good employer that simply isn't in your data file never gets thrown away.
REQUIRE_SPONSOR = False

# Keep only jobs located in the USA. Set to False to keep every location.
US_ONLY = True


def _env_flag(name):
    """An env var read as a boolean, accepting the spellings people actually type."""
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


# Print a KEEP/drop line (with the reason) for every scraped title. Great for
# tuning the filter on ONE board, but noisy across many — so it's off by default.
# SCRAPE_VERBOSE=1 turns it on without editing this file; every other tunable here is
# already env-settable, and this one being source-only is what made the filter hard to tune.
VERBOSE = _env_flag("SCRAPE_VERBOSE")

# Where to append one line per DROPPED posting, or "" for nowhere. Unset by default, so CI
# and the cron are untouched.
#
# The drop counters below tell you 213,327 titles failed the keep rule; they cannot tell you
# WHICH, so every question of the form "why isn't this job in the feed?" has been answered by
# hand (that is how the Disney "Manager, Projects" case was found). One local sweep with this
# set turns the whole reject pile into a file, and every candidate keyword can then be measured
# offline in milliseconds against real titles instead of argued about.
#
# LOCATION IS IN THE DUMP ON PURPOSE. The US gate runs only on titles that already passed the
# keep rule, so a title-rejected row never gets one -- and a measurement that forgets to apply
# it offline counts jobs in Bangalore as wins.
DUMP_REJECTS = os.environ.get("SCRAPE_DUMP_REJECTS", "").strip()

# Drop a job if its description requires MORE than this many years of experience.
# Only enforced where the scraper actually has the JD text (e.g. Amazon). 5 = keep mid-level too.
MAX_YEARS = 5

# Refuse a posting the employer published more than this many days ago — by then the role
# is usually filled, and storing it just inflates the database. Only applied when the board
# actually publishes a date: a posting with NO date (Meta, Workable, BambooHR, Rippling, and
# one of the two Avature templates) is KEPT and ages by first_seen instead, because for those
# boards "still listed" is the only freshness signal there is. 0 disables the gate.
MAX_AGE_DAYS = int(os.environ.get("MAX_AGE_DAYS", "30") or 0)

# How many not-yet-scraped employers from migratemate.co's public sponsor directory to probe
# for a readable ATS board on each scrape. Reading the directory itself is ONE request; the
# cost is the probing, at ~0.7 companies/sec, so 60 is about 90 seconds. Their JOB pages are
# never fetched here — this discovers employers, not postings. 0 disables it.
DISCOVER_LIMIT_DEFAULT = 60

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
    # Sized for the scrape's worker count, not below it. pool_connections is how many
    # per-HOST pools stay cached — the board list spans ~1,200 hosts, so a small number
    # evicts pools constantly and pays a fresh TLS handshake per board. pool_maxsize is
    # connections WITHIN one host's pool, and it has to cover the workers that can land on
    # the same host at once: 408 of the boards are on job-boards.greenhouse.io alone, and a
    # too-small pool there logs "Connection pool is full" and discards live connections.
    adapter = HTTPAdapter(max_retries=retry, pool_connections=64, pool_maxsize=32)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


SESSION = _make_session()


# ============================================================
# TRUNCATION REGISTRY
#
# Every paged scraper stops at a safety cap so one giant tenant can't page forever. The
# danger isn't the cap, it's that hitting one used to be SILENT: the board reported a
# healthy-looking count and nobody could tell it was a ceiling rather than the real total.
# That is how "Program Manager, Relo Ops Excellence (RLOI)" went missing — Amazon returned
# 734 hits for its search term and the scraper took the first 200 without comment.
#
# So a scraper that stops because of its cap now says so, and main() prints the list at the
# end of the run. Finding out costs one line of output; not finding out costs a job you only
# notice months later because you happened to see it somewhere else.
# ============================================================
TRUNCATED = []


def note_truncation(board, fetched, cap, total=None, detail=""):
    """Record that `board` stopped at its cap rather than running out of results.

    total=None means the source never told us how many there were (most don't), so all we
    can say is "we stopped at the ceiling". When a source DOES report a total, we can show
    exactly how much was left behind — far more actionable.
    """
    TRUNCATED.append({"board": board, "fetched": fetched, "cap": cap,
                      "total": total, "detail": detail})


def truncation_report():
    """Human-readable summary of everything that hit a ceiling this run ('' if nothing did)."""
    if not TRUNCATED:
        return ""
    lines = ["", "!! %d board(s) hit a paging cap — these are TRUNCATED, not complete:" % len(TRUNCATED)]
    for t in sorted(TRUNCATED, key=lambda x: -(x.get("total") or x["fetched"])):
        miss = ""
        if t.get("total"):
            miss = "  (source reports %d — missing ~%d)" % (t["total"], max(0, t["total"] - t["fetched"]))
        lines.append("   %-34s stopped at %d (cap %d)%s%s"
                     % (t["board"][:34], t["fetched"], t["cap"], miss,
                        (" " + t["detail"]) if t["detail"] else ""))
    lines.append("   Raise that source's MAX_* constant to go deeper (costs scrape time).")
    return "\n".join(lines)



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


# Analytics params. These say where a click came FROM, never WHICH posting it points at,
# so two URLs differing only in these are the same job.
_TRACKING_PARAMS = frozenset((
    "gh_src", "utm_source", "utm_medium", "utm_campaign",
    "utm_term", "utm_content", "utm_id", "utm_ref",
    # jr_id rides on every link a job-alert aggregator hands out, across unrelated ATS hosts
    # (Freshteam, CATS, Paylocity, ADP all seen with it). It identifies the referral, not the
    # posting, so without this the same job stores twice — once as we scraped it, once as the
    # user pasted it.
    "jr_id",
    # lever-source names where an applicant came from. Global rather than host-scoped because
    # the param name is already namespaced to one ATS, so it can't collide the way "se" could.
    # Measured 2026-08-09: all 180 stored Lever rows are bare, while an aggregator hands out
    # jobs.lever.co/<co>/<uuid>?lever-source=Indeed for the SAME posting — the uuid is the
    # identity, so without this every relisted Lever job would store a second time.
    "lever-source",
))

# Greenhouse serves every board under two interchangeable hostnames, and the API's
# absolute_url has flipped between them over time — so one posting arrives as
# boards.greenhouse.io/<co>/jobs/<id>?gh_jid=<id> on an old run and as
# job-boards.greenhouse.io/<co>/jobs/<id> on a later one, landing twice in `jobs`
# (which is keyed on url alone).
_GH_HOSTS = frozenset(("boards.greenhouse.io", "job-boards.greenhouse.io"))

# Adzuna mints a FRESH `se=` token on every API response, so the identical advert arrives with a
# different url each run and the url-keyed table stores it again. Measured against the live corpus
# 2026-08-09: 473 ad ids held more than one row, 1,293 surplus rows in all, one ad stored SEVEN
# times — same id, same v=, same title/company, same found_date, differing only in `se=`.
#
# Scoped to the Adzuna hosts rather than added to _TRACKING_PARAMS, because "se" is two letters and
# could plausibly identify a posting on some other board; the same reasoning that keeps gh_jid
# host-scoped. `v=` is deliberately KEPT — it was identical across all seven copies, so it isn't
# what splits them, and this file's rule is to drop only what provably cannot identify a posting.
_ADZUNA_HOSTS = frozenset(("adzuna.com", "www.adzuna.com"))
_ADZUNA_VOLATILE_PARAMS = frozenset(("se",))

# Indeed is the one host where we ALLOW-list rather than deny-list, and that inversion needs
# justifying because everywhere else in this file the rule is "drop only what provably cannot
# identify a posting".
#
# An Indeed posting URL carries exactly ONE identifying param — jk (or vjk on a search-results
# page). Everything else is session/presentation state, and the set of those changes without
# notice: from, tk, vjs, advn, adid, sjdu, xkcb, xpse, acatk, pub, rgtk, hidesmb, jsa, alid, mo
# have all been observed. A deny-list has to be edited every time Indeed adds one, and the cost
# of missing one is the Adzuna `se=` incident above — 1,293 surplus rows off a single param.
#
# The blast radius is bounded two ways: the rule is host-scoped, and it only fires when the URL
# actually carries jk or vjk. An indeed.com URL with neither (a company listing page, a saved
# search) is not a posting, so it falls through to the ordinary tracking deny-list untouched
# rather than having its whole query stripped.
_INDEED_ID_PARAMS = ("jk", "vjk")

# LinkedIn's own params. currentJobId is deliberately KEPT: on /jobs/search/?currentJobId=<id>
# it is the only thing naming the posting, exactly as gh_jid is on a company-hosted Greenhouse
# board. The rest say where a click came from.
#
# These rules earn their place even though LinkedIn is not a scrape source: the browser
# extension imports LinkedIn URLs into the corpus, which is why web._AGGREGATOR_HOSTS lists it.
_LINKEDIN_DROP_PARAMS = frozenset((
    "refid", "trackingid", "trk", "position", "pagenum", "ebp", "originalsubdomain",
))

# Workday serves one posting at both /<site>/job/... and /<locale>/<site>/job/..., and which one
# you get depends on how you arrived. Measured against the live corpus 2026-08-09: of 7,941
# Workday rows, 7,910 are bare and 31 carry /en-US/ (Bloomberg, MSD) — so BOTH forms are already
# stored, and this is a duplicate waiting to happen independently of any aggregator. An
# aggregator makes it near-certain: Indeed's direct links to Salesforce and Expedia both arrive
# with /en-US/ while our own Workday scraper emits the bare form.
#
# This is the second PATH rewrite in this file, after the Greenhouse host swap, so it is kept
# deliberately tight: the segment must be exactly xx-XX, and only as the FIRST path segment on a
# myworkdayjobs.com host. Workday site names in the corpus (External_Career_Site, abbottcareers,
# SearchJobs, Bloombergindustrygroup_External_Career_Site) cannot match that shape.
_WORKDAY_LOCALE_RE = re.compile(r"^/[a-z]{2}-[a-z]{2}(?=/)", re.I)


def canonical_url(url):
    """One posting -> one URL string, so the url-keyed `jobs` table can't hold it twice.

    DELIBERATELY conservative: merging two genuinely different postings is far worse than
    keeping a duplicate, so this only drops what provably cannot identify a posting. Two
    rules that look obvious were measured against the live corpus and REJECTED:

      * stripping gh_jid everywhere — on company-hosted Greenhouse boards it is the ONLY
        identifier (stripe.com/jobs/search?gh_jid=7061338), and dropping it collapsed 64
        distinct Stripe postings into one. It is removed only on a greenhouse.io host
        whose path already ends in that same id, where it is pure duplication.
      * dropping the #fragment — JobDiva's portal is hash-routed, so
        www1.jobdiva.com/portal/?a=<token>#/jobs/<id> tells 424 postings apart by
        fragment alone.

    The query string is rebuilt only when a param is actually dropped, so re-encoding
    can never silently rewrite a URL we meant to leave alone. Non-http(s) or unparseable
    input comes back unchanged."""
    if not url:
        return url
    try:
        s = urlsplit(url.strip())
        if s.scheme.lower() not in ("http", "https"):
            return url                       # is_http_url() rejects these before storage
        host = (s.hostname or "").lower()
        if not host or ":" in host:
            return url                       # no host, or an IPv6 literal — urlsplit drops
                                             # the [brackets] and we'd rebuild it malformed
        hostname = host                      # port-free, and stable across the host rewrite
                                             # below — the suffix rules match on this
        if s.port:
            host = "%s:%d" % (host, s.port)
        path, query = s.path, s.query
        pairs = parse_qsl(query, keep_blank_values=True)
        keep = pairs
        if host in _GH_HOSTS:
            host = "job-boards.greenhouse.io"
            job_id = path.rstrip("/").rsplit("/", 1)[-1]
            keep = [(k, v) for k, v in keep
                    if not (k.lower() == "gh_jid" and v == job_id)]
        if host in _ADZUNA_HOSTS:
            keep = [(k, v) for k, v in keep
                    if k.lower() not in _ADZUNA_VOLATILE_PARAMS]
        if hostname == "indeed.com" or hostname.endswith(".indeed.com"):
            # jk wins outright; vjk only names the posting when there is no jk. Nothing else
            # on this host identifies anything — see _INDEED_ID_PARAMS.
            present = {k.lower() for k, _ in keep} & set(_INDEED_ID_PARAMS)
            if present:
                wanted = "jk" if "jk" in present else "vjk"
                keep = [(k, v) for k, v in keep if k.lower() == wanted]
        if hostname == "linkedin.com" or hostname.endswith(".linkedin.com"):
            keep = [(k, v) for k, v in keep
                    if k.lower() not in _LINKEDIN_DROP_PARAMS]
        if hostname.endswith(".myworkdayjobs.com"):
            path = _WORKDAY_LOCALE_RE.sub("", path, count=1)
            # LOWERCASE THE SITE SEGMENT — the first one left after the locale comes off.
            #
            # Workday serves the same requisition under whatever casing the link used, and
            # `url` is the primary key on `jobs`, so /external/… and /External/… were stored as
            # TWO rows for one posting. Everything downstream falls out of that one comparison:
            # the feed showed the same opening twice, the second row carried whatever company
            # label that scrape pass derived ("Amat" beside "Applied Materials"), /companies
            # listed 117 openings as two employers with two sponsorship records, _logo_slug
            # could not resolve the alias so one card rendered a monogram, and /job offered the
            # posting to itself as a "Similar Role".
            #
            # ONLY this segment. Plenty of ATS paths are genuinely case-sensitive, and the
            # requisition id below it certainly is; lowercasing the whole path would break them.
            # The locale strip immediately above is the precedent for Workday-specific path
            # normalisation living here.
            _wd = path.split("/")
            if len(_wd) > 1 and _wd[1]:
                _wd[1] = _wd[1].lower()
                path = "/".join(_wd)
        keep = [(k, v) for k, v in keep if k.lower() not in _TRACKING_PARAMS]
        if len(keep) != len(pairs):
            query = urlencode(keep)
        if len(path) > 1:
            path = path.rstrip("/") or "/"
        return urlunsplit(("https", host, path, query, s.fragment))
    except Exception:
        return url                           # a URL we can't parse is left exactly as-is


def _ip_is_public(addr):
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or
                ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def public_http_url(url):
    """The URL if it's http(s) AND its host resolves only to PUBLIC IPs, else None — so a
    user URL can't make us reach loopback / private ranges / link-local cloud metadata.

    KNOWN RESIDUAL, recorded rather than quietly carried: this is a TIME-OF-CHECK check. It
    resolves the hostname, validates the addresses, and then returns the URL — and the caller
    hands that URL to `requests`, which resolves it a SECOND time. A hostile DNS record that
    answers public on the first lookup and 127.0.0.1 on the second defeats it. That is the
    standard bypass for this shape of guard (DNS rebinding).

    Closing it means pinning the validated address: resolve once, connect to the IP, and carry
    the original Host header and TLS SNI — a custom requests HTTPAdapter. That is a change to
    the transport every scraper in this file shares, so it is not something to land without
    exercising it against real boards; done wrong it breaks fetching everywhere, silently and
    all at once.

    What IS closed: the redirect loop re-validates every hop, so a public host cannot bounce us
    inward. And these are all classified correctly on Python 3.13/3.14, each checked:
    IPv4-mapped IPv6 (::ffff:127.0.0.1), decimal and octal IPv4 literals, and NAT64
    (64:ff9b::/96).
    """
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


def _safe_form_post(url, fields, headers=None, timeout=25):
    """Form-encoded POST, hardened exactly like _safe_post (which sends JSON).

    Needed because PeopleSoft's portal speaks application/x-www-form-urlencoded and rejects
    a JSON body outright, so the JSON helper can't be reused for it."""
    headers = dict(headers or HEADERS)
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    if not public_http_url(url):
        raise ValueError("blocked non-public URL: %s" % url)
    r = SESSION.post(url, headers=headers, data=fields, timeout=timeout,
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

def _posted(s):
    """ISO timestamp -> 'YYYY-MM-DD' ('' stays '' so main()'s scrape-stamp fallback kicks in)."""
    return (str(s) if s else "")[:10]


# ---------------------------------------------------------------------------------------------
# DESCRIPTIONS THAT ARRIVE WITH THE LISTING.
#
# Several ATS list feeds return the full description in the SAME response the sweep already
# reads -- for lever, ashby and jibe it is byte-identical to the URL score_jobs re-fetches in a
# later pass, so the text was being downloaded and thrown away twice over. Keeping it buys two
# things: main() can judge a posting by its DESCRIPTION when the title matches nothing, and the
# JD is banked for free instead of costing a per-job detail fetch out of the scoring budget.
#
# A row carrying a "jd" key needs no schema change: main() stores `{k: j.get(k, "") for k in
# FIELDNAMES}` and FIELDNAMES has no jd, so the key rides along through the whole keep loop and
# is dropped at the boundary. main() persists it separately, keyed by the CANONICAL url it just
# computed -- which is how the JobDiva bug (a stored `/portal?a=` vs a generated `/portal/?a=`,
# one character, 352 rows silently discarded every run) cannot happen here.
def _listing_jd(*parts):
    """Join the description fragments a list feed handed us into one clean block of text."""
    out = [core.html_to_text(p) for p in parts if p]
    return " ".join(t for t in out if t)


# Greenhouse serves the full description from the SAME list endpoint when asked, so the JD costs
# no extra request -- only extra bytes. It is on by default because Greenhouse is 409 of the 1,173
# boards in SOURCES, the single biggest source, and without it the description rule cannot see a
# third of the corpus. GREENHOUSE_JD=0 turns it off if a host ever objects to the transfer.
#
# MEASURED 2026-08-20 on a random 12-board sample: 0.7 MB -> 15.5 MB for the same 936 jobs,
# extrapolating to roughly 23 MB -> 529 MB across all 409 boards per sweep. That is the whole
# cost, and it buys two things: the description rule gets to vote on every Greenhouse posting,
# and score_jobs stops re-fetching JDs it could have had for free. Verified complete, not
# truncated: stripe returned 567/567 jobs with content (3.04M chars), samsara 264/264 (3.42M).
GREENHOUSE_JD = os.environ.get("GREENHOUSE_JD", "1").strip().lower() not in ("0", "false", "no", "off")

# CANONICAL URLS ALREADY IN THE CORPUS, published by main() before the sweep starts.
#
# A module global rather than a parameter because SCRAPERS dispatches every adapter as fn(url) --
# 31 of them share that signature, and threading a second argument through all of them to serve
# one adapter would be the tail wagging the dog. Empty by default, which is what makes every
# other entry point (dump_titles.py, verify_parsers.py, a bare scrape_greenhouse call) behave
# exactly as it did before: an empty set means "nothing is known", so the full fetch happens.
_KNOWN_URLS = set()


def publish_known_urls(urls):
    """Tell the adapters which postings we already hold, so they can skip work for them.

    Called once by main() with the same canonicalised, lowercased set the keep loop dedupes on,
    so an adapter's notion of "already have it" cannot drift from the writer's.
    """
    _KNOWN_URLS.clear()
    _KNOWN_URLS.update(urls or ())


def _gh_rows(data, want_jd):
    rows = []
    for j in data.get("jobs", []):
        row = {"title": (j.get("title") or "").strip(),
               "url": j.get("absolute_url", ""),
               "location": (j.get("location") or {}).get("name", "")}
        if want_jd:
            # `content` is HTML-escaped HTML -- html_to_text unescapes before parsing, which is
            # why it survives the round trip that a bare BeautifulSoup call would mangle.
            row["jd"] = _listing_jd(j.get("content"))
        d = _posted(j.get("first_published") or j.get("updated_at"))
        if d:
            row["found_date"] = d                # the REAL posting date, not the scrape date
        rows.append(row)
    return rows


def scrape_greenhouse(board_url):
    """Greenhouse via its public board API. TWO-PHASE when we already know what this board holds.

    ?content=true returns every posting's full description from the same endpoint, which is a
    bargain per job and a fortune per sweep: measured 2026-08-20, 0.7 MB -> 15.5 MB on a 12-board
    sample, extrapolating to ~23 MB -> 529 MB across all 409 Greenhouse boards. Greenhouse is the
    single biggest source here, so that cost is paid on a third of the corpus every run.

    Almost all of it was waste. main() banks a listing-supplied description only for postings that
    survive the keep loop -- i.e. NEW ones -- so on a twice-daily scrape the descriptions of every
    posting we already stored were downloaded, parsed and thrown away. The fix is to ask the cheap
    question first: fetch without content, and only pay for content if this board has a posting we
    have not seen. A board with no new postings costs the small request alone; a board with new
    ones costs one extra small request on top of what it cost before.

    Nothing about what reaches the feed changes: core.admits_on_description still sees every NEW
    Greenhouse posting, because those are exactly the boards that get the second fetch.
    """
    url = "https://boards-api.greenhouse.io/v1/boards/%s/jobs" % _slug(board_url)
    if not GREENHOUSE_JD:
        return _gh_rows(_get_json(url), False)
    if not _KNOWN_URLS:
        # No corpus to compare against (a bare call, or a test) -- behave exactly as before.
        return _gh_rows(_get_json(url + "?content=true"), True)

    cheap = _get_json(url)
    rows = _gh_rows(cheap, False)
    if not rows:
        return rows
    if any(canonical_url(r["url"]).lower() not in _KNOWN_URLS for r in rows if r.get("url")):
        return _gh_rows(_get_json(url + "?content=true"), True)
    return rows                                  # every posting already stored: no JD needed


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
        row = {
            "title": (j.get("text") or "").strip(),
            "url": j.get("hostedUrl", ""),
            "location": loc,
            # The same three fields score_jobs.jd_map_for reads off this very response.
            "jd": _listing_jd(j.get("descriptionPlain"),
                              *[(l or {}).get("content") for l in (j.get("lists") or [])],
                              j.get("additionalPlain")),
        }
        try:                                     # createdAt is epoch milliseconds
            row["found_date"] = datetime.datetime.fromtimestamp(
                int(j.get("createdAt")) / 1000).strftime("%Y-%m-%d")
        except Exception:
            pass
        rows.append(row)
    return rows


def scrape_ashby(board_url):
    data = _get_json("https://api.ashbyhq.com/posting-api/job-board/%s" % _slug(board_url))
    rows = []
    for j in data.get("jobs", []):
        if not j.get("isListed", True):
            continue
        row = {"title": (j.get("title") or "").strip(),
               "url": j.get("jobUrl", ""),
               "location": j.get("location") or "",
               # descriptionPlain is already plain; descriptionHtml is the fallback.
               "jd": _listing_jd(j.get("descriptionPlain") or j.get("descriptionHtml"))}
        d = _posted(j.get("publishedAt"))
        if d:
            row["found_date"] = d
        rows.append(row)
    return rows


def scrape_kula(board_url):
    """Kula, via the JSON index its own careers page fetches on load.

    Here because 10x Genomics MOVED off Greenhouse to Kula, which turned the entry in SOURCES
    into a hard 404 -- the board did not go quiet, it stopped existing. That is worth an adapter
    rather than a deletion for two reasons: the board came back BIGGER (5 postings on the old
    Greenhouse token, 31 here), and 10x Genomics is a genuine H-1B filer, which is the whole
    point of the list.

    The page is a client-rendered Next.js app, so there is nothing in the served HTML to parse --
    the postings arrive from /api/internal/ats_job_posts, which answers plain JSON to a plain GET
    with no key and no cookie. `internal` names the caller, not the audience: it is the route the
    public careers page uses.

    One request per board. `items=99` with a meta.pages loop after it, so a tenant larger than
    one page is read rather than silently truncated -- the failure mode a fixed page size hides.
    """
    acct = _slug(board_url)
    rows, page = [], 1
    while True:
        data = _get_json("https://careers.kula.ai/api/internal/ats_job_posts",
                         params={"accountName": acct, "page": page,
                                 "type": "ats_job_post.index", "items": 99})
        batch = data.get("data") or []
        for j in batch:
            # `listed` is Kula's own published flag. `kind` is the audience -- an internal-only
            # requisition is visible to this endpoint and has no business in a public feed, so
            # the test is for "external" rather than against a list of the values seen today.
            if not j.get("listed", True):
                continue
            if "external" not in str(j.get("kind") or "external"):
                continue
            job = j.get("ats_job") or {}
            offices = job.get("offices") or []
            row = {
                "title": (j.get("title") or "").strip(),
                # Trailing slash because that is the form the careers page links, and
                # canonical_url() should see the same string a human would paste.
                "url": "https://careers.kula.ai/%s/%s/" % (acct, j.get("id")),
                "location": ", ".join(
                    x for x in (o.get("location") or o.get("name") or "" for o in offices) if x),
                # The full description ships with the index, so the JD is free here -- the same
                # bargain lever/ashby/jibe get, and score_jobs never has to fetch these.
                "jd": _listing_jd(job.get("job_description")),
            }
            d = _posted(j.get("launch_at"))
            if d:
                row["found_date"] = d
            rows.append(row)
        meta = data.get("meta") or {}
        if page >= int(meta.get("pages") or 1) or not batch:
            return rows
        page += 1


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
            row = {
                "title": (j.get("name") or "").strip(),
                "url": "https://jobs.smartrecruiters.com/%s/%s" % (slug, j.get("id", "")),
                "location": location,
            }
            d = _posted(j.get("releasedDate"))
            if d:
                row["found_date"] = d
            rows.append(row)
        offset += len(batch)
        if not batch or offset >= data.get("totalFound", 0) or offset >= 1000:
            break
    return rows


# WORKDAY_QUERIES was a 38-term list here until 2026-08-19. It has been dead since scrape_workday
# switched to paging the WHOLE board with an empty searchText (see that function): nothing read the
# constant, and the résumé-driven block in main() was still faithfully extending it every run. A
# list that looks like it controls Workday coverage but does not is worse than no list -- the
# 2026-08-01 SWE widening added 17 terms to it in the belief that a SWE role on a Workday tenant
# would otherwise never be fetched, which was already untrue by then. Workday coverage is bounded
# by WORKDAY_MAX_JOBS and the title filter, not by any query list.
#
# Amazon is the one source that IS still query-bounded: see AMAZON_QUERIES.


def _workday_date(posted_on):
    """Workday gives 'Posted 5 Days Ago' / 'Posted Today' -> turn into a date.

    'Posted 30+ Days Ago' is a LOWER BOUND, not an age, and reading the 30 out of it as an exact
    age was the single biggest source of stale jobs in the corpus. The cutoff in main() rejects a
    posting only when `posted < age_cutoff`, and age_cutoff is exactly MAX_AGE_DAYS ago — so a
    posting stamped exactly 30 days old is not "over 30 days" and squeaked through. Measured on a
    full run 2026-08-09: 1,943 of 3,858 newly added rows carried found_date 2026-07-10, precisely
    MAX_AGE_DAYS ago, half of everything the run added. Those postings can be any age at all; '30+'
    is all Workday will say.

    So a trailing '+' means "at least this many days", and the age recorded is n+1 — still a guess,
    but on the correct side of the boundary, which lets the cutoff do what it was written to do.
    Returning '' instead would BACKFIRE: the gate skips a blank date (`if posted and ...`), so
    every one of them would be kept.
    """
    s = (posted_on or "").lower()
    if "today" in s:
        days = 0
    elif "yesterday" in s:
        days = 1
    else:
        m = re.search(r"(\d+)\s*(\+)?", s)
        days = int(m.group(1)) if m else 0
        if m and m.group(2):
            days += 1                      # '30+' -> older than 30, so the cutoff can refuse it
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
# 20 IS THE API's CEILING, not a politeness choice. Measured 2026-08-21 against
# gevernova.wd5 (total 2,148): limit=20 -> HTTP 200 with 20 postings; limit=50, 100 and 200 all
# -> HTTP 400. So a bigger page is not available and the only way to make a big Workday board
# cheaper is to stop fetching its pages one at a time.
WORKDAY_PAGE_LIMIT = 20
WORKDAY_MAX_JOBS = 3000
def _workday_page_workers():
    """Concurrent pages per Workday board, DERIVED from how wide the sweep already is.

    A flat constant was wrong here in a way that only bites in production. The per-host
    semaphore cannot cap this: every Workday tenant is its own subdomain, so page workers
    MULTIPLY against SCRAPE_WORKERS instead of sharing a gate with them. At a flat 4 that is up
    to 24 connections in flight on the cPanel cron (6 workers) and 64 in CI (16) -- against 6
    and 16 before. bin/cron_scrape.sh's own header calls concurrent outbound activity "the shape
    of activity that gets a shared account suspended", and this account has been suspended once.

    Two separate ceilings, and conflating them is what made a flat 4 look fine:

      PER HOST is a politeness question, and SCRAPE_PER_HOST already answers it. A board holds
      ONE slot of that gate while its pages fetch, so page concurrency is the one place in the
      sweep that can exceed it -- capping at SCRAPE_PER_HOST means a single board never hits a
      tenant harder than four sibling boards on that host already would.

      TOTAL is an account question, and it is the shared box that cares. Honest arithmetic: this
      does NOT hold the total where it was, because any page concurrency multiplies. It holds
      cron to 2 (12 in flight, up from 6) and lets CI have 4 (64, up from 16) -- CI runs on a
      GitHub runner, not from the cPanel account's IP, so the suspension risk does not apply
      there. What partly offsets the higher PEAK is a shorter run: the connections-over-time
      integral drops even as the instantaneous count rises.

    2 is most of the win anyway. What is being removed is up to 150 SERIAL round trips, and
    halving that matters far more than the last factor of two.

    A function rather than a module constant because SCRAPE_WORKERS is defined ~3,600 lines
    below this one; reading it at import time would be a NameError.
    """
    env = (os.environ.get("WORKDAY_PAGE_WORKERS") or "").strip()
    if env:                                  # explicit override, for a one-off measurement
        try:
            return max(1, int(float(env)))
        except ValueError:
            pass
    # The floor of 2 is deliberate: SCRAPE_WORKERS // 4 is 1 at cron's 6 workers, which would
    # hand the gentlest configuration none of the win at all -- and cron is the runner that
    # actually keeps this site's feed fresh.
    return max(2, min(int(SCRAPE_PER_HOST), int(SCRAPE_WORKERS) // 4))


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
    seen, rows = set(), []

    def _fetch(offset):
        """One page of postings, or None if it could not be read. Fetch only -- the parse and
        every mutation of `seen`/`rows` happens on the calling thread, which is what makes the
        concurrent branch below need no locking."""
        for attempt in (0, 1):
            try:
                r = SESSION.post(cxs, headers=hdr, timeout=25, data=json.dumps(
                    {"appliedFacets": {}, "limit": WORKDAY_PAGE_LIMIT, "offset": offset,
                     "searchText": ""}))
                if r.status_code == 200:
                    return r.json()
            except Exception:
                pass
            if not attempt:
                # One retry, for the same reason Avature has one: fetching offsets
                # independently means a blip drops that page silently instead of ending the
                # walk, so a short board would look like a complete one.
                time.sleep(random.uniform(0.4, 0.9))
        return None

    def _absorb(body):
        """Postings from one response into `rows`. Returns how many the page carried."""
        jp = (body or {}).get("jobPostings") or []
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
        return len(jp)

    # Page 0 buys the board total, and the total is what makes every other offset a known URL.
    first = _fetch(0)
    if not first:
        return rows
    got = _absorb(first)
    if not got:
        return rows
    total = first.get("total") or 0                      # only the FIRST page reports the real
    #                                                      count; later pages send 0

    if total > got:
        # CONCURRENT, like scrape_avature: offset is stateless -- no cursor, no session -- so
        # once the total is known the remaining pages are independent GETs of known URLs. This
        # is the whole reason Workday was two thirds of the sweep: 20 postings a page is the
        # API's hard ceiling (see WORKDAY_PAGE_LIMIT), so a 2,148-posting tenant is 108 serial
        # round trips of ~0.8s -- about 86 seconds for ONE board, times 234 boards.
        offsets = list(range(got, min(total, WORKDAY_MAX_JOBS), WORKDAY_PAGE_LIMIT))
        if offsets:
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(_workday_page_workers(), len(offsets))) as ex:
                for body in ex.map(_fetch, offsets):
                    if body:
                        _absorb(body)
        if total > WORKDAY_MAX_JOBS:
            # Worse here than elsewhere: we page with an EMPTY search, so the order is the
            # tenant's own and the jobs we never see are an arbitrary slice, not the
            # low-relevance tail.
            note_truncation(board_url, WORKDAY_MAX_JOBS, WORKDAY_MAX_JOBS, total)
        return rows

    # No usable total -- walk it the old way, one page at a time until it runs dry. Kept because
    # `total` is the one field this adapter cannot verify across every tenant template, and a
    # tenant that omits it would otherwise report exactly one page and look healthy.
    offset = got
    while offset < WORKDAY_MAX_JOBS:
        body = _fetch(offset)
        if not body:
            break
        n = _absorb(body)
        if not n:
            break
        offset += n
        time.sleep(random.uniform(0.1, 0.25))
    else:
        note_truncation(board_url, offset, WORKDAY_MAX_JOBS, total)
    return rows


AMAZON_QUERIES = (
    "program manager", "project manager", "project coordinator",
    "program coordinator", "business analyst", "operations manager",
    "data analyst", "implementation",
    # wider net — Amazon's search is query-driven, so new terms = new pages fetched
    "product manager", "supply chain analyst", "operations specialist",
    "product owner", "project planner",
    # "supply chain manager" added 2026-08-12, from a posting that was missing:
    # amazon.jobs/en/jobs/10461857 "Supply Chain Manager, SSD". Measured against the live
    # feed that day, and the numbers are the argument:
    #   "supply chain manager"  178 US hits -> 116 clear the title filter and the years gate
    #   "supply chain analyst"    8 US hits ->   2
    # The analyst term was standing in for this whole family and could not: 8 hits total, and
    # the SSD posting is not among them. It IS #7 of 178 for "manager". Nothing else in this
    # list reaches it either — checked "operations manager" (688 hits) and "program manager"
    # (739), neither returns it at any offset. So the filter never got a vote; the row was
    # never fetched. Costs 2 extra requests per run.
    #
    # THE GENERAL LESSON: Amazon is the one first-party source whose coverage is bounded by
    # this list rather than by the title filter. A job family with no term here is invisible
    # no matter how well it would score, and the symptom is silence rather than an error.
    "supply chain manager",
    # project controls / scheduling / PMO family
    "project controls", "project scheduler", "pmo", "portfolio manager",
    # software engineering (2026-08-01) — Amazon's feed is query-driven too. "software
    # development engineer" is Amazon's own title for SWE (SDE); the rest cover its
    # data/ML/infra ladders.
    "software development engineer", "software engineer", "front end engineer",
    "data engineer", "data scientist", "applied scientist", "machine learning engineer",
    "business intelligence engineer", "systems development engineer",
    "quality assurance engineer", "cloud support engineer",
    # internships / co-ops (OPT-eligible)
    "intern", "internship", "co-op",
    # ---- AUDIT, 2026-08-12 -------------------------------------------------------------------
    # Prompted by the supply-chain-manager miss above: if one family was unreachable, which
    # others were? Every one of the 198 INCLUDE keywords that was not already a query term (167
    # of them) was put through amazon.jobs and scored the way this project judges any keyword:
    # not by rows it RETURNS but by rows it ALONE admits. A posting counted only if it cleared
    # the title filter, the years gate, the US-location gate and the freshness window, AND was
    # not already in the corpus. Ranked GREEDILY by marginal contribution, because raw counts put
    # a dozen synonyms of "project manager" on top while they all return the same jobs.
    #
    # Result: 57 of the 167 would add something; together 968 rows Amazon was serving and we
    # were never asking for. The 28 below are the ones worth >= 10 rows each, which is 859 of
    # those 968 for about 136 extra requests a run. The other 29 average 4 rows apiece and were
    # left out; "computer science" is the shape of what was rejected — 3,434 hits, ten pages, 3
    # new rows.
    #
    # Marginal gain of the top few, for anyone re-tuning: software dev engineer 81, systems
    # engineer 69, sde 67, security engineer 59, data science 57, software quality 51,
    # software development 43, automation engineer 40. "sde" is Amazon's own abbreviation and
    # reaches 67 rows the spelled-out terms do not.
    "software dev engineer", "systems engineer", "sde", "security engineer", "data science",
    "software quality", "software development", "automation engineer", "big data",
    "full stack", "program management", "early career", "business intelligence",
    "computer vision", "product management", "business operations", "software engineering",
    "operations management", "embedded software", "software developer", "test engineer",
    "product operations", "engagement manager", "program lead", "machine learning",
    "infrastructure engineer", "technical program manager", "devops",
)


# Amazon's search.json caps a page at 100 and reports the true total in `hits`, so we can
# page a term to exhaustion. MAX_PER_TERM is only a safety stop, mirroring WORKDAY_MAX_JOBS.
AMAZON_PAGE_LIMIT = 100
AMAZON_MAX_PER_TERM = 1000


def _amazon_row(j, seen):
    """One search.json hit -> a row, or None. Shared by the date sweep and the term walk so the
    experience gate and the date handling cannot diverge between them."""
    jid = j.get("id_icims") or j.get("job_path")
    if not jid or jid in seen:
        return None
    seen.add(jid)
    if core.required_years(j.get("basic_qualifications") or "") > MAX_YEARS:
        return None                              # wants more experience than entry-level
    row = {
        "title": (j.get("title") or "").strip(),
        "url": "https://www.amazon.jobs" + (j.get("job_path") or ""),
        "location": j.get("normalized_location") or j.get("location") or "",
    }
    try:
        # Amazon publishes a REAL posting date ("June 13, 2026"), so store it in the bare ISO
        # shape that marks a date as trustworthy. It used to be written as "%Y-%m-%d %H:%M",
        # which appended " 00:00" and made every one of these look like a derived guess to
        # verify_dates._is_clean_api_date() — 1,315 rows, 18% of the whole verification
        # backlog, queued for a rate-limited lookup that could only ever confirm what we had.
        row["found_date"] = datetime.datetime.strptime(
            j.get("posted_date", ""), "%B %d, %Y").strftime("%Y-%m-%d")
    except Exception:
        pass
    return row


# The date-sorted global sweep. Measured 2026-08-19 against sort=recent, base_query="",
# country=USA: offset 0 is today, 1000 is 8 days back, 3000 is ~23 days and 100% inside
# MAX_AGE_DAYS=30, 5000 is ~41 days and only 36% inside, 9900 is ~3 months. A full run returned
# 4,261 postings in 57s, every one with a real posting date, 1,266 of them past the title and US
# filters. `hits` reports a flat 10000, which is an Elasticsearch result-window cap rather than
# Amazon's true US headcount, so there is nothing to read past it. result_limit maxes at 100 (200
# and 500 both return zero rows).
#
# In practice it reads to the cap, and that is the right answer: Amazon's recency sort is NOT
# monotonic — a re-posted role carries its new date, so fresh rows keep appearing past offset
# 5,000 and the "whole page is stale" test almost never fires. Measured both ways on 2026-08-19:
#   fixed depth 5,000  -> 4,261 rows,  57s, 1,266 past the title + US filters
#   read to the cap    -> 7,998 rows, 107s, 2,451 past the title + US filters
# Nearly double the on-target rows for 50 more seconds of a 26-minute step, on the largest single
# employer in the feed. So the stale-page guard below is a floor, not the plan — it exists so a
# much smaller future corpus does not pay for 100 empty pages.
AMAZON_SWEEP_MAX = 10000                 # their result-window cap; the date test normally stops first


def _amazon_sweep(country, loc, seen):
    """Every US posting inside the freshness window, newest first, with no query list involved.

    This is the fix for the ceiling AMAZON_QUERIES describes below: a keyword list decides what
    Amazon roles we are even allowed to see, and the 2026-08-12 audit of 167 unused INCLUDE terms
    was an attempt to guess our way out of that. An empty base_query with sort=recent removes the
    guessing entirely — and costs ~50 requests instead of the term walk's several hundred.
    """
    # One page of slack past the cutoff before stopping: Amazon's recency sort is not perfectly
    # monotonic (a re-posted role carries its new date), so a single stale page is not the end of
    # the fresh ones.
    cutoff = (datetime.date.today() - datetime.timedelta(days=MAX_AGE_DAYS + 3)).isoformat()
    rows, stale_pages = [], 0
    for offset in range(0, AMAZON_SWEEP_MAX, AMAZON_PAGE_LIMIT):
        try:
            data = _get_json("https://www.amazon.jobs/en/search.json", params={
                "base_query": "", "country": country, "loc_query": loc,
                "result_limit": AMAZON_PAGE_LIMIT, "offset": offset, "sort": "recent"})
        except Exception:
            break                                # partial sweep beats none
        hits = data.get("jobs") or []
        if not hits:
            break
        page = [r for r in (_amazon_row(j, seen) for j in hits) if r]
        rows.extend(page)
        dates = [r["found_date"] for r in page if r.get("found_date")]
        if dates and max(dates) < cutoff:
            stale_pages += 1
            if stale_pages >= 2:
                break                            # two consecutive pages wholly out of the window
        else:
            stale_pages = 0
        if len(hits) < AMAZON_PAGE_LIMIT:
            break
        time.sleep(random.uniform(0.2, 0.5))
    return rows


def scrape_amazon(board_url):
    """Amazon's own portal via its public search.json feed, US-only.

    TWO passes, and the first is the one that matters:

    1. A GLOBAL DATE-SORTED SWEEP (_amazon_sweep). No query terms, newest first, deep enough to
       cover MAX_AGE_DAYS. Amazon used to be the one first-party source whose coverage was bounded
       by a keyword list rather than by the title filter, which meant a role nobody had thought to
       add a term for was invisible however well it matched. It is not bounded that way any more.
    2. The AMAZON_QUERIES term walk, now OFF by default and kept only as a fallback. Once the
       sweep is date-complete inside MAX_AGE_DAYS the term walk can only re-find rows the sweep
       already has, or rows too old for main()'s cutoff to keep — for several hundred requests. It
       still runs automatically if the sweep comes back empty (i.e. Amazon stopped honouring an
       empty base_query), and can be forced with AMAZON_TERM_WALK=1.

    Both pages EVERY result rather than the first two pages. Measured 2026-08-01: "program
    manager" returns 734 US hits and "Program Manager, Relo Ops Excellence (RLOI)" sits at #351,
    invisible to the old 200-result window; across results 201-800 for that one term, 316 more
    titles passed the filter than the 168 the window caught. Same lesson scrape_workday learned.
    """
    from urllib.parse import urlparse, parse_qs
    q = parse_qs(urlparse(board_url).query)
    country = (q.get("country") or ["USA"])[0]
    loc = (q.get("loc_query") or ["United States"])[0]
    seen, rows = set(), []

    rows.extend(_amazon_sweep(country, loc, seen))
    print("   amazon: date sweep -> %d posting(s)" % len(rows))
    if rows and os.environ.get("AMAZON_TERM_WALK") != "1":
        return rows
    if not rows:
        print("   amazon: sweep returned nothing — falling back to the %d-term walk"
              % len(AMAZON_QUERIES))

    for term in AMAZON_QUERIES:
        offset, total = 0, None
        while offset < AMAZON_MAX_PER_TERM:
            data = _get_json("https://www.amazon.jobs/en/search.json", params={
                "base_query": term, "country": country, "loc_query": loc,
                "result_limit": AMAZON_PAGE_LIMIT, "offset": offset, "sort": "relevant"})
            if total is None:
                total = data.get("hits") or 0
            hits = data.get("jobs", [])
            if not hits:
                break
            for j in hits:
                row = _amazon_row(j, seen)       # `seen` also spans the date sweep above
                if row:
                    rows.append(row)
            offset += len(hits)
            if offset >= total:                  # walked the whole result set for this term
                break
            time.sleep(random.uniform(0.3, 0.7))
        else:
            # while-loop ran to AMAZON_MAX_PER_TERM without exhausting the term
            note_truncation("Amazon", offset, AMAZON_MAX_PER_TERM, total,
                            detail="term=%r" % term)
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
                # jd_map_for hits this identical endpoint later for exactly this field.
                "jd": _listing_jd(j.get("description")),
            })
        total = d.get("totalCount") or d.get("count") or 0
        if len(jobs) < 100 or page * 100 >= total:
            break
        page += 1
        time.sleep(random.uniform(0.3, 0.7))
    return rows


# ---- JobSpy: one aggregator query per selector. Config and JOBSPY_BOARDS live up by SOURCES ----


def _jobspy_best_url(row):
    """The employer's own link when the aggregator gives us one, else the aggregator's page.

    The single highest-leverage line in this adapter. A direct URL lands in the SAME namespace
    as the ~1,220 boards we already scrape, so canonical_url() and main()'s `seen` set merge it
    with the row we already hold and it costs nothing. An aggregator URL cannot be merged with
    anything, and it is second-class in our own app besides — web._dupe_rank ranks those hosts
    last and the autoapply queue refuses them outright.

    Guarded twice: the direct link must be a usable http(s) URL, and it must not point straight
    back at an aggregator. Indeed returns indeed.com/applystart?... in that field often enough
    that taking it at face value would quietly undo the whole point.
    """
    direct = (row.get("job_url_direct") or "").strip()
    if direct and is_http_url(direct) and not core.is_aggregator_url(direct):
        return direct
    return (row.get("job_url") or "").strip()


def _text(v):
    """One DataFrame cell as a clean string, treating a missing value as empty.

    pandas fills missing cells with float NaN, and NaN is TRUTHY — so `str(v or "")` sails
    straight past it and yields the literal string "nan". That is not a cosmetic bug: a row
    whose company became "nan" renders as a company called nan, groups with every other such
    row under core.posting_key, and — measured on the first live run — MATCHED the federal
    sponsor data, putting a false "h1b, green_card, 71 approvals" badge on 9 postings.

    `v != v` is true only for NaN, and needs no pandas import to say so.
    """
    if v is None or v != v:
        return ""
    return str(v).strip()


def _jobspy_location(row):
    """JobSpy splits location across city/state/country; the corpus stores one string."""
    loc = row.get("location")
    if isinstance(loc, str):
        return loc.strip()
    parts = [_text(row.get(k)) for k in ("city", "state")]
    return ", ".join(p for p in parts if p)


def scrape_jobspy(board_url):
    """One aggregator query via the python-jobspy library. board_url is a selector,
    'jobspy:<site>|<phrase>|<location>' — e.g. 'jobspy:indeed|project manager|United States'.

    Each row carries its OWN employer (set here, so scrape_all won't clobber it), and its
    description is stashed in JOBSPY_JDS for main() to persist — the aggregator hands us the JD
    for free, and without it these rows would fall to score_jobs' per-URL fetch, which mostly
    403s against indeed.com while spending the scoring budget.

    DORMANT without the library installed, the same contract scrape_adzuna has without a key.
    The import is lazy on purpose: web.py imports this module to serve /add, and the cPanel host
    installs from requirements-cpanel.txt, which does not (and must not) carry pandas.
    """
    try:
        from jobspy import scrape_jobs
    except ImportError:
        print("  note: python-jobspy not installed; jobspy boards skipped")
        return []

    sel = board_url.split(":", 1)[1] if ":" in board_url else board_url
    parts = [p.strip() for p in sel.split("|")]
    site = (parts[0] if parts else "").lower()
    phrase = parts[1] if len(parts) > 1 else ""
    location = parts[2] if len(parts) > 2 else JOBSPY_LOCATION
    if not (site and phrase):
        raise ValueError("bad jobspy selector %r (want jobspy:<site>|<phrase>|<location>)"
                         % board_url)

    JOBSPY_CALLS[0] += 1
    df = scrape_jobs(site_name=[site], search_term=phrase, location=location,
                     results_wanted=JOBSPY_RESULTS, hours_old=JOBSPY_HOURS_OLD,
                     country_indeed="usa", description_format="markdown",
                     linkedin_fetch_description=JOBSPY_LINKEDIN_JD, verbose=0)
    # to_dict immediately: nothing downstream should touch a DataFrame, and the mapping below
    # stays unit-testable without pandas installed.
    records = df.to_dict("records") if df is not None and not df.empty else []
    JOBSPY_ROWS[0] += len(records)

    if not records:
        # A silent zero is NOT the same as a quiet day, and the two look identical in the log
        # otherwise. Google has returned 0 and ZipRecruiter 403 since Sept 2025 (JobSpy #302),
        # and datacenter IPs get refused where a laptop is served — say so explicitly.
        print("  note: jobspy %s returned 0 rows for '%s' (blocked, or genuinely nothing new?)"
              % (site, phrase))
        return []

    rows, seen = [], set()
    for r in records:
        url = _jobspy_best_url(r)
        if not url or not is_http_url(url):
            continue
        canon = canonical_url(url)
        if canon in seen:                 # same posting twice within one query
            continue
        seen.add(canon)
        company = _text(r.get("company"))
        if not company:
            # No employer name means no sponsor lookup, no company page, and a card that reads
            # blank. Cheaper to drop it here than to store a row nothing downstream can use.
            continue
        row = {"title": _text(r.get("title")),
               "url": url,
               "company": company,
               "location": _jobspy_location(r),
               # Tags the row's origin so main() can apply the sponsor-record gate to THESE rows
               # and not to the direct boards. Dropped on the way to storage — FIELDNAMES does
               # not list it — so it never reaches the table.
               "_src": "jobspy"}
        posted = _text(r.get("date_posted"))
        if posted:
            # A missing date is fine and deliberate: the row is kept and ages by first_seen,
            # exactly as every dateless board's rows do.
            row["found_date"] = posted[:10]
        rows.append(row)
        jd = _text(r.get("description"))
        if jd:
            JOBSPY_JDS[canon] = jd

    if len(records) >= JOBSPY_RESULTS:
        note_truncation("jobspy:%s|%s" % (site, phrase), len(rows), JOBSPY_RESULTS,
                        detail="(results_wanted ceiling)")
    return rows


# ---- Meta (metacareers.com): no public feed, so drive a headless browser ----
# Meta's careers site is a Facebook Relay/GraphQL app: a plain HTTP request gets a 400,
# the jobs aren't in the page HTML, and the job-search query needs a CSRF token + a
# doc_id that rotates on every Meta deploy. Rather than reverse-engineer (and constantly
# re-fix) that, we load the page in a headless browser and CAPTURE the GraphQL response
# it fires on load — which returns the whole default board (~500 postings, US + intl) in
# ONE payload. main()'s title + US filter then trims it down. Needs Playwright — the same
# optional dep the Workday boards used to need:
#     pip install playwright && playwright install chromium
METACAREERS_JOBS_URL = "https://www.metacareers.com/jobs/"


def scrape_metacareers(board_url):
    """Meta's own careers site via headless-browser GraphQL capture (Meta has no public
    job API). board_url is the metacareers jobs page; returns [{title,url,location}, ...].
    NOTE: this grabs Meta's DEFAULT job payload (~500 roles), not its full multi-thousand
    catalogue — that's plenty for the entry-level PM/analyst/ops titles the filter keeps."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError(
            "Meta needs a browser. Install it once:\n"
            "    pip install playwright && playwright install chromium")
    target = board_url if "metacareers.com" in (board_url or "") else METACAREERS_JOBS_URL
    payloads = []

    def _grab(resp):
        # Read the body of every careers GraphQL POST; keep the one carrying the job list.
        try:
            if "/graphql" in resp.url and resp.request.method == "POST":
                body = resp.text()
                if "all_jobs" in body or "job_search_with_featured_jobs" in body:
                    payloads.append(body)
        except Exception:
            pass

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=HEADERS["User-Agent"])
            page.on("response", _grab)
            page.goto(target, wait_until="domcontentloaded", timeout=45000)
            for _ in range(60):                 # poll up to ~30s for the jobs payload
                if payloads:
                    break
                page.wait_for_timeout(500)
        finally:
            browser.close()

    rows, seen = [], set()
    for body in payloads:
        for line in body.splitlines():          # Meta can stream >1 JSON object (@defer)
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            block = (d.get("data") or {}).get("job_search_with_featured_jobs") or {}
            jobs = (block.get("all_jobs") or []) + (block.get("featured_jobs") or [])
            for j in jobs:
                jid = str(j.get("id") or "")
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                locs = [str(x).strip() for x in (j.get("locations") or []) if x]
                # Many Meta roles are multi-city; surface a US location when there is one
                # so the US filter keeps the role, else show them all and let it drop.
                loc = next((l for l in locs if is_us_location(l)), "; ".join(locs))
                rows.append({
                    "title": (j.get("title") or "").strip(),
                    "url": "https://www.metacareers.com/jobs/%s/" % jid,
                    "location": loc,
                })
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
                     "found_date": (o.get("published_at") or "")[:10],
                     # Free, like lever/ashby/jibe: both fields are already in this response.
                     # Measured 2026-08-20 on grantthornton -- 83/83 offers carried >400 chars.
                     "jd": _listing_jd(o.get("description"), o.get("requirements"))})
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
            # strict=False: a raw newline inside a JSON string is illegal, and sites that
            # paste an HTML description into their JSON-LD emit them constantly. Strict
            # parsing drops the whole block over it; browsers don't, and neither should we.
            data = json.loads(tag.string or "", strict=False)
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
            # found_date is OMITTED when the page states none, never set to "".
            # main() fills it with the scrape stamp via setdefault, which a present-but-empty
            # key silently defeats -- and db.add_jobs then strips the empty string, so the row
            # lands with found_date NULL and renders with no date at all. Every jsonld board
            # was affected; the 38 Work at a Startup rows are what made it visible.
            row = {"title": (it.get("title") or "").strip(),
                   "url": it.get("url") or board_url,
                   "location": loc}
            posted = (str(it.get("datePosted") or ""))[:10]
            if posted:
                row["found_date"] = posted
            rows.append(row)
    # de-dupe by url; keep only http(s) links (a malicious page could embed a
    # "url": "javascript:..." in its JobPosting JSON, which we'd later render as a link)
    seen, out = set(), []
    for r in rows:
        if r["title"] and is_http_url(r["url"]) and r["url"] not in seen:
            seen.add(r["url"]); out.append(r)
    return out


# ---- Y Combinator's Work at a Startup ------------------------------------------------------
# WHY THIS EXISTS. The corpus held 38 workatastartup.com rows, all filed under the single
# company "Y Combinator's Work at a Startup" rather than the startups actually hiring, all with
# NULL location and NULL date, half of them already 404, and not one of them clearing the match
# floor. They were a one-off import by the generic jsonld scraper on 2026-08-02 and were never
# swept again -- there has never been a Work at a Startup entry in SOURCES or in SCRAPERS.
#
# The site is an Inertia.js app: no server-rendered markup to parse, but the whole page payload
# ships in a single `data-page` attribute, which is far better than HTML. Each job arrives with
# companyName, location, salary and the batch, so rows land with the STARTUP as the employer.
# score_jobs.workatastartup_detail_jd already reads the same attribute on the job page.
#
# PAGINATION IS BY ROLE, not by page number. `?page=2` returns byte-identical ids, and the
# unfacetted /jobs is just the engineering facet -- measured: /jobs and /jobs/l/software-engineer
# return the same 29 ids, while /jobs/l/sales-manager returns 26 with zero overlap. So the ten
# role paths the page advertises in props.roleLinks ARE the pagination, and walking them is how
# you see the whole board.
WORKATASTARTUP_ROLES = ("software-engineer", "designer", "recruiting", "science",
                        "product-manager", "operations", "sales-manager", "marketing",
                        "legal", "finance")
_WAAS_PAGE_RE = re.compile(r'data-page="([^"]+)"')


def _waas_jobs(url):
    """The `jobs` array out of one Work at a Startup page, or [] if the shape moved."""
    try:
        r = _safe_get(url, timeout=25)
        if r.status_code != 200:
            return []
        m = _WAAS_PAGE_RE.search(r.text)
        if not m:
            return []
        return (json.loads(html.unescape(m.group(1))).get("props") or {}).get("jobs") or []
    except Exception:
        return []


def scrape_workatastartup(board_url):
    """Every current Work at a Startup posting, one row per job, employer = the startup.

    DELIBERATELY DOES NOT SET found_date. main() stamps the scrape date via setdefault, and a
    scraper that sets the key to "" defeats that -- db.add_jobs strips empty strings, so the
    row lands with found_date NULL. That is the bug the old jsonld rows carry, and it is why
    every one of them shows no date at all.
    """
    base = (board_url or "https://www.workatastartup.com/jobs").rstrip("/")
    root = base.rsplit("/jobs", 1)[0] or "https://www.workatastartup.com"
    seen, out = set(), []
    for role in ("",) + WORKATASTARTUP_ROLES:
        for j in _waas_jobs(base if not role else "%s/jobs/l/%s" % (root, role)):
            jid = j.get("id")
            company = (j.get("companyName") or "").strip()
            title = (j.get("title") or "").strip()
            if not (jid and title and company) or jid in seen:
                continue
            seen.add(jid)
            out.append({
                "title": title,
                # The canonical public page, NOT applyUrl -- that is an account.ycombinator.com
                # sign-up redirect, which is neither stable nor readable, and it is this form
                # that score_jobs.workatastartup_detail_jd knows how to fetch a description from.
                "url": "%s/jobs/%s" % (root, jid),
                "company": company,
                "location": (j.get("location") or "").strip(),
            })
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


# Phenom results come back "Most recent" first, so even when a giant tenant exceeds
# the cap we only lose its OLDEST tail (Actalent: 5.2k postings — cap audited 2026-06-11).
PHENOM_MAX_JOBS = 6000


def scrape_phenom(board_url):
    """Phenom People career sites (careers.<company>.com / jobs.<company>.com) via the
    public POST /widgets JSON their own search uses. board_url is the careers origin.
    NOTE: many Phenom tenants are a front-end for Workday — their applyUrl points at
    myworkdayjobs — and detect_phenom() returns the Workday board instead in that case
    (better data + JD support). This scraper is for tenants that are Phenom-native."""
    p = urlparse(board_url)
    base = "%s://%s" % (p.scheme or "https", p.netloc)
    rows, seen, offset, total = [], set(), 0, None
    while offset < PHENOM_MAX_JOBS:
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
    else:
        note_truncation(board_url, offset, PHENOM_MAX_JOBS, total)
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
    else:
        note_truncation(board_url, offset, ORACLE_MAX_JOBS, total)
    return rows


# ---- Workable — apply.workable.com/{slug} ----
def _workable_slug(board_url):
    p = urlparse(board_url)
    host = p.netloc.lower()
    segs = [s for s in p.path.split("/") if s]
    if host.endswith(".workable.com") and host not in ("apply.workable.com", "www.workable.com"):
        return host.split(".")[0]                     # {slug}.workable.com vanity host
    return segs[0] if segs else ""                    # apply.workable.com/{slug}


# 10 postings per page. Raised from 30 pages (300) on 2026-08-01 — a hard cap that low was
# silently clipping any mid-size tenant, and Workable pages are cheap.
WORKABLE_MAX_PAGES = 120


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
    for _page in range(WORKABLE_MAX_PAGES):           # 10/page
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
    else:
        note_truncation(board_url, len(seen), WORKABLE_MAX_PAGES * 10)
    return rows


# ---- UKG Pro Recruiting (UltiPro) — recruiting[N].ultipro.com/{CO}/JobBoard/{guid} ----
def _ultipro_base(board_url):
    """Normalize to https://recruiting{N}.ultipro.com/{COMPANY}/JobBoard/{guid} (the two
    path segments every UKG board URL starts with; anything after the guid is UI state).
    UKG spreads tenants across numbered hosts (recruiting, recruiting2, …), so keep the
    host's digits — Starkey lives on recruiting2, and pinning to bare 'recruiting' 404s."""
    m = re.search(r"(https://recruiting\d*\.ultipro\.com/[A-Za-z0-9_-]+/JobBoard/"
                  r"[0-9a-fA-F-]{36})", (board_url or ""), re.I)
    return m.group(1) if m else ""


ULTIPRO_PAGE = 50
ULTIPRO_MAX_JOBS = 2000


def _ultipro_body(top, skip):
    """The LoadSearchResults POST body the board's own search sends (newest first)."""
    return {"opportunitySearch": {"Top": top, "Skip": skip, "QueryString": "",
                                  "OrderBy": [{"Value": "postedDateUtc",
                                               "PropertyName": "PostedDate",
                                               "Ascending": False}],
                                  "Filters": []},
            "matchCriteria": {"PreferredJobs": [], "Educations": [],
                              "LicenseAndCertifications": [], "Skills": [],
                              "hasNoLicenses": False, "SkippedSkills": []}}


def scrape_ultipro(board_url):
    """UKG Pro Recruiting (UltiPro) job boards via the public LoadSearchResults JSON
    POST. Lots of mid-size US employers. The list rows carry a BriefDescription;
    the full JD lives in the OpportunityDetail page (score_jobs handles that)."""
    base = _ultipro_base(board_url)
    if not base:
        return []
    rows, seen, skip = [], set(), 0
    while skip < ULTIPRO_MAX_JOBS:
        try:
            r = _safe_post(base + "/JobBoardView/LoadSearchResults",
                           _ultipro_body(ULTIPRO_PAGE, skip), timeout=25)
        except ValueError:
            break                                     # non-public host -> refuse (SSRF guard)
        if r.status_code != 200 or "json" not in r.headers.get("content-type", "").lower():
            break
        d = r.json() or {}
        opps = d.get("opportunities") or []
        if not opps:
            break
        for o in opps:
            oid = str(o.get("Id") or "")
            if not oid or oid in seen:
                continue
            seen.add(oid)

            def _code(v):                             # State/Country come as dicts OR strings
                return (v.get("Code") or v.get("Name") or "") if isinstance(v, dict) else (v or "")
            locs = []
            for L in (o.get("Locations") or []):
                ad = L.get("Address") or {}
                loc = ", ".join(str(x) for x in
                                (ad.get("City"), _code(ad.get("State")),
                                 _code(ad.get("Country"))) if x)
                if loc:
                    locs.append(loc)
            rows.append({"title": (o.get("Title") or "").strip(),
                         "url": "%s/OpportunityDetail?opportunityId=%s" % (base, oid),
                         "location": "; ".join(locs),
                         "found_date": (str(o.get("PostedDate") or ""))[:10]})
        skip += len(opps)
        total = d.get("totalCount") or 0
        if total and skip >= total:
            break
        time.sleep(random.uniform(0.2, 0.5))
    else:
        note_truncation(board_url, skip, ULTIPRO_MAX_JOBS, total)
    return rows


# ---- SAP SuccessFactors Career Site Builder (jobs.<co>.com style sites) ----
CSB_MAX_ROWS = 2000
# The sitemap fallback needs a MUCH tighter cap than the table path, because the two cost
# wildly different amounts for the same number of rows: the results table returns 25 rows
# per request (2,000 rows = 80 requests), while the sitemap has to open one page PER
# POSTING (2,000 rows = 2,000 requests, plus a politeness sleep between each). At ~0.5s a
# page that is a board that occupies a worker for twenty-plus minutes on its own — long
# enough to outlast the scrape's whole budget, since a board already in flight is left to
# finish. These sites are also the rare ones, so the cap costs coverage on few boards.
CSB_SITEMAP_MAX_ROWS = 400
CSB_SITEMAP_MAX_SEC = 180              # ...and stop after this long regardless


def _csb_date(s):
    """CSB job dates look like 'Jun 11, 2026' -> '2026-06-11' (best-effort)."""
    try:
        return datetime.datetime.strptime((s or "").strip(), "%b %d, %Y").strftime("%Y-%m-%d")
    except Exception:
        return ""


def _csb_is_us(loc):
    """CSB locations always carry an ISO country code: 'Lincoln, NE, US' /
    'Walldorf, DE, 69190' / 'Bangalore, KA, IN, 562149'. A literal US token = US;
    any other alpha-2 code = foreign. Decided HERE because the generic US filter
    would read a German 'DE' or Indian 'IN' as Delaware/Indiana. One exception:
    a 'City, ST' shape (state abbr as the LAST token, no zip after) stays US.

    ...and that exception is why the city list is consulted FIRST. Not every CSB tenant writes
    the country token: Capgemini's board emits 'Casablanca, MA' and 'Buenos Aires, AR' with
    nothing after the code, which is byte-for-byte the 'City, ST' shape the exception exists to
    keep — so Morocco read as Massachusetts and Argentina as Arkansas, and 157 of that board's
    516 supposedly-US rows (30%) were foreign. A named city outranks an ambiguous code."""
    if _NON_US_RE.search(_fold(loc)):
        return False
    toks = [t.strip().upper() for t in (loc or "").split(",") if t.strip()]
    if "US" in toks or "USA" in toks:
        return True
    if toks and toks[-1] in US_STATE_ABBR and len(toks[-1]) == 2:
        return True
    return not any(len(t) == 2 and t.isalpha() for t in toks)   # no code at all -> unknown, keep


# Newer SuccessFactors / jobs2web (RMK) career sites render their results CLIENT-SIDE
# (no server tr.data-row), so the table path below comes back empty. They still publish a
# full sitemap.xml of /job/<City-Title-CODE-zip>/<id>/ URLs, and each posting page is plain
# server HTML. _csb_sitemap_rows() is the fallback: enumerate the sitemap and parse each page.
# US slugs carry a 2-letter US-state code + ZIP near the end (e.g. ...-TX-77056); other
# countries don't (France uses an -FH-/-HF- gender marker, etc.), so when US_ONLY we pre-filter
# on that pattern to avoid fetching the whole GLOBAL board just to drop most of it.
_CSB_US_SLUG = re.compile(r"-([A-Z]{2})-\d{4,6}\b")
# ...but a SuccessFactors tenant hiring across many countries labels the slug with an
# ISO-3166 COUNTRY code instead of a US state, so the pattern above matches none of its US
# postings. Wipro is the case that found this: 4,077 jobs in its sitemap, 402 tagged with a
# USA token (e.g. Plano-...-USA-75024), and the state-code filter admitted exactly zero -- so
# the board read as empty and the employer as unscrapeable. Its India rows carry IND, which
# this still correctly excludes.
_CSB_US_COUNTRY = re.compile(r"-(?:USA|US)(?:-[0-9]+)?/", re.I)


def _csb_slug_is_us(u):
    """True when a SuccessFactors /job/ slug looks like a US posting.

    One predicate, two callers: the sitemap row builder and probe_board's count. They used to
    disagree -- probe_board only ever read the server-rendered /search/ table, so every
    client-rendered tenant counted as None and could never be adopted, even when the scraper
    could read it perfectly well through the sitemap."""
    m = _CSB_US_SLUG.search(u)
    if m and m.group(1).upper() in US_STATE_ABBR:
        return True
    return bool(_CSB_US_COUNTRY.search(u))


def _csb_sitemap_us_locs(base):
    """The US-eligible /job/ URLs in a CSB sitemap. One request, no per-posting fetch."""
    try:
        r = _safe_get(base + "/sitemap.xml", timeout=30)
    except Exception:
        return []
    if r.status_code != 200:
        return []
    locs = [u for u in re.findall(r"<loc>([^<]+)</loc>", r.text) if "/job/" in u]
    if not US_ONLY:
        return locs
    return [u for u in locs if _csb_slug_is_us(unquote(u))]


def _csb_sitemap_rows(base):
    try:
        r = _safe_get(base + "/sitemap.xml", timeout=30)
    except Exception:
        return []                                     # non-public host (SSRF guard) or unreachable
    if r.status_code != 200:
        return []
    locs = [u for u in re.findall(r"<loc>([^<]+)</loc>", r.text) if "/job/" in u]
    if US_ONLY:
        cands = [u for u in locs if _csb_slug_is_us(unquote(u))]
    else:
        cands = locs
    rows = []
    window = cands[:CSB_SITEMAP_MAX_ROWS]
    stop_at = time.monotonic() + CSB_SITEMAP_MAX_SEC
    for u in window:
        if time.monotonic() >= stop_at:
            note_truncation(base, len(rows), CSB_SITEMAP_MAX_ROWS, len(cands),
                            detail="sitemap crawl hit its %ds time cap" % CSB_SITEMAP_MAX_SEC)
            return rows
        try:
            jr = _safe_get(u, timeout=20)
        except Exception:
            continue
        if jr.status_code != 200:
            continue
        soup = BeautifulSoup(jr.text, "lxml")
        raw = soup.title.get_text(strip=True) if soup.title else ""
        # The English "… Job Details |" header confirms a US/English posting (foreign-language
        # pages title it differently, e.g. French "… Détails du poste |") — a cheap second guard.
        if "Job Details" not in raw:
            continue
        title = re.split(r"\s+Job Details", raw)[0].strip()
        if not title:
            continue
        block = soup.select_one("[class*=job]")
        text = block.get_text(" ", strip=True) if block else ""
        loc = ""
        if block:                                     # the page prints "City, United States, ZIP"
            for line in block.get_text("\n", strip=True).split("\n"):
                m = re.match(r"(.+?),\s*United States\b", line)
                if m and len(line) < 80:
                    loc = "%s, United States" % m.group(1).strip().title()
                    break
        if not loc:                                   # fall back to the slug's city + state code
            seg = unquote(u).split("/job/")[1].split("/")[0]
            st = _CSB_US_SLUG.search(seg)
            loc = ("%s, %s, US" % (seg.split("-")[0].title(), st.group(1)) if st
                   else seg.split("-")[0].title() + ", US")
        row = {"title": title, "url": u, "location": loc}
        md = re.search(r"Posting Start Date:\s*(\d{1,2})/(\d{1,2})/(\d{2,4})", text)
        if md:
            mo, da, yr = md.groups()
            yr = int(yr) + (2000 if int(yr) < 100 else 0)
            try:
                row["found_date"] = datetime.date(yr, int(mo), int(da)).strftime("%Y-%m-%d")
            except ValueError:
                pass
        rows.append(row)
        time.sleep(random.uniform(0.1, 0.3))
    if len(cands) > len(window):        # the row cap, not the clock, is what stopped us
        note_truncation(base, len(rows), CSB_SITEMAP_MAX_ROWS, len(cands),
                        detail="sitemap crawl (one request per posting)")
    return rows


def scrape_successfactors(board_url):
    """SAP SuccessFactors 'Career Site Builder' career sites — the classic
    jobs.<company>.com sites with /search/?q= and /job/<City>-<Title>-<id>/ URLs.
    Server-rendered HTML results table (tr.data-row), 25 rows per page, paged with
    &startrow=N. Unlocks employers (SAP, NTT DATA, many industrials/pharma) whose
    SuccessFactors backend has no public JSON API. Newer jobs2web/RMK sites render
    results client-side (no tr.data-row) — when the table path comes up empty we fall
    back to _csb_sitemap_rows() (sitemap.xml + per-posting parse; e.g. ENGIE)."""
    p = urlparse(board_url)
    base = "%s://%s" % (p.scheme or "https", p.netloc)
    rows, seen, startrow, total = [], set(), 0, None
    while startrow < CSB_MAX_ROWS:
        try:
            r = _safe_get("%s/search/?q=&sortColumn=referencedate&sortDirection=desc"
                          "&startrow=%d" % (base, startrow), timeout=25)
        except ValueError:
            break                                     # non-public host -> refuse (SSRF guard)
        if r.status_code != 200:
            break
        soup = BeautifulSoup(r.text, "lxml")
        trs = soup.select("tr.data-row")
        if not trs:
            break
        if total is None:
            m = re.search(r"Results\s+\d+\s*\S{0,3}\s*\d+\s+of\s+([\d,]+)", r.text)
            total = int(m.group(1).replace(",", "")) if m else 0
        added = 0
        for tr in trs:
            a = tr.select_one("a.jobTitle-link")
            href = a.get("href") if a else None
            if not a or not href:
                continue
            url = urljoin(base, href)
            if url in seen:
                continue
            seen.add(url)
            added += 1
            locel = tr.select_one(".jobLocation")
            dateel = tr.select_one(".jobDate")
            loc = locel.get_text(strip=True) if locel else ""
            if US_ONLY and not _csb_is_us(loc):
                continue
            rows.append({"title": a.get_text(strip=True), "url": url,
                         "location": loc,
                         "found_date": _csb_date(dateel.get_text(strip=True) if dateel else "")})
        if not added:                                 # a page of pure repeats = the end
            break
        startrow += len(trs)
        if total and startrow >= total:
            break
        time.sleep(random.uniform(0.2, 0.5))
    if not rows:                                       # client-rendered SF -> sitemap fallback
        rows = _csb_sitemap_rows(base)
    return rows


def scrape_bamboohr(board_url):
    """BambooHR hosted careers: https://{slug}.bamboohr.com/careers/list (public JSON).
    Needs the browser UA — the default python one gets a 403. Small-company ATS;
    full JD per job at /careers/{id}/detail (score_jobs fetches it)."""
    slug = _sub(board_url)
    d = _get_json("https://%s.bamboohr.com/careers/list" % slug)
    rows = []
    for j in (d.get("result") or []):
        jid = str(j.get("id") or "")
        if not jid:
            continue
        loc = j.get("location") or {}
        loc_s = (", ".join(str(x) for x in (loc.get("city"), loc.get("state")) if x)
                 if isinstance(loc, dict) else str(loc))
        if j.get("isRemote"):
            loc_s = (loc_s + " (Remote)").strip()
        rows.append({"title": (j.get("jobOpeningName") or "").strip(),
                     "url": "https://%s.bamboohr.com/careers/%s" % (slug, jid),
                     "location": loc_s})
    return [r for r in rows if r["title"]]


def scrape_pinpoint(board_url):
    """Pinpoint: https://{slug}.pinpointhq.com/postings.json — the feed carries the
    FULL description/responsibilities/skills inline (score_jobs reuses them as JDs)."""
    d = _get_json("https://%s.pinpointhq.com/postings.json" % _sub(board_url))
    rows = []
    for j in (d.get("data") or []):
        loc = j.get("location") or {}
        loc_s = ", ".join(str(x) for x in
                          ((loc.get("city") or loc.get("name")), loc.get("province")) if x)
        if (j.get("workplace_type") or "") == "remote":
            loc_s = (loc_s + " (Remote)").strip()
        row = {"title": (j.get("title") or "").strip(),
               "url": j.get("url") or "", "location": loc_s,
               # The docstring above already promised these are inline; now the sweep uses them.
               "jd": _listing_jd(j.get("description"), j.get("key_responsibilities"),
                                 j.get("skills_knowledge_expertise"))}
        d = _posted(j.get("created_at"))             # only set when real (else main() stamps)
        if d:
            row["found_date"] = d
        rows.append(row)
    return [r for r in rows if r["title"] and r["url"]]


# ---- Rippling ATS — ats.rippling.com/{slug}/jobs (server-rendered Next.js) ----
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>', re.S)


def _rippling_jobposts(page_html):
    """(items, totalPages) from the react-query cache embedded in a board page."""
    m = _NEXT_DATA_RE.search(page_html or "")
    if not m:
        return [], 0
    try:
        d = json.loads(m.group(1))
    except Exception:
        return [], 0
    for q in ((d.get("props", {}).get("pageProps", {}).get("dehydratedState") or {})
              .get("queries") or []):
        if "job-posts" in json.dumps(q.get("queryKey") or []):
            data = (q.get("state") or {}).get("data") or {}
            return (data.get("items") or []), (data.get("totalPages") or 0)
    return [], 0


# 20 postings per page. Raised from 25 pages (500) on 2026-08-01 alongside the other caps.
RIPPLING_MAX_PAGES = 100


def scrape_rippling(board_url):
    """Rippling ATS boards. No public JSON API, but the board page is server-rendered
    Next.js — each page's job list (20/page) rides in its __NEXT_DATA__ blob."""
    m = re.search(r"ats\.rippling\.com/([^/?#]+)", board_url or "")
    if not m:
        return []
    slug = m.group(1)
    rows, seen = [], set()
    for page in range(RIPPLING_MAX_PAGES):            # 20/page
        try:
            r = _safe_get("https://ats.rippling.com/%s/jobs?page=%d" % (slug, page),
                          timeout=20)
        except ValueError:
            break
        if r.status_code != 200:
            break
        items, total_pages = _rippling_jobposts(r.text)
        if not items:
            break
        for it in items:
            url = it.get("url") or ""
            if not url or url in seen:
                continue
            seen.add(url)
            locs = []
            for L in (it.get("locations") or []):
                loc = ", ".join(str(x) for x in
                                (L.get("city"), L.get("state"), L.get("country")) if x)
                if (L.get("workplaceType") or "") == "REMOTE":
                    loc = (loc + " (Remote)").strip().strip(",").strip()
                if loc:
                    locs.append(loc)
            rows.append({"title": (it.get("name") or "").strip(), "url": url,
                         "location": "; ".join(locs)})
        if page + 1 >= (total_pages or 1):
            break
        time.sleep(random.uniform(0.2, 0.5))
    else:
        note_truncation(board_url, len(seen), RIPPLING_MAX_PAGES * 20,
                        detail="(totalPages=%s)" % total_pages)
    return rows


# ---- JobDiva candidate portals — www1.jobdiva.com/portal/?a=<token> ----
# Public REST API at ws.jobdiva.com/candPortal/rest/, the exact flow the portal's own JS uses:
#   GET auth/a   (Basic axelon:axelon + the ?a=<token> as an 'a' header) -> {portalID, token}
#   GET job/listall?portaltype=1&count=N   (N<=200)                      -> {total, data:[...]}
#   GET job/getmore?from=&to=&count=                                     -> the next slice
# Each job row carries the FULL jobDescription inline (so score_jobs scores it with no detail
# calls — see jd_map_for). These portals are usually STAFFING AGENCIES, so the per-job company
# is the END CLIENT (often "Confidential"); we surface a real client name when one is given.
JOBDIVA_API = "https://ws.jobdiva.com/candPortal/rest/"
JOBDIVA_BASIC = "Basic YXhlbG9uOmF4ZWxvbg=="      # axelon:axelon — a constant baked into the portal
JOBDIVA_PAGE = 200                                # the API rejects count > 200
JOBDIVA_MAX_JOBS = 4000


def _jobdiva_token(board_url):
    """The portal token from ?a=<token> (it scopes the feed to one agency's jobs)."""
    return (parse_qs(urlparse(board_url).query).get("a") or [""])[0]


def _jobdiva_session(token):
    """Exchange a portal token for the {portalID, token, a} headers the job calls need,
    or None if the handshake fails."""
    ah = dict(HEADERS)
    ah.update({"Authorization": JOBDIVA_BASIC, "portalID": "1", "a": token, "compid": "-1"})
    try:
        aj = SESSION.get(JOBDIVA_API + "auth/a", headers=ah, timeout=25).json()
    except Exception:
        return None
    if not aj.get("token") or not aj.get("portalID"):
        return None
    jh = dict(HEADERS)
    jh.update({"portalID": str(aj["portalID"]), "token": aj["token"], "a": aj.get("a") or token})
    return jh


def _jobdiva_pages(token, jh):
    """Yield each page's job-record list, walking job/listall then job/getmore until the
    reported total is covered. Shared by the scraper and score_jobs' JD bulk-fetch."""
    frm, total = 0, None
    while frm < JOBDIVA_MAX_JOBS:
        url = (JOBDIVA_API + "job/listall?portaltype=1&count=%d" % JOBDIVA_PAGE if frm == 0
               else JOBDIVA_API + "job/getmore?from=%d&to=%d&count=%d"
                    % (frm, frm + JOBDIVA_PAGE, JOBDIVA_PAGE))
        try:
            r = SESSION.get(url, headers=jh, timeout=30)
            if r.status_code != 200:
                break
            d = r.json()
        except Exception:
            break
        data = d.get("data") if isinstance(d, dict) else d
        if not data:
            break
        if total is None and isinstance(d, dict):
            total = d.get("total") or 0
        yield data
        frm += JOBDIVA_PAGE
        if total and frm >= total:
            break
        time.sleep(random.uniform(0.15, 0.35))
    else:
        note_truncation("jobdiva:%s" % str(token)[:20], frm, JOBDIVA_MAX_JOBS, total)


def jobdiva_job_detail(jid, jh):
    """One JobDiva posting's FULL description HTML, or "" — job/getdetailbyjobid/<id>.

    The listall/getmore feed that scrape_jobdiva walks truncates jobDescription at 400
    characters and appends "...", which is how 352 rows came to hold either a 63-character
    "You need to enable JavaScript to run this app." shell or a teaser cut off mid-word.
    Measured on the eTeam portal: 196 of 200 feed rows were EXACTLY 403 characters, while
    this endpoint returns 561-3,366 (median 2,076) for the same postings.

    403 is the dangerous number — three characters above core._MIN_JD_CHARS, so a truncated
    teaser does not read as thin and nothing would ever retry it. Hence the full text is
    fetched per job rather than taken from the feed.

    The path form matters: getdetailbyjobid takes the id as a PATH SEGMENT (?jobid= 404s),
    and the description is under the "job" key, not "data". Both were found by reading the
    portal's own index_bundle.js. 404 means the posting has closed.
    """
    if not (jid and jh):
        return ""
    try:
        r = SESSION.get(JOBDIVA_API + "job/getdetailbyjobid/%s?compid=" % jid,
                        headers=jh, timeout=20)
        if r.status_code != 200:
            return ""
        return ((r.json() or {}).get("job") or {}).get("jobDescription") or ""
    except Exception:
        return ""


def scrape_jobdiva(board_url):
    """JobDiva candidate portal via its public REST API. board_url is the portal link
    (…/portal/?a=<token>). Per-job company is the end client (often 'Confidential' on an
    agency portal) — we surface a named client when given, else scrape_all's company applies."""
    token = _jobdiva_token(board_url)
    if not token:
        return []
    jh = _jobdiva_session(token)
    if not jh:
        return []
    rows, seen = [], set()
    for data in _jobdiva_pages(token, jh):
        for j in data:
            jid = j.get("id")
            if jid is None or jid in seen:
                continue
            seen.add(jid)
            row = {"title": (j.get("title") or "").strip(),
                   "url": "https://www1.jobdiva.com/portal/?a=%s#/jobs/%s" % (token, jid),
                   "location": (j.get("location") or j.get("mainLocation") or "").strip()}
            client = (j.get("company") or "").strip()
            if client and client.lower() != "confidential":
                row["company"] = client                   # surface the real end-client when named
            pd = j.get("postDate")
            if pd:
                try:                                       # postDate is epoch milliseconds
                    row["found_date"] = datetime.datetime.fromtimestamp(
                        int(pd) / 1000).strftime("%Y-%m-%d")
                except Exception:
                    pass
            rows.append(row)
    return rows


def _jobdiva_agency(token):
    """Best-effort agency name for a portal token, decoded from auth/a's basic-auth blob
    ('eTeamUS:…' -> 'eTeam'). Only used to SUGGEST a name on '➕ Add board'. '' on failure."""
    import base64
    ah = dict(HEADERS)
    ah.update({"Authorization": JOBDIVA_BASIC, "portalID": "1", "a": token, "compid": "-1"})
    try:
        blob = SESSION.get(JOBDIVA_API + "auth/a", headers=ah, timeout=12).json().get("auth") or ""
        raw = base64.b64decode(blob + "=" * (-len(blob) % 4)).decode("utf-8", "replace")
        user = re.sub(r"(US|USA|UK)$", "", raw.split(":")[0])
        return _name_from(user) if user else ""
    except Exception:
        return ""


# ---- Avature — <tenant>.avature.net/<portal>/SearchJobs ----
# Server-rendered HTML portal (no public JSON API). We walk the WHOLE board with an empty
# search via ?jobOffset=N and let main()'s title/US filter trim it — the keyword search is a
# fuzzy relevance match (a missing phrase silently falls back to a placeholder), so walking
# is what guarantees coverage. Two card TEMPLATES exist across tenants and we handle both:
#   - 'listSingleColumnItem' (e.g. NVA): City:/State: spans, country in the JobDetail slug,
#     no posting date (found_date falls back to the scrape stamp), ~10/page.
#   - 'article--result' (e.g. Synopsys, Bloomberg): an optional .list-item-location and an
#     optional .list-item-posted ('Posted DD-Mon-YYYY'); fields vary per tenant (Bloomberg
#     has location/no date, Synopsys has date/no location), ~6-12/page.
#   - 'jobResultItem' (e.g. Epic): links /FolderDetail/ rather than /JobDetail/, carries no
#     location or date element at all (both come out of the URL slug), and — the part that
#     bites — pages by ?folderOffset=N. See _avature_offset_param.
# Page size varies, so we advance the offset by the actual cards-per-page. MAX caps a giant tenant.
AVATURE_PAGE = 10                                 # fallback only; we advance by len(cards)
AVATURE_MAX_JOBS = 3000


def _avature_base(board_url):
    """Normalize any Avature URL to '<scheme>://<tenant>.avature.net/<portal>/SearchJobs'
    (drop a trailing slash, query, keyword segment, or /JobDetail/... path)."""
    p = urlparse(board_url)
    segs = [s for s in p.path.split("/") if s]
    if "SearchJobs" in segs:
        segs = segs[:segs.index("SearchJobs") + 1]          # keep up through SearchJobs
    elif segs:
        segs = [segs[0], "SearchJobs"]                      # <portal>/JobDetail/... -> <portal>/SearchJobs
    else:
        segs = ["jobs", "SearchJobs"]
    return "%s://%s/%s" % (p.scheme, p.netloc, "/".join(segs))


# Words that mean an Avature card's header subtitle is posting metadata rather than a place.
# Kept as a whole-word match so a real city is never vetoed by a substring (there is a Posted
# Township in Kentucky as far as this regex is concerned, and "Employee" is not a place name).
_META_SUB = re.compile(r"\b(job id|posted|employee|full[- ]time|part[- ]time|contract|"
                       r"permanent|temporary|intern(ship)?|req(uisition)? ?id)\b", re.I)


def _avature_location(card, href, title=""):
    """A card's location, handling all three templates. The 'article--result' template
    (Bloomberg etc.) prints a single .list-item-location; the 'listSingleColumnItem' template
    (NVA) uses City:/State: spans + the country embedded in the JobDetail slug
    (…-United-States-… / …-Canada-…); the 'jobResultItem' template (Epic) carries no
    location element at all and is read out of the FolderDetail slug. '' when the tenant
    omits location entirely (e.g. Synopsys)."""
    if "/FolderDetail/" in href:
        return _avature_folder_location(href, title)
    el = card.select_one(".list-item-location")
    if el:
        return el.get_text(" ", strip=True).rstrip(".").strip()
    # A THIRD placement, found on lululemon 2026-08-16: same article--result template, but the
    # location sits in the header subtitle as "country · region · city" rather than in
    # .list-item-location. Without this the card read as location-''; blank passes
    # is_us_location (it has to — a blank must not be dropped), so every one of their ~1,100
    # postings entered the US feed, Zurich and Mexico included.
    #
    # THE SUBTITLE IS NOT ALWAYS A LOCATION, which is the whole reason for _META_SUB below:
    # Synopsys renders "Job ID 13924 • Employee • Posted 02-Jan-2026" into the same slot, and
    # it is bullet-separated exactly like lululemon's, so the separator cannot tell them apart.
    # Reading it blindly turned Synopsys's honest blank into a location of "Posted 02-Jan-2026,
    # Employee, Job ID 13924" — worse than blank, since it fails the US filter and would have
    # quietly dropped that board. Match on what the text says, not how it is punctuated.
    #
    # Reversed to "city, region, country" to match the shape the rest of the corpus stores and
    # that core's state parser expects.
    el = card.select_one(".article__header__text__subtitle")
    if el:
        txt = el.get_text(" ", strip=True)
        if not _META_SUB.search(txt):
            parts = [p.strip().rstrip(".").strip()
                     for p in re.split(r"[·•|]", txt) if p.strip()]
            if parts:
                return ", ".join(reversed(parts))
    city = state = ""
    for sp in card.select(".listSingleColumnItemMiscDataItem"):
        label, sep, val = sp.get_text(" ", strip=True).partition(":")
        if not sep:                                         # facility name (no 'City:'/'State:' label)
            continue
        label, val = label.strip().lower(), val.strip().rstrip(".").strip()
        if label.startswith("city"):
            city = val
        elif label.startswith("state"):
            state = val
    country = ("Canada" if "-Canada-" in href
               else "United States" if "United-States" in href else "")
    return ", ".join(x for x in (city, state, country) if x)


# A FolderDetail slug is "<city>-<state>-<country>-<title>", e.g.
# Verona-Wisconsin-United-States-Software-Developer. Nothing delimits the three location
# fields from each other or from the title, so it is read from both ends: the title slug is
# stripped off the tail (we already know the title — it is the link text), and the US state
# name is found inside what is left, which is what fixes the city boundary. Position alone
# cannot do it — "New-York-New-York-United-States-…" is four tokens with no seam.
#
# A slug with no US state (Epic's one UK folder,
# Bristol-BS1-6NL-United-Kingdom-of-Great-Britain-and-Northern-Ireland-…) is returned as
# plain text rather than guessed at: it still carries the country name, so is_us_location
# drops it on _NON_US_RE, which is the outcome that matters.
_AVATURE_SLUG_SEP = re.compile(r"[^A-Za-z0-9]+")
_AVATURE_STATE_RE = None                          # built on first use: US_STATE_NAMES is
                                                  # defined far below this point in the module


def _avature_state_re():
    global _AVATURE_STATE_RE
    if _AVATURE_STATE_RE is None:
        # Longest-first so "west virginia" is not matched as "virginia", which would put
        # "West" on the end of the city.
        _AVATURE_STATE_RE = re.compile(
            r"\b(" + "|".join(sorted((re.escape(s) for s in US_STATE_NAMES),
                                     key=len, reverse=True)) + r")\b", re.I)
    return _AVATURE_STATE_RE


def _avature_folder_location(href, title):
    """Location out of a /FolderDetail/<slug>/<id> URL. '' if the slug is only the title."""
    segs = [s for s in urlparse(href).path.split("/") if s]
    slug = segs[-2] if len(segs) >= 2 else ""
    tail = _AVATURE_SLUG_SEP.sub("-", title).strip("-")
    if tail and slug.lower().endswith(tail.lower()):
        slug = slug[:-len(tail)].strip("-")
    if not slug:
        return ""
    plain = slug.replace("-", " ")
    m = _avature_state_re().search(plain)
    if not m:
        return plain
    city = plain[:m.start()].strip()
    return ", ".join(x for x in (city, m.group(1).title(), "United States") if x)


def _avature_date(card):
    """Posting date from the 'article--result' template's .list-item-posted ('Posted
    DD-Mon-YYYY' -> 'YYYY-MM-DD'). '' for the NVA template (no date -> scrape-stamp fallback)."""
    el = card.select_one(".list-item-posted")
    if el:
        m = re.search(r"\d{1,2}-[A-Za-z]{3}-\d{4}", el.get_text(" ", strip=True))
        if m:
            try:
                return datetime.datetime.strptime(m.group(0), "%d-%b-%Y").strftime("%Y-%m-%d")
            except Exception:
                pass
    return ""


# The results header states the size of the whole board: <div class="list-controls__text__legend"
# aria-label="409 results">1-12 of 409 results</div>. Knowing the total up front is what lets the
# remaining offsets be fetched together instead of discovered one page at a time.
_AVATURE_TOTAL_RE = re.compile(r'aria-label="\s*([\d,]+)\s+results?"', re.I)


def _avature_offset_param(html):
    """Which query param pages THIS tenant. The folder template pages by ?folderOffset=N and
    ignores ?jobOffset=N outright — it does not error on it, it just serves page one again, so
    the wrong param looks like a board that is exactly one page long (Epic: 10 folders of 49,
    no warning, no short-read to notice)."""
    return "folderOffset" if "/FolderDetail/" in html else "jobOffset"


# Concurrent page fetches within ONE Avature board. Matches SCRAPE_PER_HOST: every one of these
# hits the same tenant host, so going wider would be impolite rather than faster.
AVATURE_WORKERS = 4


def _avature_cards(html, base, seen, rows):
    """Parse one results page into `rows`; returns how many NEW postings it contributed."""
    cards = BeautifulSoup(html, "lxml").select(
        "li.listSingleColumnItem, article.article--result, li.jobResultItem")
    new = 0
    for c in cards:
        a = c.select_one("a[href*='/JobDetail/'], a[href*='/FolderDetail/']")
        if not a or not a.get("href"):
            continue                                        # 'no results' placeholder card
        href = urljoin(base + "/", a["href"]).split("?")[0]
        if href in seen:
            continue
        seen.add(href)
        new += 1
        title = a.get_text(" ", strip=True).strip()
        row = {"title": title, "url": href,
               "location": _avature_location(c, href, title)}
        d = _avature_date(c)
        if d:
            row["found_date"] = d
        rows.append(row)
    return new, len(cards)


def scrape_avature(board_url):
    """Avature career portals (<tenant>.avature.net/<portal>/SearchJobs). Pages via
    ?jobOffset=N — or ?folderOffset=N on the folder template, see _avature_offset_param —
    handling all three card templates (listSingleColumnItem / article--result / jobResultItem)
    via _avature_location + _avature_date. The title (its /JobDetail/ or /FolderDetail/ link)
    is in all three; the title + US filter in main() trims the result.

    Pages are fetched CONCURRENTLY. jobOffset is stateless — no cursor, no session — so once the
    first page has told us the page size and the board total, every remaining offset is just a
    known URL. Walking them one at a time made Avature the slowest scraper in the sweep and the
    run's closing straggler: Bloomberg is 409 postings at 12 a page, i.e. 34 serial fetches of
    ~1.6s plus a politeness sleep between each.

    Falls back to the original serial walk when the total can't be read, so a template without
    the results header still works.
    """
    base = _avature_base(board_url)
    rows, seen = [], set()
    try:
        r = SESSION.get("%s/?jobOffset=0" % base, headers=HEADERS, timeout=25)
    except Exception:
        return rows
    if r.status_code != 200:
        return rows
    _new, page_size = _avature_cards(r.text, base, seen, rows)
    if not page_size:
        return rows
    off_param = _avature_offset_param(r.text)

    m = _AVATURE_TOTAL_RE.search(r.text)
    total = int(m.group(1).replace(",", "")) if m else 0
    if total > page_size:
        offsets = list(range(page_size, min(total, AVATURE_MAX_JOBS), page_size))

        def _page(off):
            # One retry. The serial walk used to STOP at the first bad page, so a blip cost the
            # tail of the board and was at least visible as a short result; fetching offsets
            # independently means a blip silently drops just that page's postings instead.
            for attempt in (0, 1):
                try:
                    p = SESSION.get("%s/?%s=%d" % (base, off_param, off),
                                    headers=HEADERS, timeout=25)
                    if p.status_code == 200:
                        return p.text
                except Exception:
                    pass
                if not attempt:
                    time.sleep(random.uniform(0.4, 0.9))
            return ""

        with concurrent.futures.ThreadPoolExecutor(max_workers=AVATURE_WORKERS) as ex:
            for html in ex.map(_page, offsets):
                if html:
                    _avature_cards(html, base, seen, rows)
        if total > AVATURE_MAX_JOBS:
            note_truncation(board_url, AVATURE_MAX_JOBS, AVATURE_MAX_JOBS, total)
        return rows

    # No total in the markup — walk it the old way, one page at a time until it runs dry.
    offset = page_size
    while offset < AVATURE_MAX_JOBS:
        try:
            r = SESSION.get("%s/?%s=%d" % (base, off_param, offset),
                            headers=HEADERS, timeout=25)
        except Exception:
            break
        if r.status_code != 200:
            break
        new, n_cards = _avature_cards(r.text, base, seen, rows)
        offset += n_cards or AVATURE_PAGE                   # advance by the real page size
        if not n_cards or new == 0:                         # reached the end of the board
            break
        time.sleep(random.uniform(0.2, 0.4))
    else:
        note_truncation(board_url, offset, AVATURE_MAX_JOBS)
    return rows


# ============================================================
# ORACLE PEOPLESOFT "Candidate Gateway" (Fluid) — jobs.<institution>.edu
#
# The stock careers portal for universities and hospital systems, which is to say the
# CAP-EXEMPT employers (no H-1B lottery) this feed cares most about. It LOOKS unscrapeable:
# a stateful ICAction portal that 302s straight to ?cmd=login. Three facts make it easy:
#
#   1. A guest session is one GET away. Hitting the /psp/ portal URL mints an anonymous
#      PS_TOKEN cookie; the /psc/ component URL then renders for us. Skip that GET and every
#      request bounces to the login page — which is why this looked walled at first glance.
#   2. The results grid (HRS_AGNT_RSLT_I) is rendered SERVER-SIDE, 50 rows at a time, and
#      each row carries title, location, job id, opened and closes dates. No JS, no JSON API.
#   3. Its "show more" is a single form POST that returns the WHOLE grown grid rather than a
#      delta — so paging is: post, re-parse, repeat until the row count stops growing.
#      Confirmed on FSU: 50 -> 100 -> 150 -> 200 -> 207 in four hops.
#
# The grid is sorted newest-first, so even a run that stops at the cap keeps the fresh end.
# ============================================================
PEOPLESOFT_GBL = "/EMPLOYEE/HRMS/c/HRS_HRAM_FL.HRS_CG_SEARCH_FL.GBL"
# ...but the component name is NOT universal. FSU serves HRS_HRAM_FL; Berkeley and Case
# Western serve HRS_HRAM_EMP_FL, and there are other variants. Substituting the constant for
# a tenant that uses a different one builds a URL that 404s, so read it off the URL we were
# given and keep the constant only as the fallback for a URL that carries no component.
_PS_GBL_RE = re.compile(r"(/ps[cp]/[^/]+)(/[A-Za-z0-9_]+/HRMS/c/HRS_[A-Za-z0-9_.]*HRS_CG_SEARCH[A-Za-z0-9_.]*)", re.I)


def _peoplesoft_gbl(url):
    """The /EMPLOYEE/HRMS/c/<COMPONENT>.GBL tail of a PeopleSoft careers URL.

    Falls back to PEOPLESOFT_GBL when the URL does not carry one, which keeps every existing
    caller behaving exactly as before."""
    m = _PS_GBL_RE.search(url or "")
    if not m:
        return PEOPLESOFT_GBL
    tail = m.group(2)
    return tail if tail.upper().endswith(".GBL") else tail + ".GBL"
PEOPLESOFT_MAX_ROWS = 1500          # ~30 "show more" hops; FSU needs 4
PEOPLESOFT_MAX_HOPS = 40

# Grid columns, by the PeopleSoft field name each cell's <span id> is built from.
_PS_ROW_FIELDS = (("title", "SCH_JOB_TITLE"),
                  ("location", "LOCATION"),
                  ("job_id", "HRS_APP_JBSCH_I_HRS_JOB_OPENING_ID"),
                  ("opened", "SCH_OPENED"))
_PS_TOTAL_RE = re.compile(r"([\d,]+)\s+jobs?\s+found", re.I)


def _peoplesoft_parts(url):
    """(origin, site_id) from any PeopleSoft careers URL, or (None, None).

    The site id is the tenant's portal name and differs per install (FSU: 'sprdhr_er'), so
    it has to be read off the URL rather than assumed. /psc/ and /psp/ are the same site
    reached through different servlets."""
    try:
        p = urlparse(url or "")
    except Exception:
        return None, None
    m = re.search(r"/ps[cp]/([^/]+)/", p.path or "")
    if not (p.netloc and m):
        return None, None
    return "%s://%s" % (p.scheme or "https", p.netloc), m.group(1)


def _ps_job_url(origin, site, job_id, gbl=None):
    """Deep link to one posting. Verified to render server-side from a COLD session (no
    cookie, no prior search), so it works both as the link we store for the user and as the
    URL score_jobs fetches the description from."""
    return ("%s/psc/%s%s?Page=HRS_APP_JBPST_FL&Action=U&FOCUS=Applicant"
            "&SiteId=1&JobOpeningId=%s&PostingSeq=1"
            % (origin, site, gbl or PEOPLESOFT_GBL, job_id))


def _ps_date(s):
    """PeopleSoft prints MM/DD/YYYY; the corpus stores ISO. '' when it's anything else."""
    m = re.match(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})", s or "")
    return "%s-%02d-%02d" % (m.group(3), int(m.group(1)), int(m.group(2))) if m else ""


def _ps_rows(html_text):
    """Grid rows out of a rendered page — or out of the XML a 'show more' POST returns, which
    carries the same markup inside CDATA, so one parser covers both."""
    from html import unescape
    by_idx = {}
    for key, fld in _PS_ROW_FIELDS:
        for i, v in re.findall(r"id='%s\$(\d+)'[^>]*>([^<]{0,200})" % fld, html_text or ""):
            by_idx.setdefault(int(i), {})[key] = unescape(v).strip()
    return [by_idx[k] for k in sorted(by_idx)]


def _ps_state(html_text, prev="1"):
    """(ICStateNum, ICSID) — PeopleSoft rejects a POST that doesn't echo its current state.
    The counter increments per interaction; fall back to prev+1 when a response omits it."""
    m = re.search(r"name='ICStateNum'[^>]*value='(\d+)'", html_text or "")
    s = re.search(r"name='ICSID'[^>]*value='([^']*)'", html_text or "")
    return (m.group(1) if m else str(int(prev) + 1)), (s.group(1) if s else "")


def scrape_peoplesoft(board_url):
    """PeopleSoft Candidate Gateway. Anonymous, no API key, no browser — see the block above."""
    origin, site = _peoplesoft_parts(board_url)
    if not origin:
        return []
    gbl = _peoplesoft_gbl(board_url)
    listing = "%s/psc/%s%s?Page=HRS_APP_SCHJOB_FL&Action=U" % (origin, site, gbl)
    try:
        # Guest session first: this GET is what makes everything after it visible.
        _safe_get("%s/psp/%s%s?Page=HRS_APP_SCHJOB_FL&Action=U&SiteId=1&FOCUS=Applicant"
                  % (origin, site, gbl), timeout=25)
        r = _safe_get(listing, timeout=30)
    except ValueError:
        return []                                   # non-public host -> refuse (SSRF guard)
    if r.status_code != 200:
        return []
    rows = _ps_rows(r.text)
    m = _PS_TOTAL_RE.search(re.sub(r"<[^>]+>", " ", r.text))
    total = int(m.group(1).replace(",", "")) if m else None
    snum, sid = _ps_state(r.text)

    hops = 0
    while (rows and len(rows) < PEOPLESOFT_MAX_ROWS and hops < PEOPLESOFT_MAX_HOPS
           and (not total or len(rows) < total)):
        hops += 1
        try:
            more = _safe_form_post(listing, {
                "ICAJAX": "1", "ICNAVTYPEDROPDOWN": "0", "ICType": "Panel", "ICElementNum": "0",
                "ICStateNum": snum, "ICAction": "HRS_AGNT_RSLT_I$hdown$0", "ICModelCancel": "0",
                "ICXPos": "0", "ICYPos": "0", "ResponsetoDiffFrame": "-1",
                "TargetFrameName": "None", "FacetPath": "None", "ICFocus": "",
                "ICSaveWarningFilter": "0", "ICChanged": "-1", "ICResubmit": "0", "ICSID": sid,
            }, headers=dict(HEADERS, **{"X-Requested-With": "XMLHttpRequest", "Referer": listing}))
        except ValueError:
            break
        if more.status_code != 200:
            break
        grown = _ps_rows(more.text)
        if len(grown) <= len(rows):                 # the grid stopped growing = end of list
            break
        rows = grown
        snum, sid2 = _ps_state(more.text, snum)
        sid = sid2 or sid
        time.sleep(random.uniform(0.2, 0.5))
    if len(rows) >= PEOPLESOFT_MAX_ROWS or hops >= PEOPLESOFT_MAX_HOPS:
        note_truncation(board_url, len(rows), PEOPLESOFT_MAX_ROWS, total)

    out = []
    for row in rows:
        title, jid = row.get("title", ""), row.get("job_id", "")
        if not (title and jid):
            continue
        job = {"title": title, "url": _ps_job_url(origin, site, jid, gbl),
               "location": row.get("location", "")}
        d = _ps_date(row.get("opened"))             # a REAL posting date, not a found-date
        if d:
            job["found_date"] = d
        out.append(job)
    return out


# ============================================================
# PAYLOCITY RECRUITING (recruiting.paylocity.com)
#
# The careers page is a React app and the company job list renders client-side, so the HTML
# looks empty to a scraper — no <a> to a posting anywhere in it. It isn't empty: the whole
# list ships in a `window.pageData` blob that React hydrates from, complete with job ids,
# real published dates and structured locations. Driving a browser here would be wasted work;
# confirmed by watching the rendered page make ZERO XHR for job data.
#
# Job DETAIL pages are ordinary server-rendered HTML (see score_jobs.paylocity_detail_jd),
# so descriptions cost one plain GET each.
# ============================================================
PAYLOCITY_HOST = "recruiting.paylocity.com"
# The slug charset is deliberately narrow. This regex is run over raw HTML as well as over
# URLs (that's how a single-posting link is resolved to its board), and a looser class like
# [^/?#]+ keeps matching straight through the closing quote into the rest of the tag.
_PAYLOCITY_ALL_RE = re.compile(
    r"/recruiting/jobs/all/([0-9a-f-]{36})(?:/([A-Za-z0-9._-]+))?", re.I)


def _paylocity_pagedata(html):
    """The window.pageData object a Paylocity careers page ships, or {}.

    Parsed with raw_decode so the JSON's own brace matching decides where the object ends —
    a regex would have to guess, and the blob contains job descriptions full of braces."""
    i = (html or "").find("window.pageData")
    j = html.find("{", i) if i >= 0 else -1
    if j < 0:
        return {}
    try:
        obj, _end = json.JSONDecoder().raw_decode(html, j)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _paylocity_board_url(guid, slug=""):
    return "https://%s/recruiting/jobs/All/%s%s" % (PAYLOCITY_HOST, guid, "/" + slug if slug else "")


def _paylocity_location(j):
    """A job's location string. Paylocity often leaves City/State null on remote roles and
    says so with IsRemote instead, so fall through those in order rather than trusting one."""
    loc = j.get("JobLocation") or {}
    named = (j.get("LocationName") or "").strip()
    city, state = (loc.get("City") or "").strip(), (loc.get("State") or "").strip()
    if named:
        return named
    if city or state:
        return ", ".join(x for x in (city, state) if x)
    if j.get("IsRemote"):
        return "Remote"
    return (loc.get("Country") or "").strip()


def scrape_paylocity(board_url):
    """One Paylocity employer's postings, read from the careers page's embedded pageData."""
    m = _PAYLOCITY_ALL_RE.search(board_url or "")
    if not m:
        return []
    try:
        r = _safe_get(_paylocity_board_url(m.group(1), m.group(2) or ""), timeout=25)
    except ValueError:
        return []
    if r.status_code != 200:
        return []
    rows = []
    for j in (_paylocity_pagedata(r.text).get("Jobs") or []):
        jid, title = j.get("JobId"), (j.get("JobTitle") or "").strip()
        if not (jid and title):
            continue
        country = ((j.get("JobLocation") or {}).get("Country") or "").strip().upper()
        if US_ONLY and country and country not in ("USA", "US", "UNITED STATES"):
            continue
        row = {"title": title,
               "url": "https://%s/Recruiting/Jobs/Details/%s" % (PAYLOCITY_HOST, jid),
               "location": _paylocity_location(j)}
        d = _posted(j.get("PublishedDate"))          # a real published date, not a found-date
        if d:
            row["found_date"] = d
        rows.append(row)
    return rows


# ============================================================
# MICHAEL PAGE — the one RECRUITMENT AGENCY board here, and that is worth stating.
#
# Every other source is an employer publishing its own openings. This is PageGroup's US
# agency site, so `company` is "Michael Page" on all of them and the actual employer is
# named only inside the JD ("Our client is a growing General Contractor..."). Two
# consequences to keep in mind before adding more agencies:
#
#   * SPONSORSHIP. The sponsor flag keys off the company name, so these rows flag against
#     Michael Page, not the hiring employer — which is why they will read as non-sponsors
#     almost across the board. That is not a bug in the flag, it is the truth about agency
#     placements, and it is the reason this feed has otherwise stayed employer-side.
#   * FLOOD. A few hundred rows all sharing one company name is exactly the shape the feed
#     groups (web._dupe_rank / the employer grouping) rather than dedupes. Nothing to do
#     here, but a second agency board doubles it.
#
# HOW IT IS READ. A plain Drupal view ("job_search"), rendered server-side: no JSON API, no
# JS. `div.job-tile` carries title, /job-detail/ link, location, contract type and salary.
# robots.txt allows /jobs and /job-detail and disallows /job-apply-external/ (the link shape
# that prompted this) plus 3-deep /jobs/*/*/*/ facets — none of which this touches.
#
# sort_by=most_recent IS LOAD-BEARING, the same lesson Adzuna taught: the default sort is
# relevance, and paging a relevance-sorted board with a cap returns an arbitrary slice that
# barely changes between runs. Sorted by date, a bounded page budget always reads the fresh
# end and the cap simply decides how far back it goes.
#
# COST is the reason for that budget. Measured 2026-08-16: 17s per page average (0.1s when
# their CDN has it warm, 15-28s cold), 30 tiles a page, 31% of them clearing the title + US
# filter. 12 pages is ~360 newest postings for ~3.5 min — several days of their posting rate
# at 3 runs a day, and one of the slower boards in the sweep, so raise MICHAELPAGE_MAX_PAGES
# only with SCRAPE_BUDGET_MIN in view.
#
# NO DATE ON THE CARD, deliberately left that way. The detail page's JSON-LD has a real
# datePosted, but reading it costs one fetch per job; score_jobs' JD pass already visits
# every detail page and picks the date up there (page_posted_date), so paying for it twice
# would buy nothing.
# ============================================================
MICHAELPAGE_MAX_PAGES = int(os.environ.get("MICHAELPAGE_MAX_PAGES") or 12)


def scrape_michaelpage(board_url):
    """Michael Page's Drupal job search. Walks ?sort_by=most_recent&page=N newest-first and
    stops at MICHAELPAGE_MAX_PAGES, an empty page, or a page that is all repeats."""
    parts = urlsplit(board_url)
    q = dict(parse_qsl(parts.query))
    q["sort_by"] = "most_recent"                     # never page a relevance sort (see above)
    rows, seen, page = [], set(), 0
    while page < MICHAELPAGE_MAX_PAGES:
        q["page"] = str(page)
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), ""))
        try:
            # 40s, not the usual 25: their cold pages measured up to 28s, and a timeout here
            # costs the whole rest of the board rather than one job.
            r = SESSION.get(url, headers=HEADERS, timeout=40)
        except Exception:
            break
        if r.status_code != 200:
            break
        tiles = BeautifulSoup(r.text, "lxml").select("div.job-tile")
        new = 0
        for t in tiles:
            a = t.select_one("div.job-title a[href]")
            if not a:
                continue
            href = urljoin(board_url, a["href"]).split("?")[0]
            if href in seen:
                continue                              # each tile links twice (title + View Job)
            seen.add(href)
            new += 1
            loc = t.select_one(".job-location")
            rows.append({"title": a.get_text(" ", strip=True),
                         "url": href,
                         "location": loc.get_text(" ", strip=True) if loc else ""})
        page += 1
        if not tiles or new == 0:                     # past the last page of results
            break
        time.sleep(random.uniform(0.2, 0.4))
    else:
        note_truncation(board_url, len(rows), MICHAELPAGE_MAX_PAGES * 30,
                        detail="MICHAELPAGE_MAX_PAGES=%d" % MICHAELPAGE_MAX_PAGES)
    return rows


# ============================================================
# AQUENT — the second recruitment AGENCY here, and the cheapest board in the sweep.
#
# WHAT IT IS. Aquent places creative, marketing and digital talent, so exactly as with Michael
# Page above, `company` is "Aquent" on all of them and the actual employer is named only inside
# the description ("Our client is a premier digital marketing agency..."). Read the
# MICHAELPAGE_BOARDS note before adding a third: it explains why these rows read as non-sponsors
# and why they arrive as one large single-company group. core.is_agency does not flag either
# name, so neither is hidden by the default hideagency pref -- that is the bargain Michael Page
# already struck, not a new one, and adding "aquent" to core._AGENCY_NAMES would badge these
# rows honestly at the cost of hiding all 104 of them by default.
#
# WHY IT NEEDS A BESPOKE ADAPTER. Aquent's own sites cannot be read: talent.aquent.com is a
# 3.8 KB Angular shell whose entire body is <app-root>, and aquent.com/find-work builds its list
# client-side, so the whole detect chain answers None on both. But the WordPress site publishes
# the ENTIRE board as one XML feed, and that feed is better shaped than most ATS APIs:
#
#   * 649 postings in ONE 3.1 MB request -- no pagination, so there is no page budget to tune
#     and no tail for a binding SCRAPE_BUDGET_MIN to starve
#   * a real pubDate per posting, so the freshness gate judges a date the employer STATED
#     rather than our scrape stamp (core.is_trusted_date)
#   * the full description inline (p50 4,210 chars, min 573), so every row reaches the keep loop
#     with a "jd": the description rule gets to vote and score_jobs owes it no fetch
#   * city / state / country as three separate elements, which is what makes the country rule
#     below possible at all
#
# MEASURED 2026-08-23: 649 postings -> 372 US -> 104 past the title filter (28%), 6 of those
# rescued by the description rule. Michael Page returns 31% for twelve paged requests; this is
# the same hit rate for one. robots.txt allows /feeds/ (Crawl-delay 10, which one request per
# sweep honours by construction), and the /find-work/<id> page every row points at carries
# JSON-LD and the real quick-apply form -- so the apply queue and score_jobs' page_posted_date
# both work on these rows, which is exactly what an aggregator relist could never offer.
# ============================================================

# Every title ends in its own req number -- "Creative Strategist [212490]". That is an id, not
# part of the role, and leaving it in costs three things at once: web.py::_title_index tokenises
# it, so "similar roles" ranks on a number; searchRank scores it; and it is the last thing the
# eye lands on on a card. Stripped only where it TRAILS -- one row reads "IT Project Manager I
# (212313) [212313]", and the parenthesised copy is the employer's own text, not ours to edit.
_AQUENT_REQ_RE = re.compile(r"\s*\[\d+\]\s*$")

# THE COUNTRY IS A TWO-LETTER CODE, AND A TWO-LETTER CODE IS THE TRAP. "Berlin, DE" is
# byte-identical to the "City, ST" shape is_us_location exists to recognise: DE is Germany and
# also Delaware, CA is Canada and also California, IN is India and also Indiana. That exact
# ambiguity is what put 157 Casablanca / Mississauga / Kolkata rows into the feed as US jobs --
# see the 2026-08-16 additions to NON_US. So the code is never written into a location string:
# a US row gets its state, and every other row gets the country's NAME, which _NON_US_RE knows.
#
# With US_ONLY on, foreign rows are dropped here on the feed's own <country> element rather than
# re-derived from a string downstream -- the same thing scrape_paylocity does with its
# JobLocation.Country. That is 277 of the 649, none of which would clear the gate anyway. The
# `or cc` fallback below is therefore reachable only with US_ONLY off, where no US gate runs.
_AQUENT_COUNTRIES = {"AU": "Australia", "CA": "Canada", "DE": "Germany", "FR": "France",
                     "GB": "United Kingdom", "JP": "Japan", "NL": "Netherlands"}


def _aquent_text(item, tag):
    """One child element's text, or "". NOT unescaped: the description is HTML source and
    core.html_to_text unescapes it itself, so doing it here too would decode &amp;lt; twice."""
    e = item.find(tag)
    return (e.text or "").strip() if e is not None and e.text else ""


def _aquent_location(item):
    """'Tokyo', '', 'JP' -> 'Tokyo, Japan'.   'Boston', 'MA', 'US' -> 'Boston, MA'.

    A US row with no city -- seven of them, all California -- would otherwise render as the bare
    string "CA", and is_us_location only reads a state abbreviation AFTER a comma, so those rows
    would be dropped as non-US. They get ", United States" instead, which the gate recognises
    and which core.parse_location still resolves to state=CA, city=''."""
    city = _aquent_text(item, "location/city")
    state = _aquent_text(item, "location/state")
    cc = _aquent_text(item, "location/country").upper()
    if cc == "US":
        parts = [city, state] if city else [state, "United States"]
    else:
        parts = [city, _AQUENT_COUNTRIES.get(cc) or cc]        # see the note above
    return ", ".join(p for p in parts if p)


def _aquent_date(s):
    """'Sun, 23 Aug 2026 21:08:19 GMT' -> '2026-08-23'.

    RFC-822, not ISO, so _posted's plain slice would hand back 'Sun, 23 Au' and the freshness
    gate would compare that to a date. Returns "" on anything unparseable, which is what lets
    main()'s setdefault fall back to the scrape stamp."""
    try:
        return email.utils.parsedate_to_datetime(s).date().isoformat()
    except Exception:
        return ""


def scrape_aquent(board_url):
    """Aquent's whole board, from the one XML feed. board_url is AQUENT_FEED itself."""
    import xml.etree.ElementTree as ET
    try:
        # 90s for one 3.1 MB response: generous on purpose, because a timeout here costs the
        # entire board rather than one posting, and it is the only request this source makes.
        r = SESSION.get(board_url or AQUENT_FEED, headers=HEADERS, timeout=90)
        root = ET.fromstring(r.content)
    except Exception:
        return []
    rows = []
    for item in root.findall(".//item"):
        if US_ONLY and _aquent_text(item, "location/country").upper() != "US":
            continue
        title = _AQUENT_REQ_RE.sub("", html.unescape(_aquent_text(item, "title"))).strip()
        url = _aquent_text(item, "url")
        if not (title and is_http_url(url)):
            continue
        loc = _aquent_location(item)
        # "Fully remote" ONLY. "Hybrid remote" is not remote, and promoting an office-attached
        # role to fully remote is the one location error the filter cannot show the user -- the
        # same reason _jobvite_location keeps "Hybrid" rather than dropping it.
        if _aquent_text(item, "remotetype").lower() == "fully remote":
            loc = ("%s (Remote)" % loc).strip() if loc else "Remote"
        row = {"title": title, "url": url, "location": loc,
               # Inline and complete -- see the note above; this is the whole point of the feed.
               "jd": _listing_jd(_aquent_text(item, "description"))}
        d = _aquent_date(_aquent_text(item, "pubDate"))
        if d:
            row["found_date"] = d
        rows.append(row)
    return rows


def detect_paylocity(url):
    """Recognize a Paylocity careers link, including a link to a SINGLE posting.

    A /Recruiting/Jobs/Details/<id> URL carries no company id, and that is the shape people
    actually copy — it's what a job alert links to. The posting page's "All Jobs" breadcrumb
    is the only place the company GUID appears, so that one case costs a fetch. The list URL
    itself is matched without touching the network."""
    url = (url or "").strip()
    if not url:
        return None
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    if PAYLOCITY_HOST not in (urlparse(url).netloc or "").lower():
        return None
    m = _PAYLOCITY_ALL_RE.search(url)
    if m:
        return (_paylocity_board_url(m.group(1), m.group(2) or ""), "paylocity",
                _name_from(m.group(2) or "Paylocity employer"))
    if not re.search(r"/recruiting/jobs/details/\d+", url, re.I):
        return None
    try:
        r = _safe_get(url, timeout=20)
        if r.status_code != 200:
            return None
        m = _PAYLOCITY_ALL_RE.search(r.text)
        if not m:
            return None
        # The <title> is "<Company> - <Job Title>", a better name than the URL slug.
        t = re.search(r"<title>([^<]*)</title>", r.text, re.I)
        name = (t.group(1).split(" - ")[0].strip() if t else "") or _name_from(m.group(2) or "")
        return (_paylocity_board_url(m.group(1), m.group(2) or ""), "paylocity", name)
    except Exception:
        return None


# ---- Eightfold AI — <tenant>.eightfold.ai/api/apply/v2/jobs ----
#
# Eightfold was written off as bot-walled and its companies routed through Adzuna. Adzuna is gone
# (2026-08-16), so those companies have had NO source at all since. Re-probed 2026-08-19 and the
# earlier judgement turns out to be half right: the API is gated PER TENANT, not per platform.
# Of 30 candidates probed, bayer (607 jobs) and insight (183) answer 200 with full JSON, while
# micron / target / wipro / dolby / vodafone / conagra / lamresearch / infosys answer a flat 403
# that no Referer, Origin, Accept or X-Requested-With header changes. So: worth scraping, and
# worth expecting roughly a third of tenants to refuse.
#
# `num` is capped at 10 SERVER-SIDE whatever we ask for (verified at 10/50/100/200), so a
# 600-posting board is 61 requests. That is why the pages go out concurrently, the same reasoning
# as scrape_avature: `start` is a stateless offset, so once page 0 reports `count` every remaining
# offset is a known URL.
EIGHTFOLD_PAGE = 10                      # their hard cap, not our choice
EIGHTFOLD_MAX_JOBS = 3000
EIGHTFOLD_WORKERS = 6


def _eightfold_domain(board_url):
    """The `domain` query param the API requires.

    Taken from the URL when the board carries it (their own careers links do:
    …/careers?domain=insight.com), else derived from the tenant label. Deriving is a guess and a
    wrong guess returns an empty list rather than an error, so prefer the explicit form in SOURCES.
    """
    q = parse_qs(urlparse(board_url).query)
    dom = (q.get("domain") or [""])[0].strip()
    return dom or (_sub(board_url) + ".com")


def _eightfold_rows(positions):
    """Map Eightfold positions to feed rows, across BOTH of their payload spellings.

    /api/apply/v2/jobs sends location / canonicalPositionUrl / t_create; the /api/pcsx/search
    variant that Microsoft serves sends locations (a LIST) / positionUrl / postedTs. Reading
    only the first spelling parsed zero rows out of a perfectly good 1,078-position response.
    """
    rows = []
    for p in positions or []:
        # "Indianola,Pennsylvania,United States" — their own join, no space after the comma, which
        # would otherwise reach the feed looking like a formatting bug of ours.
        raw = p.get("location")
        if not raw:
            alt = p.get("locations") or p.get("standardizedLocations") or []
            raw = alt[0] if isinstance(alt, list) and alt else (alt if isinstance(alt, str) else "")
        loc = ", ".join(x.strip() for x in (raw or "").split(",") if x.strip())
        row = {"title": (p.get("name") or "").strip(),
               "url": p.get("canonicalPositionUrl") or p.get("positionUrl") or "",
               "location": loc}
        try:                             # t_create is epoch SECONDS (not ms, unlike Lever);
            ts = p.get("t_create")       # postedTs from the pcsx variant can be either
            if ts in (None, ""):
                ts = p.get("postedTs")
            ts = int(ts)
            if ts > 100000000000:                       # milliseconds
                ts //= 1000
            row["found_date"] = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        except Exception:
            pass
        if row["title"] and row["url"]:
            rows.append(row)
    return rows


def scrape_eightfold(board_url):
    """Eightfold AI boards. Returns [] on a 403 tenant rather than raising — a gated tenant is a
    normal outcome here, not a broken board, and scrape_all's health tracking already records a
    board that yields nothing."""
    host = urlparse(board_url).netloc or (_sub(board_url) + ".eightfold.ai")
    dom = _eightfold_domain(board_url)
    # Two endpoints, same product. /api/apply/v2/jobs is the classic one; tenants on the
    # newer front-end serve /api/pcsx/search instead and 403 the classic path outright --
    # Microsoft is the case that found this, and it answers a plain request on pcsx while
    # refusing apply/v2. Both wrap the same positions[] + count, so the only difference is
    # the URL and that pcsx takes an explicit location filter (worth using: it turns a
    # global sweep into a US one, 1,078 rows instead of the whole catalogue).
    apis = [("https://%s/api/apply/v2/jobs" % host,
             {"domain": dom, "hl": "en"}),
            ("https://%s/api/pcsx/search" % host,
             {"domain": dom, "query": "", "location": "United States"})]
    api, extra = apis[0]

    def _page(start):
        for attempt in (0, 1):
            try:
                params = dict(extra, start=start, num=EIGHTFOLD_PAGE)
                r = SESSION.get(api, headers=HEADERS, timeout=25, params=params)
                if r.status_code == 200:
                    d = r.json()
                    # pcsx nests the same payload one level down under "data".
                    return d.get("data") if isinstance(d.get("data"), dict) else d
            except Exception:
                pass
            if not attempt:
                time.sleep(random.uniform(0.4, 0.9))
        return {}

    first = _page(0)
    if not (first.get("positions") or first.get("count")):
        api, extra = apis[1]                       # classic path refused; try the newer one
        first = _page(0)
    rows = _eightfold_rows(first.get("positions"))
    if not rows:
        return rows
    total = int(first.get("count") or 0)
    if total > EIGHTFOLD_PAGE:
        starts = list(range(EIGHTFOLD_PAGE, min(total, EIGHTFOLD_MAX_JOBS), EIGHTFOLD_PAGE))
        with concurrent.futures.ThreadPoolExecutor(max_workers=EIGHTFOLD_WORKERS) as ex:
            for d in ex.map(_page, starts):
                rows.extend(_eightfold_rows(d.get("positions")))
        if total > EIGHTFOLD_MAX_JOBS:
            note_truncation(board_url, EIGHTFOLD_MAX_JOBS, EIGHTFOLD_MAX_JOBS, total)
    # `start` paging occasionally re-serves a posting across page boundaries when the board is
    # re-indexed mid-sweep. Dedupe on URL here so the count we report is the count we stored.
    seen, out = set(), []
    for r in rows:
        if r["url"] not in seen:
            seen.add(r["url"])
            out.append(r)
    return out


# ---- Digitas (Publicis Groupe) — a branded Drupal front end over a bot-walled iCIMS tenant ----
#
# The ATS underneath is iCIMS, tenant `careers-publicisgroupe`, and it is unreadable: every path
# tried (/jobs/search, /jobs/<id>/job, /sitemap.xml, /) answers HTTP 405 with a 2,115-byte "Human
# Verification" interstitial, and browser headers change nothing. The brand's own Drupal site is
# the only way in, and it serves 200 with the full posting.
#
# There is no JSON anywhere on it — no API, no __NEXT_DATA__, no JSON-LD JobPosting — so the entry
# point is the Drupal Simple XML Sitemap. 14.2 MB in 2.7s, 5,947 urls, of which 1,938 are jobs and
# only 102 are /en-us/: the same requisitions are republished under ~19 locale prefixes.
#
# Probed 2026-08-20: razorfish, publicissapient, publicishealth and zenithmedia run the same CMS
# but publish NO job urls in their sitemaps, and leoburnett / saatchi / spark-foundry / starcomww
# do not answer at all. So this is Digitas-only in practice, though the function takes its host
# from board_url — if a sister brand starts publishing, it is one SOURCES line and no new code.
DIGITAS_MAX_JOBS = 400
DIGITAS_WORKERS = 6
_DIGITAS_JOB_RE = re.compile(r"/en-us/careers/\d+-\d+-[a-z0-9-]+$", re.I)
# "Associate Director, Project Management | New York | Digitas" — consistent across every page
# sampled, and the ONLY place the location appears in the markup.
_DIGITAS_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S | re.I)


def _digitas_location(city):
    """'Plano' -> 'Plano, TX'.

    NOT cosmetic. The page gives a BARE CITY, and is_us_location() rejects those: measured,
    "Plano", "Chicago" and "Boston" all read as non-US while only "New York" passes. Returning the
    city verbatim would have silently dropped three of every four US jobs on this board — the
    failure mode that looks identical to a company simply not hiring in the US.
    core.parse_location already knows the metro for each, so this composes it rather than shipping
    a city->state table of its own.
    """
    city = (city or "").strip()
    if not city:
        return ""
    p = core.parse_location(city)
    metro = p.get("metro") or ""
    if p.get("city") and ", " in metro:
        return "%s, %s" % (p["city"], metro.rsplit(", ", 1)[1])
    # No metro means we do not recognise it — return it unchanged and let the US filter decide.
    # Guessing here would be worse: it is how a London posting ends up in a US feed.
    return metro or city


def _digitas_job_urls(host):
    """Job urls out of a 14.2 MB sitemap without ever holding it in memory.

    STREAMED, not fetched whole, for two reasons. _safe_get caps a response at _MAX_FETCH_BYTES
    (5 MB) and is right to — it exists for user-supplied URLs — so raising that ceiling so one
    sitemap can be read would trade a real guard for a convenience. And this also runs on a shared
    cPanel box that serves the website at the same time, where a 30 MB transient (14 MB of bytes
    plus 14 MB of decoded string) is worth not allocating.

    Safe to do line-wise because the file is line-oriented: measured 3,372 newlines in the first
    300 KB with a longest line of 165 bytes, so no <loc> ever straddles a chunk boundary.
    """
    r = SESSION.get("https://%s/sitemap.xml" % host, headers=HEADERS, timeout=90, stream=True)
    try:
        if r.status_code != 200:
            return []
        out, seen_bytes = set(), 0
        for line in r.iter_lines(chunk_size=65536, decode_unicode=True):
            if not line:
                continue
            # A ceiling anyway, well clear of the 14 MB this actually is. A sitemap an order of
            # magnitude larger is a CMS fault, not a big careers section, and reading it would
            # spend the whole board budget.
            seen_bytes += len(line)
            if seen_bytes > 60 * 1024 * 1024:
                note_truncation("https://%s/sitemap.xml" % host, len(out), len(out), -1,
                                detail="sitemap exceeded 60 MB, stopped reading")
                break
            if "<loc>" in line:
                for u in re.findall(r"<loc>([^<]+)</loc>", line):
                    if _DIGITAS_JOB_RE.search(u):
                        out.add(u)
        return sorted(out)
    finally:
        r.close()


def scrape_digitas(board_url):
    """Digitas jobs, via the brand site's sitemap plus one fetch per posting.

    A page per job is more requests than any feed-backed board needs, but the whole en-us set is
    102 postings fetched concurrently — a few seconds — and there is no listing page to read
    instead: /en-us/careers is marketing copy and contains no job links at all.

    DELIBERATELY NO found_date. Every one of the 102 <lastmod> values is identical
    (2026-08-19T23:35:07), i.e. when Drupal last rebuilt the sitemap, not when anything was
    posted. Storing it would put a confident-looking timestamp on 102 rows whose real posting date
    is unknown, which is exactly what core.is_trusted_date exists to keep out of the corpus.
    main() stamps the scrape date instead, and that is honest.
    """
    host = urlparse(board_url).netloc or "www.digitas.com"
    try:
        urls = _digitas_job_urls(host)
    except Exception as e:
        # Named, not swallowed. The first version of this used _safe_get, which caps a body at
        # _MAX_FETCH_BYTES (5 MB) and therefore raised on a 14.2 MB sitemap -- and a bare
        # `except Exception: return []` reported that as "this board has no jobs", which is the
        # most expensive kind of wrong: indistinguishable from a company that stopped hiring.
        print("   digitas: sitemap read failed (%s: %s)" % (type(e).__name__, str(e)[:90]))
        return []
    if len(urls) > DIGITAS_MAX_JOBS:
        note_truncation(board_url, DIGITAS_MAX_JOBS, DIGITAS_MAX_JOBS, len(urls))
        urls = urls[:DIGITAS_MAX_JOBS]

    def _one(u):
        for attempt in (0, 1):
            try:
                p = SESSION.get(u, headers=HEADERS, timeout=25)
                if p.status_code == 200:
                    m = _DIGITAS_TITLE_RE.search(p.text)
                    if not m:
                        return None
                    # unescape BEFORE splitting on "|": these titles carry &amp; and &#8211;, and
                    # without this one shipped as "Manager, Digital Product Management – AI
                    # Products &amp" — a title that then fails to match its own keywords and looks
                    # to a reader like our bug, which it was.
                    raw = html.unescape(re.sub(r"\s+", " ", m.group(1)))
                    parts = [x.strip() for x in raw.split("|")]
                    if len(parts) < 2:
                        return None
                    return {"title": parts[0], "url": u,
                            "location": _digitas_location(parts[1])}
            except Exception:
                pass
            if not attempt:
                time.sleep(random.uniform(0.3, 0.7))
        return None

    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=DIGITAS_WORKERS) as ex:
        for row in ex.map(_one, urls):
            if row and row["title"]:
                rows.append(row)

    # COLLAPSE THE REQUISITION VARIANTS. One req is published at several urls -- 148526-0,
    # 148526-1034104 and 148526-1034105 are all the same Plano role -- and canonical_url cannot
    # see it because the paths genuinely differ. Storing all three would also read as a 3x repost
    # in scraper.reposts, inventing employer behaviour out of a CMS quirk. Keyed on
    # (title, location), the same identity core.posting_key uses.
    seen, out = set(), []
    for r0 in rows:
        k = (r0["title"].lower(), r0["location"].lower())
        if k not in seen:
            seen.add(k)
            out.append(r0)
    return out


# ---- Jobvite --------------------------------------------------------------------------------
# The note above detect_board lists Jobvite among what "genuinely remains unread", next to Taleo
# and Cornerstone. That was wrong, and cheaply so: jobs.jobvite.com/<slug>/jobs is a plain
# server-rendered <table> -- one <tr> per posting, `.jv-job-list-name a` for the title and link,
# `.jv-job-list-location` for the place -- with no JS, no API key, no pagination and no bot wall.
#
# NO found_date, DELIBERATELY. The list carries title and location only; the posting date lives in
# a JSON-LD JobPosting on each job page, so reading it costs one extra request per job on every
# run, forever. scraper/verify_dates.py buys the same thing for less -- the lookup service it
# calls rates jobvite "full" coverage, it is budgeted, and its ledger stops a row being asked
# twice -- and meanwhile main() stamps the scrape date, which core.is_trusted_date correctly
# reports as derived rather than stated. Digitas fetches a page per posting only because it has no
# listing page to read at all.
JOBVITE_HOST = "jobs.jobvite.com"
_JOBVITE_HREF_RE = re.compile(r"^/[^/]+/job/[A-Za-z0-9]+/?$")
# "6 Locations" is what the table prints when one req spans offices. It is a COUNT, not a place,
# and it is not in core.parse_location's skip set, so it lands as the city.
_JOBVITE_LOC_COUNT_RE = re.compile(r"^\d+\s+locations?$", re.I)
_JOBVITE_WORK_MODEL_RE = re.compile(r"^(hybrid|on-?site)\s+remote$", re.I)


def _jobvite_location(cell):
    """'Hybrid Remote , San Francisco, California' -> 'Hybrid, Remote, San Francisco, California'.

    A REFORMAT, not a strip. core.parse_location already skips the tokens 'hybrid' and 'remote'
    when it picks a city, but this table writes them as ONE compound token, "Hybrid Remote", which
    matches neither and becomes the city: measured on dwt, 13 of 18 rows came back with a city of
    "Hybrid Remote" and 4 more with a city of "N Locations". Splitting the compound in two lets
    the existing skip set do its job. It is not simply DELETED because parse_location reads the
    remote flag off the substring "remote" anywhere in the string; and "Hybrid" is kept alongside
    it because hybrid is not remote -- dropping it upgrades an office-attached role to fully
    remote, which is the kind of wrong the location filter cannot show the user.
    """
    out = []
    for part in [p.strip() for p in (cell or "").split(",") if p.strip()]:
        if _JOBVITE_LOC_COUNT_RE.match(part):
            continue                                   # a count, not a place
        m = _JOBVITE_WORK_MODEL_RE.match(part)
        out += [m.group(1).title(), "Remote"] if m else [part]
    return ", ".join(out)


def scrape_jobvite(board_url):
    """Jobvite career sites. board_url is the tenant root, https://jobs.jobvite.com/<slug>.
    One request: /<slug>/jobs lists the whole board, with no pagination to walk."""
    p = urlparse(board_url)
    segs = [s for s in (p.path or "").split("/") if s]
    if not segs:
        return []
    base = "%s://%s" % (p.scheme or "https", p.netloc or JOBVITE_HOST)
    try:
        r = _safe_get("%s/%s/jobs" % (base, segs[0]), timeout=25)
    except ValueError:
        return []                                      # non-public host -> refuse (SSRF guard)
    if r.status_code != 200:
        return []
    rows, seen = [], set()
    for tr in BeautifulSoup(r.text, "lxml").select("tr"):
        a = tr.select_one(".jv-job-list-name a[href]")
        if not a or not _JOBVITE_HREF_RE.match((a.get("href") or "").strip()):
            continue
        url = urljoin(base, (a.get("href") or "").strip())
        if not is_http_url(url) or url in seen:
            continue
        seen.add(url)
        title = html.unescape(re.sub(r"\s+", " ", a.get_text(" ", strip=True))).strip()
        if not title:
            continue
        cell = tr.select_one(".jv-job-list-location")
        rows.append({
            "title": title,
            "url": url,
            "location": _jobvite_location(
                re.sub(r"\s+", " ", cell.get_text(" ", strip=True)) if cell else ""),
        })
    return rows


# ---- Werfen -- a Drupal careers view over a bot-walled iCIMS tenant -------------------------
# Same shape as Digitas above and the same reason. The ATS is iCIMS, tenant `careers-werfen`, and
# every path on it (/api/jobs, /jobs/search, /jobs/<id>/job, /sitemap.xml, /) answers HTTP 405
# with the 2,115-byte "Human Verification" interstitial. Werfen's own Drupal site is the way in,
# and unlike Digitas it publishes the whole board as a paginated Views table -- 20 rows a page,
# ~10 requests -- so there is no need to fetch a page per posting.
#
# THE ROW URL IS THE WERFEN PAGE, NOT THE iCIMS APPLY LINK. Each /en/<slug> page does carry the
# careers-werfen.icims.com url, but storing that would put the bot wall on the primary key:
# score_jobs could not read a JD off it, and scraper/liveness.py would score every row dead --
# its BOT_WALL_PHRASES already lists "human verification", added for this exact interstitial. The
# Werfen page answers 200 with the full posting and links onward to apply.
#
# DELIBERATELY NO found_date, despite the table having an "Open date" column. Measured over all
# 188 rows on 2026-08-20: 18 are in the FUTURE (up to 2026-10-19, two months out) and 51 carry
# that same day. A column that forward-dates a tenth of the board is not a posting date, and it
# would arrive as a bare ISO string -- the shape core.is_trusted_date reads as STATED rather than
# derived -- so it would launder a guess into a verified-looking date and pin those 18 rows to the
# top of a date-sorted feed. main() stamps the scrape date instead, which is honest.
#
# KNOWN INCOMPLETE MIRROR. iCIMS req 10136 ("Project Manager I", Bedford MA) is live on the tenant
# and 404s on werfen.com, so this view is a SUBSET of the ATS. Still 188 rows and 32 clearing
# title_verdict, against the zero the bot wall allows.
WERFEN_MAX_PAGES = 20
# Street-address noise in the location cell. Only the four Werfen-occupied sites carry one.
# Greedy on purpose, so it runs past the LAST street word rather than the first, which is what
# "9900 Old Grove Road San Diego" needs.
_WERFEN_STREET_RE = re.compile(
    r"^.*\b(?:road|rd|route|street|st|drive|dr|avenue|ave|lane|ln|boulevard|blvd|way|court|ct"
    r"|circle|cir|parkway|pkwy|highway|hwy|place|pl|terrace)\b\.?\s*", re.I)
_WERFEN_ZIP_RE = re.compile(r"\b\d{5}(?:-\d{4})?\b")
# The site-type prefixes Werfen writes in the cell. Left alone they become the city.
_WERFEN_SITE_WORDS = {"werfen", "field"}


def _werfen_location(cell):
    """'Werfen - Bedford - 180 Hartwell Road Bedford, Massachusetts 01730 United States'
    -> 'Bedford, Massachusetts United States'.

    The cell is `<site> - [<state> - ]<label> - <address>`, and core.parse_location takes its city
    from the FIRST token, so the raw string yields a city of "Werfen" or "Field" and a metro of
    "Werfen, MA" -- a place that does not exist, offered to the user as a location filter.
    Measured over the 24 distinct US cells on 2026-08-20, keeping only the part after the last
    " - " and then dropping a leading street address recovers the real city in all 24, including
    "Salt Lake City", which any last-word-before-the-comma rule truncates to "City", and
    "526 Route 303 Orangeburg", where a house number outlives the street word.
    """
    tail = (cell or "").split(" - ")[-1].strip()
    head, _sep, rest = tail.partition(",")
    head = _WERFEN_STREET_RE.sub("", head).strip()
    head = re.sub(r"^\d[\w-]*\s+", "", head).strip()   # "303 Orangeburg" -> "Orangeburg"
    if head.lower() in _WERFEN_SITE_WORDS:
        head = ""                                      # "Field - US - Field, United States"
    rest = re.sub(r"\s+", " ", _WERFEN_ZIP_RE.sub("", rest)).strip()
    return ", ".join(x for x in (head, rest) if x)


def scrape_werfen(board_url):
    """Werfen jobs off the Drupal Views table at /en/careers-finder, walking ?page=N.

    Stops on the first page that adds no NEW url rather than on an empty one, which is also how it
    stops if Drupal answers an out-of-range ?page= by serving the last page again instead of an
    empty table -- against that, an is-the-table-empty check would walk to the cap every run.
    """
    p = urlparse(board_url)
    base = "%s://%s" % (p.scheme or "https", p.netloc or "www.werfen.com")
    path = p.path or "/en/careers-finder"
    rows, seen = [], set()
    for page in range(WERFEN_MAX_PAGES):
        try:
            r = _safe_get("%s%s?page=%d" % (base, path, page), timeout=30)
        except ValueError:
            break                                      # non-public host -> refuse (SSRF guard)
        if r.status_code != 200:
            break
        got = 0
        for tr in BeautifulSoup(r.text, "lxml").select("table tr"):
            tds = tr.select("td")
            a = tr.select_one("a[href]")
            if not a or len(tds) < 5:
                continue                               # the header row, or a table that isn't this
            url = urljoin(base, (a.get("href") or "").strip())
            if not is_http_url(url) or url in seen:
                continue
            seen.add(url)
            got += 1
            vals = [re.sub(r"\s+", " ", td.get_text(" ", strip=True)) for td in tds]
            loc, ctry = _werfen_location(vals[3]), vals[4]
            if ctry and ctry.lower() not in loc.lower():
                loc = (loc + ", " + ctry).strip(", ")
            title = html.unescape(vals[0]).strip()
            if title:
                rows.append({"title": title, "url": url, "location": loc})
        if not got:
            break
        time.sleep(random.uniform(0.3, 0.7))
    else:
        note_truncation(board_url, len(rows), WERFEN_MAX_PAGES * 20, 0,
                        detail="hit WERFEN_MAX_PAGES")
    return rows


# ---- Google careers (careers.google.com) --------------------------------------------------
# Google has NO public jobs API. Its careers site is a BOQ app that talks batchexecute RPC, so
# there is nothing to call -- but careers.google.com/jobs/sitemap lists every posting, and each
# posting page embeds its own record in an AF_initDataCallback ds:0 block:
#     [["<id>","<title>","<apply url>", ... "Mountain View, CA, USA", ...]]
#
# THE COST, because it is the only reason this adapter is shaped the way it is. A posting page is
# 1.1 MB decompressed but 161 KB on the wire (gzip, automatic), and the location exists ONLY in
# that ds:0 block, which sits past 416 KB of the page -- so there is no capped read, and the
# server does not honour Range. Fetching all 1,436 on-target postings is ~231 MB.
#
# Three things bring that down, in order of how much they save:
#   1. filter on the slug FIRST. The sitemap URL is /results/<id>-<slug>, and the slug IS the
#      title, so title_verdict runs for free and drops 3,376 to ~1,436 before any page is fetched.
#   2. a ledger of ids already resolved, so a run only pays for postings it has never seen.
#      Steady state is Google's daily new postings, i.e. tens -- call it 8 MB a run.
#   3. a per-run page cap, so the first backfill is spread over several runs instead of landing
#      as one 231 MB request storm.
GOOGLE_SITEMAP = "https://careers.google.com/jobs/sitemap"
GOOGLE_LEDGER_KEY = "google_careers_seen"
GOOGLE_MAX_PAGES = int(os.environ.get("GOOGLE_MAX_PAGES") or 250)
GOOGLE_BUDGET_MIN = float(os.environ.get("GOOGLE_BUDGET_MIN") or 6)
GOOGLE_LEDGER_MAX = 6000                 # ids kept; oldest dropped so the blob cannot grow forever
_G_JOB_RE = re.compile(r"/jobs/results/(\d+)-([a-z0-9-]+)/?$")
_G_DS0_RE = re.compile(r"key: 'ds:0'.{0,200}?data:(\[.{0,4000})", re.S)
_G_US_LOC_RE = re.compile(r'"([A-Z][A-Za-z .\'-]{1,40}, [A-Z]{2}, USA)"')


def _google_sitemap_jobs():
    """[(id, slug_title, url)] for every posting in the sitemap. One 76 KB request."""
    try:
        r = _safe_get(GOOGLE_SITEMAP, timeout=30)
    except Exception:
        return []
    if r.status_code != 200:
        return []
    out = []
    for u in re.findall(r"<loc>([^<]+)</loc>", r.text):
        m = _G_JOB_RE.search(u)
        if m:
            out.append((m.group(1), m.group(2).replace("-", " ").strip(), u))
    return out


def _google_page_row(url):
    """(title, location) from a posting page's ds:0 block, or None.

    The location is the first "City, ST, USA" string in the block; a posting outside the US has
    none, which is exactly the filter we want -- the sitemap is global.
    """
    try:
        r = _safe_get(url, timeout=25)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    m = _G_DS0_RE.search(r.text)
    if not m:
        return None
    blk = m.group(1)
    strings = re.findall(r'"([^"]{3,120})"', blk)
    title = ""
    for s in strings[1:4]:                      # [0] is the id; the title follows it
        if not s.isdigit() and not s.startswith("http"):
            title = s
            break
    loc = _G_US_LOC_RE.search(blk)
    if not (title and loc):
        return None
    return title.strip(), loc.group(1).strip()


def scrape_google(board_url):
    """Google careers via sitemap + per-posting ds:0 parse. See the cost note above.

    US-only by construction: a posting with no "City, ST, USA" in its record is skipped, which is
    what keeps Google's global catalogue out of a US feed.
    """
    jobs = _google_sitemap_jobs()
    if not jobs:
        return []
    # (1) the free filter -- the slug is the title
    cands = [(jid, url) for jid, slug, url in jobs if title_verdict(slug)[0]]
    # (2) the ledger: only pay for postings never resolved before
    try:
        import db as _db
        led = _db.get_kv(GOOGLE_LEDGER_KEY) or {}
    except Exception:
        led = {}
    seen = set(led.get("ids") or [])
    fresh = [(j, u) for j, u in cands if j not in seen]
    # A run that has nothing new still returns the postings it already knows about? No -- it
    # returns []. main() treats an empty board as "nothing new today", and reconcile_closed's
    # 3-consecutive-miss rule is what would retire rows, so the ledger is capped below to make
    # sure the whole set is revisited rather than frozen forever.
    if not fresh:
        fresh = [(j, u) for j, u in cands][:GOOGLE_MAX_PAGES]
    # (3) the per-run cap and a wall clock, so a backfill cannot eat the sweep
    plan = fresh[:GOOGLE_MAX_PAGES]
    stop = time.monotonic() + GOOGLE_BUDGET_MIN * 60
    rows, done = [], []
    for jid, url in plan:
        if time.monotonic() >= stop:
            note_truncation(board_url or GOOGLE_SITEMAP, len(rows), GOOGLE_MAX_PAGES, len(cands),
                            detail="hit GOOGLE_BUDGET_MIN (%g min)" % GOOGLE_BUDGET_MIN)
            break
        got = _google_page_row(url)
        done.append(jid)
        if got:
            rows.append({"title": got[0], "url": url, "location": got[1]})
        time.sleep(random.uniform(0.05, 0.15))
    else:
        if len(cands) > len(plan):
            note_truncation(board_url or GOOGLE_SITEMAP, len(rows), GOOGLE_MAX_PAGES, len(cands),
                            detail="hit GOOGLE_MAX_PAGES")
    try:
        import db as _db
        keep = (list(seen) + done)[-GOOGLE_LEDGER_MAX:]
        _db.put_kv(GOOGLE_LEDGER_KEY, {"ids": keep})
    except Exception:
        pass
    return rows


def _sitemap_locs_streamed(url, cap_mb=60, timeout=90):
    """Every <loc> in a sitemap, read line-wise so a large one is never held in memory.

    _safe_get caps a body at _MAX_FETCH_BYTES (5 MB) and is right to -- it guards user-supplied
    URLs -- but a real careers sitemap can be much bigger: Cognizant's is 43,794 entries, and
    _safe_get raised on it. The digitas adapter already learned this on a 14.2 MB sitemap; this is
    the same technique, factored out. Line-wise is safe because sitemaps are line-oriented.

    Raises rather than returning [], because "the sitemap could not be read" and "this employer
    has no jobs" must not look identical to the caller.
    """
    r = SESSION.get(url, headers=HEADERS, timeout=timeout, stream=True)
    try:
        if r.status_code != 200:
            raise ValueError("sitemap HTTP %s" % r.status_code)
        out, seen = [], 0
        for line in r.iter_lines(chunk_size=65536, decode_unicode=True):
            if not line:
                continue
            seen += len(line)
            if seen > cap_mb * 1024 * 1024:
                note_truncation(url, len(out), len(out), -1,
                                detail="sitemap exceeded %d MB, stopped reading" % cap_mb)
                break
            out += re.findall(r"<loc>([^<]+)</loc>", line)
        return out
    finally:
        r.close()


# ---- Generic sitemap + schema.org JobPosting -----------------------------------------------
# For employers with no API and no supported ATS, but whose posting pages carry the same
# JobPosting structured data Google for Jobs reads. Measured over the top 150 unreachable
# employers this pattern only fits 4 of them -- 105 expose no sitemap at all -- so it is NOT a
# general answer to the long tail. It is here because the ones it does fit include Cognizant,
# the second-largest H-1B filer in the corpus, at 8 KB a page.
#
# THE LOCALE TRAP. Cognizant lists the same posting under /us-en/, /ca-en/, /india-en/ and
# /global-en/, so a URL locale says nothing about where the job is: a /us-en/ posting in the
# sample had addressCountry "Mexico". The country filter therefore comes from the JSON-LD, and
# the locale is used only to pick ONE copy of each posting.
JSONLD_SM_PATHS = ("/sitemap.xml", "/sitemap_index.xml")
JSONLD_SM_MAX_PAGES = int(os.environ.get("JSONLD_SM_MAX_PAGES") or 400)
JSONLD_SM_BUDGET_MIN = float(os.environ.get("JSONLD_SM_BUDGET_MIN") or 5)
JSONLD_SM_LEDGER_MAX = 6000
_JLSM_JOB_RE = re.compile(r"/jobs?/(\d+)/([a-z0-9-]+)/?$", re.I)
_JLSM_LOCALE_RE = re.compile(r"/([a-z]{2,8}-[a-z]{2})/jobs?/", re.I)
_JLSM_US = ("united states", "usa", "us")


def _jlsm_sitemap_jobs(base, prefer_locale="us-en"):
    """[(id, slug_title, url)] from a sitemap, ONE copy per posting.

    Prefers `prefer_locale` when a posting appears under several, else takes the first seen --
    picking a copy is about not fetching the same job four times, not about location.
    """
    locs, err = [], None
    for sp in JSONLD_SM_PATHS:
        try:
            locs = _sitemap_locs_streamed(base.rstrip("/") + sp)
        except Exception as e:
            err = e
            continue
        if locs:
            break
    if not locs:
        # Named, not swallowed: a sitemap we could not read must not report as an employer
        # that stopped hiring. Same lesson the digitas adapter records above.
        if err is not None:
            print("   jsonld_sitemap: %s sitemap unreadable (%s: %s)"
                  % (base, type(err).__name__, str(err)[:90]))
        return []
    if locs and all(("sitemap" in x or x.endswith(".xml")) for x in locs[:3]):
        for child in locs[:8]:
            if "job" in child.lower():
                try:
                    locs = _sitemap_locs_streamed(child)
                    break
                except Exception:
                    pass
    best = {}
    for u in locs:
        m = _JLSM_JOB_RE.search(u)
        if not m:
            continue
        jid, slug = m.group(1), m.group(2).replace("-", " ").strip()
        loc = _JLSM_LOCALE_RE.search(u)
        pref = bool(loc and loc.group(1).lower() == prefer_locale)
        if jid not in best or (pref and not best[jid][0]):
            best[jid] = (pref, slug, u)
    return [(jid, v[1], v[2]) for jid, v in best.items()]


def _jlsm_row(url):
    """(title, location) from a posting's JobPosting JSON-LD, US only, or None."""
    try:
        r = _safe_get(url, timeout=20)
    except Exception:
        return None
    if r.status_code != 200 or '"JobPosting"' not in r.text:
        return None
    soup = BeautifulSoup(r.text, "lxml")
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "", strict=False)
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for it in list(items):
            if isinstance(it, dict) and isinstance(it.get("@graph"), list):
                items += it["@graph"]
        for it in items:
            if not isinstance(it, dict) or it.get("@type") != "JobPosting":
                continue
            title = _text(it.get("title"))
            jl = it.get("jobLocation")
            jl = jl[0] if isinstance(jl, list) and jl else jl
            addr = (jl or {}).get("address") if isinstance(jl, dict) else None
            addr = addr if isinstance(addr, dict) else {}
            ctry = addr.get("addressCountry")
            ctry = ctry.get("name") if isinstance(ctry, dict) else ctry
            if not (title and str(ctry or "").strip().lower() in _JLSM_US):
                continue
            loc = ", ".join(str(x) for x in (addr.get("addressLocality"),
                                             addr.get("addressRegion"), "United States") if x)
            return title, loc
    return None


def scrape_jsonld_sitemap(board_url):
    """Sitemap-driven JSON-LD board. Same three savings as scrape_google, same reasons.

    The slug filter is what makes this affordable: the URL carries the title, so title_verdict
    runs before any page is fetched. Cognizant: 2,053 postings -> 488 on-target -> ~4 MB.
    """
    base = re.match(r"^(https?://[^/]+)", board_url or "")
    if not base:
        return []
    base = base.group(1)
    jobs = _jlsm_sitemap_jobs(base)
    if not jobs:
        return []
    cands = [(jid, u) for jid, slug, u in jobs if title_verdict(slug)[0]]
    key = "jsonld_sm_seen:" + urlparse(base).netloc
    try:
        import db as _db
        seen = set((_db.get_kv(key) or {}).get("ids") or [])
    except Exception:
        seen = set()
    fresh = [(j, u) for j, u in cands if j not in seen] or cands[:JSONLD_SM_MAX_PAGES]
    plan = fresh[:JSONLD_SM_MAX_PAGES]
    stop = time.monotonic() + JSONLD_SM_BUDGET_MIN * 60
    rows, done = [], []
    for jid, u in plan:
        if time.monotonic() >= stop:
            note_truncation(board_url, len(rows), JSONLD_SM_MAX_PAGES, len(cands),
                            detail="hit JSONLD_SM_BUDGET_MIN (%g min)" % JSONLD_SM_BUDGET_MIN)
            break
        got = _jlsm_row(u)
        done.append(jid)
        if got:
            rows.append({"title": got[0], "url": u, "location": got[1]})
        time.sleep(random.uniform(0.05, 0.15))
    else:
        if len(cands) > len(plan):
            note_truncation(board_url, len(rows), JSONLD_SM_MAX_PAGES, len(cands),
                            detail="hit JSONLD_SM_MAX_PAGES")
    try:
        import db as _db
        _db.put_kv(key, {"ids": (list(seen) + done)[-JSONLD_SM_LEDGER_MAX:]})
    except Exception:
        pass
    return rows


# ---- IBM careers (www-api.ibm.com/search/api/v2) -------------------------------------------
# IBM's careers search is an Elasticsearch passthrough. No key, no auth, no cookie -- a plain
# POST answers it, which is why this is 40 lines and not a browser adapter. Found the way
# Microsoft's was: drive the page headless and pair each request with the response it produced.
#
# PAIRING MATTERS, and getting it wrong cost an hour. The page fires more than one query, and
# capturing requests separately from responses meant replaying a payload that legitimately
# matches nothing: it carried post_filter {field_keyword_08: "United States"} -- a JOB FAMILY
# field, not a location -- and returned total 0 while looking perfectly plausible. The query that
# actually returns the 1,498 postings has NO post_filter and lang "zz".
#
# Fields, none of them self-describing:
#   field_keyword_19  LOCATION ("Austin, US", "Bangalore, IN", "Multiple Cities")
#   field_keyword_08  job family (Consulting, Software Engineering)
#   field_keyword_18  level (Professional, Entry Level)
#   field_keyword_17  Hybrid / Remote / ""
#
# Cost: 100 rows a page at ~80 KB, so the whole board is ~1.2 MB. 18% of postings are US.
IBM_SEARCH_API = "https://www-api.ibm.com/search/api/v2"
IBM_PAGE = 100
IBM_MAX_ROWS = int(os.environ.get("IBM_MAX_ROWS") or 3000)


def _ibm_body(frm):
    # sort by _id, NOT by the page's own [_score, pageviews]: that ordering is unstable across
    # requests and overlapped 12 of 30 rows between consecutive pages, so paging it silently
    # returned duplicates and missed others.
    return {"appId": "careers", "scopes": ["careers2"], "lang": "zz",
            "sm": {"query": "", "lang": "zz"}, "localeSelector": {},
            "query": {"bool": {"must": []}},
            "sort": [{"_id": "asc"}], "size": IBM_PAGE, "from": frm,
            "_source": ["_id", "title", "url", "field_keyword_19", "field_keyword_18",
                        "field_keyword_08", "field_keyword_17"]}


def scrape_ibm(board_url):
    """IBM careers, US postings only.

    US-ness comes from field_keyword_19 ending ", US". "Multiple Cities" is deliberately dropped:
    118 of the first 500 rows carry it and it names no country, so admitting it would put
    unknown-location rows into a US feed.
    """
    rows, seen, frm = [], set(), 0
    while frm < IBM_MAX_ROWS:
        try:
            r = SESSION.post(IBM_SEARCH_API, json=_ibm_body(frm), timeout=30,
                             headers=dict(HEADERS, **{"Content-Type": "application/json",
                                                      "Accept": "application/json",
                                                      "Referer": "https://www.ibm.com/"}))
            if r.status_code != 200:
                break
            payload = (r.json() or {}).get("hits") or {}
        except Exception:
            break
        hits = payload.get("hits") or []
        if not hits:
            break
        total = ((payload.get("total") or {}).get("value")) or 0
        for h in hits:
            hid = h.get("_id")
            if hid in seen:
                continue
            seen.add(hid)
            s = h.get("_source") or {}
            loc = _text(s.get("field_keyword_19"))
            if not loc.strip().endswith(", US"):
                continue
            title, url = _text(s.get("title")), _text(s.get("url"))
            if title and url:
                rows.append({"title": title, "url": url,
                             "location": loc.rsplit(",", 1)[0].strip() + ", United States"})
        frm += IBM_PAGE
        if total and frm >= min(total, IBM_MAX_ROWS):
            break
        time.sleep(random.uniform(0.2, 0.5))
    return rows


# ---- Deloitte (apply.deloitte.com) ---------------------------------------------------------
# No API and no sitemap -- sitemap.xml serves the SPA shell -- but the search results are
# SERVER-RENDERED, which the browser sniff only revealed because it looked at the DOM rather than
# just the network: zero JSON responses, ten real job links in the HTML.
#
# Pagination is jobOffset, found by clicking "Next >>" and reading the href. jobRecordsPerPage is
# accepted and IGNORED -- 10, 50 and 100 all return exactly ten rows -- so the page size is not
# tunable and the only lever is how many offsets to walk. At ~21 KB on the wire per page, the
# whole board is ~2 MB.
DELOITTE_SEARCH = "https://apply.deloitte.com/en_US/careers/SearchJobs/"
DELOITTE_PAGE = 10
DELOITTE_MAX_PAGES = int(os.environ.get("DELOITTE_MAX_PAGES") or 140)
_DEL_JOB_RE = re.compile(r"/careers/JobDetail/[A-Za-z0-9-]+/(\d+)")


def _deloitte_rows(html):
    """[(id, title, location)] from one results page.

    The location is the LAST cell of the row -- the block reads
    "Senior Consultant | Deloitte US | Deloitte Consulting LLP | Tampa, Florida, United States"
    -- so it is taken from the tail rather than by class name, which this template does not give.
    """
    soup = BeautifulSoup(html, "lxml")
    out = []
    for a in soup.select('a[href*="/careers/JobDetail/"]'):
        m = _DEL_JOB_RE.search(a.get("href") or "")
        if not m:
            continue
        title = a.get_text(" ", strip=True)
        if not title:
            continue
        loc = ""
        block = a.find_parent(["li", "tr", "div"])
        if block:
            parts = [x.strip() for x in block.get_text("|", strip=True).split("|") if x.strip()]
            # "last cell containing a comma" is NOT enough: a title like "CCaaS x AI,
            # Manager, Technical Transformation" satisfies it and was being stored as the
            # location for its own row. Require the cell to actually name a place, and
            # never accept the title back.
            for cand in reversed(parts):
                if cand == title or len(cand) >= 90 or "," not in cand:
                    continue
                if _csb_is_us(cand) or re.search(r", [A-Z][a-z]+$", cand):
                    loc = cand
                    break
        out.append((m.group(1), title, loc))
    return out


def scrape_deloitte(board_url):
    """Deloitte US careers by walking the server-rendered result pages."""
    rows, seen = [], set()
    for page in range(DELOITTE_MAX_PAGES):
        try:
            r = _safe_get(DELOITTE_SEARCH, timeout=25,
                          params={"jobRecordsPerPage": DELOITTE_PAGE,
                                  "jobOffset": page * DELOITTE_PAGE})
        except Exception:
            break
        if r.status_code != 200:
            break
        got = _deloitte_rows(r.text)
        fresh = [x for x in got if x[0] not in seen]
        if not fresh:
            break                                  # a page of pure repeats is the end
        for jid, title, loc in fresh:
            seen.add(jid)
            if US_ONLY and loc and not _csb_is_us(loc):
                continue
            rows.append({"title": title,
                         "url": "%sJobDetail/%s" % (DELOITTE_SEARCH.rsplit("SearchJobs/", 1)[0],
                                                    jid),
                         "location": loc})
        time.sleep(random.uniform(0.15, 0.4))
    else:
        note_truncation(board_url or DELOITTE_SEARCH, len(rows),
                        DELOITTE_MAX_PAGES * DELOITTE_PAGE, 0, detail="hit DELOITTE_MAX_PAGES")
    return rows


# ---- Apple (jobs.apple.com) -----------------------------------------------------------------
# Apple looked client-rendered and is not: the results are embedded in the page as a React Router
# hydration blob, window.__staticRouterHydrationData = JSON.parse("..."). Only the LINKS are built
# client-side, which is why a link-based check found one job in 318 KB and concluded there was
# nothing there. The blob is a JSON document inside a JS string literal, so it decodes twice.
#
# No API was needed in the end, and no browser. Cost: 20 results a page, fixed -- pageSize, limit
# and sort are all accepted and ignored -- so 4,486 US postings is ~225 requests at ~55 KB on the
# wire, about 12 MB. Comparable to one large Workday tenant.
APPLE_SEARCH = "https://jobs.apple.com/en-us/search"
APPLE_LOCATION = "united-states-USA"
APPLE_MAX_PAGES = int(os.environ.get("APPLE_MAX_PAGES") or 240)
# Apple Retail is excluded at the source. It is thousands of store roles, and the title filter
# admits some of them -- "US-Operations Specialist" at The Shops at Blackstone Valley survives,
# as do US-Manager and US-Technical Specialist. That is the same store-floor class already
# blocked for Ulta and Family Dollar: E-Verify enrolment does not make a store job satisfy
# STEM-OPT, because the ROLE has to relate to the degree. Excluding by teamID rather than by
# title is exact, and it keeps Apple's corporate engineering roles, which are the point.
APPLE_SKIP_TEAMS = frozenset(("teamsAndSubTeams-APPST",))
_APPLE_HYDRATE_RE = re.compile(r"window\.__staticRouterHydrationData\s*=\s*JSON\.parse\(", re.S)


def _apple_hydration(text):
    """The decoded hydration object, or None. Two json.loads: literal -> string -> object."""
    m = _APPLE_HYDRATE_RE.search(text)
    if not m:
        return None
    try:
        i = text.index('"', m.end())
    except ValueError:
        return None
    j, n = i + 1, len(text)
    while j < n:                                   # walk the JS string, honouring escapes
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == '"':
            break
        j += 1
    try:
        return json.loads(json.loads(text[i:j + 1]))
    except Exception:
        return None


def _apple_results(obj):
    """The dict holding searchResults, wherever the router nested it."""
    if isinstance(obj, dict):
        if isinstance(obj.get("searchResults"), list):
            return obj
        for v in obj.values():
            r = _apple_results(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _apple_results(v)
            if r:
                return r
    return None


def scrape_apple(board_url):
    """Apple US postings from the search pages' hydration blob."""
    rows, seen, total = [], set(), 0
    for page in range(1, APPLE_MAX_PAGES + 1):
        try:
            r = _safe_get(APPLE_SEARCH, timeout=30,
                          params={"location": APPLE_LOCATION, "page": page})
        except Exception:
            break
        if r.status_code != 200:
            break
        blk = _apple_results(_apple_hydration(r.text) or {})
        if not blk:
            break
        res = blk.get("searchResults") or []
        if not res:
            break
        total = total or int(blk.get("totalRecords") or 0)
        added = 0
        for it in res:
            pid = _text(it.get("positionId"))
            if not pid or pid in seen:
                continue
            seen.add(pid)
            added += 1
            locs = it.get("locations") or []
            # countryID is the reliable US test; the location NAME can be a store
            # ("The Shops at Blackstone Valley") or the bare country.
            us = [l for l in locs if "USA" in str(l.get("countryID") or "")]
            if US_ONLY and not us:
                continue
            team = it.get("team") or {}
            if _text(team.get("teamID")) in APPLE_SKIP_TEAMS:
                continue
            title = _text(it.get("postingTitle"))
            slug = _text(it.get("transformedPostingTitle"))
            if not (title and slug):
                continue
            rows.append({"title": title,
                         "url": "https://jobs.apple.com/en-us/details/%s/%s" % (pid, slug),
                         "location": _text((us[0] if us else locs[0]).get("name")),
                         "found_date": _text(it.get("postDateInGMT"))[:10]})
        if not added:                              # a page of pure repeats is the end
            break
        if total and len(seen) >= total:
            break
        time.sleep(random.uniform(0.15, 0.35))
    else:
        if total > len(seen):
            note_truncation(board_url or APPLE_SEARCH, len(rows), APPLE_MAX_PAGES * 20, total,
                            detail="hit APPLE_MAX_PAGES")
    return rows


SCRAPERS = {
    "greenhouse": scrape_greenhouse,
    "eightfold": scrape_eightfold,
    "google": scrape_google,
    "ibm": scrape_ibm,
    "deloitte": scrape_deloitte,
    "apple": scrape_apple,
    "jsonld_sitemap": scrape_jsonld_sitemap,
    "digitas": scrape_digitas,
    "lever": scrape_lever,
    "ashby": scrape_ashby,
    "kula": scrape_kula,
    "smartrecruiters": scrape_smartrecruiters,
    "amazon": scrape_amazon,
    "workday": scrape_workday,
    "jibe": scrape_jibe,
    "recruitee": scrape_recruitee,
    "breezy": scrape_breezy,
    "personio": scrape_personio,
    "jsonld": scrape_jsonld,
    "jobspy": scrape_jobspy,
    "phenom": scrape_phenom,
    "oracle": scrape_oracle,
    "workable": scrape_workable,
    "ultipro": scrape_ultipro,
    "successfactors": scrape_successfactors,
    "bamboohr": scrape_bamboohr,
    "pinpoint": scrape_pinpoint,
    "rippling": scrape_rippling,
    "avature": scrape_avature,
    "jobdiva": scrape_jobdiva,
    "peoplesoft": scrape_peoplesoft,
    "paylocity": scrape_paylocity,
    "metacareers": scrape_metacareers,
    "michaelpage": scrape_michaelpage,
    "workatastartup": scrape_workatastartup,
    "jobvite": scrape_jobvite,
    "werfen": scrape_werfen,
    "aquent": scrape_aquent,
}


# ============================================================
# ADD-A-BOARD  — turn a pasted careers link into a scrapeable source
# ============================================================
# Not every careers site exposes a feed we can read, and detect_board() returns None for the
# ones that do not — the app routes those to careers links instead. The list of exceptions has
# shrunk: Oracle (ORC), Phenom, SuccessFactors, Avature, Paylocity, PeopleSoft and now Eightfold
# all turned out to have one, and Jobvite joined them on 2026-08-20 -- see scrape_jobvite, whose
# listing page is a plain server-rendered table. What genuinely remains unread: Taleo, Teamtailor,
# Cornerstone, Google/Meta-style bespoke portals, and NATIVE iCIMS portals -- those answer 405 +
# "Human Verification" on every path, and the only way past one is a branded CMS in front of it
# that republishes the reqs (scrape_digitas, scrape_werfen).
_LOCALES = {"en-us", "en-gb", "en", "us", "global", "en-us"}


def _name_from(slug):
    s = slug.replace("-", " ").replace("_", " ")
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)   # split camel/Pascal: AveryDennison -> Avery Dennison
    s = re.sub(r"\s+", " ", s).strip()
    return s.title() if (s.islower() or s.isupper()) else s


# Decoration a board puts in its OWN name that says nothing about which employer it is.
# Deliberately NOT probe_migratemate._BOARD_DECOR: that one is used to compare two names for
# equality during grading, where over-stripping loses a real signal. This one is used to turn a
# page title into something displayable, so it can afford to be greedier -- "Candidate
# Experience Site" is pure Oracle boilerplate and reduces to nothing, which is the honest answer.
_TITLE_DECOR = re.compile(
    r"\b(careers?|career site|job board|jobs?|external|internal|website|web site|site|"
    r"corporate|corp site|opportunities|general|portal|hiring|recruiting|talent|employment|"
    r"candidate experience|lateral|campus|search)\b", re.I)

# The two platforms whose API states the employer outright.
_NAME_ENDPOINTS = {
    "greenhouse": ("https://boards-api.greenhouse.io/v1/boards/%s", "name"),
    "smartrecruiters": ("https://api.smartrecruiters.com/v1/companies/%s", "name"),
}
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_OG_SITE_RE = re.compile(
    r"""<meta[^>]+property=["']og:site_name["'][^>]+content=["']([^"']+)""", re.I)


def _clean_title_name(s):
    s = (s or "").replace(chr(92) + "/", "/")   # Oracle escapes / in its titles
    s = re.sub(r"\s+", " ", _TITLE_DECOR.sub(" ", s))
    return re.sub(r"\s+", " ", s).strip(" -|:,–—")


def board_display_name(board_url, ats_type, timeout=15):
    """What the board calls ITSELF, or "" when the platform will not say.

    detect_board can only guess from the URL slug, and _name_from turns an opaque tenant code
    into a plausible-looking company: hdpc.fa.us2.oraclecloud.com became "Hdpc", which is how
    131 Goldman Sachs postings sat under a four-letter Oracle tenant id -- carrying no
    sponsorship signal at all, because "Hdpc" matches nothing in the filing data while
    "Goldman Sachs" matches 5,417 petitions.

    Two sources, strongest first: an API that names the employer, for the two platforms that
    offer one; then the board page's own og:site_name / <title>, which is where Oracle Cloud
    and Ashby put a real company name. Workday and UltiPro render their titles client-side and
    so answer nothing -- hence the "" contract rather than a guess dressed up as an answer.
    """
    ep = _NAME_ENDPOINTS.get(ats_type)
    if ep:
        api, field = ep
        slug = (board_url or "").rstrip("/").rsplit("/", 1)[-1]
        try:
            r = SESSION.get(api % slug, headers=HEADERS, timeout=timeout)
            if r.status_code == 200:
                got = _clean_title_name((r.json() or {}).get(field) or "")
                if got:
                    return got
        except Exception:
            pass
    try:
        r = SESSION.get(board_url, headers=HEADERS, timeout=timeout)
        if r.status_code == 200:
            for rx in (_OG_SITE_RE, _TITLE_RE):
                m = rx.search(r.text)
                if m:
                    got = _clean_title_name(m.group(1))
                    if got:
                        return got
    except Exception:
        pass
    return ""


def name_is_sluglike(name, board_url):
    """True when `name` carries nothing the URL did not already say.

    The one test that would have caught every one of the nine garbled employers found on
    2026-08-22. detect_board's third return value is a SUGGESTION for a paste box, not a fact;
    stored unchallenged it records World Fuel Services as "Wfscorp" and Monogram Health as
    "Mon1026Monoh". Compare the proposed name against what _name_from would make of each URL
    segment: if they agree, the name is the tenant slug wearing title case.
    """
    n = _norm_name(name or "")
    if not n:
        return True
    for seg in re.split(r"[/.]", (board_url or "").lower()):
        if seg and _norm_name(_name_from(seg)) == n:
            return True
    return False


def host_is(host, *domains):
    """True when `host` IS one of `domains`, or a subdomain of one. Nothing else.

    `"greenhouse.io" in host` is not a host test — it is a substring search, and it says yes to
    greenhouse.io.evil.example, to notgreenhouse.io, and to anything at all with those bytes
    somewhere in it. Several branches of detect_board then keep the CALLER'S host in the board
    URL they return, so a spoofed hostname survived into a stored board and into probe_board's
    fetch. Reachable from /add by any signed-in account:

        https://myworkdaysite.com.169.254.169.254.nip.io/recruiting/t/s
          -> ('https://myworkdaysite.com.169.254.169.254.nip.io/recruiting/t/s', 'workday', 'T')

    An anchored suffix cannot do that. Ports are stripped so host:8080 is still the same host,
    and a trailing dot (the DNS root form, which resolves identically) is too.
    """
    h = (host or "").lower().split("@")[-1].split(":")[0].rstrip(".")
    return any(h == d or h.endswith("." + d) for d in domains)


def detect_board(url):
    """Map a pasted job-board URL to (normalized_board_url, ats_type, suggested_name),
    or None if it isn't one of the scrapeable ATS feeds. The normalized URL is the exact
    form the matching scrape_* function expects.

    EVERY host test here is anchored through host_is — see the note there for what an
    unanchored `in` let through."""
    url = (url or "").strip()
    if not url:
        return None
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    p = urlparse(url)
    host = p.netloc.lower()
    segs = [s for s in p.path.split("/") if s]

    if host_is(host, "greenhouse.io"):
        slug = (parse_qs(p.query).get("for") or [None])[0]      # embed link: ?for=slug
        if not slug and "boards" in segs:                        # boards-api/v1/boards/<slug>/jobs
            i = segs.index("boards")
            slug = segs[i + 1] if i + 1 < len(segs) else None
        if not slug and segs:
            slug = segs[0]
        if slug:
            return ("https://job-boards.greenhouse.io/%s" % slug, "greenhouse", _name_from(slug))

    if host_is(host, "lever.co") and segs:
        return ("https://jobs.lever.co/%s" % segs[0], "lever", _name_from(segs[0]))

    if host_is(host, "ashbyhq.com") and segs:
        return ("https://jobs.ashbyhq.com/%s" % segs[0], "ashby", _name_from(segs[0]))

    if host_is(host, "smartrecruiters.com") and segs:
        return ("https://jobs.smartrecruiters.com/%s" % segs[0], "smartrecruiters", _name_from(segs[0]))

    if host_is(host, "recruitee.com"):
        sub = host.split(".")[0]
        return ("https://%s.recruitee.com" % sub, "recruitee", _name_from(sub))

    if host_is(host, "eightfold.ai"):
        sub = host.split(".")[0]
        # The `domain` param is mandatory, and the tenant label is only sometimes the domain
        # ("insight" -> insight.com holds, plenty do not). Keep whatever the pasted URL carried;
        # scrape_eightfold falls back to <tenant>.com and probe_board will reject a wrong guess
        # rather than adding a board that silently yields nothing.
        dom = (parse_qs(p.query).get("domain") or [""])[0].strip() or (sub + ".com")
        return ("https://%s/careers?domain=%s" % (host, dom), "eightfold", _name_from(sub))

    if host_is(host, "breezy.hr"):
        sub = host.split(".")[0]
        return ("https://%s.breezy.hr" % sub, "breezy", _name_from(sub))

    if host_is(host, "personio.com", "jobs.personio.com"):
        sub = host.split(".")[0]
        return ("https://%s.jobs.personio.com" % sub, "personio", _name_from(sub))

    if host == "aquent.com" or host.endswith(".aquent.com"):
        # ONE feed is the whole board, so every Aquent link resolves to the same source: a
        # talent.aquent.com/quick-apply page, a /find-work/<id> posting, the marketing site.
        # It is already in SOURCES, and custom_sources() dedupes app-added boards against
        # SOURCES by URL, so adding it from /add cannot make the sweep read it twice.
        return (AQUENT_FEED, "aquent", "Aquent")

    if host_is(host, "myworkdayjobs.com", "myworkdaysite.com"):
        _h, tenant, site = _workday_parts(url)
        if tenant and site:
            norm = ("https://%s/recruiting/%s/%s" % (_h, tenant, site)
                    if host_is(host, "myworkdaysite.com") else "https://%s/%s" % (_h, site))
            return (norm, "workday", _name_from(tenant))

    # Oracle Fusion recruiting, on its own host OR behind a vanity domain. The host test
    # alone missed every vanity deployment -- careers.autozone.com/hcmUI/... is the same
    # product and _oracle_parts already parses it, so the board read fine (10,172 postings)
    # while detect_board reported "not an ATS". /hcmUI/CandidateExperience/ is specific to
    # this product, so matching the PATH cannot collide with another vendor.
    if ("/hcmUI/CandidateExperience/" in p.path
            or host_is(host, "oraclecloud.com")) and "/sites/" in p.path:
        origin, site = _oracle_parts(url)
        # THE NAME COMES FROM /sites/<site>, NOT FROM THE TENANT SUBDOMAIN.
        #
        # Oracle's tenant host is an opaque code — fa-exhh-saasfaprod1 — and taking the name
        # from it recorded Staples as "Fa Exhh Saasfaprod1". That is not cosmetic: the stored
        # label is what core.sponsor_strength looks up, so an employer with 612 H-1B filings on
        # record rendered as no sponsorship record at all, which is the inversion the "colour
        # means sponsorship" rule exists to prevent. The real name was sitting in the SAME URL
        # the whole time: …/hcmUI/CandidateExperience/en/sites/StaplesInc.
        #
        # Site ids are not always a name (CX_45001, CX_1). Those are rejected here and the host
        # label is used as before, so this only ever improves on the old answer. name_is_sluglike
        # in web.py remains the second gate on whatever comes out.
        guess = ""
        if site and not re.match(r"^CX[_-]?\d*$", site, re.I):
            guess = _TITLE_DECOR.sub(" ", _name_from(site)).strip()
            guess = re.sub(r"\s+", " ", guess)
        return ("%s/hcmUI/CandidateExperience/en/sites/%s" % (origin, site),
                "oracle", guess or _name_from(host.split(".")[0]))

    if host_is(host, "workable.com"):
        slug = _workable_slug(url)
        if slug:
            return ("https://apply.workable.com/%s" % slug, "workable", _name_from(slug))

    if re.match(r"recruiting\d*\.ultipro\.com$", host):          # recruiting / recruiting2 / …
        base = _ultipro_base(url)
        if base:
            return (base, "ultipro", _name_from(urlparse(base).path.split("/")[1]))

    if host == JOBVITE_HOST or host.endswith(".jobvite.com"):
        # /<slug>/jobs, /<slug>/job/<id>, /<slug>/job/<id>/apply and /careers/<slug>/jobs all
        # normalize to the tenant root. "careers" is a path PREFIX on some tenants, not a slug.
        rest = segs[1:] if (segs and segs[0] == "careers") else segs
        slug = rest[0] if rest else ""
        if slug and slug not in ("job", "jobs"):
            # Same initialism rule as the PeopleSoft branch: these slugs are short and often an
            # acronym ("dwt" is Davis Wright Tremaine), which _name_from title-cases into "Dwt".
            name = slug.upper() if (len(slug) <= 4 and slug.isalpha()) else _name_from(slug)
            return ("https://%s/%s" % (JOBVITE_HOST, slug), "jobvite", name)

    if host.endswith(".bamboohr.com"):
        sub = host.split(".")[0]
        return ("https://%s.bamboohr.com" % sub, "bamboohr", _name_from(sub))

    if host.endswith(".pinpointhq.com"):
        sub = host.split(".")[0]
        return ("https://%s.pinpointhq.com" % sub, "pinpoint", _name_from(sub))

    if host == "ats.rippling.com" and segs:
        return ("https://ats.rippling.com/%s/jobs" % segs[0], "rippling", _name_from(segs[0]))

    if host.endswith(".avature.net"):
        portal = segs[0] if segs else "jobs"                # <tenant>.avature.net/<portal>/SearchJobs
        return ("https://%s/%s/SearchJobs" % (host, portal), "avature",
                _name_from(host.split(".")[0]))

    if host_is(host, "jobdiva.com"):                        # www1.jobdiva.com/portal/?a=<token>
        token = (parse_qs(p.query).get("a") or [""])[0]
        if token:
            return ("https://www1.jobdiva.com/portal/?a=%s" % token, "jobdiva",
                    _jobdiva_agency(token) or "JobDiva portal")

    # PeopleSoft Candidate Gateway: any /ps[cp]/<site>/…/HRS_HRAM_FL.HRS_CG_SEARCH_FL.GBL URL,
    # whichever page of it the user happened to copy (search, one posting, the portal frame).
    # Matched on the component name, not the host, since every institution self-hosts.
    if _PS_GBL_RE.search(url or "") or ("HRS_HRAM_FL" in (p.path or "")
                                       and re.search(r"/ps[cp]/[^/]+/", p.path or "")):
        origin, site = _peoplesoft_parts(url)
        if origin and site:
            label = [x for x in host.split(".")
                     if x not in ("www", "jobs", "careers", "omni", "edu", "org", "com")]
            name = label[0] if label else host
            # These are mostly universities, whose domain label IS an acronym — _name_from
            # would title-case "fsu" into "Fsu". Anything this short is an initialism.
            name = name.upper() if (len(name) <= 4 and name.isalpha()) else _name_from(name)
            return ("%s/psc/%s%s" % (origin, site, _peoplesoft_gbl(url)),
                    "peoplesoft", name)

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
            # Only hand off to Workday if that board actually HAS postings. The apply link on a
            # Phenom job routes to whatever Workday site handles applications, which is not
            # always the site the jobs are listed under: UVA's Phenom front-end serves 897 jobs
            # while uva.wd1/UVAJobs behind it reports total=0. Returning the empty board made
            # discover() probe 0 and drop the company, so a live 897-job board was invisible.
            if wd:
                try:
                    if (probe_board(wd[0], wd[1]) or 0) > 0:
                        return wd
                except Exception:
                    pass
        host = p.netloc.split(":")[0]
        parts = [x for x in host.split(".")
                 if x not in ("www", "careers", "jobs", "career", "mycareer")]
        return (base, "phenom", _name_from(parts[0]) if parts else host)
    except Exception:
        return None


def detect_successfactors(url):
    """Network probe for SAP SuccessFactors 'Career Site Builder' sites — custom
    domains (jobs.<co>.com). Classic sites give themselves away with a server-rendered
    /search/ results table (tr.data-row). Newer jobs2web/RMK sites render results
    client-side (no table) but still ship the tell-tale SuccessFactors assets and a
    /job/ sitemap — scrape_successfactors handles both. Returns
    (origin, 'successfactors', name) when either fingerprint is present."""
    url = (url or "").strip()
    if not url:
        return None
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    p = urlparse(url)
    base = "%s://%s" % (p.scheme, p.netloc)
    try:
        r = _safe_get(base + "/search/?q=&startrow=0", timeout=12)
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, "lxml")
        classic = bool(soup.select_one("tr.data-row a.jobTitle-link"))
        if not classic:
            low = r.text.lower()
            # client-rendered SF: SuccessFactors RMK markers + a job sitemap to enumerate
            if not (("successfactors" in low or "rmkcdn" in low) and "j2w" in low):
                return None
            try:
                sm = _safe_get(base + "/sitemap.xml", timeout=12)
                if sm.status_code != 200 or "/job/" not in sm.text:
                    return None
            except Exception:
                return None
        host = p.netloc.split(":")[0]
        parts = [x for x in host.split(".")
                 if x not in ("www", "careers", "jobs", "career", "us")]
        return (base, "successfactors", _name_from(parts[0]) if parts else host)
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
      | [a-z0-9.-]+/hcmUI/CandidateExperience[A-Za-z0-9_/.-]*/sites/[A-Za-z0-9_]+
      | recruiting\d*\.ultipro\.com/[A-Za-z0-9_-]+/JobBoard/[0-9a-fA-F-]{36}
      | [a-z0-9-]+\.bamboohr\.com/careers
      | [a-z0-9-]+\.pinpointhq\.com
      | ats\.rippling\.com/[A-Za-z0-9_-]+
      | [a-z0-9-]+\.avature\.net/[A-Za-z0-9_-]+
      | www\d*\.jobdiva\.com/portal/\?a=[A-Za-z0-9]+
      | recruiting\.paylocity\.com/recruiting/jobs/All/[0-9a-fA-F-]{36}/[A-Za-z0-9_-]+
      | [a-z0-9.-]+/ps[cp]/[A-Za-z0-9_]+/[A-Za-z0-9_]+/HRMS/c/HRS_[A-Za-z0-9_.]+
    )""", re.X | re.I)
# Four platforms we can already SCRAPE were missing from the link list above, so a careers page
# that linked straight to one was read as "no board found":
#   ultipro   the host is numbered — Starkey is recruiting2.ultipro.com — and the pattern was
#             pinned to the bare `recruiting.` host, so every numbered tenant was invisible.
#   avature / jobdiva / paylocity  had fetchers and detect_board support but no link pattern.
# Found by fingerprinting the 71 careers-page-only companies from the E-Verify+ probe: the
# platforms behind them were overwhelmingly ones we support, not ones we lack.
# Same lesson again on 2026-08-24, fingerprinting 473 careers-page-only companies from
# the LinkedIn/Indeed sweeps -- two more of ours were missing:
#   peoplesoft  absent from this list entirely, so every university that LINKS to its
#               /psc/<site>/EMPLOYEE/HRMS/c/HRS_... search page read as no board. These
#               are the CAP-EXEMPT employers, i.e. the ones that matter most here.
#   oracle      pinned to the .oraclecloud.com host, which missed every vanity-domain
#               deployment. careers.autozone.com/hcmUI/... is the same product and
#               _oracle_parts already parsed it -- the board read 10,172 postings while
#               detect_board called it 'not an ATS'. The PATH is the product-specific
#               part, so match on that instead.


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
    # A careers page that REDIRECTS to its ATS is the most common miss, and the cheapest
    # to fix: the landing URL names the vendor outright (ntrs.wd1.myworkdayjobs.com,
    # eaton.eightfold.ai) while the SPA it serves carries no _ATS_LINK_RE match at all, so
    # the link scan below finds nothing and the company is reported as unclassifiable.
    # Measured 2026-08-23 over 473 employers whose careers page loaded but resolved to no
    # board: 26 of the top 120 were already on a platform in SCRAPERS, mostly behind a
    # Workday or Eightfold redirect. Checked FIRST because it is free -- the fetch that
    # would tell us has already happened -- and it cannot regress the non-redirect case,
    # where r.url is the careers host and detect_board returns None for it anyway.
    det = detect_board(str(getattr(r, 'url', '') or url))
    if det:
        return det
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


def detect_eightfold(url):
    """Network probe for Eightfold boards served from the company's OWN host.

    detect_board only matches *.eightfold.ai, so a vanity deployment reads as "not an ATS"
    even though the adapter can scrape it perfectly: Netflix serves
    explore.jobs.netflix.net and yields 509 postings, 185 of them on-target.

    The `domain` query param is required and is NOT always the host's own domain -- Netflix
    answers for netflix.com while being served from netflix.net -- so candidates are tried in
    order and the first that returns positions wins.

    Expect 403 far more often than 200. Eightfold gates per tenant: Microsoft, Qualcomm,
    Ericsson, Eaton, Lumen and TriNet all refuse this same call, so a None here is the normal
    outcome and not a sign the probe is broken.
    """
    url = (url or "").strip()
    if not url:
        return None
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    host = (urlparse(url).netloc or "").split(":")[0]
    if not host:
        return None
    labels = [x for x in host.split(".") if x]
    reg = ".".join(labels[-2:]) if len(labels) >= 2 else host
    stem = labels[-2] if len(labels) >= 2 else host
    cands = []
    for d in (reg, stem + ".com"):
        if d and d not in cands:
            cands.append(d)
    # Both endpoint spellings, for the same reason scrape_eightfold tries both: a tenant on
    # the newer front-end 403s /api/apply/v2/jobs and answers /api/pcsx/search. Microsoft is
    # that case, and probing only the classic path reported it as "not an ATS" while the
    # scraper could read 1,077 US postings from it.
    paths = ("/api/apply/v2/jobs", "/api/pcsx/search")
    for dom in cands:
      for path in paths:
        try:
            r = SESSION.get("https://%s%s" % (host, path), headers=HEADERS, timeout=12,
                            params={"domain": dom, "hl": "en", "start": 0, "num": 1})
        except Exception:
            continue
        if r.status_code != 200:
            continue
        try:
            d = r.json()
            if isinstance(d.get("data"), dict):        # pcsx nests one level down
                d = d["data"]
        except Exception:
            continue
        if not isinstance(d, dict) or not (d.get("positions") or d.get("count")):
            continue
        label = [x for x in labels if x not in ("www", "jobs", "careers", "career",
                                                  "explore", "apply", "com", "net", "org")]
        name = _name_from(label[0]) if label else host
        return ("https://%s/careers?domain=%s" % (host, dom), "eightfold", name)
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
            # _safe_post, NOT a raw SESSION.post. This host comes from a URL a user pasted into
            # /add, and this was the one probe arm that skipped the hardened helpers entirely —
            # so a hostname crafted to look like Workday reached whatever it resolved to, with
            # no public-IP check, no redirect refusal and no size cap. The peoplesoft arm below
            # already used _safe_get, which is why that half of the same finding was caught at
            # probe time and this half was not.
            r = _safe_post(cxs, {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
                           timeout=12)
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
        if ats_type == "ultipro":
            base = _ultipro_base(board_url)
            if not base:
                return None
            r = _safe_post(base + "/JobBoardView/LoadSearchResults",
                           _ultipro_body(1, 0), timeout=12)
            return (r.json() or {}).get("totalCount") if r.status_code == 200 else None
        if ats_type == "successfactors":
            p = urlparse(board_url)
            r = _safe_get("%s://%s/search/?q=&startrow=0" % (p.scheme or "https", p.netloc),
                          timeout=12)
            if r.status_code != 200:
                return None
            m = re.search(r"Results\s+\d+\s*\S{0,3}\s*\d+\s+of\s+([\d,]+)", r.text)
            if m:
                return int(m.group(1).replace(",", ""))
            soup = BeautifulSoup(r.text, "lxml")
            n = len(soup.select("tr.data-row a.jobTitle-link"))
            if n:
                return n
            # Client-rendered tenant: no table to count. scrape_successfactors already
            # falls back to the sitemap, so counting anything else here would refuse a
            # board we can actually read. Wipro: 402 US postings, previously None.
            return len(_csb_sitemap_us_locs("%s://%s" % (p.scheme or "https", p.netloc))) or None
        if ats_type == "eightfold":
            # No count endpoint worth trusting here: the classic path 403s on newer tenants
            # and probe_board had no eightfold branch at all, so every one of them counted
            # as None and could never be adopted. Defer to the scraper, which already knows
            # about both endpoints -- same shape as the paylocity and pinpoint branches.
            return len(scrape_eightfold(board_url)) or None
        if ats_type == "jsonld_sitemap":
            # Sitemap-only count: no per-posting fetch, so validating is one request.
            base = re.match(r"^(https?://[^/]+)", board_url or "")
            if not base:
                return None
            return len([1 for _i, s, _u in _jlsm_sitemap_jobs(base.group(1))
                        if title_verdict(s)[0]]) or None
        if ats_type == "apple":
            # totalRecords off page one: no need to walk the board to validate it.
            r = _safe_get(APPLE_SEARCH, timeout=25,
                          params={"location": APPLE_LOCATION, "page": 1})
            blk = _apple_results(_apple_hydration(r.text) or {}) or {}
            return int(blk.get("totalRecords") or 0) or None
        if ats_type == "deloitte":
            return len(scrape_deloitte(board_url)) or None
        if ats_type == "ibm":
            r = SESSION.post(IBM_SEARCH_API, json=_ibm_body(0), timeout=25,
                             headers=dict(HEADERS, **{"Content-Type": "application/json",
                                                      "Accept": "application/json",
                                                      "Referer": "https://www.ibm.com/"}))
            if r.status_code != 200:
                return None
            h = (r.json() or {}).get("hits") or {}
            return ((h.get("total") or {}).get("value")) or None
        if ats_type == "google":
            # The sitemap count is the honest one: it needs no per-posting fetch.
            return len([1 for _i, s, _u in _google_sitemap_jobs()
                        if title_verdict(s)[0]]) or None
        if ats_type == "paylocity":
            return len(scrape_paylocity(board_url)) or None
        if ats_type == "peoplesoft":
            origin, site = _peoplesoft_parts(board_url)
            ps_gbl = _peoplesoft_gbl(board_url)
            if not origin:
                return None
            _safe_get("%s/psp/%s%s?Page=HRS_APP_SCHJOB_FL&Action=U&SiteId=1&FOCUS=Applicant"
                      % (origin, site, ps_gbl), timeout=15)   # guest cookie first
            r = _safe_get("%s/psc/%s%s?Page=HRS_APP_SCHJOB_FL&Action=U"
                          % (origin, site, ps_gbl), timeout=20)
            if r.status_code != 200:
                return None
            m = _PS_TOTAL_RE.search(re.sub(r"<[^>]+>", " ", r.text))
            # The stated total beats the row count: the grid only renders its first 50.
            return int(m.group(1).replace(",", "")) if m else (len(_ps_rows(r.text)) or None)
        if ats_type == "jobdiva":
            token = _jobdiva_token(board_url)
            jh = _jobdiva_session(token) if token else None
            if not jh:
                return None
            r = SESSION.get(JOBDIVA_API + "job/listall?portaltype=1&count=1",
                            headers=jh, timeout=12)
            return r.json().get("total") if r.status_code == 200 else None
        if ats_type == "avature":
            base = _avature_base(board_url)
            r = SESSION.get("%s/?jobOffset=0" % base, headers=HEADERS, timeout=12)
            if r.status_code != 200:
                return None
            m = re.search(r"of\s+([\d,]+)", r.text)         # 'Displaying 10 of 999+' (count is capped)
            if m:
                return int(m.group(1).replace(",", ""))
            return len(BeautifulSoup(r.text, "lxml").select("li.listSingleColumnItem")) or None
        if ats_type == "bamboohr":
            d = _get_json("https://%s.bamboohr.com/careers/list" % _sub(board_url))
            return (d.get("meta") or {}).get("totalCount", len(d.get("result") or []))
        if ats_type == "pinpoint":
            return len(scrape_pinpoint(board_url))
        if ats_type == "rippling":
            m = re.search(r"ats\.rippling\.com/([^/?#]+)", board_url or "")
            if not m:
                return None
            r = _safe_get("https://ats.rippling.com/%s/jobs" % m.group(1), timeout=15)
            if r.status_code != 200:
                return None
            mm = _NEXT_DATA_RE.search(r.text)
            if not mm:
                return None
            try:
                d = json.loads(mm.group(1))
                for q in ((d.get("props", {}).get("pageProps", {})
                           .get("dehydratedState") or {}).get("queries") or []):
                    if "job-posts" in json.dumps(q.get("queryKey") or []):
                        return ((q.get("state") or {}).get("data") or {}).get("totalItems")
            except Exception:
                return None
            return None
        if ats_type == "workable":
            return len(scrape_workable(board_url))
        if ats_type == "recruitee":
            return len(scrape_recruitee(board_url))
        if ats_type == "breezy":
            return len(scrape_breezy(board_url))
        if ats_type == "personio":
            return len(scrape_personio(board_url))
        if ats_type == "jobvite":
            return len(scrape_jobvite(board_url))
        if ats_type == "aquent":
            # No count endpoint to ask, so read the feed -- the same shape workable, recruitee,
            # personio and jobvite above use. Without this the /add and extension routes refuse
            # an Aquent link on a falsy probe count, even though detect_board resolved it.
            return len(scrape_aquent(board_url))
        if ats_type == "jsonld":
            return len(scrape_jsonld(board_url))
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

# ---------------------------------------------------------------------------------------------
# "Manager, Projects" -- the reversed form INCLUDE cannot see.
#
# Every one of the 198 INCLUDE phrases reads "thing role" ("project manager"), and a good many
# employers write the head first with a comma: Disney posted "Manager, Projects" in Celebration FL
# and the filter dropped it with "no PM/coordinator/analyst/software keyword". Reported by Kunal
# 2026-08-20 from a disneycareers.com link; the board itself was already being scraped (114 Disney
# rows in the corpus), so this was never a coverage gap, only a filter blind spot.
#
# A SEPARATE, COMMA-ANCHORED pattern rather than new INCLUDE entries, and rather than teaching
# _make_matcher to treat punctuation as a separator. Both alternatives were measured on 1,493 US
# titles across the Disney, Capital One and Salesforce boards:
#
#   punctuation-as-separator in _make_matcher   -> +0 rows. The comma is not what blocks these;
#                                                  the word ORDER is. Not shipped: an unmeasured
#                                                  matcher change is how a flood gets in.
#   a loose "(role), (thing)" with bare product/data
#                                               -> +13 rows, SIX of them off-target -- it swept in
#                                                  Product Design, Product Marketing and Product
#                                                  Architecture, which are not product management.
#   this pattern (whole role phrases only)      -> +2 rows, both on-target, 0 false positives.
#
# The thing-list is deliberately only unambiguous role phrases. "product" alone is the trap: it
# reads as design/marketing/architecture far more often than as product management. Keeping the
# head adjacent to the comma matters too -- it is why "Senior Analyst, Product & Pricing
# Operations" does not match on a stray "operations" three words later.
_REVERSED_RE = re.compile(
    r"\b(manager|director|lead|specialist|coordinator|analyst|administrator)\s*,\s*"
    r"(projects?|programs?|operations|project management|program management|"
    r"product management|project controls?|scrum)\b", re.I)


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


def apply_resume_terms():
    """Fold resume.txt's derived phrases into the live title matcher. Returns them.

    Called by main() before the keep loop, and by ANY other process that needs to reproduce the
    filter that actually ran -- notably web.py, which asks title_verdict whether a stored row got
    in on its title in order to badge the ones that got in on their description. Without this the
    feed would judge against the base list, and every row admitted by a résumé-derived phrase
    would be mislabelled "matched on description".
    """
    global _INCLUDE_RE
    extra = resume_terms()
    if extra:
        _INCLUDE_RE = _make_matcher(tuple(INCLUDE) + tuple(extra))
    return extra


def title_verdict(title):
    """Judge a posting by its TITLE alone. Returns (keep, reason) so a VERBOSE run
    shows exactly why each title survived or was dropped — makes tuning easy."""
    bad = _EXCLUDE_RE.search(title)
    if bad:
        return False, "off-target function ('%s')" % bad.group(0)
    good = _INCLUDE_RE.search(title)
    if good:
        return True, "matched '%s'" % good.group(0)
    # The reversed "Manager, Projects" form. Checked AFTER the forward list so the reason string
    # keeps naming the forward phrase whenever one matched, and after EXCLUDE so it can never
    # re-admit something the exclude list turned away.
    rev = _REVERSED_RE.search(title)
    if rev:
        return True, "matched reversed '%s'" % rev.group(0)
    return False, "no PM/coordinator/analyst/software keyword"


def is_entry_level(title):
    """Back-compat: title-only boolean (ignores location)."""
    return title_verdict(title)[0]


# ---------------------------------------------------------------------------------------------
# THE REJECT DUMP. See DUMP_REJECTS near the top for why it exists at all.
_reject_fh = [None]


def dump_reject(title, company, location, url, reason):
    """Append one dropped posting to DUMP_REJECTS. A no-op unless that env var is set."""
    if not DUMP_REJECTS:
        return
    fh = _reject_fh[0]
    if fh is None:
        # TRUNCATE PER PROCESS, not per board. A sweep is one process; opening in append mode
        # would silently mix two different filters' verdicts into one file, and attributing a
        # verdict to a filter is the entire point of this file.
        fh = _reject_fh[0] = open(DUMP_REJECTS, "w", encoding="utf-8", newline="")
        fh.write("title\tcompany\tlocation\turl\treason\n")
    fh.write("\t".join(_dump_field(v) for v in (title, company, location, url, reason)) + "\n")


def _dump_field(v):
    """One TSV cell. Collapses all whitespace, because job titles really do contain tabs and
    newlines and either one would shift every column after it."""
    return re.sub(r"\s+", " ", str(v or "")).strip()


def close_reject_dump():
    """Flush and close the dump. Safe when nothing was ever opened."""
    if _reject_fh[0] is not None:
        _reject_fh[0].close()
        _reject_fh[0] = None


# ---------------------------------------------------------------------------------------------
# THE SECOND OPINION FOR BOARDS THAT DO NOT HAND A DESCRIPTION OVER.
#
# greenhouse, lever, ashby, jibe, recruitee and pinpoint return the description in the same
# response the sweep already reads, so those postings reach the keep loop with a "jd" and the
# description rule costs nothing. That is 655 of the 1,173 boards in SOURCES.
#
# The other 518 -- workday (234), smartrecruiters (128), successfactors (83), oracle (33),
# phenom (20, teaser only) and the long tail -- publish the description at a SEPARATE URL, one
# request per posting. Fetching all of them is out of the question: a full sweep leaves ~213,000
# postings on the floor, and a request each is not a scrape, it is a crawl.
#
# So this is targeted and bounded, and both halves matter:
#
#   TARGETED. Only postings that (a) are new this run, (b) failed the title filter for want of a
#   keyword rather than on an EXCLUDE hit, and (c) pass core.pm_title_gate -- the same cheap
#   title hint the description rule applies anyway. Measured on 400 title-rejected Workday /
#   SmartRecruiters postings: the gate cuts the pool to 5.75% of eligible drops, and of what it
#   does fetch roughly one in six is rescued. Without the gate it is one in eleven AND the wins
#   are things like "Regional Sales Director" -- more requests for a worse feed.
#
#   BOUNDED. A hard ceiling per run and a lower one per employer, because the first run after
#   this ships sees a whole corpus of unseen postings rather than a day's worth, and because one
#   40,000-posting Workday tenant must not spend the entire budget.
#
# It reuses score_jobs.detail_jd, which already knows every ATS this project can read and has the
# shell guard that keeps "You need to enable JavaScript" out of the corpus. Imported lazily
# because score_jobs imports scraper at module load: by the time main() calls this, this module
# is fully initialised and the circular import resolves.
JD_LOOKUP_BUDGET = int(os.environ.get("JD_LOOKUP_BUDGET") or 1200)
JD_LOOKUP_PER_BOARD = int(os.environ.get("JD_LOOKUP_PER_BOARD") or 60)
JD_LOOKUP_WORKERS = int(os.environ.get("JD_LOOKUP_WORKERS") or 8)
# AND A CEILING ON TIME, because the one above is a ceiling on REQUESTS and those are not the
# same thing -- the lesson SCRAPE_BUDGET_MIN, SCORE_BUDGET_MIN and SCORE_ANALYZE_BUDGET_MIN were
# each added to learn separately. MEASURED 2026-08-21: 1,200 fetches at 8 workers took 4.5 min
# (~1.8 s each), which put the whole scrape step at 26.2 min against a timeout-minutes of 26.
# The runner killed it 0.14 s after it printed its last line, and because GitHub skips every
# later step once one fails, that one overrun also cost the score pass and the digest.
JD_LOOKUP_BUDGET_MIN = float(os.environ.get("JD_LOOKUP_BUDGET_MIN") or 3)


def fill_missing_jds(scraped, seen, blocked):
    """Fetch descriptions for the postings a second opinion could plausibly rescue.

    Mutates rows in place, setting j["jd"]. Returns (fetched, rescued_candidates) for the run
    summary. Never raises: a JD lookup failing is a posting that drops on its title, which is
    exactly what would have happened without this pass.
    """
    if JD_LOOKUP_BUDGET <= 0:
        return 0, 0
    per_board, want = {}, []
    for j in scraped:
        if len((j.get("jd") or "").strip()) >= core._MIN_JD_CHARS:
            continue                                  # the board already gave us one
        url = canonical_url(j.get("url", ""))
        if not url or url.lower() in seen:
            continue                                  # already in the corpus: never re-judged
        if blocked and db.block_key(j.get("company", "")) in blocked:
            continue
        title = j.get("title") or ""
        # THE US GATE, APPLIED EARLY AND ONLY HERE. In the keep loop it deliberately runs after
        # the title verdict, so a rejected posting never reaches it -- which means a budget that
        # skipped this check would spend itself on jobs the loop is about to drop anyway. It is
        # not a theoretical worry: the first smoke test of this pass spent 21 of 80 fetches on GE
        # Vernova's French and German reqs ("Directeur de projet", "Automation Operations Leader
        # (f/m/d)"), every one of them rescued by the description and then dropped as non-US.
        if US_ONLY and (not is_us_location(j.get("location", ""))
                        or title_says_non_us(title)):
            continue
        keep, why = title_verdict(title)
        if keep or why.startswith("off-target"):
            continue                                  # kept already, or vetoed and not ours to
        if not core.pm_title_gate(title):             # overturn
            continue
        c = j.get("company", "")
        if per_board.get(c, 0) >= JD_LOOKUP_PER_BOARD:
            continue
        per_board[c] = per_board.get(c, 0) + 1
        want.append(j)
        if len(want) >= JD_LOOKUP_BUDGET:
            break
    if not want:
        return 0, 0

    from . import score_jobs                           # lazy: see the note above
    print("JD lookup: fetching %d description(s) for title-rejected postings across %d employer(s)"
          % (len(want), len(per_board)))

    def one(j):
        try:
            _u, jd, _d = score_jobs.detail_jd(canonical_url(j.get("url", "")))
            return j, jd or ""
        except Exception:
            return j, ""

    # ex.map WOULD NOT HAVE SURVIVED A `break`, which is why this is submit/as_completed rather
    # than the obvious two-line edit: map submits every future up front, so the `with` block's
    # shutdown(wait=True) drains all 1,200 of them on the way out no matter where the loop
    # stopped. Only shutdown(cancel_futures=True) drops the queued work. The requests already in
    # flight still finish, so the overshoot is one fetch per worker, not one pass. cancel_futures
    # needs Python 3.9, which is what cPanel runs (bin/cron_scrape.sh) -- that is the floor here.
    #
    # Truncating is the same no-op the docstring promises for a failed fetch: an unfetched row
    # drops on its title, and since it was never stored it is offered again on the next run.
    got, tried = 0, 0
    deadline = (time.monotonic() + JD_LOOKUP_BUDGET_MIN * 60) if JD_LOOKUP_BUDGET_MIN > 0 else None
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=JD_LOOKUP_WORKERS)
    try:
        futures = [ex.submit(one, j) for j in want]
        try:
            for f in concurrent.futures.as_completed(
                    futures,
                    timeout=None if deadline is None else max(0.1, deadline - time.monotonic())):
                j, jd = f.result()
                tried += 1
                if len(jd) >= core._MIN_JD_CHARS:
                    j["jd"] = jd
                    got += 1
        except concurrent.futures.TimeoutError:
            # NOT the builtin: on 3.9 concurrent.futures.TimeoutError is its own class and is not
            # a subclass of builtins.TimeoutError. On 3.11+ it is an alias, so this covers both.
            print("  !! JD lookup budget of %g min ran out after %d of %d fetch(es) -- the rest"
                  " drop on their titles and are offered again next run."
                  % (JD_LOOKUP_BUDGET_MIN, tried, len(want)))
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    # `tried`, not len(want): the caller prints this as "N of M returned a usable description",
    # and M has to be what was actually asked for or a truncated pass reads as a failure rate.
    return tried, got


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
# Matched as WHOLE WORDS via _NON_US_RE below (never substrings).
NON_US = {"india", "united kingdom", "uk", "canada", "ireland", "germany", "france",
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
    "guadalajara", "monterrey", "bogota", "medellin", "lima", "cebu",
    # --- Added 2026-08-16 after measuring the new Capgemini board: 157 of the 516 rows that
    # passed the US filter (30%) were foreign, because a two-letter code is ambiguous and these
    # cities carry ONLY the code. Casablanca reads as Massachusetts, Mississauga and Calgary as
    # California, Buenos Aires as Arkansas, Kolkata as Indiana. Naming the city is what breaks
    # the tie — every one of these is unambiguous, unlike (say) Ottawa, which is also a real
    # town in Illinois and Kansas and is therefore deliberately NOT here.
    "casablanca", "rabat", "marrakech", "sala al jadida", "buenos aires", "mississauga",
    "calgary", "winnipeg", "edmonton", "ottawa, on", "quebec", "kolkata", "calcutta",
    "ahmedabad", "kochi", "coimbatore", "jaipur", "santiago", "montevideo", "quito",
    "san jose, cr", "cairo", "nairobi", "lagos"}

_STATE_ABBR_RE = re.compile(r",\s*([A-Za-z]{2})\b")

# Whole-word matcher for the NON_US list. Substring matching burned us: 'india' is
# inside 'Indianapolis', so every Indianapolis job was silently dropped (found
# 2026-06-12). Word boundaries also let bare 'UK' match at the start of a string.
_NON_US_RE = re.compile(
    r"\b(?:%s)\b" % "|".join(sorted((re.escape(t.strip()) for t in NON_US),
                                    key=len, reverse=True)))


def _fold(loc):
    """Lowercased and stripped of accents, for matching against NON_US.

    The list is written in ASCII and the boards are not: EY publishes 'Medellín, Antioquía, CO'
    and 'medellin' does not match it, so the row fell through to the alpha-2 test and Colombia
    read as Colorado. Folding here rather than adding accented spellings to the list, because
    the accented forms are open-ended (Medellín, Bogotá, São Paulo, México, Málaga, Kraków…) and
    a list you have to remember to double is a list that will be wrong again."""
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", (loc or "").lower())
                   if not unicodedata.combining(c))


_TITLE_PLACE_RE = re.compile(r"[(\[]([^)\]]{2,40})[)\]]\s*$")


def _country_from_title(title):
    """A place named in a trailing parenthetical, e.g. "Data Analyst (Remote, India)" -> the
    text inside. "" when the title has none, which leaves is_us_location's blank case alone.

    Deliberately only the LAST bracketed group and only at the end."""
    m = _TITLE_PLACE_RE.search(title or "")
    return m.group(1).strip() if m else ""


def title_says_non_us(title):
    """True only when a title's trailing parenthetical NAMES a non-US place.

    VETO ONLY, and that asymmetry is the point. Feeding the parenthetical to is_us_location
    instead looks equivalent and is not: that function answers False for anything it does not
    recognise as American, so "Data Analyst (Senior)" and "(Contract)" came back non-US and
    would have been dropped. This asks the one question worth asking -- does this text match the
    NON_US list -- and stays silent otherwise."""
    place = _country_from_title(title)
    return bool(place) and bool(_NON_US_RE.search(_fold(place)))


def is_us_location(loc):
    """Heuristic: True if the location looks US-based. Unknown/blank -> kept."""
    if not loc:
        return True
    low = _fold(loc)                                # accent-folded: see _fold's docstring
    if re.search(r"\b\d+\s+locations?\b|multiple locations?", low):
        return True                                 # bare 'N Locations' count -> unknown, keep
    if _NON_US_RE.search(low):                      # explicit non-US signal -> drop
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


# Apostrophes are DELETED, not turned into a space. Every other punctuation mark separates two
# words ("Avery-Dennison" is two words); an apostrophe is inside one, and replacing it with a
# space split a whole class of employers into a name plus a stray "s":
#
#     Kohl's                              -> 'kohl s'    (misses; 'kohls' has 135 filings)
#     Domino's Pizza                      -> 'domino s pizza'
#     BJ's Wholesale Club                 -> 'bj s wholesale club'
#     Children's Hospital of Philadelphia -> 'children s hospital of philadelphia'
#
# The DOL/USCIS source rows mostly spell these WITHOUT the apostrophe, so the two sides could
# never meet and these employers rendered as "no sponsorship record" — the exact inversion the
# "colour means sponsorship" rule in CLAUDE.md exists to prevent, for exactly the employers an
# F-1 candidate most needs to see. Both curly and straight forms, because scraped board text
# uses both.
#
# AFTER CHANGING THIS, REBUILD THE INDEXES: `python -m scraper.build_sponsor_counts` and the
# visa_tags build. sponsor_counts.json was written by the OLD rule and still carries 1,316 keys
# with a stray-s token ('a s engineers', '505 games u s'), so the index is polluted on its side
# too until it is regenerated.
_APOSTROPHE_RE = re.compile(r"[’ʼ']")


def _norm_name(s):
    """Normalize a company name for matching: lowercase, drop apostrophes, strip the remaining
    punctuation and common legal suffixes. 'Avery-Dennison Corp.' -> 'avery dennison',
    "Kohl's" -> 'kohls'."""
    s = _APOSTROPHE_RE.sub("", s.lower())
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = _LEGAL_SUFFIX.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


# Legal suffixes safe to strip when matching a SELF-REPORTED federal row against the sponsor
# indexes. Deliberately much shorter than _LEGAL_SUFFIX above: that one also eats "group",
# "labs", "technologies" and "co". Dropping those is fine for our own hand-curated names, but
# pointed at 35k rows of whatever an HR person typed at E-Verify enrolment it collapses
# genuinely different companies onto a big-name key — "Target Labs INC" (5-9 staff, VA) becomes
# "target" and inherits Target Corp's 1,411 filings; "Box Technology Group" becomes "box".
_STRICT_SUFFIX = re.compile(
    r"\b(?:inc|incorporated|llc|l l c|corp|corporation|ltd|limited|llp|plc|pllc)\b")

# Upper bound of each USCIS "Workforce Size" band. None = open-ended (no plausibility cap).
_WORKFORCE_CEILING = {
    "5 to 9": 9, "10 to 19": 19, "20 to 99": 99, "100 to 499": 499, "500 to 999": 999,
    "1,000 to 2,499": 2499, "2,500 to 4,999": 4999, "5,000 to 9,999": 9999,
    "10,000 and over": None,
}

# How many cumulative filings we'll believe per head before calling a match a collision.
# The indexes are ~15 years deep, so 3x the band's headcount ceiling is already generous.
_PLAUSIBLE_FILINGS_PER_HEAD = 3

# Words too generic to prove that an employer and its DBA are the same entity. Without these,
# "CKS Pizza llc" claims Domino's filings through the shared word "pizza", and "North Wheeler
# County Hospital District" claims Parkview Hospital's through "hospital".
_GENERIC_NAME_WORDS = frozenset((
    "pizza", "restaurant", "cafe", "coffee", "food", "hospital", "health", "healthcare",
    "medical", "clinic", "care", "dental", "pharmacy", "group", "holdings", "holding",
    "management", "services", "service", "systems", "system", "solutions", "center",
    "centre", "auto", "parts", "tech", "technology", "technologies", "company", "enterprise",
    "enterprises", "industries", "associates", "partners", "consulting", "international",
    "national", "global", "american", "america", "usa", "the", "and", "of", "school",
    "university", "college", "store", "market", "shop", "construction", "transport",
    "trucking", "logistics", "staffing", "insurance", "bank", "financial",
))


def _strict_norm_name(s):
    """_norm_name's cautious twin: lowercase and strip punctuation, but keep the words that
    distinguish one company from another. 'Target Labs INC' -> 'target labs' (not 'target')."""
    s = re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())
    s = _STRICT_SUFFIX.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def _safe_sponsor_match(employer, counts, workforce_size=None, dba=None):
    """Look up an employer's H-1B filing count with the two false positives guarded.

    Returns (count, method) where method is one of:
        "employer"       matched on the employer name, cautious normalisation
        "employer_loose" only matched once the aggressive normaliser was allowed (see below)
        "dba"            matched via Doing-Business-As, and the DBA is a variant of that name
        "implausible"    a name matched but the volume is impossible for the headcount
        ""               no match

    Guards the three ways naive matching goes wrong on the federal E-Verify list:

    1. SUFFIX COLLISIONS — handled by _strict_norm_name (see above).
    2. FRANCHISEE INHERITANCE — most rows that match only through Doing-Business-As are
       franchise operators, not the filer: "Balde Restaurant Group LLC" (dba McDonalds),
       "Calixto Franchise Association" (dba Dominos Pizza), every 7-Eleven franchisee. So a
       DBA match is only trusted when the DBA shares a word with the employer name, i.e. it
       is the same entity under a shorter trading name ("Equinox Holdings, Inc" / "Equinox",
       "University of Washington, College of Engineering" / "University of Washington").
    3. IMPLAUSIBLE VOLUME — a backstop for whatever the first two miss. 320 filings against a
       nine-person headcount ceiling is not believable regardless of how the name matched.

    Note the asymmetry the loose fallback exists for: `counts` was keyed with _norm_name, so a
    strict lookup alone silently MISSES real sponsors — "Ford Motor Company" is stored under
    "ford motor". We therefore retry with _norm_name and let the size check adjudicate: Ford
    (10,000+, uncapped) is kept, while "Target Labs INC" resolving to "target" is thrown out by
    1,411 filings against a nine-person ceiling. Those retries are reported separately so a
    reviewer can see which matches leaned on the risky normaliser.

    `workforce_size` is the raw USCIS band string; pass None to skip the plausibility check.
    """
    if not counts or not employer:
        return 0, ""
    method = ""
    n = 0
    key = _strict_norm_name(employer)
    if key:
        n = int(counts.get(key) or 0)
        if n:
            method = "employer"
    if not n:
        loose = _norm_name(employer)
        if loose and loose != key:
            n = int(counts.get(loose) or 0)
            if n:
                method = "employer_loose"
    if not n and dba:
        dkey = _strict_norm_name(dba)
        shared = (set(dkey.split()) & set(key.split())) - _GENERIC_NAME_WORDS
        if dkey and dkey != key and shared:
            n = int(counts.get(dkey) or 0)
            if n:
                method = "dba"
    if not n:
        return 0, ""
    ceiling = _WORKFORCE_CEILING.get((workforce_size or "").strip())
    if ceiling is not None and n > ceiling * _PLAUSIBLE_FILINGS_PER_HEAD:
        return 0, "implausible"
    return n, method


def build_sponsor_index(names, wide=None):
    """Pre-normalize the sponsor list once so per-job lookups are fast.

    `wide` is an optional set of ALREADY-NORMALISED keys from the federal indexes
    (visa_tags + sponsor_counts). It is matched EXACTLY and never fuzzily. At 77k/118k
    entries a fuzzy pass is both too slow to run per company and far too eager: the
    nearest key to "Affinity" is "affinity aeronautical solutions" and the nearest to
    "Adaptive Innovations" is "adaptive health" — different companies, both would flag.
    """
    return {"raw": names, "norm": {_norm_name(n) for n in names}, "wide": set(wide or ())}


_sponsor_cache = {}

def sponsors_h1b(company, sponsor_index):
    """True if `company` looks like a known H1B sponsor. Cached per company.

    Three tiers, cheapest first:
      1. exact normalised match against sponsors.txt;
      2. exact normalised match against the federal indexes (`wide`), which is where
         nearly all the coverage lives — sponsors.txt holds ~620 names against
         visa_tags' ~77k. Measured over the live corpus, tier 1 alone resolved 277 of
         1,682 employers and tier 2 adds 973 more. Without it every cap-exempt
         university and hospital we scrape stores 'no', which is the opposite of the
         truth for exactly the employers an F-1 candidate should be looking at;
      3. fuzzy, and ONLY over the small curated list, so "Meta" still reaches
         "Meta Platforms" and "Oak Ridge National Laboratory" still reaches
         "Ut Battelle LLC Oak Ridge National Laboratory".
    """
    if company in _sponsor_cache:
        return _sponsor_cache[company]
    norm = _norm_name(company)
    hit = bool(norm) and (norm in sponsor_index["norm"] or norm in sponsor_index["wide"])
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

def _env_num(name, default, cast=float):
    """Numeric env override that can't take the whole module down. These are read at IMPORT
    time, so a typo in a CI variable would otherwise raise before anything runs — including
    the web app, which imports this module just to list boards."""
    try:
        return cast(os.environ.get(name) or default)
    except (TypeError, ValueError):
        print("  (ignoring bad %s=%r, using %s)" % (name, os.environ.get(name), default))
        return cast(default)


# Concurrent board fetches. These are independent hosts and the work is almost entirely
# waiting on the network, so this is the cheapest knob there is.
#
# MEASURED 2026-08-08, full 1,266-board sweep: 16.0 min at 16 workers. Note that the sum of
# per-board times is ~10,100 worker-seconds, which divides out to 10.6 min — the extra five
# are packing loss, and they are not recoverable by adding workers. The sweep ends with a
# long straggler (one Avature board alone runs 335s) and a worker that picks one up near the
# end holds the whole sweep open while the others idle. So treat throughput as sublinear in
# this number: 8 -> 16 was a real win, 16 -> 32 mostly would not be.
SCRAPE_WORKERS = _env_num("SCRAPE_WORKERS", 16, int)
# Wall-clock safety net for the whole board sweep, in minutes. 0 = unlimited.
#
# This is NOT the throttle that decides how long a run takes — at the default worker count
# the sweep lands well inside it and the budget never binds. It exists because the board
# list GROWS on its own (auto-discovery adds employers every weekday), so "fits today" is
# not a property that stays true, and the failure mode when it stops being true is the
# worst one available: the CI job is killed at its hard timeout, mid-sweep, having saved
# nothing and having skipped the scoring and email steps that run afterwards.
#
# Binding this budget instead costs a slice of one run's board coverage and says so in the
# log. Keep it comfortably under the job's timeout-minutes so the steps AFTER the scrape
# still have room to run.
SCRAPE_BUDGET_MIN = _env_num("SCRAPE_BUDGET_MIN", 22)
# SLICING, which is the structural version of the paragraph above. The budget bounds how LONG
# the sweep runs; this bounds how much it HOLDS while running. main() sweeps this many boards,
# writes them, releases them, then takes the next batch -- so peak memory is one slice rather
# than the whole corpus, and a run killed mid-sweep keeps every slice it finished.
#
# 0 = off, one pass, exactly the old behaviour. That is the default BECAUSE CI has never been
# killed for memory -- a GitHub runner is a whole machine -- and an unsliced run is what
# scripts/verify_parsers.py and any before/after measurement expect. bin/cron_scrape.sh turns
# it on, because the cPanel box is shared, is already swapping, and killed two consecutive
# unsliced runs on 2026-08-24 (rc=137) -- the second after a COMPLETE 48.4-minute sweep of
# 1,736 boards, which banked nothing because the only write came after all of them.
SCRAPE_SLICE = _env_num("SCRAPE_SLICE", 0, int)
# ROTATION. main() rotates where the sweep starts, because scrape_all walks the source list in
# order and the budget cuts whatever is left -- so a fixed order plus a binding budget starves
# the SAME tail on every run, forever. Off (0) makes a run byte-reproducible, which is what
# scripts/verify_parsers.py and any before/after measurement want; on is what CI should use.
SCRAPE_ROTATE = _env_num("SCRAPE_ROTATE", 1)
# How far the start moves per day. Coprime-ish with the list length so consecutive days do not
# land on near-identical starting points; the exact value does not matter much, only that it is
# large enough that one day's skipped slice is fully inside the next day's swept region.
SCRAPE_ROTATE_STRIDE = _env_num("SCRAPE_ROTATE_STRIDE", 137)
# Concurrent fetches allowed against any ONE host. Worker count alone is the wrong control
# here because boards are not evenly spread across hosts: 408 of them are on
# job-boards.greenhouse.io, 128 on jobs.smartrecruiters.com and 113 on jobs.ashbyhq.com.
# Raising workers without this would raise the peak load on exactly those few hosts.
#
# It costs the sweep nothing: the shared-host boards are the FAST ones (greenhouse ~0.5s),
# so even 400 of them at 4-wide is under a minute, and the critical path is Workday, which
# gives every tenant its own subdomain and so is never gated by this at all.
SCRAPE_PER_HOST = _env_num("SCRAPE_PER_HOST", 4, int)

# Per-board wall-clock ceiling in seconds, keyed by ats_type. An ats_type absent from this map
# has no ceiling, which is the historical behaviour for every board here.
#
# Every scraper in this file reaches the network through SESSION, whose per-request timeout and
# Retry policy bound it — so until now nothing could hang forever and no ceiling was needed.
# JobSpy breaks that: it ships its own HTTP stack (tls-client), sets no request timeout, and
# fans out over an unbounded ThreadPoolExecutor internally.
#
# A hang costs far more than a failure. db.add_jobs runs AFTER scrape_all returns, so a run
# killed at the CI step timeout saves nothing AND skips scoring, date verification and the
# digest, because only the digest step names a status function.
#
# Deliberately NOT a global default: the slowest honest board measured is an Avature tenant at
# 335s, so a global ceiling would have to sit above ~360s to avoid failing real boards, and a
# 6-minute ceiling guards nothing worth guarding.
# How long the sweep will WAIT on one board before abandoning it. Not a request timeout -- the
# adapters have their own -- but a cap on a board that keeps answering slowly enough to never
# trip one.
#
# WORKDAY, ADDED 2026-08-21 FROM THE FIRST RUN THAT MEASURED PER-BOARD COST. The numbers are the
# whole argument. That run spent 7,021 worker-seconds over 694 boards and skipped 571 (45%) when
# the budget ran out, and ONE board was 42% of the entire bill:
#
#     Itron     2951s -> 1 posting          <- 49 minutes, for one job
#     DirecTV    275s -> 12 postings
#     RBC        106s -> 1431 postings      <- the most expensive HONEST board
#     Accenture   45s -> 1705 postings
#
# So the distribution is not a long tail, it is one pathological board plus a clean median of
# 7.5s. 1dd90a0 predicted exactly this ("no value under ~21 min changes the sweep until the
# per-board straggler is capped") and capping pagination did not do it: Itron is not slow because
# it has many pages, it is slow because it answers slowly.
#
# 300s, not 150s, and the reason is the OTHER change in this file: page concurrency is derived
# from SCRAPE_WORKERS, so cron gets 2 pages where this measurement had 4. RBC's honest 106s
# roughly doubles at half the concurrency, so a 150s cap would abandon a 1,431-posting board on
# the runner that matters. 300s clears it with margin and still reclaims ~2,650s from Itron
# alone -- about 38% of the run, which is most of the reason those 571 boards went unread.
#
# Abandoning is safe and self-correcting: the board reports ok=False, reconcile_closed refuses
# to retire anything from a board it could not read, and it is fetched again next run.
SCRAPE_BOARD_TIMEOUT = {
    "jobspy": _env_num("JOBSPY_BOARD_TIMEOUT_SEC", 90, int),
    "workday": _env_num("WORKDAY_BOARD_TIMEOUT_SEC", 300, int),
}


def _run_with_timeout(fn, url, secs):
    """fn(url), but stop WAITING on it after `secs` and raise instead.

    Python cannot kill a thread, so the abandoned worker runs to completion in the background
    and its result is discarded. This bounds how long the sweep waits on one board, not how
    long that board runs — which is the part that matters, since the cost being avoided is one
    stuck board holding the whole run past its step timeout.

    The worker is a daemon thread rather than a pooled one on purpose: concurrent.futures joins
    its workers at interpreter exit, so a pooled hang would still block the process from
    exiting after main() had finished its work.
    """
    box = {}

    def _run():
        try:
            box["rows"] = fn(url)
        except BaseException as e:                       # re-raised on the caller's thread
            box["err"] = e

    t = threading.Thread(target=_run, daemon=True, name="board-timeout")
    t.start()
    t.join(secs)
    if t.is_alive():
        raise TimeoutError("no response after %ss (abandoned, still running)" % secs)
    if "err" in box:
        raise box["err"]
    return box.get("rows") or []


# Hosts needing a tighter gate than SCRAPE_PER_HOST. LinkedIn rate-limits an unauthenticated IP
# within a few hundred results and Glassdoor runs real bot management, so those two get one
# in-flight request at a time — being slow there is much cheaper than being blocked there.
_PER_HOST_OVERRIDE = {"jobspy:linkedin": 1, "jobspy:glassdoor": 1}


def _host_key(url, ats_type):
    """Which rate-limited thing this board actually talks to.

    Usually the URL's host. A few entries are SELECTORS rather than URLs, and those fall back to
    the ATS name so that everything sharing one API key shares one gate.

    JobSpy selectors get one key PER SITE ('jobspy:indeed'), because these are five unrelated
    hosts with five separate reputation budgets — throttling LinkedIn should not throttle Indeed.
    """
    try:
        host = urlparse(url or "").netloc.lower()
    except Exception:
        host = ""
    if not host and (ats_type or "").lower() == "jobspy":
        site = (url or "").split(":", 1)[-1].split("|", 1)[0].strip().lower()
        return "jobspy:" + site if site else "jobspy"
    return host or (ats_type or "").lower().replace("-search", "")


def scrape_all(sources, workers=None, progress=None, board_results=None, budget_min=None):
    """Scrape boards CONCURRENTLY (each is an independent host) so the whole run takes
    a few minutes, not ~30. One bad source never stops the run. `progress(done, total,
    found)` is called after each board finishes (used to drive the in-page progress bar).

    Pass `board_results` (a list) to also collect per-board outcomes as dicts
    {entry, ok, urls} — reconcile_closed() needs to know which board a URL came from and
    whether that board's fetch actually succeeded.

    Stops STARTING boards once `budget_min` is spent and returns what it has. Boards
    already in flight are left to finish; they are bounded by their own request timeouts,
    or by SCRAPE_BOARD_TIMEOUT for the ats_types that bring their own HTTP stack.
    """
    workers = workers or SCRAPE_WORKERS
    budget = SCRAPE_BUDGET_MIN if budget_min is None else budget_min
    deadline = (time.monotonic() + budget * 60) if budget and budget > 0 else 0
    gates, gates_lock = {}, threading.Lock()

    def _gate_for(key):
        with gates_lock:
            g = gates.get(key)
            if g is None:
                n = _PER_HOST_OVERRIDE.get(key, SCRAPE_PER_HOST)
                g = gates[key] = threading.Semaphore(max(1, n))
            return g

    def _one(entry):
        """-> (entry, company, rows, err, secs). `secs` is wall time INCLUDING the per-host gate
        wait and the stagger sleep, because that is what the board actually costs the sweep --
        a board that spends 20s queued behind three siblings on the same host is expensive even
        though its own fetch was fast. None when the board was never started."""
        url, ats_type, company = entry
        # Out of time: return without touching the network. A skipped board reports ok=False
        # below, which is what we want — it was not fetched, so the closed-posting check must
        # not read its silence as "these postings are gone".
        if deadline and time.monotonic() >= deadline:
            return entry, company, None, None, None
        fn = SCRAPERS.get(ats_type)
        if fn is None:
            return entry, company, None, "unknown ats_type '%s'" % ats_type, None
        b0 = time.monotonic()
        try:
            secs = SCRAPE_BOARD_TIMEOUT.get(ats_type)
            with _gate_for(_host_key(url, ats_type)):   # cap the load on any one host
                time.sleep(random.uniform(0, 1.0))      # small stagger so we don't burst one API
                rows = _run_with_timeout(fn, url, secs) if secs else fn(url)
            for r in rows:
                r.setdefault("company", company)        # keep a per-row company if the scraper set
                                                         # one (aggregator search spans many firms)
            return entry, company, rows, None, time.monotonic() - b0
        except Exception as e:
            # Timed even on failure: a board that fails SLOWLY is the expensive kind, and the
            # one most worth finding.
            return entry, company, None, str(e), time.monotonic() - b0

    all_jobs = []
    total = len(sources) if hasattr(sources, "__len__") else 0
    done = out_of_time = 0
    t0 = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for entry, company, rows, err, bsecs in ex.map(_one, sources):  # results in source order
            done += 1
            if err is not None:
                print(f"  FAIL {company:<26} {err}")
            elif rows is None:
                out_of_time += 1                         # budget spent; not fetched, not failed
            else:
                all_jobs.extend(rows)
                # The time is on the OK line because that is the line anyone reads when asking
                # where the sweep went. Only above a second: 1,200 boards printing "0.3s" is
                # noise that hides the four printing "48s".
                slow = "  %.0fs" % bsecs if (bsecs or 0) >= 1 else ""
                print(f"  OK   {company:<26} {len(rows):>3} postings{slow}")
            if board_results is not None:
                board_results.append({
                    "entry": entry, "company": company, "ok": rows is not None,
                    # A board the budget never STARTED is not a board that failed, and `ok`
                    # cannot carry that difference: reconcile_closed needs it False for both,
                    # because an unfetched board proves nothing about its postings. So the third
                    # state rides alongside it instead of inside it. Without this the board
                    # health report files every starved board as a failure -- 566 of them on the
                    # 2026-08-21 17:58 run against 3 boards that actually raised -- which is a
                    # triage list nobody can triage.
                    "skipped": rows is None and err is None,
                    "err": err,
                    "secs": bsecs,
                    "urls": {r.get("url") for r in (rows or []) if r.get("url")}})
            if progress:
                try:
                    progress(done, total, len(all_jobs))
                except Exception:
                    pass
    mins = (time.monotonic() - t0) / 60
    if out_of_time:
        print("\n  !! %d of %d board(s) SKIPPED — the %g-minute scrape budget ran out at %.1f min."
              % (out_of_time, total, budget, mins))
        # The old wording here said the skipped boards "are read again next run". That was only
        # true if the budget stopped binding: main() rotates the start of the list precisely so
        # a DIFFERENT slice is skipped next time, and without that rotation these same boards
        # were being skipped on every run indefinitely.
        print("     Everything fetched before that is saved as usual. main() rotates where the"
              " sweep starts,\n     so next run skips a DIFFERENT slice — but a budget that"
              " binds every run still means\n     no single run sees the whole list. Make the"
              " sweep cheaper before raising SCRAPE_BUDGET_MIN:\n     the tail after it (prune,"
              " reconcile, writes) has no clock and shares the same step.")
    else:
        print("\n  Swept %d board(s) in %.1f min with %d workers." % (total, mins, workers))
    return all_jobs


# ============================================================
# CLOSED-POSTING DETECTION
# Boards hand us their COMPLETE current listing every run, so a stored job that stops
# appearing has almost certainly been filled or pulled. Nothing detected that before, so a
# dead posting sat in the feed forever — applying to those is the most expensive way to
# waste the one thing a job-seeker can't get back.
#
# Rows are MARKED (is_active=false), never deleted, so Saved/Applied history survives.
# ============================================================
# Absent from this many consecutive successful fetches of its OWN board before we call it
# closed. The scrape runs 3x/day, so this is a few hours of corroboration, not one blip.
CLOSED_AFTER_MISSES = 3
# A board must return at least this many postings, and at least this fraction of what we
# already have under its URL prefix, before we trust its listing enough to close anything.
RECONCILE_MIN_ROWS = 3
RECONCILE_MIN_RATIO = 0.5
# Search aggregators return a QUERY's results, not a board's full inventory — absence from
# one run means nothing, so they can never close a row. reconcile_closed's whole premise is that
# a board IS an employer's own listing, so absence from it means the posting is gone; an
# aggregator phrase search is neither, and its rows all share one url prefix, so a single query
# for "project manager" would be judged against the entire aggregator corpus. (Adzuna's two
# entry points were here for exactly this reason before the source was removed.)
# Log-only until a real run has been checked against the shadow report's prediction. With this
# off, main() PRINTS what it would have suppressed and stores the row anyway — the same dry-run
# posture reconcile_closed uses, and the only safe way to calibrate a filter whose mistakes are
# invisible. Turn it on once "provable false merges" has been read and found to be ~0.
JOBSPY_FINGERPRINT_ENFORCE = (
    (os.environ.get("JOBSPY_FINGERPRINT_ENFORCE") or "").lower() in ("1", "true", "yes"))


def fingerprint_duplicate(job, fingerprints):
    """The stored URL this posting is an aggregator's copy of, or None.

    Catches the one duplicate class a url-keyed table cannot: a job we already hold from the
    employer's own board, relisted by an aggregator under its own domain. Those are genuinely
    different strings, so they are different primary keys, and canonical_url cannot bridge them.

    Three guards keep it to exactly that case:
      1. the CANDIDATE must be on an aggregator host. An employer-hosted row is ground truth and
         is never suppressed — which is also what makes a wrong call self-healing, since the real
         posting still arrives from its own board on the next sweep.
      2. the INCUMBENT must be on a DIFFERENT host. Within one host, two rows that look alike are
         two separate reqs with separate ids — Amazon really does list 431 "Operations Manager"
         roles. This is the same constraint web._dedupe_rows applies at render time.
      3. the incumbent must not itself be an aggregator row. Two aggregator copies of one job are
         a plain url duplicate, which the `seen` set already handles.

    The "keep it if it carries a NEW employer-direct link" tiebreak is not implemented here
    because it cannot fire: _jobspy_best_url already promotes a usable direct link into job["url"],
    so any row still holding an aggregator URL at this point had no direct link to offer, and
    guard 1 is what enforces that.

    require_location=True is the one place the key is stricter than the feed's: ("pm","acme","")
    would collide with every unplaced Acme PM row, which is survivable when it merges two cards
    and not survivable when it drops a row before insert.
    """
    url = job.get("url") or ""
    if not fingerprints or not core.is_aggregator_url(url):
        return None                                        # guard 1
    key = core.posting_key(job.get("title"), job.get("company"), job.get("location"),
                           require_location=True)
    if not key:
        return None
    host = core.url_host(url)
    for other in fingerprints.get(key, ()):
        other_host = core.url_host(other)
        if not other_host or other_host == host:
            continue                                       # guard 2
        if core.is_aggregator_url(other):
            continue                                       # guard 3
        return other
    return None


RECONCILE_SKIP_ATS = {"jobspy"}


def _url_prefix(urls):
    """Longest common '/'-delimited prefix of a board's URLs — the namespace that board owns.

    This is what makes per-board scoping correct on shared hosts: every Greenhouse customer
    lives on boards.greenhouse.io, so matching by host alone would let Stripe's board close
    Flexport's jobs. The prefix comes out as boards.greenhouse.io/flexport instead.
    """
    parts = None
    for u in urls:
        segs = re.sub(r"^[a-z]+://", "", (u or ""), flags=re.I).split("/")
        if parts is None:
            parts = segs
            continue
        keep = []
        for a, b in zip(parts, segs):
            if a != b:
                break
            keep.append(a)
        parts = keep
        if not parts:
            return ""
    return "/".join(parts or [])


def _norm_url(u):
    return re.sub(r"^[a-z]+://", "", (u or ""), flags=re.I)


BOARD_HEALTH_KEY = "board_health"
BOARD_HEALTH_RUNS = 8            # how many runs of history to keep per board


def board_run_failed(run):
    """Did this recorded run actually FAIL, as opposed to never having happened?

    One definition with two readers -- the triage list below and web.py's admin panel, which
    reaches it through the `sc` lazy module -- for the same reason reposts.cluster_key has one:
    two answers to "is this board broken" is worse than either answer on its own.

    The test is for EVIDENCE that the board ran. `ok=False` cannot mean failure by itself,
    because a board the budget never started reports exactly that, and reconcile_closed needs it
    to. A run that really ran carries `secs`; one that really raised carries `err`. A record with
    neither is either older than per-board timings (2026-08-21) or was written by the bug this
    replaced, and the honest reading of it is "unknown", not "broken" -- which is not a fine
    distinction: of the 569 records that looked like failures on the last run written before this
    fix, 566 were boards that had never been fetched at all.
    """
    if not isinstance(run, dict) or run.get("ok"):
        return False
    return bool(run.get("err")) or run.get("secs") is not None


def save_board_health(board_results):
    """Record what every board returned this run, and print the ones worth looking at.

    Keeps a short rolling window per board rather than one snapshot, because the question that
    matters is not "did this board return 0 today" -- plenty legitimately do -- but "has it
    returned 0 every run for a week", which is a board that has broken or been walled off.
    """
    if not board_results:
        return
    blob = db.get_kv(BOARD_HEALTH_KEY) or {}
    boards = blob.get("boards") or {}
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    for br in board_results:
        entry = br.get("entry") or ()
        url = (entry[0] if len(entry) > 0 else "") or ""
        ats = (entry[1] if len(entry) > 1 else "") or ""
        company = br.get("company") or ""
        n = len(br.get("urls") or ())
        rec = boards.get(url) or {"company": company, "ats": ats, "runs": []}
        rec["company"], rec["ats"] = company, ats
        # `secs` joins the record because nothing in the repo has ever measured what a board
        # COSTS -- only what it returned. Without it, "Workday is two thirds of the sweep" is a
        # number somebody worked out by hand once and left in a workflow comment, and any attempt
        # to make the sweep fit its budget is guesswork. It rides in the kv blob that is already
        # written every run, so it earns no migration -- the same argument score_jobs' thin-JD
        # ledger makes for itself.
        #
        # Absent for a board the budget skipped: it was never started, so it cost nothing, and
        # recording a 0 would drag its median down and make the expensive ones look cheap.
        #
        # A SKIPPED BOARD GETS NO RUN AT ALL, for the same reason. It was never fetched, so it
        # has no outcome, and filing one as ok=False is why this report became unreadable. Two
        # costs, both real: the triage list below filled up with healthy boards, and -- quieter
        # -- every skip burned one of the eight history slots, so `silent` (three clean
        # zero-fetches running) was reading a window that mostly held non-events. Counting the
        # streak instead keeps the signal that matters, a board starved run after run.
        if br.get("skipped"):
            rec["skips"] = int(rec.get("skips") or 0) + 1
            rec["last_skip"] = stamp
            boards[url] = rec
            continue
        run = {"at": stamp, "n": n, "ok": bool(br.get("ok"))}
        if br.get("secs") is not None:
            run["secs"] = round(float(br["secs"]), 1)
        if br.get("err"):
            # Both the log and the admin panel could say "this board failed" and neither could
            # say why, so every failure cost a local re-run to reproduce. One string per board.
            run["err"] = str(br["err"])[:200]
        rec["runs"] = (rec.get("runs") or [])[-(BOARD_HEALTH_RUNS - 1):] + [run]
        rec["skips"] = 0                  # fetched this run: the starvation streak is broken
        boards[url] = rec
    db.put_kv(BOARD_HEALTH_KEY, {"updated_at": stamp, "boards": boards})

    # The triage list. A board erroring is louder than one returning zero, because zero can be
    # honest and an error never is. A STARVED board is louder than neither -- it says nothing
    # about the board and everything about the budget -- so it is counted here, not listed.
    failed = [r for r in boards.values() if r["runs"] and board_run_failed(r["runs"][-1])]
    silent = [r for r in boards.values()
              if len(r["runs"]) >= 3 and all(x["n"] == 0 and x["ok"] for x in r["runs"][-3:])]
    # Keyed on THIS run's stamp, not on a non-zero counter: a board that has left the source
    # list keeps whatever streak it died with, and counting that as "skipped this run" would be
    # the same kind of lie this whole change is undoing.
    starved = [r for r in boards.values() if r.get("last_skip") == stamp]
    print("\nBoard health: %d boards tracked, %d failed this run, %d returning 0 for 3+"
          " runs, %d never reached by the budget"
          % (len(boards), len(failed), len(silent), len(starved)))
    for label, group in (("FAILED", failed), ("SILENT", silent)):
        for r in sorted(group, key=lambda x: x["company"])[:25]:
            # The error is the whole point of a FAILED line. Without it the next step is always
            # to re-run the board by hand to find out what it said.
            why = (r["runs"][-1].get("err") or "")[:60]
            print("  %-6s %-30s %-16s last=%s%s"
                  % (label, r["company"][:30], r["ats"], r["runs"][-1]["n"],
                     ("  " + why) if why else ""))
        if len(group) > 25:
            print("  %-6s ...and %d more" % (label, len(group) - 25))
    if starved:
        # The streak is the useful number: the daily rotation is meant to MOVE the starved slice,
        # so a board with a long one means the rotation is not covering the list.
        stuck = sorted(starved, key=lambda x: -(x.get("skips") or 0))[:5]
        print("  STARVED %d board(s) were never started this run (SCRAPE_BUDGET_MIN=%g)."
              " Longest streaks: %s"
              % (len(starved), SCRAPE_BUDGET_MIN,
                 ", ".join("%s x%d" % (r["company"][:22], r.get("skips") or 0)
                           for r in stuck)))

    # WHERE THE SWEEP WENT. One pass over a dict we just built, and the most useful few lines in
    # the run log for anyone trying to make the sweep fit its budget. Grouped by ATS rather than
    # by board because the fix for a slow board is almost always a fix to its adapter, and one
    # adapter carries hundreds of boards.
    per_ats = {}
    for r in boards.values():
        s = [x["secs"] for x in (r.get("runs") or []) if x.get("secs") is not None]
        if not s:
            continue
        a = per_ats.setdefault(r.get("ats") or "?", {"boards": 0, "secs": 0.0})
        a["boards"] += 1
        a["secs"] += s[-1]                     # this run only, not the whole 8-run window
    if per_ats:
        spent = sum(a["secs"] for a in per_ats.values())
        print("")
        print("  Cost by ATS this run: %.0f worker-seconds over %d board(s) that ran"
              % (spent, sum(a["boards"] for a in per_ats.values())))
        for ats, a in sorted(per_ats.items(), key=lambda kv: -kv[1]["secs"])[:8]:
            print("    %-18s %6.0fs  %5.1f%%  across %4d board(s)  (%.1fs each)"
                  % (ats, a["secs"], 100.0 * a["secs"] / max(1.0, spent), a["boards"],
                     a["secs"] / max(1, a["boards"])))
        slowest = sorted(
            ((max(x["secs"] for x in r["runs"] if x.get("secs") is not None), r)
             for r in boards.values()
             if any(x.get("secs") is not None for x in (r.get("runs") or []))),
            key=lambda t: -t[0])[:10]
        print("  Slowest single boards seen in the last %d run(s):" % BOARD_HEALTH_RUNS)
        for s, r in slowest:
            print("    %6.0fs  %-30s %s" % (s, (r.get("company") or "?")[:30], r.get("ats")))


def reconcile_closed(board_results, apply=False):
    """Mark jobs that have vanished from their own board as closed. Returns (closed, considered).

    `apply=False` reports what it WOULD do and writes nothing — always run that first on a
    new board set, because the failure mode (a bot-walled board returning an empty list)
    would otherwise retire its entire inventory in one pass.
    """
    try:
        # Only url/is_active/miss_count/last_seen are read below, and this runs on every scrape
        # — 4.4 MB a call instead of 11.6 MB at 19k rows.
        rows = db.load_jobs(cols=db.COLS_RECONCILE)
    except Exception as e:
        print("  (closed-posting check skipped, could not load jobs: %s)" % str(e)[:90])
        return 0, 0

    by_url = {r.get("url"): r for r in rows if r.get("url")}
    today = datetime.date.today().isoformat()
    seen_now, to_close, considered = {}, [], 0

    for br in board_results:
        if not br.get("ok"):
            continue                                   # a failed fetch proves nothing
        ats = (br["entry"][1] or "").lower()
        if ats in RECONCILE_SKIP_ATS:
            continue
        urls = {_norm_url(u) for u in br["urls"]}
        if len(urls) < RECONCILE_MIN_ROWS:
            continue
        prefix = _url_prefix(br["urls"])
        if not prefix or "/" not in prefix:
            # Too broad to scope safely (a bare host would span every company on it).
            continue

        mine = [r for r in rows if _norm_url(r.get("url")).startswith(prefix)]
        if not mine:
            continue
        # A board that suddenly returns a fraction of what we have is having a bad day
        # (rate-limited, bot-walled, partial page) — don't let it retire the rest.
        if len(urls) < RECONCILE_MIN_RATIO * len(mine):
            print("  ~ %-26s returned %d vs %d stored — too few to trust, skipping"
                  % (br["company"][:26], len(urls), len(mine)))
            continue

        considered += len(mine)
        for r in mine:
            u = r.get("url")
            if _norm_url(u) in urls:
                seen_now[u] = r
                continue
            if _truthy_false(r.get("is_active")):
                continue                               # already retired; leave it alone
            misses = int(r.get("miss_count") or 0) + 1
            if misses >= CLOSED_AFTER_MISSES:
                to_close.append({"url": u, "is_active": False, "miss_count": misses})
            else:
                to_close.append({"url": u, "miss_count": misses})

    fresh = [{"url": u, "last_seen": today, "miss_count": 0, "is_active": True}
             for u, r in seen_now.items()
             if (r.get("last_seen") or "")[:10] != today or int(r.get("miss_count") or 0)]
    newly_closed = [d for d in to_close if d.get("is_active") is False]

    print("  closed-posting check: %d row(s) under healthy boards · %d still listed · "
          "%d newly closed · %d missing but under the %d-miss threshold"
          % (considered, len(seen_now), len(newly_closed),
             len(to_close) - len(newly_closed), CLOSED_AFTER_MISSES))
    if not apply:
        print("  DRY RUN — nothing written. Set RECONCILE_CLOSED=1 to apply.")
        for d in newly_closed[:10]:
            r = by_url.get(d["url"]) or {}
            print("     would close: %-40s %s" % ((r.get("company") or "?")[:40], d["url"][:70]))
        return 0, considered

    try:
        if fresh:
            db.update_job_fields(fresh)
        if to_close:
            db.update_job_fields(to_close)
    except Exception as e:
        print("  (closed-posting write failed: %s)" % str(e)[:140])
        print("  If that mentions an unknown column, run db.JOBS_DERIVED_SQL once in Supabase.")
        return 0, considered
    return len(newly_closed), considered


def _truthy_false(v):
    """True when the stored value already means 'not active' — avoids rewriting rows we
    already retired on an earlier run."""
    return v is False or str(v).strip().lower() in ("false", "f", "0")


def _explain_auth_failure(err):
    """Turn a proxy 401 into the one sentence that fixes it, and return True if that is what it was.

    A 401 from /api/db has exactly one cause: DB_PROXY_SECRET does not match the server's. The
    signature covers the exact request bytes, so a single stray byte in the key — a trailing newline
    picked up when the value was pasted, most often — produces this and nothing else. Scheduled runs
    had been dying on it for days behind a raw PgRestError traceback that named neither the secret
    nor where to change it.
    """
    text = str(err)
    if "401" not in text or "signature" not in text.lower():
        return False
    print("\n" + "=" * 78)
    print("SCRAPE ABORTED: the database proxy rejected our signature (HTTP 401).")
    print("")
    print("DB_PROXY_SECRET does not match the value the server was started with. The signature is")
    print("computed over the exact request bytes, so one extra character — a trailing newline from")
    print("a copy-paste is the usual one — fails every request and nothing else does this.")
    print("")
    print("  * running in GitHub Actions: re-paste the secret at")
    print("    Settings -> Secrets and variables -> Actions -> DB_PROXY_SECRET,")
    print("    with NO trailing whitespace or newline.")
    print("  * running locally: export it whitespace-stripped, e.g.")
    print("    DB_PROXY_SECRET=\"$(tr -d '[:space:]' < .db_proxy_secret)\"")
    print("")
    print("  * to see which half is wrong without guessing: python scripts/probe_db_proxy.py")
    print("=" * 78)
    return True


def main():
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"\n=== Job scrape @ {stamp} ===")

    sponsors = load_sponsors()
    # The two federal indexes, read ONCE and used for two different jobs: widening the
    # sponsor flag here, and the JobSpy record gate further down. They used to be loaded
    # only when an aggregator sweep was on, which left the flag reading a ~620-name file
    # while a 77k-name index sat on disk unused — so every cap-exempt university and
    # hospital we scrape stored sponsors_h1b='no'. ~5 MB resident, paid once per scrape.
    try:
        visa_index = core.load_visa_tags()
        sponsor_counts = core.load_sponsor_counts()
    except Exception as e:
        # A missing data file must never take a scrape down; the flag just narrows back
        # to sponsors.txt, which is exactly the old behaviour.
        print("  note: federal sponsor records unavailable (%s)" % str(e)[:70])
        visa_index = sponsor_counts = None
    wide = set(visa_index or ()) | set(sponsor_counts or ())
    sponsor_index = build_sponsor_index(sponsors, wide) if (sponsors or wide) else None
    if sponsor_index:
        action = "dropping non-sponsors" if REQUIRE_SPONSOR else "flag only"
        print("Sponsor flag: %d name(s) from %s + %d from visa_tags/sponsor_counts (%s)."
              % (len(sponsors or []), SPONSORS_FILE, len(wide), action))
    else:
        print(f"No {SPONSORS_FILE} found — keeping all entry-level jobs, "
              f"sponsor status marked 'unknown'.")

    extra = apply_resume_terms()             # broadens the title keep-filter, in one place
    if extra:
        global AMAZON_QUERIES
        AMAZON_QUERIES = tuple(dict.fromkeys(AMAZON_QUERIES + tuple(extra)))     # + Amazon searches
        print("Résumé-driven (%s): also searching %s" % (RESUME_FILE, ", ".join(extra)))
    else:
        print("No %s found — using the base role filter only." % RESUME_FILE)

    # Canonicalize BOTH sides of the dedupe: stored rows predate normalization (and hold
    # e.g. the old boards.greenhouse.io form), so comparing raw would re-insert them.
    # Compared CASE-INSENSITIVELY. Workday's site segment isn't case-stable — Applied
    # Materials' board answered on both /external/ and /External/, and because those are
    # different strings the same 79 postings were stored twice, under two different company
    # labels ("Amat" and "Applied Materials"), and rendered as duplicate cards. Two genuinely
    # distinct postings whose URLs differ only by letter case don't occur in practice.
    #
    # With an aggregator source on we also need the POSTING fingerprint (title+company+location),
    # because those sources return a second URL for a job we may already hold and no url rule can
    # merge the two. That needs three more columns, taken as ONE widened read rather than a second
    # call: ~3 MB against existing_urls()' ~1.2 MB at 20k rows. Dormant means dormant — with
    # JOBSPY_BOARDS empty this stays on the narrow path and the run costs exactly what it does now.
    fingerprints = {}
    if JOBSPY_BOARDS:
        corpus = db.load_jobs(include_jd=False, cols=db.COLS_DEDUPE)
        seen = {canonical_url(r.get("url") or "").lower() for r in corpus}
        seen.discard("")
        for r in corpus:
            k = core.posting_key(r.get("title"), r.get("company"), r.get("location"),
                                 require_location=True)
            if k:
                fingerprints.setdefault(k, []).append(r.get("url") or "")
        print("Dedupe index: %d url(s), %d posting fingerprint(s)." % (len(seen), len(fingerprints)))
    else:
        seen = {canonical_url(u).lower() for u in db.existing_urls()}

    # Hand the dedupe index to the adapters that can use it to skip work. Only Greenhouse reads
    # it today, to decide whether a board is worth asking for descriptions; see
    # scrape_greenhouse. Set from `seen` rather than from a second read, so the two cannot
    # disagree about what "already have it" means.
    publish_known_urls(seen)

    # The JobSpy record gate reuses the indexes already loaded at the top of this run.
    # It stays OPT-IN, and the guard is the point: now that those files are read on every
    # scrape for the sponsor flag, gating on "did they load" would silently switch this
    # gate on for every aggregator row, which is a drop rule, not a flag.
    jobspy_visa_gate = bool(JOBSPY_BOARDS and JOBSPY_REQUIRE_VISA_RECORD and visa_index)
    if jobspy_visa_gate:
        print("Sponsor records: %d employer(s) with a visa tag, %d with USCIS approvals."
              % (len(visa_index or {}), len(sponsor_counts or {})))

    # Companies an admin blocked from /admin/data. Loaded once per run, next to `seen`, because
    # the keep loop below consults it per posting. Returns an empty set on ANY failure — a
    # blocklist read must never be the reason a scrape aborts; worst case it behaves as it did
    # before the feature existed. Without this check, deleting a company's jobs is theatre:
    # the twice-daily scrape puts them straight back.
    blocked = db.blocked_company_keys()

    # AUTO-DISCOVERY. Before building the source list, probe a rotating slice of the employers
    # migratemate.co lists that we DON'T yet scrape, and save any with a readable ATS board to
    # the `boards` table. custom_sources() below then picks them up, so a board found here is
    # scraped in THIS run — no code edit, no deploy. Bounded per run (~0.7 companies/sec) so it
    # can't push the run past the CI timeout; the window rotates daily to cover the whole list.
    # DISCOVER_LIMIT=0 turns it off. Defensive by construction: discover() never raises.
    # Defaults ON (not 0): "every scrape keeps finding new employers" is the point, and a
    # default of off would mean it only ever ran in CI, never on a manual or app-button run.
    _dlimit = int(os.environ.get("DISCOVER_LIMIT", str(DISCOVER_LIMIT_DEFAULT)) or 0)
    if _dlimit > 0:
        try:
            from scraper.discover import discover as _discover
            _discover(_dlimit)
        except Exception as e:
            print("  discovery skipped: %s" % str(e)[:100])

    sources = SOURCES + custom_sources()
    if len(sources) > len(SOURCES):
        print("+ %d board(s) added via the app." % (len(sources) - len(SOURCES)))

    # ROTATE THE START. scrape_all consumes this list in order and stops STARTING boards once
    # SCRAPE_BUDGET_MIN is spent, so when the budget binds it is always the SAME tail that goes
    # unread -- and the log said those boards "are read again next run", which is only true if
    # the budget stops binding. Both halves of that were measured on 2026-08-21 (see 1dd90a0):
    #
    #     budget 22 -> all 1265 boards swept in 20.6 min, 0 skipped
    #     budget 16 -> ran out at 21.4 min, 95 boards skipped
    #
    # So this is INSURANCE, not the repair of a live bug: at 22 the budget does not bind and
    # nothing is starved. It exists because the day the budget was 16, the cost was not a random
    # 95 boards -- it was the same 95 every run, and nothing in the log said so.
    #
    # Rotating by day-of-year makes a starved slice move instead of persist, the same trick
    # auto-discovery above uses to cover its whole list from a stateless runner. It costs
    # nothing: same boards, same count, different starting point. Deliberately NOT random -- a
    # fixed rotation covers the list evenly and keeps a run reproducible from its date, and
    # SCRAPE_ROTATE=0 turns it off for a before/after measurement.
    #
    # The actual repair for a binding budget is to make the sweep cheaper, which is what the
    # concurrent Workday pagination below does: 1dd90a0 found the sweep's floor was "the
    # per-board straggler" -- some Workday tenants taking minutes each -- and that is exactly
    # the number scrape_workday now attacks.
    if sources and SCRAPE_ROTATE:
        _doy = datetime.datetime.now(datetime.timezone.utc).timetuple().tm_yday
        # int(): _env_num returns a float, and a float is not a valid slice index.
        _off = int(_doy * SCRAPE_ROTATE_STRIDE) % len(sources)
        if _off:
            sources = sources[_off:] + sources[:_off]
            print("Sweep starts at board %d of %d (rotates daily so the budget cannot starve"
                  " the same tail every run)." % (_off + 1, len(sources)))

    # --- live progress for the in-page "Update jobs" bar (best-effort; never blocks a scrape) ---
    started = datetime.datetime.now(datetime.timezone.utc).isoformat()   # UTC so the browser's elapsed math is right
    _last_write = [0.0]
    def _progress(done, total, found, phase="scraping", force=False):
        now = time.time()
        if force or now - _last_write[0] >= 2.5 or (total and done >= total):
            _last_write[0] = now
            db.set_scrape_status({"phase": phase, "done": done, "total": total,
                                  "found": found, "started_at": started, "run": stamp})
    _progress(0, len(sources), 0, force=True)
    board_results = []          # per-board outcome, for the closed-posting check below
    # --- THE SWEEP RUNS IN SLICES, and the reason is a production incident, not tidiness. ---
    #
    # On 2026-08-24 two consecutive cPanel runs were SIGKILLed (rc=137) after the deadline was
    # lifted: one 37.5 min in, mid-sweep, the other after a COMPLETE 48.4-minute sweep of 1,736
    # boards, two minutes into the JD lookup. Both banked nothing, because `scraped` held every
    # posting from every board and the single db.add_jobs() came after all of it. A kill
    # anywhere before that line cost the entire run.
    #
    # Slicing fixes both halves of that. Peak memory is now one slice rather than the whole
    # corpus -- which is what the kill was about, since the box is shared and swapping -- and a
    # slice that finishes is written before the next one starts, so a kill costs the slice in
    # flight, not the run. The writes are idempotent upserts keyed on url (db._upsert), so a
    # re-run after a kill re-reads the banked boards and changes nothing.
    #
    # SCRAPE_SLICE=0 keeps the old single-pass behaviour, which is what CI uses: a GitHub runner
    # is a whole machine and has never been killed for memory.
    _slices = ([sources[i:i + SCRAPE_SLICE] for i in range(0, len(sources), SCRAPE_SLICE)]
               if SCRAPE_SLICE and SCRAPE_SLICE > 0 else [sources])
    if len(_slices) > 1:
        print("Sweeping in %d slices of up to %d board(s). Each slice is written before"
              " the next starts, so a run that is killed keeps what it had banked."
              % (len(_slices), SCRAPE_SLICE))
    all_kept = []               # every kept row of the whole run, for the summary + notify.py
    scanned_total = 0
    _swept = 0                  # boards finished in earlier slices, so the progress bar is
                                # a whole-run number rather than restarting each slice
    kept = []
    fp_seen = []            # aggregator relists caught by the fingerprint, for the run summary
    # {canonical url -> description} for rows whose board handed the JD over with the listing.
    # Keyed off j["url"] AFTER canonical_url() has run on it, so the writer and the jobs table
    # cannot disagree about the key the way jd_map_for did.
    listing_jds = {}
    # NOT a tally key: tally is printed as the run's DROP reasons, and this is a keep.
    kept_on_jd = 0
    tally = {"already known": 0, "off-target function title": 0,
             "no matching role keyword": 0, "non-US location": 0,
             "blocked company": 0,
             "aggregator copy of a job we hold": 0,
             "no federal sponsor record (aggregator)": 0,
             "posted over %d days ago (%d for long-lived boards)"
             % (MAX_AGE_DAYS, db.AGE_LONG_DAYS): 0}
    age_cutoff = ((datetime.date.today() - datetime.timedelta(days=MAX_AGE_DAYS)).isoformat()
                  if MAX_AGE_DAYS > 0 else "")
    # The longer window for sources that publish a real posting date AND only serve live reqs.
    # The host set and the number both live in db, because the PRUNE has to use the same two or
    # the corpus drifts to whichever half is looser — db.stale_urls' comment has the full story.
    long_cutoff = ((datetime.date.today() - datetime.timedelta(days=db.AGE_LONG_DAYS)).isoformat()
                   if (MAX_AGE_DAYS > 0 and db.AGE_LONG_DAYS) else age_cutoff)

    # SCRAPE_BUDGET_MIN IS A WHOLE-RUN DEADLINE, and slicing is what could quietly have made it
    # a per-slice one: scrape_all reads the global when no budget_min is passed, so six slices
    # would have meant six full budgets and, in CI, six times the timeout it was sized against.
    # Spend it down across slices instead, and stop STARTING slices once it is gone -- the same
    # contract scrape_all has always had for boards, one level up.
    _sweep_t0 = time.monotonic()
    for _sl in _slices:
        def _sl_progress(done, total, found, phase="scraping", force=False):
            # Offset into whole-run terms; scrape_all only knows about its own slice.
            _progress(_swept + done, len(sources), scanned_total + found, phase, force)
        _left = None
        if SCRAPE_BUDGET_MIN and SCRAPE_BUDGET_MIN > 0:
            _left = SCRAPE_BUDGET_MIN - (time.monotonic() - _sweep_t0) / 60
            if _left <= 0:
                _rest = _slices[_slices.index(_sl):]
                print("\n  !! %d board(s) in %d unstarted slice(s) SKIPPED -- the %g-minute budget"
                      " went on the slices before them. Everything already swept is stored:"
                      " each slice was written as it finished."
                      % (sum(len(s) for s in _rest), len(_rest), SCRAPE_BUDGET_MIN))
                break
        scraped = scrape_all(_sl, progress=_sl_progress, board_results=board_results,
                             budget_min=_left)
        _progress(_swept + len(_sl), len(sources), scanned_total + len(scraped),
                  phase="saving", force=True)
        # These sites publish no quota and return no usage headers, so the only honest way to know
        # what a run spends against them is to count it. Print it every run: a number in the log
        # beats the guess in a comment. (Adzuna was counted the same way until it was removed.)
        if JOBSPY_CALLS[0]:
            print("JobSpy: %d quer%s, %d raw row(s)."
                  % (JOBSPY_CALLS[0], "y" if JOBSPY_CALLS[0] == 1 else "ies", JOBSPY_ROWS[0]))

        # PHASE TIMING, because the alternative is a silent gap. The 2026-08-21 run was killed by
        # the step timeout with its last line being the JobSpy count and NOTHING for the 5m17s after
        # it -- everything between here and the tally print is a single unlogged stretch, so the log
        # could not say whether the filter loop, the insert, the prune or the closed-posting check
        # had taken the time. Two runs earlier the same stretch took 2.4 minutes. `_phase` costs one
        # line each and makes the next occurrence diagnosable instead of a guess.
        _phase_t = [time.time()]

        def _phase(label):
            now = time.time()
            print("  [phase] %-22s %5.1fs" % (label, now - _phase_t[0]))
            _phase_t[0] = now

        # Buy descriptions for the postings the boards did not hand one over for, before the keep
        # loop runs — so a row rescued by a FETCHED description takes exactly the same path through
        # the loop as one rescued by a description that arrived free. See fill_missing_jds.
        kept = []
        listing_jds = {}
        try:
            jd_tried, jd_got = fill_missing_jds(scraped, seen, blocked)
            if jd_tried:
                print("JD lookup: %d of %d returned a usable description." % (jd_got, jd_tried))
        except Exception as e:
            print("  note: JD lookup pass failed (%s); titles decide on their own" % str(e)[:80])

        for j in scraped:
            j["url"] = canonical_url(j.get("url", ""))
            if j["url"].lower() in seen:
                tally["already known"] += 1
                continue                       # already in jobs.csv from a past run
            # Right after the dedupe and before any title work: this is the cheapest position, and
            # putting it in the tally makes the drop visible in the run summary. A blocklist you
            # can't see working is one you won't trust.
            if blocked and db.block_key(j.get("company", "")) in blocked:
                tally["blocked company"] += 1
                continue
            keep, why = title_verdict(j["title"])
            # A SECOND OPINION FROM THE DESCRIPTION, when the title said nothing useful.
            #
            # Plenty of employers title a delivery role "Coordinator II" or "Business Operations
            # Specialist", and no keyword list will ever cover that. Where a board handed us the
            # description with the listing (_listing_jd above), read it instead of guessing from
            # eight words of title.
            #
            # ONLY WHEN THE REASON WAS "no matching keyword". An EXCLUDE hit is a different claim --
            # the title named a job we do not want -- and this must never overturn it, for the same
            # reason _REVERSED_RE runs after EXCLUDE rather than before it.
            #
            # The US gate below still applies: it sits in the `elif` on `keep`, so a row rescued
            # here goes through it exactly like a title-matched one. That was the point of doing the
            # rescue here rather than after the gate.
            if not keep and not why.startswith("off-target"):
                if core.admits_on_description(j["title"], j.get("jd")):
                    keep, why = True, "matched on description"
                    kept_on_jd += 1
            if not keep:
                tally["off-target function title" if why.startswith("off-target")
                      else "no matching role keyword"] += 1
            # WHEN THE LOCATION IS BLANK, ASK THE TITLE. is_us_location keeps an unknown location
            # on purpose -- only 0.8% of the corpus has none, and dropping them would lose real US
            # jobs from Uber, Synopsys and McKinsey, whose boards simply do not publish one. But a
            # blank location does not mean the posting is silent about where it is: "Data Analyst
            # (Remote, India)" arrived with an empty location field and the country in its title,
            # and sailed straight through. It is a VETO ONLY -- see title_says_non_us.
            elif US_ONLY and (not is_us_location(j.get("location", ""))
                              or title_says_non_us(j.get("title", ""))):
                keep, why = False, "non-US location (%s)" % (j.get("location") or "n/a")
                tally["non-US location"] += 1
            if VERBOSE:
                print("  %s %-52s %s" % ("KEEP " if keep else "drop ", j["title"][:52], why))
            if not keep:
                dump_reject(j.get("title"), j.get("company"), j.get("location"), j["url"], why)
                continue
            # Freshness gate. This has to run BEFORE the setdefault below: that line stamps
            # undated rows with today's date, so a gate placed after it would see every dateless
            # board as brand new and could never reject anything. Here the value is still exactly
            # what the employer published — a date, an empty string, or nothing at all.
            if age_cutoff:
                posted = (j.get("found_date") or "")[:10]
                cut = long_cutoff if db.is_long_lived(j["url"]) else age_cutoff
                if posted and posted < cut:
                    tally["posted over %d days ago (%d for long-lived boards)"
                          % (MAX_AGE_DAYS, db.AGE_LONG_DAYS)] += 1
                    if VERBOSE:
                        print("  drop  %-52s posted %s" % (j["title"][:52], posted))
                    continue
            if sponsor_index:
                sponsored = sponsors_h1b(j["company"], sponsor_index)
                if REQUIRE_SPONSOR and not sponsored:
                    continue
                j["sponsors_h1b"] = "yes" if sponsored else "no"
            else:
                j["sponsors_h1b"] = "unknown"
            # Sponsor-record gate, JOBSPY ROWS ONLY. A keyword sweep of Indeed returns the long tail
            # of small US employers — roofers, local contractors, mobile-home services — and 41% of
            # the ones it found had no record in ANY federal file, against 10% for the corpus. The
            # direct boards are exempt because those employers were chosen deliberately, and several
            # are cap-exempt universities and hospitals this test would wrongly drop.
            if jobspy_visa_gate and j.get("_src") == "jobspy":
                co = j.get("company") or ""
                if not core.visa_tags(co, visa_index) and not core.sponsor_strength(
                        co, sponsor_counts)[0]:
                    tally["no federal sponsor record (aggregator)"] += 1
                    if VERBOSE:
                        print("  drop  %-52s no LCA/PERM/E-Verify/USCIS record" % co[:52])
                    continue
            # DEAD LAST in the chain, on purpose. This is the only drop here that can be wrong in the
            # "lost a real job" direction, so it sees only rows that already cleared every other gate
            # — which is what lets the line below name exactly what was suppressed and against which
            # stored posting. It is also the most expensive check, so it should see the fewest rows.
            dupe_of = fingerprint_duplicate(j, fingerprints)
            if dupe_of:
                fp_seen.append((j["title"], j["url"], dupe_of))
                if VERBOSE or not JOBSPY_FINGERPRINT_ENFORCE:
                    print("  %s %-44s\n        we already hold %s"
                          % ("dupe " if JOBSPY_FINGERPRINT_ENFORCE else "dupe?",
                             j["title"][:44], dupe_of[:96]))
                if JOBSPY_FINGERPRINT_ENFORCE:
                    tally["aggregator copy of a job we hold"] += 1
                    continue
            j.setdefault("found_date", stamp)        # keep the JD's posting date if set
            seen.add(j["url"].lower())               # two boards in ONE run can serve the same
                                                     # posting (e.g. both Greenhouse hosts)
            # Bank a description that came with the listing -- for EVERY kept row, not just the ones
            # rescued by it. A title-matched row gets its JD for free here too, which is a straight
            # saving against the scoring budget: a measured detail pass once spent 2,640 fetches to
            # recover 8 usable descriptions. Guarded at _MIN_JD_CHARS so a truncated teaser can
            # never be stored as a complete description (the 403-char JobDiva trap).
            if len((j.get("jd") or "").strip()) >= core._MIN_JD_CHARS:
                listing_jds[j["url"]] = j["jd"]
            kept.append({k: j.get(k, "") for k in FIELDNAMES})

        all_kept.extend(kept)
        scanned_total += len(scraped)
        try:            # persist this run's new jobs to disk FIRST so a DB hiccup can't lose the scrape
            json.dump(all_kept, open("last_new_jobs.json", "w", encoding="utf-8"))
        except Exception:
            pass
        if kept:
            _phase("filter + dedupe")
            db.add_jobs(kept)               # (also the breadcrumb notify.py reads for this run's alerts)
            _phase("add_jobs (%d rows)" % len(kept))

        # JobSpy returns the description WITH the row, so store it for the jobs we kept. Without this
        # they fall to score_jobs' per-URL detail fetch, which mostly 403s against the aggregators
        # while spending the scoring budget — the rows would score 0, render as "JD pending", and
        # never reach the match filter or the digest.
        # ...and so do lever / ashby / jibe / pinpoint, from the same response the sweep already
        # read. Merged into one write: both are "the description arrived with the listing", and one
        # db.update_jds call is one round trip instead of two.
        if kept:
            jds = dict(listing_jds)
            jds.update({r["url"]: JOBSPY_JDS[r["url"]]
                        for r in kept if r.get("url") in JOBSPY_JDS})
            if jds:
                try:
                    db.update_jds(jds)
                    print("Stored %d description(s) that arrived with the listing." % len(jds))
                except Exception as e:
                    print("  note: JD write failed (%s); score_jobs will refetch" % str(e)[:80])
        del scraped                 # the slice is banked; release it before the next one
        _swept += len(_sl)

    if fp_seen:
        # Broken out by host on purpose. The check keys off "is this an aggregator row", not
        # "did jobspy fetch it", so switching an aggregator source on also starts catching
        # Adzuna relists — the same duplicate class, but a wider blast radius than the feature
        # that prompted it. Read this breakdown before setting JOBSPY_FINGERPRINT_ENFORCE.
        print("Posting fingerprint: %d aggregator relist(s) of jobs we already hold%s."
              % (len(fp_seen), "" if JOBSPY_FINGERPRINT_ENFORCE
                 else " — LOG ONLY, all stored anyway"))
        by_host = {}
        for _t, u, _o in fp_seen:
            h = core.url_host(u) or "?"
            by_host[h] = by_host.get(h, 0) + 1
        for h, n in sorted(by_host.items(), key=lambda kv: -kv[1]):
            print("   %-38s %6d" % (h[:38], n))

    # Corpus pruning. This is the OTHER HALF of the freshness policy and defaults to the same
    # window as MAX_AGE_DAYS, deliberately: the gate above refuses stale postings on the way
    # IN, this removes stale rows already stored, and if the two numbers ever disagree the
    # corpus drifts to whichever is looser. One knob, so they can't.
    #
    # It has to run on every scheduled scrape, not by hand. Purging manually doesn't hold: the
    # table was cut to 12,711 twice in one day and a scheduled run put the rows straight back,
    # including a Palantir posting dated 2014 and Northwestern Mutual internships from 2015.
    # Flagged jobs (liked / applied / hidden) are never deleted — see db.prune_old_jobs.
    # PRUNE_DAYS=0 disables it.
    prune_days = int(os.environ.get("PRUNE_DAYS", str(MAX_AGE_DAYS)) or 0)
    if prune_days > 0:
        # db.AGE_LONG_DAYS explicitly, matching the intake gate above. Relying on the default
        # would work today and break the moment somebody passes PRUNE_DAYS without thinking
        # about the exemption; naming it here is what makes the pairing visible at the call site.
        pruned = db.prune_old_jobs(prune_days, long_days=db.AGE_LONG_DAYS)
        if pruned:
            print(f"Pruned {pruned} stale job(s) older than {prune_days} days (kept flagged ones).")

    # Retire postings that have vanished from their own board. DRY RUN unless
    # RECONCILE_CLOSED=1 — the damage case (a bot-walled board returning nothing) is bad
    # enough that the first run on any new board set should be inspected, not trusted.
    if os.environ.get("RECONCILE_CLOSED", "dry").lower() not in ("0", "off", "no"):
        try:
            closed, considered = reconcile_closed(
                board_results, apply=os.environ.get("RECONCILE_CLOSED", "").strip() == "1")
            if closed:
                print(f"Marked {closed} posting(s) closed (rows kept; Saved/Applied unaffected).")
        except Exception as e:
            print("  (closed-posting check errored, scrape unaffected: %s)" % str(e)[:120])

    # PER-BOARD HEALTH, PERSISTED. Until now the only record that a board returned nothing was
    # a line of stdout: scrape_status holds a single overwritten row, the cPanel cron log is
    # truncated at 5 MB, and Actions logs expire in 90 days. So "is every company still being
    # scraped?" could not be answered without re-running the whole sweep, and a board that
    # quietly went to zero looked exactly like a board that had no new jobs.
    #
    # Stored as a keyed blob in scrape_status rather than a new table, deliberately: there is no
    # DDL path from the scraper to the database (the proxy allowlists tables, and the cPanel
    # Postgres takes schema changes by hand), and this needs to work on the next run, not after
    # a migration.
    try:
        save_board_health(board_results)
    except Exception as e:
        print("  (board health not saved, scrape unaffected: %s)" % str(e)[:120])

    # Any board that stopped at its paging cap rather than running out of results. Printed
    # BEFORE the new-jobs list so it can't scroll off the end of a long run's output.
    trunc = truncation_report()
    if trunc:
        print(trunc)

    dropped = ", ".join("%d %s" % (n, k) for k, n in tally.items() if n)
    print(f"\nScanned {scanned_total} postings ({dropped or 'nothing dropped'}).")
    if kept_on_jd:
        print("%d kept on the DESCRIPTION alone -- the title matched nothing." % kept_on_jd)
    if DUMP_REJECTS:
        close_reject_dump()
        print(f"Dropped-posting dump written to {DUMP_REJECTS}.")
    print(f"{len(all_kept)} NEW matching job(s):")
    for j in all_kept:
        flag = "" if j["sponsors_h1b"] != "yes" else "  [sponsors H1B]"
        print(f"  - {j['title']} - {j['company']} ({j['location'] or 'n/a'}){flag}")
        print(f"    {j['url']}")
    if all_kept:
        where = db.backend_name() if db.using_supabase() else OUTPUT_CSV
        print(f"\nSaved to {where}. Run `python -m scraper.score_jobs` next to score them.")
    else:
        print("Nothing new this run.")

    # Scrape phase finished; scoring (score_jobs) runs next and will flip this to 'done'.
    db.set_scrape_status({"phase": "scoring", "done": 0, "total": 0, "found": scanned_total,
                          "new": len(all_kept), "started_at": started, "run": stamp})


if __name__ == "__main__":
    main()
