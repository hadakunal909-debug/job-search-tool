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
import json
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
    elif ats == "jibe":
        # The /api/jobs feed carries each job's FULL description inline — one paged
        # pass over the board covers every posting (no per-job detail calls).
        from urllib.parse import urlparse
        p = urlparse(board_url)
        base = "%s://%s" % (p.scheme or "https", p.netloc)
        for page in range(1, 21):
            d = scraper._safe_get("%s/api/jobs?limit=100&page=%d" % (base, page),
                                  timeout=25).json()
            jobs = d.get("jobs") or []
            for w in jobs:
                j = w.get("data", w) or {}
                u, desc = j.get("apply_url") or "", _text(j.get("description") or "")
                if u and desc:
                    out[u] = desc
            total = d.get("totalCount") or d.get("count") or 0
            if len(jobs) < 100 or page * 100 >= total:
                break
    elif ats == "pinpoint":
        # postings.json carries description + responsibilities + skills inline.
        d = scraper._get_json("https://%s.pinpointhq.com/postings.json"
                              % scraper._sub(board_url))
        for j in (d.get("data") or []):
            u = j.get("url") or ""
            jd = " ".join(_text(j.get(k) or "") for k in
                          ("description", "key_responsibilities",
                           "skills_knowledge_expertise", "benefits"))
            if u and jd.strip():
                out[u] = jd.strip()
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
    if ats == "jibe":
        # Jibe rows store the APPLY url, whose host varies per tenant (icims.com,
        # Oracle, ...) — there's no cheap URL test, and there are only a few jibe
        # boards, so always re-read their feeds when anything at all is missing.
        return True
    if ats == "pinpoint":
        host = scraper._sub(board_url).lower() + ".pinpointhq.com"
        return any(host in u.lower() for u in missing_urls)
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


_PHENOM_JOB_RE = re.compile(r"^(https?://[^/]+)/(?:[a-z]{2,6}/)?[a-z]{2}(?:_[a-z]{2})?/job/([^/?#]+)", re.I)


def phenom_detail_jd(url):
    """Phenom-native job pages ({origin}/us/en/job/{id}) — the jobDetail widget POST
    returns the full description (the search feed only has a ~300-char teaser)."""
    m = _PHENOM_JOB_RE.match(url)
    if not m:
        return ""
    origin, jid = m.group(1), m.group(2)
    try:
        body = scraper._phenom_body(0, 1)
        body.update({"ddoKey": "jobDetail", "jobId": jid,
                     "pageName": "job-details", "pageId": "page-job-details"})
        r = scraper._safe_post(origin + "/widgets", body, timeout=20)
        if r.status_code != 200:
            return ""
        job = (((r.json() or {}).get("jobDetail") or {}).get("data") or {}).get("job") or {}
        return _text(job.get("description") or "")
    except Exception:
        return ""


def ultipro_detail_jd(url):
    """UKG Pro: the OpportunityDetail page is a JS shell, but the full Description
    rides inside its embedded JSON (so core.fetch_jd — which drops <script> — can't
    see it; pull it out with a regex instead)."""
    try:
        r = scraper._safe_get(url, timeout=20)
        if r.status_code != 200:
            return ""
        m = re.search(r'"Description"\s*:\s*"((?:[^"\\]|\\.)*)"', r.text)
        return _text(json.loads('"%s"' % m.group(1))) if m else ""
    except Exception:
        return ""


def bamboo_detail_jd(url):
    """BambooHR: /careers/{id}/detail JSON carries the full description."""
    try:
        d = scraper._get_json(url.rstrip("/") + "/detail")
        res = d.get("result") or {}
        jo = res.get("jobOpening") if isinstance(res.get("jobOpening"), dict) else res
        return _text((jo or {}).get("description") or "")
    except Exception:
        return ""


