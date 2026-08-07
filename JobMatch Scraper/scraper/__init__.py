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
    ("https://jobs.ashbyhq.com/todyl", "ashby", "Todyl"),                                    # ~8
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
    ("https://job-boards.greenhouse.io/10xgenomics", "greenhouse", "10x Genomics"),          # ~5
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
    ("https://jobs.ashbyhq.com/ernest", "ashby", "Ernest"),                                  # ~5
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
    # --- Added 2026-06-11: big consulting sponsors with NO public feed (custom /
    # SuccessFactors / bot-walled careers sites). BCG + Accenture scrape directly.
    ("adzuna:McKinsey & Company",   "adzuna", "McKinsey & Company"),
    ("adzuna:Bain & Company",       "adzuna", "Bain & Company"),
    ("adzuna:Deloitte",             "adzuna", "Deloitte"),
    ("adzuna:EY",                   "adzuna", "EY"),
    ("adzuna:PwC",                  "adzuna", "PwC"),
    ("adzuna:KPMG",                 "adzuna", "KPMG"),
    ("adzuna:Capgemini",            "adzuna", "Capgemini"),
    # --- Added 2026-06-18: top FY2026-Q2 H1B sponsors whose own sites can't be scraped
    # server-side — Eightfold/custom career portals behind bot-walls (403) or session-coupled
    # Taleo. The aggregator is the sanctioned route for these (same as Tesla/Google). ---
    ("adzuna:Verizon",              "adzuna", "Verizon"),            # Eightfold (Happydance), bot-walled
    ("adzuna:Goldman Sachs",        "adzuna", "Goldman Sachs"),      # custom higher.gs.com
    ("adzuna:Nutanix",              "adzuna", "Nutanix"),            # custom/Eightfold SPA
    ("adzuna:Zoom",                 "adzuna", "Zoom Video Communications"),  # Greenhouse+Clinch behind AWS WAF
]

# Generic Adzuna role searches across ALL employers (not company-scoped) — the widest single
# lever: pulls postings from the thousands of firms Adzuna indexes, including custom-portal
# companies we can't scrape directly. Each phrase = up to 4 API pages; keep the list modest so
# the daily run stays within Adzuna's free ~250 calls/day budget. DORMANT without a key.
ADZUNA_SEARCH_BOARDS = [
    ("adzuna-search:project manager",     "adzuna-search", "Adzuna"),
    ("adzuna-search:program manager",     "adzuna-search", "Adzuna"),
    ("adzuna-search:project coordinator", "adzuna-search", "Adzuna"),
    ("adzuna-search:business analyst",    "adzuna-search", "Adzuna"),
    ("adzuna-search:product manager",     "adzuna-search", "Adzuna"),
    ("adzuna-search:operations analyst",  "adzuna-search", "Adzuna"),
    ("adzuna-search:implementation manager", "adzuna-search", "Adzuna"),
    ("adzuna-search:supply chain analyst", "adzuna-search", "Adzuna"),
    # "project manager" already surfaces "assistant project manager"; add project controls
    # explicitly so the aggregator pulls that family from employers we don't scrape directly.
    ("adzuna-search:project controls",    "adzuna-search", "Adzuna"),
    # Internship / co-op pulls — the LEGITIMATE stand-in for Handshake/university portals
    # (those are login-gated, student-only, no public feed). Adzuna indexes thousands of the
    # same employers; these intern-specific phrases surface the intern roles that the full-time
    # role searches above bury. The title filter trims each pull to PM/controls/product/ops interns.
    ("adzuna-search:project management intern", "adzuna-search", "Adzuna"),
    ("adzuna-search:manager intern",       "adzuna-search", "Adzuna"),
    ("adzuna-search:coordinator intern",   "adzuna-search", "Adzuna"),
    ("adzuna-search:analyst intern",       "adzuna-search", "Adzuna"),
    ("adzuna-search:operations intern",    "adzuna-search", "Adzuna"),
    ("adzuna-search:management co-op",     "adzuna-search", "Adzuna"),
    # Software engineering (2026-08-01). Deliberately a SHORT list of high-yield umbrella
    # phrases rather than one per title — each phrase costs up to 4 of Adzuna's ~250
    # free calls/day, and "software engineer" alone already surfaces the senior/junior/
    # frontend/backend variants. Trim these first if the daily budget starts erroring.
    ("adzuna-search:software engineer",    "adzuna-search", "Adzuna"),
    ("adzuna-search:software developer",   "adzuna-search", "Adzuna"),
    ("adzuna-search:full stack developer", "adzuna-search", "Adzuna"),
    ("adzuna-search:data engineer",        "adzuna-search", "Adzuna"),
    ("adzuna-search:data scientist",       "adzuna-search", "Adzuna"),
    ("adzuna-search:machine learning engineer", "adzuna-search", "Adzuna"),
    ("adzuna-search:devops engineer",      "adzuna-search", "Adzuna"),
    ("adzuna-search:qa engineer",          "adzuna-search", "Adzuna"),
    ("adzuna-search:software engineering intern", "adzuna-search", "Adzuna"),
]

