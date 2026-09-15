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
    """HTML (or already-plain) -> clean text. Kept as a local name because ~40 call sites in
    this file use it; the implementation moved to core.html_to_text so the sweep shares it."""
    return core.html_to_text(raw)


# Phenom's jobDetail widget is ONE POST PER JOB, so it is capped per run and backfills across
# runs rather than spending a whole budget at once. Actalent alone is 1,461 rows.
PHENOM_JD_DETAILS_MAX = int(os.environ.get("PHENOM_JD_DETAILS_MAX") or 400)
PHENOM_JD_WORKERS = 6


def phenom_jd_by_id(origin, jid):
    """The full description for one Phenom posting, from its jobDetail widget. "" on any failure.

    PUBLIC and standalone because there are now two callers and they are in different modules:
    _phenom_jd_map below (which has to pair applyUrl -> jobId off the search feed first) and the
    SWEEP, which already holds the jobId from the listing response and needs no pairing at all.
    One definition, because the two differ only in where the id came from.

    Ask by id rather than fetching the Phenom job PAGE. Both work -- careers.<tenant>.com
    /us/en/job/<id> renders the same text -- but the page is ~708 KB against a few KB of JSON
    here, and at 400 rows a run that difference is ~280 MB of transfer on a box that is already
    being SIGKILLed at a 1.2 GB memory cap.
    """
    try:
        body = scraper._phenom_body(0, 1)
        body.update({"ddoKey": "jobDetail", "jobId": jid,
                     "pageName": "job-details", "pageId": "page-job-details"})
        r = scraper._safe_post(origin.rstrip("/") + "/widgets", body, timeout=20)
        if r.status_code != 200:
            return ""
        job = (((r.json() or {}).get("jobDetail") or {}).get("data") or {}).get("job") or {}
        return _text(job.get("description") or "")
    except Exception:
        return ""


def _phenom_jd_map(board_url, needed):
    """{applyUrl: full description} for a Phenom board.

    Phenom is the one ATS where the LIST feed is not enough and the stored URL is not the
    Phenom page. Each job's `applyUrl` is where the candidate actually applies, and that is
    what the scraper stores — for Actalent it is a Salesforce Lightning app, which is why
    1,461 rows held 46 characters of "Loading ... Sorry to interrupt CSS Error Refresh"
    instead of a description. The list feed only carries a ~390-char teaser; the real text is
    behind the jobDetail widget, one POST per job.

    So: page the cheap search feed to pair applyUrl -> jobId, then fetch details ONLY for the
    URLs this run is actually missing, capped so one big board cannot eat the whole budget.
    """
    origin = board_url.rstrip("/")
    pairs = {}
    for off in range(0, scraper.PHENOM_MAX_JOBS, 50):
        try:
            r = scraper._safe_post(origin + "/widgets", scraper._phenom_body(off, 50), timeout=20)
            if r.status_code != 200:
                break
            jobs = (((r.json() or {}).get("refineSearch") or {}).get("data") or {}).get("jobs") or []
        except Exception:
            break
        if not jobs:
            break
        for j in jobs:
            u, jid = (j.get("applyUrl") or "").strip(), j.get("jobId")
            if u and jid:
                # Key on the CANONICAL url. The scraper canonicalises before storing, and for
                # Actalent the feed and the stored row differ by exactly one character —
                # ".../v1/s/?opco=" from the feed against ".../v1/s?opco=" in the table. A raw
                # key matches nothing at all, which is how this returned 0 JDs on the first try.
                pairs[scraper.canonical_url(u)] = jid
        if len(jobs) < 50:
            break
        # Stop paging once we already have a full run's worth of jobs we actually need. A
        # board like Actalent advertises 5,219 postings; without this the cheap feed alone
        # costs 104 requests every run just to rediscover pairs we will not use.
        if needed is not None and \
                sum(1 for u in pairs if u in needed) >= PHENOM_JD_DETAILS_MAX:
            break

    want = [(u, jid) for u, jid in pairs.items() if needed is None or u in needed]
    want = want[:PHENOM_JD_DETAILS_MAX]
    if not want:
        return {}

    def _detail(item):
        u, jid = item
        return u, phenom_jd_by_id(origin, jid)

    out = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=PHENOM_JD_WORKERS) as ex:
        for u, jd in ex.map(_detail, want):
            if jd:
                out[u] = jd
    return out


# JobDiva's list feed truncates every description at 400 chars + "...", so the full text is
# one request per job like Phenom's. Same cap-and-backfill shape, same reason.
JOBDIVA_JD_DETAILS_MAX = int(os.environ.get("JOBDIVA_JD_DETAILS_MAX") or 400)
JOBDIVA_JD_WORKERS = 6


_JOBDIVA_SESSIONS = {}       # portal token -> auth headers, one handshake per process


def _jobdiva_job_id(url):
    """The posting id out of a stored JobDiva URL. Its portal is hash-routed, so the id lives
    in the FRAGMENT — .../portal?a=<token>#/jobs/<id> — which is also why canonical_url keeps
    fragments on this host."""
    m = re.search(r"#/jobs/(\d+)", url or "")
    return m.group(1) if m else ""


def _jobdiva_jd_map(board_url, needed=None):
    """{job_url: full description} for one JobDiva portal.

    Was a single paged pass over the feed, reading its inline jobDescription. That field is
    TRUNCATED at 400 characters (measured: 196 of 200 rows exactly 403 chars, cut mid-word),
    and 403 is three characters above core._MIN_JD_CHARS — so the teaser would have read as a
    complete description and nothing would ever have retried it. The feed is now used only to
    enumerate ids; the text comes from scraper.jobdiva_job_detail, one request per job, capped
    per run and backfilling across runs exactly as Phenom does.

    Falls back to the teaser when the detail call fails but the feed had something, because
    400 characters of the real posting still beats an empty column.
    """
    token = scraper._jobdiva_token(board_url)
    jh = scraper._jobdiva_session(token) if token else None
    if not jh:
        return {}
    teaser, want = {}, []
    for data in scraper._jobdiva_pages(token, jh):
        for j in data:
            jid = j.get("id")
            if jid is None:
                continue
            u = scraper.canonical_url(
                "https://www1.jobdiva.com/portal/?a=%s#/jobs/%s" % (token, jid))
            if needed is not None and u not in needed:
                continue
            teaser[u] = _text(j.get("jobDescription") or "")
            want.append((u, jid))
            if len(want) >= JOBDIVA_JD_DETAILS_MAX:
                break
        if len(want) >= JOBDIVA_JD_DETAILS_MAX:
            break

    def _one(item):
        u, jid = item
        return u, _text(scraper.jobdiva_job_detail(jid, jh))

    out = {}
    if want:
        with concurrent.futures.ThreadPoolExecutor(max_workers=JOBDIVA_JD_WORKERS) as ex:
            for u, jd in ex.map(_one, want):
                out[u] = jd or teaser.get(u, "")
    return {u: jd for u, jd in out.items() if jd}


def jobdiva_detail_jd(url):
    """One stored JobDiva row, repaired on its own — the per-job twin of _jobdiva_jd_map.

    Needed as well as the bulk path because a single row can go thin (or arrive) without its
    whole board being swept, and because refetch_thin_jds drives detail_jd, not jd_map_for.
    The auth handshake is per portal token, so it is cached for the life of the process.
    """
    jid = _jobdiva_job_id(url)
    token = scraper._jobdiva_token(url)
    if not (jid and token):
        return ""
    jh = _JOBDIVA_SESSIONS.get(token)
    if jh is None:
        jh = _JOBDIVA_SESSIONS[token] = scraper._jobdiva_session(token) or {}
    return _text(scraper.jobdiva_job_detail(jid, jh)) if jh else ""


_IBM_JOB_RE = re.compile(r"[?&]jobId=(\d+)", re.I)


def _ibm_jd_map(needed=None):
    """{job_url: jd} for the IBM rows a run is still missing — the bulk half of the IBM fix.

    KEYED BY THE URL WE WERE GIVEN, not by one rebuilt from the feed's own `url` field, because
    the requisition id is parsed OUT of that url: there is no second spelling for the stored one
    to disagree with, which is the whole class of bug _canonical_keys exists to catch.

    Derived entirely from `needed`, so an unset one yields {} rather than paging the board. Every
    caller passes it (jd_map_for hands over the run's missing set), and a full IBM backfill is
    `--full`'s job, not this function's.
    """
    want = {}
    for u in (needed or ()):
        m = _IBM_JOB_RE.search(u or "")
        if m and "careers.ibm.com" in u:
            want.setdefault(m.group(1), u)
    if not want:
        return {}
    return {want[jid]: jd
            for jid, jd in (scraper.ibm_job_bodies(sorted(want)) or {}).items()
            if jid in want and jd}


def ibm_detail_jd(url):
    """One IBM row on its own — the per-job twin of _ibm_jd_map, for the paths that drive
    detail_jd (refetch_thin_jds, the thin-host probe) rather than the bulk map."""
    m = _IBM_JOB_RE.search(url or "")
    if not m:
        return ""
    return (scraper.ibm_job_bodies([m.group(1)]) or {}).get(m.group(1), "")


# Shortest plausible description from a RAW PAGE FETCH. Applies only to that last-resort path
# in detail_jd — structured feeds and per-job APIs are trusted at any length.
MIN_PAGE_JD_CHARS = 250


def _canonical_keys(jd_map):
    """Re-key a bulk JD map by canonical_url — the ONE place that reconciliation happens.

    Every branch of jd_map_for BUILDS the job URL it returns, from whatever shape that ATS's
    feed gives it. The `jobs` table stores scraper.canonical_url() of that URL. Nothing forced
    the two to agree, and two of the eight branches did not. Both were the SAME rule biting a
    URL that happens to carry a trailing slash before its query — canonical_url does
    path.rstrip("/"), and these two branches build a path ending in "/":

      * phenom   the feed's applyUrl ends ".../v1/s/?opco=" and the stored row is ".../v1/s?opco="
      * jobdiva  we emit "/portal/?a=<token>#/jobs/<id>" and the row is "/portal?a=<token>#..."

    The other six (greenhouse, lever, ashby, amazon, pinpoint, jibe) were verified canonical-
    stable — so this is not a fix for two branches, it is the removal of a way for the NEXT one
    to be wrong. `scripts/verify_parsers.py`-style checking cannot catch it, because both stores
    look internally consistent.

    A miss here is SILENT and expensive: the description is fetched, then dropped on the floor,
    and the row keeps whatever shell it had — 352 JobDiva rows sat at 63 characters of "You
    need to enable JavaScript to run this app." while every run downloaded their real text.

    So this is applied at the boundary rather than in each branch. Branch nine gets it free.
    """
    if not jd_map:
        return jd_map or {}
    out = {}
    for u, jd in jd_map.items():
        out[scraper.canonical_url(u)] = jd
        # Keep the raw key too when it differs. Costs a few dict slots and means a caller
        # holding a NON-canonical URL (refetch_thin_jds passes `want` straight from the DB,
        # but a board's own feed may be the source elsewhere) still matches.
        if u not in out:
            out[u] = jd
    return out


