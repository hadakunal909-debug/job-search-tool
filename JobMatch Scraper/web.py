"""
web.py — Flask UI for cPanel (Passenger WSGI).

Mirrors the Streamlit app's features but as plain request/response pages, so it runs
on shared cPanel (which can't host Streamlit's always-on server). It REUSES the same
backend modules — core.py (ATS scoring), db.py (Supabase storage), auth.py (login),
scraper.py (add-board) — so behaviour matches the Render app.

Run locally:   python web.py        (http://127.0.0.1:5000)
On cPanel:     passenger_wsgi.py exposes `application = web.app`
"""
import os
import re
import json
import time
import html
import hmac
import hashlib
import secrets
import datetime
import functools
import collections

import gzip as _gzip

# Load a local .env (GEMINI_API_KEY, SUPABASE_*, etc.) when running `python web.py`. No-op if
# python-dotenv isn't installed or there's no .env — production sets real env vars.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    pass

from flask import (Flask, request, session, redirect, url_for,
                   render_template, flash, g, Response)

import core
import db
import auth
# `scraper` is a heavy (~2000-line) module needed only by the add-board + extension routes,
# and Resume Brain is needed only on /brain* routes — both are imported lazily (scraper inside
# its functions, Resume Brain via the _LazyMod proxies below) so a cold Passenger start doesn't
# pay their import cost (Resume Brain transitively pulls requests/bs4). `requests` itself is now
# only imported by db on the first DB call and inside _trigger_github_action.


class _LazyMod:
    """A stand-in for a module that imports it on first attribute access. Lets /brain* routes
    keep writing `rb.run_tailor(...)` etc. while the import stays off the cold-start path."""
    def __init__(self, name):
        self.__dict__["_name"] = name
        self.__dict__["_mod"] = None

    def __getattr__(self, attr):
        m = self.__dict__.get("_mod")
        if m is None:
            import importlib
            m = importlib.import_module(self.__dict__["_name"])
            self.__dict__["_mod"] = m
        return getattr(m, attr)


rb = _LazyMod("resume_brain.brain")
rb_ai = _LazyMod("resume_brain.ai")
rb_export = _LazyMod("resume_brain.export")
rb_latex = _LazyMod("resume_brain.latex")

app = Flask(__name__)


def _fallback_secret():
    """Signing key when neither APP_SECRET nor a Supabase key is configured (local dev).
    Derive a stable, MACHINE-LOCAL value rather than a globally-known constant, so the
    session/extension-token signature can't be forged just by reading this source."""
    import platform
    seed = "jobmatch-dev|%s|%s" % (platform.node(), os.path.abspath(__file__))
    return hashlib.sha256(seed.encode()).hexdigest()


# Session signing key: explicit APP_SECRET, else the (secret, server-side) Supabase key,
# else a machine-local dev fallback. Stable across restarts so logins persist.
_explicit_secret = os.environ.get("APP_SECRET")
_supabase_key = db._creds()[1]
app.secret_key = (_explicit_secret
                  or (hashlib.sha256(_supabase_key.encode()).hexdigest() if _supabase_key
                      else _fallback_secret()))
if not _explicit_secret and not _supabase_key:
    import sys as _sys
    print("WARNING: no APP_SECRET or SUPABASE_KEY set — using a machine-local dev signing "
          "key. Set APP_SECRET in production so sessions/tokens can't be forged.", file=_sys.stderr)

app.permanent_session_lifetime = 60 * 60 * 24 * 30      # 30-day login
# Cookie hardening: HttpOnly (no JS access) + SameSite=Lax (blocks cross-site POST CSRF on
# our cookie-auth forms). Secure is opt-in via env so local/preview over http still works —
# set SESSION_COOKIE_SECURE=1 in production (https) to stop the cookie leaking over http.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "").lower() in ("1", "true", "yes"),
)


@app.before_request
def _csp_nonce():
    """A fresh random nonce per request. Templates stamp it onto their inline <script>
    tags (nonce="{{ csp_nonce }}") so the CSP can allowlist OUR inline scripts by nonce
    without opening the door to all inline script ('unsafe-inline')."""
    g.csp_nonce = secrets.token_urlsafe(16)


# Resources the UI legitimately loads from off-site, kept here so the CSP stays readable:
# Google Fonts (CSS from googleapis, font files from gstatic) + company logos (Google's
# favicon service at www.google.com/s2/favicons, which 301-REDIRECTS to tN.gstatic.com —
# CSP checks every hop of a redirect, so the gstatic wildcard must be allowed too).
# Everything else is same-origin ('self').
_CSP_TEMPLATE = (
    "default-src 'self'; "
    "script-src 'self' 'nonce-%s'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; "
    "img-src 'self' data: https://www.google.com https://*.gstatic.com; "
    "connect-src 'self'; "
    "form-action 'self'; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'"
)


@app.after_request
def _security_headers(resp):
    """Baseline hardening headers on every response (set-if-absent, so CORS/other headers
    are untouched). The CSP restricts script execution to same-origin files plus this
    request's nonce — inline <script> must carry nonce="{{ csp_nonce }}", and inline on*=
    handlers (which a nonce can't cover) have all been removed — so an injected <script> or
    event-handler attribute can't run, giving XSS defense-in-depth beyond input escaping.
    Inline styles stay allowed ('unsafe-inline' in style-src) — low risk and the templates
    rely on <style> blocks and dynamic style="" attributes."""
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Content-Security-Policy",
                            _CSP_TEMPLATE % getattr(g, "csp_nonce", ""))
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy",
                            "camera=(), microphone=(), geolocation=()")
    if request.is_secure or request.headers.get("X-Forwarded-Proto", "").lower() == "https":
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    # Static assets (style.css / app.js) are fingerprinted with a ?v= query (see base.html), so
    # they can be cached hard — the browser stops re-requesting them on every page load.
    if request.path.startswith("/static/"):
        resp.headers.setdefault("Cache-Control", "public, max-age=604800, immutable")
    return resp


# Content types worth gzipping. Static files (served via send_file) are skipped because Flask
# marks them direct_passthrough; they're small and already cache hard via the header above.
_COMPRESSIBLE = ("text/html", "application/json", "text/css", "application/javascript",
                 "text/javascript", "application/xml", "text/plain", "image/svg+xml")


@app.after_request
def _compress(resp):
    """gzip text responses (the ~500 KB inline feed JSON drops to ~80-120 KB). Honors
    Accept-Encoding, skips tiny / already-encoded / streamed bodies. Safe behind cPanel's
    mod_deflate — Apache won't re-compress a response that already carries Content-Encoding."""
    try:
        if resp.direct_passthrough or resp.headers.get("Content-Encoding"):
            return resp
        if "gzip" not in request.headers.get("Accept-Encoding", "").lower():
            return resp
        ctype = (resp.content_type or "").split(";")[0].strip().lower()
        if ctype not in _COMPRESSIBLE:
            return resp
        data = resp.get_data()
        if len(data) < 1024:
            return resp
        resp.set_data(_gzip.compress(data, 6))
        resp.headers["Content-Encoding"] = "gzip"
        resp.headers["Content-Length"] = str(len(resp.get_data()))
        vary = resp.headers.get("Vary")
        if not vary:
            resp.headers["Vary"] = "Accept-Encoding"
        elif "accept-encoding" not in vary.lower():
            resp.headers["Vary"] = vary + ", Accept-Encoding"
    except Exception:
        return resp
    return resp


# ----------------------------- caches -----------------------------
_jobs_cache = {"rows": None, "at": 0}
_score_cache = {}            # (username, resume_md5) -> {url: score}
_rows_cache = {}             # (username, resume_md5) -> [row dict w/o status], sorted by score desc
_SCORE_CACHE_MAX = 64        # cap so a long-lived process doesn't grow unbounded across profiles
# Above this many jobs, the feed stops shipping EVERY job inline and switches to top-N inline +
# server-side search/paging (/api/feed), so the payload + browser parse stay small at any corpus
# size. Below it, the original all-inline client-filtered path is used unchanged. Env-tunable.
_FEED_INLINE_MAX = int(os.environ.get("FEED_INLINE_MAX", "4000"))
_FEED_TOPN = int(os.environ.get("FEED_TOPN", "400"))      # how many top-match jobs to inline when paged
# Feed grouping: how many cards one (title, company) run gets before the rest collapse into a
# "+N more at <company>" tile. 0 turns grouping off entirely (an env-only kill switch — the value
# is handed to app.js via the template so both sides read the same number). _GROUP_MIN is derived
# rather than configured: a group only collapses if the tile hides at least TWO postings, so we
# never trade a card for a tile that reveals a single row.
_GROUP_LEAD = int(os.environ.get("FEED_GROUP_LEAD", "2"))
_GROUP_MIN = _GROUP_LEAD + 2
_sponsor_cache = {}          # url -> (verdict, reason) read from the JD (same for everyone)
_jdmeta = core.load_jdmeta()  # url -> {analyzed, exp_years, exp_level, sponsor_jd}; prewarmed from
                              # jdmeta.json (built by the cron scorer) so cold renders skip recompute
# Shape for a job with no precomputed JD analysis (a job added since the last cron score run).
# The feed list no longer carries JD text, so such a job simply shows no JD-derived badges and
# its baseline match_score until the next cron run refreshes jdmeta.json.
_EMPTY_META = {"analyzed": {}, "exp_years": None, "exp_level": "", "sponsor_jd": ("", "")}
_EVERIFY_INDEX = core.load_everify()              # None until everify.txt is built; tiny file

# sponsor_counts.json parses to ~118k keys / ~11 MB of Python objects. Loading that at import
# put it on Passenger's cold-start path, where shared-hosting memory is tightest and a failure
# takes down the whole app rather than one feature. Defer it to first use instead, like db.py's
# _LazyHTTP and the lazy `import scraper` in the routes below. {} until the file is built.
_sponsor_counts_cache = None


def sponsor_counts():
    global _sponsor_counts_cache
    if _sponsor_counts_cache is None:
        try:
            _sponsor_counts_cache = core.load_sponsor_counts()
        except Exception:
            _sponsor_counts_cache = {}      # a bad/huge file must not 500 the feed
    return _sponsor_counts_cache
_resume_cache = {}           # username -> (resume_text, fetched_at)
_status_cache = {}           # username -> ({url: status}, fetched_at); busted on every action
_STATUS_TTL = 30             # seconds; mutations bust immediately, this just bounds cross-worker drift
_RESUME_TTL = 60             # seconds; short so an edit in another worker shows up quickly


def current_resume():
    """The logged-in user's résumé text, from a short-lived in-process cache backed by
    the DB. The résumé must NEVER ride in the session cookie: cookies cap at ~4 KB and
    browsers silently DROP oversized ones — with a multi-KB résumé in the session, the
    login cookie itself gets dropped and the user can't sign in at all."""
    user = session.get("user")
    if not user:
        return ""
    hit = _resume_cache.get(user)
    if hit and time.time() - hit[1] < _RESUME_TTL:
        return hit[0]
    try:
        txt = (db.get_user(user) or {}).get("resume", "") or ""
    except Exception:
        return hit[0] if hit else ""     # transient DB failure -> stale value over nothing
    _resume_cache[user] = (txt, time.time())
    return txt


_profile_cache = {}          # username -> (profile_text, fetched_at)


def _ensure_resume_migrated(user):
    """One-time: fold the legacy single user.resume into the résumé library so it counts toward
    the complete profile. Safe to call repeatedly (no-op once the library has any résumé)."""
    try:
        if db.list_resumes(user):
            return
        legacy = (db.get_user(user) or {}).get("resume", "") or ""
        if legacy.strip():
            db.save_resume(user, {"name": "My résumé", "content": legacy})
    except Exception:
        pass


def current_profile():
    """The COMPLETE matching profile — every résumé + every story in the user's Resume Brain,
    combined. This is what the feed scores jobs against (the 'match my whole profile' design),
    from a short-lived cache backed by the DB."""
    user = session.get("user")
    if not user:
        return ""
    hit = _profile_cache.get(user)
    if hit and time.time() - hit[1] < _RESUME_TTL:
        return hit[0]
    _ensure_resume_migrated(user)
    try:
        txt = db.profile_text(user)
    except Exception:
        return hit[0] if hit else ""
    _profile_cache[user] = (txt, time.time())
    return txt


def _bust_profile(user=None):
    """Drop cached profile + scores after a résumé/story/lesson edit so the feed updates."""
    if user:
        _profile_cache.pop(user, None)
    else:
        _profile_cache.clear()
    _score_cache.clear()
    _rows_cache.clear()


def sponsor_signal(job):
    """(verdict, reason) for a job's JD sponsorship signal, computed once per URL."""
    u = job.get("url") or ""
    if u not in _sponsor_cache:
        _sponsor_cache[u] = core.sponsorship_from_jd(job.get("jd") or "")
    return _sponsor_cache[u]


_JOBS_TTL = 3600             # jobs change only on the daily scrape; force-refresh paths exist


def get_jobs(force=False):
    """All jobs from Supabase, cached ~1 h (or force-reloaded). Jobs only change on the daily
    cron scrape; /reload, add-board, and the extension endpoints force-refresh, so a long TTL
    just avoids needless full-table re-fetches between scrapes."""
    if force or _jobs_cache["rows"] is None or time.time() - _jobs_cache["at"] > _JOBS_TTL:
        try:
            # include_jd=False: the feed never shows the JD; the detail panel fetches one JD on
            # demand (db.get_job_jd), so we skip pulling ~20 MB of description text into memory.
            _jobs_cache["rows"] = db.load_jobs(include_jd=False) or []
        except Exception:
            _jobs_cache["rows"] = _jobs_cache["rows"] or []
        _jobs_cache["at"] = time.time()
    return _jobs_cache["rows"]


