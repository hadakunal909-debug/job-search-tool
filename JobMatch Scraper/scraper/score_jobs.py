#!/usr/bin/env python3
"""
score_jobs.py — precompute the resume<->JD match score for every job in jobs.csv.

It pulls each job's real description from the SAME public ATS APIs the scraper uses
(fast, in bulk), scores it against resume.txt, and writes a `match_score` column back
to jobs.csv. The app then shows every card's match ring instantly (no live fetching).

INCREMENTAL by default: jobs that already have a stored JD in the database reuse it —
only NEW jobs get fetched (and only their JDs get uploaded back). Every job is still
re-SCORED each run (cheap, and it picks up résumé edits). Use --full to re-fetch
every JD from scratch (e.g. after a posting's description changed).

Run it after you scrape new jobs or edit resume.txt:
    python -m scraper.score_jobs          # incremental (fast)
    python -m scraper.score_jobs --full   # re-fetch every JD
"""
import os
import csv
import re
import html
import time
import random
import sys
import concurrent.futures

# Force UTF-8 stdout (Windows cp1252 consoles crash on em dashes / accents in titles).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

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
    elif ats == "adzuna":
        # Adzuna's redirect pages often block our JD page-fetch, but its search API
        # carries a (truncated) description per ad — far better than scoring against
        # nothing. Reuses the same company query the scraper runs.
        import os as _os
        app_id, app_key = _os.environ.get("ADZUNA_APP_ID"), _os.environ.get("ADZUNA_APP_KEY")
        company = board_url.split(":", 1)[1] if ":" in board_url else board_url
        if app_id and app_key:
            for page in range(1, 6):
                try:
                    data = scraper._get_json(
                        "https://api.adzuna.com/v1/api/jobs/us/search/%d" % page,
                        params={"app_id": app_id, "app_key": app_key, "company": company,
                                "what_or": "project program analyst coordinator operations implementation scrum consultant consulting",
                                "results_per_page": 50, "content-type": "application/json"})
                except Exception:
                    break
                results = data.get("results", [])
                for j in results:
                    u = j.get("redirect_url") or ""
                    desc = _text(j.get("description") or "")
                    if u and desc:
                        out[u] = desc
                if len(results) < 50 or page * 50 >= data.get("count", 0):
                    break
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
    """Workday list has no JD; fetch the posting detail (CXS) for this one job.
    Handles both URL formats ({tenant}.{dc}.myworkdayjobs.com and
    {dc}.myworkdaysite.com/recruiting/{tenant}/{site})."""
    from urllib.parse import urlparse
    try:
        host, tenant, site = scraper._workday_parts(url)
        segs = [x for x in urlparse(url).path.split("/") if x]
        jobpath = "/".join(segs[segs.index("job"):]) if "job" in segs else (segs[-1] if segs else "")
        d = scraper._get_json("https://%s/wday/cxs/%s/%s/%s" % (host, tenant, site, jobpath))
        return _text(d.get("jobPostingInfo", {}).get("jobDescription", ""))
    except Exception:
        return ""


def _board_has_missing(board_url, ats, missing_urls):
    """True if any still-missing job URL belongs to this board (cheap substring check
    on the board slug / host), so we only bulk-fetch boards that can actually help."""
    if ats == "amazon":
        return any("amazon.jobs" in u for u in missing_urls)
    if ats == "adzuna":
        return any("adzuna.com" in u for u in missing_urls)
    slug = scraper._slug(board_url).lower()
    host = {"greenhouse": "greenhouse.io", "lever": "lever.co",
            "ashby": "ashbyhq.com"}.get(ats, "")
    return any(host in u and ("/%s/" % slug) in (u.lower() + "/") for u in missing_urls)


def oracle_detail_jd(url):
    """Oracle Cloud Recruiting job detail. The LIST API truncates descriptions to
    ~300-1000 chars, so fetch the per-job detail (full description + qualifications +
    responsibilities) instead. url: .../sites/{site}/job/{id}"""
    try:
        m = re.search(r"/sites/(\w+)/job/(\d+)", url)
        if not m:
            return ""
        origin = url.split("/hcmUI/")[0]
        d = scraper._get_json(
            origin + "/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails",
            params={"onlyData": "true", "expand": "all",
                    "finder": 'ById;siteNumber=%s,Id="%s"' % (m.group(1), m.group(2))})
        items = d.get("items") or []
        if not items:
            return ""
        return " ".join(_text(items[0].get(k) or "") for k in
                        ("ExternalDescriptionStr", "ShortDescriptionStr",
                         "ExternalQualificationsStr", "ExternalResponsibilitiesStr")).strip()
    except Exception:
        return ""


