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
import gzip
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
    elif ats == "jobdiva":
        # The JobDiva list rows carry the full jobDescription inline — one paged pass
        # over the portal covers every posting (no per-job detail calls).
        token = scraper._jobdiva_token(board_url)
        jh = scraper._jobdiva_session(token) if token else None
        if jh:
            for data in scraper._jobdiva_pages(token, jh):
                for j in data:
                    u = "https://www1.jobdiva.com/portal/?a=%s#/jobs/%s" % (token, j.get("id"))
                    jd = _text(j.get("jobDescription") or "")
                    if jd:
                        out[u] = jd
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
    {dc}.myworkdaysite.com/recruiting/{tenant}/{site}).

    Returns (jd, date). The date is the free half of this request: the same response already
    being fetched for the JD carries jobPostingInfo.startDate as a bare ISO date, and it was
    being thrown away while the stored date came from parsing "Posted 30+ Days Ago" off the
    LIST view.

    That matters because "30+" is a CEILING, not a measurement. Checked against 11 tenants
    2026-08-09: startDate agreed exactly with the postedOn-derived date on all 8 rows showing a
    real "Posted N Days Ago", and disagreed on all 3 showing "30+" — by 30, 37 and 77 days, in
    every case because the derived value had been clamped to MAX_AGE_DAYS+1. Zero startDates
    were in the future, which is what would give away an employment start date rather than a
    posting date. So this is not a second opinion; it is the accurate one.

    This is also the fix for the damage recorded in scraper._workday_date's docstring — 1,943 of
    3,858 newly added rows carrying a found_date of exactly MAX_AGE_DAYS ago, all of them the
    clamp. Expect the correction to age some rows past the freshness window on first application.
    """
    from urllib.parse import urlparse
    try:
        host, tenant, site = scraper._workday_parts(url)
        segs = [x for x in urlparse(url).path.split("/") if x]
        jobpath = "/".join(segs[segs.index("job"):]) if "job" in segs else (segs[-1] if segs else "")
        d = scraper._get_json("https://%s/wday/cxs/%s/%s/%s" % (host, tenant, site, jobpath))
        info = d.get("jobPostingInfo") or {}
        date = _parse_date_any(str(info.get("startDate") or ""))
        return _text(info.get("jobDescription", "")), (date or "")
    except Exception:
        return "", ""


def _board_has_missing(board_url, ats, missing_urls):
    """True if any still-missing job URL belongs to this board (cheap substring check
    on the board slug / host), so we only bulk-fetch boards that can actually help."""
    if ats == "amazon":
        return any("amazon.jobs" in u for u in missing_urls)
    if ats == "jobdiva":
        return any("jobdiva.com" in u for u in missing_urls)
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


def workatastartup_detail_jd(url):
    """Y Combinator's Work at a Startup — an Inertia.js app, so the page ships its whole
    payload in a single `data-page` attribute and renders client-side.

    This was the ONLY 'extractable-MISSING' class left in the JD backlog (38 rows): the host
    answers 200 with 66-93 KB and the generic text extractor still pulls nothing, because the
    description never exists as markup. audit_jd_coverage.py exists to separate exactly this
    case from the 519 rows that are genuinely blocked or gone.

    Takes descriptionHtml plus interviewProcessHtml — the interview stages name the stack and
    the seniority bar, which is signal the scorer can use — and the company's tech blurb.
    """
    try:
        r = scraper._safe_get(url, timeout=20)
        if r.status_code != 200:
            return ""
        m = re.search(r'data-page="([^"]+)"', r.text)
        if not m:
            return ""
        d = json.loads(html.unescape(m.group(1))).get("props") or {}
        job, company = d.get("job") or {}, d.get("company") or {}
        parts = [job.get("descriptionHtml"), job.get("interviewProcessHtml"),
                 company.get("techDescriptionHtml"), company.get("description")]
        return " ".join(_text(p) for p in parts if p).strip()
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


def paylocity_detail_jd(url):
    """Paylocity posting pages. The company's job LIST is client-rendered, but a posting page
    is plain server-rendered HTML with the description in div.job-preview-details."""
    if "recruiting.paylocity.com" not in (url or "").lower():
        return ""
    try:
        r = scraper._safe_get(url, timeout=25)
        if r.status_code != 200:
            return ""
        el = BeautifulSoup(r.text, "lxml").select_one("div.job-preview-details")
        return _text(el.get_text(" ", strip=True)) if el else ""
    except Exception:
        return ""


_PS_JOB_RE = re.compile(r"HRS_HRAM_FL\.HRS_CG_SEARCH_FL\.GBL.*[?&]JobOpeningId=\d+", re.I)


def peoplesoft_detail_jd(url):
    """PeopleSoft posting pages. The description sits in HRS_SCH_PSTDSC_DESCRLONG$N spans —
    one per posting section (overview, responsibilities, qualifications, ...), which is why
    they're joined rather than taking the first.

    Targeted extraction, not a whole-page scrape: the page also carries the portal chrome and
    the 50-row search grid, so core.fetch_jd's page text would bury the actual posting in
    other jobs' titles — and then the résumé match would be scoring the wrong thing."""
    if not _PS_JOB_RE.search(url or ""):
        return ""
    try:
        r = scraper._safe_get(url, timeout=25)
        if r.status_code != 200:
            return ""
        soup = BeautifulSoup(r.text, "lxml")
        parts = [el.get_text(" ", strip=True) for el in
                 soup.select("span[id^='HRS_SCH_PSTDSC_DESCRLONG$']")]
        return _text(" ".join(p for p in parts if p))
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