def user_statuses(user):
    """{url: status} for this user's liked/hidden/applied jobs, from a short-lived cache so a
    warm feed render needs no DB round-trip. Busted immediately on every like/hide/apply."""
    hit = _status_cache.get(user)
    if hit and time.time() - hit[1] < _STATUS_TTL:
        return hit[0]
    try:
        st = db.get_user_statuses(user)
    except Exception:
        return hit[0] if hit else {}
    _status_cache[user] = (st, time.time())
    return st


def jd_meta(job, idf):
    """JD-derived fields that are the SAME for every user (they depend only on the JD text):
    the analyzed keyword/weight structure (to score against any résumé), the experience
    floor + level, and the sponsorship signal. Computed once per URL and reused across all
    users and renders — this is what stops the feed re-running regex/keyword work per row,
    per request. Cleared on /reload and when JDs change via the extension."""
    u = job.get("url") or ""
    if u and u in _jdmeta:
        return _jdmeta[u]
    # Miss = a job added since the last cron score run; compute live with the SAME function
    # the cron uses (core.job_meta) so persisted and live values agree. Only cache it when we
    # actually had JD text — otherwise (the feed passes JD-less rows) we'd poison the cache with
    # an empty entry that survives until /reload re-pulls jdmeta.json.
    jd = job.get("jd") or ""
    meta = core.job_meta(jd, idf)
    if u and jd:
        _jdmeta[u] = meta
    return meta


def user_scores(username, resume):
    """{url: match%} for this user. Scores each job's stored JD against the résumé
    (core.skill_match); falls back to the precomputed baseline when no résumé/JD."""
    key = (username, hashlib.md5((resume or "").encode("utf-8")).hexdigest())
    if key in _score_cache:
        return _score_cache[key]
    resume_low = (resume or "").lower()      # lowercase ONCE, not per job (was ×2,500)
    scores = {}
    for j in get_jobs():
        u = j.get("url")
        if not u:
            continue
        # Score live against the precomputed JD analysis (jdmeta.json) when present — the feed
        # rows no longer carry JD text, and the analysis is JD-only so it needs no text here.
        # Jobs not yet in jdmeta (added since the last cron run) fall back to the baseline.
        meta = _jdmeta.get(u)
        if resume and meta:
            try:
                scores[u] = int(core.score_against(resume_low, meta["analyzed"])[0])
            except Exception:
                scores[u] = 0
        else:
            try:
                scores[u] = int(j.get("match_score") or 0)
            except Exception:
                scores[u] = 0
    if len(_score_cache) >= _SCORE_CACHE_MAX:
        _score_cache.pop(next(iter(_score_cache)), None)   # drop oldest; bounds memory growth
    _score_cache[key] = scores
    return scores


# Internship / co-op detection from the title (for the "Internship" badge + the Intern/Co-op
# filter). Whole-word so it won't fire on "international"/"internal". Matches intern(s|ship|ships),
# co-op / coop / co op (+ plurals), and the finance-internship "summer analyst/associate".
_INTERN_RE = re.compile(
    r"\b(?:intern(?:s|ship|ships)?|co[-\s]?ops?|summer analyst|summer associate)\b", re.I)


def _build_row(j, score):
    """One feed card's data (everything EXCEPT the per-user status, which is overlaid at serve
    time). Computes the JD badges from the cron precompute + the logo/sponsor/e-verify fields —
    the same shape app.js renders. Built once per (profile) and cached in _rows_cache."""
    c = j.get("company") or ""
    u = j.get("url")
    meta = _jdmeta.get(u) or _EMPTY_META
    sv, sreason = meta.get("sponsor_jd") or ("", "")
    strength, scount = core.sponsor_strength(c, sponsor_counts())
    exp_y = meta.get("exp_years")
    # A too-thin/truncated JD can't be scored honestly (see core.analyze_jd) — surface it as
    # "JD pending" instead of a misleading number, and keep it at 0 so it sorts/filters low
    # rather than sitting at a fake ~100% on top of the feed.
    pending = bool((meta.get("analyzed") or {}).get("thin"))
    # Location: prefer the columns score_jobs derived (it had the JD, so its `remote` is
    # better informed), but fall back to parsing the raw string here so the "where" filter
    # works even before db.JOBS_DERIVED_SQL has been run. parse_location is memoized over
    # the ~4,100 distinct spellings, so this costs nothing per row.
    lstate, lmetro = j.get("loc_state") or "", j.get("loc_metro") or ""
    lremote = j.get("remote")
    if not (lstate or lmetro):
        p = core.parse_location(j.get("location") or "")
        lstate, lmetro = p["state"], p["metro"]
        if lremote is None:
            lremote = p["remote"]
    # Salary comes from the columns only. It can't be parsed here as a fallback the way
    # location is: get_jobs() selects without `jd` on purpose (~20 MB of description text),
    # so pay appears once db.JOBS_DERIVED_SQL is applied and score_jobs has run once.
    smin, smax = j.get("salary_min") or None, j.get("salary_max") or None
    speriod = j.get("salary_period") or ""
    active = j.get("is_active")
    # `or ""` not .get(k, "") throughout: a NULL column comes back as None, not a missing key.
    return {"title": j.get("title") or "", "company": c, "location": j.get("location") or "",
            "loc_state": lstate, "loc_metro": lmetro, "remote": bool(lremote),
            "salary_min": smin, "salary_max": smax, "salary_period": speriod,
            "salary_label": core.salary_label(smin, smax, speriod),
            "closed": active is False or str(active).lower() == "false",
            "url": u, "apply_url": u if (u or "").startswith(("http://", "https://")) else "#",
            "sponsors_h1b": j.get("sponsors_h1b", ""),
            # date = the real posting date (verify_dates) when we have it, else found_date.
            # The "New" badge is derived client-side from this date (it shows iff it reads "Today").
            "date": ((j.get("posted_verified") or j.get("found_date")) or "")[:10],
            "date_verified": bool(j.get("posted_verified")),
            "score": 0 if pending else score, "score_pending": pending,
            "sponsor_jd": sv, "sponsor_reason": sreason, "agency": core.is_agency(c),
            "cap_exempt": core.is_cap_exempt(c), "everify": core.is_everify(c, _EVERIFY_INDEX),
            "exp_years": exp_y if exp_y is not None else "", "exp_level": meta.get("exp_level") or "",
            "strength": strength, "strength_n": scount,
            "intern": bool(_INTERN_RE.search(j.get("title") or "")),
            "logo_domain": logodomain(c), "logo_color": logocolor(c),
            "initial": c[:1].upper() if c else "?"}


_AGGREGATOR_HOSTS = ("adzuna.", "indeed.", "linkedin.", "ziprecruiter.", "glassdoor.")
_HOST_RE = re.compile(r"^[a-z]+://([^/?#]+)", re.I)


def _dupe_key(r):
    """Identity of a POSTING rather than of a URL: title + company + full location.

    Location is the RAW string, not just the state. Using the state collapsed 4,770 rows in
    this corpus, but almost all of them were real, distinct openings — Amazon genuinely lists
    431 "Operations Manager" roles and Walmart 144 store-level pharmacy internships. Those are
    inventory, not duplicates.
    """
    t = re.sub(r"[^a-z0-9]+", " ", (r.get("title") or "").lower()).strip()
    c = re.sub(r"[^a-z0-9]+", " ", (r.get("company") or "").lower()).strip()
    if not (t and c):
        return None
    loc = re.sub(r"[^a-z0-9]+", " ", (r.get("location") or "").lower()).strip()
    return (t, c, loc)


def _host(r):
    m = _HOST_RE.match(r.get("url") or "")
    return (m.group(1) if m else "").lower()


def _dupe_rank(r):
    """Preference among duplicates, lowest wins: the employer's own posting over an
    aggregator's copy, a verified posting date over a derived one, then a real score."""
    aggregator = any(h in _host(r) for h in _AGGREGATOR_HOSTS)
    return (1 if aggregator else 0,
            0 if r.get("date_verified") else 1,
            0 if r.get("score") else 1,
            -(r.get("score") or 0))


def _dedupe_rows(rows):
    """Collapse the SAME posting reaching us from two different hosts, keeping the better copy.

    Only groups spanning more than one host are collapsed. Within a single host, two rows that
    look alike are two separate openings with different job ids, and merging them would delete
    real jobs from the feed. In practice this is a small, precise fix — the duplicates it finds
    are Greenhouse serving one posting as both boards.greenhouse.io and job-boards.greenhouse.io.

    Render-time only: nothing is deleted, so it's reversible and can't lose a posting.
    """
    groups = {}
    singles = []
    for r in rows:
        k = _dupe_key(r)
        if k is None:                        # missing title or company: never merge blindly
            singles.append(r)
        else:
            groups.setdefault(k, []).append(r)

    out = singles
    for grp in groups.values():
        if len(grp) > 1 and len({_host(x) for x in grp}) > 1:
            out.append(min(grp, key=_dupe_rank))
        else:
            out.extend(grp)
    return out


def ranked_rows(username, resume):
    """The FULL corpus as card rows, sorted by this user's match score (desc), cached per
    (user, profile). Reuses user_scores; the master ordering for both the inline top-N and the
    server-paged /api/feed. Status is NOT baked in (overlaid per request) so the cache is shared
    and immutable. Cheap to filter in Python even at tens of thousands of rows."""
    key = (username, hashlib.md5((resume or "").encode("utf-8")).hexdigest())
    if key in _rows_cache:
        return _rows_cache[key]
    scores = user_scores(username, resume)
    rows = [_build_row(j, scores.get(j.get("url"), 0)) for j in get_jobs() if j.get("url")]
    rows = _dedupe_rows(rows)
    rows.sort(key=lambda r: r["score"], reverse=True)
    if len(_rows_cache) >= _SCORE_CACHE_MAX:
        _rows_cache.pop(next(iter(_rows_cache)), None)
    _rows_cache[key] = rows
    return rows


def _date_cutoff(date_param):
    """ISO date N days ago for the 'Posted within' filter, or '' for 'any'."""
    if not date_param or date_param == "any":
        return ""
    try:
        return (datetime.date.today() - datetime.timedelta(days=int(date_param))).isoformat()
    except Exception:
        return ""


def _user_prefs(user):
    """The user's saved search, always a complete valid dict (defaults if never saved or if
    the search_prefs column hasn't been migrated yet)."""
    try:
        return core.normalize_prefs((db.get_profile(user) or {}).get("search_prefs"))
    except Exception:
        return dict(core.DEFAULT_PREFS)


def _prefs_as_params(prefs):
    """Saved prefs -> the same query-arg shape _filter_rows reads, so one code path decides
    what matches whether the filters came from the URL or from the user's saved search."""
    return {
        "tab": "recommended", "min": str(prefs.get("min", 0)),
        "date": prefs.get("date") or "any", "exp": prefs.get("exp") or "any",
        "intern": prefs.get("intern") or "any", "loc": prefs.get("loc") or "",
        "minsal": str(prefs.get("minsal") or 0), "sort": prefs.get("sort") or "score",
        "remote": "1" if prefs.get("remote") else "",
        "hideagency": "1" if prefs.get("hideagency") else "",
        "everify": "1" if prefs.get("everify") else "",
        "hidenospon": "1" if prefs.get("hidenospon") else "",
    }


def _visa_badge_context(prof, timeline):
    """Two booleans app.js uses to word the E-Verify / cap-exempt badges for THIS viewer.

    Empty dict when the user hasn't entered any dates, which leaves the badges reciting the
    general rule — the correct default, since we shouldn't imply we know someone's situation.
    """
    if not timeline.get("has_data"):
        return {}
    return {
        # Still on post-completion OPT with a STEM window ahead: E-Verify actually matters now.
        "stemPending": any(i["key"] == "stem_window" for i in timeline["items"]),
        # Needs sponsorship and isn't cap-exempt-bound: the March lottery is a real dependency.
        "needsLottery": str(prof.get("requires_sponsorship_future")
                            or prof.get("needs_sponsorship") or "").strip().lower()
        in ("yes", "true", "1"),
    }


def _feed_metros(rows, limit=40):
    """Metros present in the corpus, busiest first — the location box's suggestions.
    Only offering places that actually have jobs keeps the user out of dead ends."""
    ct = collections.Counter(r.get("loc_metro") for r in rows if r.get("loc_metro"))
    return [m for m, _ in ct.most_common(limit)]


def _feed_states(rows):
    """State codes present in the corpus, alphabetical (they're suggestions, not a ranking)."""
    return sorted({r.get("loc_state") for r in rows if r.get("loc_state")})


# Pay scaling and location matching live in core so the feed, the digest and app.js can't
# drift apart on what "$100k+" or "boston" means. app.js mirrors both in JS.
_HOURS_PER_YEAR = core.HOURS_PER_YEAR
_annualize = core.annualize_pay
_loc_hit = core.location_matches