# Meta (metacareers.com): the ONLY source with no public feed AND no aggregator stand-in
# we trust for it — Meta's careers site is a Facebook Relay/GraphQL app, so it's scraped
# by driving a headless browser (Playwright). Kept in its own list because, unlike every
# other source, this one needs `playwright install chromium` to be present to work.
METACAREERS_BOARDS = [
    ("https://www.metacareers.com/jobs/", "metacareers", "Meta"),
]

# Everything scrapeable: Amazon + boards + Workday + iCIMS/Jibe + Oracle + Phenom +
# Avature + SuccessFactors + Adzuna + Meta.
# (Amazon-only: SOURCES = AMAZON   |   boards only: SOURCES = ATS_BOARDS + EXTRA_BOARDS)
SOURCES = (AMAZON + ATS_BOARDS + EXTRA_BOARDS + WORKDAY_BOARDS + JIBE_BOARDS
           + ORACLE_BOARDS + PHENOM_BOARDS + AVATURE_BOARDS + ULTIPRO_BOARDS + JOBDIVA_BOARDS
           + SF_BOARDS + ADZUNA_BOARDS + ADZUNA_SEARCH_BOARDS + METACAREERS_BOARDS)

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
    # product strategy family (PM-adjacent; product-scoped so it avoids the
    # marketing "brand/content/media strategist" noise that bare "strategist" pulls).
    "product strategist", "product strategy",
    # --- Coordination / operations / analyst (related domain) ---
    "operations coordinator", "operations manager", "operations analyst",
    "operations specialist", "operations associate", "business operations",
    "business analyst", "data analyst",
    "implementation", "implementation manager", "implementation specialist",
    "delivery manager", "engagement manager",
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
    "data scientist", "applied scientist", "machine learning scientist",
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
    # --- Early-career / new-grad markers (program-style roles; low noise) ---
    "entry level", "entry-level", "graduate", "new grad", "early career",
    "rotation program", "rotational program", "trainee", "apprentice",
    # Internships & co-ops — OPT/STEM-OPT lets the user do these. The matcher is
    # whole-word, so plurals/variants are listed explicitly. The EXCLUDE block still
    # drops eng/clinical/retail/trades interns (incl. the eng/research-intern phrases
    # added to EXCLUDE below, which the word-boundary "engineer"/"scientist" miss).
    "intern", "interns", "internship", "internships",
    "co-op", "co-ops", "coop", "coops", "co op",
    "summer analyst", "summer associate",
)
# ...but drop it if the title ALSO matches any of these.
EXCLUDE = (
    # Seniority markers — only CLEARLY senior ones now. The wider net intentionally KEEPS
    # mid-level roles, so "senior", "sr", "lead", "staff", "ii", "iii" were dropped from here
    # (a "Senior Analyst" / "Analyst II" now passes). Add them back to re-tighten to junior-only.
    "principal", "head", "director", "vp", "vice president", "chief", "iv", "expert", "architect",
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
    "passenger engineer", "train engineer", "locomotive",
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

# Print a KEEP/drop line (with the reason) for every scraped title. Great for
# tuning the filter on ONE board, but noisy across many — so it's off by default.
# Flip to True (ideally with just one board in SOURCES) to see why titles drop.
VERBOSE = False

# Drop a job if its description requires MORE than this many years of experience.
# Only enforced where the scraper actually has the JD text (e.g. Amazon). 5 = keep mid-level too.
MAX_YEARS = 5

# Refuse a posting the employer published more than this many days ago — by then the role
# is usually filled, and storing it just inflates the database. Only applied when the board
# actually publishes a date: a posting with NO date (Meta, Workable, BambooHR, Rippling, and
# one of the two Avature templates) is KEPT and ages by first_seen instead, because for those
# boards "still listed" is the only freshness signal there is. 0 disables the gate.
MAX_AGE_DAYS = int(os.environ.get("MAX_AGE_DAYS", "30") or 0)

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
))