def _date_beats_stored(new, stored):
    """Should a date read off the detail page replace what the scrape stored?

    Yes when the stored value is blank, and yes when it carries a time ("YYYY-MM-DD HH:MM") —
    that shape is this codebase's marker for a DERIVED date, either the scrape stamp or
    _workday_date()'s reading of "Posted N Days Ago". A date the ATS states outright beats both,
    and beats "30+ Days Ago" by a lot: that string is a ceiling clamped to MAX_AGE_DAYS+1, and
    three sampled tenants were really 30, 37 and 77 days old.

    No when the stored value is already a bare ISO date. That came from a publisher field
    (Greenhouse first_published, Adzuna created, Lever createdAt) and is not ours to churn —
    scripts/audit_dates.py exists to question those separately.

    posted_verified is deliberately not consulted: only found_date is written here, and every
    consumer (web._build_row, db.row_age_date) already prefers posted_verified over it.
    """
    new = (new or "").strip()
    if not new:
        return False
    s = (stored or "").strip()
    return (not s) or (" " in s)


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
            # strict=False, and it is not cosmetic: JSON forbids a raw newline inside a
            # string, and a site that pastes an HTML job description straight into its
            # JSON-LD emits exactly that. Michael Page does, on every posting — the block
            # parses as far as the description and then raises, so with strict parsing this
            # loop skipped a perfectly good datePosted (and, below, a 4.8k-char JD) and the
            # job silently read as "JD pending" forever. Browsers and Google's parser are
            # equally lenient here; matching them costs nothing on well-formed data.
            data = json.loads(tag.string or "", strict=False)
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
                data = json.loads(tag.string or "", strict=False)   # see page_posted_date
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
        jd, date = wd_detail_jd(url)      # the CXS response carries the posting date too
    if not jd and "oraclecloud.com" in url:
        jd = oracle_detail_jd(url)
    if not jd and "apply.workable.com" in url and "/j/" in url:
        jd = workable_detail_jd(url)
    if not jd and re.search(r"recruiting\d*\.ultipro\.com", url):   # recruiting / recruiting2 / …
        jd = ultipro_detail_jd(url)
    if not jd and ".bamboohr.com/careers/" in url:
        jd = bamboo_detail_jd(url)
    if not jd and "ats.rippling.com" in url:
        jd = rippling_detail_jd(url)
    if not jd and "workatastartup.com" in url:
        jd = workatastartup_detail_jd(url)
    if not jd and _PHENOM_JOB_RE.match(url) and "/job/" in url:
        jd = phenom_detail_jd(url)
    if not jd and "HRS_HRAM_FL" in url:                              # PeopleSoft posting page
        jd = peoplesoft_detail_jd(url)
    if not jd and "recruiting.paylocity.com" in url:
        jd = paylocity_detail_jd(url)
    if not jd:                                      # structured data beats page text
        jd, date = microdata_jd(url)
    if not jd:                                      # last resort: fetch the page
        jd = core.fetch_jd(url)
    return url, jd, date