def _filter_rows(rows, statuses, p):
    """Server-side mirror of app.js matches() + sort: filter the ranked rows by the feed
    controls and return a list of (row, status) in display order. `p` is the query args."""
    q = (p.get("q") or "").strip().lower()
    searching = bool(q)
    tab = p.get("tab") or "recommended"
    try:
        minv = int(p.get("min") or 0)
    except Exception:
        minv = 0
    cut = _date_cutoff(p.get("date"))
    everify_only = (p.get("everify") or "") in ("1", "true", "yes", "on")
    hide_no = (p.get("hidenospon") or "") in ("1", "true", "yes", "on")
    exp = p.get("exp") or "any"
    intern = p.get("intern") or "any"      # any | only (intern/co-op only) | no (exclude them)
    loc = (p.get("loc") or "").strip().lower()
    remote_only = (p.get("remote") or "") in ("1", "true", "yes", "on")
    hide_agency = (p.get("hideagency") or "") in ("1", "true", "yes", "on")
    show_closed = (p.get("showclosed") or "") in ("1", "true", "yes", "on")
    try:
        minsal = int(p.get("minsal") or 0)
    except Exception:
        minsal = 0
    out = []
    for r in rows:
        st = statuses.get(r["url"], "")
        if tab in ("liked", "applied", "hidden"):
            if st != tab:
                continue
        else:
            if st == "hidden":
                continue
            if not (searching or r["score"] >= minv):   # search bypasses the match floor
                continue
        # Search covers LOCATION too — "boston" and "remote" are things people type here.
        if searching and q not in (r["title"] + " " + r["company"] + " " +
                                   (r.get("location") or "")).lower():
            continue
        if cut and r["date"] and r["date"] < cut:
            continue
        if hide_no and r["sponsor_jd"] == "blocked":
            continue
        if everify_only and not r["everify"]:
            continue
        if loc and not _loc_hit(r, loc):
            continue
        if remote_only and not r.get("remote"):
            continue
        if minsal:
            # Requires a STATED range, like every other job board: keeping unknown-pay rows
            # made the control look broken (the count never moved, because only ~a third of
            # descriptions state pay). The tooltip warns that this narrows the list a lot.
            sm = r.get("salary_min")
            if not sm or _annualize(sm, r.get("salary_period")) < minsal:
                continue
        if hide_agency and r.get("agency"):
            continue
        # Closed rows stay visible in the saved/applied tabs so tracker history never breaks.
        if not show_closed and r.get("closed") and tab not in ("liked", "applied"):
            continue
        if intern == "only" and not r.get("intern"):
            continue
        if intern == "no" and r.get("intern"):
            continue
        if exp != "any":
            ev = r["exp_years"]
            if ev != "" and ev is not None:
                try:
                    yrs = int(ev)
                except Exception:
                    yrs = None
                if yrs is not None:
                    if exp == "senior":
                        if yrs >= 6:
                            continue
                    elif yrs > (int(exp) if str(exp).isdigit() else 99):
                        continue
        out.append((r, st))
    if (p.get("sort") or "score") == "newest":
        out.sort(key=lambda rs: rs[0]["date"] or "", reverse=True)
    return out                                  # else already in score order (rows pre-sorted)


# ----------------------------- feed grouping -----------------------------
# Some employers list one role hundreds of times, once per site or store. Measured on the live
# corpus (16,416 rows): Actalent 527 "Project Manager", Amazon 431 "Operations Manager", Walmart
# 150 "Pharmacy Pre-Grad Intern - WM" — 403 such runs of 4+ covering 32% of every row we have.
# They are DISTINCT openings with their own job ids, so _dedupe_rows must not touch them (keying
# on title+company+state once collapsed 4,770 real postings). The problem isn't duplication, it's
# that one employer eats a screen. So we group for DISPLAY instead: a run of rows sharing
# (normalized title, company) shows its best one or two cards plus a "+N more at <company>"
# tile that expands the rest in place. Nothing is dropped — fully expanding a tile gets every
# posting back, and scripts/feed_parity.py asserts that.
#
# Grouping runs AFTER filtering, never before, so "+N more" always counts what the user's own
# filters left behind — a stale count would be a lie the moment they typed a location.
_NONALNUM_RE = re.compile(r"[^a-z0-9]+")


def _group_key(r):
    """Identity of a (title, company) run, or "" for a row that must never be grouped.

    Normalization is the same shape _dupe_key uses, so "Operations Manager" and
    "operations  manager" land together. The two halves are joined with "|", which is
    collision-free because normalization has already stripped every non-alphanumeric
    character from both. Mirrored in app.js groupKey().
    """
    t = _NONALNUM_RE.sub(" ", (r.get("title") or "").lower()).strip()
    c = _NONALNUM_RE.sub(" ", (r.get("company") or "").lower()).strip()
    if not (t and c):
        return ""
    return c + "|" + t


def _pick_leaders(members, lead):
    """The cards that represent a collapsed group, in their original rank order.

    `members` is already in display order, so the first one is the best under whichever sort is
    active. The second is the best row in a DIFFERENT state where one exists — two Amazon
    "Operations Manager" cards are worth far more when they're in two different places than when
    they're the top two rows of the same warehouse town. Falls back to plain rank order when the
    group has too few distinct states. Mirrored in app.js pickLeaders().
    """
    if len(members) <= lead:
        return list(members)
    picked, seen = [], set()
    for i, m in enumerate(members):
        s = (m[0].get("loc_state") or "").upper()
        if s not in seen:
            seen.add(s)
            picked.append(i)
            if len(picked) == lead:
                break
    for i in range(len(members)):               # not enough distinct states — fill by rank
        if len(picked) >= lead:
            break
        if i not in picked:
            picked.append(i)
    picked.sort()                               # keep the leaders in their original order
    return [members[i] for i in picked]


def _group_units(pairs):
    """[(row, status)] -> [(row, status, hidden_count, group_key)] display units.

    A group is anchored at the position of its FIRST member, so the active sort still drives the
    feed's order; the tile is attached to the group's LAST leader (hidden_count > 0 there, 0 on
    every other unit) so it renders directly beneath the cards it belongs to.
    """
    order, groups = [], {}
    for pr in pairs:
        k = _group_key(pr[0])
        if not k:
            order.append(("", pr))              # no title or company: never group blindly
            continue
        if k not in groups:
            groups[k] = []
            order.append((k, None))             # placeholder holding this group's slot
        groups[k].append(pr)

    out = []
    for k, pr in order:
        if not k:
            out.append((pr[0], pr[1], 0, ""))
            continue
        members = groups[k]
        if len(members) < _GROUP_MIN:            # too short to be noise — show every card
            out.extend((m[0], m[1], 0, "") for m in members)
            continue
        leaders = _pick_leaders(members, _GROUP_LEAD)
        hidden = len(members) - len(leaders)
        for i, m in enumerate(leaders):
            last = i == len(leaders) - 1
            out.append((m[0], m[1], hidden if last else 0, k if last else ""))
    return out


def _grouping_on(p):
    """Group the Recommended feed only. Liked/Applied/Hidden are the user's own shortlists —
    collapsing rows they deliberately saved would hide their tracker from them."""
    return _GROUP_LEAD > 0 and (p.get("tab") or "recommended") not in ("liked", "applied", "hidden")


def login_required(f):
    @functools.wraps(f)
    def wrap(*a, **k):
        if not session.get("user"):
            return redirect(url_for("login", next=request.path))
        return f(*a, **k)
    return wrap


@app.context_processor
def _inject():
    return {"current_user": session.get("user"),
            "csp_nonce": getattr(g, "csp_nonce", "")}


# --- company logo helpers (Google favicon by domain, with a letter-avatar fallback;
#     logo.clearbit.com is DEAD — Clearbit sunset the free logo API) ---
_DOMAIN_MAP = {
    "affirm": "affirm.com", "airbnb": "airbnb.com", "alixpartners": "alixpartners.com",
    "amazon": "amazon.com", "anaplan": "anaplan.com", "aurora innovation": "aurora.tech",
    "avery dennison": "averydennison.com", "bitgo": "bitgo.com", "block": "block.xyz",
    "brex": "brex.com", "bytedance": "bytedance.com", "cme group": "cmegroup.com",
    "celonis": "celonis.com", "chargepoint": "chargepoint.com", "checkr": "checkr.com",
    "ciena": "ciena.com", "clarivate": "clarivate.com", "cursor": "cursor.com",
    "dana-farber cancer institute": "dana-farber.org", "databricks": "databricks.com",
    "datadog": "datadoghq.com", "digitalocean": "digitalocean.com", "dropbox": "dropbox.com",
    "edwards lifesciences": "edwards.com", "enova": "enova.com", "experian": "experian.com",
    "flexport": "flexport.com", "globalfoundries": "gf.com", "guidewire": "guidewire.com",
    "h&r block": "hrblock.com", "harvard university": "harvard.edu", "instacart": "instacart.com",
    "iris software": "irissoftware.com", "legend biotech": "legendbiotech.com",
    "linkedin": "linkedin.com", "lyft": "lyft.com", "marqeta": "marqeta.com",
    "may mobility": "maymobility.com", "mongodb": "mongodb.com", "netcracker": "netcracker.com",
    "new relic": "newrelic.com", "northeastern university": "northeastern.edu", "notion": "notion.so",
    "nvidia": "nvidia.com", "okta": "okta.com", "palantir": "palantir.com",
    "people tech group": "peopletech.com", "pinterest": "pinterest.com", "pure storage": "purestorage.com",
    "q2": "q2.com", "ramp": "ramp.com", "replit": "replit.com", "robinhood": "robinhood.com",
    "rochester institute of technology": "rit.edu", "rockwell automation": "rockwellautomation.com",
    "s&p global": "spglobal.com", "salesforce": "salesforce.com", "samsara": "samsara.com",
    "samsung research america": "samsung.com", "saviynt": "saviynt.com", "scale ai": "scale.com",
    "servicenow": "servicenow.com", "snowflake": "snowflake.com", "sofi": "sofi.com",
    "softworld technologies": "softworldinc.com", "stripe": "stripe.com", "tech tammina": "tammina.com",
    "toast": "toasttab.com", "toyota motor north america": "toyota.com",
    "turner construction": "turnerconstruction.com", "twilio": "twilio.com", "uber": "uber.com",
    "university of south florida": "usf.edu", "university of washington": "washington.edu",
    "university of wisconsin system": "wisconsin.edu", "vanta": "vanta.com", "verkada": "verkada.com",
    "visa": "visa.com", "weride": "weride.ai", "worldquant": "worldquant.com", "yipitdata": "yipitdata.com",
}
_PALETTE = ["#0e8a5f", "#2c5bd6", "#b8730a", "#7c3aed", "#c0392b", "#0c7a8a", "#b03060", "#475569"]


@app.template_filter("logodomain")
def logodomain(name):
    key = (name or "").strip().lower()
    if key in _DOMAIN_MAP:
        return _DOMAIN_MAP[key]
    base = re.sub(r"[^a-z0-9]", "", key)        # join words -> best-effort guess
    return (base or "example") + ".com"


@app.template_filter("logocolor")
def logocolor(name):
    return _PALETTE[sum(ord(c) for c in (name or "x")) % len(_PALETTE)]


@app.template_global()
def static_v(filename):
    """Static URL with a ?v=<mtime> cache-buster: paired with the long immutable Cache-Control
    on /static/, a deploy (which changes the file mtime) invalidates the asset while unchanged
    files keep serving from the browser cache."""
    url = url_for("static", filename=filename)
    try:
        mt = int(os.path.getmtime(os.path.join(app.static_folder, filename)))
        return "%s?v=%s" % (url, mt)
    except Exception:
        return url


# ----------------------------- auth -----------------------------
# In-memory per-username throttle. Bounds online brute-force AND the CPU cost of PBKDF2
# (each guess against a real user runs a 200k-iteration hash). Keyed by username so a
# shared proxy IP can't lock everyone out; self-heals as old failures age past the window.
# Only real usernames get tracked (a failed lookup is cheap and not counted), so the dict
# can't be grown without bound by spraying random names.
_login_fails = {}
_LOGIN_WINDOW = 600        # seconds to remember a failed attempt
_LOGIN_MAX = 12            # failures within the window before we make them wait