# The ATS families whose LIST api already carries the description, so ONE request covers a whole
# board and score_jobs owes those rows no per-job fetch. Every name here needs a branch in
# jd_map_for and a case in _board_has_missing; a name without one is a board that gets asked and
# silently answers nothing, which is indistinguishable from a board that has no backlog.
BULK_JD_ATS = ("greenhouse", "lever", "ashby", "amazon", "jibe", "pinpoint", "jobdiva",
               "phenom", "ibm")
# HOW MANY BOARD MAPS MAY BE IN MEMORY AT ONCE. Not a worker count -- workers stay at 8, which
# is a THROTTLING decision about outbound concurrency on shared hosting and is deliberately not
# traded for a memory one. This bounds how many COMPLETED maps can pile up waiting to be
# consumed, which is a different quantity and the one that overflows: jd_map_for returns a whole
# board with every description, so 151 of them buffered is gigabytes while 16 is bounded.
# 16 = twice the worker count, so a slow board never starves the pool.
_BULK_WINDOW = int(os.environ.get("SCORE_BULK_WINDOW") or 16)
# THE TWO ATS WHOSE ROWS CANNOT BE TESTED AGAINST A BOARD CHEAPLY. Their rows store an APPLY
# url whose host varies per tenant (icims.com, Oracle, Salesforce), so _board_has_missing
# answers True for them unconditionally rather than skip a board that might hold the rows we
# want -- which is right, and is why Actalent's 1,461 rows stopped sitting on a loading shell.
_BLIND_ATS = ("jibe", "phenom")
# ...and how many of them one run may fetch. 149 of the 151 boards a live run bulk-fetches are
# these, each returning its WHOLE board -- 533 and 735 descriptions in one sampled run -- to
# satisfy approximately none of the 547 rows wanted. 20 keeps the cost bounded and still walks
# all 149 in about eight runs, which is four days at two crons a weekday.
_BLIND_BOARDS_PER_RUN = int(os.environ.get("SCORE_BLIND_BOARDS") or 20)


def jd_map_for(board_url, ats, needed=None):
    """Return {job_url: jd_text} for one board, using the JD the API already returns.

    `needed` is the set of URLs this run is still missing. Most branches ignore it — their
    feed returns every description in one call anyway — but Phenom costs one request per job,
    so it uses it to avoid fetching thousands of descriptions we already hold.
    """
    slug = scraper._slug(board_url)
    out = {}
    if ats == "phenom":
        return _canonical_keys(_phenom_jd_map(board_url, needed))
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
        return _canonical_keys(_jobdiva_jd_map(board_url, needed))
    elif ats == "ibm":
        return _canonical_keys(_ibm_jd_map(needed))
    return _canonical_keys(out)


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
    if ats == "ibm":
        return any("careers.ibm.com" in u for u in missing_urls)
    if ats in ("jibe", "phenom"):
        # These rows store the APPLY url, whose host varies per tenant (icims.com, Oracle,
        # Salesforce, ...) — there's no cheap URL test. Actalent is the case that matters:
        # its board is careers.actalentservices.com but every row it produces is stored under
        # apply.actalentservices.com, so the slug/host test below answers False and the board
        # is skipped forever. That is why 1,461 rows sat on a "Loading ..." shell.
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
        # ([\w-]+) for the id, not (\d+). Oracle requisition ids are only USUALLY numeric —
        # Albertsons' are "W739723" — and a digits-only pattern silently returned "" for all 25
        # of their rows, which read as "this host needs an extractor" when the extractor was
        # fine. Verified: that tenant's detail response carries a 102,717-char
        # ExternalDescriptionStr under exactly the keys already read below.
        m = re.search(r"/sites/([\w-]+)/job/([\w-]+)", url)
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
        body = " ".join(_text(items[0].get(k) or "") for k in
                        ("ExternalDescriptionStr", "ShortDescriptionStr",
                         "ExternalQualificationsStr", "ExternalResponsibilitiesStr")).strip()
        # ...AND THE FIELD TABLE, which is where this ATS puts the two facts that decide whether
        # a posting is worth opening. Oracle's candidate page renders a labelled block above the
        # description -- Role, Job Type, Years, Additional Info -- and every one of those is a
        # `requisitionFlexFields` entry, not prose. Reading only the four description fields
        # meant a real Oracle posting showing "Years: 3 to 5+ years" on its own page reported
        # "Not stated in this posting" here.
        #
        # THE SPONSORSHIP ONE IS WHY THIS IS URGENT rather than tidy. "Additional Info: Visa /
        # work permit sponsorship is not available for this position" lives in the same block,
        # and core.sponsorship_from_jd reads it correctly the moment it can see it -- so without
        # this the card fell back to Oracle's EMPLOYER filing history and said "H-1B Likely" on
        # a posting that rules sponsorship out in writing. 2,495 active rows are on this host,
        # including JPMorgan Chase (582), Oracle (513) and American Express (170).
        #
        # Appended as "Prompt: Value" lines, which is what the page shows and what
        # jdrender._field_label already renders as a definition list.
        fields = []
        for f in (items[0].get("requisitionFlexFields") or []):
            label = _text(str(f.get("Prompt") or f.get("Name") or "")).strip()
            value = _text(str(f.get("Value") or "")).strip()
            if label and value and value.lower() not in ("null", "none"):
                fields.append("%s: %s" % (label[:60], value[:200]))
        return (body + ("\n" + "\n".join(fields) if fields else "")).strip()
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


_GH_JID_RE = re.compile(r"[?&]gh_jid=(\d+)")
# Greenhouse's own embed page names the board in its form action:
#   <form action="/embed/job_app?for=fivetran&amp;token=7810450003">
_GH_FOR_RE = re.compile(r"[?&]for=([A-Za-z0-9_-]+)")
# careerpuck and friends put the board straight in the path: /job-board/lyft/job/<id>
_GH_PATH_RE = re.compile(r"/job-board/([A-Za-z0-9_-]+)/")
_GH_BOARDS = {}          # stored-url host -> greenhouse board token, resolved once per process


def _gh_embed_html(jid):
    """Greenhouse's tokenless embed page for one job id, or "". Works for ANY job id without
    knowing the board, which is what makes it both the board-token oracle and the fallback."""
    try:
        r = scraper._safe_get(
            "https://boards.greenhouse.io/embed/job_app?token=%s" % jid, timeout=20)
        return r.text or "" if r.status_code == 200 else ""
    except Exception:
        return ""


def _gh_board_token(url, embed_html):
    """The Greenhouse board token for a company-hosted job page.

    Read off GREENHOUSE, not off the employer's site, and not looked up in SOURCES. The whole
    point of this extractor is the long tail of employers who embed Greenhouse on their own
    domain; many are not boards we scrape, and several (Fivetran, careerpuck) render their page
    client-side so the server HTML mentions Greenhouse nowhere at all. The embed page's form
    action names the board for every job id, so one fetch we are making anyway answers it.

    Cached per host: the board is a property of the employer, not of the posting.
    """
    from urllib.parse import urlparse
    host = urlparse(url).netloc.lower()
    if host in _GH_BOARDS:
        return _GH_BOARDS[host]
    token = ""
    m = _GH_PATH_RE.search(url or "")
    if m:
        token = m.group(1)
    if not token:
        for cand in _GH_FOR_RE.findall(embed_html or ""):
            if cand not in ("embed", "job_app"):
                token = cand
                break
    # Cache only a SUCCESS. A closed posting's embed page 404s, so it names no board — caching
    # that "" would poison every other row on the same host, which is how PathAI's 7 rows would
    # have stayed unreadable because one of them happened to be tried first.
    if token:
        _GH_BOARDS[host] = token
    return token


def amazon_rematch_jd(url, title, location):
    """A withdrawn amazon.jobs posting, matched to a LIVE requisition by title and location.

    The only recovery route left for a 404 class. amazon.jobs returns 404 for 27 stored rows —
    the req id is gone — but search.json still answers 200 with the full description, and Amazon
    reposts the same role under a new id constantly. So the description very often still exists;
    it is just not at the id we stored.

    STRICT ON PURPOSE, and this is the whole design. Matching loosely would attach one posting's
    description to a different row, which is worse than a blank: the score, the tailoring and
    the "why this matched" highlights would all be about another job. So the title must be
    EXACTLY equal after case/whitespace folding, the city must agree, and exactly ONE live req
    may satisfy both. Two candidates means we cannot tell them apart, and we return "".

    Returns "" unless all three hold — never a best guess.
    """
    if "amazon.jobs" not in (url or "") or not (title or "").strip():
        return ""
    want = " ".join((title or "").split()).lower()
    # City only: the stored location is "Austin, Texas, USA" and the feed's is "US, TX, Austin",
    # so the field orders never line up. The city is the part both spell the same way.
    city = ""
    for part in (location or "").replace("|", ",").split(","):
        part = part.strip()
        if part and not part.isupper() and len(part) > 2:
            city = part.lower()
            break
    try:
        d = scraper._get_json("https://www.amazon.jobs/en/search.json",
                              params={"base_query": title, "country": "USA",
                                      "result_limit": 20, "sort": "relevant"})
    except Exception:
        return ""
    hits = []
    for j in (d.get("jobs") or []):
        if " ".join((j.get("title") or "").split()).lower() != want:
            continue
        if city and city not in (j.get("normalized_location") or j.get("location") or "").lower():
            continue
        jd = " ".join(_text(x) for x in (j.get("description"), j.get("basic_qualifications"),
                                         j.get("preferred_qualifications")) if x).strip()
        if len(jd) >= core._MIN_JD_CHARS:
            hits.append(jd)
    return hits[0] if len(hits) == 1 else ""