def _norm_cmp(key, val):
    """Comparable form of a derived value. Supabase hands back real booleans/ints but the
    local jobs.csv fallback hands back strings, and "False" is a truthy string — comparing
    raw would mark every row changed on every run."""
    if val is None or val == "":
        return None
    if key == "remote":
        return str(val).strip().lower() in ("1", "true", "t", "yes")
    if key in ("salary_min", "salary_max", "exp_max_years"):
        try:
            return int(val)
        except (TypeError, ValueError):
            return None
    return str(val)


def _persist_derived(row_loc, row_jd, current_rows=None, jdmeta=None, idf=None):
    """Derive each job's state/metro/remote flag, pay range and JD signals, and write them to
    the jobs table. Only rows whose values actually CHANGED are sent, so the daily run costs
    one small upsert instead of re-writing the whole corpus.

    `current_rows` is the caller's already-loaded corpus, used purely to diff against. Pass
    it: this used to re-fetch every row INCLUDING the `jd` column, which is ~20 MB over the
    wire and made the scoring pass load the whole corpus twice for no benefit. The columns
    being compared here are not written by anything earlier in the run, so rows read at the
    start are still current at this point. Whatever set is diffed MUST also be in the caller's
    `cols=` select (db.COLS_SCORE) — a column missing from `have` reads as None forever and
    marks every row changed on every run.

    `jdmeta` is the {url: core.job_meta} map the scoring loop already built. Reusing it saves
    re-running the sponsorship scan over ~16k descriptions, and — more importantly — means the
    exp/sponsor COLUMNS and jdmeta.json come from one core.job_meta call and are structurally
    incapable of disagreeing.

    Never raises: the columns don't exist until someone runs db.JOBS_DERIVED_SQL once, and a
    missing column must not throw away a completed scoring pass.
    """
    try:
        if current_rows is None:
            current_rows = db.load_jobs()
        current = {r["url"]: r for r in current_rows if r.get("url")}
    except Exception as e:
        print("  (derived fields skipped, could not reload jobs: %s)" % str(e)[:90])
        return

    payload, jd_payload = [], []
    stats = {"state": 0, "remote": 0, "salary": 0, "exp": 0, "spon": 0, "terms": 0}
    for u, loc in row_loc.items():
        p = core.parse_location(loc, row_jd.get(u) or "")
        s = core.parse_salary(row_jd.get(u) or "")
        want = {
            "loc_state": p["state"], "loc_metro": p["metro"], "remote": bool(p["remote"]),
            "salary_min": s["min"], "salary_max": s["max"], "salary_period": s["period"],
        }
        if p["state"]:
            stats["state"] += 1
        if p["remote"]:
            stats["remote"] += 1
        if s["period"]:
            stats["salary"] += 1

        have = current.get(u) or {}
        if any(_norm_cmp(k, v) != _norm_cmp(k, have.get(k)) for k, v in want.items()):
            payload.append(dict(want, url=u))

        # JD-derived, and ONLY for rows we actually hold the description for. A row whose JD
        # we couldn't read this run must keep whatever an earlier run derived — writing an
        # empty verdict over a real one is worse than writing nothing.
        m = (jdmeta or {}).get(u)
        if m is None:
            jd = row_jd.get(u) or ""
            if not jd:
                continue
            # The SAME idf the scoring loop used. Analyzing with idf=None would silently give
            # every term weight 1.0, so this row's score would not be comparable with any other.
            m = core.job_meta(jd, idf)
        exp_y, (sv, sreason) = m.get("exp_years"), (m.get("sponsor_jd") or ("", ""))
        # The keyword weights the FEED scores every résumé against — see core.pack_analyzed.
        terms = core.pack_analyzed(m.get("analyzed") or {})
        want_jd = {"exp_max_years": exp_y, "sponsor_jd": sv, "sponsor_reason": sreason,
                   "jd_terms": terms or None}
        if exp_y is not None:
            stats["exp"] += 1
        if sv:
            stats["spon"] += 1
        if terms:
            stats["terms"] += 1
        # NOTE on the diff: jd_terms is deliberately NOT in db.COLS_SCORE, so in new-only mode
        # `have` never carries it and every row here writes. That is correct rather than
        # wasteful — new-only narrows row_loc to `todo` (a few hundred rows it just analyzed),
        # and putting an 11 MB column into that read to save writing them would cost far more
        # than it saves. The daily FULL pass reads select=* and diffs it properly, so the
        # steady state is still ~0 writes.
        if any(_norm_cmp(k, v) != _norm_cmp(k, have.get(k)) for k, v in want_jd.items()):
            jd_payload.append(dict(want_jd, url=u))

    # TWO writes, not one combined payload. db._upsert normalizes each chunk to the UNION of
    # its rows' keys and fills the gaps with None, so a row that skipped the JD block above
    # would be sent with an explicit exp_max_years: null and ERASE a value an earlier run
    # derived. Separate calls mean separate key unions.
    _send_derived(payload, "Derived fields",
                  "%d state, %d remote, %d with pay"
                  % (stats["state"], stats["remote"], stats["salary"]))
    _send_derived(jd_payload, "JD fields",
                  "%d with an experience floor, %d with a sponsorship verdict, %d scoreable"
                  % (stats["exp"], stats["spon"], stats["terms"]))


