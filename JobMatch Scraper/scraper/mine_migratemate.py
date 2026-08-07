#!/usr/bin/env python3
"""
mine_migratemate.py — harvest employers and postings from migratemate.co's PUBLIC pages.

Scope is deliberately limited to what their robots.txt allows: the SEO landing pages
(/companies-that-sponsor, /visa-sponsorship-jobs/*, /h1b-jobs/*, ...). It never touches
/api/ or /_next/, which that file disallows.

TWO OUTPUTS, and they are not equally useful:

  companies  The whole point. Their directory lists ~3,000 sponsor-friendly employers in a
             SINGLE request. Diffed against SOURCES and the jobs table, that's a ready-made
             target list for find_boards.py — and a board we find ourselves yields real apply
             URLs, refreshable JDs and our own scoring.

  jobs       Marginal. Each page exposes exactly 5 postings in ld+json and pagination is
             dead on the allowed paths (every ?page=/?offset= variant returns a byte-identical
             page), so the reachable slice is single-digit percent of their corpus. Worse,
             their JobPosting records carry NO url field, so a harvested posting can't be
             keyed, deduped against our 14k rows, or applied to. They are collected here for
             inspection, NOT written to the jobs table.

    python -m scraper.mine_migratemate                 # companies only (1 request)
    python -m scraper.mine_migratemate --jobs          # + the full reachable job surface
    python -m scraper.mine_migratemate --jobs --limit 200
"""
import argparse
import collections
import concurrent.futures
import csv
import json
import os
import re
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

BASE = "https://migratemate.co"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
H = {"User-Agent": UA}

# Only paths robots.txt Allows. /api/ and /_next/ are Disallowed and are never requested.
DIRECTORY = "/companies-that-sponsor"
ROLE_HUB = "/h1b-jobs"                 # carries the full role-slug list
JOB_PREFIX = "visa-sponsorship-jobs"   # the all-visa superset, so one pass covers every route

COMPANIES_CSV = "migratemate_companies.csv"
JOBS_JSON = "migratemate_jobs.json"


def get(path, tries=2):
    for i in range(tries):
        try:
            r = requests.get(BASE + path if path.startswith("/") else path,
                             headers=H, timeout=40)
            if r.status_code == 200:
                return r.text
        except Exception:
            time.sleep(1.0)
    return ""


# The directory mixes employer links with a handful of VISA-CATEGORY links under the same
# path (/companies-that-sponsor/h1b, /green-card, /f1-opt, ...). Their anchor text always ends
# in "Sponsors", which no real employer name does — matched on the anchor rather than a slug
# allowlist so a category they add later is still excluded.
_CATEGORY_ANCHOR = re.compile(r"\bsponsors\s*$", re.I)


def company_directory():
    """[(slug, name)] for every employer on the directory page — one request for the lot.

    The NAME comes from the anchor text, not the slug. Title-casing the slug is wrong for
    790 of the 2,958 entries: IBM -> "Ibm", NVIDIA -> "Nvidia", PayPal -> "Paypal",
    JPMorganChase -> "Jpmorganchase". Those mangled spellings would then be fed to the board
    prober and to _norm_name, costing matches for exactly the big sponsors we care about.
    """
    html = get(DIRECTORY)
    out, seen = [], set()
    for slug, inner in re.findall(
            r'href="/companies-that-sponsor/([^"/?#]+)"[^>]*>(.*?)</a>', html, re.S):
        if slug in seen:
            continue
        seen.add(slug)
        name = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", inner)).strip()
        if not name or _CATEGORY_ANCHOR.search(name):
            continue
        out.append((slug, name))
    return out


def role_slugs():
    html = get(ROLE_HUB)
    return sorted(set(re.findall(r'href="/h1b-jobs/([^"/?#]+)"', html)))