def greenhouse_detail_jd(url):
    """Greenhouse behind an employer's own domain, identified by gh_jid in the query string.

    These pages render client-side, so microdata_jd finds no JobPosting and the page-text
    fallback returns the nav bar — which is how 198 rows (Fivetran, Lyft via careerpuck, Cribl,
    Orion, Aurora, PathAI, Revolution Medicines, Agility Robotics, MongoDB ...) came to hold
    32-212 characters of chrome, or nothing at all. The id needed to ask Greenhouse directly
    was sitting in the stored URL the whole time.

    Two routes, in order of trust:
      1. boards-api.greenhouse.io/v1/boards/<board>/jobs/<id> -> `content`, the description
         alone. Measured 5,014 / 7,296 / 13,322 / 24,200 chars on these rows.
      2. the embed page's own HTML. Its description sits INSIDE the <form>, so core.fetch_jd
         cannot be reused — it decomposes forms and returns ~150 chars of button labels. Text
         from here carries some application-form chrome, which is why it is second.

    Empty from both means the posting is gone (Agility's and Aurora's sampled ids 404), and ""
    is then the honest answer: the row stays retryable instead of scoring 0 on a nav bar.
    """
    m = _GH_JID_RE.search(url or "")
    if not m:
        return ""
    jid = m.group(1)
    from urllib.parse import urlparse
    # Only pay for the embed fetch when this host's board is not already known.
    embed = "" if urlparse(url or "").netloc.lower() in _GH_BOARDS else _gh_embed_html(jid)
    token = _gh_board_token(url, embed)
    if token:
        try:
            d = scraper._get_json(
                "https://boards-api.greenhouse.io/v1/boards/%s/jobs/%s" % (token, jid))
            jd = _text(d.get("content") or "")
            if jd:
                return jd
        except Exception:
            pass
    if not embed:
        embed = _gh_embed_html(jid)
    if not embed:
        return ""
    soup = BeautifulSoup(embed, "lxml")
    for t in soup(["script", "style", "nav", "header", "footer"]):
        t.decompose()
    return re.sub(r"\s{2,}", " ", soup.get_text(" ", strip=True))


# How deep a JobPosting may be nested before we stop looking. Two is enough for every shape
# seen (mainEntity, @graph, a bare list) and stops a pathological document walking forever.
_LD_MAX_DEPTH = 4


def _jobposting_nodes(data, depth=0):
    """Every JobPosting in a JSON-LD document, however it is wrapped.

    THE TOP LEVEL IS NOT WHERE IT ALWAYS IS. This used to test `data["@type"] == "JobPosting"`
    on the root only, and roberthalf.com wraps its posting in a WebPage whose `mainEntity` is
    the JobPosting -- so the extractor found nothing, fell through to core.fetch_jd, and stored
    7,921 characters of the site's own navigation as the description. `@graph` is the other
    common wrapper. Both are ordinary schema.org, not quirks.
    """
    if depth > _LD_MAX_DEPTH:
        return
    if isinstance(data, list):
        for x in data:
            for hit in _jobposting_nodes(x, depth + 1):
                yield hit
        return
    if not isinstance(data, dict):
        return
    t = data.get("@type")
    if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
        yield data
        return
    for key in ("mainEntity", "@graph", "mainEntityOfPage", "itemListElement"):
        if key in data:
            for hit in _jobposting_nodes(data[key], depth + 1):
                yield hit


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
            for it in _jobposting_nodes(data):
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
    if scraper.urlparse(url).hostname == "digitalcareers.infosys.com":
        from scraper.infosys import detail_jd as infosys_detail_jd
        jd = infosys_detail_jd(url)
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
    if not jd and "jobdiva.com" in url:
        jd = jobdiva_detail_jd(url)
    if not jd and "careers.ibm.com" in url:
        jd = ibm_detail_jd(url)
    if not jd and _GH_JID_RE.search(url):           # Greenhouse on the employer's own domain
        jd = greenhouse_detail_jd(url)
    if not jd:                                      # structured data beats page text
        jd, date = microdata_jd(url)
    if not jd:                                      # last resort: fetch the page
        jd = core.fetch_jd(url)
        # A raw page fetch is the ONLY step here that can SUCCEED AT READING THE WRONG THING.
        # Every branch above is a structured feed or a per-job API, so it either returns that
        # job's description or nothing. This one returns whatever the URL renders — and when
        # the posting has closed, or the stored url is a JS app or an embed that draws the
        # whole job LIST, that is a page title and a nav bar.
        #
        # Storing it is strictly worse than storing nothing: a non-empty `jd` drops the row
        # out of `missing`, so nothing ever retries it, it reads blank in the feed, and it
        # scores 0 forever. Every shell chased down on 2026-08-17 entered here — Suvoda's
        # "Job Openings | Suvoda Careers Current Positions" (47), Actalent's 1,461 "Loading
        # ... Sorry to interrupt CSS Error Refresh" (46), JobDiva's 352 "You need to enable
        # JavaScript to run this app." (63), Michael Page's 29 title-plus-nav (97-122).
        #
        # The longest shell observed is 153 chars and the shortest real JD is well over 1,000,
        # so this threshold has wide margin on both sides. Leaving the column empty keeps the
        # row honest: it reads as "description pending" and stays retryable.
        if len(jd or "") < MIN_PAGE_JD_CHARS:
            jd = ""
        # ...AND THE CEILING, which is what this gate never had. MIN_PAGE_JD_CHARS is a FLOOR,
        # and the whole comment above reasons about shells that are too SHORT — 47, 46, 63, 97
        # characters. The opposite failure was invisible: careers.google.com returned its
        # entire application shell, 1,052 rows of it, 790 truncated at fetch_jd's own 8,000
        # limit. Long, so never thin; never thin, so never retried; and _accept_jd's gain rule
        # then made it permanent, because a correct 3,400-char description cannot beat 3x8,000.
        # core.clean_jd names it for what it is, and nothing else has to change.
        elif core.clean_jd(jd)[1] == "not-a-posting":
            jd = ""
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


# The JD-derived column group, NAMED rather than inferred from the rows being written. Handed to
# every batch below, because db._upsert computes its key union per CALL and drops Nones on the
# way: a batch in which no row states an experience floor would not send exp_max_years at all,
# and a stale value the parser no longer agrees with would survive. Naming the group means every
# batch writes all four columns whatever it happens to hold.
JD_DERIVED_COLS = ("url", "exp_max_years", "sponsor_jd", "sponsor_reason", "jd_terms",
                   # WHICH TEXT the four columns before it were read from. Written in the
                   # same payload deliberately: a stamp that could land without its
                   # readings, or the reverse, would be worse than no stamp at all.
                   "facts_fp")