def _send_derived(payload, label, summary):
    """One diffed payload -> the jobs table, or a self-serve migration hint if the columns
    aren't there yet. Split out so the location/pay and JD groups can be written separately."""
    if not payload:
        print("%s already current (%s)." % (label, summary))
        return
    try:
        db.update_job_fields(payload)
        print("%s: updated %d job(s) — %s." % (label, len(payload), summary))
    except Exception as e:
        print("  (%s write failed: %s)" % (label.lower(), str(e)[:160]))
        print("  If that mentions an unknown column, run this once in Supabase -> SQL Editor:\n")
        print(db.JOBS_DERIVED_SQL)


# Breadcrumb the scraper writes with THIS run's new postings (same file notify.py reads).
NEW_JOBS_FILE = "last_new_jobs.json"


JD_CACHE_FILE = "jd_cache.json.gz"       # {url: jd}; restored by CI from actions/cache


def _load_jd_cache():
    try:
        with gzip.open(JD_CACHE_FILE, "rt", encoding="utf-8") as fh:
            return {u: v for u, v in (json.load(fh) or {}).items() if u and v}
    except Exception:
        return {}                        # absent, corrupt, or half-written -> just re-read


def _save_jd_cache(cache):
    try:
        tmp = JD_CACHE_FILE + ".tmp"
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump(cache, fh)
        os.replace(tmp, JD_CACHE_FILE)
    except Exception as e:
        print("  (jd cache not saved: %s)" % str(e)[:80])


def _jd_corpus(all_urls, full):
    """(row_jd, missing, db_missing) for a pass that needs EVERY stored description.

    build_idf runs over the whole corpus and every row gets re-analyzed, so this pass genuinely
    needs all ~16k JDs — but it does NOT need to re-download them. A stored JD never changes:
    the scheduled pass only fetches rows that have none (`missing` below), and _persist_jds only
    ever writes newly-fetched text. So the corpus is cached on disk and CI restores it from
    actions/cache; a normal run then pulls only the JDs added since the last one.

    This is the single biggest item on the egress bill — the full read measured ~128 MB at
    19,268 rows, daily. A cold or evicted cache falls back to exactly that read, so the worst
    case is today's cost and every subsequent run is near-free.

    `missing` and `db_missing` are DIFFERENT SETS and both are returned because they answer
    different questions — see the reconciliation in main(). `missing` is "no description on
    disk", which is what governs fetching; `db_missing` is "no description in the database",
    which is what the website actually renders.
    """
    cache = _load_jd_cache()
    before = len(cache)
    cache = {u: jd for u, jd in cache.items() if u in all_urls}   # forget pruned rows
    dropped = before - len(cache)
    db_missing = db.urls_missing_jd() & all_urls
    have_jd = all_urls - db_missing
    need = have_jd - set(cache)
    print("JD corpus: %d cached, %d dropped as pruned, %d to pull."
          % (len(cache), dropped, len(need)))
    if len(need) > 5000:
        # Cold cache. One paged walk beats ~%d by-url round-trips, so pay for the full read
        # once and let the next run reuse it.
        print("  cold cache — one full corpus read (~128 MB); later runs reuse the cache.")
        cache.update({r["url"]: (r.get("jd") or "")
                      for r in (db.load_jobs() or []) if r.get("url")})
    elif need:
        cache.update({r["url"]: (r.get("jd") or "")
                      for r in db.load_jobs_by_urls(sorted(need)) if r.get("url")})
    cache = {u: jd for u, jd in cache.items() if jd}
    # `missing` keeps its original meaning: rows with no stored description (or every row on
    # --full, which deliberately refetches the lot from the boards).
    return dict(cache), (set(all_urls) if full else all_urls - set(cache)), db_missing