def jobs_on(slug):
    """The <=5 JobPosting records a role page exposes as schema.org ld+json."""
    html = get("/%s/%s" % (JOB_PREFIX, slug))
    if not html:
        return []
    out = []
    for block in re.findall(r'<script type="application/ld\+json"[^>]*>(.*?)</script>',
                            html, re.S):
        try:
            d = json.loads(block)
        except Exception:
            continue
        if not (isinstance(d, dict) and d.get("@type") == "ItemList"):
            continue
        for it in d.get("itemListElement") or []:
            jp = it.get("item") or {}
            loc, jl = "", jp.get("jobLocation")
            if isinstance(jl, dict):
                a = jl.get("address") or {}
                loc = ", ".join(x for x in (a.get("addressLocality"),
                                            a.get("addressRegion")) if x)
            sal = jp.get("baseSalary") or {}
            val = (sal.get("value") or {}) if isinstance(sal, dict) else {}
            out.append({
                "title": (jp.get("title") or "").strip(),
                "company": ((jp.get("hiringOrganization") or {}).get("name") or "").strip(),
                "location": loc,
                "posted": jp.get("datePosted") or "",
                "salary_min": val.get("minValue"), "salary_max": val.get("maxValue"),
                "salary_period": (val.get("unitText") or "").lower(),
                "jd": jp.get("description") or "",
                "found_on": "%s/%s/%s" % (BASE, JOB_PREFIX, slug),
            })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", action="store_true", help="also harvest postings (slow)")
    ap.add_argument("--limit", type=int, default=0, help="cap role pages when using --jobs")
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()

    import db
    import scraper
    nm = scraper._norm_name

    def keys(name):
        """Both the normalized name and its de-spaced form.

        Migrate Mate writes "JPMorganChase" where we write "JPMorgan Chase"; _norm_name keeps
        the space, so a plain comparison reports a company we already scrape (556 jobs) as
        brand new. Comparing the de-spaced forms too closes that class of miss, which
        otherwise inflates the gap list and wastes probe time on employers we already cover.
        """
        k = nm(name)
        return {k, k.replace(" ", "")} if k else set()

    in_sources = set()
    for _u, _t, c in list(scraper.SOURCES) + list(scraper.custom_sources()):
        in_sources |= keys(c)
    rows = db.load_jobs()
    in_db = set()
    for r in rows:
        in_db |= keys(r.get("company") or "")

    print("harvesting the company directory (1 request)...")
    comps = company_directory()
    print("  %d employers listed" % len(comps))

    seen_jobs, jobs = set(), []
    job_companies = collections.Counter()
    if a.jobs:
        slugs = role_slugs()
        if a.limit:
            slugs = slugs[:a.limit]
        print("\nharvesting postings from %d role pages "
              "(<=5 each — their pagination is dead on the allowed paths)..." % len(slugs))
        t0 = time.time()
        done = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=a.workers) as ex:
            for got in ex.map(jobs_on, slugs):
                done += 1
                if done % 250 == 0:
                    print("   %d/%d pages, %d distinct so far (%.0fs)"
                          % (done, len(slugs), len(jobs), time.time() - t0), flush=True)
                for j in got:
                    k = (j["company"].lower(), j["title"].lower(), j["location"].lower())
                    if not j["company"] or k in seen_jobs:
                        continue
                    seen_jobs.add(k)
                    jobs.append(j)
                    job_companies[j["company"]] += 1
        json.dump(jobs, open(JOBS_JSON, "w", encoding="utf-8"), indent=1)
        print("  %d distinct postings -> %s" % (len(jobs), JOBS_JSON))

    # Union the directory with any employer seen on a job card (the directory is the bulk).
    names = {}
    for _slug, pretty in comps:
        names.setdefault(nm(pretty), pretty)
    for c in job_companies:
        names.setdefault(nm(c), c)

    out = []
    for key, pretty in sorted(names.items(), key=lambda kv: kv[1].lower()):
        kk = keys(pretty)
        out.append({"company": pretty,
                    "in_sources": "yes" if kk & in_sources else "NO",
                    "in_db": "yes" if kk & in_db else "NO",
                    "mm_jobs_seen": job_companies.get(pretty, 0)})
    with open(COMPANIES_CSV, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out[0].keys()))
        w.writeheader()
        w.writerows(out)

    new_src = [r for r in out if r["in_sources"] == "NO"]
    new_db = [r for r in out if r["in_db"] == "NO"]
    print("\n=== companies ===")
    print("  total                 : %d" % len(out))
    print("  already a scrape source: %d" % (len(out) - len(new_src)))
    print("  NOT in SOURCES         : %d" % len(new_src))
    print("  not in the jobs table  : %d" % len(new_db))
    print("  -> %s" % COMPANIES_CSV)
    if a.jobs:
        print("\n=== postings (inspection only, NOT written to the jobs table) ===")
        print("  distinct           : %d" % len(jobs))
        print("  with a salary      : %d" % sum(1 for j in jobs if j["salary_min"]))
        print("  avg JD length      : %d chars"
              % (sum(len(j["jd"]) for j in jobs) / max(1, len(jobs))))
        print("  with an apply URL  : 0   (their JobPosting records carry no url field)")


if __name__ == "__main__":
    main()