def workable_detail_jd(url):
    """Workable job detail: apply.workable.com/{slug}/j/{shortcode}/ -> v2 detail JSON
    (description + requirements + benefits)."""
    try:
        segs = [s for s in url.split("/") if s]
        slug, sc = segs[segs.index("j") - 1], segs[segs.index("j") + 1]
        d = scraper._get_json(
            "https://apply.workable.com/api/v2/accounts/%s/jobs/%s" % (slug, sc))
        return " ".join(_text(d.get(k) or "") for k in
                        ("description", "requirements", "benefits")).strip()
    except Exception:
        return ""


def detail_jd(url):
    """JD for ONE job via its ATS detail endpoint, else scraping the posting page."""
    jd = ""
    if "smartrecruiters.com" in url:
        jd = sr_detail_jd(url)
    if not jd and ("myworkdayjobs.com" in url or "myworkdaysite.com" in url):
        jd = wd_detail_jd(url)
    if not jd and "oraclecloud.com" in url:
        jd = oracle_detail_jd(url)
    if not jd and "apply.workable.com" in url and "/j/" in url:
        jd = workable_detail_jd(url)
    if not jd:                                      # last resort: fetch the page
        jd = core.fetch_jd(url)
    return url, jd


def main():
    full = "--full" in sys.argv
    resume = open("resume.txt", encoding="utf-8").read() if os.path.exists("resume.txt") else ""
    if not resume:
        print("No resume.txt found — scores would all be 0. Aborting.")
        return

    # 1) What do we already have? Stored JDs are reused (incremental); --full refetches.
    rows = db.load_jobs()
    row_jd = {r["url"]: (r.get("jd") or "") for r in rows if r.get("url")}
    missing = {u for u, jd in row_jd.items() if not jd or full}
    print("%d jobs: %d JDs stored, %d to fetch%s."
          % (len(row_jd), len(row_jd) - len(missing), len(missing),
             " (--full refetch)" if full else ""))

    fetched = {}
    if missing:
        # 2) Bulk-fetch boards whose list API already includes the JD — one request
        #    covers the whole board, so try these first. Boards run concurrently.
        boards = scraper.SOURCES + scraper.custom_sources()
        bulk = [(b, a, c) for b, a, c in boards
                if a in ("greenhouse", "lever", "ashby", "amazon", "adzuna")
                and _board_has_missing(b, a, missing)]
        if bulk:
            print("Bulk-fetching JDs from %d board(s)..." % len(bulk))

            def _one(entry):
                board_url, ats, company = entry
                try:
                    time.sleep(random.uniform(0, 0.8))
                    return company, jd_map_for(board_url, ats), None
                except Exception as e:
                    return company, {}, str(e)

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
                for company, m, err in ex.map(_one, bulk):
                    if err:
                        print("  FAIL %-16s %s" % (company, err))
                        continue
                    hits = {u: jd for u, jd in m.items() if u in missing and jd}
                    fetched.update(hits)
                    print("  OK   %-16s %d of %d JDs needed" % (company, len(hits), len(m)))
            missing -= set(fetched)

        # 3) The rest need a per-job detail fetch (SmartRecruiters/Workday/page scrape) —
        #    parallel, since each is an independent host round-trip.
        if missing:
            print("Detail-fetching %d remaining JD(s)..." % len(missing))
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
                for u, jd in ex.map(detail_jd, sorted(missing)):
                    if jd:                  # a failed fetch must never blank a stored JD
                        fetched[u] = jd
        row_jd.update(fetched)

    # 4) Build IDF over the whole JD corpus (so common terms count less), then score
    #    EVERY job — scoring is cheap and picks up résumé edits since last run.
    idf = core.build_idf([j for j in row_jd.values() if j])
    core.save_idf(idf)
    print("Scoring %d jobs with IDF weighting (%d terms in corpus)..."
          % (len(row_jd), len(idf)))
    scores = {u: core.skill_match(resume, jd, idf)[0] for u, jd in row_jd.items()}

    # 5) Persist all scores, but upload only the JDs fetched THIS run — re-pushing
    #    hundreds of unchanged multi-KB JDs is what used to reset the connection.
    db.update_scores(scores)
    db.update_jds(fetched)
    if scores:
        vals = list(scores.values())
        where = "Supabase" if db.using_supabase() else "jobs.csv"
        print("Done. Scored %d jobs (avg %d%%, max %d%%), %d new JD(s) stored -> %s."
              % (len(vals), sum(vals) // len(vals), max(vals), len(fetched), where))


if __name__ == "__main__":
    main()
