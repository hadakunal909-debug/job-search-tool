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
import sys
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

# Anchor to this file's directory BEFORE core/db import, because both read files by relative
# path (.env, idf.json, sponsor_counts.json, sponsors.txt, everify.txt).
#
# This can't be left to passenger_wsgi.py. cPanel REGENERATES that file from the Python App's
# "Application startup file" setting, and its generated stub does no chdir — so depending on
# which entry style is configured, the app would boot with the wrong cwd and silently come up
# with no Supabase credentials and no data. Doing it here makes the app work identically
# however it is launched: cPanel's stub, our own passenger_wsgi.py, a cron job, or `python web.py`.
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)
try:
    os.chdir(_APP_DIR)
except OSError:
    pass                    # a read-only or vanished cwd must not stop the app from booting

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

try:
    import analytics
except Exception:                       # pragma: no cover - deploy safety net
    # Usage tracking is a nice-to-have; the app must not fail to BOOT without it. A deploy that
    # forgets analytics.py (it has to be listed in .cpanel.yml) then loses the events rather
    # than the site. Every call site uses only emit() and stats().
    class _NoAnalytics(object):
        _optout = {"names": frozenset(), "at": 0.0}

        @staticmethod
        def emit(*a, **k):
            pass

        @staticmethod
        def stats():
            return {"off": True, "queued": 0, "muted": False, "hour_count": 0, "optouts": 0}

    analytics = _NoAnalytics()
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
    print("WARNING: no APP_SECRET or SUPABASE_KEY set. Using a machine-local dev signing "
          "key. Set APP_SECRET in production so sessions/tokens can't be forged.", file=_sys.stderr)

app.permanent_session_lifetime = 60 * 60 * 24 * 30      # 30-day login
# Cookie hardening: HttpOnly (no JS access) + SameSite=Lax (blocks cross-site POST CSRF on
# our cookie-auth forms). Secure is opt-in via env so local/preview over http still works —
# set SESSION_COOKIE_SECURE=1 in production (https) to stop the cookie leaking over http.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "").lower() in ("1", "true", "yes"),
    # Résumé uploads are the only file input in the product. Flask rejects a larger body before
    # a byte reaches a view, so an oversized or hostile upload costs nothing to refuse. Slightly
    # above core.RESUME_UPLOAD_MAX_BYTES so the friendlier per-file message is what users
    # normally see, with this as the hard backstop.
    MAX_CONTENT_LENGTH=6 * 1024 * 1024,
)


# A session is 30 minutes of inactivity, GA4's convention and about the length of a real
# feed-scanning sitting. The id lives in the existing signed, HttpOnly login cookie: no table,
# no DB round trip, and it is shared by server events and client beacons so the two streams
# stitch together. sessionStorage was the alternative and is wrong — two open tabs would look
# like two people.
_SESSION_GAP = 30 * 60


def _sid():
    now = int(time.time())
    sid, at = session.get("sid"), session.get("sid_at") or 0
    if not sid or now - at > _SESSION_GAP:
        sid = secrets.token_urlsafe(9)
        session["sid"], session["sid_at"] = sid, now
    elif now - at > 60:
        # Rewriting sid_at on EVERY request marks the session modified, which makes Flask emit
        # Set-Cookie on every response including each /api/feed poll. Once a minute is invisible.
        session["sid_at"] = now
    return sid


@app.before_request
def _csp_nonce():
    """A fresh random nonce per request. Templates stamp it onto their inline <script>
    tags (nonce="{{ csp_nonce }}") so the CSP can allowlist OUR inline scripts by nonce
    without opening the door to all inline script ('unsafe-inline')."""
    g.csp_nonce = secrets.token_urlsafe(16)
    g.t0 = time.perf_counter()
    g.sid = _sid() if session.get("user") else ""


# Resources the UI legitimately loads from off-site, kept here so the CSP stays readable:
# Google Fonts (CSS from googleapis, font files from gstatic) + company logos from
# tN.gstatic.com.
#
# www.google.com is NO LONGER in img-src. The logos used to be requested from
# www.google.com/s2/favicons, which 301-redirects to gstatic, so the wildcard had to be
# allowed for the second hop as well. They now request gstatic directly (see LOGO_BASE in
# static/app.js for why), which means one fewer origin the page may load images from.
# Everything else is same-origin ('self').
_CSP_TEMPLATE = (
    "default-src 'self'; "
    "script-src 'self' 'nonce-%s'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; "
    "img-src 'self' data: https://*.gstatic.com; "
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
    # Static assets are fingerprinted, so they can be cached hard and the browser stops
    # re-requesting them on every page load.
    #
    # This used to be setdefault(), which never fired: Flask's send_from_directory already
    # sets "no-cache" (SEND_FILE_MAX_AGE_DEFAULT has defaulted to None since Flask 2.0, which
    # switches it to ETag revalidation), so the header below was overridden before it was ever
    # read. style.css and app.js have been revalidating on every single page load.
    #
    # Assignment, not setdefault, and only for a URL that is genuinely fingerprinted:
    #   * /static/dist/**  is content hashed by Vite, so the name changes when the bytes do.
    #   * ?v=<mtime>       is what static_v() stamps on everything else.
    # An unfingerprinted /static/ URL keeps Flask's revalidation, because a week of immutable
    # caching on a name that can be reused is unfixable from the server side.
    if request.path.startswith("/static/"):
        fingerprinted = request.path.startswith("/static/dist/") or request.args.get("v")
        if fingerprinted:
            resp.headers["Cache-Control"] = "public, max-age=604800, immutable"
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


# Registered AFTER _compress on purpose. Flask runs after_request handlers in REVERSE
# registration order, so this executes first — before the body is gzipped — and it never
# touches the body anyway.
#
# This one hook is what makes dead-feature detection free: every page in the app reports itself
# with no per-route work, so "which of these 58 routes has nobody opened in 30 days" becomes a
# query instead of a guess. props.ms is the render time, which is the closest thing this app has
# to latency monitoring — on shared cPanel a slow feed is the most likely reason someone quietly
# stops using it.
@app.after_request
def _ev_page_view(resp):
    try:
        user = session.get("user")
        if (user and resp.status_code == 200
                and (resp.content_type or "").startswith("text/html")
                and request.endpoint not in ("static", "healthz", "api_ev")):
            analytics.emit(user, getattr(g, "sid", ""), "page_view",
                           ep=request.endpoint or "?",
                           ms=int((time.perf_counter() - getattr(g, "t0", 0)) * 1000),
                           src=(request.args.get("src") or "")[:20])
    except Exception:
        pass
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
_sponsor_cache = {}          # url -> (verdict, reason) read from the JD (same for everyone)
_jdmeta = core.load_jdmeta()  # url -> {analyzed, exp_years, exp_level, sponsor_jd}; prewarmed from
                              # jdmeta.json (built by the cron scorer) so cold renders skip recompute
# Shape for a job with no precomputed JD analysis (a job added since the last cron score run).
# The feed list no longer carries JD text, so such a job simply shows no JD-derived badges and
# its baseline match_score until the next cron run refreshes jdmeta.json.
_EMPTY_META = {"analyzed": {}, "exp_years": None, "exp_level": "", "sponsor_jd": ("", "")}

def job_analysis(j):
    """One row's résumé-independent keyword analysis (analyze_jd's shape), or {} when we have
    none — callers must treat {} as "not scoreable" and fall back to match_score.

    THIS is what makes the feed's match score personal. score_against needs a job's keyword
    weights to score it against any résumé; those live in jdmeta.json, which is ~30 MB,
    gitignored, and built on an ephemeral GitHub Actions runner — so it never reached the live
    site. With it missing, user_scores had nothing to score against and fell back to the stored
    match_score: the baseline the cron computed against the repo's own resume.txt. Every signed
    -in user was shown the OWNER's percentages. The jd_terms column carries the same data
    through Supabase with no file deploy.

    jdmeta.json is still preferred where it exists, because a dev machine that just ran the
    scorer can be fresher than the last published run.

    Unpacked per call rather than held in an index on purpose. Measured over the 19,314-row
    corpus: streaming unpack-and-score costs 1.05 s for a whole feed, while an index of the same
    data costs 1.5 s to build AND 47 MB resident for as long as the corpus stands. Both callers
    that walk every row (user_scores, ranked_rows) already cache their own result per user, so
    the CPU is paid once per corpus change — and shared hosting is far tighter on memory than
    on that.
    """
    m = (_jdmeta.get(j.get("url") or "") or {}).get("analyzed")
    if m and m.get("terms"):
        return m
    return core.unpack_analyzed(j.get("jd_terms")) if j.get("jd_terms") else {}


def _jd_fields(j):
    """The JD-derived fields for one job, from the ONE source both the card and the detail
    panel read. Returns (exp_years, exp_level, sponsor_verdict, sponsor_reason).

    Ladder, per field independently: real Supabase COLUMN -> jdmeta.json -> nothing. Only the
    column reaches the live site — jdmeta.json is gitignored and never deployed, and the
    scraper that builds it runs on an ephemeral GitHub Actions runner. That is why the
    experience and "hide no-sponsorship" filters were silent no-ops in production while the
    detail panel, which recomputed the same fields from the JD at request time, confidently
    showed "8+ yrs" on a job the "<=2 yrs" filter had just let through.

    jdmeta stays as the fallback so a developer's machine, where the file DOES exist and can be
    fresher than the last scoring run, doesn't regress. Per-field rather than all-or-nothing
    because a JD can state a sponsorship verdict and no year count, or the reverse.
    """
    meta = _jdmeta.get(j.get("url")) or _EMPTY_META
    m_sv, m_sr = meta.get("sponsor_jd") or ("", "")
    # `is not None` not truthiness: '' is a real stored verdict meaning "the JD says nothing",
    # and it must win over a stale jdmeta entry rather than fall through to it.
    c_sv = j.get("sponsor_jd")
    sv, sreason = ((c_sv, j.get("sponsor_reason") or "") if c_sv is not None else (m_sv, m_sr))
    c_exp = j.get("exp_max_years")
    exp_y = c_exp if c_exp is not None else meta.get("exp_years")
    # exp_level is DERIVED, never stored: two columns that can disagree is a bug generator,
    # and the level is one comparison away from the number.
    return exp_y, core.exp_level_for(exp_y), sv, sreason
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


# Per-year H-1B history, read only by the company panel. Deferred like sponsor_counts above,
# though this one is small (~0.2 MB) — the feed never touches it, so there is no reason for it
# to be on the import path at all.
_sponsor_years_cache = None


def sponsor_years():
    global _sponsor_years_cache
    if _sponsor_years_cache is None:
        try:
            _sponsor_years_cache = core.load_sponsor_years()
        except Exception:
            _sponsor_years_cache = {}
    return _sponsor_years_cache


# visa_tags.json is ~1.8 MB / ~77k keys — same cold-start reasoning as sponsor_counts above,
# so it's deferred to first use rather than loaded at import. {} until the file is built,
# which makes every visa badge and the visa filter simply not render.
_visa_index_cache = None

# (key, checkbox label, tooltip) for the five visa filters, in core.VISA_TAGS order so the
# controls, the card badges and the digest chips can never drift out of order.
_VISA_TAG_CONTROLS = [(k, core.VISA_TAG_LABELS[k], core.VISA_TAG_TIPS[k]) for k in core.VISA_TAGS]


def visa_index():
    global _visa_index_cache
    if _visa_index_cache is None:
        try:
            _visa_index_cache = core.load_visa_tags()
        except Exception:
            _visa_index_cache = {}
    return _visa_index_cache
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
        txt = (db.get_user(user, "resume") or {}).get("resume", "") or ""
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
        legacy = (db.get_user(user, "resume") or {}).get("resume", "") or ""
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

# Snapshot of the feed rows, SHARED BY EVERY PASSENGER WORKER.
#
# _jobs_cache above is per-process. Passenger runs several workers and recycles them freely, so
# the 1 h TTL alone still meant a full feed read per worker per recycle — measured at ~10.7 MB
# decoded (19,268 rows x ~601 bytes), which made this the largest single consumer of the
# free-tier egress budget, well ahead of the scraper. A worker that starts cold now reads this
# file instead of the network. Overridable so a read-only deploy can point it at a temp dir;
# every read and write is best-effort, so if the path isn't writable the app simply degrades to
# the old per-worker behaviour rather than failing a request.
_JOBS_SNAPSHOT = os.environ.get("JOBS_SNAPSHOT") or os.path.join(_APP_DIR, "jobs_snapshot.json.gz")


def _snapshot_read(max_age):
    """(rows, fingerprint) from the shared snapshot if it exists and is younger than max_age
    seconds, else (None, None)."""
    try:
        if time.time() - os.path.getmtime(_JOBS_SNAPSHOT) > max_age:
            return None, None
        with _gzip.open(_JOBS_SNAPSHOT, "rt", encoding="utf-8") as fh:
            blob = json.load(fh)
        rows = blob.get("rows") or None
        return rows, tuple(blob.get("fingerprint") or ())
    except Exception:
        return None, None            # missing, half-written, or corrupt -> just re-read


def _snapshot_write(rows, fingerprint):
    """Replace the shared snapshot atomically. Written to a pid-suffixed temp file and renamed,
    because two workers can refresh at once and a reader must never see a partial file."""
    try:
        tmp = "%s.%d.tmp" % (_JOBS_SNAPSHOT, os.getpid())
        with _gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump({"rows": rows, "fingerprint": list(fingerprint or ())}, fh)
        os.replace(tmp, _JOBS_SNAPSHOT)
    except Exception:
        pass                          # an optimization only; never fail a request over it


def get_jobs(force=False):
    """All jobs, from a three-level cache: this worker's memory, then the snapshot file shared
    across workers, then Supabase.

    Jobs only change on the cron scrape, so the network is touched only when a fingerprint probe
    (db.jobs_fingerprint — a HEAD plus a one-row select, ~0 bytes) says the corpus actually
    moved. /reload, add-board and the admin refresh links pass force=True, which always re-reads
    and rewrites the snapshot. Endpoints that change rows but render nothing call
    _invalidate_jobs() instead — see there for why.
    """
    if not force and _jobs_cache["rows"] is not None \
            and time.time() - _jobs_cache["at"] <= _JOBS_TTL:
        return _jobs_cache["rows"]

    if not force:
        # Another worker may already have paid for this read.
        rows, fp = _snapshot_read(_JOBS_TTL)
        if rows:
            _jobs_cache["rows"], _jobs_cache["fp"] = rows, fp
            _jobs_cache["at"] = time.time()
            return rows
        # Past the TTL but possibly unchanged. The probe costs ~nothing against the full read it
        # can avoid; an unavailable probe returns (None, "") and falls through to the re-read.
        if _jobs_cache["rows"] is not None:
            fp = db.jobs_fingerprint()
            if fp[0] is not None and fp == _jobs_cache.get("fp"):
                _jobs_cache["at"] = time.time()
                _snapshot_write(_jobs_cache["rows"], fp)   # refresh mtime for the other workers
                return _jobs_cache["rows"]

    try:
        # include_jd=False: the feed never shows the JD; the detail panel fetches one JD on
        # demand (db.get_job_jd), so we skip pulling the description text into memory. At 19k
        # rows that column is the difference between ~10.7 MB and ~122 MB.
        rows = db.load_jobs(include_jd=False) or []
        fp = db.jobs_fingerprint()
        _jobs_cache["rows"], _jobs_cache["fp"] = rows, fp
        _snapshot_write(rows, fp)
    except Exception:
        _jobs_cache["rows"] = _jobs_cache["rows"] or []
    _jobs_cache["at"] = time.time()
    return _jobs_cache["rows"]


def _invalidate_jobs():
    """Mark the feed rows stale WITHOUT paying for the re-read here.

    get_jobs(force=True) re-reads the whole corpus on the spot — ~12 MB at 20k rows. That is
    the right trade for a request that goes on to RENDER the feed (/reload, the admin
    ?refresh=1 links, the delete-plan apply all redirect into a page that calls get_jobs), since
    the render would have paid for it a moment later anyway. It is the wrong trade for a JSON
    endpoint that renders nothing: the extension posts /api/ext/bulk_jobs and then /api/ext/jds
    for every board it scans, so a ten-board session bought twenty full reads nobody ever
    looked at. Against a 5 GB/month free-tier egress cap that is ~5% of the month in one
    sitting. Deferring collapses those twenty into the one read the next feed render does.

    All three lines matter:
      * at=0 alone would not work — the next get_jobs() falls through to the snapshot file,
        whose mtime is fresh, and serves back exactly the rows we just invalidated.
      * fp=None is what makes a PATCH visible. jobs_fingerprint() is (row count, max
        first_seen) and update_job_fields moves neither, so the probe would report "unchanged"
        and keep the stale rows indefinitely. None can never compare equal to a real
        fingerprint, so the probe falls through to the re-read.
      * dropping the snapshot is best-effort like every other write to it; the first worker
        past here re-reads and writes it back for the others.
    """
    _jobs_cache["at"] = 0
    _jobs_cache["fp"] = None
    try:
        os.remove(_JOBS_SNAPSHOT)
    except Exception:
        pass


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
        # Score THIS user's profile against the job's precomputed keyword analysis. The feed
        # rows carry no JD text, but the analysis is résumé-independent, so no text is needed
        # here — see job_analysis(). A job with no stored analysis (added since the last cron
        # run, or never had a readable JD) falls back to the baseline match_score, which is
        # scored against the repo's resume.txt and is therefore NOT this user's number. That
        # fallback used to be every row on the live site.
        analyzed = job_analysis(j)
        if resume and analyzed.get("terms"):
            try:
                scores[u] = int(core.score_against(resume_low, analyzed)[0])
            except Exception:
                scores[u] = 0
        elif resume:
            # This user HAS a résumé but we have no analysis for this row yet (scraped since the
            # last scoring run, or never had a readable JD). The stored match_score is scored
            # against the repo's resume.txt, so it is somebody else's number — but for a user
            # with a profile it is a reasonable placeholder until scoring catches up.
            try:
                scores[u] = int(j.get("match_score") or 0)
            except Exception:
                scores[u] = 0
        else:
            # NO RÉSUMÉ: no score. This used to fall through to match_score for every row, so a
            # brand-new account saw a feed of confident 62-64% rings computed against the
            # scraper's own resume.txt — someone else's CV, rendered identically to a real
            # personalised match. A number that looks personalised and isn't is worse than no
            # number, because it teaches the user to distrust every other one on the card.
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
    exp_y, exp_lvl, sv, sreason = _jd_fields(j)
    strength, scount = core.sponsor_strength(c, sponsor_counts())
    # Employer-level routes, then narrowed by what THIS posting says: a JD that rules out
    # sponsorship must not carry sponsorship badges (see core.visa_tags_for_posting).
    vtags = core.visa_tags_for_posting(core.visa_tags(c, visa_index()), sv, sreason)
    # A too-thin/truncated JD can't be scored honestly (see core.analyze_jd) — surface it as
    # "JD pending" instead of a misleading number, and keep it at 0 so it sorts/filters low
    # rather than sitting at a fake ~100% on top of the feed. Read from the same analysis
    # user_scores scored against, so a card can't show a percentage AND call itself pending.
    pending = bool(job_analysis(j).get("thin"))
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
            # Broader than date_verified: "somebody STATED this date" rather than "the lookup
            # service confirmed it". core.is_trusted_date is the one definition; the feed's
            # verifiedonly filter reads this, and app.js just checks the flag rather than
            # re-deriving the string-shape rule.
            "date_trusted": core.is_trusted_date(j.get("found_date"), j.get("posted_verified")),
            # Which role families this title belongs to. Computed here rather than in JS so the
            # phrase vocabulary has ONE definition; app.js just intersects two lists.
            "roles": list(core.roles_for_title(j.get("title"))),
            # Some employers publish no posting date at all (Tesla's careers API has no date
            # field anywhere), so this is when the job first entered OUR database. Rendered as
            # "Added <x>", never as a posting date. "" until the migration has been run.
            "first_seen": str(j.get("first_seen") or "")[:10],
            "score": 0 if pending else score, "score_pending": pending,
            "sponsor_jd": sv, "sponsor_reason": sreason, "agency": core.is_agency(c),
            "cap_exempt": core.is_cap_exempt(c),
            # Which immigration routes this employer has actually filed for (DOL LCA + PERM
            # + E-Verify). A missing tag means "no record", never "won't sponsor".
            "visa": vtags,
            # stem_opt IS the E-Verify fact; the everify.txt path stays as a fallback for
            # anyone who built that file (it has never existed in this repo).
            "everify": ("stem_opt" in vtags) or core.is_everify(c, _EVERIFY_INDEX),
            "exp_years": exp_y if exp_y is not None else "", "exp_level": exp_lvl,
            "strength": strength, "strength_n": scount,
            "intern": bool(_INTERN_RE.search(j.get("title") or "")),
            # 'dev' (software/data/infra) vs 'mgmt' (project/product/ops) — the feed's one-click
            # career split. core.role_track is the single definition; the digest reads it too.
            "track": core.role_track(j.get("title") or ""),
            "logo_domain": logodomain(c), "logo_color": logocolor(c),
            "initial": c[:1].upper() if c else "?"}


