#!/usr/bin/env python3
"""Probe sponsor companies (the ~82 not already scraped) for a scrapeable ATS board
on Greenhouse / Lever / Ashby / SmartRecruiters. Prints ready-to-paste SOURCES lines."""
import re
import requests

H = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
SUF = (" inc", " corporation", " corp", " group", " technologies", " platforms", " company")


def _get(url):
    try:
        return requests.get(url, headers=H, timeout=8)
    except Exception:
        return None


def nospace(name):
    b = name.lower()
    for s in SUF:
        b = b.replace(s, "")
    return re.sub(r"[^a-z0-9]", "", b)


def pascal(name):
    b = name
    for s in (" Inc", " Corporation", " Corp", " Group"):
        b = b.replace(s, "")
    return re.sub(r"[^A-Za-z0-9]", "", b)


def gh(slug):
    r = _get("https://boards-api.greenhouse.io/v1/boards/%s/jobs" % slug)
    if r is not None and r.status_code == 200:
        n = len(r.json().get("jobs", []))
        return n or None


def lever(slug):
    r = _get("https://api.lever.co/v0/postings/%s?mode=json" % slug)
    if r is not None and r.status_code == 200:
        d = r.json()
        return len(d) if isinstance(d, list) and d else None


def ashby(slug):
    r = _get("https://api.ashbyhq.com/posting-api/job-board/%s" % slug)
    if r is not None and r.status_code == 200:
        n = len(r.json().get("jobs", []))
        return n or None


def sr(slug):
    r = _get("https://api.smartrecruiters.com/v1/companies/%s/postings?limit=1" % slug)
    if r is not None and r.status_code == 200:
        return r.json().get("totalFound", 0) or None


# companies = sponsors.txt minus the 'currently in SOURCES' group and Amazon
companies, skip = [], True
for line in open("sponsors.txt", encoding="utf-8"):
    s = line.strip()
    if s.startswith("# ---"):
        skip = "SOURCES" in s
        continue
    if not s or s.startswith("#") or skip or "Amazon" in s:
        continue
    companies.append(s)

print("Probing %d companies for a scrapeable board...\n" % len(companies))
URL = {"greenhouse": "https://job-boards.greenhouse.io/%s", "lever": "https://jobs.lever.co/%s",
       "ashby": "https://jobs.ashbyhq.com/%s", "smartrecruiters": "https://jobs.smartrecruiters.com/%s"}
hits = []
for name in companies:
    slug, found = nospace(name), None
    for ats, fn in (("greenhouse", gh), ("lever", lever), ("ashby", ashby)):
        n = fn(slug)
        if n:
            found = (ats, slug, n); break
    if not found:
        ps = pascal(name)
        n = sr(ps)
        if n:
            found = ("smartrecruiters", ps, n)
    if found:
        ats, sl, n = found
        print("HIT   %-24s %-15s %-20s %s jobs" % (name, ats, sl, n))
        hits.append((name, ats, sl))
    else:
        print("  --  %-24s no Greenhouse/Lever/Ashby/SR board" % name)

print("\n%d of %d have a scrapeable board.\n" % (len(hits), len(companies)))
print("SOURCES lines to add:")
for name, ats, sl in hits:
    print('    ("%s", "%s", "%s"),' % (URL[ats] % sl, ats, name))