# Greenhouse serves every board under two interchangeable hostnames, and the API's
# absolute_url has flipped between them over time — so one posting arrives as
# boards.greenhouse.io/<co>/jobs/<id>?gh_jid=<id> on an old run and as
# job-boards.greenhouse.io/<co>/jobs/<id> on a later one, landing twice in `jobs`
# (which is keyed on url alone).
_GH_HOSTS = frozenset(("boards.greenhouse.io", "job-boards.greenhouse.io"))


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

def _posted(s):
    """ISO timestamp -> 'YYYY-MM-DD' ('' stays '' so main()'s scrape-stamp fallback kicks in)."""
    return (str(s) if s else "")[:10]


def scrape_greenhouse(board_url):
    data = _get_json("https://boards-api.greenhouse.io/v1/boards/%s/jobs" % _slug(board_url))
    rows = []
    for j in data.get("jobs", []):
        row = {"title": (j.get("title") or "").strip(),
               "url": j.get("absolute_url", ""),
               "location": (j.get("location") or {}).get("name", "")}
        d = _posted(j.get("first_published") or j.get("updated_at"))
        if d:
            row["found_date"] = d                # the REAL posting date, not the scrape date
        rows.append(row)
    return rows


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
               "location": j.get("location") or ""}
        d = _posted(j.get("publishedAt"))
        if d:
            row["found_date"] = d
        rows.append(row)
    return rows


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


