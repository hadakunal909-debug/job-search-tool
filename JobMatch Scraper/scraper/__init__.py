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
]

# Meta (metacareers.com): the ONLY source with no public feed AND no aggregator stand-in
# we trust for it — Meta's careers site is a Facebook Relay/GraphQL app, so it's scraped
# by driving a headless browser (Playwright). Kept in its own list because, unlike every
# other source, this one needs `playwright install chromium` to be present to work.
METACAREERS_BOARDS = [
    ("https://www.metacareers.com/jobs/", "metacareers", "Meta"),
]

# Everything scrapeable: Amazon + boards + Workday + iCIMS/Jibe + Oracle + Phenom +
# SuccessFactors + Adzuna + Meta.
# (Amazon-only: SOURCES = AMAZON   |   boards only: SOURCES = ATS_BOARDS + EXTRA_BOARDS)
SOURCES = (AMAZON + ATS_BOARDS + EXTRA_BOARDS + WORKDAY_BOARDS + JIBE_BOARDS
           + ORACLE_BOARDS + PHENOM_BOARDS + SF_BOARDS + ADZUNA_BOARDS
           + ADZUNA_SEARCH_BOARDS + METACAREERS_BOARDS)

OUTPUT_CSV    = "jobs.csv"        # master list; only new jobs get appended
LOG_NOTE_FILE = "log.txt"         # the scheduler writes run output here (see README)
SPONSORS_FILE = "sponsors.txt"    # OPTIONAL: one employer name per line (DOL H1B data)
RESUME_FILE   = "resume.txt"      # résumé-driven scraping reads this to tune the search