def _safe_next(target):
    """Only follow a same-site relative ?next= path — never an absolute/scheme-relative URL,
    so a crafted login link can't open-redirect the user to a phishing site after sign-in."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return None


def _too_many_logins(username):
    hist = _login_fails.get(username)
    if not hist:
        return False
    now = time.time()
    q = [t for t in hist if now - t < _LOGIN_WINDOW]
    if q:
        _login_fails[username] = q
    else:
        _login_fails.pop(username, None)
    return len(q) >= _LOGIN_MAX


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user"):
        return redirect(url_for("feed"))
    if request.method == "POST":
        u = (request.form.get("username") or "").strip()
        p = request.form.get("password") or ""
        if u and _too_many_logins(u):              # short-circuit BEFORE the expensive hash
            flash("Too many sign-in attempts. Please wait a few minutes and try again.")
            return render_template("login.html")
        rec = None
        try:
            rec = db.get_user(u)
        except Exception:
            flash("Couldn't reach the database. Try again.")
        if rec and auth.verify_password(p, rec.get("password_hash", "")):
            _login_fails.pop(u, None)              # clear on success
            session.permanent = True
            session["user"] = u                    # résumé stays OUT of the cookie (size cap)
            _resume_cache[u] = (rec.get("resume", "") or "", time.time())
            return redirect(_safe_next(request.args.get("next")) or url_for("feed"))
        if rec is not None:
            _login_fails.setdefault(u, []).append(time.time())
            flash("Wrong username or password.")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ----------------------------- feed -----------------------------
@app.route("/")
@login_required
def feed():
    """The jobs feed. Small corpus: ship EVERY job inline; app.js filters/sorts client-side
    (instant). Large corpus (> _FEED_INLINE_MAX): ship only the top-N by match score inline and
    let app.js fetch /api/feed for search/filter/paging over the full set — so the payload stays
    small at any scale. The switch is automatic + env-tunable; behaviour is unchanged below it."""
    user = session["user"]
    resume = current_profile()           # match against the WHOLE profile (résumés + stories)
    rows = ranked_rows(user, resume)     # full corpus, score-sorted, status-free, cached
    statuses = user_statuses(user)
    counts = {"liked": 0, "applied": 0, "hidden": 0}
    for stv in statuses.values():
        if stv in counts:
            counts[stv] += 1
    total = len(rows)
    # The user's saved search seeds the controls, so ten filters stop resetting every visit.
    # No résumé means no meaningful score, so the match floor drops to 0 regardless.
    prefs = _user_prefs(user)
    if not resume:
        prefs = dict(prefs, min=0)
    default_min = prefs["min"]
    paged = total > _FEED_INLINE_MAX
    inline = rows[:_FEED_TOPN] if paged else rows
    # Default ("Recommended") count so the header + Load-more are right without a first fetch.
    # Must apply the SAME filters the toolbar ships with, now that those come from prefs, or
    # the "N of M" on first paint disagrees with what the user actually sees.
    default_total = len(_filter_rows(rows, statuses, _prefs_as_params(prefs)))
    feed_rows = [dict(r, status=statuses.get(r["url"], "")) for r in inline]   # overlay status (copy)
    # Work-authorization nudge. visa_alert returns None unless something is actually close, so
    # a user with no dates entered — or with months of runway — sees nothing at all.
    try:
        vprof = db.get_profile(user) or {}
        vtl = core.visa_timeline(vprof)
        visa = core.visa_alert(vtl)
        visa_ctx = _visa_badge_context(vprof, vtl)
    except Exception:
        visa, visa_ctx = None, {}
    return render_template("feed.html", feed_rows=feed_rows, has_resume=bool(resume),
                           total=total, default_total=default_total, counts=counts,
                           default_min=default_min, paged=paged, scraping=False,
                           group_lead=_GROUP_LEAD,
                           metros=_feed_metros(rows), states=_feed_states(rows),
                           visa=visa, visa_ctx=visa_ctx, prefs=prefs)


def _page_args(p, default_limit=60):
    """(offset, limit) from the query string, clamped."""
    try:
        offset = max(0, int(p.get("offset") or 0))
    except Exception:
        offset = 0
    try:
        limit = min(120, max(1, int(p.get("limit") or default_limit)))
    except Exception:
        limit = default_limit
    return offset, limit


@app.route("/prefs", methods=["POST"])
@login_required
def save_prefs():
    """Save the current toolbar state as this user's default search — which also decides what
    goes into their email digest, so there is only one definition of "my search"."""
    from flask import jsonify
    user = session["user"]
    body = request.get_json(silent=True) or request.form.to_dict() or {}
    prefs = core.normalize_prefs(dict(_user_prefs(user), **body))
    ok, msg = db.save_profile(user, {"search_prefs": prefs})
    if not ok:
        return jsonify({"ok": False, "error": msg[:200]}), 200
    _rows_cache.clear()          # the first-paint count is derived from prefs
    return jsonify({"ok": True, "prefs": prefs, "note": msg})


@app.route("/api/feed")
@login_required
def api_feed():
    """Server-side search/filter/sort/paging over the FULL corpus, for the large-dataset feed.
    Mirrors app.js's client filters; returns a compact page of card rows in the same shape.

    Paging counts DISPLAY UNITS (a collapsed group is one unit), while `total` stays the number
    of matching JOBS so the header's "N of M jobs" keeps meaning jobs. `units` is what the
    Load-more button has to count against — the two differ whenever a group collapsed.
    """
    user = session["user"]
    resume = current_profile()
    rows = ranked_rows(user, resume)
    statuses = user_statuses(user)
    matched = _filter_rows(rows, statuses, request.args)
    if _grouping_on(request.args):
        units = _group_units(matched)
    else:
        units = [(r, st, 0, "") for (r, st) in matched]
    offset, limit = _page_args(request.args)
    page = units[offset:offset + limit]
    out_rows = [dict(r, status=st, group_more=more, group_key=gk)      # status on a copy
                for (r, st, more, gk) in page]
    return {"rows": out_rows, "total": len(matched), "units": len(units),
            "has_more": offset + limit < len(units)}


@app.route("/api/group")
@login_required
def api_group():
    """The postings a "+N more at <company>" tile hides, for expanding it in place.

    Same filters as /api/feed (the client resends them) narrowed to one (title, company) group,
    minus the leader cards already on screen, and paged — expanding Amazon's Operations Manager
    run must not ship 431 cards at once.
    """
    gk = request.args.get("gk") or ""
    if not gk:
        return {"rows": [], "total": 0, "has_more": False}
    user = session["user"]
    rows = ranked_rows(user, current_profile())
    statuses = user_statuses(user)
    members = [p for p in _filter_rows(rows, statuses, request.args) if _group_key(p[0]) == gk]
    # Recompute the leaders the same way the feed did, so expanding shows exactly the rows the
    # tile was standing in for — no repeats of what's already rendered, nothing skipped.
    if _grouping_on(request.args) and len(members) >= _GROUP_MIN:
        leaders = {m[0]["url"] for m in _pick_leaders(members, _GROUP_LEAD)}
        rest = [p for p in members if p[0]["url"] not in leaders]
    else:
        rest = members
    offset, limit = _page_args(request.args)
    page = rest[offset:offset + limit]
    return {"rows": [dict(r, status=st) for (r, st) in page],
            "total": len(rest), "has_more": offset + limit < len(rest)}


@app.route("/api/job")
@login_required
def api_job():
    """Full job detail for the slide-in panel: JD text + matched/missing skills."""
    url = request.args.get("url", "")
    job = next((j for j in get_jobs() if j.get("url") == url), None)
    if not job:
        return {"ok": False}, 404
    resume = current_profile()           # whole-profile match, consistent with the feed
    jd = db.get_job_jd(url) or ""        # feed rows omit JD text; fetch this one on demand
    meta = jd_meta({"url": url, "jd": jd}, core.load_idf())
    if resume and jd:
        score, have, missing = core.score_against(resume.lower(), meta["analyzed"])
    else:
        try:
            score = int(job.get("match_score") or 0)
        except Exception:
            score = 0
        have, missing = [], []
    sv, sreason = meta["sponsor_jd"]
    exp_y = meta["exp_years"]
    pending = bool((meta.get("analyzed") or {}).get("thin"))
    return {"ok": True, "title": job.get("title", ""), "company": job.get("company", ""),
            "location": job.get("location", ""), "date": (job.get("found_date") or "")[:10],
            "url": url, "sponsors_h1b": job.get("sponsors_h1b", ""),
            "score": 0 if pending else int(score or 0), "score_pending": pending,
            "sponsor_jd": sv, "sponsor_reason": sreason,
            "agency": core.is_agency(job.get("company", "")),
            "cap_exempt": core.is_cap_exempt(job.get("company", "")),
            "everify": core.is_everify(job.get("company", ""), _EVERIFY_INDEX),
            "exp_years": exp_y if exp_y is not None else "",
            "have": list(have)[:30], "missing": list(missing)[:30], "jd": jd[:7000]}


@app.route("/api/action", methods=["POST"])
@login_required
def api_action():
    """JSON like/hide/apply for the JS feed. Body: {url, status} (status '' clears)."""
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    status = data.get("status", "")
    if not url:
        return {"ok": False, "error": "no url"}, 400
    try:
        db.set_user_status(session["user"], url, status)
        _status_cache.pop(session["user"], None)     # reflect the change on the next feed render
        if status == "applied":
            _autolog_application(session["user"], url)
        return {"ok": True, "status": status}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500


@app.route("/reload")
@login_required
def reload_jobs():
    get_jobs(force=True)
    _score_cache.clear()
    _rows_cache.clear()
    _profile_cache.clear()
    _resume_cache.clear()
    _status_cache.clear()
    _sponsor_cache.clear()
    _jdmeta.clear()
    _jdmeta.update(core.load_jdmeta())       # re-pull the cron's latest precompute from disk
    core._reset_idf_cache()
    flash("Reloaded jobs from the database.")
    return redirect(url_for("feed"))


GH_REPO = os.environ.get("GH_REPO", "hadakunal909-debug/job-search-tool")
GH_WORKFLOW = os.environ.get("GH_WORKFLOW", "scrape.yml")


def _gh_token():
    """GitHub PAT from env or the .env file (so the button can trigger the Action)."""
    tok = os.environ.get("GH_TOKEN")
    if not tok and os.path.exists(".env"):
        try:
            for ln in open(".env", encoding="utf-8"):
                ln = ln.strip()
                if ln.startswith("GH_TOKEN") and "=" in ln:
                    tok = ln.split("=", 1)[1].strip().strip('"').strip("'")
                    break
        except Exception:
            pass
    return tok


def _trigger_github_action():
    """Fire workflow_dispatch so the scrape runs on GitHub's servers. Returns None if no
    token is set (caller falls back to a local scrape), else (ok, message)."""
    tok = _gh_token()
    if not tok:
        return None
    import requests
    try:
        r = requests.post(
            "https://api.github.com/repos/%s/actions/workflows/%s/dispatches" % (GH_REPO, GH_WORKFLOW),
            headers={"Authorization": "Bearer %s" % tok,
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
            data=json.dumps({"ref": "main"}), timeout=20)
        if r.status_code in (201, 204):
            return (True, "")
        return (False, "GitHub returned %s: %s" % (r.status_code, r.text[:160]))
    except Exception as e:
        return (False, str(e))


@app.route("/scrape", methods=["POST"])
@login_required
def scrape_now():
    """Trigger the scrape on GitHub Actions (workflow_dispatch) — runs on GitHub's servers.
    Needs GH_TOKEN in .env (a fine-grained PAT with Actions: read+write). Returns JSON so the
    feed page can start polling /api/scrape_status and draw the live progress bar."""
    gh = _trigger_github_action()
    if gh is None:
        return {"ok": False, "msg": "To enable this, add GH_TOKEN to .env (a GitHub token with "
                "Actions read+write). You can also run the scrape from the repo's Actions tab."}
    if not gh[0]:
        return {"ok": False, "msg": "Couldn't start the scrape — " + gh[1]}
    # Optimistic 'queued' status so the bar appears the instant you click — the scraper overwrites
    # it with real progress once the Action spins up on GitHub's servers.
    try:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        db.set_scrape_status({"phase": "queued", "done": 0, "total": 0, "found": 0,
                              "started_at": now, "run": now})
    except Exception:
        pass
    return {"ok": True, "msg": "Scrape started on GitHub Actions."}


@app.route("/api/scrape_status")
@login_required
def api_scrape_status():
    """Latest scrape progress (phase/done/total/found/started_at/updated_at/finished_at) for the
    in-page progress bar. The scraper + score_jobs write this row as they run."""
    return db.get_scrape_status() or {}


@app.route("/action", methods=["POST"])
@login_required
def action():
    url = request.form.get("url", "")
    status = request.form.get("status", "")          # liked|hidden|applied|'' (clear)
    try:
        db.set_user_status(session["user"], url, status)
        _status_cache.pop(session["user"], None)     # reflect the change on the next feed render
        if status == "applied":
            _autolog_application(session["user"], url)
    except Exception:
        flash("Couldn't save that action — try again.")
    return redirect(request.referrer or url_for("feed"))


# ----------------------------- résumé -----------------------------
@app.route("/resume", methods=["GET", "POST"])
@login_required
def resume():
    if request.method == "POST":
        txt = request.form.get("resume", "")
        try:
            db.set_user_resume(session["user"], txt)
            _resume_cache[session["user"]] = (txt, time.time())   # not the cookie (size cap)
            _score_cache.clear()
            flash("Saved. Your match scores now reflect this résumé.")
        except Exception:
            flash("Couldn't save — try again.")
        return redirect(url_for("resume"))
    return render_template("resume.html", resume=current_resume())


# ----------------------------- tailor (keyword gaps + optional AI) -----------------------------
def _ai_key_for(_user=None):
    """The AI key to use. Precedence: server ANTHROPIC_API_KEY (Claude — preferred when configured,
    since it's the deliberate server config and Gemini quotas run out), then a per-user Gemini key
    saved in this session (signed HttpOnly cookie, ~30 days), then GEMINI_API_KEY from the env. The
    key's prefix (sk-ant-… vs AIza…) selects the provider downstream in resume_brain/ai.py."""
    try:
        sess_key = session.get("gemini_key")
    except Exception:
        sess_key = None                                # called outside a request context (e.g. a test/script)
    return os.environ.get("ANTHROPIC_API_KEY") or sess_key or os.environ.get("GEMINI_API_KEY")


def _save_ai_key(key):
    session["gemini_key"] = key
    session.permanent = True       # ride the 30-day login cookie


@app.route("/tailor")
@login_required
def tailor():
    url = request.args.get("url", "")
    job = next((j for j in get_jobs() if j.get("url") == url), None)
    if not job:
        return redirect(url_for("feed"))
    resume = current_resume()
    jd = db.get_job_jd(url) or ""        # feed rows omit JD text; fetch this one on demand
    if resume and jd:
        score, have, missing = core.score_against(resume.lower(), jd_meta({"url": url, "jd": jd}, core.load_idf())["analyzed"])
    else:
        score, have, missing = job.get("match_score") or 0, [], []
    return render_template("tailor.html", job=job, score=int(score or 0),
                           have=have, missing=missing, has_resume=bool(resume),
                           tailored="", ai_err="", ai_ready=bool(_ai_key_for(session["user"])))


@app.route("/tailor/ai", methods=["POST"])
@login_required
def tailor_ai():
    """Rewrite the résumé for this job with Claude (truthful reorder/reword). Uses
    ANTHROPIC_API_KEY from the environment, or a key the user pastes (held in memory
    for the session only — never written to the DB or the cookie)."""
    user = session["user"]
    url = request.form.get("url", "")
    key_in = (request.form.get("api_key") or "").strip()
    if key_in:
        _save_ai_key(key_in)
    key = _ai_key_for(user)
    job = next((j for j in get_jobs() if j.get("url") == url), None)
    if not job:
        return redirect(url_for("feed"))
    resume = current_resume()
    jd = db.get_job_jd(url) or ""        # feed rows omit JD text; fetch this one on demand
    if resume and jd:
        score, have, missing = core.score_against(resume.lower(), jd_meta({"url": url, "jd": jd}, core.load_idf())["analyzed"])
    else:
        score, have, missing = job.get("match_score") or 0, [], []
    tailored, ai_err = "", ""
    if not resume:
        ai_err = "Add your résumé first in Resume Brain, then tailor it here."
    elif not jd:
        ai_err = "No job description stored for this role yet — open Apply to read it on the company site."
    elif not key:
        ai_err = "Paste a Google Gemini API key below (or set GEMINI_API_KEY on the server) to enable AI tailoring."
    else:
        try:
            tailored = core.tailor(resume, jd, key)        # Claude for sk-ant keys, else Gemini
        except Exception as e:
            ai_err = "AI tailoring failed: %s" % str(e)[:200]
    return render_template("tailor.html", job=job, score=int(score or 0), have=have,
                           missing=missing, has_resume=bool(resume),
                           tailored=tailored, ai_err=ai_err, ai_ready=bool(key))


@app.route("/api/tailor", methods=["POST"])
@login_required
def api_tailor():
    """JSON tailoring for the no-reload loading-bar flow. Body: {url, api_key?}."""
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    key_in = (data.get("api_key") or "").strip()
    if key_in:
        _save_ai_key(key_in)
    key = _ai_key_for(session["user"])
    job = next((j for j in get_jobs() if j.get("url") == url), None)
    if not job:
        return {"ok": False, "error": "Job not found."}, 404
    resume = current_resume()
    jd = job.get("jd", "") or ""
    if not resume:
        return {"ok": False, "error": "Add your résumé first in Resume Brain, then tailor it here."}
    if not jd:
        return {"ok": False, "error": "No job description is stored for this role yet — open Apply to read it on the company site."}
    if not key:
        return {"ok": False, "error": "Add a Google Gemini API key (the field below, or GEMINI_API_KEY on the server)."}
    try:
        return {"ok": True, "tailored": core.tailor(resume, jd, key)}   # Claude for sk-ant keys, else Gemini
    except Exception as e:
        return {"ok": False, "error": "Tailoring failed: %s" % str(e)[:250]}


# ----------------------------- Resume Brain (the tailoring brain) -----------------------------
def _job_for(url):
    return next((j for j in get_jobs() if j.get("url") == url), None)


def _csvf(s):
    return [x.strip() for x in (s or "").replace("\n", ",").split(",") if x.strip()]


def _render_brain(user, inputs, data):
    return render_template("brain_tailor.html", inputs=inputs, data=data,
                           have_resumes=bool(rb.list_resumes(user)),
                           have_key=bool(_ai_key_for(user)))


@app.route("/brain")
@login_required
def brain_home():
    """Resume Brain home — tailor a job; prefilled + auto-run when ?job=<url> from the feed."""
    user = session["user"]
    _ensure_resume_migrated(user)        # fold any legacy single résumé into the library
    inputs = {"company": "", "company_url": "", "job_url": "", "jd": ""}
    data = None
    job_url = request.args.get("job", "")
    if job_url:
        j = _job_for(job_url)
        if j:
            inputs["company"] = j.get("company", "")
            inputs["jd"] = db.get_job_jd(job_url) or ""     # feed rows omit JD; fetch on demand
            inputs["job_url"] = j.get("url", "") if (j.get("url") or "").startswith("http") else ""
            if inputs["jd"]:
                data = rb.run_tailor(user, jd_text=inputs["jd"], company_name=inputs["company"])
    return _render_brain(user, inputs, data)


@app.route("/brain/tailor", methods=["POST"])
@login_required
def brain_tailor():
    user = session["user"]
    inputs = {"company": (request.form.get("company") or "").strip(),
              "company_url": (request.form.get("company_url") or "").strip(),
              "job_url": (request.form.get("job_url") or "").strip(),
              "jd": (request.form.get("jd") or "").strip()}
    data = rb.run_tailor(user, jd_text=inputs["jd"], job_url=inputs["job_url"],
                         company_name=inputs["company"], company_url=inputs["company_url"],
                         force_research=(request.form.get("refresh") == "1"))
    return _render_brain(user, inputs, data)


@app.route("/brain/feedback", methods=["POST"])
@login_required
def brain_feedback():
    user = session["user"]
    jd_terms = _csvf(request.form.get("jd_terms"))
    story_ids = request.form.getlist("story_ids")
    rb.apply_feedback(user, jd_terms, story_ids, request.form.get("feedback", ""),
                      request.form.get("company", ""))
    _bust_profile(user)
    flash("Learned. The brain will weight these for similar jobs from now on.")
    return redirect(url_for("brain_home"))


@app.route("/brain/rewrite", methods=["POST"])
@login_required
def brain_rewrite():
    """OPTIONAL AI layer: re-derive the plan, then have Gemini write the finished résumé +
    cover letter. Reuses the app's existing Gemini key (session or GEMINI_API_KEY)."""
    user = session["user"]
    key_in = (request.form.get("api_key") or "").strip()
    if key_in:
        _save_ai_key(key_in)
    key = _ai_key_for(user)
    inputs = {"company": (request.form.get("company") or "").strip(),
              "company_url": (request.form.get("company_url") or "").strip(),
              "job_url": (request.form.get("job_url") or "").strip(),
              "jd": (request.form.get("jd") or "").strip()}
    if not key:
        flash("Add a Google Gemini API key to use AI rewrite (the field on the tailor page).")
        return redirect(url_for("brain_home"))
    data = rb.run_tailor(user, jd_text=inputs["jd"], job_url=inputs["job_url"],
                         company_name=inputs["company"], company_url=inputs["company_url"],
                         record=False)
    ctx = rb.build_rewrite_context(user, data, request.form.getlist("story_ids"))
    if not ctx:
        flash("Add a job description and a résumé first.")
        return redirect(url_for("brain_home"))
    out, err = None, ""
    try:
        out = rb_ai.rewrite(ctx, key)
    except Exception as e:
        err = str(e)[:250]
    return render_template("brain_rewrite.html", out=out, error=err, ctx=ctx, inputs=inputs)


def _docx_response(text, title, filename):
    try:
        body = rb_export.build_docx(text, title)
        return Response(body, mimetype=rb_export.DOCX_MIME,
                        headers={"Content-Disposition": 'attachment; filename="%s.docx"' % filename})
    except ImportError:
        return Response(text or "", mimetype="text/plain; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="%s.txt"' % filename})


@app.route("/brain/export/resume.docx", methods=["POST"])
@login_required
def brain_export_resume():
    return _docx_response(request.form.get("content", ""),
                          request.form.get("title", "Tailored Résumé"), "Tailored_Resume")


@app.route("/brain/export/cover.docx", methods=["POST"])
@login_required
def brain_export_cover():
    return _docx_response(request.form.get("content", ""), "", "Cover_Letter")


def _pdf_response(text, user, filename):
    """Tailored résumé as a LaTeX-compiled PDF (Calibri template via Tectonic). Header comes from
    the user's profile, or — if there's no profile yet — the name/contact lines the résumé text
    starts with. Degrades to .docx (then .txt) if compilation isn't available."""
    try:
        try:
            prof = db.get_profile(user) or {}
        except Exception:
            prof = {}
        body = rb_latex.build_pdf(text or "", prof)
        return Response(body, mimetype=rb_latex.PDF_MIME,
                        headers={"Content-Disposition": 'attachment; filename="%s.pdf"' % filename})
    except Exception:
        return _docx_response(text, "", filename)


@app.route("/brain/export/resume.pdf", methods=["POST"])
@login_required
def brain_export_resume_pdf():
    return _pdf_response(request.form.get("content", ""), session["user"], "Tailored_Resume")


@app.route("/brain/pdf_diag")
@login_required
def brain_pdf_diag():
    """Why does PDF export fall back to .txt? Reports (1) whether the auto-Tectonic code is deployed,
    (2) python-docx availability, (3) Tectonic resolution + version, (4) a tiny live compile with the
    real error. Open it logged-in and paste the JSON. _pdf_response swallows the error, this doesn't."""
    from flask import jsonify
    import platform, subprocess
    info = {"os": platform.system(),
            "code_has_autobootstrap": hasattr(rb_latex, "_bootstrap_tectonic"),
            "repo_root": getattr(rb_latex, "_REPO_ROOT", "?")}
    try:
        import docx  # noqa: F401
        info["python_docx"] = True
    except Exception:
        info["python_docx"] = False
    try:
        binp = rb_latex._tectonic_bin()               # triggers the auto-download on a fresh host
        info["tectonic_bin"] = binp
        try:
            v = subprocess.run([binp, "--version"], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=30)
            info["tectonic_runs"] = (v.returncode == 0)
            info["tectonic_version"] = (v.stdout or b"").decode("utf-8", "replace").strip()[:200]
        except Exception as e:
            info["tectonic_runs"] = False
            info["tectonic_run_error"] = str(e)[:300]
        try:
            pdf = rb_latex.build_pdf("Diag Test\n\nEXPERIENCE\n- compiled a tiny test resume", {})
            info["compile_ok"] = True
            info["pdf_bytes"] = len(pdf)
        except Exception as e:
            info["compile_ok"] = False
            info["compile_error"] = str(e)[:1200]
    except Exception as e:
        info["tectonic_resolve_error"] = str(e)[:500]
    return jsonify(info)


# ---- Teach: the knowledge base (per user) ----
@app.route("/brain/teach")
@login_required
def brain_teach():
    user = session["user"]
    _ensure_resume_migrated(user)
    return render_template("brain_teach.html", resumes=rb.list_resumes(user),
                           stories=rb.list_stories(user), lessons=rb.list_lessons(user))


@app.route("/brain/resume/save", methods=["POST"])
@login_required
def brain_resume_save():
    user = session["user"]
    rb.save_resume(user, {"id": request.form.get("id", ""),
                          "name": (request.form.get("name") or "Untitled résumé").strip(),
                          "content": request.form.get("content", "")})
    _bust_profile(user)
    flash("Résumé saved — your feed match scores now include it.")
    return redirect(url_for("brain_teach"))


@app.route("/brain/resume/delete", methods=["POST"])
@login_required
def brain_resume_delete():
    user = session["user"]
    rb.delete_resume(user, request.form.get("id", ""))
    _bust_profile(user)
    flash("Résumé deleted")
    return redirect(url_for("brain_teach"))


@app.route("/brain/story/save", methods=["POST"])
@login_required
def brain_story_save():
    user = session["user"]
    rb.save_story(user, {"id": request.form.get("id", ""),
                         "title": (request.form.get("title") or "Untitled story").strip(),
                         "tags": _csvf(request.form.get("tags")),
                         "skills": _csvf(request.form.get("skills")),
                         "text": request.form.get("text", "")})
    _bust_profile(user)
    flash("Story saved — it now counts toward your feed match scores.")
    return redirect(url_for("brain_teach"))


@app.route("/brain/story/delete", methods=["POST"])
@login_required
def brain_story_delete():
    user = session["user"]
    rb.delete_story(user, request.form.get("id", ""))
    _bust_profile(user)
    flash("Story deleted")
    return redirect(url_for("brain_teach"))


@app.route("/brain/lesson/save", methods=["POST"])
@login_required
def brain_lesson_save():
    user = session["user"]
    rb.save_lesson(user, {"text": (request.form.get("text") or "").strip(),
                          "triggers": _csvf(request.form.get("triggers")),
                          "boost_story_ids": [], "boost_terms": _csvf(request.form.get("boost_terms")),
                          "weight": 1.0, "source": "manual"})
    flash("Lesson saved.")
    return redirect(url_for("brain_teach"))


@app.route("/brain/lesson/delete", methods=["POST"])
@login_required
def brain_lesson_delete():
    user = session["user"]
    rb.delete_lesson(user, request.form.get("id", ""))
    flash("Lesson deleted")
    return redirect(url_for("brain_teach"))


@app.route("/brain/companies")
@login_required
def brain_companies():
    c = db.list_brain_companies()
    items = sorted(c.values(), key=lambda x: x.get("fetched_at", ""), reverse=True)
    return render_template("brain_companies.html", companies=items)


@app.route("/brain/jobs.json")
@login_required
def brain_jobs_json():
    """Job search for the in-Brain picker — up to 20 matches (with a stored JD) by title/company."""
    q = (request.args.get("q") or "").strip().lower()
    out = []
    if q:
        have_jd = db.urls_with_jd()      # which jobs have a stored description (feed rows omit it)
        for j in get_jobs():
            hay = (j.get("title", "") + " " + j.get("company", "")).lower()
            if q in hay and j.get("url") in have_jd:
                out.append({"url": j.get("url", ""), "title": j.get("title", ""),
                            "company": j.get("company", "")})
                if len(out) >= 20:
                    break
    return {"jobs": out}


# ----------------------------- sponsor careers -----------------------------
def _md_to_html(md):
    """Tiny Markdown -> HTML (headings, list items, [text](url) links). Avoids a dep."""
    out = []
    for ln in md.splitlines():
        ln = ln.rstrip()
        esc = html.escape(ln)
        esc = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)",
                     r'<a href="\2" target="_blank" rel="noopener">\1</a>', esc)
        if ln.startswith("# "):
            out.append("<h2>%s</h2>" % esc[2:])
        elif ln.startswith("## "):
            out.append("<h3>%s</h3>" % esc[3:])
        elif ln.startswith("- "):
            out.append("<li>%s</li>" % esc[2:])
        elif not ln:
            out.append("<br>")
        else:
            out.append("<p>%s</p>" % esc)
    return "\n".join(out)