def _new_only_targets(known_urls, fetched):
    """URLs worth (re)scoring when we're not doing the whole corpus.

    A stored job's score can only move for three reasons: its JD changed, the résumé
    changed, or IDF drifted as the corpus turned over. The first is exactly what this
    catches — the postings this run added, plus any job whose JD only just arrived. The
    other two are corpus-wide and belong to the full pass, which still runs daily.

    `known_urls` is every url we hold a row for. It used to be the JD map, which is no longer
    loaded in new-only mode — the set of urls is all this ever needed from it.
    """
    targets = set(fetched)                        # JDs that landed this run
    try:                                          # ...plus the postings this run added
        with open(NEW_JOBS_FILE, encoding="utf-8") as fh:
            targets.update(j.get("url") for j in (json.load(fh) or []) if j.get("url"))
    except Exception:
        pass                                      # no breadcrumb (manual run) -> just the JDs
    return targets & set(known_urls)              # never score a URL we hold no row for


def main():
    full = "--full" in sys.argv
    # Score only what this run actually pulled, instead of re-deriving all ~20k rows. The
    # full pass is not wasted work — it re-scores against the CURRENT résumé and a freshly
    # built IDF — but it is a per-job regex/keyword analysis plus a whole-corpus upsert, and
    # paying that on the second scrape of the day buys almost nothing. So: full pass once a
    # day, new-only on the other run.
    new_only = ("--new-only" in sys.argv
                or (os.environ.get("SCORE_NEW_ONLY") or "").strip().lower()
                in ("1", "true", "yes"))
    if full and new_only:                         # --full is the explicit "redo everything"
        new_only = False                          # so it wins over the cheap mode
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
    #
    # The `jd` column is pulled ONLY when this pass will use all of it — that is, a full
    # re-score, which builds IDF over every JD and re-analyzes every row. In new-only mode
    # neither holds, and reading it anyway cost a whole-corpus fetch WITH descriptions
    # (~52 MB gzipped / ~129 MB raw at 20,350 rows) on all three scrapes a day to use a few
    # hundred rows of it. So: cheap columns always, JD text on demand further down.
    # The `jd` column is never read wholesale any more. Both modes take the cheap columns; the
    # descriptions come from the on-disk corpus cache (heavy pass) or by url (new-only). At
    # 19,268 rows this select is ~5.1 MB against the ~128 MB the jd column used to cost, and it
    # ran on all three scrapes a day.
    rows = db.load_jobs(cols=db.COLS_SCORE)
    row_date = {r["url"]: (r.get("found_date") or "") for r in rows if r.get("url")}
    row_loc = {r["url"]: (r.get("location") or "") for r in rows if r.get("url")}
    # When the row entered OUR database, which is not found_date — that one is the
    # employer's posting date and can be weeks old on a job we first saw an hour ago.
    row_seen = {r["url"]: (r.get("first_seen") or "") for r in rows if r.get("url")}
    all_urls = {r["url"] for r in rows if r.get("url")}
    if new_only:
        # "Which rows still need a description?" was the only thing the JD text was needed for
        # here. urls_missing_jd() answers it directly with a urls-only select over just the
        # backlog — 2,940 rows rather than the 16,328 that already have one.
        row_jd = {}
        db_missing = db.urls_missing_jd() & all_urls
        missing = set(db_missing)
    else:
        row_jd, missing, db_missing = _jd_corpus(all_urls, full)
    stored = len(all_urls) - len(missing)

    # Per-run fetch cap: a scheduled CI run does BOUNDED work so it always finishes inside
    # the Actions timeout; the rest of the backlog drains on the next runs. 0 / unset = no
    # cap (a manual full backfill). Set via env SCORE_MAX_FETCH or the --max=N flag.
    cap = 0
    try:
        cap = int(os.environ.get("SCORE_MAX_FETCH") or 0)
    except Exception:
        cap = 0
    # ...and a per-run TIME budget, because the cap alone does not bound the clock. A capped
    # count says nothing about how long those fetches take, and the answer turned out to be
    # "much longer than it looks": a measured pass attempted 2,640 detail fetches and only 8
    # returned a usable JD — the rest were dead or blocked URLs that each still cost a
    # timeout. So the cap governs how much of the backlog we bite off, and this governs how
    # long we are willing to chew. 0 = unlimited (manual backfill).
    budget_min = 0.0
    try:
        budget_min = float(os.environ.get("SCORE_BUDGET_MIN") or 0)
    except ValueError:
        budget_min = 0.0
    for _a in sys.argv:
        if _a.startswith("--budget-min="):
            try:
                budget_min = float(_a.split("=", 1)[1])
            except ValueError:
                pass
    deadline = (time.time() + budget_min * 60) if budget_min > 0 else 0
    for _a in sys.argv:
        if _a.startswith("--max="):
            try:
                cap = int(_a.split("=", 1)[1])
            except Exception:
                pass
    # NEWEST FIRST, and this ordering is the whole point rather than a nicety.
    #
    # Both the cap below and the time budget above cut the TAIL off this list, and the list
    # used to be sorted by URL — so what survived was whatever happened to sort early, and
    # the fetch budget was spent alphabetically from abbott.wd5.myworkdayjobs.com onward.
    # With ~3.6k missing against a 3,000 cap that left ~646 URLs with no attempt at all,
    # chosen by hostname, while jobs scraped an hour earlier waited behind a backlog of old
    # ones. A failed fetch also records nothing, so a URL we can never read (closed posting,
    # bot-walled host) re-enters this queue every single run — one measured pass attempted
    # 2,640 and got 8 usable JDs back. Ordering by recency is what stops that backlog
    # starving today's postings: the dead weight sinks to the tail, where the budget cuts it.
    #
    # There is deliberately no "give up on this URL" flag, which would need a new column and
    # a manual migration. It isn't needed: the 30-day prune evicts these rows anyway, so the
    # graveyard is self-limiting, and the time budget bounds what it can cost meanwhile.
    order = sorted(missing, key=lambda u: (row_seen.get(u) or "", row_date.get(u) or ""),
                   reverse=True)
    if cap and len(order) > cap:
        order = order[:cap]
    missing = set(order)
    print("%d jobs: %d JDs stored, %d to fetch this run%s%s."
          % (len(all_urls), stored, len(missing), " (--full refetch)" if full else "",
             " (capped)" if cap else ""))

    fetched, dates = {}, {}
    # Persist freshly-fetched JDs in CHUNKS as we go, so an interruption/timeout never
    # discards work: whatever we've uploaded counts as 'stored', so the next run resumes
    # from a smaller backlog (guaranteed forward progress). Previously every JD was pushed
    # only at the very end, so a run that ran out of time persisted NOTHING and the backlog
    # could never shrink — which is how ~6.5k jobs ended up with no description.
    _flushed = set()

    def _persist_jds(buf):
        chunk = {u: jd for u, jd in buf.items() if jd and u not in _flushed}
        if not chunk:
            return
        try:
            db.update_jds(chunk)
            _flushed.update(chunk)
        except Exception as e:
            print("  (persist %d JDs failed: %s)" % (len(chunk), str(e)[:80]))

    # ---- Reconcile the two stores BEFORE spending any network. -------------------------
    #
    # There are two records of "we have this job's description": jd_cache.json.gz on disk, and
    # the `jd` column the website renders from. They are written at different moments — the
    # cache once at the end of the run, the column in chunks during it — so they can disagree,
    # and NOTHING used to notice when they did. A row whose JD was fetched, banked to disk, and
    # then not written to the database (a dropped upsert, a run killed between the last flush
    # and the cache save) was excluded from the fetch queue by the cache and from the site by
    # the empty column, with no path back: _persist_jds only ever writes text fetched THIS run.
    # It showed on the site as "description pending" forever.
    #
    # Measured 2026-08-16: 1,827 of the 3,816 rows reading as pending — 48% — had a full
    # description sitting in the local cache, including all 79 lululemon postings (7.2k chars
    # each) and 1,186 of Amazon's.
    #
    # New-only mode had the mirror of the same bug. There `missing` IS database-truth, so those
    # rows were re-DOWNLOADED every single run: the fetch budget was being spent re-reading
    # text already on disk, which is why the backlog grew while every run reported progress.
    #
    # One reconciliation fixes both directions, costs no network, and is idempotent.
    if new_only and db_missing:
        # New-only mode hasn't loaded the cache (it deliberately avoids the ~23 MB read), so
        # read it here — but only when there is a backlog it could satisfy, and keep just the
        # overlap rather than holding the whole corpus in memory for the rest of the run.
        bank = _load_jd_cache()
        repair = {u: bank[u] for u in (db_missing & set(bank)) if bank.get(u)}
        del bank
    else:
        repair = {u: row_jd[u] for u in (db_missing & set(row_jd)) if row_jd.get(u)}
    if repair:
        print("Re-persisting %d cached JD(s) the database was missing (no fetch needed)..."
              % len(repair))
        items = list(repair.items())
        for i in range(0, len(items), 300):
            _persist_jds(dict(items[i:i + 300]))
        row_jd.update(repair)
        # NOT on --full, whose entire contract is to re-read every description from its board.
        # Dropping the repaired rows from `missing` there would quietly turn a full refetch into
        # a partial one, and the cached text --full exists to replace would survive.
        if not full:
            missing -= set(_flushed)

    if missing:
        # 2) Bulk-fetch boards whose list API already includes the JD — one request
        #    covers the whole board, so try these first. Boards run concurrently.
        boards = scraper.SOURCES + scraper.custom_sources()
        bulk = [(b, a, c) for b, a, c in boards
                if a in ("greenhouse", "lever", "ashby", "amazon",
                         "jibe", "pinpoint", "jobdiva")
                and _board_has_missing(b, a, missing)]
        if bulk:
            print("Bulk-fetching JDs from %d board(s)..." % len(bulk))

            def _one(entry):
                board_url, ats, company = entry
                if deadline and time.time() >= deadline:
                    return company, {}, None      # out of time: skip, retry next run
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
            _persist_jds(fetched)           # save bulk hits before the slower detail phase
            missing -= set(fetched)

        # 3) The rest need a per-job detail fetch (SmartRecruiters/Workday/page scrape) —
        #    parallel, since each is an independent host round-trip. Flush every CHUNK so a
        #    timeout mid-phase still banks the JDs fetched so far.
        if missing:
            print("Detail-fetching %d remaining JD(s)%s..."
                  % (len(missing),
                     "" if not deadline else " (%g min budget)" % budget_min))
            CHUNK, buf, unspent = 150, {}, 0

            def _detail(u):
                # Budget checked before the request, so once time is up the remaining queue
                # drains without touching the network. These URLs stay in `missing` and are
                # simply retried next run — nothing is recorded either way.
                if deadline and time.time() >= deadline:
                    return u, "", ""
                return detail_jd(u)

            # Newest first here too. The cap decides WHICH urls are in play; this decides the
            # order they are attempted in, and the time budget can stop the run part-way
            # through — so alphabetical order would hand the same starvation back at a
            # different stage. `order` is already recency-sorted; re-filter rather than
            # re-sort so the bulk phase's hits drop out.
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
                for u, jd, date in ex.map(_detail, [u for u in order if u in missing]):
                    if jd:                  # a failed fetch must never blank a stored JD
                        fetched[u] = jd
                        buf[u] = jd
                        if len(buf) >= CHUNK:
                            _persist_jds(buf)
                            buf = {}
                    elif deadline and time.time() >= deadline:
                        unspent += 1
                    if date and _date_beats_stored(date, row_date.get(u)):
                        dates[u] = date     # the detail page dated it better than the list did
            _persist_jds(buf)
            if unspent:
                print("  JD budget reached — ~%d left for next run (they stay queued)." % unspent)
        row_jd.update(fetched)

    # Bank the descriptions on disk so the next heavy pass doesn't re-download them. New-only
    # mode has to MERGE — its row_jd holds only the rows it scored, so writing that back would
    # throw the corpus away — while the heavy pass already holds the whole thing.
    if new_only:
        if fetched:
            bank = _load_jd_cache()
            bank.update({u: jd for u, jd in fetched.items() if jd})
            _save_jd_cache(bank)
    else:
        _save_jd_cache({u: jd for u, jd in row_jd.items() if jd})

    # 4) IDF over the whole JD corpus (so common terms count less), then score.
    #    The FULL pass rebuilds it from every JD — it is a property of the corpus, and
    #    weighting a new job against a partial one would score it differently than the same
    #    job scored yesterday. New-only mode therefore REUSES the last full pass's idf.json
    #    rather than rebuilding: hours-stale is still corpus-wide, a subset is not. Same
    #    reasoning as the jdmeta merge below, and it is what lets new-only skip the jd column.
    idf = core.load_idf() if new_only else {}
    if not idf:
        if new_only:
            # Cold start: no idf.json to reuse (fresh checkout, or it was never written).
            # Building from this run's handful of JDs would mis-weight every score, so pay
            # for the one full read and let the next run reuse what we save here.
            print("No idf.json to reuse — reading the full JD corpus once to build it.")
            row_jd = {r["url"]: (r.get("jd") or "")
                      for r in (db.load_jobs() or []) if r.get("url")}
        idf = core.build_idf([j for j in row_jd.values() if j])
        core.save_idf(idf)
    # Which jobs get re-analyzed. The full pass does all of them (and so also picks up résumé
    # edits since last run); new-only does just what this run pulled.
    todo = _new_only_targets(all_urls, fetched) if new_only else set(row_jd)
    # New-only mode never read the jd column, so pull the text for just these rows. JDs this
    # run fetched itself are already in row_jd from the phase above; the on-disk corpus covers
    # most of the rest, so only genuinely-unseen rows cost a request.
    if new_only:
        need = [u for u in todo if u not in row_jd]
        if need:
            bank = _load_jd_cache()
            row_jd.update({u: bank[u] for u in need if u in bank})
            need = [u for u in need if u not in row_jd]
        if need:
            row_jd.update({r["url"]: (r.get("jd") or "")
                           for r in db.load_jobs_by_urls(need) if r.get("url")})
    print("Scoring %d of %d jobs with IDF weighting (%d terms in corpus)%s..."
          % (len(todo), len(all_urls), len(idf), " [NEW ONLY]" if new_only else ""))
    # Compute each job's résumé-INDEPENDENT analysis ONCE, reuse it for the score, AND persist
    # it to jdmeta.json so the web app never recomputes it at request time (kills cold-load
    # regex/keyword work). score_against(resume, analyzed) == the old skill_match(resume, jd).
    resume_low = resume.lower()
    # In new-only mode start from what's already on disk and MERGE, because this map is the
    # web app's precomputed cache for the whole corpus — writing back only the handful of
    # jobs we just scored would blank the other ~20k and push that work back to request time.
    jdmeta = (core.load_jdmeta() or {}) if new_only else {}
    scores = {}
    for u in todo:
        jd = row_jd.get(u)
        # An absent KEY means the by-url lookup above failed or the row vanished mid-run —
        # skip it and leave the stored score alone. An empty STRING is different and must
        # still be scored: that is a real row with no JD yet, and it scores 0 by design.
        if jd is None:
            continue
        m = core.job_meta(jd, idf)
        jdmeta[u] = m
        # A too-thin/truncated JD can't be scored honestly (it's what produced the fake ~100%s):
        # store 0 so it sorts/filters low and the feed shows it as "JD pending" (the web layer
        # keys off the same `thin` flag) instead of a misleading number.
        scores[u] = 0 if m["analyzed"].get("thin") else core.score_against(resume_low, m["analyzed"])[0]
    core.save_jdmeta(jdmeta)

    # 5) Persist all scores. JDs were already uploaded incrementally in the fetch phase
    #    (guaranteeing forward progress on a timeout); this just banks any residual.
    db.update_scores(scores)
    _persist_jds(fetched)
    if dates:                       # fill in real posting dates the list view omitted (e.g. SAP)
        db.update_job_fields([{"url": u, "found_date": d} for u, d in dates.items()])

    # 6) Derived fields the FEED filters on — location, pay, AND the JD signals (experience
    #    floor, sponsorship verdict). These live in real columns rather than jdmeta.json
    #    because jdmeta.json is gitignored and never deployed: it is built HERE, on an
    #    ephemeral GitHub Actions runner whose filesystem is discarded when the run ends, so
    #    there is no host holding a fresh copy to ship. A column reaches the live site through
    #    Supabase with no file deploy, the same way match_score already does. Until the JD
    #    group moved into columns, the live feed's experience and no-sponsorship filters were
    #    silent no-ops — every row read as "states nothing", which both filters keep.
    #    Narrowed to the same set in new-only mode: these are parsed from a row's own location
    #    and JD, so a row nobody touched this run can only re-derive to what it already holds.
    _persist_derived({u: row_loc[u] for u in todo if u in row_loc} if new_only else row_loc,
                     row_jd, current_rows=rows, jdmeta=jdmeta, idf=idf)
    if scores:
        vals = list(scores.values())
        where = db.backend_name()
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