# Keep a posting only if its TITLE matches one of these (whole word/phrase, not
# substring). Specific phrases keep precision: "program manager" matches, but
# "Experiential Programs Manager" (plural 'programs') does NOT — exactly what we want.
INCLUDE = (
    # Core PM / coordination / analyst / ops titles (specific phrases).
    "project manager", "program manager", "project coordinator",
    "program coordinator", "operations coordinator", "operations manager",
    "project analyst", "business analyst", "operations analyst", "data analyst",
    "project specialist", "program specialist", "project associate",
    "operations associate", "scrum master", "project management",
    "program management", "pmo", "implementation",
    # Adjacent PM / product / supply-chain / delivery roles (specific manager phrases —
    # we intentionally do NOT add a bare "manager", which would pull store/restaurant mgrs).
    "product manager", "associate product manager", "product owner",
    "technical program manager", "implementation manager", "implementation specialist",
    "delivery manager", "engagement manager", "supply chain manager", "logistics manager",
    "marketing manager", "operations specialist", "business operations", "consultant",
    # WIDE NET — broad single words. These pull general business roles across functions
    # (finance/marketing/supply-chain/HR/etc.); the EXCLUDE list below + the per-profile
    # match score keep it relevant. Comment these four out to return to the precise net.
    "analyst", "coordinator", "specialist", "associate",
    # Entry-level consulting shapes.
    "consulting analyst", "associate consultant", "management consultant",
    "strategy consultant", "business consultant", "consulting development analyst",
    # Early-career / new-grad markers (program-style roles; low noise).
    "entry level", "entry-level", "graduate", "new grad", "early career",
    "rotation program", "rotational program", "trainee", "apprentice",
)
# ...but drop it if the title ALSO matches any of these.
EXCLUDE = (
    # Seniority markers — only CLEARLY senior ones now. The wider net intentionally KEEPS
    # mid-level roles, so "senior", "sr", "lead", "staff", "ii", "iii" were dropped from here
    # (a "Senior Analyst" / "Analyst II" now passes). Add them back to re-tighten to junior-only.
    "principal", "head", "director", "vp", "vice president", "chief", "iv", "expert", "architect",
    # Clearly off-target functions for a PM/analyst/ops search. Word boundaries
    # mean "engineer" drops "Software Engineer" but NOT "Engineering Program
    # Manager". Comment any of these back in if you DO want that function.
    "engineer", "developer", "designer", "scientist", "counsel", "attorney",
    "physician", "nurse", "account executive", "sales development", "sdr",
    # Trades / retail / hospitality — these sneak in via the early-career markers
    # ("apprentice"/"trainee"/"entry level"): e.g. Tesla's "Apprentice Collision
    # Technician" or Safeway's "Front End Entry Level".
    "technician", "technicien", "mechanic", "machinist", "welder", "electrician",
    "plumber", "detailer", "collision", "culinary", "chef", "barista", "advisor",
    "cashier", "janitor", "custodian",
    # Grocery / retail floor roles (a single big grocery board — Safeway/Albertsons —
    # otherwise floods the feed: 462 "Front End Entry Level" clerks in one run).
    "front end", "courtesy clerk", "grocery", "deli", "bakery", "cake decorator",
    "produce", "meat", "seafood", "stocker", "bagger", "checker", "store associate",
    "retail associate", "sales associate", "sales representative", "merchandiser",
    "stock clerk", "pharmacy graduate", "pharmacy intern", "warehouse associate",
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
    "product manager", "supply chain analyst", "financial analyst",
    "implementation manager", "operations specialist", "marketing analyst", "consultant",
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
    # wider net — Amazon's search is query-driven, so new terms = new pages fetched
    "product manager", "supply chain analyst", "financial analyst",
    "operations specialist", "marketing analyst", "logistics coordinator",
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
                        "what_or": "project program analyst coordinator operations implementation scrum consultant consulting",
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


# ---- UKG Pro Recruiting (UltiPro) — recruiting.ultipro.com/{CO}/JobBoard/{guid} ----
def _ultipro_base(board_url):
    """Normalize to https://recruiting.ultipro.com/{COMPANY}/JobBoard/{guid} (the two
    path segments every UKG board URL starts with; anything after the guid is UI state)."""
    m = re.search(r"(https://recruiting\.ultipro\.com/[A-Za-z0-9_-]+/JobBoard/"
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


def scrape_successfactors(board_url):
    """SAP SuccessFactors 'Career Site Builder' career sites — the classic
    jobs.<company>.com sites with /search/?q= and /job/<City>-<Title>-<id>/ URLs.
    Server-rendered HTML results table (tr.data-row), 25 rows per page, paged with
    &startrow=N. Unlocks employers (SAP, NTT DATA, many industrials/pharma) whose
    SuccessFactors backend has no public JSON API."""
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


def scrape_rippling(board_url):
    """Rippling ATS boards. No public JSON API, but the board page is server-rendered
    Next.js — each page's job list (20/page) rides in its __NEXT_DATA__ blob."""
    m = re.search(r"ats\.rippling\.com/([^/?#]+)", board_url or "")
    if not m:
        return []
    slug = m.group(1)
    rows, seen = [], set()
    for page in range(25):                            # 20/page -> up to 500 postings
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

    if host == "recruiting.ultipro.com":
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
    domains (jobs.<co>.com), so only the /search/ results-table markup gives them
    away. Returns (origin, 'successfactors', name) when the table is present."""
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
        if not soup.select_one("tr.data-row a.jobTitle-link"):
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
                r.setdefault("company", company)        # keep a per-row company if the scraper set
                                                         # one (aggregator search spans many firms)
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

    # OPTIONAL corpus pruning — OFF by default (purely additive scrape; never deletes unless asked).
    # Set PRUNE_DAYS=60 (e.g. in the cron env) to drop jobs first seen > that many days ago,
    # except any a user has liked/applied/hidden — to keep the DB bounded as the wider net grows it.
    prune_days = int(os.environ.get("PRUNE_DAYS", "0"))
    if prune_days > 0:
        pruned = db.prune_old_jobs(prune_days)
        if pruned:
            print(f"Pruned {pruned} stale job(s) older than {prune_days} days (kept flagged ones).")

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