# Rows per JD-fields write. This used to be the whole corpus accumulated in memory and sent in
# ONE call after the loop, which is exactly where the 2026-09-03 pass died: `Derived fields:
# updated 1809 job(s)` printed, then silence and a nonzero exit with no traceback, at 1.24 GB RSS
# against a ~1.2 GB account-wide LVE cap. Read the missing traceback as the diagnosis --
# _send_derived's `except Exception` would have CAUGHT a proxy timeout or an unknown column and
# printed the migration hint, so a silent death past that guard is a SIGKILL.
#
# jd_terms is ~600 B a row over ~46k rows, and db._upsert copies the list it is handed (it merges
# duplicate urls first) before serialising it -- so the peak arrives on the one phase whose work
# nothing else banks. match_score, loc_state and every fetched description had already landed
# that day; an hour of analysis was re-run to recover a write that was 100% complete in memory.
# Writing as we go costs the same requests (_upsert already chunks the WIRE at 200) and caps what
# a kill can lose at one batch. 2000 is a bound rather than a tuned number -- the ~1.2 MB of
# terms it holds is nowhere near the cap. SCORE_JD_WRITE_CHUNK moves it without a deploy.
JD_WRITE_CHUNK = int(os.environ.get("SCORE_JD_WRITE_CHUNK") or 2000)


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
    # URLs whose ANALYSIS changed in this batch, which is not the same set as jd_payload: a row
    # is in that for any JD-derived column, and a moved exp_max_years does not change anybody's
    # match score. The stored per-user scores in db.user_scores ARE derived from jd_terms, so
    # exactly these rows are the ones whose stored number is now about an analysis that no
    # longer exists. Deleting rather than recomputing is deliberate -- this process has no
    # business loading every user's resume, and web.user_scores already treats a missing row as
    # "compute it", so the reader degrades to the behaviour it had before the table existed.
    jd_dirty = []
    stats = {"state": 0, "remote": 0, "salary": 0, "exp": 0, "spon": 0, "terms": 0}
    jd_write = {"sent": 0, "lost": 0, "hint": True, "stale": 0}

    def _flush_jd(force=False):
        """Bank the JD columns built so far, then forget them. A batch is what a kill costs.

        A failed batch does NOT stop the rest: db._upsert has already ridden out four attempts,
        and a proxy that answered one write with HTML has no bearing on the next. The migration
        hint is printed once, though -- twenty-four batches must not print the schema
        twenty-four times.
        """
        if not jd_payload or (len(jd_payload) < JD_WRITE_CHUNK and not force):
            return
        n = len(jd_payload)
        if _send_derived(jd_payload, "JD fields", keys=JD_DERIVED_COLS, hint=jd_write["hint"]):
            jd_write["sent"] += n
            # ONLY after the analysis itself landed. Dropping the scores first and then failing
            # to write the terms they were stale against would throw away good numbers and put
            # nothing in their place.
            if jd_dirty:
                try:
                    jd_write["stale"] += db.clear_scores_for_urls(jd_dirty)
                except Exception as e:
                    print("  (stored user scores not cleared: %s)" % str(e)[:80])
        else:
            jd_write["lost"] += n
            jd_write["hint"] = False
        del jd_payload[:]
        del jd_dirty[:]
        # Progress an operator can act on: these rows are IN the table now, so polling
        # `exp_max_years IS NULL` mid-run finally means something. It did not before -- the whole
        # corpus was analysed in memory and written in one call at the very end, which read as a
        # pass stalled at ~2% until the last second.
        if not force:
            print("  JD fields: %d job(s) banked." % jd_write["sent"], flush=True)

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
        # THE CACHED READING IS ONLY GOOD FOR THE TEXT IT WAS READ FROM, and in new-only
        # mode `jdmeta` is loaded FROM DISK (main() line ~2250), so an entry can predate
        # this run by days. That is not hypothetical: the 2026-09-03 re-derive notes say
        # "_persist_derived prefers a cached entry over recomputing, so a stale file writes
        # the OLD parser's answers straight back", and the only remedy on offer was to
        # remember to move jdmeta.json aside by hand. Comparing fingerprints makes that
        # structural: an entry whose jd_fp does not match the text we are holding is not a
        # cache hit, it is a different document, and it gets recomputed.
        #
        # Entries written before this column existed carry no jd_fp at all, so they all
        # miss once and are rebuilt with one. That is a one-off cost of ~206 ms per row
        # over the few hundred rows a new-only run touches, not over the corpus.
        fp = db.jd_fingerprint(row_jd.get(u))
        m = (jdmeta or {}).get(u)
        if m is not None and m.get("jd_fp") != fp:
            m = None
        if m is None:
            jd = row_jd.get(u) or ""
            if not jd:
                continue
            # The SAME idf the scoring loop used. Analyzing with idf=None would silently give
            # every term weight 1.0, so this row's score would not be comparable with any other.
            m = core.job_meta(jd, idf)
            m["jd_fp"] = fp
        exp_y, (sv, sreason) = m.get("exp_years"), (m.get("sponsor_jd") or ("", ""))
        # The keyword weights the FEED scores every résumé against — see core.pack_analyzed.
        terms = core.pack_analyzed(m.get("analyzed") or {})
        # facts_fp answers "which text is this reading about". Taken from `m` rather than
        # recomputed here, so it names the document that actually produced the four values
        # beside it even when they came from the cache.
        want_jd = {"exp_max_years": exp_y, "sponsor_jd": sv, "sponsor_reason": sreason,
                   "facts_fp": m.get("jd_fp"),
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
            if _norm_cmp("jd_terms", want_jd["jd_terms"]) != _norm_cmp(
                    "jd_terms", have.get("jd_terms")):
                jd_dirty.append(u)
            _flush_jd()

    # TWO writes, not one combined payload. db._upsert normalizes each chunk to the UNION of
    # its rows' keys and fills the gaps with None, so a row that skipped the JD block above
    # would be sent with an explicit exp_max_years: null and ERASE a value an earlier run
    # derived. Separate calls mean separate key unions.
    #
    # The location/pay group stays ONE write, and that is measured rather than assumed: it only
    # ever carries rows whose parse CHANGED -- 1,809 of ~46k on the run that died, and it had
    # already landed when the process was killed. The JD group is the big one because jd_terms is
    # deliberately not in db.COLS_SCORE, so every row a pass analyses diffs as changed.
    _send_derived(payload, "Derived fields",
                  "%d state, %d remote, %d with pay"
                  % (stats["state"], stats["remote"], stats["salary"]))
    _flush_jd(force=True)
    jd_summary = ("%d with an experience floor, %d with a sponsorship verdict, %d scoreable"
                  % (stats["exp"], stats["spon"], stats["terms"]))
    # One line at the end whatever the batch count -- `JD fields:` is what the cron log is
    # grepped for, and what says the pass got all the way through its write.
    if jd_write["lost"]:
        print("JD fields: updated %d job(s), LOST %d to a failed write -- %s."
              % (jd_write["sent"], jd_write["lost"], jd_summary))
    elif jd_write["sent"]:
        print("JD fields: updated %d job(s) — %s." % (jd_write["sent"], jd_summary))
    else:
        print("JD fields already current (%s)." % jd_summary)
    # Said out loud because it is the number that explains the next feed render: these users'
    # cards fall back to "Not scored" until scripts/score_users.py fills them in again.
    if jd_write["stale"]:
        print("Stored user scores dropped for %d re-analysed job(s) — run "
              "scripts/score_users.py to refill." % jd_write["stale"])


def _send_derived(payload, label, summary=None, keys=None, hint=True):
    """One diffed payload -> the jobs table, or a self-serve migration hint if the columns
    aren't there yet. Split out so the location/pay and JD groups can be written separately, and
    so the JD group can be written in batches as its loop builds it.

    True if the rows landed. An empty payload counts as landed -- there was nothing to lose.

    `summary` prints alongside the row count; a batch passes None, because the counts it would
    quote are the run's totals and not the batch's, so its caller prints one line at the end.
    `keys` names the column group -- see db._upsert on why a batched write has to state it.
    `hint` prints the schema; the caller turns it off after the first failure.
    """
    if not payload:
        if summary is not None:
            print("%s already current (%s)." % (label, summary))
        return True
    # ONE RETRY, AND ONLY FOR THE PROVENANCE STAMP. facts_fp is the newest column written
    # here, so the window between deploying this code and pasting
    # MIGRATION_jd_fingerprints.sql is the one time the whole JD group would fail -- taking
    # exp_max_years and jd_terms down with it, which is the very data the stamp exists to
    # protect. Losing the stamp for a day is a cost; losing the readings is a regression.
    #
    # Written as a loop over two payloads rather than a nested try, so the failure reporting
    # below stays the single exit for every kind of failure -- including a retry that also
    # fails, which an inner except would otherwise swallow into its own message.
    # ONE RETRY, AND ONLY FOR THE PROVENANCE STAMP. facts_fp is the newest column written
    # here, so the window between deploying this code and pasting
    # MIGRATION_jd_fingerprints.sql is the one time the whole JD group would fail -- taking
    # exp_max_years and jd_terms down with it, which is the very data the stamp exists to
    # protect. Losing the stamp for a day is a cost; losing the readings is a regression.
    #
    # A flag rather than a message re-test bounds the retry BY CONSTRUCTION. Deciding whether
    # to go round again by re-reading the error would loop for ever if a database ever
    # answered a stripped payload with a message that still named the column -- and 'that
    # cannot happen' is not something this file gets to assume on the scrape's critical path.
    body, cols, retried, err = payload, keys, False, None
    while True:
        try:
            db.update_job_fields(body, keys=cols)
            err = None
            break
        except Exception as e:
            err = e
            if retried or not db._column_missing(e, "facts_fp"):
                break
            retried = True
            print("  (jobs.facts_fp not migrated yet — writing the readings without their "
                  "provenance stamp. Run MIGRATION_jd_fingerprints.sql.)")
            body = [{k: v for k, v in r.items() if k != "facts_fp"} for r in body]
            cols = tuple(c for c in (cols or ()) if c != "facts_fp") or None
    if err is not None:
        print("  (%s write failed: %s)" % (label.lower(), str(err)[:160]))
        if hint:
            print("  If that mentions an unknown column, run this once in the SQL editor:")
            print("")
            print(db.JOBS_DERIVED_SQL)
        return False
    if summary is not None:
        print("%s: updated %d job(s) — %s." % (label, len(payload), summary))
    return True


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


# ---- Thin descriptions: a bounded, self-scheduling retry -------------------------------
#
# A row can hold a description that is really a JavaScript loading shell or a page title plus a
# nav bar. It scores 0 and reads blank in the feed, exactly like an empty one — but the fetch
# queue is built from rows whose `jd` is EMPTY, so a junk value is STICKY: nothing ever tries
# again. 1,533 rows (7.0% of the corpus) were in that state on 2026-08-17.
#
# The obvious fix, treating thin as missing, is a trap. Most of those rows sit on hosts that
# genuinely cannot be read server-side (an Akamai 403, an AWS WAF challenge, a closed posting),
# so a blanket retry spends the whole fetch budget re-failing the same rows every run. That is
# the waste audit_jd_coverage.py's BLOCKED class exists to name.
#
# So the unit of memory is the HOST, not the row: the fact worth recording is "does this host's
# extractor work", a property of ~200 hosts and of this file, not of 1,533 rows. Each heavy pass
# probes a few rows from every host that is due; a host that fails backs off exponentially, and
# a host that succeeds drains its backlog over the following runs.
THIN_LEDGER_KEY = "jd_thin_hosts"
THIN_PROBE_MAX = int(os.environ.get("SCORE_THIN_PROBE") or 150)      # per run, across all hosts
THIN_PROBE_PER_HOST = int(os.environ.get("SCORE_THIN_PER_HOST") or 3)
THIN_DRAIN_MAX = int(os.environ.get("SCORE_THIN_DRAIN") or 400)      # host that just worked
THIN_BACKOFF_CAP = 6                                                 # 2**6 = 64 days

CURSOR_KEY = "score_cursor"
"""Where a budget-truncated full pass stopped, so the next one resumes instead of restarting."""

ANALYZE_CHUNK = int(os.environ.get("SCORE_ANALYZE_CHUNK") or 500)
"""Rows analyzed per banked upsert. The analysis is ~200 ms/row, so this is ~100 s of work at
risk if the process is killed between flushes — against one extra upsert per 500 rows, which is
~46 writes over a 23k-work corpus. Tuned for "lose a little", not for "write as rarely as
possible": the whole point of this phase is that its work survives being cut off.

TUNABLE SINCE 2026-09-02, because 500 is only "a little" when the process usually finishes.
On the cPanel box it often does not: the CloudLinux LVE budget is metered per ACCOUNT, and the
website's own Passenger workers plus two unrelated sibling apps were measured holding 1,355 MB
of it, so a score pass gets SIGKILLed with no traceback partway through the loop. Measured that
day: a pass reached "Scoring 925 of 40675" and was killed before row 500, which banked NOTHING
even though the run had done minutes of real work and the 614 descriptions it was analysing were
already stored. bin/cron_scrape.sh sets 100 for that reason.

Lower is not free -- it is one upsert per N rows -- but an upsert is cheap and losing the whole
loop is not, and on a box where the kill is routine the trade moves."""


def _is_thin_jd(jd):
    """A stored description that is present but unusable.

    ONE definition, shared by the scorer, the retry planner and scripts/refetch_thin_jds.py.
    core._MIN_JD_CHARS is already the threshold at which core.analyze gives up and the feed
    renders "description pending", so a second number here could only ever disagree with it.
    """
    t = (jd or "").strip()
    return 0 < len(t) < core._MIN_JD_CHARS


def _accept_jd(url, jd, thin_len, stored=None):
    """Should this freshly-fetched text replace what is stored?

    A GAIN RULE, not a length test, and the distinction is the whole point: re-reading the same
    46-character Salesforce shell must not count as a repair, while a genuine 7,000-character
    description must. Requiring several times the old length separates them. A row that stored
    NOTHING accepts whatever the extractor chain was willing to return — that path has its own
    guard in MIN_PAGE_JD_CHARS.

    Consequence worth stating plainly: a probe can never shorten or blank a description we
    already hold, so the retry below cannot make the corpus worse.

    ...EXCEPT WHERE WHAT WE HOLD IS NOT A DESCRIPTION. The gain rule is right for shells and
    exactly backwards for a captured careers-site page: those are LONG, so a correct 3,400-char
    Google description would have to reach 24,000 to displace an 8,000-char navigation bar, and
    the 1,052 rows in that state were frozen permanently. `stored` is optional and only ever
    read to ask core.clean_jd whether the incumbent is a posting at all; when it is not, any
    replacement that IS one wins on merit rather than on length.
    """
    if not jd:
        return False
    if stored and core.clean_jd(stored)[1] == "not-a-posting" \
            and core.clean_jd(jd)[1] != "not-a-posting":
        return True
    old = thin_len.get(url, 0)
    if not old:
        return True
    return len(jd) >= core._MIN_JD_CHARS and len(jd) >= 3 * old


def _extractor_rev():
    """A fingerprint of the JD-extraction code, so shipping a working extractor re-opens every
    host that had backed off, without anyone having to remember to clear a flag.

    Hashes THIS FILE plus core.fetch_jd, rather than inspect.getsource over a list of extractor
    functions. Two reasons, in order of importance:

      * a name list is a thing to forget. Add branch nine to detail_jd, leave it out of the
        list, and the mechanism silently keeps every host backed off — the exact failure it
        exists to prevent, and invisible.
      * reading live globals made the fingerprint depend on what was monkeypatched. The retry
        tests replace detail_jd with a stub, which changed the hash and made a backed-off host
        look due; the test caught it, but the same fragility would apply to any caller that
        wraps an extractor.

    The cost of hashing the whole file is one probe round — at most SCORE_THIN_PROBE fetches —
    after any edit to it, including one that touches no extractor. That is a few seconds of a
    nine-minute budget, and it buys a guarantee instead of a convention.
    """
    try:
        import hashlib
        with open(__file__, "rb") as fh:
            src = fh.read()
        try:
            import inspect
            src += inspect.getsource(core.fetch_jd).encode("utf-8", "replace")
        except Exception:
            pass
        return hashlib.sha1(src).hexdigest()[:12]
    except Exception:
        return "nosrc"        # stable: fingerprint invalidation off, timed backoff still on


def _load_thin_ledger():
    """{"rev": <fingerprint>, "hosts": {host: {f, next, last, n, ok}}}.

    Stored as one JSON blob in the scrape_status KV table — the same trick web.py uses for the
    database-size history, and for the same reason: it earns no migration. ~20 KB, one read and
    one write per heavy pass. get_kv never raises, so an absent table reads as an empty ledger,
    which _thin_retry_plan is designed to survive.
    """
    led = db.get_kv(THIN_LEDGER_KEY) or {}
    hosts = led.get("hosts")
    return {"rev": led.get("rev") or "", "hosts": hosts if isinstance(hosts, dict) else {}}


def _save_thin_ledger(led):
    db.put_kv(THIN_LEDGER_KEY, {"rev": led.get("rev") or "", "hosts": led.get("hosts") or {}})


def _score_rev(resume):
    """A fingerprint of what the match score MEANS: the three functions that define it, plus the
    résumé it is measured against.

    The cursor below is only safe to resume from while the answer would not have changed. Change
    the algorithm or edit the résumé and every stored score is stale, so resuming mid-corpus
    would leave the tail on the old scale indefinitely — the exact drift that made the manual
    2026-08-18 re-score necessary. A changed rev throws the cursor away and restarts at the
    newest row.

    Named functions rather than the whole of core.py, which is the opposite of what
    _extractor_rev does, and deliberately: core.py is also the web app's helper module, so
    hashing it whole would reset the cursor on edits that cannot move a single score — and the
    cost of a false reset here is a corpus that never finishes, not one extra probe round. The
    trade is that editing a HELPER these three call will not reset it; `--full` and
    SCORE_RESET_CURSOR=1 are the manual overrides for that case, and both are cheap."""
    try:
        import hashlib
        import inspect
        src = b""
        # HOW A DESCRIPTION IS READ IS PART OF WHAT THE SCORE MEANS, and these two were not in
        # the list. clean_jd decides which characters analyze_jd ever sees and experience_years
        # produces exp_max_years outright, but getsource(analyze_jd) returns only its OWN body,
        # so both are invisible here. Both were edited on 2026-09-03 -- markdown escapes, then
        # the "1-year experience" form -- and neither moved the fingerprint, so the cursor would
        # have skipped rows whose answer had just changed. Named individually rather than
        # hashing core.py whole, for the reason the docstring above gives.
        for fn in (core.analyze_jd, core.score_against, core.job_meta,
                   core.clean_jd, core.experience_years):
            try:
                src += inspect.getsource(fn).encode("utf-8", "replace")
            except Exception:
                pass
        # The tunables live at module scope, so a weight change is invisible to getsource above.
        #
        # ...AND SO IS THE VOCABULARY, which is why ATS_KEYWORDS joined this tuple on
        # 2026-09-08. getsource(analyze_jd) contains the IDENTIFIER and not the value, so
        # adding a term left this rev unchanged, _load_cursor returned the stored cursor,
        # and the resumable pass carried on from where it stopped -- head of the corpus on
        # the new vocabulary, tail on the old, for the ~22 heavy runs a cursor takes to
        # lap. jd_fp/facts_fp hash the description TEXT, which a vocabulary edit does not
        # touch, so scripts/check_derived.py would have reported the corpus healthy
        # throughout. Same failure the note above records for clean_jd and
        # experience_years, one level further out.
        #
        # sorted() IS LOAD-BEARING: repr() of a SET is order-randomised per process by
        # PYTHONHASHSEED (see core.analyze_jd's note on pack_analyzed), so the unsorted
        # spelling would differ on every run, discard the cursor every run, and starve
        # the tail exactly as if there were no cursor at all -- the precise failure the
        # except branch below exists to avoid.
        src += repr((getattr(core, "CORE_WEIGHT_FRACTION", ""), getattr(core, "MIN_SCALE", ""),
                     getattr(core, "_MIN_JD_CHARS", ""),
                     sorted(getattr(core, "ATS_KEYWORDS", ())))).encode("utf-8")
        src += (resume or "").encode("utf-8", "replace")
        return hashlib.sha1(src).hexdigest()[:12]
    except Exception:
        # Stable rather than random: a fingerprint we cannot compute must not silently restart
        # the pass on every run, which would starve the tail exactly like no cursor at all.
        return "norev"


def _load_cursor(rev):
    """The (first_seen, found_date, url) key the last truncated pass stopped after, or None.

    None whenever the pass should start from the newest row: no cursor stored, a different rev,
    or a stored shape we don't recognise. Never raises — get_kv doesn't, and a cursor is an
    optimisation, so anything unexpected must degrade to "start at the top" rather than abort."""
    cur = db.get_kv(CURSOR_KEY) or {}
    if (cur.get("rev") or "") != rev:
        return None
    key = cur.get("key")
    if isinstance(key, list) and len(key) == 3 and all(isinstance(x, str) for x in key):
        return tuple(key)
    return None


def _save_cursor(rev, key):
    """Store where to resume, or clear it when the pass completed (key=None)."""
    db.put_kv(CURSOR_KEY, {} if key is None else {"rev": rev, "key": list(key)})


def _thin_host(url):
    from urllib.parse import urlparse
    return (urlparse(url or "").netloc or "?").lower()


def _host_window(urls, n, seed):
    """The `n` rows to probe on this host today, ROTATED so it is a different `n` tomorrow.

    This used to be `sorted(by_host[h])[:n]` -- the same three URLs, alphabetically, on every
    single run forever. The daily `seed` above rotates which HOSTS are due; nothing rotated which
    ROWS within a host, so a host whose first three URLs happened to be unfetchable recorded a
    failure every time, doubled its backoff toward 64 days, and the rest of its backlog was never
    touched at all.

    That is not hypothetical. Measured on the live corpus 2026-09-01: apply.actalentservices.com
    held 755 rows the feed called "JD pending" and sat at f=2 with next=2026-09-05, while 31 of
    those rows were still listed on the board and would have returned a 5,393-character
    description on request. Three fixed draws spoke for 755 rows.

    The offset is `seed * n`, not `seed`. With a bare seed, consecutive days share n-1 of their n
    rows -- day 40 draws [40,41,42] and day 41 draws [41,42,43] -- so a host is re-probed almost
    entirely on rows that just failed. Stepping by the window size makes each run a fresh window
    and walks the whole backlog in len(urls)/n runs.

    seed=0 keeps the plain head, which is what the existing tests pin and what a caller that does
    not care about rotation gets.
    """
    if not urls or n <= 0:
        return []
    if not seed or len(urls) <= n:
        return urls[:n]
    off = (seed * n) % len(urls)
    # Modulo indexing rather than a slice, so the window wraps instead of being truncated when
    # the offset lands near the end -- otherwise the tail of every list is probed less often than
    # its head, which is a quieter version of the bug this function exists to fix.
    return [urls[(off + i) % len(urls)] for i in range(n)]


def _thin_retry_plan(thin_urls, ledger, rev, today, seed=0):
    """(urls to probe, hosts probed) for this run.

    Pure, and `today`/`seed` are parameters rather than clock reads so a test can drive dates.

    Bounded BY CONSTRUCTION rather than by the ledger: with no ledger at all — a fresh database,
    an unreachable KV table, a cold CI runner — every host looks due and the result is still at
    most THIN_PROBE_MAX urls. That is the property that makes this safe to schedule.
    """
    hosts = ledger.get("hosts") or {}
    stale_rev = (ledger.get("rev") or "") != rev
    by_host = {}
    for u in thin_urls:
        by_host.setdefault(_thin_host(u), []).append(u)

    due = []
    for h, us in by_host.items():
        rec = hosts.get(h) or {}
        if stale_rev or not rec or (rec.get("next") or "") <= today:
            due.append((int(rec.get("f") or 0), -len(us), h))
    # Fewest failures first (likeliest to work), then biggest backlog. Rotated by day so a long
    # tail of equally-stale hosts cannot be starved behind the per-run cap forever.
    due.sort()
    if due and seed:
        k = seed % len(due)
        due = due[k:] + due[:k]

    picked, probed, spent = [], [], 0
    for fails, _neg, h in due:
        rec = hosts.get(h) or {}
        hot = int(rec.get("f") or 0) == 0 and int(rec.get("ok") or 0) > 0
        take = _host_window(sorted(by_host[h]),
                            THIN_DRAIN_MAX if hot else THIN_PROBE_PER_HOST, seed)
        if not take:
            continue
        probed.append(h)
        picked.extend(take)
        if not hot:
            spent += len(take)
        if spent >= THIN_PROBE_MAX:
            break
        if len(picked) >= THIN_DRAIN_MAX + THIN_PROBE_MAX:
            break
    return picked[:THIN_DRAIN_MAX + THIN_PROBE_MAX], probed


def _record_thin_outcomes(ledger, probed_hosts, thin_urls, repaired, rev, today):
    """Update the ledger from what the run actually managed. Mutates and returns it."""
    import datetime as _dt
    hosts = ledger.setdefault("hosts", {})
    won = {_thin_host(u) for u in repaired}
    counts = {}
    for u in thin_urls:
        h = _thin_host(u)
        counts[h] = counts.get(h, 0) + 1
    d0 = _dt.date.fromisoformat(today)
    for h in probed_hosts:
        rec = hosts.setdefault(h, {})
        rec["n"] = counts.get(h, 0)
        rec["last"] = today
        if h in won:
            rec["f"] = 0
            rec["ok"] = int(rec.get("ok") or 0) + 1
            rec["next"] = today                    # hot: drain the rest of it next run
        else:
            rec["f"] = int(rec.get("f") or 0) + 1
            days = 2 ** min(rec["f"], THIN_BACKOFF_CAP)
            rec["next"] = (d0 + _dt.timedelta(days=days)).isoformat()
    # Forget hosts with no thin rows left — the 30-day prune retired them.
    for h in [h for h in hosts if h not in counts and h not in won]:
        hosts.pop(h, None)
    ledger["rev"] = rev
    return ledger


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


def _new_only_targets(known_urls, fetched, unscored=()):
    """URLs worth (re)scoring when we're not doing the whole corpus.

    A stored job's score can only move for three reasons: its JD changed, the résumé
    changed, or IDF drifted as the corpus turned over. The first is exactly what this
    catches — the postings this run added, plus any job whose JD only just arrived. The
    other two are corpus-wide and belong to the full pass, which still runs daily.

    `known_urls` is every url we hold a row for. It used to be the JD map, which is no longer
    loaded in new-only mode — the set of urls is all this ever needed from it.

    `unscored` closes a hole that stranded rows permanently. This function used to look only at
    what THIS run touched, on the stated grounds that the daily full pass catches everything
    else. But a run that ingests more than SCORE_MAX_FETCH rows (843 and 1,833 on two days this
    month, against a cap of 400 on cPanel) leaves the excess with a NULL match_score, and those
    rows are in neither set — not fetched this run, and gone from last_new_jobs.json the moment
    the next sweep overwrites it. The full pass that was supposed to sweep them up runs only in
    the GitHub Actions job, which had been failing for four days, so 729 rows sat unscored and
    therefore invisible to the feed's match filter no matter where the floor was set.
    """
    targets = set(fetched)                        # JDs that landed this run
    try:                                          # ...plus the postings this run added
        with open(NEW_JOBS_FILE, encoding="utf-8") as fh:
            targets.update(j.get("url") for j in (json.load(fh) or []) if j.get("url"))
    except Exception:
        pass                                      # no breadcrumb (manual run) -> just the JDs
    targets.update(unscored)                      # ...plus anything a past run left unscored
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

    # ---- Rows that HOLD a junk description, not rows that hold none. --------------------
    #
    # Heavy pass only. --new-only never loads the corpus, runs on a smaller budget and fires
    # more often; giving it a second queue would be spending the cheap pass's budget on the
    # expensive pass's problem.
    thin_len, thin_probed = {}, []
    ledger, rev, today = None, "", ""
    if not new_only and not full:
        thin = {u: len(jd.strip()) for u, jd in row_jd.items() if _is_thin_jd(jd)}
        # VERIFY AGAINST THE DATABASE before queueing anything, and this is not optional.
        # Today "cache junk == column junk" holds only by accident: nothing ever writes a thin
        # row. Once repairs start, a run that writes the column and dies before _save_jd_cache
        # leaves the shell on disk — and the scorer analyses the CACHE, so the row would keep
        # scoring 0 while holding 7,000 characters. That is the 621-row half-repair of
        # 2026-08-17, mirrored. One bounded read closes it, and where the database is already
        # good this HEALS the cache for free, with no fetch at all.
        if thin:
            healed = 0
            for r in (db.load_jobs_by_urls(sorted(thin)) or []):
                u, jd = r.get("url"), (r.get("jd") or "")
                if not u or u not in thin:
                    continue
                if not _is_thin_jd(jd) and jd.strip():
                    row_jd[u] = jd                 # database is right, the cache was stale
                    thin.pop(u, None)
                    healed += 1
            if healed:
                print("Healed %d stale cache entr%s from the database (no fetch)."
                      % (healed, "y" if healed == 1 else "ies"))
        if thin:
            import datetime as _thin_dt
            _now = _thin_dt.date.today()
            today, rev = _now.isoformat(), _extractor_rev()
            ledger = _load_thin_ledger()
            retry, thin_probed = _thin_retry_plan(sorted(thin), ledger, rev, today,
                                                  seed=_now.timetuple().tm_yday)
            retry = [u for u in retry if u in all_urls]
            if retry:
                thin_len = {u: thin[u] for u in retry}
                # APPENDED to `order`, after the cap has already been applied — a probe must
                # never displace a row that has no description at all. The existing deadline
                # check inside the detail phase drains this tail for free when time runs out.
                missing |= set(retry)
                _have = set(order)
                order += [u for u in retry if u not in _have]
                print("Thin descriptions: %d row(s) hold a shell; probing %d across %d host(s) "
                      "that are due." % (len(thin), len(retry), len(thin_probed)))

    if missing:
        # 2) Bulk-fetch boards whose list API already includes the JD — one request
        #    covers the whole board, so try these first. Boards run concurrently.
        boards = scraper.SOURCES + scraper.custom_sources()
        bulk = [(b, a, c) for b, a, c in boards
                if a in BULK_JD_ATS
                and _board_has_missing(b, a, missing)]
        # THE UNTESTABLE BOARDS ARE ROTATED, NOT SKIPPED, AND THIS IS WHERE THE MEMORY WENT.
        #
        # _board_has_missing answers True unconditionally for jibe and phenom -- deliberately,
        # because those rows store an APPLY url whose host varies per tenant, so no cheap URL
        # test exists and Actalent's 1,461 rows once sat on a "Loading ..." shell forever
        # because the slug test said False. The cost of that decision was never counted:
        # measured on the live box, 149 of the 151 boards a run fetches are these, each
        # returning its WHOLE board (533 and 735 descriptions were in one sample) to satisfy
        # approximately none of the 547 rows actually wanted. That is ~450 MB of text
        # downloaded and parsed per run on an account with almost no headroom, and it is why
        # bounding the in-flight window alone still died at board 78 of 151.
        #
        # Rotated by the same rule _host_window uses for thin probes, and for the same reason:
        # a fixed head would fetch the same boards forever and never reach the tail. Every
        # board is still visited, just over several runs instead of all in one -- and a board
        # that CAN be tested cheaply is never rotated out, so this only ever delays the boards
        # we cannot ask about anyway.
        blind = sorted(e for e in bulk if e[1] in _BLIND_ATS)
        if len(blind) > _BLIND_BOARDS_PER_RUN:
            seed = int(_dtm.date.today().strftime("%Y%m%d"))
            keep = set(_host_window(blind, _BLIND_BOARDS_PER_RUN, seed))
            bulk = [e for e in bulk if e[1] not in _BLIND_ATS or e in keep]
            print("  %d board(s) have no cheap missing-row test: taking %d this run, the rest "
                  "on later runs." % (len(blind), _BLIND_BOARDS_PER_RUN))
        if bulk:
            print("Bulk-fetching JDs from %d board(s)..." % len(bulk))

            def _one(entry):
                board_url, ats, company = entry
                if deadline and time.time() >= deadline:
                    return company, {}, None      # out of time: skip, retry next run
                try:
                    time.sleep(random.uniform(0, 0.8))
                    return company, jd_map_for(board_url, ats, missing), None
                except Exception as e:
                    return company, {}, str(e)

            # BOUNDED IN FLIGHT, AND THIS IS THE PHASE THE CRON DIES IN.
            #
            # jd_map_for returns EVERY posting on a board with its description -- that is what
            # makes it one request instead of hundreds -- and a big board is tens of megabytes.
            # `ex.map(_one, bulk)` submits all 151 boards at once and holds each result until
            # the consumer reaches it IN ORDER, so one slow board pins every map that finished
            # behind it. Measured on the live box: 4 of the 6 score-step runs in the log exited
            # rc=137, always here, always inside "Bulk-fetching JDs from N board(s)".
            #
            # The account is the constraint and it cannot be tuned away: 1,189 MB of the ~1.2 GB
            # budget is already held by the web app's Passenger workers (842 MB) and two sibling
            # apps this account must not touch (586 MB). SCORE_MAX_FETCH was the documented
            # lever and it is the wrong one -- it bounds ROWS, and what overflows here is BOARD
            # MAPS, which the cap does not govern at all.
            #
            # as_completed, a submission window, and `del m` so a board's map is released the
            # moment its handful of wanted rows have been copied out. Flushed every CHUNK too,
            # for the reason the detail phase below already gives: a run killed mid-phase should
            # keep what it has fetched. The window is the memory bound; the flush is the
            # progress bound.
            CHUNK, buf, pending, queue = 150, {}, {}, list(bulk)
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
                while queue or pending:
                    while queue and len(pending) < _BULK_WINDOW:
                        entry = queue.pop()
                        pending[ex.submit(_one, entry)] = entry
                    done, _ = concurrent.futures.wait(
                        pending, return_when=concurrent.futures.FIRST_COMPLETED)
                    for fut in done:
                        pending.pop(fut, None)
                        company, m, err = fut.result()
                        if err:
                            print("  FAIL %-16s %s" % (company, err))
                            continue
                        hits = {u: jd for u, jd in m.items()
                                if u in missing and _accept_jd(u, jd, thin_len, row_jd.get(u))}
                        n_board = len(m)
                        del m                   # the board's whole map, gone before the next
                        fetched.update(hits)
                        buf.update(hits)
                        print("  OK   %-16s %d of %d JDs needed" % (company, len(hits), n_board))
                        if len(buf) >= CHUNK:
                            _persist_jds(buf)
                            buf = {}
            if buf:
                _persist_jds(buf)
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
                    # _accept_jd, not `if jd`: a failed fetch must never blank a stored JD, and
                    # re-reading the same shell must not count as a repair either.
                    if _accept_jd(u, jd, thin_len, row_jd.get(u)):
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
            # RELEASED IMMEDIATELY, the way the repair path at the top of the run already
            # does it. This dict is every stored description -- 492 MB peak RSS for 29,991
            # entries, measured on the cPanel box 2026-09-04 -- and without this `del` it
            # stayed referenced for the whole ANALYSIS below, which is the most expensive
            # phase in the process and the one that then gets SIGKILLed by an account-wide
            # ~1.2 GB LVE budget it shares with the website.
            #
            # Merging here is unavoidable: new-only holds only the rows it scored, so the
            # cache has to be read to avoid writing the corpus away. Holding it afterwards
            # is not.
            del bank
    else:
        _save_jd_cache({u: jd for u, jd in row_jd.items() if jd})

    # Which probed hosts actually yielded a description? A host that did goes hot and drains its
    # backlog next run; a host that did not doubles its backoff. Written AFTER both JD stores, so
    # a crash in between costs only the schedule — never a repair.
    if ledger is not None and thin_probed:
        repaired = [u for u in thin_len if u in fetched]
        _record_thin_outcomes(ledger, thin_probed, sorted(thin_len), repaired, rev, today)
        _save_thin_ledger(ledger)
        print("Thin probe: repaired %d of %d; %d host(s) rescheduled."
              % (len(repaired), len(thin_len), len(thin_probed)))

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
    # A stored score of NULL means no run has ever scored this row -- see _new_only_targets.
    unscored = {r["url"] for r in rows if r.get("url") and r.get("match_score") is None}
    # ...PLUS the rows a past run scored BEFORE they had a description.
    #
    # "match_score is NULL" was the whole backlog, and it has a hole a row never comes back out
    # of. Score a row while it holds no description and it gets match_score 0; give it a
    # description later -- the reconciliation pass above, a new extractor, an extension patch --
    # and it is in neither set: not NULL any more, and not something this run fetched. jd_terms
    # stays empty, so the feed renders it "JD pending" and the match filter cannot see it,
    # permanently. Every hole of this shape is silent by construction.
    #
    # ROWS WITH NO DESCRIPTION AT ALL ARE DELIBERATELY EXCLUDED, because analysing an empty
    # string writes match_score 0 and leaves jd_terms empty, so they would re-enter this set on
    # every run forever. They already have a queue: the JD FETCH, which is where a missing
    # description is actually fixable.
    #
    # THE SUBTRACTION IS NOT AIRTIGHT, and the measurement is worth more than the intention:
    # urls_missing_jd() matches jd NULL or jd = '', so a row holding WHITESPACE or a truncated
    # fragment is "not missing" and does land here. Measured on the live corpus 2026-09-01, the
    # whole set was 8 rows -- 7 of them 1 character or less, one between 10 and 399, and NOT ONE
    # with a real description. Those cannot gain terms (core.analyze_jd flags them thin and
    # pack_analyzed stores "" rather than a term-less analysis), so they do re-enter every run.
    # Left alone on purpose: at that size the analysis is a few microseconds each and a length
    # filter would have to read the jd column to apply, which is the ~130 MB read this whole
    # path exists to avoid. If that count ever grows into the hundreds, filter on length inside
    # urls_missing_jd_terms rather than here.
    #
    # And do not read the count above as the size of the WIN. The class this closes is mostly
    # drained by the match_score-is-NULL line already -- of 169 never-analysed active rows
    # measured earlier the same day, 161 carried a NULL score and were reachable without this.
    # What this adds is that the remainder stops being permanent, not a large one-off recovery.
    #
    # urls_missing_jd() is re-read rather than reusing `db_missing` from the top of the run: the
    # reconciliation pass above has since written descriptions for some of those rows, and the
    # stale snapshot would exclude exactly the ones it just repaired. Both calls select `url`
    # alone, so the pair costs ~50 KB.
    if new_only:
        stranded = db.urls_missing_jd_terms() - db.urls_missing_jd()
        if stranded:
            print("Picking up %d row(s) holding a description no run ever analysed."
                  % len(stranded))
        unscored |= stranded
    if new_only and unscored:
        print("Picking up %d row(s) a previous run left unscored." % len(unscored))
    todo = _new_only_targets(all_urls, fetched, unscored) if new_only else set(row_jd)
    # New-only mode never read the jd column, so pull the text for just these rows. JDs this
    # run fetched itself are already in row_jd from the phase above; the on-disk corpus covers
    # most of the rest, so only genuinely-unseen rows cost a request.
    if new_only:
        need = [u for u in todo if u not in row_jd]
        # THE DATABASE IS ASKED FIRST, AND THE ORDER IS THE ENTIRE FIX.
        #
        # _load_jd_cache() materialises EVERY stored description into one dict. Measured on
        # the cPanel box 2026-09-04: 8 MB -> 492 MB peak RSS, 29,991 entries, 159 MB of text,
        # and json.load() over the 35 MB gzip takes all of it in a single step that cannot be
        # chunked. This block asked for that FIRST, to serve a `need` of a few hundred rows --
        # the 18:14 run wanted 267.
        #
        # 484 MB landing on a fetch pass that already holds the corpus, 22 board maps and its
        # freshly fetched JDs, against an ACCOUNT-wide ~1.2 GB LVE budget shared with the
        # website's Passenger workers, is why that pass was SIGKILLed on EVERY run from
        # 2026-09-02 -- always immediately after the "Picking up N row(s)" lines, which are the
        # statements directly above this one. rc=137, twice a weekday, straight to cron mail.
        #
        # load_jobs_by_urls asks for exactly these urls, batched by query-string length, and
        # was ALREADY the fallback below. So this reorder introduces no new path -- it runs the
        # one every run already reaches, before the expensive one instead of after it.
        #
        # The cache stays as the fallback rather than being deleted: it is the only place a
        # description lives when a row reached the corpus but not the table, the case the
        # reconciliation phase above prints as "Re-persisting N cached JD(s)". It is now read
        # only when the table genuinely holds no text, so the 484 MB is spent on that repair
        # instead of on every run.
        #
        # Where both hold text the table now wins, which is the right way round: it is the
        # system of record, the analysis writes back to it, and the cache is a rebuildable
        # by-product of fetches that already happened.
        if need:
            row_jd.update({r["url"]: (r.get("jd") or "")
                           for r in db.load_jobs_by_urls(need) if r.get("url")})
            # `not row_jd.get(u)`, not `u not in row_jd`: a row the table answers with an
            # EMPTY jd has to stay in `need` so the cache can still repair it. Testing
            # membership would drop it here and analyse a blank description.
            need = [u for u in need if not row_jd.get(u)]
        if need:
            bank = _load_jd_cache()
            row_jd.update({u: bank[u] for u in need if u in bank})
            del bank                     # same reason as the merge above
    # THE ANALYSIS IS THE EXPENSIVE PHASE, and until now it was the only unbudgeted one.
    # core.job_meta costs ~206 ms/row against a 622k-term idf (core.score_against is 2.5 ms —
    # the cost is reading the posting, not comparing it to the résumé), so a full pass over
    # 25k rows is ~79 minutes. Measured 2026-08-18.
    #
    # That number sat behind a `timeout-minutes: 14` step with the single db.update_scores()
    # below it, which made the phase ALL-OR-NOTHING: killed mid-loop it banked the JDs it had
    # fetched (those already persist in chunks) and not one score. The daily heavy re-score
    # therefore cannot ever have completed on Actions, which is why stored scores were three
    # scoring commits stale until a manual run rewrote them.
    #
    # So: its own clock, chunked writes, and a cursor. Deliberately a SEPARATE knob from
    # SCORE_BUDGET_MIN rather than a shared deadline — that one is spent by the JD fetch ahead
    # of this, and a fetch that used all of it would leave scoring zero time and bank nothing,
    # turning one starved phase into two. 0 / unset = unlimited, which is what a manual
    # backfill wants and what every existing caller gets.
    analyze_min = 0.0
    try:
        analyze_min = float(os.environ.get("SCORE_ANALYZE_BUDGET_MIN") or 0)
    except ValueError:
        analyze_min = 0.0
    for _a in sys.argv:
        if _a.startswith("--analyze-budget-min="):
            try:
                analyze_min = float(_a.split("=", 1)[1])
            except ValueError:
                pass
    analyze_deadline = (time.time() + analyze_min * 60) if analyze_min > 0 else 0

    # Compute each job's résumé-INDEPENDENT analysis ONCE, reuse it for the score, AND persist
    # it to jdmeta.json so the web app never recomputes it at request time (kills cold-load
    # regex/keyword work). score_against(resume, analyzed) == the old skill_match(resume, jd).
    resume_low = resume.lower()
    # In new-only mode start from what's already on disk and MERGE, because this map is the
    # web app's precomputed cache for the whole corpus — writing back only the handful of
    # jobs we just scored would blank the other ~20k and push that work back to request time.
    jdmeta = (core.load_jdmeta() or {}) if new_only else {}

    # NEWEST FIRST, for the same reason the fetch queue is (see `order` above): a budget cuts
    # the tail, so the order decides who gets starved. A set's iteration order would hand that
    # decision to the hash seed. `u` is in the key so the sort is total and the cursor below
    # can name one exact row.
    def _skey(u):
        return (row_seen.get(u) or "", row_date.get(u) or "", u)
    todo_order = sorted(todo, key=_skey, reverse=True)

    # ...EXCEPT for the rows a PAST run left unscored, which go behind them OLDEST first.
    #
    # Newest-first is right for what this run just found and wrong for the backlog behind it.
    # Every cheap pass rebuilds this same list, so a budget in front of it cuts the tail in the
    # same place every time and an old unscored row is never reached — the analysis equivalent
    # of the starvation the cursor fixes for the full pass, which new-only has no cursor for
    # (it must never skip what it just fetched, so it cannot resume below one). FIFO for the
    # backlog bounds how long a row can wait instead; measured 2026-08-30, 944 rows held a full
    # description (median 5,528 chars) and no analysis at all, with stragglers three weeks old
    # under the days that buried them.
    #
    # `fetched` stays in the head, which is what keeps this from costing freshness: a row this
    # run got a description for is scored first either way. What moves behind the backlog is a
    # row that is new AND still has no JD — and that one scores 0 for being empty wherever it
    # sits in the list.
    if new_only and unscored:
        _back = {u for u in todo_order if u in unscored and u not in fetched}
        todo_order = ([u for u in todo_order if u not in _back]
                      + sorted(_back, key=_skey))

    # RESUMING. A budget alone still starves the tail: every run would re-analyze the same
    # newest rows and stop in the same place. The cursor is what turns repeated truncation into
    # coverage — each run picks up below where the last one stopped, and clearing it on
    # completion starts the next cycle from the top.
    #
    # Full pass only. New-only already targets a handful of rows it just fetched and must never
    # skip any of them, and it is the mode that runs on the crons.
    rev = _score_rev(resume) if not new_only else ""
    cursor = None
    if not new_only and analyze_deadline and not full:
        if (os.environ.get("SCORE_RESET_CURSOR") or "").strip().lower() in ("1", "true", "yes"):
            _save_cursor(rev, None)
        else:
            cursor = _load_cursor(rev)
    if cursor:
        resumed = [u for u in todo_order if _skey(u) < cursor]
        # Empty means the previous run finished the tail. Fall through to the whole list rather
        # than score nothing: that is the start of the next cycle, not a completed one.
        if resumed:
            todo_order = resumed
            print("  resuming below %s (%d row(s) left in this cycle)."
                  % ((cursor[0] or cursor[1] or "?"), len(todo_order)))
        else:
            cursor = None
            _save_cursor(rev, None)
            print("  previous cycle finished the corpus — starting a fresh pass.")

    print("Scoring %d of %d jobs with IDF weighting (%d terms in corpus)%s%s..."
          % (len(todo_order), len(all_urls), len(idf), " [NEW ONLY]" if new_only else "",
             "" if not analyze_deadline else " (%g min budget)" % analyze_min))

    scores, banked, last_key, unscored_left = {}, {}, None, 0
    stopped_at = len(todo_order)      # index we stopped at; the whole list unless the budget cuts in
    # The cursor must name the last row whose score actually REACHED the database, not the last
    # one analyzed. Those differ exactly when a flush fails, and taking the analyzed row there
    # would advance the cursor over rows that were never written — the next run would skip them
    # and they would hold a stale score until the rev changed. A dict because this is assigned
    # from inside _bank().
    progress = {"key": None}

    def _bank():
        """Flush what we have. Called every ANALYZE_CHUNK and once at the end, so a run that is
        killed or times out keeps everything up to its last flush."""
        if not scores:
            return
        try:
            db.update_scores(scores)
            banked.update(scores)
        except Exception as e:
            # Keep them for the next flush rather than dropping: a transient proxy error must
            # not silently cost the analysis we already paid ~200 ms/row for.
            print("  (score flush of %d failed, retrying next chunk: %s)"
                  % (len(scores), str(e)[:110]))
            return
        progress["key"] = last_key
        scores.clear()
        db.set_scrape_status({"phase": "scoring", "done": len(banked), "total": len(todo_order),
                              "found": _prev.get("found", 0), "new": _prev.get("new", 0),
                              "started_at": _started, "run": _prev.get("run", "")})

    for i, u in enumerate(todo_order):
        # Checked BEFORE the work, so the deadline is the last moment we START a row rather
        # than a moment we hope to land on. The remainder stays queued: an unreached row keeps
        # whatever score it already had, and the cursor sends the next run here.
        if analyze_deadline and time.time() >= analyze_deadline:
            unscored_left, stopped_at = len(todo_order) - i, i
            break
        jd = row_jd.get(u)
        # An absent KEY means the by-url lookup above failed or the row vanished mid-run —
        # skip it and leave the stored score alone. An empty STRING is different and must
        # still be scored: that is a real row with no JD yet, and it scores 0 by design.
        if jd is None:
            continue
        m = core.job_meta(jd, idf)
        # Stamped HERE, where the text is still in hand, so the entry stays self-describing
        # once it has been written to jdmeta.json and re-read by a later run.
        m["jd_fp"] = db.jd_fingerprint(jd)
        jdmeta[u] = m
        # A too-thin/truncated JD can't be scored honestly (it's what produced the fake ~100%s):
        # store 0 so it sorts/filters low and the feed shows it as "JD pending" (the web layer
        # keys off the same `thin` flag) instead of a misleading number.
        scores[u] = 0 if m["analyzed"].get("thin") else core.score_against(resume_low, m["analyzed"])[0]
        last_key = _skey(u)
        if len(scores) >= ANALYZE_CHUNK:
            _bank()

    # 5) Persist all scores. JDs were already uploaded incrementally in the fetch phase
    #    (guaranteeing forward progress on a timeout); this banks the final chunk.
    _bank()
    truncated = bool(unscored_left)
    if truncated:
        print("  analysis budget reached — %d row(s) left for next run (they keep their stored "
              "score)." % unscored_left)
    # DID THIS RUN ANALYSE THE WHOLE CORPUS? There are TWO ways it can fail to and `truncated`
    # only asks about one of them, which is the bug this flag exists to close.
    #
    # A pass is partial when the clock cut it off (`truncated`) OR when it started from a
    # cursor -- because then `todo_order` was never the corpus in the first place, it was the
    # slice below where the last run stopped. Finish that slice inside the budget and
    # `truncated` is False while 43,570 of 53,247 rows have not been looked at.
    #
    # MEASURED, because this failed in production on alternating days and read as flaky:
    #
    #   Sep  8  cursor slice 12,736  finished in budget  -> derived write over all 47,825  TIMEOUT (15m)
    #   Sep  9  no cursor, 51,585    budget cut at 19,742 -> derived write over 19,742      ok (20.9m)
    #   Sep 10  cursor slice 30,333  budget cut at 19,521 -> derived write over 19,521      ok (20.3m)
    #   Sep 11  cursor slice  9,677  finished in budget  -> derived write over all 53,247  TIMEOUT (26m)
    #
    # So the run that did the LEAST analysis did the MOST writing, and the step cap was raised
    # 14 -> 15 -> 26 chasing it. The two consumers below are the ones that asked the wrong
    # question; `_save_cursor` is NOT, and still keys off `truncated` -- "I ran out of clock,
    # resume here" is exactly what a cursor means, and a finished slice must still clear it so
    # the next cycle starts from the newest row.
    partial = truncated or bool(cursor)
    # New-only never touches the cursor: it is not walking the corpus, and its own leftovers are
    # already picked up by the NULL-score path in _new_only_targets.
    if not new_only:
        # Cleared on ANY completed full pass, not just a budgeted one: an unlimited run (a manual
        # backfill) leaves the whole corpus fresh, and a cursor surviving that would make the
        # next budgeted run resume mid-corpus and skip the newest rows for a whole cycle.
        _save_cursor(rev, progress["key"] if truncated else None)

    # jdmeta is the web app's cache for the WHOLE corpus and save_jdmeta REPLACES the file, so a
    # truncated pass must not write its partial map — that would blank the rows it never reached
    # and push their analysis back to request time. Worse, _persist_derived recomputes
    # core.job_meta for any row missing from this map, at the same ~206 ms each, so a partial
    # map would hand the whole cost we just budgeted straight to the derived-fields phase.
    # Carry the previous entries for rows still in the corpus; dead urls are still dropped.
    #
    # `partial`, NOT `truncated`: a cursor-resumed pass that finishes its slice holds a map of
    # just that slice -- 9,677 entries against a 53,247-row corpus on 2026-09-11 -- and this
    # merge was skipped for it, so save_jdmeta below replaced the whole file with the slice.
    # Every one of the other 43,570 rows then read as "no cached analysis": blanked for the web
    # app, and recomputed at ~206 ms each by _persist_derived immediately below.
    if partial and not new_only:
        prior = core.load_jdmeta() or {}
        for _u in all_urls:
            if _u not in jdmeta and _u in prior:
                jdmeta[_u] = prior[_u]
    core.save_jdmeta(jdmeta)
    scores = banked
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
    #
    #    A PARTIAL pass narrows for the same reason PLUS a sharper one: this function
    #    recomputes core.job_meta for any row absent from `jdmeta`, at the ~206 ms/row we just
    #    spent a budget bounding. The merge above keeps that from biting while jdmeta.json is
    #    warm, but on a cold cache (fresh checkout, a CI runner) every unreached row would be
    #    re-analyzed here — handing the derived phase the whole cost the budget just refused.
    #    Walked rows only, so the clock cannot escape through the back door.
    #
    #    AND `partial` IS WHAT CLOSES THAT BACK DOOR, because `truncated` left it open on every
    #    cursor-resumed run that finished its slice: this fell through to `row_loc`, the whole
    #    corpus, and the phase has no budget of its own. Measured on the 2026-09-11 run -- the
    #    write goes at ~2,000 rows per 80 s through the proxy, so 53,247 rows is ~35 minutes
    #    behind a 26-minute cap. It banked 26,000 and was killed. The walked-rows branch is
    #    ~19,700 rows at worst (what a 4-minute analysis budget reaches), which is the ~11
    #    minutes the runs that passed actually spent here.
    if new_only:
        _derive_src = {u: row_loc[u] for u in todo if u in row_loc}
    elif partial:
        _derive_src = {u: row_loc[u] for u in todo_order[:stopped_at] if u in row_loc}
    else:
        _derive_src = row_loc
    _persist_derived(_derive_src, row_jd, current_rows=rows, jdmeta=jdmeta, idf=idf)
    # vals/where were assigned only under `if scores:` while the sign-off below sits outside it,
    # so a run that scored NOTHING died with UnboundLocalError on its own summary line — after
    # every JD and score it did produce had already been written. Reachable in production any
    # time a scrape adds no new jobs; found by test_new_only_does_no_thin_probing.
    vals = list(scores.values()) if scores else []
    where = db.backend_name()

    # ONE LINE that answers "how many jobs can actually be READ", which is not the same question
    # as "how many have a jd". Coverage was reported at 97.4% while 7% of rows held a loading
    # shell, because those two were conflated. Cheap — row_jd is already in memory.
    if not new_only:
        _usable = sum(1 for jd in row_jd.values() if jd and not _is_thin_jd(jd))
        _thin_n = sum(1 for jd in row_jd.values() if _is_thin_jd(jd))
        print("JD health: %d usable, %d thin (a shell), %d with none — %.1f%% of %d rows readable."
              % (_usable, _thin_n, len(all_urls) - _usable - _thin_n,
                 100.0 * _usable / max(len(all_urls), 1), len(all_urls)))

    # "Done" has to stop meaning "finished" when the budget cut in, or the log reads as a
    # completed re-score while part of the corpus still holds scores from the previous scale —
    # which is precisely the confusion that let three scoring commits sit unapplied.
    _partial = (" PARTIAL: %d row(s) still on their previous score, resuming next run."
                % unscored_left) if truncated else ""
    if vals:
        print("Done. Scored %d jobs (avg %d%%, max %d%%), %d new JD(s), %d date(s) -> %s.%s"
              % (len(vals), sum(vals) // len(vals), max(vals),
                 len(fetched), len(dates), where, _partial))
    else:
        print("Done. Nothing to score, %d new JD(s), %d date(s) -> %s.%s"
              % (len(fetched), len(dates), where, _partial))

    # Progress bar: everything finished — the feed page polls this and shows "Done".
    db.set_scrape_status({"phase": "done", "scored": len(scores),
                          "new": _prev.get("new", 0), "found": _prev.get("found", 0),
                          "started_at": _started,
                          "finished_at": _dtm.datetime.now(_dtm.timezone.utc).isoformat(),
                          "run": _prev.get("run", "")})


if __name__ == "__main__":
    main()