@app.route("/careers")
@login_required
def careers():
    md = ""
    if os.path.exists("careers_us.md"):
        md = open("careers_us.md", encoding="utf-8").read()
    q = (request.args.get("q") or "").strip().lower()
    if md and q:
        md = "\n".join(l for l in md.splitlines()
                       if (not l.startswith("- ")) or q in l.lower())
    return render_template("careers.html", body=_md_to_html(md) if md else "",
                           q=request.args.get("q", ""))


# ----------------------------- add a board -----------------------------
BOARDS_SQL = ("create table if not exists public.boards (\n"
              "  url text primary key, ats_type text not null, company text,\n"
              "  added_by text, created_at timestamptz default now());")


@app.route("/add", methods=["GET", "POST"])
@login_required
def add_board():
    import scraper
    result = None
    if request.method == "POST":
        url = (request.form.get("url") or "").strip()
        name = (request.form.get("name") or "").strip()
        if url:
            det = (scraper.detect_board(url) or scraper.detect_jibe(url)
                   or scraper.detect_phenom(url) or scraper.detect_successfactors(url)
                   or scraper.detect_linked_ats(url) or scraper.detect_jsonld(url))
            if not det:
                result = ("err", "That isn't a readable job board (Greenhouse, Lever, Ashby, "
                          "SmartRecruiters, Workday, Oracle Cloud, Workable, Phenom, iCIMS/Jibe, "
                          "SuccessFactors, UltiPro/UKG, BambooHR, Pinpoint, Rippling, "
                          "Recruitee, Breezy, Personio, or a page with embedded job data). "
                          "Add the company to sponsors.txt instead.")
            elif det[0] in {u for u, _, _ in scraper.SOURCES}:
                result = ("info", "%s is already a built-in source — nothing to add." % det[2])
            else:
                burl, ats, guess = det
                n = scraper.probe_board(burl, ats)
                if n is None:
                    result = ("err", "Detected a %s board but couldn't read any postings." % ats)
                else:
                    ok, msg = db.add_board(burl, ats, name or guess, added_by=session["user"])
                    if ok:
                        result = ("ok", "Added %s (%s, ~%s postings). It joins the next scrape."
                                  % (name or guess, ats, n))
                    elif "boards" in msg.lower() or "does not exist" in msg or "42P01" in msg:
                        result = ("sql", msg)
                    else:
                        result = ("err", msg)
    boards = []
    try:
        boards = db.list_boards()
    except Exception:
        boards = []
    return render_template("addboard.html", result=result, boards=boards, sql=BOARDS_SQL)