_AGGREGATOR_HOSTS = core.AGGREGATOR_HOSTS


def _dupe_key(r):
    """Identity of a POSTING rather than of a URL: title + company + full location.

    core.posting_key is the single definition — the scraper applies the same key before insert,
    and if the two ever drift the feed would hide a posting the scraper kept (or the reverse).
    Render-time keeps the permissive variant: a blank location still yields a key here, because
    nothing is deleted and the worst case is one collapsed card.
    """
    return core.posting_key(r.get("title"), r.get("company"), r.get("location"))


def _host(r):
    return core.url_host(r.get("url"))


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
        "intern": prefs.get("intern") or "any", "track": prefs.get("track") or "any",
        "loc": prefs.get("loc") or "",
        "minsal": str(prefs.get("minsal") or 0), "sort": prefs.get("sort") or "score",
        "remote": "1" if prefs.get("remote") else "",
        "hideagency": "1" if prefs.get("hideagency") else "",
        "visatags": prefs.get("visatags") or "",
        "hidenospon": "1" if prefs.get("hidenospon") else "",
        "verifiedonly": "1" if prefs.get("verifiedonly") else "",
        "roles": prefs.get("roles") or "",
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


# The date a row is ORDERED AND FILTERED by. "Posted within" and "Newest" go through this rather
# than reading r["date"], so a job whose employer publishes no posting date is judged on when it
# entered the corpus instead of being treated as dateless. Without the fallback those rows passed
# every date filter — the test is `date and date < cut`, and an empty date short-circuits to
# "keep" — so "Past 24 hours" silently returned 374 undated Tesla cards. One function for both
# uses, deliberately: a separate filter/sort pair is two things that can drift.
# app.js mirrors this as rowDate(); scripts/feed_parity.py checks the mirror.
def _row_date(r):
    return r.get("date") or r.get("first_seen") or ""


# Ordering for sort=sponsor. core.sponsor_rank is the single definition — the email digest applies
# the same ladder and cannot import this module, so the rule lives one level down.
# app.js mirrors it as sponsorRank(); scripts/feed_parity.py checks that mirror.
_row_sponsor_rank = core.sponsor_rank


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
    want_visa = core.parse_visa_pref(p.get("visatags"))
    hide_no = (p.get("hidenospon") or "") in ("1", "true", "yes", "on")
    verified_only = (p.get("verifiedonly") or "") in ("1", "true", "yes", "on")
    want_roles = core.parse_roles_pref(p.get("roles"))
    exp = p.get("exp") or "any"
    intern = p.get("intern") or "any"      # any | only (intern/co-op only) | no (exclude them)
    track = p.get("track") or "any"        # any | dev (software/data) | mgmt (project/product/ops)
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
        if cut:
            rdate = _row_date(r)
            if rdate and rdate < cut:
                continue
        if hide_no and r["sponsor_jd"] == "blocked":
            continue
        if verified_only and not r.get("date_trusted"):
            continue
        if not core.roles_match(r.get("roles"), want_roles):
            continue
        if not core.visa_tags_match(r.get("visa"), want_visa):
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
        if track != "any" and r.get("track") != track:
            continue
        if exp != "any":
            # exp_years is the HIGHEST year count the JD states (core.experience_years), so
            # "8+ years required; 2 years of SQL preferred" is an 8-year job and "<=2 yrs"
            # drops it. A JD that states no number is ALWAYS kept — many genuine entry-level
            # posts state none. Mirrored in app.js matches() and core.prefs_match().
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
    sort = p.get("sort") or "score"
    if sort == "newest":
        out.sort(key=lambda rs: _row_date(rs[0]), reverse=True)
    elif sort == "sponsor":
        out.sort(key=lambda rs: _row_sponsor_rank(rs[0]))
    return out                                  # else already in score order (rows pre-sorted)


def login_required(f):
    """Session check, plus a per-request confirmation that the account still exists and is
    enabled. Without that second half, deleting or disabling an account changes nothing for up
    to the 30-day cookie lifetime — the session cookie is self-contained and was never checked
    against the database again after sign-in. The lookup is a dict hit against _accounts'
    60-second cache, not a query."""
    @functools.wraps(f)
    def wrap(*a, **k):
        user = session.get("user")
        if not user:
            return redirect(url_for("login", next=request.path))
        dead = _session_dead(user)
        if dead:
            session.clear()
            flash(dead)
            return redirect(url_for("login"))
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


_vite_manifest = {}                # {} = not read yet, None = absent or unreadable


def _read_vite_manifest():
    """Vite's build manifest, read once per process. {} when there is no build.

    Lazy on purpose, exactly like sponsor_counts() and visa_index(). Passenger cold start is
    where shared-hosting memory and time are tightest, and this module is deliberately kept
    thin on the import path; a file read at module scope here is a regression.
    """
    global _vite_manifest
    if _vite_manifest != {}:
        return _vite_manifest or {}
    path = os.path.join(app.static_folder, "dist", ".vite", "manifest.json")
    try:
        with open(path, encoding="utf-8") as fh:
            _vite_manifest = json.load(fh) or None
    except Exception:
        _vite_manifest = None                    # no build, or a corrupt one
    return _vite_manifest or {}


@app.template_global()
def vite_preloads(src):
    """Chunks the entry imports STATICALLY, so they can be fetched in parallel with it.

    With more than one entry, Rollup hoists the shared runtime (React) into its own chunk. That
    is desirable: it is content hashed and immutably cached, so it downloads once across every
    migrated page. But the import only becomes visible after the browser has parsed the entry,
    which puts a round trip in front of ~190 KB on the critical path of the FIRST screen a new
    account ever sees. A modulepreload link makes it a parallel fetch instead.
    """
    entry = _read_vite_manifest().get(src) or {}
    out = []
    for key in entry.get("imports") or ():
        dep = _read_vite_manifest().get(key) or {}
        if dep.get("file"):
            out.append(url_for("static", filename="dist/" + dep["file"]))
    return out


@app.template_global()
def vite_entry(src):
    """URL for a built entry, or None when there is no build.

    Returning None rather than raising is the whole safety story: react_page.html omits the
    script tag, and a route that can't find its bundle must fall back to its Jinja template
    instead of serving a blank page. Filenames are content hashed, so no ?v= is needed.
    """
    entry = _read_vite_manifest().get(src)
    if not entry or not entry.get("file"):
        return None
    return url_for("static", filename="dist/" + entry["file"])


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
            return render_template("login.html", bad_login=True, username=u)
        rec = None
        db_down = False
        try:
            rec = db.get_user(u)
        except Exception:
            db_down = True
            flash("Couldn't reach the database. Try again.")
        if rec and auth.verify_password(p, rec.get("password_hash", "")):
            # Checked AFTER the password, so a wrong guess can't be used to enumerate which
            # accounts are disabled. Read off the row we already fetched, not the cache, so a
            # just-disabled account can't sign in during the cache's 60-second window.
            if rec.get("disabled_at"):
                flash("That account has been disabled.")
                return render_template("login.html", bad_login=True, username=u)
            _login_fails.pop(u, None)              # clear on success
            session.permanent = True
            session["user"] = u                    # résumé stays OUT of the cookie (size cap)
            _resume_cache[u] = (rec.get("resume", "") or "", time.time())
            analytics.emit(u, _sid(), "login")
            return redirect(_safe_next(request.args.get("next")) or url_for("feed"))
        # ONE response whether or not the account exists. This used to flash only when
        # db.get_user returned a row, so an unknown username re-rendered in silence while a
        # real one said "Wrong username or password" — which let anyone enumerate accounts by
        # watching for the message, the exact thing the disabled-check above is careful to
        # avoid. It also read as a dead form when you simply mistyped your username.
        # The failure counter still only records real accounts, so junk names can't grow
        # _login_fails without bound.
        if not db_down:
            if rec is not None:
                _login_fails.setdefault(u, []).append(time.time())
            flash("Wrong username or password.")
            return render_template("login.html", bad_login=True, username=u)
    return render_template("login.html", username=(u if request.method == "POST" else ""))


@app.route("/logout")
def logout():
    # Before the clear: session.clear() drops the sid, and the next login mints a fresh one.
    if session.get("user"):
        analytics.emit(session["user"], session.get("sid") or "", "logout")
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
    # A brand-new account lands on an unpersonalized feed: no résumé means no meaningful score,
    # so the match floor silently drops to 0 and every card reads the same. Send them through
    # the wizard once instead. _needs_onboarding checks the PROFILE, not just a flag, so an
    # existing account is never dragged through it, and every step offers "Skip for now".
    if _needs_onboarding(user):
        return redirect(url_for("welcome"))
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
        # ...and "Best match" means nothing when every score is suppressed, so fall back to
        # newest. Done by rewriting the PREF rather than special-casing the sort, so the
        # rendered control, _prefs_as_params and app.js all see the same value and the
        # server/client twins cannot disagree.
        prefs = dict(prefs, min=0, sort="newest")
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
                           default_min=default_min, paged=paged,
                           metros=_feed_metros(rows), states=_feed_states(rows),
                           visa_tag_controls=_VISA_TAG_CONTROLS,
                           role_groups=core.role_families_grouped(), role_counts=role_counts(),
                           role_max=ROLE_PICK_MAX,
                           visa=visa, visa_ctx=visa_ctx, prefs=prefs)


# Words that carry no signal in a "what does this employer hire for" list: they appear in
# almost every posting, so ranking by weight surfaces them above the actual tools.
_SKILL_STOP = frozenset("""
communication teamwork leadership collaboration interpersonal verbal written organizational
problem solving detail oriented time management customer service work experience team player
fast paced self starter multi task english degree bachelor master responsibilities requirements
qualifications preferred required ability able strong excellent knowledge understanding
""".split())


def _clean_research_list(items, lo=2, hi=48, cap=14, no_digits=False):
    """Filter a scraped list (values / initiatives / tech_stack) down to what is readable.

    The research crawler takes what a careers page gives it, and marketing pages give it junk:
    BYD's stored "values" include "{{sonItem.btnText}}" and "2.9 L/100km Fuel Consumption at Low
    SOC". Rather than show a fuel-economy figure as a company's ethics, drop template syntax,
    symbol-heavy strings, and lengths no human label has.

    `no_digits` is for the VALUES list only. A stated value never contains a number, while an
    initiative ("carbon neutral by 2030") and a tech stack ("HTML5", "Python 3") often do — so
    the strictest rule can't be the shared default.
    """
    out = []
    for s in (items or []):
        s = re.sub(r"\s+", " ", str(s or "")).strip(" .;,-")
        if not (lo <= len(s) <= hi) or "{{" in s or "}}" in s:
            continue
        if no_digits and any(c.isdigit() for c in s):
            continue
        letters = sum(c.isalpha() for c in s)
        if letters < len(s) * 0.6:            # mostly digits/symbols -> a spec, not a value
            continue
        if s.lower() not in (o.lower() for o in out):
            out.append(s)
        if len(out) >= cap:
            break
    return out


_research_cache = {"at": 0.0, "by_name": None}
_RESEARCH_TTL = 300


def _research_for(display):
    """The Resume Brain research record for an employer, or {}.

    Exact domain first — that is how the KB is keyed. Failing that, match on the record's own
    `name`, because logodomain() guesses a domain from the feed's spelling and the two rarely
    agree: the corpus says "BYD America" (-> bydamerica.com) where the crawler filed "BYD" under
    byd.com. The name index is cached, since the miss path is the common one until the KB fills
    up and it would otherwise re-read the table on every company page view.
    """
    try:
        rec = db.get_brain_company(logodomain(display))
        if rec:
            return rec
    except Exception:
        pass
    idx = _research_cache["by_name"]
    if idx is None or time.time() - _research_cache["at"] > _RESEARCH_TTL:
        try:
            idx = {}
            for dom, rec in (db.list_brain_companies() or {}).items():
                for label in ((rec or {}).get("name"), (dom or "").rsplit(".", 1)[0]):
                    k = db.block_key(label or "")
                    if k:
                        idx.setdefault(k, rec)
            _research_cache.update({"by_name": idx, "at": time.time()})
        except Exception:
            idx = _research_cache["by_name"] or {}
    key = db.block_key(display)
    if key in idx:
        return idx[key]
    # "BYD America" / "Amazon.com Services LLC": the KB name is a prefix of the feed's spelling.
    for k, rec in idx.items():
        if len(k) >= 3 and (key.startswith(k + " ") or key == k):
            return rec
    return {}