def rippling_detail_jd(url):
    """Rippling job pages are server-rendered Next.js; the posting (incl. description)
    rides in __NEXT_DATA__. Shapes vary by tenant, so just take the longest
    description-ish string anywhere in the blob."""
    try:
        r = scraper._safe_get(url, timeout=20)
        if r.status_code != 200:
            return ""
        m = scraper._NEXT_DATA_RE.search(r.text)
        if not m:
            return ""
        best = [""]

        def walk(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    if isinstance(v, str) and "description" in k.lower() and len(v) > len(best[0]):
                        best[0] = v
                    else:
                        walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
        walk(json.loads(m.group(1)))
        return _text(best[0])
    except Exception:
        return ""


import datetime


def _parse_date_any(s):
    """Best-effort 'whatever the page says' -> 'YYYY-MM-DD' ('' if unparseable).
    Handles ISO, 'Jun 13, 2026', 'June 13, 2026', '06/13/2026', '13 Jun 2026', and
    SuccessFactors' 'Sat Jun 13 02:00:00 UTC 2026'."""
    s = (s or "").strip()
    if not s:
        return ""
    m = re.search(r"\d{4}-\d{2}-\d{2}", s)
    if m:
        return m.group(0)
    s2 = re.sub(r"\b(UTC|GMT|[A-Z]{3,4}T)\b", "", s).strip()   # drop tz token strptime chokes on
    for fmt in ("%a %b %d %H:%M:%S %Y", "%b %d, %Y", "%B %d, %Y",
                "%m/%d/%Y", "%d %b %Y", "%d %B %Y", "%b %d %Y"):
        try:
            return datetime.datetime.strptime(s2, fmt).strftime("%Y-%m-%d")
        except Exception:
            pass
    return ""


def page_posted_date(soup):
    """Pull a posting date from a job page's structured data: SuccessFactors'
    [data-careersite-propertyid=date] / [itemprop=datePosted], or an embedded
    schema.org JobPosting datePosted. Returns 'YYYY-MM-DD' or ''."""
    el = soup.select_one("[data-careersite-propertyid=date]")
    if el:
        d = _parse_date_any(el.get_text(strip=True))
        if d:
            return d
    el = soup.select_one("[itemprop=datePosted]")
    if el:
        d = _parse_date_any(el.get("content") or el.get_text(strip=True))
        if d:
            return d
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except Exception:
            continue
        for it in (data if isinstance(data, list) else [data]):
            if isinstance(it, dict) and it.get("@type") == "JobPosting":
                d = _parse_date_any(str(it.get("datePosted") or ""))
                if d:
                    return d
    return ""


def microdata_jd(url):
    """Generic deep fallback: many career sites (incl. every SuccessFactors CSB job
    page) mark the JD up with schema.org microdata (itemprop=description) or embed a
    JobPosting JSON-LD — both survive when nav noise would drown plain page text.
    Returns (jd_text, posting_date) — the page often carries the date too, which the
    list view (e.g. SAP) omits."""
    try:
        r = scraper._safe_get(url, timeout=20)
        if r.status_code != 200:
            return "", ""
        soup = BeautifulSoup(r.text, "lxml")
        date = page_posted_date(soup)
        el = soup.select_one("[itemprop=description]")
        if el:
            txt = el.get_text(" ", strip=True)
            if len(txt) > 200:
                return re.sub(r"\s{2,}", " ", txt), date
        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(tag.string or "")
            except Exception:
                continue
            items = data if isinstance(data, list) else [data]
            for it in items:
                if isinstance(it, dict) and it.get("@type") == "JobPosting":
                    txt = _text(it.get("description") or "")
                    if len(txt) > 200:
                        return txt, date
        return "", date
    except Exception:
        return "", ""


# Meta's job pages have no public API, but each detail page server-EMBEDS the JD as
# structured JSON (responsibilities + minimum/preferred qualifications). We pull those
# fields straight from the HTML — far cheaper than re-driving a browser per job. Needs
# the same full browser headers the page demands (a bare request gets a 400).
_META_BROWSER_HEADERS = {
    "User-Agent": scraper.HEADERS["User-Agent"],
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9", "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}


def _meta_json_str(txt, key):
    """Pull a JSON string value ("key":"...") out of the embedded page JSON."""
    m = re.search(r'"%s":"((?:[^"\\]|\\.)*)"' % key, txt)
    if not m:
        return ""
    try:
        return json.loads('"' + m.group(1) + '"')
    except Exception:
        return ""


def _meta_json_items(txt, key):
    """Pull a JSON list-of-{item} ("key":[{"item":".."},..]) and join the items."""
    m = re.search(r'"%s":(\[(?:[^\[\]]|\[[^\]]*\])*\])' % key, txt)
    if not m:
        return ""
    try:
        return " ".join(d.get("item", "") for d in json.loads(m.group(1))
                        if isinstance(d, dict))
    except Exception:
        return ""


def metacareers_detail_jd(url):
    """Meta job description from the detail page's embedded JSON (responsibilities +
    minimum/preferred qualifications). No browser needed — the JD is server-rendered
    into the page as JSON, just not as visible HTML (so core.fetch_jd misses it)."""
    try:
        r = scraper.SESSION.get(url, headers=_META_BROWSER_HEADERS, timeout=20)
        if r.status_code != 200:
            return ""
        t = r.text
        parts = [_meta_json_str(t, "responsibilities"),
                 _meta_json_items(t, "minimum_qualifications"),
                 _meta_json_items(t, "preferred_qualifications")]
        return _text(" ".join(p for p in parts if p))
    except Exception:
        return ""


def detail_jd(url):
    """JD + posting date for ONE job via its ATS detail endpoint, else the posting page.
    Returns (url, jd, date) — date is '' unless the page/feed exposed one."""
    jd, date = "", ""
    if "metacareers.com" in url:
        jd = metacareers_detail_jd(url)
    if not jd and "smartrecruiters.com" in url:
        jd = sr_detail_jd(url)
    if not jd and ("myworkdayjobs.com" in url or "myworkdaysite.com" in url):
        jd = wd_detail_jd(url)
    if not jd and "oraclecloud.com" in url:
        jd = oracle_detail_jd(url)
    if not jd and "apply.workable.com" in url and "/j/" in url:
        jd = workable_detail_jd(url)
    if not jd and "recruiting.ultipro.com" in url:
        jd = ultipro_detail_jd(url)
    if not jd and ".bamboohr.com/careers/" in url:
        jd = bamboo_detail_jd(url)
    if not jd and "ats.rippling.com" in url:
        jd = rippling_detail_jd(url)
    if not jd and _PHENOM_JOB_RE.match(url) and "/job/" in url:
        jd = phenom_detail_jd(url)
    if not jd:                                      # structured data beats page text
        jd, date = microdata_jd(url)
    if not jd:                                      # last resort: fetch the page
        jd = core.fetch_jd(url)
    return url, jd, date


def main():
    full = "--full" in sys.argv
    resume = open("resume.txt", encoding="utf-8").read() if os.path.exists("resume.txt") else ""
    if not resume:
        print("No resume.txt found — scores would all be 0. Aborting.")
        return

    # Progress bar: mark the SCORING phase (the scraper already set 'scraping'/'saving').
    import datetime as _dtm
    _prev = db.get_scrape_status() or {}
    _started = _prev.get("started_at") or _dtm.datetime.now(_dtm.timezone.utc).isoformat()
    db.set_scrape_status({"phase": "scoring", "done": 0, "total": 0,
                          "found": _prev.get("found", 0), "new": _prev.get("new", 0),
                          "started_at": _started, "run": _prev.get("run", "")})

    # 1) What do we already have? Stored JDs are reused (incremental); --full refetches.
    rows = db.load_jobs()
    row_jd = {r["url"]: (r.get("jd") or "") for r in rows if r.get("url")}
    row_date = {r["url"]: (r.get("found_date") or "") for r in rows if r.get("url")}
    missing = {u for u, jd in row_jd.items() if not jd or full}
    print("%d jobs: %d JDs stored, %d to fetch%s."
          % (len(row_jd), len(row_jd) - len(missing), len(missing),
             " (--full refetch)" if full else ""))

    fetched, dates = {}, {}
    if missing:
        # 2) Bulk-fetch boards whose list API already includes the JD — one request
        #    covers the whole board, so try these first. Boards run concurrently.
        boards = scraper.SOURCES + scraper.custom_sources()
        bulk = [(b, a, c) for b, a, c in boards
                if a in ("greenhouse", "lever", "ashby", "amazon", "adzuna",
                         "jibe", "pinpoint")
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
                for u, jd, date in ex.map(detail_jd, sorted(missing)):
                    if jd:                  # a failed fetch must never blank a stored JD
                        fetched[u] = jd
                    if date and not (row_date.get(u) or "").strip():
                        dates[u] = date     # the page carried a posting date the list omitted
        row_jd.update(fetched)

    # 4) Build IDF over the whole JD corpus (so common terms count less), then score
    #    EVERY job — scoring is cheap and picks up résumé edits since last run.
    idf = core.build_idf([j for j in row_jd.values() if j])
    core.save_idf(idf)
    print("Scoring %d jobs with IDF weighting (%d terms in corpus)..."
          % (len(row_jd), len(idf)))
    # Compute each job's résumé-INDEPENDENT analysis ONCE, reuse it for the score, AND persist
    # it to jdmeta.json so the web app never recomputes it at request time (kills cold-load
    # regex/keyword work). score_against(resume, analyzed) == the old skill_match(resume, jd).
    resume_low = resume.lower()
    jdmeta, scores = {}, {}
    for u, jd in row_jd.items():
        m = core.job_meta(jd, idf)
        jdmeta[u] = m
        scores[u] = core.score_against(resume_low, m["analyzed"])[0]
    core.save_jdmeta(jdmeta)

    # 5) Persist all scores, but upload only the JDs fetched THIS run — re-pushing
    #    hundreds of unchanged multi-KB JDs is what used to reset the connection.
    db.update_scores(scores)
    db.update_jds(fetched)
    if dates:                       # fill in real posting dates the list view omitted (e.g. SAP)
        db.update_job_fields([{"url": u, "found_date": d} for u, d in dates.items()])
    if scores:
        vals = list(scores.values())
        where = "Supabase" if db.using_supabase() else "jobs.csv"
        print("Done. Scored %d jobs (avg %d%%, max %d%%), %d new JD(s), %d date(s) -> %s."
              % (len(vals), sum(vals) // len(vals), max(vals),
                 len(fetched), len(dates), where))

    # Progress bar: everything finished — the feed page polls this and shows "Done".
    db.set_scrape_status({"phase": "done", "scored": len(scores),
                          "new": _prev.get("new", 0), "found": _prev.get("found", 0),
                          "started_at": _started,
                          "finished_at": _dtm.datetime.now(_dtm.timezone.utc).isoformat(),
                          "run": _prev.get("run", "")})


if __name__ == "__main__":
    main()