@app.route("/board/delete", methods=["POST"])
@login_required
def board_delete():
    try:
        db.delete_board(request.form.get("url", ""))
    except Exception:
        pass
    return redirect(url_for("add_board"))


# ----------------------------- application tracker -----------------------------
APP_STATUSES = ["saved", "applied", "assessment", "interview", "offer", "rejected"]


def _default_resume(user):
    """The user's default résumé name (a file label, e.g. 'Kunal_PM_Resume.pdf')."""
    try:
        return (db.get_profile(user) or {}).get("default_resume", "") or ""
    except Exception:
        return ""


def _autolog_application(user, url):
    """When a feed job is marked 'applied', auto-fill a tracker row (deduped by url) with
    your default résumé name. Never raises."""
    try:
        if not url or db.find_application_by_url(user, url):
            return
        job = next((j for j in get_jobs() if j.get("url") == url), None)
        if not job:
            return
        import datetime
        db.save_application(user, {
            "company": job.get("company", ""), "title": job.get("title", ""), "url": url,
            "status": "applied", "applied_date": datetime.date.today().isoformat(),
            "resume_name": _default_resume(user), "notes": ""})
    except Exception:
        pass


@app.route("/applications")
@login_required
def applications():
    user = session["user"]
    try:
        apps = db.list_applications(user)
    except Exception:
        apps = []
    import datetime
    cut = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
    counts, week = {}, 0
    for a in apps:
        s = a.get("status", "") or ""
        counts[s] = counts.get(s, 0) + 1
        if (a.get("applied_date") or "")[:10] >= cut and s not in ("saved", "rejected"):
            week += 1
    default_resume = _default_resume(user)
    resume_names = sorted({a.get("resume_name", "") for a in apps if a.get("resume_name")}
                          | ({default_resume} if default_resume else set()))
    return render_template("applications.html", apps=apps, statuses=APP_STATUSES,
                           counts=counts, total=len(apps), week=week, sql=db.APPLICATIONS_SQL,
                           resume_names=resume_names, default_resume=default_resume)


@app.route("/application/save", methods=["POST"])
@login_required
def application_save():
    f = request.form
    rec = {"id": f.get("id", "").strip(), "company": f.get("company", "").strip(),
           "title": f.get("title", "").strip(), "url": f.get("url", "").strip(),
           "status": f.get("status", "applied"), "applied_date": f.get("applied_date", "").strip(),
           "notes": f.get("notes", "").strip()}
    if not rec["company"] and not rec["title"]:
        flash("Add at least a company or a title.")
        return redirect(url_for("applications"))
    rec["resume_name"] = f.get("resume_name", "").strip()    # just the résumé file name you used
    ok, msg = db.save_application(session["user"], rec)
    if (not ok) and ("does not exist" in msg or "42P01" in msg or "could not find" in msg.lower()):
        flash("One-time setup needed — run the SQL at the bottom of this page in Supabase, then try again.")
    elif ok:
        flash("Saved.")
    else:
        flash("Couldn't save — " + msg[:120])
    return redirect(url_for("applications"))


@app.route("/application/delete", methods=["POST"])
@login_required
def application_delete():
    try:
        db.delete_application(session["user"], request.form.get("id", ""))
        flash("Deleted.")
    except Exception:
        flash("Couldn't delete — try again.")
    return redirect(url_for("applications"))


@app.route("/applications.csv")
@login_required
def applications_csv():
    import csv, io
    from flask import Response
    try:
        apps = db.list_applications(session["user"])
    except Exception:
        apps = []
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["company", "title", "status", "applied_date", "url", "notes"])
    for a in apps:
        w.writerow([a.get("company", ""), a.get("title", ""), a.get("status", ""),
                    a.get("applied_date", "") or "", a.get("url", ""), a.get("notes", "")])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=applications.csv"})


@app.route("/application/resume")
@login_required
def application_resume():
    from flask import Response
    aid = request.args.get("id", "")
    try:
        apps = db.list_applications(session["user"])
    except Exception:
        apps = []
    rec = next((a for a in apps if a.get("id") == aid), None)
    if not rec or not rec.get("resume_used"):
        flash("No saved résumé for that application.")
        return redirect(url_for("applications"))
    name = (rec.get("company") or "job").replace(" ", "_")
    return Response(rec["resume_used"], mimetype="text/plain",
                    headers={"Content-Disposition": "attachment; filename=resume-%s.txt" % name})


# ----------------------------- profile + Chrome-extension API -----------------------------
def _ext_token(username):
    """A stable per-user token for the browser extension (HMAC of the username with the
    app secret). No DB storage needed; we re-derive + compare to validate."""
    sig = hmac.new(str(app.secret_key).encode(), ("ext:" + username).encode(),
                   hashlib.sha256).hexdigest()[:32]
    return "%s:%s" % (username, sig)


def _ext_user(token):
    """Username for a valid extension token, else None."""
    token = (token or "").strip()
    if ":" not in token:
        return None
    username = token.rsplit(":", 1)[0]
    if username and hmac.compare_digest(_ext_token(username), token):
        return username
    return None


def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    user = session["user"]
    if request.method == "POST":
        f = request.form
        text_keys = (
            "name", "email", "phone", "location", "linkedin",
            "work_authorized", "needs_sponsorship", "default_resume", "notes",
            "first_name", "last_name", "pronouns",
            "address_line1", "address_line2", "city", "state", "postal_code", "country",
            "github", "portfolio", "website",
            "work_auth_status", "requires_sponsorship_now", "requires_sponsorship_future",
            "program_end_date", "opt_type", "opt_start_date", "opt_end_date",
            "stem_eligible", "unemployment_days_used",
            "gender", "race_ethnicity", "hispanic_latino", "veteran_status", "disability_status",
            "desired_salary", "salary_currency", "available_start_date",
            "willing_to_relocate", "how_did_you_hear",
        )
        payload = {k: f.get(k, "").strip() for k in text_keys}
        for jk in ("extra", "application_defaults"):    # optional advanced JSON blobs
            raw = (f.get(jk) or "").strip()
            if raw:
                payload[jk] = raw                       # save_profile validates/parses
        # The two alert settings live INSIDE search_prefs (they're part of "my search", and the
        # digest reads one object), so merge them into the saved prefs rather than adding columns.
        if "alerts" in f or "alert_min" in f:
            payload["search_prefs"] = core.normalize_prefs(dict(
                _user_prefs(user),
                alerts=f.get("alerts", ""), alert_min=f.get("alert_min", "") or 0))
        ok, msg = db.save_profile(user, payload)
        flash("Saved." if ok else ("Couldn't save — " + msg[:120]))
        return redirect(url_for("profile"))
    try:
        prof = db.get_profile(user) or {}
    except Exception:
        prof = {}
    return render_template("profile.html", prof=prof, token=_ext_token(user),
                           timeline=core.visa_timeline(prof),
                           prefs=core.normalize_prefs(prof.get("search_prefs")))


@app.route("/api/ext/save", methods=["POST", "OPTIONS"])
def ext_save():
    """Extension -> log a job to the tracker. Token-authenticated; CORS-open (the token
    is the secret). Deduped by URL."""
    import scraper
    from flask import jsonify
    if request.method == "OPTIONS":
        return _cors(app.make_response(("", 204)))
    data = request.get_json(silent=True) or {}
    user = _ext_user(data.get("token", ""))
    if not user:
        return _cors(jsonify({"ok": False, "error": "Invalid token"})), 401
    title = (data.get("title") or "").strip()[:300]
    company = (data.get("company") or "").strip()[:200]
    url = (data.get("url") or "").strip()[:1000]
    if url and not scraper.is_http_url(url):       # never store a javascript:/data: link
        url = ""
    if not (title or company):
        return _cors(jsonify({"ok": False, "error": "No job info"})), 400
    try:
        if url and db.find_application_by_url(user, url):
            return _cors(jsonify({"ok": True, "dup": True}))
        import datetime
        rname = (data.get("resume_name") or "").strip() or _default_resume(user)
        db.save_application(user, {"company": company, "title": title, "url": url,
            "status": "applied", "applied_date": datetime.date.today().isoformat(),
            "resume_name": rname, "notes": ""})
        return _cors(jsonify({"ok": True}))
    except Exception as e:
        return _cors(jsonify({"ok": False, "error": str(e)[:160]})), 500


@app.route("/api/ext/profile", methods=["GET", "OPTIONS"])
def ext_profile():
    """Extension -> the user's profile fields for autofill. Token-authenticated."""
    from flask import jsonify
    if request.method == "OPTIONS":
        return _cors(app.make_response(("", 204)))
    user = _ext_user(request.args.get("token", ""))
    if not user:
        return _cors(jsonify({"ok": False, "error": "Invalid token"})), 401
    try:
        p = db.get_profile(user) or {}
    except Exception:
        p = {}
    default_resume = p.get("default_resume") or ""
    try:
        names = sorted({a.get("resume_name", "") for a in db.list_applications(user) if a.get("resume_name")}
                       | ({default_resume} if default_resume else set()))
    except Exception:
        names = [default_resume] if default_resume else []
    keys = ("name", "email", "phone", "location", "linkedin", "work_authorized", "needs_sponsorship")
    return _cors(jsonify({"ok": True, "profile": {k: (p.get(k) or "") for k in keys},
                          "default_resume": default_resume, "resume_names": names}))


def _ext_profile_fields(user, p=None):
    """Normalized profile map the form-filler consumes (stable nested shape, NOT raw columns).
    Pass `p` to reuse an already-loaded profile dict and avoid a second DB read."""
    if p is None:
        try:
            p = db.get_profile(user) or {}
        except Exception:
            p = {}

    def g(k):
        v = p.get(k)
        return ("" if v is None else str(v)).strip()

    def truthy(k):
        return g(k).lower() in ("yes", "true", "1", "y")

    first, last = g("first_name"), g("last_name")
    full = (first + " " + last).strip() or g("name")
    fields = {
        "first_name": first, "last_name": last, "full_name": full,
        "email": g("email"), "phone": g("phone"), "pronouns": g("pronouns"),
        "address": {"line1": g("address_line1"), "line2": g("address_line2"),
                    "city": g("city"), "state": g("state"), "postal": g("postal_code"),
                    "country": g("country"), "location": g("location")},
        "links": {"linkedin": g("linkedin"), "github": g("github"),
                  "portfolio": g("portfolio"), "website": g("website")},
        "work_auth": {"authorized": truthy("work_authorized"),
                      # "Will you NOW OR IN THE FUTURE require sponsorship?" — true if either now or
                      # future (an OPT/F-1 candidate who'll need H-1B later must answer Yes).
                      "requires_sponsorship": (truthy("requires_sponsorship_now")
                                               or truthy("requires_sponsorship_future")
                                               or truthy("needs_sponsorship")),
                      "requires_sponsorship_future": truthy("requires_sponsorship_future"),
                      "status_label": g("work_auth_status")},
        "eeo": {"gender": g("gender"), "race": g("race_ethnicity"),
                "hispanic_latino": g("hispanic_latino"), "veteran": g("veteran_status"),
                "disability": g("disability_status")},
        "comp": {"desired_salary": g("desired_salary"), "currency": g("salary_currency")},
        "start_date": g("available_start_date"), "relocate": truthy("willing_to_relocate"),
        "how_did_you_hear": g("how_did_you_hear"),
    }
    defaults = p.get("application_defaults")
    return {"fields": fields, "defaults": defaults if isinstance(defaults, dict) else {},
            "default_resume": g("default_resume")}