def _company_profile(display, key, rows, open_rows):
    """Everything the "More about this employer" panel shows.

    Two sources, deliberately labelled apart in the template so the reader knows which is which:

      * The Resume Brain research KB (what they do, mission, values, tech stack, initiatives).
        Written by the crawler the first time anyone tailors for that employer, so it is absent
        for most companies — the panel says so instead of pretending.

      * THEIR OWN POSTINGS, which we always have. Aggregating jd_terms across every opening
        gives the tools and skills this employer actually hires for, which is a better answer to
        "what do they use" than a marketing page is, and it exists for every employer.

    On sponsorship the panel carries three different things, and they are NOT interchangeable:
    the FY2009-23 approvals history from the USCIS Data Hub (real per-year counts), the routes
    this employer has filed under from the DOL LCA/PERM files (which carry no counts and no
    dates at all), and what their own live postings say right now. Only the first has years.
    """
    try:
        research = _research_for(display) or {}
    except Exception:
        research = {}

    # ---- what they hire for, from their own ads ----
    skills, seen_urls = collections.Counter(), {r["url"] for r in open_rows}
    for j in get_jobs():
        if j.get("url") not in seen_urls:
            continue
        a = job_analysis(j)
        for t, w in (a.get("weight") or {}).items():
            if t in _SKILL_STOP or len(t) < 2:
                continue
            skills[t] += w
    tracks = collections.Counter(r.get("track") or "other" for r in open_rows)
    states = collections.Counter(r["loc_state"] for r in open_rows if r.get("loc_state"))
    levels = collections.Counter()
    for r in open_rows:
        lv = r.get("exp_level")
        if lv:
            levels[lv] += 1
    pays = [r["salary_min"] for r in open_rows if r.get("salary_min")]
    highs = [r["salary_max"] for r in open_rows if r.get("salary_max")]
    today = datetime.date.today()
    d30 = (today - datetime.timedelta(days=30)).isoformat()
    d7 = (today - datetime.timedelta(days=7)).isoformat()

    # Per-year approvals, scaled here rather than in the template so the bars are one number
    # each. Height is a percentage of the tallest year, floored at 2% so a year with a single
    # approval still draws a mark instead of vanishing into the axis.
    hist = core.sponsor_history(display, sponsor_years())
    peak = max([n for _y, n in hist] or [0])
    bars = [{"year": y, "n": n, "pct": (2 + int(96.0 * n / peak)) if (peak and n) else 0}
            for y, n in hist]

    return {
        "research": research,
        "researched": bool(research.get("what_they_do") or research.get("about")
                           or research.get("mission")),
        "bars": bars,
        "hist_total": sum(n for _y, n in hist),
        "hist_from": hist[0][0] if hist else None,
        "hist_to": hist[-1][0] if hist else None,
        # The most recent year is a partial-ish figure in the Hub export and always reads low;
        # the 5-year window is the honest "are they sponsoring lately" number.
        "hist_recent": sum(n for y, n in hist if y >= (hist[-1][0] - 4)) if hist else 0,
        # NOT "values": Jinja resolves `about.values` to dict.values() and hands the template a
        # bound method, which then fails to iterate. Renamed rather than reached for with
        # about['values'], so the trap can't be walked into again from a different template.
        "company_values": _clean_research_list(research.get("values"), no_digits=True),
        "initiatives": _clean_research_list(research.get("initiatives"), hi=160, cap=6),
        "tech_stack": _clean_research_list(research.get("tech_stack"), hi=28, cap=18),
        # Top weighted terms across every open posting = the stack they hire for.
        "skills": [t for t, _w in skills.most_common(24)],
        "tracks": {"dev": tracks.get("dev", 0), "mgmt": tracks.get("mgmt", 0),
                   "other": tracks.get("other", 0)},
        "levels": {"entry": levels.get("entry", 0), "mid": levels.get("mid", 0),
                   "senior": levels.get("senior", 0),
                   "unstated": len(open_rows) - sum(levels.values())},
        "states": states.most_common(6),
        "remote": sum(1 for r in open_rows if r.get("remote")),
        "pay_lo": min(pays) if pays else None,
        "pay_hi": max(highs) if highs else None,
        "pay_n": len(pays),
        "fresh30": sum(1 for r in open_rows if (r.get("date") or "") >= d30),
        "fresh7": sum(1 for r in open_rows if (r.get("date") or "") >= d7),
        # The live sponsorship picture, from what these postings themselves say.
        "jd_blocked": sum(1 for r in rows if r.get("sponsor_jd") == "blocked"),
        "jd_open": sum(1 for r in rows if r.get("sponsor_jd") == "open"),
        "jd_silent": sum(1 for r in rows if not r.get("sponsor_jd")),
    }


@app.route("/company")
@login_required
def company():
    """Every opening at one employer, plus what we know about how they sponsor.

    Reuses the feed's pipeline end to end — ranked_rows for the personalized score and the
    same-posting-two-hosts dedupe, db.block_key for company identity (the key the admin bulk
    delete already matches on), and /api/feed + app.js for the cards themselves.

    The name travels as ?c= rather than in the path. Company names in this corpus contain
    slashes ("Kennedy/Jenks Consultants", "RE/SPEC Inc"), and on cPanel/Passenger behind Apache
    a %2F inside a path segment is 404'd before Flask ever sees it (AllowEncodedSlashes defaults
    to Off). Every other identifier in this file already travels in request.args for its own
    reasons, so this matches the house style too.

    Always server-paged: an employer with 527 openings must cost one small fetch, not a ~700 KB
    inline payload.
    """
    name = (request.args.get("c") or "").strip()
    key = db.block_key(name)
    if not key:
        return redirect(url_for("feed"))
    user = session["user"]
    rows = [r for r in ranked_rows(user, current_profile())
            if db.block_key(r["company"]) == key]
    # One employer reaches us under several spellings (a Workday casing bug once stored the
    # same postings under both "Amat" and "Applied Materials"), so show the label the corpus
    # uses most rather than whatever the link happened to carry.
    display = (collections.Counter(r["company"] for r in rows).most_common(1)[0][0]
               if rows else name)
    statuses = user_statuses(user)
    # What the page will actually list: /api/feed sends no saved prefs, so the only rows the
    # header must not count are the ones _filter_rows drops unconditionally.
    open_rows = [r for r in rows
                 if not r.get("closed") and statuses.get(r["url"]) != "hidden"]
    strength, scount = core.sponsor_strength(display, sponsor_counts())
    info = {
        "name": display, "n": len(open_rows), "n_all": len(rows),
        "remote": sum(1 for r in open_rows if r.get("remote")),
        "states": len({r.get("loc_state") for r in open_rows if r.get("loc_state")}),
        # EMPLOYER-level routes, deliberately NOT narrowed by any one JD: _build_row narrows
        # per posting via visa_tags_for_posting, which is a fact about that posting. Here the
        # question is what this company has filed for, ever.
        "visa": list(core.visa_tags(display, visa_index())),
        "agency": core.is_agency(display), "cap_exempt": core.is_cap_exempt(display),
        "strength": strength, "strength_n": scount,
        "logo_domain": logodomain(display), "logo_color": logocolor(display),
        "initial": display[:1].upper() if display else "?",
    }
    analytics.emit(user, getattr(g, "sid", ""), "page_view", page="company",
                   company=display, n=len(open_rows))
    return render_template("company.html", info=info, company_arg=display,
                           about=_company_profile(display, key, rows, open_rows),
                           # _feedgrid.html reads this to decide whether to draw a % ring.
                           # Omit it and every card here would suppress its score, including
                           # for users who do have a résumé.
                           has_resume=bool(current_profile()),
                           # Seeds the sort control, which used to hardcode "Best match" here —
                           # so a user whose saved sort was Newest or Sponsorship silently got
                           # score order on this page only. app.js's filter memory covers the
                           # feed -> company path; this covers a cold load straight to /company.
                           prefs=_user_prefs(user),
                           visa_labels=core.VISA_TAG_LABELS,
                           visa_tips=core.VISA_TAG_TIPS)


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
    # Which keys they actually moved off the defaults — the saved-search adoption signal.
    analytics.emit(user, getattr(g, "sid", ""), "prefs_save",
                   keys=[k for k, v in prefs.items() if v != core.DEFAULT_PREFS.get(k)][:12])
    return jsonify({"ok": True, "prefs": prefs, "note": msg})


@app.route("/api/feed")
@login_required
def api_feed():
    """Server-side search/filter/sort/paging over the FULL corpus, for the large-dataset feed.
    Mirrors app.js's client filters; returns a compact page of card rows in the same shape.

    One row = one card, so `total` is both the "N of M jobs" count and what Load-more pages
    against. (This used to page over DISPLAY UNITS, because a run of identical postings from one
    employer collapsed into a single "+N more" tile.)

    `company` narrows to one employer for /company. It is applied to the INPUT rather than
    inside _filter_rows, because that function is the declared twin of app.js's matches() and
    scripts/feed_parity.py diffs the two row for row — a server-only clause in there would
    either break parity or force a db.block_key mirror in JS.
    """
    user = session["user"]
    resume = current_profile()
    rows = ranked_rows(user, resume)
    ckey = db.block_key(request.args.get("company") or "")
    if ckey:
        rows = [r for r in rows if db.block_key(r["company"]) == ckey]
    statuses = user_statuses(user)
    matched = _filter_rows(rows, statuses, request.args)
    offset, limit = _page_args(request.args)
    page = matched[offset:offset + limit]
    out_rows = [dict(r, status=st) for (r, st) in page]                # status on a copy
    _ev_feed_view(user, request.args, out_rows, len(matched), offset)
    out = {"rows": out_rows, "total": len(matched),
           "has_more": offset + limit < len(matched)}
    if not matched:
        out["relax"] = _relax_suggestions(rows, statuses, request.args)
    return out


# What each narrowing filter reverts TO, and how to name the thing being dropped. The label
# describes the VALUE, not the control: "Removing Boston, MA" is actionable where "Removing the
# location filter" makes the reader go and look at what it was set to.
_RELAX = [
    ("min",          "0",   lambda v: "the %s%% match minimum" % v),
    ("loc",          "",    lambda v: v),
    ("visatags",     "",    lambda v: "the visa route filter"),
    ("date",         "any", lambda v: {"1": "Past 24 hours", "7": "Past 7 days",
                                       "30": "Past 30 days", "90": "Past 90 days"}.get(v, v)),
    ("minsal",       "",    lambda v: "the pay minimum"),
    ("exp",          "any", lambda v: "the experience filter"),
    ("intern",       "any", lambda v: "the internship filter"),
    ("remote",       "0",   lambda v: "Remote only"),
    ("hidenospon",   "0",   lambda v: "Hide no-sponsorship"),
    ("verifiedonly", "0",   lambda v: "Confirmed posting date"),
    ("hideagency",   "0",   lambda v: "Hide staffing agencies"),
    ("roles",        "",    lambda v: "the role filter"),
    ("track",        "any", lambda v: "the career track"),
]


def _relax_suggestions(rows, statuses, args, top=2):
    """For an EMPTY result: which single filter, if dropped, brings back the most jobs.

    The old empty state guessed ("Lower Match or clear your search") and named a control rather
    than a value. The app can simply know: re-run the filter with one control reverted and count
    what comes back. Preventing the dead end beats styling it.

    Only ever runs when nothing matched, so the cost is a handful of extra passes on the one
    render where the user is stuck and nothing is being shown anyway. Filters already at their
    default are skipped, so a typical stuck search costs two or three passes, not thirteen.
    """
    base = {k: v for k, v in args.items(True)} if hasattr(args, "items") else dict(args)
    out = []
    for key, default, label in _RELAX:
        cur = (base.get(key) or "").strip()
        if not cur or cur == default:
            continue                              # not set, so dropping it changes nothing
        probe = dict(base)
        probe[key] = default
        try:
            n = len(_filter_rows(rows, statuses, probe))
        except Exception:
            continue                              # a suggestion is a nicety, never a blocker
        if n > 0:
            out.append({"key": key, "value": default, "label": label(cur), "n": n})
    out.sort(key=lambda d: -d["n"])
    return out[:top]