WORKDAY_QUERIES = (
    "program manager", "project manager", "project coordinator",
    "program coordinator", "business analyst", "operations analyst",
    # wider net — surface the new role types in Workday's ranked search too
    "product manager", "supply chain analyst", "operations specialist",
    "implementation manager", "product owner",
    # project controls / scheduling / PMO family
    "project controls", "scheduler", "project scheduler", "pmo", "portfolio manager",
    "project planner", "cost analyst",
    # software engineering (2026-08-01) — Workday's search is query-driven, so without
    # these terms a SWE role on a Workday tenant is never even fetched to be filtered.
    "software engineer", "software developer", "full stack", "front end", "back end",
    "data engineer", "data scientist", "machine learning engineer", "devops",
    "qa engineer", "test engineer", "cloud engineer", "systems engineer",
    "application developer", "web developer", "database administrator",
    # internships / co-ops (OPT-eligible)
    "intern", "internship", "co-op", "summer analyst",
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
    else:
        # Fell out on WORKDAY_MAX_JOBS. Worse here than elsewhere: we page with an EMPTY
        # search, so the order is the tenant's own and the jobs we never see are an
        # arbitrary slice, not the low-relevance tail.
        note_truncation(board_url, offset, WORKDAY_MAX_JOBS, total)
    return rows


AMAZON_QUERIES = (
    "program manager", "project manager", "project coordinator",
    "program coordinator", "business analyst", "operations manager",
    "data analyst", "implementation",
    # wider net — Amazon's search is query-driven, so new terms = new pages fetched
    "product manager", "supply chain analyst", "operations specialist",
    "product owner", "project planner",
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
)


# Amazon's search.json caps a page at 100 and reports the true total in `hits`, so we can
# page a term to exhaustion. MAX_PER_TERM is only a safety stop, mirroring WORKDAY_MAX_JOBS.
AMAZON_PAGE_LIMIT = 100
AMAZON_MAX_PER_TERM = 1000


def scrape_amazon(board_url):
    """Amazon's own portal via its public search.json feed. Runs each term in
    AMAZON_QUERIES (US-only) and pages it to exhaustion; the title filter then decides
    what to keep.

    Pages EVERY result, not the first two pages. Amazon's relevance ranking buries plenty
    of on-target roles: measured 2026-08-01, "program manager" returns 734 US hits and
    "Program Manager, Relo Ops Excellence (RLOI)" sits at #351 — invisible to the old
    2-page (200-result) window. Across results 201-800 for that one term, 316 more titles
    passed the filter than the 168 the window caught, i.e. the cap was costing us about
    two thirds of Amazon. Same lesson scrape_workday learned; see its docstring."""
    from urllib.parse import urlparse, parse_qs
    q = parse_qs(urlparse(board_url).query)
    country = (q.get("country") or ["USA"])[0]
    loc = (q.get("loc_query") or ["United States"])[0]
    seen, rows = set(), []
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
                        # 2026-08-01: software/data terms added alongside the PM ones so a
                        # company-scoped pull spends its 250-result budget on BOTH tracks.
                        "what_or": ("project program analyst coordinator operations implementation "
                                    "scrum consultant consulting software engineer developer "
                                    "data scientist devops"),
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
    else:
        # Ran all 5 pages with more still available. Deliberately NOT raised: Adzuna's free
        # tier is ~250 calls/day and these company pulls already use most of it. This is a
        # budget ceiling, not an oversight — but it should still be visible.
        note_truncation("adzuna:" + company, len(seen), 250, data.get("count"),
                        detail="(Adzuna free-tier budget)")
    return rows


def scrape_adzuna_search(board_url):
    """Generic Adzuna search across ALL employers for a role phrase — the single WIDEST source:
    it pulls jobs from the thousands of companies Adzuna indexes, including custom-portal employers
    we can't scrape directly. board_url is 'adzuna-search:<phrase>' (e.g. 'adzuna-search:project
    manager'). Each row carries its OWN employer (set here; scrape_all won't clobber it). main()'s
    title + US filter still trims it.

    DORMANT without an Adzuna key. Free-tier friendly: capped at 4 pages (200 results) per phrase
    and recent postings only, so a handful of phrases stay within the ~250 calls/day budget."""
    app_id  = os.environ.get("ADZUNA_APP_ID")
    app_key = os.environ.get("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        return []
    query = board_url.split(":", 1)[1].strip() if ":" in board_url else board_url
    rows, seen = [], set()
    for page in range(1, 5):                          # up to 4 pages x 50 = 200 results per phrase
        try:
            data = _get_json(
                "https://api.adzuna.com/v1/api/jobs/us/search/%d" % page,
                params={"app_id": app_id, "app_key": app_key,
                        "what_phrase": query,         # exact phrase keeps results on-role
                        "results_per_page": 50, "max_days_old": 30,
                        "content-type": "application/json"})
        except Exception:
            break
        results = data.get("results", [])
        if not results:
            break
        for j in results:
            url = j.get("redirect_url") or ""
            if not url or url in seen:
                continue
            seen.add(url)
            rows.append({
                "title": (j.get("title") or "").strip(),
                "url": url,
                "company": ((j.get("company") or {}).get("display_name") or "").strip(),
                "location": ((j.get("location") or {}).get("display_name") or ""),
                "found_date": (j.get("created") or "")[:10],
            })
        if len(results) < 50 or page * 50 >= data.get("count", 0):
            break
        time.sleep(random.uniform(0.3, 0.7))
    else:
        note_truncation("adzuna-search:" + query, len(seen), 200, data.get("count"),
                        detail="(Adzuna free-tier budget)")
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
    a 'City, ST' shape (state abbr as the LAST token, no zip after) stays US."""
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


def _csb_sitemap_rows(base):
    try:
        r = _safe_get(base + "/sitemap.xml", timeout=30)
    except Exception:
        return []                                     # non-public host (SSRF guard) or unreachable
    if r.status_code != 200:
        return []
    locs = [u for u in re.findall(r"<loc>([^<]+)</loc>", r.text) if "/job/" in u]
    if US_ONLY:
        cands = [u for u in locs
                 if (lambda m: m and m.group(1).upper() in US_STATE_ABBR)(_CSB_US_SLUG.search(unquote(u)))]
    else:
        cands = locs
    rows = []
    for u in cands[:CSB_MAX_ROWS]:
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
               "url": j.get("url") or "", "location": loc_s}
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


def _avature_location(card, href):
    """A card's location, handling both templates. The 'article--result' template (Bloomberg
    etc.) prints a single .list-item-location; the 'listSingleColumnItem' template (NVA) uses
    City:/State: spans + the country embedded in the JobDetail slug (…-United-States-… /
    …-Canada-…). '' when the tenant omits location entirely (e.g. Synopsys)."""
    el = card.select_one(".list-item-location")
    if el:
        return el.get_text(" ", strip=True).rstrip(".").strip()
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


def scrape_avature(board_url):
    """Avature career portals (<tenant>.avature.net/<portal>/SearchJobs). Walks the whole
    board via ?jobOffset=N, handling both card templates (listSingleColumnItem / article--result)
    via _avature_location + _avature_date. The title (its /JobDetail/ link) is in both; the
    title + US filter in main() trims the result."""
    base = _avature_base(board_url)
    rows, seen, offset = [], set(), 0
    while offset < AVATURE_MAX_JOBS:
        try:
            r = SESSION.get("%s/?jobOffset=%d" % (base, offset), headers=HEADERS, timeout=25)
        except Exception:
            break
        if r.status_code != 200:
            break
        cards = BeautifulSoup(r.text, "lxml").select("li.listSingleColumnItem, article.article--result")
        new = 0
        for c in cards:
            a = c.select_one("a[href*='/JobDetail/']")
            if not a or not a.get("href"):
                continue                                    # 'no results' placeholder card
            href = urljoin(base + "/", a["href"]).split("?")[0]
            if href in seen:
                continue
            seen.add(href)
            new += 1
            row = {"title": a.get_text(" ", strip=True).strip(), "url": href,
                   "location": _avature_location(c, href)}
            d = _avature_date(c)
            if d:
                row["found_date"] = d
            rows.append(row)
        offset += len(cards) or AVATURE_PAGE                # advance by the real page size
        if not cards or new == 0:                           # reached the end of the board
            break
        time.sleep(random.uniform(0.2, 0.4))
    else:
        note_truncation(board_url, offset, AVATURE_MAX_JOBS)
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
    "adzuna-search": scrape_adzuna_search,
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
    "metacareers": scrape_metacareers,
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

    if re.match(r"recruiting\d*\.ultipro\.com$", host):          # recruiting / recruiting2 / …
        base = _ultipro_base(url)
        if base:
            return (base, "ultipro", _name_from(urlparse(base).path.split("/")[1]))

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

    if host.endswith("jobdiva.com"):                        # www1.jobdiva.com/portal/?a=<token>
        token = (parse_qs(p.query).get("a") or [""])[0]
        if token:
            return ("https://www1.jobdiva.com/portal/?a=%s" % token, "jobdiva",
                    _jobdiva_agency(token) or "JobDiva portal")

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
      | [a-z0-9.-]+\.oraclecloud\.com/hcmUI/CandidateExperience[A-Za-z0-9_/.-]*/sites/[A-Za-z0-9_]+
      | recruiting\.ultipro\.com/[A-Za-z0-9_-]+/JobBoard/[0-9a-fA-F-]{36}
      | [a-z0-9-]+\.bamboohr\.com/careers
      | [a-z0-9-]+\.pinpointhq\.com
      | ats\.rippling\.com/[A-Za-z0-9_-]+
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
            return len(soup.select("tr.data-row a.jobTitle-link")) or None
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
    return False, "no PM/coordinator/analyst/software keyword"


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
    "guadalajara", "monterrey", "bogota", "medellin", "lima", "cebu"}

_STATE_ABBR_RE = re.compile(r",\s*([A-Za-z]{2})\b")

# Whole-word matcher for the NON_US list. Substring matching burned us: 'india' is
# inside 'Indianapolis', so every Indianapolis job was silently dropped (found
# 2026-06-12). Word boundaries also let bare 'UK' match at the start of a string.
_NON_US_RE = re.compile(
    r"\b(?:%s)\b" % "|".join(sorted((re.escape(t.strip()) for t in NON_US),
                                    key=len, reverse=True)))


def is_us_location(loc):
    """Heuristic: True if the location looks US-based. Unknown/blank -> kept."""
    if not loc:
        return True
    low = loc.lower()
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

def scrape_all(sources, workers=8, progress=None, board_results=None):
    """Scrape boards CONCURRENTLY (each is an independent host) so the whole run takes
    a few minutes, not ~30. One bad source never stops the run. `progress(done, total,
    found)` is called after each board finishes (used to drive the in-page progress bar).

    Pass `board_results` (a list) to also collect per-board outcomes as dicts
    {entry, ok, urls} — reconcile_closed() needs to know which board a URL came from and
    whether that board's fetch actually succeeded.
    """
    def _one(entry):
        url, ats_type, company = entry
        fn = SCRAPERS.get(ats_type)
        if fn is None:
            return entry, company, None, "unknown ats_type '%s'" % ats_type
        try:
            time.sleep(random.uniform(0, 1.0))          # small stagger so we don't burst one API
            rows = fn(url)
            for r in rows:
                r.setdefault("company", company)        # keep a per-row company if the scraper set
                                                         # one (aggregator search spans many firms)
            return entry, company, rows, None
        except Exception as e:
            return entry, company, None, str(e)

    all_jobs = []
    total = len(sources) if hasattr(sources, "__len__") else 0
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for entry, company, rows, err in ex.map(_one, sources):  # results come back in source order
            done += 1
            if err is not None:
                print(f"  FAIL {company:<26} {err}")
            elif rows is None:
                print(f"  SKIP {company:<26}")
            else:
                all_jobs.extend(rows)
                print(f"  OK   {company:<26} {len(rows):>3} postings")
            if board_results is not None:
                board_results.append({
                    "entry": entry, "company": company, "ok": rows is not None,
                    "urls": {r.get("url") for r in (rows or []) if r.get("url")}})
            if progress:
                try:
                    progress(done, total, len(all_jobs))
                except Exception:
                    pass
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
# one run means nothing, so they can never close a row.
RECONCILE_SKIP_ATS = {"adzuna"}


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


def reconcile_closed(board_results, apply=False):
    """Mark jobs that have vanished from their own board as closed. Returns (closed, considered).

    `apply=False` reports what it WOULD do and writes nothing — always run that first on a
    new board set, because the failure mode (a bot-walled board returning an empty list)
    would otherwise retire its entire inventory in one pass.
    """
    try:
        rows = db.load_jobs(include_jd=False)
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

    # Canonicalize BOTH sides of the dedupe: stored rows predate normalization (and hold
    # e.g. the old boards.greenhouse.io form), so comparing raw would re-insert them.
    # Compared CASE-INSENSITIVELY. Workday's site segment isn't case-stable — Applied
    # Materials' board answered on both /external/ and /External/, and because those are
    # different strings the same 79 postings were stored twice, under two different company
    # labels ("Amat" and "Applied Materials"), and rendered as duplicate cards. Two genuinely
    # distinct postings whose URLs differ only by letter case don't occur in practice.
    seen = {canonical_url(u).lower() for u in db.existing_urls()}
    sources = SOURCES + custom_sources()
    if len(sources) > len(SOURCES):
        print("+ %d board(s) added via the app." % (len(sources) - len(SOURCES)))

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
    scraped = scrape_all(sources, progress=_progress, board_results=board_results)
    _progress(len(sources), len(sources), len(scraped), phase="saving", force=True)

    kept = []
    tally = {"already known": 0, "senior/off-target title": 0,
             "no matching role keyword": 0, "non-US location": 0,
             "posted over %d days ago" % MAX_AGE_DAYS: 0}
    age_cutoff = ((datetime.date.today() - datetime.timedelta(days=MAX_AGE_DAYS)).isoformat()
                  if MAX_AGE_DAYS > 0 else "")
    for j in scraped:
        j["url"] = canonical_url(j.get("url", ""))
        if j["url"].lower() in seen:
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
        # Freshness gate. This has to run BEFORE the setdefault below: that line stamps
        # undated rows with today's date, so a gate placed after it would see every dateless
        # board as brand new and could never reject anything. Here the value is still exactly
        # what the employer published — a date, an empty string, or nothing at all.
        if age_cutoff:
            posted = (j.get("found_date") or "")[:10]
            if posted and posted < age_cutoff:
                tally["posted over %d days ago" % MAX_AGE_DAYS] += 1
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
        j.setdefault("found_date", stamp)        # keep the JD's posting date if set
        seen.add(j["url"].lower())               # two boards in ONE run can serve the same
                                                 # posting (e.g. both Greenhouse hosts)
        kept.append({k: j.get(k, "") for k in FIELDNAMES})

    try:            # persist this run's new jobs to disk FIRST so a DB hiccup can't lose the scrape
        json.dump(kept, open("last_new_jobs.json", "w", encoding="utf-8"))
    except Exception:
        pass
    if kept:
        db.add_jobs(kept)               # (also the breadcrumb notify.py reads for this run's alerts)

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
        pruned = db.prune_old_jobs(prune_days)
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

    # Any board that stopped at its paging cap rather than running out of results. Printed
    # BEFORE the new-jobs list so it can't scroll off the end of a long run's output.
    trunc = truncation_report()
    if trunc:
        print(trunc)

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

    # Scrape phase finished; scoring (score_jobs) runs next and will flip this to 'done'.
    db.set_scrape_status({"phase": "scoring", "done": 0, "total": 0, "found": len(scraped),
                          "new": len(kept), "started_at": started, "run": stamp})


if __name__ == "__main__":
    main()