@app.route("/api/ext/profile_fields", methods=["GET", "OPTIONS"])
def ext_profile_fields():
    """Extension -> normalized profile field map + résumé text + learned-answer bank, so the
    extension can build prompts and match the user's own past answers CLIENT-SIDE (hybrid mode:
    the extension calls Claude directly). Token-authenticated."""
    from flask import jsonify
    if request.method == "OPTIONS":
        return _cors(app.make_response(("", 204)))
    user = _ext_user(request.args.get("token", ""))
    if not user:
        return _cors(jsonify({"ok": False, "error": "Invalid token"})), 401
    try:
        resume = (db.profile_text(user) or "")[:6000]
    except Exception:
        resume = ""
    try:
        learned = db.get_learned(user)
    except Exception:
        learned = {}
    return _cors(jsonify({"ok": True, "resume": resume, "learned": learned, **_ext_profile_fields(user)}))


@app.route("/api/ext/tailor", methods=["POST", "OPTIONS"])
def ext_tailor():
    """Extension -> tailor the résumé to a JD (Resume Brain + optional Gemini), compile it to a
    PDF via LaTeX/Tectonic, and return the file (base64) + the profile field map for autofill.
    Cached per (user, job, format, résumé-corpus) so retriggers are instant. Token-authenticated."""
    from flask import jsonify
    import base64, hashlib, re
    if request.method == "OPTIONS":
        return _cors(app.make_response(("", 204)))
    data = request.get_json(silent=True) or {}
    user = _ext_user(data.get("token", ""))
    if not user:
        return _cors(jsonify({"ok": False, "error": "Invalid token"})), 401

    job_url = (data.get("job_url") or "").strip()
    company = (data.get("company") or "").strip()
    company_url = (data.get("company_url") or "").strip()
    fmt = (data.get("format") or "pdf").strip().lower()
    if fmt not in ("pdf", "docx", "text"):
        fmt = "pdf"
    force = bool(data.get("force"))
    jd = (data.get("jd_text") or "").strip() or (db.get_job_jd(job_url) or "")

    raw = {}
    try:
        raw = db.get_profile(user) or {}
    except Exception:
        raw = {}
    prof = _ext_profile_fields(user, raw)

    # Cache key: busts when the user's résumé corpus changes; `force` bypasses it.
    try:
        corpus = db.profile_text(user) or ""
    except Exception:
        corpus = ""
    jd_part = job_url or hashlib.sha256(jd.encode("utf-8", "ignore")).hexdigest()[:16]
    corpus_h = hashlib.sha256(corpus.encode("utf-8", "ignore")).hexdigest()[:16]
    prof_h = hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode("utf-8", "ignore")).hexdigest()[:16]
    cache_key = hashlib.sha256("|".join([user, jd_part, fmt, corpus_h, prof_h]).encode()).hexdigest()
    if not force:
        cached = db.get_tailored(cache_key)
        if cached:
            cached = dict(cached); cached["cached"] = True
            return _cors(jsonify({"ok": True, **cached}))

    # Deterministic plan (cheap); Gemini rewrite (expensive) only if a key exists.
    try:
        plan = rb.run_tailor(user, jd_text=jd, job_url=job_url, company_name=company,
                             company_url=company_url, record=False)
    except Exception as e:
        return _cors(jsonify({"ok": False, "error": "tailor failed: %s" % str(e)[:160]})), 500
    result = (plan or {}).get("result") or {}
    best = result.get("best_resume")
    base_resume_text = ((best or {}).get("resume") or {}).get("content", "") if best else ""

    out_resume, out_cover, notes, ai_used = "", "", [], False
    key = _ai_key_for(user)
    if key:
        try:
            ctx = rb.build_rewrite_context(user, plan, None)
            if ctx:
                rw = rb_ai.rewrite(ctx, key)
                out_resume = (rw.get("tailored_resume") or "").strip()
                out_cover = (rw.get("cover_letter") or "").strip()
                notes = rw.get("notes") or []
                ai_used = bool(out_resume)
        except Exception as e:
            notes = ["AI rewrite failed, using base résumé: %s" % str(e)[:120]]
    if not out_resume:
        out_resume = base_resume_text
    if not out_resume.strip():
        return _cors(jsonify({"ok": False,
                              "error": "No résumé found — add one in Resume Brain first."})), 400

    # Build the file: pdf (LaTeX/Tectonic) -> docx -> text, degrading gracefully.
    full_name = prof["fields"]["full_name"] or user
    last = (raw.get("last_name") or "").strip() or (full_name.split()[-1] if full_name else "Resume")
    safe = lambda s: re.sub(r"[^A-Za-z0-9]+", "", s or "") or "Application"
    ext = {"pdf": "pdf", "docx": "docx", "text": "txt"}[fmt]
    compiled = False
    try:
        if fmt == "pdf":
            body = rb_latex.build_pdf(out_resume, raw)
            mime = rb_latex.PDF_MIME
            compiled = True
        elif fmt == "docx":
            body = rb_export.build_docx(out_resume, full_name)
            mime = rb_export.DOCX_MIME
        else:
            body = out_resume.encode("utf-8")
            mime = "text/plain"
    except Exception as e:
        try:
            body = rb_export.build_docx(out_resume, full_name)
            mime = rb_export.DOCX_MIME
            ext = "docx"
            notes = list(notes) + ["PDF compile unavailable, used .docx: %s" % str(e)[:100]]
        except Exception as e2:
            body = out_resume.encode("utf-8")
            mime = "text/plain"
            ext = "txt"
            notes = list(notes) + ["PDF/.docx unavailable, used .txt: %s" % str(e2)[:80]]

    file_name = "%s_%s_Resume.%s" % (safe(last), safe(company)[:24], ext)
    payload = {
        "tailored_resume": out_resume, "cover_letter": out_cover, "notes": notes,
        "file": {"name": file_name, "mime": mime,
                 "b64": base64.b64encode(body).decode("ascii")},
        "fields": prof["fields"], "defaults": prof["defaults"],
        "default_resume": prof["default_resume"],
        "ai_used": ai_used, "compiled": compiled, "cached": False,
    }
    try:
        db.put_tailored(cache_key, payload, username=user)
    except Exception:
        pass
    return _cors(jsonify({"ok": True, **payload}))


# Every ATS the scraper feeds, so the apply queue covers all our boards (keep in sync with
# scraper/__init__.py detect_board + popup.js applyAts). greenhouse/lever/ashby/smartrecruiters have
# tuned filler.js adapters; the rest rely on the GENERIC adapter (best-effort). Login/account-walled
# ones (workday/oracle) are included by request — you review each open tab and sign in if needed.
_FILLABLE_HOSTS = (
    "greenhouse.io", "lever.co", "ashbyhq.com", "smartrecruiters.com",
    "recruitee.com", "breezy.hr", "personio.com", "workable.com",
    "ultipro.com", "bamboohr.com", "pinpointhq.com", "rippling.com",
    "avature.net", "jobdiva.com", "myworkdayjobs.com", "myworkdaysite.com",
    "oraclecloud.com", "jibeapply.com",
)


@app.route("/api/ext/apply_queue", methods=["GET", "OPTIONS"])
def ext_apply_queue():
    """Extension batch runner -> the user's auto-apply queue: unapplied jobs on a supported ATS
    (Greenhouse/Lever/Ashby/SmartRecruiters), ordered NEWEST-FIRST by real posting date so the runner
    works the latest postings. Auto-sourced so it needs no manual URLs. ?limit= caps the count (default 50)."""
    from flask import jsonify
    from urllib.parse import urlparse
    if request.method == "OPTIONS":
        return _cors(app.make_response(("", 204)))
    user = _ext_user(request.args.get("token", ""))
    if not user:
        return _cors(jsonify({"ok": False, "error": "Invalid token"})), 401
    try:
        st = db.get_user_statuses(user) or {}
    except Exception:
        st = {}
    applied = {u for u, s in st.items() if s == "applied"}
    liked = {u for u, s in st.items() if s == "liked"}

    def supported(u):
        try:
            h = (urlparse(u).hostname or "").lower()
        except Exception:
            return False
        return any(host in h for host in _FILLABLE_HOSTS)

    def score(j):
        try:
            return int(j.get("match_score") or 0)
        except Exception:
            return 0

    rows = []
    try:
        for j in get_jobs():
            u = j.get("url")
            if u and u not in applied and supported(u):
                rows.append(j)
    except Exception:
        pass
    # Newest postings first: real verified posting date when known, else when we first found it
    # (same recency expression the feed uses). ISO YYYY-MM-DD strings sort lexically.
    rows.sort(key=lambda j: ((j.get("posted_verified") or j.get("found_date") or "")[:10]), reverse=True)
    try:
        limit = max(1, min(int(request.args.get("limit", 50)), 200))
    except Exception:
        limit = 50
    jobs = [{"url": j.get("url"), "title": j.get("title", ""), "company": j.get("company", ""),
             "score": score(j), "liked": j.get("url") in liked} for j in rows[:limit]]
    return _cors(jsonify({"ok": True, "jobs": jobs, "count": len(jobs)}))


@app.route("/api/ext/answer", methods=["POST", "OPTIONS"])
def ext_answer():
    """Extension form-filler -> AI maps the user's profile + résumé onto a batch of still-empty
    form fields. Body: {token, fields:[{key,label,type,options}], company}. Returns
    {ok, answers:{key:value}}. Needs a Gemini key (session or server GEMINI_API_KEY)."""
    from flask import jsonify
    if request.method == "OPTIONS":
        return _cors(app.make_response(("", 204)))
    data = request.get_json(silent=True) or {}
    user = _ext_user(data.get("token", ""))
    if not user:
        return _cors(jsonify({"ok": False, "error": "Invalid token"})), 401
    fields = data.get("fields") or []
    if not fields:
        return _cors(jsonify({"ok": True, "answers": {}}))
    try:
        prof = db.get_profile(user) or {}
    except Exception:
        prof = {}

    # 1) LEARNED ANSWERS FIRST: the user's own past answers beat an AI guess. For option fields we
    #    only reuse a learned value that still matches one of the offered options. This also lets the
    #    bank work with NO AI key (only the genuinely-new questions need Gemini).
    try:
        learned = db.get_learned(user)
    except Exception:
        learned = {}
    answers, unknown, used_learned = {}, [], 0
    for f in fields:
        fk = f.get("key")
        lk = db.normalize_label(f.get("label") or "")
        rec = learned.get(lk) if lk else None
        val = (rec or {}).get("value") if rec else None
        if val:
            opts = f.get("options") or []
            if opts:
                vl = str(val).lower().strip()
                if any(vl == str(o).lower().strip() or vl in str(o).lower() or str(o).lower().strip() in vl
                       for o in opts):
                    answers[fk] = val; used_learned += 1; continue
            else:
                answers[fk] = val; used_learned += 1; continue
        unknown.append(f)

    # 2) AI only for what the bank didn't cover.
    key = _ai_key_for(user)
    no_key = False
    if unknown and key:
        try:
            resume = db.profile_text(user) or ""
            answers.update(rb_ai.answer_fields(prof, resume, unknown, key, company=(data.get("company") or "")))
        except Exception as e:
            return _cors(jsonify({"ok": False, "error": str(e)[:160], "answers": answers, "learned": used_learned}))
    elif unknown and not key:
        no_key = True                                    # return learned answers anyway; AI just unavailable
    return _cors(jsonify({"ok": True, "answers": answers, "learned": used_learned, "no_ai_key": no_key}))


@app.route("/api/ext/learn", methods=["POST", "OPTIONS"])
def ext_learn():
    """'Train' the auto-apply: save how the USER answered a form's fields (captured from a page they
    filled) so future fills prefer their real answer over an AI guess. Body:
    {token, company, fields:[{label,type,value,options}]}. Returns {ok, saved:int}."""
    from flask import jsonify
    if request.method == "OPTIONS":
        return _cors(app.make_response(("", 204)))
    data = request.get_json(silent=True) or {}
    user = _ext_user(data.get("token", ""))
    if not user:
        return _cors(jsonify({"ok": False, "error": "Invalid token"})), 401
    items = data.get("fields") or []
    company = (data.get("company") or "").strip()[:120]
    if company:
        for it in items:
            if isinstance(it, dict):
                it.setdefault("company", company)
    try:
        saved = db.save_learned(user, items)
    except Exception as e:
        return _cors(jsonify({"ok": False, "error": str(e)[:160], "saved": 0}))
    return _cors(jsonify({"ok": True, "saved": saved}))


@app.route("/api/ext/learned", methods=["GET", "OPTIONS"])
def ext_learned_list():
    """Extension -> list the user's learned-answer bank for the 'manage learned answers' UI.
    Returns {ok, items:[{key,label,value,company,count}]} sorted by most-used."""
    from flask import jsonify
    if request.method == "OPTIONS":
        return _cors(app.make_response(("", 204)))
    user = _ext_user(request.args.get("token", ""))
    if not user:
        return _cors(jsonify({"ok": False, "error": "Invalid token"})), 401
    try:
        bank = db.get_learned(user)
    except Exception:
        bank = {}
    items = [{"key": k, "label": (v.get("label") or k), "value": v.get("value", ""),
              "company": v.get("company", ""), "count": v.get("count", 0)} for k, v in bank.items()]
    items.sort(key=lambda x: (-int(x.get("count") or 0), x["label"].lower()))
    return _cors(jsonify({"ok": True, "items": items}))