def _ev_feed_view(user, args, rows, total, offset):
    """The workhorse event. app.js already serialises the entire toolbar into this request's
    query string (filterParams), so every filter change, search, sort and page arrives here for
    free — no client instrumentation, and nothing that can slow the feed down.

    props.f holds ONLY the prefs that differ from core.DEFAULT_PREFS, which is what makes
    "which of the 12 filters does anyone touch" answerable instead of "everyone sets all 12".
    """
    try:
        defaults = core.DEFAULT_PREFS
        f = {}
        for k, default in defaults.items():
            if k in ("alerts", "alert_min"):
                continue                       # set on /profile, never in the feed toolbar
            raw = args.get(k)
            if raw is None or raw == "":
                continue
            # Booleans arrive on the wire as "1"/"0", never as "True"/"False", so comparing
            # str(raw) to str(default) can NEVER match for a bool and every bool pref reads as
            # "changed" on every view. That silently made hideagency (the one default-True pref)
            # look like the most-used filter in the app at 80% of views, when it was only ever
            # the default being logged. Compare booleans as booleans.
            if isinstance(default, bool):
                if (str(raw).strip().lower() in ("1", "true", "on", "yes")) == default:
                    continue
            elif str(raw) == str(default):
                continue
            f[k] = str(raw)[:40]
        q = (args.get("q") or "").strip()
        props = {"tab": (args.get("tab") or "recommended")[:20], "n": total,
                 "off": offset, "shown": len(rows), "qn": len(q), "f": f}
        # A 5-bucket histogram of what was actually ON SCREEN. This is the impression
        # denominator for score calibration; per-job impressions would be ~60 rows a render.
        hist = [0] * 5
        for r in rows:
            try:
                hist[min(int(r.get("score") or 0), 100) // 20] += 1
            except Exception:
                pass
        props["hist"] = hist
        # Raw search text ONLY when it found nothing. A query that returned results reveals more
        # and teaches less; a query that returned nothing IS the finding — it names a gap in the
        # corpus. See the privacy note in analytics.py.
        if q and total == 0:
            props["q"] = q[:60]
        analytics.emit(user, getattr(g, "sid", ""), "feed_view", **props)
    except Exception:
        pass


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
    # The card's OWN analysis wherever we have it, so the ring in this panel is the same number
    # the card showed and "Add these to your résumé" is drawn from the same terms that produced
    # it. Only a job the scorer has never reached falls back to analyzing the JD we just
    # fetched — better information than nothing, and the card shows match_score meanwhile.
    analyzed = job_analysis(job)
    if not analyzed.get("terms"):
        analyzed = jd_meta({"url": url, "jd": jd}, core.load_idf())["analyzed"]
    if resume and analyzed.get("terms"):
        score, have, missing = core.score_against(resume.lower(), analyzed)
    else:
        try:
            score = int(job.get("match_score") or 0)
        except Exception:
            score = 0
        have, missing = [], []
    # Read the SAME stored fields the card does, so the panel and the card can never disagree.
    # They used to: the card read jdmeta.json (empty in production) while this route re-parsed
    # the JD per request, which is why a "<=2 yrs" filter would let a job through and then its
    # detail panel would announce "8+ yrs".
    exp_y, _exp_lvl, sv, sreason = _jd_fields(job)
    pending = bool(analyzed.get("thin"))
    # company/source/score are stamped on the event rather than looked up later: the 30-day
    # pruner deletes this row, and match_score is rewritten every scoring run.
    analytics.emit(session["user"], getattr(g, "sid", ""), "job_open", job_url=url,
                   company=job.get("company"), source=_host(job),
                   score=0 if pending else int(score or 0), pending=pending)
    return {"ok": True, "title": job.get("title", ""), "company": job.get("company", ""),
            "location": job.get("location", ""), "date": (job.get("found_date") or "")[:10],
            "url": url, "sponsors_h1b": job.get("sponsors_h1b", ""),
            "score": 0 if pending else int(score or 0), "score_pending": pending,
            "sponsor_jd": sv, "sponsor_reason": sreason,
            "agency": core.is_agency(job.get("company", "")),
            "cap_exempt": core.is_cap_exempt(job.get("company", "")),
            # Keep in step with _build_row — the modal and the card must not disagree.
            "visa": core.visa_tags_for_posting(
                core.visa_tags(job.get("company", ""), visa_index()), sv, sreason),
            "everify": ("stem_opt" in core.visa_tags(job.get("company", ""), visa_index()))
                       or core.is_everify(job.get("company", ""), _EVERIFY_INDEX),
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
    user = session["user"]
    # Read the PREVIOUS status before the write. user_jobs is current-state-only with no
    # timestamp, so like -> hide -> unhide leaves one row or none; this from/to pair is the
    # transition ledger that table structurally cannot keep. Without it, "hiding is really being
    # used as dismiss-for-now" is unanswerable, because the unhide erases the evidence.
    try:
        prev = user_statuses(user).get(url, "")
    except Exception:
        prev = ""
    try:
        db.set_user_status(user, url, status)
        _status_cache.pop(user, None)                # reflect the change on the next feed render
        if status == "applied":
            _autolog_application(user, url)
        _ev_action(user, url, prev, status, "api")
        return {"ok": True, "status": status}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500


def _ev_action(user, url, prev, status, via):
    """One like/hide/apply, with the job's company, board and score attached."""
    try:
        job = next((j for j in get_jobs() if j.get("url") == url), None) or {}
        try:
            score = int(job.get("match_score") or 0)
        except Exception:
            score = 0
        analytics.emit(user, getattr(g, "sid", ""), "action", job_url=url,
                       company=job.get("company"), source=_host(job), score=score,
                       to=status or "cleared", frm=prev or "none", via=via)
    except Exception:
        pass


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
    flash("Jobs reloaded.")
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


@app.route("/api/ev", methods=["POST"])
def api_ev():
    """Client-only interaction events (rail toggles, outbound apply clicks, dwell).

    Always 204, and deliberately NOT @login_required: a sendBeacon can neither follow a redirect
    nor report an error, so a 302 to /login would just burn a round trip. Logged out, this
    quietly does nothing — SameSite=Lax means a cross-site beacon arrives with no session and
    fails the `if user` check, which is the same posture /api/action already has.
    """
    user = session.get("user")
    if user:
        try:
            body = request.get_json(silent=True) or {}
            sid = getattr(g, "sid", "")      # server-side, never taken from the request body
            for e in (body.get("ev") or [])[:40]:
                if not isinstance(e, dict):
                    continue
                analytics.emit(user, sid, str(e.get("e") or ""), **(e.get("p") or {}))
        except Exception:
            pass
    return ("", 204)


@app.route("/api/scrape_status")
@login_required
def api_scrape_status():
    """Latest scrape progress (phase/done/total/found/started_at/updated_at/finished_at) for the
    in-page progress bar. The scraper + score_jobs write this row as they run."""
    return db.get_scrape_status() or {}


# ----------------------------- admin dashboard -----------------------------
# Who may open /admin. ADMIN_USERS is a comma-separated allowlist; with it unset we fall back
# to "the only account is the admin", which is right for the single-user install this started
# as and fails CLOSED the moment a second account is created. The obvious alternative default
# — any logged-in user — would silently hand the scrape trigger and the account list to every
# new login, and nothing about creating an account would prompt you to notice.
_ADMIN_USERS = {u.strip().lower()
                for u in (os.environ.get("ADMIN_USERS") or "").split(",") if u.strip()}
# 60s, not 300: a disable has to bite quickly, and this is a 3-row table. One PostgREST GET per
# TTL per worker process, amortised to a dict lookup per request.
_ACCOUNTS_TTL = 60
_accounts_cache = {"map": None, "at": 0.0}


def _accounts(force=False):
    """{username: row} for the whole users table, cached.

    On a DB error we keep the LAST GOOD map and retry sooner rather than returning {} — an
    empty map reads as "every account has been deleted", which would log everyone out of their
    own app over a transient Supabase blip. Returns None only when we have NEVER had a good
    read; callers treat that as "can't tell" and fail open.
    """
    c = _accounts_cache
    if force or c["map"] is None or time.time() - c["at"] > _ACCOUNTS_TTL:
        try:
            c["map"] = {(u.get("username") or ""): u for u in (db.list_users() or [])}
            c["at"] = time.time()
        except Exception:
            if c["map"] is None:
                return None
            c["at"] = time.time() - _ACCOUNTS_TTL + 10      # serve stale, retry in 10s
    return c["map"]


def _account_state(username):
    """The users row for `username`. None when the account is genuinely gone; {} when we can't
    tell. Callers fail CLOSED on None and OPEN on {} — see _accounts."""
    m = _accounts()
    if m is None:
        return {}
    return m.get(username)


def _session_dead(username):
    """Reason this session should be ended, or "" to let it through. One definition of "is this
    login still real", shared by login_required, admin_required and the login route."""
    st = _account_state(username)
    if st is None:
        return "That account no longer exists."
    if st.get("disabled_at"):
        return "That account has been disabled."
    return ""


def _sole_user():
    """The username when this install has exactly one account, else "".

    Counts DISABLED accounts too, deliberately. If they were excluded, disabling the second of
    two accounts would silently re-promote the first to sole-admin — admin scope must never
    widen as a side effect of a disable.
    """
    m = _accounts()
    names = list(m or {})
    return names[0] if len(names) == 1 else ""


@app.template_global()
def session_id():
    """The session id, for base.html's data-sid. Server-derived so a client beacon can't
    invent one — /api/ev ignores any sid in the request body and uses g.sid."""
    return getattr(g, "sid", "")


@app.template_global()
def is_admin(user=None):
    user = (user or session.get("user") or "").strip()
    if not user:
        return False
    if _ADMIN_USERS:
        return user.lower() in _ADMIN_USERS
    return user == _sole_user()


def _csrf_token():
    tok = session.get("_csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        session["_csrf"] = tok
    return tok


@app.template_global()
def csrf_token():
    return _csrf_token()


def _check_csrf():
    sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token") or ""
    return bool(sent) and hmac.compare_digest(sent, _csrf_token())


def admin_required(f):
    """login_required + admin, plus CSRF on anything that isn't a read.

    The CSRF check lives HERE rather than in each route so a future admin route cannot forget
    it. SameSite=Lax and `form-action 'self'` already stop classic cross-site form posts, which
    is the right bar for like/hide — it is not the right bar for "delete this account and every
    row belonging to it".
    """
    @functools.wraps(f)
    def wrap(*a, **k):
        user = session.get("user")
        if not user:
            return redirect(url_for("login", next=request.path))
        dead = _session_dead(user)
        if dead:
            session.clear()
            flash(dead)
            return redirect(url_for("login"))
        if not is_admin():
            flash("That page is admin-only.")
            return redirect(url_for("feed"))
        if request.method not in ("GET", "HEAD", "OPTIONS") and not _check_csrf():
            flash("That form expired. Reload the page and try again.")
            return redirect(url_for("admin_users"))
        return f(*a, **k)
    return wrap


@app.route("/scrape", methods=["POST"])
@admin_required
def scrape_now():
    """Trigger the scrape on GitHub Actions (workflow_dispatch) — runs on GitHub's servers.
    Needs GH_TOKEN in .env (a fine-grained PAT with Actions: read+write). Returns JSON so the
    admin page can start polling /api/scrape_status and draw the live progress bar.

    ADMIN-ONLY, and defined down HERE rather than beside the other feed routes for a concrete
    reason: decorators evaluate at import, and admin_required is defined immediately above.
    Move this back up beside /reload and Passenger dies on a NameError at cold start — a dead
    site, not a failed request.

    Why admin: every press burns from a shared, finite pool of GitHub Actions free minutes
    (~2,000/month, a run capped at 45). Under @login_required any account could drain it, and
    nothing about creating an account prompted anyone to notice. admin_required also brings the
    CSRF check this route never had.
    """
    gh = _trigger_github_action()
    if gh is None:
        return {"ok": False, "msg": "To enable this, add GH_TOKEN to .env (a GitHub token with "
                "Actions read+write). You can also run the scrape from the repo's Actions tab."}
    if not gh[0]:
        return {"ok": False, "msg": "Couldn't start the scrape: " + gh[1]}
    # Optimistic 'queued' status so the bar appears the instant you click — the scraper overwrites
    # it with real progress once the Action spins up on GitHub's servers.
    try:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        db.set_scrape_status({"phase": "queued", "done": 0, "total": 0, "found": 0,
                              "started_at": now, "run": now})
    except Exception:
        pass
    analytics.emit(session["user"], getattr(g, "sid", ""), "scrape_click")
    return {"ok": True, "msg": "Scrape started on GitHub Actions."}


def _gh_runs(limit=8):
    """Recent runs of the scrape workflow. None when no token is configured, [] when GitHub
    refuses. Read-only, and the fine-grained PAT the Update-jobs button already uses is scoped
    Actions: read+write — so this tile needs no new secret, just the token you already have."""
    tok = _gh_token()
    if not tok:
        return None
    import requests
    try:
        r = requests.get(
            "https://api.github.com/repos/%s/actions/workflows/%s/runs" % (GH_REPO, GH_WORKFLOW),
            headers={"Authorization": "Bearer %s" % tok,
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
            params={"per_page": limit}, timeout=15)
        if r.status_code >= 400:
            return []
        out = []
        for run in (r.json().get("workflow_runs") or [])[:limit]:
            started, ended = run.get("run_started_at") or "", run.get("updated_at") or ""
            mins = ""
            try:
                # A run still in progress has no meaningful end, so only completed runs get a
                # duration — updated_at on a live run is just "a moment ago" and would render
                # as a bogus 0m next to a bar that is still moving.
                if started and ended and run.get("status") == "completed":
                    d = (datetime.datetime.fromisoformat(ended.replace("Z", "+00:00"))
                         - datetime.datetime.fromisoformat(started.replace("Z", "+00:00")))
                    mins = "%dm %02ds" % (int(d.total_seconds()) // 60, int(d.total_seconds()) % 60)
            except Exception:
                pass
            out.append({
                "n": run.get("run_number"),
                "status": run.get("status") or "",
                # in-progress runs carry conclusion=None; show the live status instead of a blank
                "conclusion": run.get("conclusion") or (run.get("status") or ""),
                "event": run.get("event") or "",
                "started": started[:16].replace("T", " "),
                "mins": mins,
                "url": run.get("html_url") or "",
            })
        return out
    except Exception:
        return []


def _job_date(j):
    """Posting date for a RAW db row — the same choice _build_row makes for feed cards
    (verified posting date, else the day we first saw it), so both agree on what "fresh" is."""
    return ((j.get("posted_verified") or j.get("found_date")) or "")[:10]


_ADMIN_STATS_TTL = 60
_admin_stats_cache = {"data": None, "at": 0.0}


def _admin_stats():
    """Corpus health, computed off the already-cached job rows (get_jobs is a 1 h cache), so
    opening the dashboard costs no DB round-trip on a warm process. Cached another 60 s on top
    because every tile below walks the full ~25 k-row list."""
    c = _admin_stats_cache
    if c["data"] is not None and time.time() - c["at"] < _ADMIN_STATS_TTL:
        return c["data"]
    jobs = get_jobs()
    today = datetime.date.today()
    d1 = (today - datetime.timedelta(days=1)).isoformat()
    d7 = (today - datetime.timedelta(days=7)).isoformat()
    d30 = (today - datetime.timedelta(days=30)).isoformat()

    s = {"total": len(jobs), "closed": 0, "fresh1": 0, "fresh7": 0, "fresh30": 0, "undated": 0,
         "verified": 0, "scored": 0, "salaried": 0, "remote": 0}
    buckets = [0] * 5                       # 0-19 / 20-39 / 40-59 / 60-79 / 80-100
    hosts, companies, host_fresh = collections.Counter(), collections.Counter(), collections.Counter()
    for j in jobs:
        # is_active is None on an un-migrated row; only an explicit False means "we checked and
        # the posting is gone". `is False` rather than `not ...` keeps None out of the count.
        if j.get("is_active") is False:
            s["closed"] += 1
        if j.get("posted_verified"):
            s["verified"] += 1
        if j.get("salary_min"):
            s["salaried"] += 1
        if j.get("remote"):
            s["remote"] += 1
        dt = _job_date(j)
        if not dt:
            s["undated"] += 1
        else:
            if dt >= d30:
                s["fresh30"] += 1
            if dt >= d7:
                s["fresh7"] += 1
            if dt >= d1:
                s["fresh1"] += 1
        try:
            sc = int(j.get("match_score") or 0)
        except Exception:
            sc = 0
        if sc > 0:
            s["scored"] += 1
            buckets[min(sc, 100) // 20] += 1
        c_name = (j.get("company") or "").strip()
        if c_name:
            companies[c_name] += 1
        h = _host(j)                        # the feed's own host helper: ATS domain per posting
        if h:
            hosts[h] += 1
            if dt and dt >= d7:
                host_fresh[h] += 1
    s["pending"] = s["total"] - s["scored"]
    s["buckets"] = [{"label": lbl, "n": n}
                    for lbl, n in zip(("0-19", "20-39", "40-59", "60-79", "80-100"), buckets)]
    s["bucket_max"] = max(buckets) or 1
    # Sources ranked by size, each with how many of its postings are from the last 7 days. A
    # big board sitting at 0 fresh is the signal worth having here: it means that scraper is
    # returning rows but nothing NEW, which is what a silently-broken board looks like — it
    # never errors, it just stops finding things.
    s["sources"] = [{"host": h, "n": n, "fresh": host_fresh.get(h, 0)}
                    for h, n in hosts.most_common(14)]
    s["companies"] = companies.most_common(12)
    s["source_count"] = len(hosts)
    s["company_count"] = len(companies)
    c["data"], c["at"] = s, time.time()
    return s


@app.route("/admin")
@admin_required
def admin():
    """Operator dashboard: corpus health, board freshness, recent Action runs, and the same
    Update-jobs trigger the feed has. ?refresh=1 re-pulls jobs from Supabase first (the normal
    view reads the 1 h cache, so right after a scrape it would otherwise show stale counts)."""
    if request.args.get("refresh"):
        get_jobs(force=True)
        _admin_stats_cache["data"] = None
        _accounts(force=True)
        flash("Jobs reloaded.")
        return redirect(url_for("admin"))
    try:
        users = db.list_users() or []
    except Exception:
        users = []
    return render_template(
        "admin.html", stats=_admin_stats(), status=db.get_scrape_status() or {},
        runs=_gh_runs(), users=users, gh_token=bool(_gh_token()),
        gh_repo=GH_REPO, gh_workflow=GH_WORKFLOW,
        admin_mode=("ADMIN_USERS" if _ADMIN_USERS else "sole-account"))


# ---- /admin/data — storage, growth, health ----------------------------------
# Supabase's free tier caps the database at 500 MB. Nothing in this app has ever measured its
# own size, and the only retention is age-based (PRUNE_DAYS, applied on every scrape), never
# size-triggered — so the first warning of a full database would have been writes failing.
_FREE_TIER_BYTES = 500 * 1024 * 1024
_SIZE_HISTORY_KEY = "db_size_history"
_SIZE_HISTORY_MAX = 90          # ~3 months of daily points; the blob stays a few KB

# Fallback list for the degraded panel shown when the db_stats RPC isn't installed. When the
# RPC IS available we count whatever tables IT reports instead (see _admin_db) — a fixed list
# here silently showed "—" for every table added after it was written, which is exactly what
# happened to events/admin_audit/blocked_companies the day they were created.
#
# brain_companies is deliberately absent: it was never migrated to Supabase and lives only in
# brain_companies_local.json, so counting it always yields None. The health checks say so
# explicitly rather than leaving a permanent blank row here.
_COUNTED_TABLES = ("jobs", "users", "user_jobs", "applications", "profiles",
                   "resumes", "tailored_cache", "learned_answers", "boards")
# Every table keyed by username, for the orphan check. db.delete_user() historically removed
# only user_jobs + users, so anything else here can hold rows belonging to a deleted account.
_USER_SCOPED_TABLES = ("user_jobs", "profiles", "applications", "resumes", "learned_answers")


def _record_db_size(nbytes):
    """Append one {date, bytes} sample, at most once a day.

    The date gate is load-bearing: without it an admin refreshing the page twenty times fills
    the window with same-day points and flattens the slope to nothing. Stored in the existing
    scrape_status table via put_kv — it is already (id, data jsonb, updated_at), so the history
    needs no migration of its own."""
    if not nbytes:
        return
    try:
        hist = db.get_kv(_SIZE_HISTORY_KEY) or {}
        samples = [s for s in (hist.get("samples") or []) if isinstance(s, dict)]
        today = datetime.date.today().isoformat()
        if samples and samples[-1].get("d") == today:
            return
        samples.append({"d": today, "b": int(nbytes)})
        db.put_kv(_SIZE_HISTORY_KEY, {"samples": samples[-_SIZE_HISTORY_MAX:]})
    except Exception:
        pass


def _size_projection(samples):
    """Least-squares MB/day over the size history, and when that reaches the free-tier cap.

    Returns a dict with a `verdict` the template renders verbatim. The flat case gets its own
    wording on purpose: prune_old_jobs runs at the end of every scrape, so a corpus in a steady
    state is the EXPECTED answer, and extrapolating noise into a scary date is how a tile stops
    being believed."""
    pts = [(i, s.get("b") or 0) for i, s in enumerate(samples) if isinstance(s, dict)]
    if len(pts) < 3:
        need = 3 - len(pts)
        return {"state": "cold", "verdict": "Collecting data. %d more daily sample%s needed."
                % (need, "" if need == 1 else "s")}
    try:
        span = (datetime.date.fromisoformat(samples[-1]["d"])
                - datetime.date.fromisoformat(samples[0]["d"])).days
    except Exception:
        span = len(pts) - 1
    if span < 7:
        return {"state": "cold", "verdict": "Collecting data. %d days so far, 7 needed for a "
                "trend." % max(span, 1)}
    n = len(pts)
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    denom = sum((p[0] - mx) ** 2 for p in pts)
    slope = (sum((p[0] - mx) * (p[1] - my) for p in pts) / denom) if denom else 0.0
    per_day = slope * (n - 1) / float(span) if span else 0.0     # samples/day -> bytes/day
    cur = pts[-1][1]
    if per_day <= 0:
        return {"state": "flat", "per_day_mb": per_day / 1048576.0,
                "verdict": "Flat or shrinking. The %s-day prune is keeping up."
                           % os.environ.get("PRUNE_DAYS", "30")}
    days_left = (_FREE_TIER_BYTES - cur) / per_day
    if days_left <= 0:
        return {"state": "over", "per_day_mb": per_day / 1048576.0,
                "verdict": "Already over the 500 MB free-tier cap."}
    when = datetime.date.today() + datetime.timedelta(days=min(int(days_left), 3650))
    return {"state": "growing", "per_day_mb": per_day / 1048576.0, "days_left": int(days_left),
            "verdict": "+%.1f MB/day · reaches 500 MB around %s (%d days)"
                       % (per_day / 1048576.0, when.isoformat(), int(days_left))}


_ADMIN_DB_TTL = 300
_admin_db_cache = {"data": None, "at": 0.0}


def _admin_db(force=False):
    """Sizes (one RPC) + exact row counts (one HEAD each). Cached 5 minutes — an /admin/data
    load is ~10 round trips and none of these numbers move minute to minute."""
    c = _admin_db_cache
    if not force and c["data"] is not None and time.time() - c["at"] < _ADMIN_DB_TTL:
        return c["data"]
    stats = db.db_stats() or {}
    nbytes = int(stats.get("db_bytes") or 0)
    if nbytes:
        _record_db_size(nbytes)
    hist = (db.get_kv(_SIZE_HISTORY_KEY) or {}).get("samples") or []
    # Count exactly the tables the RPC found, so a table added later appears with a real count
    # instead of a dash. Falls back to the fixed list only when the RPC isn't installed.
    named = [row.get("table") for row in (stats.get("tables") or []) if row.get("table")]
    counts = {t: db.table_count(t) for t in (named or _COUNTED_TABLES)}
    # est_rows is autovacuum's estimate and is -1 on a never-analyzed table; it is shown only
    # next to the real count so a big divergence is visible as "stats are stale", never alone.
    tables = []
    for row in (stats.get("tables") or []):
        name = row.get("table") or ""
        tables.append({"name": name, "pretty": row.get("pretty") or "",
                       "total": int(row.get("total_bytes") or 0),
                       "table": int(row.get("table_bytes") or 0),
                       "index": int(row.get("index_bytes") or 0),
                       "toast": int(row.get("toast_bytes") or 0),
                       "est_rows": int(row.get("est_rows") or 0),
                       "rows": counts.get(name)})
    out = {"have_rpc": bool(stats), "sql": db.DB_STATS_SQL,
           "bytes": nbytes, "pretty": stats.get("db_pretty") or "",
           "pct": (100.0 * nbytes / _FREE_TIER_BYTES) if nbytes else 0.0,
           "cap_mb": _FREE_TIER_BYTES // 1048576,
           "tables": tables, "counts": counts,
           "history": hist, "projection": _size_projection(hist)}
    c["data"], c["at"] = out, time.time()
    return out


def _check(name, ok, detail, fix="", warn=False):
    return {"name": name, "state": "pass" if ok else ("warn" if warn else "fail"),
            "detail": detail, "fix": fix}


def _health_checks():
    """Pass/fail rows over the corpus and the account tables. Runs on demand (a button), not on
    page render — it costs ~8 round trips plus a walk of the cached job rows."""
    out = []
    jobs = get_jobs()
    total = len(jobs)
    stats = _admin_stats()

    jd = db.table_count("jobs", {"jd": "not.is.null"})
    if jd is None:
        out.append(_check("JD coverage", False, "Couldn't read the count.", warn=True))
    else:
        pct = (100.0 * jd / total) if total else 0
        out.append(_check("JD coverage", pct >= 60,
                          "%s of %s jobs have a stored description (%.0f%%)."
                          % ("{:,}".format(jd), "{:,}".format(total), pct),
                          "Raise SCORE_MAX_FETCH or SCORE_BUDGET_MIN in scrape.yml. A job with "
                          "no JD scores 0 and is invisible to the match filter."))

    pend = stats["pending"]
    out.append(_check("Scored rows", total and (100.0 * pend / total) <= 30,
                      "%s of %s unscored (%.0f%%)." % ("{:,}".format(pend), "{:,}".format(total),
                                                       (100.0 * pend / total) if total else 0),
                      "Same fix as JD coverage. Unscored is almost always JD-pending."))

    # Same posting reaching us from two hosts. _dupe_key is the feed's own identity function, so
    # this counts exactly what the feed already hides but the database still pays to store.
    groups = {}
    for j in jobs:
        k = _dupe_key(j)
        if k:
            groups.setdefault(k, set()).add(_host(j))
    dupes = sum(1 for hosts in groups.values() if len(hosts) > 1)
    out.append(_check("Cross-host duplicates", total and (100.0 * dupes / total) <= 3,
                      "%s postings appear on 2+ hosts (%.1f%%)."
                      % ("{:,}".format(dupes), (100.0 * dupes / total) if total else 0),
                      "Run python -m scraper.dedupe_urls"))

    # Rows with no date in ANY of the three columns row_age_date checks. stale_urls() can never
    # touch these, so they are a permanent storage leak — distinct from the overview's "undated"
    # tile, which only means the employer published no date.
    undated = sum(1 for j in jobs if not db.row_age_date(j))
    out.append(_check("Prunable rows", undated == 0,
                      "%s rows have no date at all and can never be pruned."
                      % "{:,}".format(undated),
                      "These need first_seen backfilled; the column defaults to current_date "
                      "for new rows only.", warn=True))

    # Any source big enough to rank in stats["sources"] (the top 14 by volume) yet contributing
    # nothing in a week. Deliberately NOT an absolute row threshold: the first version used
    # ">500 rows" and sailed past Tesla at 452 — the one genuinely dead board in this corpus.
    # Ranking scales with the corpus; a hand-picked number only ever fits the day it was chosen.
    stale = [s for s in stats["sources"] if s["fresh"] == 0]
    out.append(_check("Source freshness", not stale,
                      ("Every major source has fresh postings." if not stale else
                       "%s of the top %s sources returned nothing new in 7 days: %s"
                       % (len(stale), len(stats["sources"]),
                          ", ".join("%s (%s rows)" % (s["host"], "{:,}".format(s["n"]))
                                    for s in stale[:4]))),
                      "Those scrapers still return rows but find nothing new. Check the parser."))

    # Orphans: rows keyed to a username that no longer exists. db.delete_user() removed only
    # user_jobs + users, so every other table here can hold them.
    try:
        names = [u.get("username") or "" for u in (db.list_users() or [])]
    except Exception:
        names = []
    orphans = {}
    if names:
        for t in _USER_SCOPED_TABLES:
            n = db.table_count(t, {"username": "not." + db._in_list(names)})
            if n:
                orphans[t] = n
    out.append(_check("Orphaned rows", not orphans,
                      ("No rows belong to a deleted account." if not orphans else
                       ", ".join("%s: %s" % (t, n) for t, n in orphans.items())),
                      "delete_user() only cleared user_jobs + users; the rest was left behind."))

    tc = db.table_count("tailored_cache")
    out.append(_check("Unbounded tables", tc is not None and tc <= 5000,
                      "tailored_cache holds %s rows and has no expiry anywhere in the codebase."
                      % ("{:,}".format(tc) if tc is not None else "?"),
                      "Needs an age-based prune; nothing deletes from it today.", warn=True))

    out.append(_check("brain_companies table", db.table_count("brain_companies") is not None,
                      "Not present in Supabase. Resume Brain's company cache is local-file only, "
                      "so it is empty on the deployed app and not shared between machines.",
                      "Run the create-table SQL in BRAIN_SETUP.md.", warn=True))

    out.append(_check("APP_SECRET", bool(os.environ.get("APP_SECRET")),
                      ("Set." if os.environ.get("APP_SECRET") else
                       "Unset. The session key is derived from the Supabase key instead, so "
                       "rotating that key silently logs everyone out and invalidates every "
                       "extension token."),
                      "Set APP_SECRET in the cPanel .env."))

    secure = os.environ.get("SESSION_COOKIE_SECURE", "").lower() in ("1", "true", "yes")
    out.append(_check("Secure cookies", secure,
                      "SESSION_COOKIE_SECURE is %s." % ("on" if secure else "off"),
                      "Set SESSION_COOKIE_SECURE=1 in production so the session cookie can't "
                      "leak over http.", warn=True))
    return out


_ADMIN_HEALTH_TTL = 300
_admin_health_cache = {"data": None, "at": 0.0}


@app.route("/admin/data")
@admin_required
def admin_data():
    """Storage against the free-tier cap, growth trend, and corpus/account health checks."""
    if request.args.get("refresh"):
        get_jobs(force=True)
        _admin_stats_cache["data"] = None
        _admin_db_cache["data"] = None
        _admin_health_cache["data"] = None
        flash("Jobs reloaded.")
        return redirect(url_for("admin_data"))
    return render_template("admin_data.html", dbi=_admin_db(), stats=_admin_stats(),
                           health=_admin_health_cache["data"], blocked=db.list_blocked(),
                           audit=db.list_audit(20), delete_max=ADMIN_DELETE_MAX)


@app.route("/admin/health.json")
@admin_required
def admin_health():
    """Run the checks on demand. Behind a button rather than the page render because it costs
    ~8 round trips; cached so a double-click doesn't pay twice."""
    c = _admin_health_cache
    if c["data"] is None or time.time() - c["at"] > _ADMIN_HEALTH_TTL:
        c["data"], c["at"] = _health_checks(), time.time()
    return {"checks": c["data"]}


# ---- /admin/usage — behaviour, from the data that already exists ------------
# Everything here is derived from user_jobs (current like/hide/apply state) and applications
# (which carries a real created_at). Two limits are structural and are stated on the page
# rather than hidden: user_jobs has NO timestamp, so there is no "when" and no history — a
# like that was later undone left no trace; and jobs.match_score is re-derived on every scoring
# run, so a score shown next to a like is today's score, not the score at the moment of the click.
_ADMIN_USAGE_TTL = 300
_admin_usage_cache = {"data": None, "at": 0.0}

_STATUSES = ("liked", "applied", "hidden")

# Title words too common to carry signal. Everything else is fair game — the point of the
# token lift is to surface the words nobody thought to filter on.
_TITLE_STOP = frozenset("""
a an and at by for from in of on or the to with new senior sr jr junior lead i ii iii iv
engineer manager analyst specialist associate developer director intern
""".split())


def _median(vals):
    s = sorted(vals)
    n = len(s)
    if not n:
        return 0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _all_user_jobs():
    """Every (username, url, status) row. 126 rows today — one paged GET, no per-user fan-out."""
    try:
        return db._fetch_all(db.USERJOBS_TABLE, {"select": "username,url,status"})
    except Exception:
        return []


def _all_applications():
    """Every application row across all users, newest first."""
    try:
        r = db._http.get(db._rest(db.APPLICATIONS_TABLE), headers=db._headers(),
                         params={"select": "*", "order": "created_at.desc"}, timeout=30)
        return r.json() if r.status_code < 400 else []
    except Exception:
        return []


def _rate_table(counter_by_key, min_events, limit=15):
    """{key: {liked,applied,hidden}} -> rows sorted by hide rate, for the auto-filter candidates.
    `min_events` guards against a single hide on a single posting reading as a 100% verdict."""
    rows = []
    for key, c in counter_by_key.items():
        n = c["liked"] + c["applied"] + c["hidden"]
        if n < min_events:
            continue
        rows.append({"key": key, "n": n, "liked": c["liked"], "applied": c["applied"],
                     "hidden": c["hidden"], "hide_pct": 100.0 * c["hidden"] / n})
    rows.sort(key=lambda r: (-r["hide_pct"], -r["n"]))
    return rows[:limit]


def _admin_usage(force=False):
    """Behaviour rollup. Reads two small tables plus the already-warm job cache, so this costs
    no more than a couple of round trips; the 5-minute cache is for the Python walk, not the IO."""
    c = _admin_usage_cache
    if not force and c["data"] is not None and time.time() - c["at"] < _ADMIN_USAGE_TTL:
        return c["data"]

    jobs = get_jobs()
    by_url = {j.get("url"): j for j in jobs if j.get("url")}
    flags = _all_user_jobs()
    apps = _all_applications()

    def _blank():
        return {"liked": 0, "applied": 0, "hidden": 0}

    per_user = collections.defaultdict(_blank)
    per_company = collections.defaultdict(_blank)
    per_host = collections.defaultdict(_blank)
    scores = {s: [] for s in _STATUSES}
    liked_company = collections.Counter()
    per_user_company = collections.defaultdict(collections.Counter)
    hidden_tokens = collections.Counter()
    matched = 0

    for row in flags:
        st = (row.get("status") or "").strip()
        if st not in _STATUSES:
            continue
        user, url = row.get("username") or "?", row.get("url") or ""
        per_user[user][st] += 1
        # Host comes straight off the URL, so this breakdown is COMPLETE — it still counts a
        # posting whose job row the 30-day pruner has since removed. The company breakdown
        # below needs the join and therefore can't be, which is why coverage is reported.
        h = _host({"url": url})
        if h:
            per_host[h][st] += 1
        j = by_url.get(url)
        if not j:
            continue
        matched += 1
        co = (j.get("company") or "").strip()
        if co:
            per_company[co][st] += 1
            if st == "liked":
                liked_company[co] += 1
            per_user_company[user][co] += 1
        try:
            sc = int(j.get("match_score") or 0)
        except Exception:
            sc = 0
        if sc > 0:
            scores[st].append(sc)
        if st == "hidden":
            for tok in re.split(r"[^a-z0-9+#]+", (j.get("title") or "").lower()):
                if len(tok) > 2 and tok not in _TITLE_STOP:
                    hidden_tokens[tok] += 1

    # Title-token lift: how much more often a word appears in what someone hid than in the
    # corpus at large. A word at 3x+ is a filter or a scoring penalty waiting to be written.
    corpus_tokens = collections.Counter()
    for j in jobs:
        for tok in set(re.split(r"[^a-z0-9+#]+", (j.get("title") or "").lower())):
            if len(tok) > 2 and tok not in _TITLE_STOP:
                corpus_tokens[tok] += 1
    hid_total = sum(1 for r in flags if (r.get("status") or "") == "hidden") or 1
    cor_total = len(jobs) or 1
    token_lift = []
    for tok, n in hidden_tokens.most_common(120):
        if n < 3:
            continue
        base = corpus_tokens.get(tok, 0) / float(cor_total)
        if base <= 0:
            continue
        token_lift.append({"tok": tok, "n": n, "lift": (n / float(hid_total)) / base,
                           "corpus": corpus_tokens.get(tok, 0)})
    token_lift.sort(key=lambda r: -r["lift"])

    # Score distribution per action. This is the first honest read on whether the scoring engine
    # predicts anything: if applied and hidden sit on the same median, the number is noise.
    score_summary = []
    for st in _STATUSES:
        v = scores[st]
        score_summary.append({"status": st, "n": len(v), "median": _median(v),
                              "mean": (sum(v) / float(len(v))) if v else 0})

    # Corpus supply vs revealed demand, both as 5 buckets, normalised to percentages so a
    # 19k-row corpus and a 126-row flag set are comparable on the same axis.
    def _hist(vals):
        b = [0] * 5
        for v in vals:
            b[min(int(v), 100) // 20] += 1
        tot = sum(b) or 1
        return [100.0 * x / tot for x in b]

    corpus_scores = []
    for j in jobs:
        try:
            s = int(j.get("match_score") or 0)
        except Exception:
            s = 0
        if s > 0:
            corpus_scores.append(s)
    supply_demand = {"labels": ["0-19", "20-39", "40-59", "60-79", "80-100"],
                     "supply": _hist(corpus_scores),
                     "demand": _hist(scores["liked"] + scores["applied"])}

    # ---- applications: the only real timeline in the product ----
    by_day = collections.Counter()
    by_hour = collections.Counter()
    by_dow = collections.Counter()
    outcomes = collections.Counter()
    resumes_used = collections.Counter()
    apps_per_user = collections.Counter()
    auto_logged = 0
    for a in apps:
        u = a.get("username") or "?"
        apps_per_user[u] += 1
        outcomes[(a.get("status") or "applied").strip() or "applied"] += 1
        resumes_used[(a.get("resume_name") or "(none)").strip() or "(none)"] += 1
        created = str(a.get("created_at") or "")
        if len(created) >= 10:
            by_day[created[:10]] += 1
            try:
                d = datetime.date.fromisoformat(created[:10])
                by_dow[d.strftime("%a")] += 1
            except Exception:
                pass
        # db._now() writes "%Y-%m-%d %H:%M"; Supabase's own default is ISO with a T.
        if len(created) >= 13:
            hh = created[11:13]
            if hh.isdigit():
                by_hour[int(hh)] += 1
        # _autolog_application writes applied_date == the creation day with no notes; a row the
        # user typed themselves almost always differs on one of those.
        if (a.get("applied_date") or "")[:10] == created[:10] and not (a.get("notes") or "").strip():
            auto_logged += 1

    days = sorted(by_day.items())[-30:]
    users = sorted(per_user.items(), key=lambda kv: -(kv[1]["liked"] + kv[1]["applied"]))

    # Features nobody uses. Stated outright rather than left to be inferred from a table of
    # zeros — "0% hide rate" across every company reads like a broken query, when what it
    # actually means is that the Hide button has never been pressed. That is the more useful
    # fact and it is the one the page should say.
    totals = collections.Counter()
    for v in per_user.values():
        for st in _STATUSES:
            totals[st] += v[st]
    acted = sum(totals.values())
    dead = []
    if acted and not totals["liked"]:
        dead.append({"what": "Save",
                     "detail": "Never used. All %s recorded actions are applies." % acted,
                     "sowhat": "The card button said Save and the tab said Liked, so anyone "
                               "who saved a job had no tab by that name to find it in. The "
                               "tab was renamed to Saved on 2026-08-09. Watch this row for "
                               "two weeks before deciding the feature is dead, because until "
                               "now it has not had a fair test."})
    if acted and not totals["hidden"]:
        dead.append({"what": "Hide",
                     "detail": "Never used. No job has been hidden by anyone.",
                     "sowhat": "This is why the auto-filter tables below are empty. Hiding was "
                               "meant to be the signal that teaches the feed what to stop "
                               "showing; with none of it, the filters are the only control."})
    if apps and auto_logged == len(apps):
        dead.append({"what": "Manual application entry",
                     "detail": "All %s applications were auto-logged from the feed; none were "
                               "typed in." % len(apps),
                     "sowhat": "The add/edit form on /applications is unused. Good news for the "
                               "feed, because it means people really do apply from here."})
    advanced = sum(n for s, n in outcomes.items() if s not in ("applied", "saved"))
    if apps and not advanced:
        dead.append({"what": "Outcome tracking",
                     "detail": "All %s applications are still at 'applied'. Nothing has been "
                               "moved to assessment, interview, offer or rejected." % len(apps),
                     "sowhat": "The one metric that would measure real-world success is not "
                               "being fed. Worth a nudge in the digest, or dropping the "
                               "statuses entirely."})

    out = {
        "dead": dead, "totals": dict(totals),
        "flag_rows": len(flags), "app_rows": len(apps),
        "coverage": (100.0 * matched / len(flags)) if flags else 0.0,
        "per_user": [{"user": u, "liked": v["liked"], "applied": v["applied"],
                      "hidden": v["hidden"],
                      "top": per_user_company[u].most_common(5)} for u, v in users],
        "top_liked": liked_company.most_common(15),
        "hide_company": _rate_table(per_company, min_events=3),
        "hide_host": _rate_table(per_host, min_events=5),
        "token_lift": token_lift[:15],
        "scores": score_summary,
        "supply_demand": supply_demand,
        "apps_per_user": apps_per_user.most_common(),
        "outcomes": [(s, outcomes.get(s, 0)) for s in APP_STATUSES if outcomes.get(s)],
        "by_day": days, "by_day_max": max([n for _, n in days] or [1]),
        "by_hour": [(h, by_hour.get(h, 0)) for h in range(24)],
        "by_hour_max": max(list(by_hour.values()) or [1]),
        "by_dow": [(d, by_dow.get(d, 0)) for d in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")],
        # Weekday totals need their own scale — sizing those bars by the busiest single DAY
        # makes a full week of applications look like a quiet one.
        "by_dow_max": max(list(by_dow.values()) or [1]),
        "resumes": resumes_used.most_common(8),
        "auto_logged": auto_logged,
    }
    c["data"], c["at"] = out, time.time()
    return out


@app.route("/admin/usage")
@admin_required
def admin_usage():
    """What the three accounts actually do, from data that already exists. No tracking code —
    and therefore no history: see the caveats rendered at the top of the page."""
    if request.args.get("refresh"):
        get_jobs(force=True)
        _admin_usage_cache["data"] = None
        _admin_ev_cache["data"] = None
        flash("Jobs reloaded.")
        return redirect(url_for("admin_usage"))
    return render_template("admin_usage.html", u=_admin_usage(), ev=_admin_ev(),
                           evstats=analytics.stats())


_ADMIN_EV_TTL = 300
_admin_ev_cache = {"data": None, "at": 0.0}
_EV_WINDOW = 30


def _admin_ev(force=False):
    """Tracked-behaviour panels, entirely from the ev_usage RPC.

    Never pages the events table: PostgREST returns 1000 rows a request, so 60k events is 60
    sequential round trips from shared cPanel for numbers Postgres computes in one. `have` is
    False until SUPABASE_EVENTS_MIGRATION.sql is run, and the page says so rather than showing
    a wall of zeroes that reads like the feature is broken.
    """
    c = _admin_ev_cache
    if not force and c["data"] is not None and time.time() - c["at"] < _ADMIN_EV_TTL:
        return c["data"]
    raw = db.ev_usage(_EV_WINDOW) or {}
    out = {"have": bool(raw), "days": _EV_WINDOW, "sql_file": db.EVENTS_SQL_FILE}
    if raw:
        f = raw.get("funnel") or {}
        shown, opens = f.get("shown") or 0, f.get("opens") or 0
        out.update({
            "events": raw.get("events") or 0, "sessions": raw.get("sessions") or 0,
            "users": raw.get("users") or 0,
            "by_event": sorted((raw.get("by_event") or {}).items(), key=lambda kv: -kv[1]),
            "routes": raw.get("by_route") or [],
            "funnel": f,
            "ctr": (100.0 * opens / shown) if shown else 0.0,
            "open_apply": (100.0 * (f.get("applies") or 0) / opens) if opens else 0.0,
            "by_score": raw.get("by_score") or [],
            "top_co": raw.get("top_co") or [],
            "filters": sorted((raw.get("filters") or {}).items(), key=lambda kv: -kv[1]),
            "zero_q": raw.get("zero_q") or [],
        })
        # Filters nobody has touched in the window. The point of the panel is the ABSENCE —
        # a filter at zero is UI debt, and only the full DEFAULT_PREFS list reveals it.
        seen = {k for k, _ in out["filters"]}
        out["unused_filters"] = [k for k in core.DEFAULT_PREFS
                                 if k not in seen and k not in ("alerts", "alert_min")]
        # Routes with no page_view at all. Every route reports itself via the after_request
        # hook, so silence here is evidence rather than an oversight.
        hit = {r.get("ep") for r in out["routes"]}
        out["cold_routes"] = sorted(
            rule.endpoint for rule in app.url_map.iter_rules()
            if rule.endpoint not in hit and "GET" in (rule.methods or ())
            and not rule.rule.startswith(("/api/", "/static"))
            and rule.endpoint not in ("static", "healthz"))[:20]
    c["data"], c["at"] = out, time.time()
    return out


# ---- /admin/users — account management --------------------------------------
# The operations mirror manage_users.py exactly (get_user existence check -> create_user /
# set_user_password / delete_user with auth.hash_password); this is the same logic behind a
# form, not a second implementation of it.
#
# db.get_user() interpolates the username straight into a PostgREST `eq.` filter without
# escaping, so a name containing a comma, quote or paren produces a nonsense filter rather
# than a lookup. Cheaper to refuse those at the door than to fix every call site.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{2,40}$")
_MIN_PASSWORD = auth.MIN_PASSWORD_LEN     # one definition, shared with manage_users.py


def _can_disable():
    """Whether SUPABASE_ADMIN_MIGRATION.sql has been run. list_users' select ladder drops the
    admin columns when they don't exist, so their absence from a row is the signal."""
    m = _accounts() or {}
    for row in m.values():
        return "disabled_at" in row
    return False


def _admin_user_guard(target, action):
    """Shared refusals for every mutating user route. Returns a reason, or "" to proceed."""
    me = session.get("user") or ""
    if not _USERNAME_RE.match(target or ""):
        return "That username isn't valid (2-40 chars: letters, digits, dot, dash, underscore)."
    if not (_accounts() or {}).get(target):
        return "No such account: %s" % target
    if action in ("disable", "delete") and target == me:
        # One click would otherwise cost the only admin their own access, with no way back in
        # short of the CLI.
        return "You can't %s your own account." % action
    if action == "delete" and len(_accounts() or {}) <= 1:
        # Zero accounts is unrecoverable: with none left, _sole_user() returns "" and — unless
        # ADMIN_USERS names someone who no longer exists — nobody can reach this page again.
        return "That's the last account. Deleting it would lock everyone out for good."
    return ""


@app.route("/admin/users")
@admin_required
def admin_users():
    rows = []
    for name, row in sorted((_accounts() or {}).items(), key=lambda kv: kv[1].get("created_at") or ""):
        rows.append({"username": name,
                     "created_at": (row.get("created_at") or "")[:16].replace("T", " "),
                     "disabled_at": (row.get("disabled_at") or "")[:16].replace("T", " ")
                                    if row.get("disabled_at") else "",
                     "epoch": row.get("token_epoch") or 0,
                     "is_admin": is_admin(name),
                     "is_you": name == session.get("user")})
    return render_template("admin_users.html", users=rows, can_disable=_can_disable(),
                           admin_mode=("ADMIN_USERS" if _ADMIN_USERS else "sole-account"),
                           min_password=_MIN_PASSWORD)


@app.route("/admin/user/create", methods=["POST"])
@admin_required
def admin_user_create():
    name = (request.form.get("username") or "").strip()
    pw = request.form.get("password") or ""
    if not _USERNAME_RE.match(name):
        flash("That username isn't valid (2-40 chars: letters, digits, dot, dash, underscore).")
    elif auth.password_problem(pw):
        flash(auth.password_problem(pw))
    elif db.get_user(name):
        flash("User '%s' already exists. Use Reset password instead." % name)
    else:
        ok, msg = db.create_user(name, auth.hash_password(pw))
        flash("Created '%s'. They can sign in now." % name if ok else msg)
        _accounts(force=True)
    return redirect(url_for("admin_users"))


@app.route("/admin/user/password", methods=["POST"])
@admin_required
def admin_user_password():
    name = (request.form.get("username") or "").strip()
    pw = request.form.get("password") or ""
    bad = _admin_user_guard(name, "reset")
    if bad:
        flash(bad)
    elif auth.password_problem(pw):
        flash(auth.password_problem(pw))
    else:
        try:
            db.set_user_password(name, auth.hash_password(pw))
            _resume_cache.pop(name, None)          # the cached résumé was keyed to the old login
            flash("Password reset for '%s'." % name)
        except Exception as e:
            flash("Couldn't reset that password: %s" % e)
    return redirect(url_for("admin_users"))


@app.route("/admin/user/disable", methods=["POST"])
@admin_required
def admin_user_disable():
    name = (request.form.get("username") or "").strip()
    on = (request.form.get("on") or "") in ("1", "true", "yes", "on")
    bad = _admin_user_guard(name, "disable" if on else "enable")
    if bad:
        flash(bad)
    else:
        try:
            db.set_user_disabled(name, on)
            if on:
                # Disabling without this leaves the extension token working, which is the
                # larger of the two doors — it is CORS-open and needs no cookie.
                db.bump_token_epoch(name)
            _accounts(force=True)
            flash("%s '%s'.%s" % ("Disabled" if on else "Re-enabled", name,
                                  " Their session ends on their next request and their "
                                  "extension token is revoked." if on else
                                  " They'll need a new extension token from their profile."))
        except Exception as e:
            flash("Couldn't change that. Has SUPABASE_ADMIN_MIGRATION.sql been run? (%s)" % e)
    return redirect(url_for("admin_users"))


@app.route("/admin/user/revoke_token", methods=["POST"])
@admin_required
def admin_user_revoke_token():
    name = (request.form.get("username") or "").strip()
    bad = _admin_user_guard(name, "revoke")
    if bad:
        flash(bad)
    else:
        try:
            db.bump_token_epoch(name)
            _accounts(force=True)
            flash("Revoked '%s' extension tokens. They can copy a new one from their profile."
                  % name)
        except Exception as e:
            flash("Couldn't revoke that. Has SUPABASE_ADMIN_MIGRATION.sql been run? (%s)" % e)
    return redirect(url_for("admin_users"))


@app.route("/admin/user/delete", methods=["GET", "POST"])
@admin_required
def admin_user_delete():
    """GET previews (counts only, nothing removed); POST applies and requires the username
    typed back. Two routes' worth of behaviour on one rule, but the split that matters —
    no GET can delete — is preserved: the GET branch never calls delete_user without dry_run.
    """
    name = (request.args.get("username") or request.form.get("username") or "").strip()
    bad = _admin_user_guard(name, "delete")
    if bad:
        flash(bad)
        return redirect(url_for("admin_users"))

    try:
        counts = db.delete_user(name, dry_run=True)
    except Exception as e:
        flash("Couldn't read what that would delete: %s" % e)
        return redirect(url_for("admin_users"))

    if request.method == "GET":
        return render_template("admin_confirm.html", name=name, counts=counts,
                               total=sum(counts.values()))

    if (request.form.get("confirm") or "").strip() != name:
        flash("Type the username exactly to confirm.")
        return redirect(url_for("admin_user_delete", username=name))
    try:
        removed = db.delete_user(name)
    except Exception as e:
        flash("Delete failed partway. The account was left in place. (%s)" % e)
        return redirect(url_for("admin_users"))
    _accounts(force=True)
    _resume_cache.pop(name, None)
    _status_cache.pop(name, None)
    _profile_cache.pop(name, None)
    flash("Deleted '%s'. Rows removed: %s" % (
        name, ", ".join("%s %d" % (t, n) for t, n in sorted(removed.items()) if n) or "none"))
    return redirect(url_for("admin_users"))


# ---- destructive data actions (/admin/data) ---------------------------------
# Hard ceiling on one web-initiated delete. Above this the route refuses and points at
# scripts/prune_stale.py, so even a bug in the preview cannot wipe the corpus from a browser.
ADMIN_DELETE_MAX = int(os.environ.get("ADMIN_DELETE_MAX", "5000") or 5000)
_PLAN_TTL = 15 * 60


def _require_supabase():
    """"" when it's safe to touch data, else the reason to refuse.

    Every db.* function silently falls through to a local *_local.json / jobs.csv when
    using_supabase() is false, and most of those files do not exist on the deployed box. So
    with Supabase briefly unreachable a delete would walk an empty local file, report
    "0 removed", and leave the real rows untouched. Reading that as "there was nothing to
    delete" is precisely how you delete the wrong thing on the retry, so destructive actions
    refuse rather than no-op. The count probe doubles as the liveness check.
    """
    if not db.using_supabase():
        return ("No Supabase credentials are configured. Refusing to run against the local-file "
                "fallback. Nothing here would touch the real database.")
    if db.table_count(db.TABLE) is None:
        return ("Can't reach Supabase right now. Refusing to run a destructive action. "
                "try again in a moment.")
    return ""


def _bust_job_caches():
    """Everything derived from the job rows, after they change under us."""
    get_jobs(force=True)
    _score_cache.clear()
    _rows_cache.clear()
    _sponsor_cache.clear()
    _admin_stats_cache["data"] = None
    _admin_usage_cache["data"] = None
    _admin_db_cache["data"] = None
    _admin_health_cache["data"] = None


def _build_plan(mode, company="", urls=()):
    """What a delete would remove, computed fresh against the current corpus.

    Flagged rows (liked / applied / hidden by anyone) are separated out and KEPT — that
    protection is not overridable from the web UI at all. The escape hatch is
    scripts/prune_stale.py --include-flagged, which needs shell access, and that is the right
    amount of friction for "delete something a user is tracking".
    """
    jobs = get_jobs()
    flagged = db.all_flagged_urls()
    if mode == "company":
        key = db.block_key(company)
        match = [j for j in jobs if key and db.block_key(j.get("company") or "") == key]
    else:
        want = {u.strip() for u in urls if u.strip()}
        match = [j for j in jobs if j.get("url") in want]
    doomed = [j for j in match if j.get("url") not in flagged]
    protected = [j for j in match if j.get("url") in flagged]
    # One employer reaches us under several labels — the Workday case-sensitivity bug stored
    # the same postings as both "Amat" and "Applied Materials". Offer every distinct string so
    # a block covers the aliases too, rather than blocking one spelling and looking broken.
    labels = collections.Counter((j.get("company") or "").strip() for j in match)
    return {"mode": mode, "company": company,
            "urls": [j.get("url") for j in doomed] if mode == "urls" else [],
            "n": len(doomed), "protected": len(protected), "matched": len(match),
            "labels": labels.most_common(),
            "samples": [{"title": (j.get("title") or "")[:70],
                         "company": (j.get("company") or "")[:40],
                         "date": db.row_age_date(j) or "None",
                         "url": j.get("url") or ""} for j in doomed[:20]],
            "after": len(jobs) - len(doomed)}


@app.route("/admin/jobs/preview", methods=["POST"])
@admin_required
def admin_jobs_preview():
    """Compute and show what a delete would remove. Never deletes anything."""
    bad = _require_supabase()
    if bad:
        flash(bad)
        return redirect(url_for("admin_data"))
    mode = "company" if (request.form.get("company") or "").strip() else "urls"
    company = (request.form.get("company") or "").strip()
    urls = [u for u in re.split(r"[\s,]+", request.form.get("urls") or "") if u.startswith("http")]
    if mode == "urls" and not urls:
        flash("Give a company name, or paste at least one job URL.")
        return redirect(url_for("admin_data"))
    plan = _build_plan(mode, company, urls[:ADMIN_DELETE_MAX])
    if not plan["n"]:
        flash("Nothing matched%s. %d row(s) matched but every one is liked/applied/hidden and "
              "is protected." % (" '%s'" % company if company else "", plan["protected"])
              if plan["matched"] else
              "Nothing matched%s." % (" '%s'" % company if company else ""))
        return redirect(url_for("admin_data"))
    if plan["n"] > ADMIN_DELETE_MAX:
        flash("That would delete %s rows, over the %s cap for a browser-initiated delete. "
              "Use scripts/prune_stale.py for something that large."
              % ("{:,}".format(plan["n"]), "{:,}".format(ADMIN_DELETE_MAX)))
        return redirect(url_for("admin_data"))

    # The plan lives in the signed session, not the database: it is ~200 bytes of filter (not
    # the URL list), it is already scoped to this admin and unforgeable, and there is no row to
    # clean up afterwards. Apply re-derives the actual URLs from the filter, so protection and
    # counts are always evaluated against the corpus as it stands at that moment.
    session["del_plan"] = {"mode": mode, "company": company, "urls": plan["urls"],
                           "n": plan["n"], "at": int(time.time())}
    confirm = company if mode == "company" else "DELETE %d JOBS" % plan["n"]
    return render_template("admin_delete_confirm.html", plan=plan, confirm=confirm)


@app.route("/admin/jobs/apply", methods=["POST"])
@admin_required
def admin_jobs_apply():
    """Delete, and optionally block. Requires all four of: a valid CSRF token (enforced in
    admin_required), an unexpired plan, the typed confirmation string, and a live Supabase."""
    bad = _require_supabase()
    if bad:
        flash(bad)
        return redirect(url_for("admin_data"))
    plan = session.get("del_plan") or {}
    if not plan or time.time() - (plan.get("at") or 0) > _PLAN_TTL:
        flash("That confirmation expired. Start again so the counts are current.")
        return redirect(url_for("admin_data"))

    fresh = _build_plan(plan["mode"], plan.get("company", ""), plan.get("urls") or [])
    expected = plan.get("company") if plan["mode"] == "company" else "DELETE %d JOBS" % plan["n"]
    if (request.form.get("confirm") or "").strip() != expected:
        flash("Type the confirmation exactly as shown.")
        return redirect(url_for("admin_data"))
    # A scrape landing between preview and apply changes what you agreed to. Refuse rather
    # than delete a different set than the one on the screen.
    if fresh["n"] != plan["n"]:
        session.pop("del_plan", None)
        flash("The corpus changed since that preview (%d rows now, %d then). Nothing was "
              "deleted. Preview again." % (fresh["n"], plan["n"]))
        return redirect(url_for("admin_data"))

    actor = session.get("user") or "?"
    target = plan.get("company") or "%d urls" % plan["n"]
    audit_id = db.audit_log(actor, "jobs.delete", target, 0,
                            {"planned": plan["n"], "protected": fresh["protected"],
                             "sample": [s["url"] for s in fresh["samples"][:10]]})
    try:
        removed = db.delete_urls(_plan_urls(fresh), remote_only=True)
    except Exception as e:
        db.audit_update(audit_id, 0, {"error": str(e)[:300]})
        flash("Delete failed: %s" % e)
        return redirect(url_for("admin_data"))
    db.audit_update(audit_id, removed)

    blocked_msg = ""
    if (request.form.get("block") or "") in ("1", "true", "yes", "on"):
        names = request.form.getlist("label") or ([plan["company"]] if plan.get("company") else [])
        added = [n for n in names if n.strip() and db.add_blocked(n, "deleted from admin", actor)]
        if added:
            db.audit_log(actor, "company.block", ", ".join(added)[:200], len(added))
            blocked_msg = (" Blocked %s from future scrapes." % ", ".join(added))
    session.pop("del_plan", None)
    _bust_job_caches()
    flash("Deleted %s job%s.%s%s" % ("{:,}".format(removed), "" if removed == 1 else "s",
                                     blocked_msg,
                                     " %d protected row(s) were kept." % fresh["protected"]
                                     if fresh["protected"] else ""))
    return redirect(url_for("admin_data"))


def _plan_urls(plan):
    """The URL list a plan resolves to right now — re-derived, never carried over from the
    preview, so flagged protection is re-evaluated against current data."""
    jobs = get_jobs()
    flagged = db.all_flagged_urls()
    if plan["mode"] == "company":
        key = db.block_key(plan.get("company") or "")
        return [j["url"] for j in jobs
                if j.get("url") and j["url"] not in flagged
                and key and db.block_key(j.get("company") or "") == key]
    want = set(plan.get("urls") or [])
    return [u for u in want if u not in flagged]


@app.route("/admin/block", methods=["POST"])
@admin_required
def admin_block():
    """Add or remove a company blocklist entry without deleting anything."""
    actor = session.get("user") or "?"
    remove = (request.form.get("remove") or "").strip()
    if remove:
        db.remove_blocked(remove)
        db.audit_log(actor, "company.unblock", remove, 1)
        flash("Unblocked. It can be scraped again from the next run.")
    else:
        name = (request.form.get("name") or "").strip()
        if not name:
            flash("Give a company name to block.")
        elif db.add_blocked(name, (request.form.get("reason") or "").strip(), actor):
            db.audit_log(actor, "company.block", name, 1)
            flash("Blocked '%s'. Existing rows stay until you delete them; no new ones will be "
                  "added." % name)
        else:
            flash("Couldn't save that. Has SUPABASE_ADMIN_MIGRATION.sql been run?")
    return redirect(url_for("admin_data"))


@app.route("/action", methods=["POST"])
@login_required
def action():
    url = request.form.get("url", "")
    status = request.form.get("status", "")          # liked|hidden|applied|'' (clear)
    user = session["user"]
    try:
        prev = user_statuses(user).get(url, "")
    except Exception:
        prev = ""
    try:
        db.set_user_status(user, url, status)
        _status_cache.pop(user, None)                # reflect the change on the next feed render
        if status == "applied":
            _autolog_application(user, url)
        # via='form' is how this route earns its keep or gets deleted: nothing in app.js, any
        # template, or the extension references it. Thirty days of zero and it can go.
        _ev_action(user, url, prev, status, "form")
    except Exception:
        flash("Couldn't save that action. Try again.")
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
            flash("Saved. Your match scores now include it.")
        except Exception:
            flash("Couldn't save. Try again.")
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
        ai_err = "No job description stored for this role yet. Open Apply to read it on the company site."
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
        return {"ok": False, "error": "No job description is stored for this role yet. Open Apply to read it on the company site."}
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
    uploaded, err = _uploaded_resume_text()
    if err:
        flash(err)
    content = uploaded or request.form.get("content", "")
    if not (content or "").strip():
        flash(err or "Nothing to save. Attach a file or paste the text.")
        return redirect(url_for("brain_teach"))
    name = (request.form.get("name") or "").strip()
    if not name and uploaded:
        # Name it after the file rather than "Untitled résumé", so a library of several
        # uploads stays tellable apart without anyone having to type a label.
        up = request.files.get("resume_file")
        name = os.path.splitext(os.path.basename(up.filename or ""))[0].strip() if up else ""
    rb.save_resume(user, {"id": request.form.get("id", ""),
                          "name": name or "Untitled résumé",
                          "content": content})
    _bust_profile(user)
    flash(("Read %d characters from that file. " % len(content) if uploaded else "")
          + "résumé saved. Your match scores now include it.")
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
    flash("Story saved. Your match scores now include it.")
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
            det = (scraper.detect_board(url) or scraper.detect_paylocity(url)
                   or scraper.detect_jibe(url)
                   or scraper.detect_phenom(url) or scraper.detect_successfactors(url)
                   or scraper.detect_linked_ats(url) or scraper.detect_jsonld(url))
            if not det:
                result = ("err", "That isn't a readable job board (Greenhouse, Lever, Ashby, "
                          "SmartRecruiters, Workday, Oracle Cloud, Workable, Phenom, iCIMS/Jibe, "
                          "SuccessFactors, UltiPro/UKG, BambooHR, Pinpoint, Rippling, "
                          "Recruitee, Breezy, Personio, or a page with embedded job data). "
                          "Add the company to sponsors.txt instead.")
            elif det[0] in {u for u, _, _ in scraper.SOURCES}:
                result = ("info", "%s is already a built-in source, so there is nothing to add." % det[2])
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
        flash("One-time setup needed. Run the SQL at the bottom of this page in Supabase, then try again.")
    elif ok:
        flash("Saved.")
    else:
        flash("Couldn't save: " + msg[:120])
    return redirect(url_for("applications"))


@app.route("/application/delete", methods=["POST"])
@login_required
def application_delete():
    try:
        db.delete_application(session["user"], request.form.get("id", ""))
        flash("Deleted.")
    except Exception:
        flash("Couldn't delete. Try again.")
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
def _ext_token(username, epoch=None):
    """A stable per-user token for the browser extension (HMAC of the username with the app
    secret). No DB storage needed; we re-derive + compare to validate.

    users.token_epoch is folded into the signed message so the token can be REVOKED — bumping
    the epoch changes the message and every token issued at the old value stops verifying.
    That is the only revocation available for a derived token.

    Epoch 0 deliberately keeps the ORIGINAL message shape ("ext:<user>", no suffix), so
    shipping this does not invalidate the tokens already pasted into installed extensions. The
    first revocation moves that user to epoch 1 and their old tokens die then, not on deploy.
    """
    if epoch is None:
        epoch = (_account_state(username) or {}).get("token_epoch") or 0
    try:
        epoch = int(epoch or 0)
    except Exception:
        epoch = 0
    msg = "ext:%s" % username if not epoch else "ext:%s|%d" % (username, epoch)
    sig = hmac.new(str(app.secret_key).encode(), msg.encode(), hashlib.sha256).hexdigest()[:32]
    return "%s:%s" % (username, sig)


def _ext_user(token):
    """Username for a valid extension token, else None.

    The HMAC is verified FIRST and the account state only afterwards, so an unauthenticated
    caller spraying tokens at the CORS-open /api/ext/* routes can never make us touch the
    account cache — only a token that already proves knowledge of the secret gets that far.
    """
    token = (token or "").strip()
    if ":" not in token:
        return None
    username = token.rsplit(":", 1)[0]
    if not (username and hmac.compare_digest(_ext_token(username), token)):
        return None
    st = _account_state(username)
    if st is None or st.get("disabled_at"):
        return None
    return username


@app.route("/profile/tracking", methods=["POST"])
@login_required
def profile_tracking():
    """The usage-tracking opt-out, on its own route rather than folded into the profile form.

    /profile's POST rebuilds every text column from the submitted fields, so a small form that
    only carried the checkbox would save empty strings over the user's name, email, address and
    the rest. Separate route, separate payload, nothing else touched.
    """
    if not _check_csrf():
        flash("That form expired. Reload and try again.")
        return redirect(url_for("profile"))
    user = session["user"]
    try:
        extra = (db.get_profile(user) or {}).get("extra")
        if isinstance(extra, str):
            extra = json.loads(extra or "{}")
        if not isinstance(extra, dict):
            extra = {}
    except Exception:
        extra = {}
    off = bool(request.form.get("ev_off"))
    extra["ev_off"] = off
    ok, msg = db.save_profile(user, {"extra": json.dumps(extra)})
    analytics._optout["at"] = 0.0            # take effect now, not in five minutes
    flash("Usage recording is now %s for your account." % ("off" if off else "on")
          if ok else "Couldn't save that: " + msg[:120])
    return redirect(url_for("profile"))


@app.route("/profile/revoke_token", methods=["POST"])
@login_required
def profile_revoke_token():
    """Let a user revoke their OWN extension tokens. Needing an admin to rotate a credential
    you leaked yourself is the kind of friction that means it doesn't get done."""
    if not _check_csrf():
        flash("That form expired. Reload and try again.")
        return redirect(url_for("profile"))
    try:
        db.bump_token_epoch(session["user"])
        _accounts(force=True)
        flash("Old tokens revoked. Paste the new one below into the extension.")
    except Exception:
        flash("Couldn't revoke that token. The database may need "
              "SUPABASE_ADMIN_MIGRATION.sql run first.")
    return redirect(url_for("profile"))


def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


# ------------------------------------------------------------------
# ONBOARDING
#
# Accounts are admin-created and there is no signup, so "first login with an empty profile" is
# the only hook available. The flag lives in profiles.extra (jsonb, already there) rather than
# a new column, so this needs no migration.
#
# Each step POSTs and advances, instead of one page that collects everything and saves at the
# end: closing the tab half way then keeps what you filled in.
#
# It must NOT post to /profile. That route rebuilds its payload as
# {k: f.get(k, "").strip() for k in text_keys} over all 39 fields, so a partial form BLANKS
# everything it doesn't contain. db.save_profile itself is safe with a partial dict — it filters
# to the keys you passed — so these steps write straight through it.
# ------------------------------------------------------------------
ONBOARD_STEPS = 4

# The question set, promoted out of the route so tests and the React entry can both assert
# against ONE definition instead of re-deriving it from rendered HTML.
#
# Why these four and nothing else. The organising question is "does this input change the
# feed?", and only four things do. The résumé is what every match score is computed against.
# Roles are the largest single cut on a 25k corpus. Sponsorship decides which signals surface.
# Location is the second most used filter in any job product.
#
# What was cut, and where it went. Eight contact fields (first/last name, email, phone,
# LinkedIn, GitHub, portfolio) exist to autofill application forms through the extension:
# nothing in the feed changes because you typed a GitHub URL, and to someone who has not
# installed the extension yet the promise is not just unpersuasive, it is meaningless. They
# move to /profile and are asked at the moment of obvious need, the first Apply click.
# The four OPT date fields drive deadline reminders, not ranking; asking a new user for four
# immigration dates before they have seen a single job asks for trust the product has not
# earned. Target companies only lift ranking and never filter.
#
# Order matters: the résumé moved from LAST to FIRST. It is the only skip that costs something
# irreversible, and at position five it sat behind sixteen low-value fields at exactly the point
# where the flow has spent four screens teaching the user that Skip is harmless.
ONBOARD_QUESTIONS = (
    {"n": 1, "key": "resume",      "title": "Add Your Résumé."},
    {"n": 2, "key": "roles",       "title": "What Kind of Work?"},
    {"n": 3, "key": "sponsorship", "title": "Do You Need Visa Sponsorship?"},
    {"n": 4, "key": "location",    "title": "Where Do You Want to Work?"},
)
# Profile columns each question is allowed to write. A step must never post a key outside its
# own list: POST /profile rebuilds all 39 text keys, so a partial form blanks everything it
# does not contain, and that is the single hazard this whole flow has to respect.
ONBOARD_STEP_FIELDS = {
    1: (),                                    # résumé is its own table, not a profile column
    2: (),                                    # roles are a search pref, not a profile column
    3: ("needs_sponsorship", "requires_sponsorship_future"),
    4: ("location",),
}
# The three answers that actually drive the feed, and what each one means for the profile.
# work_auth_status with its seven options is autofill, so it is not asked here.
SPONSORSHIP_ANSWERS = {
    "now":    {"needs_sponsorship": "Yes", "requires_sponsorship_future": "Yes"},
    "future": {"needs_sponsorship": "No",  "requires_sponsorship_future": "Yes"},
    "no":     {"needs_sponsorship": "No",  "requires_sponsorship_future": "No"},
}
# How many role families someone may target at once. A cap is a feature, not a limit: an
# unbounded pick is the same as no pick, and the filter stops meaning anything.
ROLE_PICK_MAX = 6


_role_counts_cache = {"at": 0.0, "v": None}


def role_counts():
    """{role_key: how many live postings} for the picker, cached 10 minutes.

    Shown next to each option so the choice is made against what is actually in the corpus. A
    family sitting at 0 is worth seeing too — it says the search is empty, rather than letting
    someone tick it and conclude the feed is broken.
    """
    now = time.time()
    if _role_counts_cache["v"] is not None and now - _role_counts_cache["at"] < 600:
        return _role_counts_cache["v"]
    counts = {k: 0 for k in core.ROLE_KEYS}
    try:
        for j in get_jobs():
            for k in core.roles_for_title(j.get("title")):
                counts[k] += 1
    except Exception:
        pass                                   # a picker without counts still works
    _role_counts_cache.update(at=now, v=counts)
    return counts


def _uploaded_resume_text(field="resume_file"):
    """(text, error) for an uploaded résumé, or ('', '') when no file was attached.

    The upload is a CONVENIENCE over the textarea, never a replacement: every caller falls back
    to pasted text, because a scanned PDF has nothing to extract and no amount of parsing fixes
    that. Flask's MAX_CONTENT_LENGTH rejects an oversized body before it reaches here; the size
    check in core is the second line for anything that slips past it.
    """
    try:
        f = request.files.get(field)
        if not f or not (f.filename or "").strip():
            return "", ""
        return core.resume_text_from_upload(f.filename, f.read())
    except Exception:
        return "", "Couldn't read that upload. Paste the text below instead."


def _extra(user):
    """The profile's `extra` jsonb as a dict, whatever shape it is stored in."""
    try:
        e = (db.get_profile(user) or {}).get("extra")
        if isinstance(e, str):
            e = json.loads(e or "{}")
        return e if isinstance(e, dict) else {}
    except Exception:
        return {}


def _save_extra(user, updates):
    """MERGE into extra, never replace it. db.save_profile overwrites the whole jsonb value, so
    writing {'onboarded': True} on its own would silently drop ev_off (the analytics opt-out)."""
    e = _extra(user)
    e.update(updates)
    return db.save_profile(user, {"extra": e})


def _needs_onboarding(user):
    """A genuinely EMPTY account — no contact details, no saved search, no résumé.

    All three, not just contact details. Accounts predate this wizard, and plenty of them were
    used for months without anyone typing a name: testing contact fields alone would have
    ambushed a long-standing user with a setup flow for an app they already knew. Any one of
    the three is proof the account has been used.

    The flag is checked first so finishing or skipping is final.
    """
    if _extra(user).get("onboarded"):
        return False
    prof = db.get_profile(user) or {}
    if any((prof.get(k) or "").strip()
           for k in ("first_name", "last_name", "name", "email", "phone")):
        return False
    if prof.get("search_prefs"):                 # they have saved a search
        return False
    try:
        return not (current_profile() or "").strip()      # ...or a résumé / brain story
    except Exception:
        return False                             # never block the feed on a lookup failure


@app.route("/api/onboard/resume", methods=["POST"])
@login_required
def api_onboard_resume():
    """Save a résumé and echo back what was actually read out of it.

    The echo is the point. A scanned or photographed PDF has no text to extract, and the old
    flow discovered that AFTER a redirect, as a flash message on the next screen, by which
    point the user had moved on. Reporting the parse inline, where the decision is being made,
    turns a silent failure into a correctable one and buys the cheapest trust in the product.

    Only ever reports what was genuinely detected. Years is omitted rather than guessed when
    core.experience_years finds no stated figure, because an invented number on the one screen
    that is asking the user to trust the parser would be worse than saying nothing.
    """
    if not _check_csrf():
        return {"ok": False, "error": "That form expired. Reload and try again."}, 400
    user = session["user"]
    text, err = _uploaded_resume_text()
    if not text:
        text = (request.form.get("resume") or "").strip()
    if not text:
        return {"ok": False,
                "error": err or ("That file has no text in it. A scanned or photographed "
                                 "PDF has none to extract, so paste the text instead.")}
    try:
        db.save_resume(user, {"name": "My résumé", "content": text[:60000]})
        _bust_profile(user)
    except Exception as ex:
        return {"ok": False, "error": "Couldn't save that résumé: " + str(ex)[:120]}

    low = text.lower()
    skills = sorted((k for k in core.ATS_KEYWORDS if k in low), key=lambda k: (-len(k), k))[:8]
    roles = [{"key": k, "label": lab}
             for k, lab, _g, _p in core.ROLE_FAMILIES
             if k in core.roles_for_title(text)][:ROLE_PICK_MAX]
    out = {"ok": True, "chars": len(text), "skills": skills, "roles": roles}
    yrs = core.experience_years(text)
    if yrs:
        out["years"] = yrs
    analytics.emit(user, _sid(), "resume_add",
                   via="upload" if request.files.get("resume_file") else "paste",
                   ok=True, chars=len(text))
    return out


def _onboard_rows():
    """Rows behind the location suggestions. Same source the feed uses, so the box on question
    four offers exactly what the feed's own location filter will accept."""
    try:
        return get_jobs()
    except Exception:
        return []                              # suggestions are a nicety, never a blocker


def _onboard_advance(user, step):
    """Move to the next question, or finish. onboarded is set ONLY past the last question.

    That is the whole fix to Skip: previously any skip wrote onboarded=True, so the flag meant
    "stopped" rather than "reached the end", and there was no way back in.
    """
    if step >= ONBOARD_STEPS:
        _save_extra(user, {"onboarded": True})
        return redirect(url_for("feed", welcome=1))
    return redirect(url_for("welcome", step=step + 1))


@app.route("/welcome", methods=["GET", "POST"])
@login_required
def welcome():
    user = session["user"]
    prof = db.get_profile(user) or {}
    e = _extra(user)

    if request.method == "POST":
        if not _check_csrf():
            flash("That form expired. Please fill it in again.")
            return redirect(url_for("welcome"))
        f = request.form
        try:
            step = max(1, min(ONBOARD_STEPS, int(f.get("step") or 1)))
        except ValueError:
            step = 1

        # Skip advances ONE question. It used to write onboarded=True from any step, so a
        # single click on screen one ended setup permanently, including the résumé prompt,
        # while the button said "Skip for now". That label promised a second chance the code
        # never gave. Unanswered questions are recorded so the feed can offer to finish.
        if f.get("action") == "skip":
            un = [n for n in (e.get("onboarding_unanswered") or []) if n != step]
            _save_extra(user, {"onboarding_unanswered": sorted(un + [step])})
            return _onboard_advance(user, step)

        answered = True
        if step == 1:
            # An upload wins if it produced text; otherwise fall through to whatever was
            # pasted, so a failed parse never costs the user what they typed.
            text, err = _uploaded_resume_text()
            if err:
                flash(err)
            if not text:
                text = (f.get("resume") or "").strip()
            if err and not text:
                return redirect(url_for("welcome", step=step))     # let them try again
            if text:
                try:
                    db.save_resume(user, {"name": "My résumé", "content": text[:60000]})
                    _bust_profile(user)
                except Exception as ex:
                    flash("Couldn't save that résumé: " + str(ex)[:120])
            else:
                answered = bool(current_profile())
        elif step == 2:
            # Roles are a saved-search PREF, not profile extra: they filter the feed and the
            # digest, so they have to live where every other filter lives and go through
            # normalize_prefs. Merged over the stored prefs the same way POST /profile merges
            # the alert settings, or saving here would reset the rest of the search.
            picked = ",".join(core.parse_roles_pref(f.getlist("roles") or f.get("roles")))
            db.save_profile(user, {"search_prefs": core.normalize_prefs(
                dict(_user_prefs(user), roles=picked))})
            _rows_cache.clear()                # the first-paint count is derived from prefs
            answered = bool(picked) or f.get("all_roles") == "1"
        elif step == 3:
            payload = dict(SPONSORSHIP_ANSWERS.get(f.get("sponsorship") or "", {}))
            if payload:
                ok, msg = db.save_profile(user, payload)
                if not ok:
                    flash("Couldn't save: " + msg[:120])
            answered = bool(payload)
        elif step == 4:
            loc = "" if f.get("anywhere") == "1" else (f.get("location") or "").strip()
            if loc or f.get("anywhere") == "1":
                ok, msg = db.save_profile(user, {"location": loc[:120]})
                if not ok:
                    flash("Couldn't save: " + msg[:120])
            answered = bool(loc) or f.get("anywhere") == "1"

        un = [n for n in (e.get("onboarding_unanswered") or []) if n != step]
        if not answered:
            un = sorted(un + [step])
        _save_extra(user, {"onboarding_unanswered": sorted(set(un))})
        return _onboard_advance(user, step)

    try:
        step = max(1, min(ONBOARD_STEPS, int(request.args.get("step") or 1)))
    except ValueError:
        step = 1
    cur = (prof.get("needs_sponsorship") or "").strip().lower()
    fut = (prof.get("requires_sponsorship_future") or "").strip().lower()
    sponsorship = ""
    for key, want in SPONSORSHIP_ANSWERS.items():
        if (want["needs_sponsorship"].lower() == cur
                and want["requires_sponsorship_future"].lower() == fut):
            sponsorship = key
            break
    return render_template("welcome.html", step=step, steps=ONBOARD_STEPS, prof=prof,
                           questions=ONBOARD_QUESTIONS,
                           role_groups=core.role_families_grouped(), role_counts=role_counts(),
                           role_max=ROLE_PICK_MAX,
                           chosen=core.parse_roles_pref(_user_prefs(user).get("roles")),
                           sponsorship=sponsorship,
                           metros=_feed_metros(_onboard_rows()),
                           states=_feed_states(_onboard_rows()),
                           has_resume=bool(current_profile()),
                           entry=vite_entry("src/entries/welcome.tsx"),
                           props={"csrf": csrf_token(), "hasResume": bool(current_profile())})


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
        flash("Saved." if ok else ("Couldn't save: " + msg[:120]))
        return redirect(url_for("profile"))
    try:
        prof = db.get_profile(user) or {}
    except Exception:
        prof = {}
    extra = prof.get("extra")
    if isinstance(extra, str):
        try:
            extra = json.loads(extra or "{}")
        except Exception:
            extra = {}
    return render_template("profile.html", prof=prof, token=_ext_token(user),
                           timeline=core.visa_timeline(prof),
                           ev_off=bool((extra or {}).get("ev_off")),
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
    # Same normalization the scraper stores jobs under, so a link the extension picked up off the
    # apply page (…?gh_src=, boards. vs job-boards.) lands on the SAME row the feed shows rather
    # than becoming a second, unlinked tracker entry.
    if url:
        url = scraper.canonical_url(url)
    if not (title or company):
        return _cors(jsonify({"ok": False, "error": "No job info"})), 400
    # Mark it applied in the feed too (the app's own Apply button does both). Best-effort: the
    # status is keyed by url, so it only lands when the page url matches the posting we scraped.
    if url:
        try:
            db.set_user_status(user, url, "applied")
            _status_cache.pop(user, None)
        except Exception:
            pass
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
                              "error": "No résumé found. Add one in Resume Brain first."})), 400

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
# ones (workday/oracle/icims) are included by request — you review each open tab and sign in if needed.
_FILLABLE_HOSTS = (
    "greenhouse.io", "lever.co", "ashbyhq.com", "smartrecruiters.com",
    "recruitee.com", "breezy.hr", "personio.com", "workable.com",
    "ultipro.com", "bamboohr.com", "pinpointhq.com", "rippling.com",
    "avature.net", "jobdiva.com", "myworkdayjobs.com", "myworkdaysite.com",
    "oraclecloud.com", "jibeapply.com", "icims.com", "successfactors.com",
    "phenompeople.com", "jobvite.com",
)

# Never queued, whatever the caller asks for: aggregators list a posting they don't host, so the
# page has no form to fill — following one is a redirect, not an application. (Adzuna is how the
# feed reaches employers with no public board; the apply link there belongs to someone else.)
_QUEUE_SKIP_HOSTS = ("adzuna.", "indeed.", "linkedin.", "ziprecruiter.", "glassdoor.")

# Most openings ONE employer may contribute to a batch queue. Some employers list a single role
# hundreds of times, once per site or store — Actalent 527 "Project Manager", Amazon 431
# "Operations Manager" — so without this a 50-slot queue can be one company's warehouse network.
# The FEED deliberately shows every one of those postings its own card; a runner is different,
# because there you spend a finite number of applications rather than scroll past rows.
_QUEUE_PER_COMPANY = 3


def _queue_fillable(url, wide=False):
    """Is this a page the filler should open? Known ATS always; with `wide`, any employer-hosted
    career site too. The corpus is now mostly company domains running Phenom/SuccessFactors/iCIMS
    behind a custom hostname, which no host list can enumerate — hence the opt-in wide net."""
    from urllib.parse import urlparse
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not host or any(h in host for h in _QUEUE_SKIP_HOSTS):
        return False
    if any(h in host for h in _FILLABLE_HOSTS):
        return True
    # PeopleSoft runs on each institution's own hostname (jobs.omni.fsu.edu), so no host list
    # can catch it — its component name in the path is the reliable tell. Account-walled like
    # Workday, and included on the same terms: the tab opens, you sign in, the filler fills.
    if "HRS_HRAM_FL" in (url or ""):
        return True
    return bool(wide)


@app.route("/api/ext/apply_queue", methods=["GET", "OPTIONS"])
def ext_apply_queue():
    """Extension batch filler -> the jobs this user's FEED would show, narrowed to pages the
    filler can actually fill.

    Built from the feed's own pipeline rather than re-derived from the raw table, so the queue
    can't disagree with the app: `ranked_rows` gives the personalized match score and collapses
    the same-posting-two-hosts duplicates, the user's SAVED SEARCH decides what qualifies (match
    floor, visa routes, location, pay, dev/mgmt track, staffing agencies, posting age), and
    closed postings are dropped.

    One thing here is deliberately NOT the feed's behaviour: the queue caps how many openings a
    single employer may contribute. The feed used to collapse employer runs into a "+N more"
    tile and this route reused that; the feed now shows every posting its own card, but a RUNNER
    is different from a list — without a cap "fill my latest matches" spends all 50 slots on 50
    copies of one Amazon opening. Hence the local cap below, which is not mirrored anywhere.

    Query args: `limit` (<=200), `sort=newest|score` (default newest — the runner wants the
    freshest postings), `all=1` to include employer career domains beyond the tuned ATS list.
    """
    from flask import jsonify
    if request.method == "OPTIONS":
        return _cors(app.make_response(("", 204)))
    user = _ext_user(request.args.get("token", ""))
    if not user:
        return _cors(jsonify({"ok": False, "error": "Invalid token"})), 401
    try:
        limit = max(1, min(int(request.args.get("limit", 50)), 200))
    except Exception:
        limit = 50
    wide = (request.args.get("all") or "") in ("1", "true", "yes", "on")
    try:
        statuses = user_statuses(user)
    except Exception:
        statuses = {}
    try:
        resume = db.profile_text(user)          # same whole-profile text the feed scores against
    except Exception:
        resume = ""
    params = dict(_prefs_as_params(_user_prefs(user)))
    params["sort"] = "score" if request.args.get("sort") == "score" else "newest"
    try:
        matched = _filter_rows(ranked_rows(user, resume), statuses, params)
    except Exception:
        matched = []
    jobs, per_company = [], collections.Counter()
    for r, st in matched:
        url = r.get("url") or ""
        # Already applied stays out of the queue: _filter_rows only drops `hidden` on this tab.
        if st == "applied" or not _queue_fillable(url, wide):
            continue
        # One employer must not eat the whole queue — see the docstring. Blank company names
        # are exempt rather than lumped together, since "" is missing data, not an employer.
        ck = (r.get("company") or "").strip().lower()
        if ck:
            per_company[ck] += 1
            if per_company[ck] > _QUEUE_PER_COMPANY:
                continue
        jobs.append({"url": url, "title": r.get("title", ""), "company": r.get("company", ""),
                     "score": int(r.get("score") or 0), "liked": st == "liked",
                     "location": r.get("location", ""), "remote": bool(r.get("remote")),
                     "date": r.get("date") or r.get("first_seen") or "",
                     "visa": list(r.get("visa") or ()), "track": r.get("track") or "",
                     "agency": bool(r.get("agency")), "salary": r.get("salary_label") or ""})
        if len(jobs) >= limit:
            break
    return _cors(jsonify({"ok": True, "jobs": jobs, "count": len(jobs),
                          "wide": wide, "prefs_applied": True}))


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

    # The OTHER way jobs enter the table. Without the same blocklist check the scraper has, a
    # blocked company walks straight back in through the extension and the block looks broken.
    # Deliberately not pushed down into db.add_jobs(): that would put a blocklist read in every
    # write path, including the scorer's score upserts.
    blocked = db.blocked_company_keys()

    kept, scanned = [], 0
    # Tally WHY jobs were dropped — when an import adds 0, this is the diagnosis.
    dropped = {"dup": 0, "title": 0, "us": 0, "bad": 0, "blocked": 0}
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
        company = (j.get("company") or "").strip()[:200]
        if blocked and db.block_key(company) in blocked:
            dropped["blocked"] += 1
            continue
        seen.add(url)
        spons = "unknown"
        if sidx is not None and company:
            spons = "yes" if scraper.sponsors_h1b(company, sidx) else "no"
        # NO import-time stamp: an imported job's posting date is UNKNOWN until the
        # detail-fetch finds a real datePosted — a stamp would show as "Today" in the
        # feed and lie about freshness. The honest "when did this reach us" value is the
        # jobs.first_seen column, which the DATABASE fills on insert; empty found_date now
        # means the card shows "Added <date>" rather than no date at all.
        kept.append({"found_date": (j.get("found_date") or ""), "title": title,
                     "company": company, "location": loc, "url": url, "sponsors_h1b": spons})

    if kept:
        try:
            db.add_jobs(kept)
        except Exception as e:
            return _cors(jsonify({"ok": False, "error": str(e)[:160]})), 500
        _invalidate_jobs()                       # imported jobs show on the next feed load
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
        det = (scraper.detect_board(page) or scraper.detect_paylocity(page)
               or scraper.detect_jibe(page)
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
        _invalidate_jobs()                       # next feed load re-pulls the changed rows…
        _score_cache.clear()                     # …and per-user scores recompute with JDs
        _sponsor_cache.clear()
        # Only these JDs changed -> drop their stale meta. Nothing to invalidate on the column
        # side: job_analysis reads jd_terms straight off the row each time, and the next
        # get_jobs() re-pulls those. Their analysis stays as the last scoring run left it,
        # which is the same lag the score itself has.
        for _u in list(clean) + removed:
            _jdmeta.pop(_u, None)
    return _cors(jsonify({"ok": True, "stored": len(clean), "patched": len(patches),
                          "removed_nonus": len(removed)}))


@app.route("/__react")
@admin_required
def react_harness():
    """Phase 2 pipeline probe. Admin-only, and deleted in Phase 3 with its entry.

    Proves the chain nothing else can prove until a real screen depends on it: hashed asset in
    static/dist, manifest lookup, module script under the app's own CSP, React mounting, props
    crossing the boundary, and the design tokens applying to React markup. If vite_entry()
    returns None the page still renders, which is the fallback every migrated route relies on.
    """
    entry = vite_entry("src/entries/harness.tsx")
    return render_template("react_page.html", entry=entry,
                           props={"user": session.get("user") or "",
                                  "csrf": csrf_token(),
                                  "builtFor": "/__react"})


@app.route("/healthz")
def healthz():
    """Public liveness probe — no auth, no DB, no work. An uptime pinger hits this every few
    minutes to keep the Passenger process (and its warm job/score/status caches) alive, so
    visitors don't pay the cold-start re-import + cache refill. See CPANEL_DEPLOY.md."""
    return Response("ok", mimetype="text/plain")


# WSGI alias. cPanel's generated stub does `application = wsgi.<entry point>`, and its
# "Application Entry point" field defaults to `application` while Flask convention names the
# object `app` — which is an AttributeError at startup, not a 404 you can debug from the page.
# Exporting both names means the app starts whichever value that field happens to hold, and
# also satisfies any generic WSGI server that looks for `application`.
application = app

if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
