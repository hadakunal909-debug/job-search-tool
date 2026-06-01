#!/usr/bin/env python3
"""
score_jobs.py — precompute the resume<->JD match score for every job in jobs.csv.

It pulls each job's real description from the SAME public ATS APIs the scraper uses
(fast, in bulk), scores it against resume.txt, and writes a `match_score` column back
to jobs.csv. The app then shows every card's match ring instantly (no live fetching).

Run it after you scrape new jobs or edit resume.txt:
    python score_jobs.py
"""
import os
import csv
import re
import html
import time
import random

import scraper
import core
import db
from bs4 import BeautifulSoup


def _text(raw):
    """HTML (or already-plain) -> clean text."""
    if not raw:
        return ""
    soup = BeautifulSoup(html.unescape(raw), "lxml")
    return re.sub(r"\s{2,}", " ", soup.get_text(" ", strip=True))


def jd_map_for(board_url, ats):
    """Return {job_url: jd_text} for one board, using the JD the API already returns."""
    slug = scraper._slug(board_url)
    out = {}
    if ats == "greenhouse":
        d = scraper._get_json(
            "https://boards-api.greenhouse.io/v1/boards/%s/jobs?content=true" % slug)
        for j in d.get("jobs", []):
            out[j.get("absolute_url", "")] = _text(j.get("content", ""))
    elif ats == "lever":
        d = scraper._get_json("https://api.lever.co/v0/postings/%s?mode=json" % slug)
        for j in d:
            lists = " ".join(_text(l.get("content", "")) for l in j.get("lists", []))
            out[j.get("hostedUrl", "")] = " ".join(
                [j.get("descriptionPlain", ""), lists, j.get("additionalPlain", "")])
    elif ats == "ashby":
        d = scraper._get_json("https://api.ashbyhq.com/posting-api/job-board/%s" % slug)
        for j in d.get("jobs", []):
            out[j.get("jobUrl", "")] = j.get("descriptionPlain", "") or _text(j.get("descriptionHtml", ""))
    elif ats == "amazon":
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(board_url).query)
        country = (q.get("country") or ["USA"])[0]
        loc = (q.get("loc_query") or ["United States"])[0]
        for term in scraper.AMAZON_QUERIES:
            offset = 0
            for _ in range(3):
                d = scraper._get_json("https://www.amazon.jobs/en/search.json", params={
                    "base_query": term, "country": country, "loc_query": loc,
                    "result_limit": 100, "offset": offset, "sort": "relevant"})
                hits = d.get("jobs", [])
                if not hits:
                    break
                for j in hits:
                    url = "https://www.amazon.jobs" + (j.get("job_path") or "")
                    out[url] = " ".join(_text(x) for x in (j.get("description"),
                        j.get("basic_qualifications"), j.get("preferred_qualifications")) if x)
                offset += len(hits)
    return out


def sr_detail_jd(url):
    """SmartRecruiters list has no JD, so fetch the posting detail for this one job."""
    try:
        parts = url.rstrip("/").split("/")
        slug, jid = parts[-2], parts[-1]
        det = scraper._get_json(
            "https://api.smartrecruiters.com/v1/companies/%s/postings/%s" % (slug, jid))
        secs = det.get("jobAd", {}).get("sections", {})
        return " ".join(_text(secs.get(k, {}).get("text", "")) for k in secs)
    except Exception:
        return ""


def wd_detail_jd(url):
    """Workday list has no JD; fetch the posting detail (CXS) for this one job."""
    from urllib.parse import urlparse
    try:
        p = urlparse(url); host = p.netloc; tenant = host.split(".")[0]
        d = scraper._get_json("https://%s/wday/cxs/%s%s" % (host, tenant, p.path))
        return _text(d.get("jobPostingInfo", {}).get("jobDescription", ""))
    except Exception:
        return ""


def main():
    resume = open("resume.txt", encoding="utf-8").read() if os.path.exists("resume.txt") else ""
    if not resume:
        print("No resume.txt found — scores would all be 0. Aborting.")
        return

    # 1) Bulk-fetch JDs for every Greenhouse/Lever/Ashby board in SOURCES.
    print("Fetching job descriptions from ATS APIs...")
    jd_map = {}
    for board_url, ats, company in scraper.SOURCES:
        if ats in ("greenhouse", "lever", "ashby", "amazon"):
            try:
                m = jd_map_for(board_url, ats)
                jd_map.update(m)
                print("  OK   %-16s %d JDs" % (company, len(m)))
            except Exception as e:
                print("  FAIL %-16s %s" % (company, e))
            time.sleep(random.uniform(1, 2))

    # 2) Resolve each job's JD (from the bulk map, or a per-job detail fetch).
    rows = db.load_jobs()
    print("Resolving JDs for %d jobs..." % len(rows))
    row_jd = {}
    for r in rows:
        u = r.get("url", "")
        if not u:
            continue
        jd = jd_map.get(u, "")
        if not jd and "smartrecruiters.com" in u:
            jd = sr_detail_jd(u)
        if not jd and "myworkdayjobs.com" in u:
            jd = wd_detail_jd(u)
        if not jd:                                  # last resort: fetch the page
            jd = core.fetch_jd(u)
        row_jd[u] = jd

    # 3) Build IDF over the whole JD corpus (so common terms count less), then score.
    idf = core.build_idf([j for j in row_jd.values() if j])
    core.save_idf(idf)
    print("Scoring with IDF weighting (%d terms in corpus)..." % len(idf))
    scores = {u: core.skill_match(resume, jd, idf)[0] for u, jd in row_jd.items()}

    # 4) Persist the scores (Supabase update, or rewrite jobs.csv).
    db.update_scores(scores)
    if scores:
        vals = list(scores.values())
        where = "Supabase" if db.using_supabase() else "jobs.csv"
        print("Done. Scored %d jobs (avg %d%%, max %d%%) -> %s."
              % (len(vals), sum(vals) // len(vals), max(vals), where))


if __name__ == "__main__":
    main()