@app.route("/api/ext/learn_delete", methods=["POST", "OPTIONS"])
def ext_learn_delete():
    """Extension -> delete one learned answer (by normalized key) from the user's bank."""
    from flask import jsonify
    if request.method == "OPTIONS":
        return _cors(app.make_response(("", 204)))
    data = request.get_json(silent=True) or {}
    user = _ext_user(data.get("token", ""))
    if not user:
        return _cors(jsonify({"ok": False, "error": "Invalid token"})), 401
    ok = False
    try:
        ok = db.delete_learned(user, (data.get("key") or "").strip())
    except Exception as e:
        return _cors(jsonify({"ok": False, "error": str(e)[:160]}))
    return _cors(jsonify({"ok": bool(ok)}))


@app.route("/api/ext/vision", methods=["POST", "OPTIONS"])
def ext_vision():
    """VISION FALLBACK form-filler -> the model sees a SCREENSHOT of the page plus enumerated
    interactive elements and returns a fill plan. Used only when the normal deterministic+answer
    pass parks. Body: {token, elements:[{index,label,type,options,value}], screenshot:<base64 png>,
    company}. Returns {ok, plan:{sets:[{index,value}], submit_index}}. Needs a Gemini key."""
    from flask import jsonify
    if request.method == "OPTIONS":
        return _cors(app.make_response(("", 204)))
    data = request.get_json(silent=True) or {}
    user = _ext_user(data.get("token", ""))
    if not user:
        return _cors(jsonify({"ok": False, "error": "Invalid token"})), 401
    elements = data.get("elements") or []
    shot = (data.get("screenshot") or "").strip()
    if shot.startswith("data:"):                         # strip a data:image/png;base64, prefix
        shot = shot.split(",", 1)[-1]
    if not elements:
        return _cors(jsonify({"ok": True, "plan": {"sets": [], "submit_index": None}}))
    key = _ai_key_for(user)
    if not key:
        return _cors(jsonify({"ok": False, "error": "no_ai_key", "plan": {"sets": [], "submit_index": None}}))
    try:
        prof = db.get_profile(user) or {}
    except Exception:
        prof = {}
    try:
        resume = db.profile_text(user) or ""
    except Exception:
        resume = ""
    try:
        plan = rb_ai.vision_fill_plan(prof, resume, elements, shot, key, company=(data.get("company") or ""))
    except Exception as e:
        return _cors(jsonify({"ok": False, "error": str(e)[:160], "plan": {"sets": [], "submit_index": None}}))
    return _cors(jsonify({"ok": True, "plan": plan}))


@app.route("/api/ext/debug", methods=["POST", "OPTIONS"])
def ext_debug():
    """Extension -> capture a failing form's STRUCTURE (labels/types/options only — not the user's
    answers) so the filler can be improved without the user relaying errors. Appended to a local
    JSONL log. Token-authenticated."""
    from flask import jsonify
    if request.method == "OPTIONS":
        return _cors(app.make_response(("", 204)))
    data = request.get_json(silent=True) or {}
    user = _ext_user(data.get("token", ""))
    if not user:
        return _cors(jsonify({"ok": False})), 401
    rec = {"ts": db._now(), "user": user, "url": (data.get("url") or "")[:400],
           "company": (data.get("company") or "")[:120], "status": data.get("status", ""),
           "reason": (data.get("reason") or "")[:300], "ai": (data.get("ai") or "")[:80],
           "fields": (data.get("fields") or [])[:40]}
    try:
        with open("ext_debug_log.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec)[:9000] + "\n")
    except Exception:
        pass
    return _cors(jsonify({"ok": True}))


@app.route("/api/ext/bulk_jobs", methods=["POST", "OPTIONS"])
def ext_bulk_jobs():
    """Extension -> bulk-add postings READ FROM A PAGE in the user's own browser into the
    shared jobs feed. This is how we get jobs from sites that block server-side scraping
    (e.g. Tesla's Akamai bot-wall): the user's real, already-trusted browser can read the
    listings the page loaded, so the extension hands them to us. We apply the SAME
    title + US-location filter as the scraper, flag H1B sponsors, and de-dupe by URL —
    so a bulk import looks identical to a scraped board in the feed."""
    import scraper
    from flask import jsonify
    if request.method == "OPTIONS":
        return _cors(app.make_response(("", 204)))
    data = request.get_json(silent=True) or {}
    if not _ext_user(data.get("token", "")):
        return _cors(jsonify({"ok": False, "error": "Invalid token"})), 401
    jobs = data.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        return _cors(jsonify({"ok": False, "error": "No jobs in payload"})), 400

    try:
        names = scraper.load_sponsors()
        sidx = scraper.build_sponsor_index(names) if names else None
    except Exception:
        sidx = None
    try:
        # canonical on both sides — same reason as the scraper's dedupe (stored rows
        # predate normalization), so a re-import can't add a second row for one posting
        seen = {scraper.canonical_url(u) for u in db.existing_urls()}
    except Exception:
        seen = set()

    kept, scanned = [], 0
    # Tally WHY jobs were dropped — when an import adds 0, this is the diagnosis.
    dropped = {"dup": 0, "title": 0, "us": 0, "bad": 0}
    # Already-imported jobs that still have NO stored description: re-running an import
    # returns them as needs_jd so the extension can backfill their JDs (a first import
    # may have failed mid-fetch, or predates JD support).
    have_jd = db.urls_with_jd()          # feed rows omit JD text; ask the DB which have one
    # canonical form -> the url AS STORED. needs_jd must hand back the exact key the jobs
    # table uses: a canonical variant the DB doesn't hold would upsert a whole new row.
    no_jd = {scraper.canonical_url(u): u
             for u in (j.get("url") for j in get_jobs()) if u and u not in have_jd}
    needs_jd = []
    for j in jobs[:2000]:
        if not isinstance(j, dict):
            continue
        scanned += 1
        url = (j.get("url") or "").strip()[:1000]
        title = (j.get("title") or "").strip()[:300]
        if not url or not title:
            dropped["bad"] += 1
            continue
        url = scraper.canonical_url(url)     # one posting -> one row (non-http passes through
                                             # untouched and is rejected just below)
        if url in seen:
            dropped["dup"] += 1
            if url in no_jd and len(needs_jd) < 25:
                needs_jd.append(no_jd[url])
            continue
        if not scraper.is_http_url(url):                        # block javascript:/data: URLs —
            dropped["bad"] += 1                                 # these get rendered as <a href> for everyone
            continue
        if not scraper.title_verdict(title)[0]:                 # entry-level PM/analyst filter
            dropped["title"] += 1
            continue
        loc = (j.get("location") or "").strip()[:300]
        if not scraper.is_us_location(loc):                     # US-only (blank/unknown is kept)
            dropped["us"] += 1
            continue
        seen.add(url)
        company = (j.get("company") or "").strip()[:200]
        spons = "unknown"
        if sidx is not None and company:
            spons = "yes" if scraper.sponsors_h1b(company, sidx) else "no"
        # NO import-time stamp: an imported job's posting date is UNKNOWN until the
        # detail-fetch finds a real datePosted — a stamp would show as "Today" in the
        # feed and lie about freshness. Empty -> the card simply shows no date.
        kept.append({"found_date": (j.get("found_date") or ""), "title": title,
                     "company": company, "location": loc, "url": url, "sponsors_h1b": spons})

    if kept:
        try:
            db.add_jobs(kept)
        except Exception as e:
            return _cors(jsonify({"ok": False, "error": str(e)[:160]})), 500
        get_jobs(force=True)                     # imported jobs show on the next feed load
    # added_urls lets the extension follow up with JDs for the new jobs;
    # needs_jd asks it to also backfill known jobs whose JD is still missing.
    return _cors(jsonify({"ok": True, "added": len(kept), "scanned": scanned,
                          "dropped": dropped, "needs_jd": needs_jd,
                          "added_urls": [k["url"] for k in kept][:500]}))


@app.route("/api/ext/detect_board", methods=["POST", "OPTIONS"])
def ext_detect_board():
    """Extension -> 'can this site be scraped DAILY?' Runs the same detection chain as
    "Add board" over the page URL plus candidates collected from the LIVE DOM (iframe
    srcs + ATS-host links) — which catches JS-injected embeds that a server-side fetch
    of the page would never see. Body: {token, url, candidates?, add?}. With add=true
    the found board is saved to the boards table and joins the next scrape."""
    import scraper
    from flask import jsonify
    if request.method == "OPTIONS":
        return _cors(app.make_response(("", 204)))
    data = request.get_json(silent=True) or {}
    user = _ext_user(data.get("token", ""))
    if not user:
        return _cors(jsonify({"ok": False, "error": "Invalid token"})), 401
    page = (data.get("url") or "").strip()
    det = None
    if page:
        det = (scraper.detect_board(page) or scraper.detect_jibe(page)
               or scraper.detect_phenom(page) or scraper.detect_successfactors(page)
               or scraper.detect_linked_ats(page))
    if not det:
        for c in (data.get("candidates") or [])[:10]:
            if not isinstance(c, str):
                continue
            det = scraper.detect_board(c)          # candidates are ATS-looking URLs:
            if not det and "jibeapply.com" in c:   # URL rules cover them; jibe needs a probe
                det = scraper.detect_jibe(c)
            if det:
                break
    if not det:
        return _cors(jsonify({"ok": True, "found": False}))
    burl, ats, name = det
    if burl in {u for u, _, _ in scraper.SOURCES}:
        return _cors(jsonify({"ok": True, "found": True, "ats": ats, "name": name,
                              "builtin": True}))
    try:
        n = scraper.probe_board(burl, ats)
    except Exception:
        n = None
    added = False
    if data.get("add") and n:
        try:
            added = db.add_board(burl, ats, (data.get("name") or name), added_by=user)[0]
        except Exception:
            added = False
    return _cors(jsonify({"ok": True, "found": True, "board_url": burl, "ats": ats,
                          "name": name, "count": n, "added": added}))


@app.route("/api/ext/jds", methods=["POST", "OPTIONS"])
def ext_jds():
    """Extension -> attach job DESCRIPTIONS to jobs it just bulk-imported. Bot-walled
    sites (Tesla) block our servers, so score_jobs can never fetch these JDs — but the
    user's browser can, and without a stored JD the job scores 0% and hides below the
    match slider. Token-auth; only urls already in the jobs table are accepted; text is
    length-gated (too short = nav junk) and size-capped. Body: {token, jds: {url: text}}."""
    import scraper
    from flask import jsonify
    if request.method == "OPTIONS":
        return _cors(app.make_response(("", 204)))
    data = request.get_json(silent=True) or {}
    if not _ext_user(data.get("token", "")):
        return _cors(jsonify({"ok": False, "error": "Invalid token"})), 401
    jds = data.get("jds")
    if not isinstance(jds, dict) or not jds:
        return _cors(jsonify({"ok": False, "error": "No jds"})), 400
    known = {j.get("url") for j in get_jobs()}
    clean, patches, removed = {}, [], []
    for u, val in list(jds.items())[:200]:
        if u not in known:
            continue
        # value is either a bare JD string, or {jd, location, found_date} from the
        # generic detail-fetch (JSON-LD detail pages carry real location + datePosted).
        jd = val if isinstance(val, str) else (val.get("jd") if isinstance(val, dict) else "")
        if isinstance(jd, str) and len(jd.strip()) > 200:
            clean[u] = jd.strip()[:12000]
        if isinstance(val, dict):
            patch = {"url": u}
            loc = (val.get("location") or "").strip()[:300]
            if loc and re.search(r"[A-Za-z]", loc):
                # SELF-CLEAN: a job imported with an unknown location only survived the
                # US filter by benefit of the doubt. If its real location turns out
                # non-US, remove it — it never belonged in a US feed (Tesla Osaka bug).
                if not scraper.is_us_location(loc):
                    removed.append(u)
                    clean.pop(u, None)
                    continue
                patch["location"] = loc
            date = (val.get("found_date") or "").strip()[:10]
            if re.match(r"^\d{4}-\d{2}-\d{2}$", date):
                patch["found_date"] = date
            if len(patch) > 1:
                patches.append(patch)
    try:
        if clean:
            db.update_jds(clean)
        if patches:
            db.update_job_fields(patches)
        if removed:
            db.delete_urls(removed)
    except Exception as e:
        return _cors(jsonify({"ok": False, "error": str(e)[:160]})), 500
    if clean or patches or removed:
        get_jobs(force=True)                     # re-pull rows so the new data is visible…
        _score_cache.clear()                     # …and per-user scores recompute with JDs
        _sponsor_cache.clear()
        for _u in list(clean) + removed:         # only these JDs changed -> drop their stale meta
            _jdmeta.pop(_u, None)
    return _cors(jsonify({"ok": True, "stored": len(clean), "patched": len(patches),
                          "removed_nonus": len(removed)}))


@app.route("/healthz")
def healthz():
    """Public liveness probe — no auth, no DB, no work. An uptime pinger hits this every few
    minutes to keep the Passenger process (and its warm job/score/status caches) alive, so
    visitors don't pay the cold-start re-import + cache refill. See CPANEL_DEPLOY.md."""
    return Response("ok", mimetype="text/plain")


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
