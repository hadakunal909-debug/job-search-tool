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
from urllib.parse import quote, urlsplit
import sys
import json
import time
import html
import threading
import hmac
import base64
import hashlib
import secrets
import datetime
import functools
import collections
import math

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
                   render_template, flash, g, Response, abort)

import core
import db
import dbproxy
import auth
import jdrender
import resume_score
import resume_keywords
import resume_bullets

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
        def set_optout(*a, **k):
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
rb_voice = _LazyMod("resume_brain.voice")   # the shared style guide + intensity levels
rb_reposts = _LazyMod("scraper.reposts")   # cluster_key; +31ms on first use, measured
sc = _LazyMod("scraper")                   # title_verdict, for the "matched on description" chip

app = Flask(__name__)


def _fallback_secret():
    """Signing key when neither APP_SECRET nor a Supabase key is configured (local dev).
    Derive a stable, MACHINE-LOCAL value rather than a globally-known constant, so the
    session/extension-token signature can't be forged just by reading this source."""
    import platform
    seed = "jobmatch-dev|%s|%s" % (platform.node(), os.path.abspath(__file__))
    return hashlib.sha256(seed.encode()).hexdigest()


# Session signing key: explicit APP_SECRET, else a machine-local dev fallback.
#
# THE MIDDLE OPTION IS GONE, and it was load-bearing. Until 2026-09-01 this read
# `db._creds()[1]` — the Supabase key — and hashed it into the session secret, so an install
# that never set APP_SECRET was signing every session cookie and extension token with a
# credential from a database this project left on 2026-08-15. Deleting the Supabase transport
# deletes that source, which means:
#
#   * the signing key CHANGES on the deploy that lands this, so every existing session cookie
#     and extension token is invalidated once. Unavoidable: the old key was derived from a
#     secret being removed. Setting APP_SECRET to sha256(old_supabase_key) preserves them if
#     that matters more than a clean break.
#   * _fallback_secret() must never be what a real deployment lands on. It is sha256 over
#     hostname + this file's path — deterministic, so logins survive a restart, and therefore
#     GUESSABLE by anyone who knows both. Its own docstring calls it a dev key.
#
# So: with a remote database configured, refuse to start rather than sign with it. Same rule
# db._check_backend_intent applies one module over — a half-configured process fails in one
# second instead of doing something that looks like working.
_explicit_secret = os.environ.get("APP_SECRET")
app.secret_key = _explicit_secret or _fallback_secret()
if not _explicit_secret:
    import sys as _sys
    if db.has_remote_db():
        raise RuntimeError(
            "APP_SECRET is not set, and this process has a real database (%s). The session key "
            "would fall back to a machine-local value derived from the hostname and this file's "
            "path, which is guessable — sessions and extension tokens could be forged. Set "
            "APP_SECRET to a long random string (cPanel: Setup Python App -> Environment "
            "variables). Note it invalidates existing logins once; set it to the sha256 of the "
            "old SUPABASE_KEY instead if you need them to survive." % db.backend_name())
    print("WARNING: no APP_SECRET set. Using a machine-local dev signing key, which is fine "
          "offline and unsafe anywhere real.", file=_sys.stderr)

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
# Google Fonts, and nothing else. CSS from googleapis, font files from gstatic.
#
# img-src IS NOW 'self' data', WITH NO REMOTE ORIGIN AT ALL, 2026-08-22. It used to allow
# *.gstatic.com for the favicon service and img.logo.dev for the logo service. Both are gone
# because the logos are harvested and committed now (scripts/build_logos.py), and this line is
# what ENFORCES that rather than merely recording it: anyone who reintroduces a hotlinked logo
# gets a blocked request and a console error instead of a silent third-party dependency. Which
# matters, because Clearbit's free logo API -- the previous incumbent in this exact slot -- was
# switched off on 2025-12-08 and took every page that hotlinked it down with it.
# Everything else is same-origin ('self').
_CSP_TEMPLATE = (
    "default-src 'self'; "
    "script-src 'self' 'nonce-%s'; "
    # NO REMOTE ORIGIN IN EITHER, since the fonts were self-hosted. This is the same move
    # img-src makes for the logos: the policy is what ENFORCES "nothing is fetched from a third
    # party at request time", so a stray <link href="https://fonts.googleapis.com"> creeping
    # back into a template fails visibly in the console instead of quietly re-adding two
    # handshakes to first paint. Note font-src previously did not include 'self' at all -- only
    # gstatic -- so self-hosting could not have worked without changing this line.
    "style-src 'self' 'unsafe-inline'; "
    "font-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    # pdf.js starts its worker from a blob: URL, and worker-src has no fallback to script-src -- it
    # falls back to child-src then default-src, and 'self' does not cover blob:. Without this the
    # PDF preview fails with a CSP violation naming a directive nobody set.
    "worker-src 'self' blob:; "
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
# OrderedDict rather than dict so eviction can drop the LEAST RECENTLY USED entry. A plain dict
# can only cheaply drop the oldest INSERTED one, which is a different thing and the wrong one:
# 64 sign-ins in a row would evict somebody still scrolling and charge them a full ~1 s rebuild,
# no matter how recently they had asked for a page.
_score_cache = collections.OrderedDict()   # (username, resume_md5) -> {url: score}
_rows_cache = collections.OrderedDict()    # (username, resume_md5) -> [row w/o status], by score desc
# THE CARD FIELDS THAT ARE NOT YOURS. _build_row emits 41 keys and exactly ONE of them --
# `score` -- depends on who is asking. Everything else (the sponsor tier, the visa routes, the
# logo, the dates, the pay label, the badges) is a property of the POSTING. Before this cache
# existed, every distinct (user, résumé) re-derived all 40 impersonal fields for all ~22k rows
# purely to attach a different integer to each: 1,941 ms measured, per user, per worker.
#
# So build them once per CORPUS and let ranked_rows overlay the score. Keyed on the jobs
# fingerprint -- the same value _scores_read already trusts to decide a stored file is still
# about this corpus -- and holding exactly one entry, because a second fingerprint means the
# first is dead, not colder.
#
# Measured over all 21,960 rows: 1,941 ms -> 184 ms, with zero rows differing in any field and
# an identical row order. The dedupe and the sort stay per-user; see ranked_rows for why the
# dedupe in particular cannot move in here.
_base_rows_cache = {"fp": None, "rows": None}
# BOUNDED BY MEMORY, NOT BY COUNT, and the difference is the whole point.
#
# This was `_SCORE_CACHE_MAX = 64` for the life of the app, and it was safe when the corpus
# was 2,674 rows. It is not safe now. _rows_cache holds the WHOLE corpus as built card dicts
# per (user, résumé), measured at ~49 MB of RSS per entry at 21,982 rows — so a count of 64
# permitted about 3.1 GB in a process shared cPanel caps somewhere under 1 GB. The host would
# have killed the worker long before the LRU ever evicted anything, and the failure would
# have looked like a random restart rather than a cache that was sized in the wrong unit.
#
# A count cap gets MORE dangerous every time the scraper runs. A byte budget does not, which
# is why the limit is now derived from the live row count on every check.
#
# Raising it: CACHE_BUDGET_MB in .env. The default leaves headroom under a 512 MB cap on top
# of the ~155 MB an idle worker already holds (imports + the corpus). Once the real per-process
# limit is known from cPanel's Resource Usage page, this is the one number worth tuning — every
# extra entry that fits is one more person who gets a 50 ms feed instead of a ~1.8 s rebuild.
_CACHE_BUDGET_MB = int(os.environ.get("CACHE_BUDGET_MB") or 256)
_ROW_CACHE_BYTES_PER_ROW = 2240   # measured by RSS delta, 8 distinct users at 21,982 rows
# 1, not 2. A minimum of two looked kinder but broke the promise this budget makes: at a
# corpus where one entry alone exceeds the budget, a floor of two would silently hold double
# it. One entry is the smallest useful cache -- zero would recompute on literally every
# request -- and if the cap ever lands on 1, the answer is to raise CACHE_BUDGET_MB, not to
# quietly overspend it.
_SCORE_CACHE_MIN = 1
_SCORE_CACHE_CEIL = 64            # the old constant, kept as an upper bound


def _cache_max():
    """How many per-user cache entries fit in the budget, at today's corpus size.

    Uses the rows already in memory, so it costs a len() and needs no configuration. Falls
    back to a pessimistic 20k when the corpus has not been read yet — guessing LOW there
    would raise the cap on a worker that is about to load a large corpus, which is backwards.
    """
    rows = len(_jobs_cache.get("rows") or ()) or 20000
    per_entry_mb = max(1.0, rows * _ROW_CACHE_BYTES_PER_ROW / 1048576.0)
    # _base_rows_cache holds one corpus-worth of the same dicts and is charged one entry here.
    # It is shared by every user, so it is not free and it is not per-user: leaving it out of
    # the arithmetic would grow the worker by a whole corpus with the budget none the wiser.
    # Below the point where one entry fits, this goes negative and the floor returns 1 -- which
    # is the same answer the un-adjusted form gave, so the invariants in
    # scripts/test_speed_caches.py::cache_budget still hold.
    return max(_SCORE_CACHE_MIN, min(_SCORE_CACHE_CEIL,
                                     int((_CACHE_BUDGET_MB - per_entry_mb) / per_entry_mb)))
# Above this many jobs, the feed stops shipping EVERY job inline and switches to top-N inline +
# server-side search/paging (/api/feed), so the payload + browser parse stay small at any corpus
# size. Below it, the original all-inline client-filtered path is used unchanged. Env-tunable.
_FEED_INLINE_MAX = int(os.environ.get("FEED_INLINE_MAX", "4000"))
# How many rows to inline as PAGE ONE when paged. 60 because that is app.js's PAGE size, so the
# bootstrap is exactly the page /api/feed would have returned for offset=0. It was 400, which was
# both too many (the browser discarded them) and the wrong rows (unfiltered).
_FEED_TOPN = int(os.environ.get("FEED_TOPN", "60"))
_sponsor_cache = {}          # url -> (verdict, reason) read from the JD (same for everyone)
# url -> {analyzed, exp_years, exp_level, sponsor_jd}. OFF unless JDMETA=1.
#
# Every field this file holds is also a COLUMN on the jobs table — jd_terms, exp_max_years,
# sponsor_jd, sponsor_reason — and both readers below already prefer the column and fall back to
# here (_analyzed_of, _jd_fields). Their own comments say only the column reaches the live site,
# "jdmeta.json is gitignored and never deployed", which was true while the scorer only ever ran
# on an ephemeral GitHub runner.
#
# MOVING THE SCRAPER ONTO THE APP SERVER BROKE THAT ASSUMPTION SILENTLY. The scorer writes
# jdmeta.json wherever it runs, so the file now appears next to the app — and this line loaded it
# into EVERY Passenger worker at import. Measured: 29 MB on disk becomes 94 MB of Python objects,
# per worker, permanently, to duplicate columns the same query already returns. On a 2 GB account
# where a warmed worker is 310 MB, that is most of a worker's footprint spent on a fallback for a
# case that cannot happen in production: the file and the columns are written by the SAME run, so
# it is never fresher than they are.
#
# Left switchable rather than deleted because the fallback is genuinely wanted on a developer's
# machine, where a scoring run may have written the file while the database write failed. Set
# JDMETA=1 there.
_jdmeta = core.load_jdmeta() if (os.environ.get("JDMETA") or "").strip() in ("1", "true", "yes") else {}
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
# Usernames current_resume() has already tried to repair this process. Unbounded is fine: it holds
# one short string per user who has signed in here, and the alternative (retrying every cache miss)
# costs two queries a minute forever for anyone who genuinely has no résumé.
_resume_repair_tried = set()


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
    # SELF-HEAL. An empty legacy column used to mean "no résumé", but it can also mean the user
    # only ever added résumés through Resume Brain, which writes the `resumes` table and never
    # touched this one — and then the feed scores every job against "". Repairing it here rather
    # than only on /brain matters because the feed is the page that shows the damage, and a user
    # has no reason to guess that visiting another page would fix their match percentages.
    #
    # Gated to fire at most once per user per process, and only when the column is actually empty,
    # so the common case pays nothing.
    if not txt.strip() and user not in _resume_repair_tried:
        _resume_repair_tried.add(user)
        _ensure_resume_migrated(user)
        try:
            txt = (db.get_user(user, "resume") or {}).get("resume", "") or ""
        except Exception:
            pass
    _resume_cache[user] = (txt, time.time())
    return txt


_profile_cache = {}          # username -> (profile_text, fetched_at)


def _ensure_resume_migrated(user):
    """Keep the two résumé stores in step, in BOTH directions. Safe to call repeatedly.

    The library (`resumes`) is the truth and `users.resume` is a cache of whichever row is
    active — but only the legacy -> library half of that ever existed, and it ran only when the
    library was completely empty. Measured live, that left two of four accounts with a résumé in
    the library and '' in users.resume, which is the single value the feed's match % scores
    against: their entire feed was scored against an empty string, silently, with a full résumé
    sitting one table away. A third account had two different documents in the two places.

    So there are three jobs here, each a no-op once satisfied:
      1. library empty, legacy present  -> adopt the legacy text as the first library row
      2. no row flagged active          -> flag one (prefer the one matching legacy, else newest)
      3. legacy empty, library present  -> seed users.resume from the active row
    """
    try:
        rows = db.list_resumes(user) or []
        legacy = (db.get_user(user, "resume") or {}).get("resume", "") or ""
        if not rows:
            if not legacy.strip():
                return
            db.save_resume(user, {"name": "My résumé", "content": legacy, "active": True})
            return
        if not any(r.get("active") for r in rows):
            # Prefer the row the feed has actually been scoring, so activating cannot silently
            # change someone's match percentages; fall back to the newest.
            pick = next((r for r in rows if (r.get("content") or "") == legacy and legacy.strip()),
                        rows[-1])
            db.set_active_resume(user, pick.get("id"))
            _resume_cache.pop(user, None)
            return
        if not legacy.strip():
            active = db.get_active_resume(user) or {}
            if (active.get("content") or "").strip():
                db.set_user_resume(user, active["content"])
                _resume_cache.pop(user, None)
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


# The profiles ROW, which is NOT what _profile_cache above holds — that one caches the profile
# TEXT from db.profile_text(). Different call, different shape, and the row was uncached entirely.
#
# Measured on a warm GET /: the SAME row was fetched twice in one render, 46 ms + 54 ms for 1,431
# bytes each — 100 ms of a 197 ms request, and on a process-per-request pool that is 100 ms a
# worker spends holding a slot while doing nothing. Once via _user_prefs, once for the visa nudge.
#
# Same TTL and the same bust points as the other per-user caches. Every write goes through
# _save_profile() below, so a save is never followed by a stale read.
_profile_row_cache = {}      # username -> (profile dict, fetched_at)


def _profile_row(user):
    """This user's profiles row, from a short-lived per-worker cache."""
    if not user:
        return {}
    hit = _profile_row_cache.get(user)
    if hit and time.time() - hit[1] < _RESUME_TTL:
        return hit[0]
    try:
        row = db.get_profile(user) or {}
    except Exception:
        return hit[0] if hit else {}        # serve stale rather than lose the page
    _profile_row_cache[user] = (row, time.time())
    return row


def _save_profile(user, fields):
    """db.save_profile + drop this user's cached row. The ONLY way web.py should write a profile:
    a bare db.save_profile would leave the cache serving the pre-save value for up to _RESUME_TTL,
    which reads to the user as 'I saved it and nothing happened'."""
    ok, msg = db.save_profile(user, fields)
    _profile_row_cache.pop(user, None)
    return ok, msg


def _bust_profile(user=None):
    """Drop cached profile + scores after a résumé/story/lesson edit so the feed updates.

    ONE user's entries, not everyone's. _score_cache and _rows_cache are keyed on
    (username, md5(resume)), so an edit changes the KEY: the stale entry is unreachable the
    instant the new one is written, and this pop only reclaims its memory. Clearing every OTHER
    user was the expensive half — it invalidated entries that were already correct and charged
    each of those users a full corpus rebuild (~1 s apiece) on their next page. Measured at
    12.2 s of worker CPU for a single résumé save, which is what a stalled site is made of.

    Scanning the dict is bounded by _cache_max(), not by the number of users."""
    if user:
        _profile_cache.pop(user, None)
        _profile_row_cache.pop(user, None)
        for cache in (_score_cache, _rows_cache):
            # SNAPSHOT THE KEYS FIRST. Passenger serves requests on threads, so another one can
            # be inserting into these caches while this comprehension walks them, and iterating
            # a dict during mutation raises RuntimeError: OrderedDict mutated during iteration.
            # list() copies the keys before the scan, which costs one small list and removes the
            # race entirely.
            for k in [k for k in list(cache.keys()) if k and k[0] == user]:
                cache.pop(k, None)
    else:
        # No user named: an admin-level reset (see /reload), where wiping everything is the point.
        _profile_cache.clear()
        _profile_row_cache.clear()
        _score_cache.clear()
        _scores_clear()
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

# How old a snapshot may be and still be worth REVALIDATING (not serving blind — see get_jobs).
# Correctness comes from the fingerprint, so this is not a freshness limit; it is a bound on the
# one thing the fingerprint cannot see. jobs_fingerprint() is (row count, max first_seen), and
# update_job_fields moves NEITHER, so a run that only PATCHes existing rows — the scorer writing
# match_score, a JD backfill — is invisible to it. Inserts move both, and the weekday scrapes
# insert, so in practice the probe is right twice a day and this cap only binds across a quiet
# weekend. A day is deliberately shorter than the exposure the in-memory path already carries
# (a long-lived worker revalidates by fingerprint with no age bound at all).
#
# Raise it on a DEVELOPMENT machine, where the snapshot is routinely days old because nobody
# opened the feed over the weekend, and where a real corpus carrying last week's match_scores is
# perfectly good to run the suite against: JOBS_SNAPSHOT_MAX_AGE=604800 turns the ~14.5 MB cold
# read that every local run of test_prefs and test_onboarding pays into a fingerprint probe.
# Leave it at the default in production, where the scores are what the page actually shows.
_SNAPSHOT_MAX_AGE = int(os.environ.get("JOBS_SNAPSHOT_MAX_AGE") or 24 * 3600)


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
        # compresslevel=6, matching _compress. The default is 9, and at ~25k rows this file is
        # tens of megabytes of JSON — level 9 buys a few percent of disk for several times the
        # CPU, on a path that runs inside a request.
        with _gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as fh:
            json.dump({"rows": rows, "fingerprint": list(fingerprint or ())}, fh)
        os.replace(tmp, _JOBS_SNAPSHOT)
    except Exception:
        pass                          # an optimization only; never fail a request over it


def _snapshot_touch(rows, fingerprint):
    """Mark the shared snapshot freshly-validated WITHOUT rewriting it.

    The caller has just confirmed the fingerprint still matches, so the bytes on disk are
    already right and the only thing that needs to move is the mtime the other workers read as
    "someone checked this recently" (_SNAPSHOT_MAX_AGE). This used to call _snapshot_write,
    which re-serialised the whole corpus and gzipped it — on the one request whose entire
    purpose was to AVOID re-reading the corpus. os.utime achieves the stated goal for free.
    """
    try:
        os.utime(_JOBS_SNAPSHOT, None)
    except OSError:
        _snapshot_write(rows, fingerprint)     # missing or unwritable: do the real write


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

        # Past the TTL — but OLD IS NOT WRONG, and the fingerprint is what knows the difference.
        # Revalidate whatever rows we can reach, in order of what they cost to reach: this
        # worker's own memory (free), then the stale snapshot on disk (a local file read).
        #
        # THE SECOND HALF IS THE ONE THAT MATTERS. Until it existed, only a worker that already
        # held rows could revalidate; a worker that started cold skipped straight to the full
        # ~13 MB read whenever the snapshot had aged past an hour. That is the normal state of
        # this app — Passenger recycles workers freely, the corpus moves only on the two
        # weekday scrapes, and traffic is thin enough that the snapshot is usually stale by the
        # time anyone asks. So the standing cost was a full corpus read per cold worker per
        # quiet hour, for rows that had not changed since yesterday. It is now a HEAD.
        stale_rows, stale_fp = (None, None)
        if _jobs_cache["rows"] is None:
            stale_rows, stale_fp = _snapshot_read(_SNAPSHOT_MAX_AGE)
        have_rows = _jobs_cache["rows"] if _jobs_cache["rows"] is not None else stale_rows
        have_fp = _jobs_cache.get("fp") if _jobs_cache["rows"] is not None else stale_fp
        # An unavailable probe returns (None, "") and falls through to the re-read, which is
        # the only safe reading of "don't know" — see db.jobs_fingerprint.
        if have_rows is not None and have_fp:
            fp = db.jobs_fingerprint()
            if fp[0] is not None and fp == have_fp:
                _jobs_cache["rows"], _jobs_cache["fp"] = have_rows, fp
                _jobs_cache["at"] = time.time()
                _snapshot_touch(have_rows, fp)             # refresh mtime for the other workers
                return have_rows

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
    # A FOURTH line, and it is here for the reason the fp=None note above gives. _base_rows_cache
    # is keyed on jobs_fingerprint(), which is (row count, max first_seen) -- and the extension's
    # JD patch moves neither. So a re-read would come back with an fp EQUAL to the stored one and
    # the built rows would keep serving jd_admit / score_pending / sponsor badges derived from
    # descriptions that have since changed. Clearing it here covers every caller at once.
    _base_rows_cache["fp"] = _base_rows_cache["rows"] = None
    try:
        os.remove(_JOBS_SNAPSHOT)
    except Exception:
        pass


_job_idx = {"fp": None, "by_url": None}


def _job_for(url):
    """The raw job row for one url, or None.

    This replaced eight copies of a `next(j for j in get_jobs() if j["url"] == url)` generator —
    a linear walk of the whole ~20k-row corpus to find one row, paid on the job page (twice),
    both tailor routes, the application autolog, and once just to attach a company name to an
    analytics event. A dict costs one pass to build and answers every later lookup in O(1).

    Keyed on the jobs fingerprint, exactly like _title_index and _hay_idx: the corpus only moves
    on a scrape, and which row owns a url has nothing to do with who is asking. Rebuilding on a
    fingerprint change (rather than never) is what keeps a re-scraped or PATCHed row visible.
    """
    if not url:
        return None
    rows = get_jobs()                       # must come first: it is what refreshes the fingerprint
    fp = _jobs_cache.get("fp")
    if _job_idx["by_url"] is None or _job_idx["fp"] != fp:
        _job_idx["by_url"] = {j.get("url"): j for j in rows if j.get("url")}
        _job_idx["fp"] = fp
    return _job_idx["by_url"].get(url)


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


# Per-user scores, on disk, shared across workers and surviving a restart.
#
# THE MEASUREMENT THIS EXISTS FOR. Scoring the corpus against one résumé is 21,982 calls to
# core.score_against, and that is 5-15 SECONDS of CPU. A warm feed render is 41 ms. So the
# feed is not "slow" or "fast" — it is 41 ms or fifteen seconds, and which one you get depends
# entirely on whether _score_cache happens to hold your entry. A 13.8 s Largest Contentful
# Paint was reported on the live site and this was all of it.
#
# _score_cache ALONE cannot fix that, which is the part that is easy to get wrong: it is a
# per-PROCESS dict, Passenger runs a pool of 2 to 6, and the keep-warm pinger hits /healthz,
# which has no user and so builds nothing. Even on a permanently warm app, the first request
# each worker serves for each (user, résumé) pays the whole rebuild. A restart, a deploy, a
# prefs save, a résumé edit and the Reload button each reset it too.
#
# Same shape as jobs_snapshot.json.gz above, for the same reason: a file is the only cache
# several short-lived processes can share. Keyed on the jobs fingerprint as well as the
# résumé, so a scrape invalidates it, and every failure path just recomputes.
_SCORES_DIR = os.environ.get("SCORES_DIR") or os.path.join(_APP_DIR, "score_cache")
_SCORES_MAX_FILES = 64                   # bounded like _score_cache; oldest mtime evicted


def _scores_path(username, rmd5):
    """One file per (user, résumé). Hashed, because a username is not a safe filename.

    Newline as the separator: it cannot appear in either half, so no (user, résumé) pair can
    collide with a different one by concatenating to the same string.
    """
    h = hashlib.sha256(("%s\n%s" % (username, rmd5)).encode("utf-8")).hexdigest()[:32]
    return os.path.join(_SCORES_DIR, "%s.json.gz" % h)


def _scores_read(username, rmd5, fp):
    """The stored {url: score} for this (user, résumé), IF it was written against `fp`.

    None on anything unexpected — absent, unreadable, or built for a different corpus. Every
    failure path recomputes, which is slow but never wrong.
    """
    if not fp:
        return None                      # no fingerprint means nothing safe to key on
    try:
        with _gzip.open(_scores_path(username, rmd5), "rt", encoding="utf-8") as fh:
            blob = json.load(fh)
        if list(blob.get("fingerprint") or ()) != list(fp):
            return None                  # the corpus moved under it
        scores = blob.get("scores")
        return scores if isinstance(scores, dict) else None
    except Exception:
        return None


def _scores_write(username, rmd5, fp, scores):
    """Persist one user's scores. Atomic, bounded, and never fails a request."""
    if not fp or not scores:
        return
    try:
        os.makedirs(_SCORES_DIR, exist_ok=True)
        # Bound the directory the way _score_cache bounds memory. Oldest mtime first, so the
        # file about to be written is never the one evicted.
        try:
            kept = sorted((os.path.getmtime(os.path.join(_SCORES_DIR, n)),
                           os.path.join(_SCORES_DIR, n))
                          for n in os.listdir(_SCORES_DIR) if n.endswith(".json.gz"))
            for _, path in kept[:max(0, len(kept) - _SCORES_MAX_FILES + 1)]:
                os.remove(path)
        except Exception:
            pass
        target = _scores_path(username, rmd5)
        tmp = "%s.%d.tmp" % (target, os.getpid())
        with _gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as fh:
            json.dump({"fingerprint": list(fp), "scores": scores}, fh)
        os.replace(tmp, target)          # two workers may write at once; readers see one file
    except Exception:
        pass                             # an optimization only


def _scores_clear():
    """Drop every stored score file. /reload means recompute everything, including these."""
    try:
        for n in os.listdir(_SCORES_DIR):
            if n.endswith(".json.gz"):
                os.remove(os.path.join(_SCORES_DIR, n))
    except Exception:
        pass


def user_scores(username, resume):
    """{url: match%} for this user. Scores each job's stored JD against the résumé
    (core.skill_match); falls back to the precomputed baseline when no résumé/JD.

    Three layers, cheapest first: this process's dict, then the shared file (see _scores_read),
    then the scoring pass itself. Only the last one is slow, and it is the one the other two
    exist to stop repeating.
    """
    rmd5 = hashlib.md5((resume or "").encode("utf-8")).hexdigest()
    key = (username, rmd5)
    if key in _score_cache:
        _score_cache.move_to_end(key)        # a read is a use: keeps active users out of the evictor
        return _score_cache[key]
    # get_jobs() FIRST: it is what refreshes the fingerprint the stored file is keyed on.
    rows = get_jobs()
    fp = _jobs_cache.get("fp")
    stored = _scores_read(username, rmd5, fp)
    if stored is not None:
        if len(_score_cache) >= _cache_max():
            _score_cache.popitem(last=False)
        _score_cache[key] = stored
        return stored
    resume_low = (resume or "").lower()      # lowercase ONCE, not per job (was ×2,500)
    scores = {}
    for j in rows:
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
                # score_pct, not score_against(...)[0]. Identical answer; it skips the have /
                # missing lists, which are two full sorts per row that only the job page and
                # the resume tailorer ever read. See core.score_pct.
                scores[u] = core.score_pct(resume_low, analyzed)
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
    if len(_score_cache) >= _cache_max():
        _score_cache.popitem(last=False)     # drop least-recently-used; bounds memory growth
    _score_cache[key] = scores
    _scores_write(username, rmd5, fp, scores)
    return scores


# Internship / co-op detection from the title (for the "Internship" badge + the Intern/Co-op
# filter). Whole-word so it won't fire on "international"/"internal". Matches intern(s|ship|ships),
# co-op / coop / co op (+ plurals), and the finance-internship "summer analyst/associate".
_INTERN_RE = re.compile(
    r"\b(?:intern(?:s|ship|ships)?|co[-\s]?ops?|summer analyst|summer associate)\b", re.I)


_JD_VERDICT_KEY = "jd_host_verdicts"
_jd_blocked_hosts = None


def _host_jd_blocked(url):
    """True when this URL's host has been PROVED unreadable server-side.

    Read from the jd_host_verdicts KV row that scripts/close_dead_jds.py writes after probing,
    not guessed here: the distinction between "we have not fetched this yet" and "this host
    refuses us" is a measurement, and putting a hostname list in the web layer would be a second
    place for it to drift. Cached for the life of the worker — the verdict changes when someone
    re-runs that script, not per request. Absent row -> nothing is blocked, i.e. today's copy.
    """
    global _jd_blocked_hosts
    if _jd_blocked_hosts is None:
        try:
            hosts = (db.get_kv(_JD_VERDICT_KEY) or {}).get("hosts") or {}
            _jd_blocked_hosts = {h for h, v in hosts.items()
                                 if (v or {}).get("verdict") == "blocked"}
        except Exception:
            _jd_blocked_hosts = set()
    if not _jd_blocked_hosts:
        return False
    return core.url_host(url) in _jd_blocked_hosts


# ---------------------------------------------------------------------------------------------
# "MATCHED ON DESCRIPTION" — the chip for a job whose TITLE said nothing useful.
#
# Since 2026-08-20 the sweep keeps a posting when the description reads like delivery work even
# though no INCLUDE phrase matched the title, so the feed carries jobs called "Coordinator II"
# that really are project management. The chip says which rule let a row in, so the wider net is
# auditable instead of a black box.
#
# DERIVED, NOT STORED. The scraper keeps a row for exactly two reasons, and both are functions
# of data already in the row, so a title that fails the filter while sitting in the corpus is
# itself the evidence. That is worth more than a jobs column: this repo's standing bias is
# against adding them (they need a hand-run migration, and the Actions runner cannot write at
# all until DB_PROXY_SECRET is re-pasted), and a derived answer cannot drift out of sync with
# the filter the way a stamped one can.
#
# TWO GUARDS, both for legacy rows, and without them the chip would lie:
#   1. An EXCLUDE hit is not a description admission. The description rule only ever runs when
#      the verdict was "no matching keyword", so a row the exclude list would now veto is a
#      leftover from before a tightening (the surviving Sephora and Aspen Dental rows), not
#      something admitted on its text.
#   2. first_seen must be on or after the day the rule shipped. Rows admitted by INCLUDE terms
#      that were later REMOVED -- "operations associate", "trainee", "entry level" -- fail the
#      filter today for a completely different reason. prune_offtarget.py deleted 1,430 of those
#      and a few dozen survived, and every one would otherwise wear this chip.
_JD_ADMIT_FROM = "2026-08-20"
_admit_cache = {}


def _admitted_on_description(title, first_seen):
    """Did this row get in on its DESCRIPTION rather than its title?"""
    if not title or str(first_seen or "")[:10] < _JD_ADMIT_FROM:
        return False
    hit = _admit_cache.get(title)
    if hit is None:
        # Reproduce the filter that actually ran, résumé-derived phrases included — judging
        # against the base INCLUDE would mislabel every row one of those admitted.
        if not _admit_cache:
            try:
                sc.apply_resume_terms()
            except Exception:
                pass
        try:
            keep, why = sc.title_verdict(title)
            # THREE conditions, not two, and the third is what keeps the chip honest. Since
            # 2026-08-20 the sweep also requires core.pm_title_gate before a description gets a
            # vote, so a title that fails the keyword filter AND the gate cannot have been
            # admitted on its text — it is a legacy row, and chipping it would assert a
            # provenance that never happened. Same shape as the two guards above.
            hit = (not keep and not why.startswith("off-target")
                   and core.pm_title_gate(title))
        except Exception:
            hit = False
        if len(_admit_cache) < 60000:          # bounded, like core._role_cache
            _admit_cache[title] = hit
    return hit


_REPOST_KEY = "repost_clusters"
_repost_clusters = None


def _repost_count(title, company, location):
    """How many times this exact role has been advertised at this location, or 0.

    Read from the repost_clusters KV row that scripts/detect_reposts.py --write publishes, keyed by
    scraper.reposts.cluster_key so the writer and this reader cannot disagree about what a role IS.
    Cached for the worker's lifetime, same as _host_jd_blocked: the map changes when someone reruns
    that script, not per request.

    Keyed on role identity rather than on URL deliberately — a posting scraped AFTER the map was
    built still gets badged, because it hashes to the same key as the cluster it belongs to. That
    is the whole reason the stored map is ~500 keys and not ~2,200 URLs.
    """
    global _repost_clusters
    if _repost_clusters is None:
        try:
            _repost_clusters = (db.get_kv(_REPOST_KEY) or {}).get("clusters") or {}
        except Exception:
            _repost_clusters = {}
    if not _repost_clusters:
        return 0
    try:
        return int(_repost_clusters.get(
            rb_reposts.cluster_key(title, company, location)) or 0)
    except Exception:
        return 0


# The exact byte suffix pack_analyzed leaves on a NON-thin analysis. It writes
#   json.dumps({"w": {...}, "n": 0|1}, separators=(",", ":"))
# with "n" inserted last, so a stored value always ends "n":0} or "n":1} and always opens
# {"w":{" when there is at least one term. Both halves are checked: pack_analyzed returns ""
# rather than an empty "w", so a {"w":{},"n":0} would be data this code did not write, and
# unpack_analyzed calls that thin.
_NOT_THIN_TAIL = '"n":0}'
_THIN_TAIL = '"n":1}'
_HAS_TERMS_HEAD = '{"w":{"'


def _row_pending(j):
    """Is this job's description unusable — thin, malformed, or absent?

    This is the ONE thing _build_row wanted from job_analysis, and getting it used to cost a
    full core.unpack_analyzed per row: a json.loads plus a rebuilt weight dict, a term list and
    a sum. user_scores had ALREADY unpacked the same row a moment earlier, so a ranked_rows
    rebuild paid for the whole corpus twice — ~50k unpacks at ~25k rows.

    Reading the packed string directly is O(1) and needs no cache, which matters more than the
    speed: `pending` also depends on _jdmeta, and _jdmeta is mutated at runtime by jd_meta(),
    by /reload and by the extension JD patch. Anything memoized on the jobs fingerprint alone
    would have gone stale on all three. Equivalence with the old expression is proved over the
    real corpus by scripts/test_speed_caches.py.
    """
    m = (_jdmeta.get(j.get("url") or "") or {}).get("analyzed")
    if m and m.get("terms"):
        return bool(m.get("thin"))          # jdmeta wins, exactly as job_analysis has it
    packed = j.get("jd_terms")
    if not packed:
        return True                         # job_analysis returns {}, and `not _an` is pending
    if isinstance(packed, str):
        if packed.endswith(_THIN_TAIL):
            return True
        if packed.endswith(_NOT_THIN_TAIL):
            # An empty "w" is thin however it got there, and pack_analyzed cannot have
            # written it — it returns "" rather than storing a term-less analysis.
            return not packed.startswith(_HAS_TERMS_HEAD)
    # Anything else — a legacy value with no "n" key, trailing whitespace, a non-str, or plain
    # garbage — is worth the real parse. Zero of 21,980 rows in the live snapshot take this
    # branch, but "the shapes I saw were canonical" is not a reason to answer a different
    # question than unpack_analyzed would. scripts/test_speed_caches.py pins all of them.
    return bool(core.unpack_analyzed(packed).get("thin"))


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
    # THE DATE, and whether it is a posting date at all.
    #
    # A trusted value is a bare ISO date and stays sliced to 10 chars. An UNTRUSTED one is
    # "YYYY-MM-DD HH:MM", which core.is_trusted_date documents as this codebase's marker for a
    # derived value — the scrape stamp for boards that publish no date, or _workday_date()
    # converting "Posted 3 Days Ago". Those used to be sliced too, throwing away the only
    # hour-precision timestamp in the corpus, and then rendered as if they were posting dates,
    # which is exactly what verify_dates.py's docstring complains about. Keep the time and let
    # the card label it "Added <n>h ago": hours where hours genuinely exist, and honest about
    # whose clock they came from.
    #
    # Safe for every consumer: _row_date() feeds string ">= cutoff" comparisons and a
    # descending sort, and "2026-08-04 14:00" both clears a "2026-07-12" cutoff and sorts
    # after a bare "2026-08-04", which is the correct order rather than an accident.
    trusted = core.is_trusted_date(j.get("found_date"), j.get("posted_verified"))
    draw = (j.get("posted_verified") or j.get("found_date")) or ""
    date = draw[:10] if trusted else draw
    # THE ONE CHIP, including the legacy fallback, decided here rather than in app.js.
    #
    # The fallback is the pre-visa_tags.json seed-list flag (jobs.sponsors_h1b). app.js used to
    # own it as a second branch reading `if (!chip && j.sponsors_h1b === "yes")`, and that had
    # two faults now that the card shows a single hedged chip:
    #
    #   1. It ignored the JD narrowing. A posting whose own text says "no visa sponsorship"
    #      empties vtags, which made the branch fire — so cards rendered "H1B (top sponsor)"
    #      and "No sponsorship" side by side. That is precisely the contradiction
    #      core.visa_tags_for_posting exists to prevent, and it was visible on the live feed.
    #   2. It fired whenever vtags was empty, not when the INDEX was missing, which is all it
    #      was ever meant to cover. sponsors_h1b fuzzy-matches a 621-name list at
    #      token_set_ratio >= 90 and scores "Northeastern University" 95.65 against
    #      "northwestern university", so trusting it over a built index is backwards.
    #
    # So: only when the index genuinely isn't there, and never over a blocked posting. One
    # field reaches the client and app.js has a single code path.
    vlikely = core.sponsor_likely(vtags)
    if (not vlikely and not visa_index() and sv != "blocked"
            and j.get("sponsors_h1b") == "yes"):
        vlikely = "h1b"
    # A too-thin/truncated JD can't be scored honestly (see core.analyze_jd) — surface it as
    # "JD pending" instead of a misleading number, and keep it at 0 so it sorts/filters low
    # rather than sitting at a fake ~100% on top of the feed. Read from the same analysis
    # user_scores scored against, so a card can't show a percentage AND call itself pending.
    # `or not _an`, and this is a second defect the badge work turned up. job_analysis returns
    # {} when a row has no stored analysis at all, and {}.get("thin") is None — so the 521 rows
    # with NO DESCRIPTION were falling through to a real score ring and rendering "0%". A job we
    # cannot read is not a 0% match; it is unscoreable, and 0% is a false statement rather than a
    # missing one. Measured before the change: jd_terms is NULL on exactly those 521 rows (2.37%)
    # and every one of them already had match_score 0, so nothing else in the feed moves.
    pending = _row_pending(j)             # was: job_analysis(j), a second full unpack per row
    # "pending" and "will never arrive" are different facts and the feed used to conflate them.
    # ~285 rows are real, open jobs on hosts that refuse every server-side read — Tesla behind
    # Akamai, iCIMS behind an AWS WAF human-verification challenge — so telling the user "it'll
    # get a match score once the full job description is fetched" is a promise that cannot be
    # kept. scripts/close_dead_jds.py probes and records the per-host verdict; this reads it.
    unavailable = pending and _host_jd_blocked(u)
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
    return {"title": j.get("title") or "", "company": c,
            "location": core.tidy_location(j.get("location") or ""),
            "loc_state": lstate, "loc_metro": lmetro, "remote": bool(lremote),
            "salary_min": smin, "salary_max": smax, "salary_period": speriod,
            "salary_label": core.salary_label(smin, smax, speriod),
            "closed": active is False or str(active).lower() == "false",
            "url": u, "apply_url": u if (u or "").startswith(("http://", "https://")) else "#",
            "sponsors_h1b": j.get("sponsors_h1b", ""),
            # date = the real posting date (verify_dates) when we have it, else found_date.
            # Carries "YYYY-MM-DD HH:MM" when date_trusted is false; see the block above.
            # The "New" badge is derived client-side from this date and date_trusted.
            "date": date,
            "date_verified": bool(j.get("posted_verified")),
            # Broader than date_verified: "somebody STATED this date" rather than "the lookup
            # service confirmed it". core.is_trusted_date is the one definition; the feed's
            # verifiedonly filter reads this, and app.js just checks the flag rather than
            # re-deriving the string-shape rule.
            "date_trusted": trusted,
            # Which role families this title belongs to. Computed here rather than in JS so the
            # phrase vocabulary has ONE definition; app.js just intersects two lists.
            "roles": list(core.roles_for_title(j.get("title"))),
            # Some employers publish no posting date at all (Tesla's careers API has no date
            # field anywhere), so this is when the job first entered OUR database. Rendered as
            # "Added <x>", never as a posting date. "" until the migration has been run.
            "first_seen": str(j.get("first_seen") or "")[:10],
            "score": 0 if pending else score, "score_pending": pending,
            "jd_unavailable": unavailable,
            "sponsor_jd": sv, "sponsor_reason": sreason, "agency": core.is_agency(c),
            "cap_exempt": core.is_cap_exempt(c),
            # How many distinct URLs this same role has had at this location inside the repost
            # window. 0 for the overwhelming majority. A measurement, not a judgement: the card
            # states the count and lets the reader decide whether it smells like a ghost req.
            "repost": _repost_count(j.get("title"), c, j.get("location")),
            # Which rule let this row in. True = its title matched nothing and the DESCRIPTION
            # carried it, so the card says so. See _admitted_on_description for why this is
            # derived rather than stored, and for the two legacy guards it needs.
            "jd_admit": _admitted_on_description(j.get("title"), j.get("first_seen")),
            # Which immigration routes this employer has actually filed for (DOL LCA + PERM
            # + E-Verify). A missing tag means "no record", never "won't sponsor".
            #
            # KEEP THE FULL LIST even though the card now renders only one chip. Three things
            # need every route: the visa filter (core.visa_tags_match / app.js visaHit, which
            # NARROWS — filtering on the single chip would hide every green-card employer whose
            # chip reads H-1B), core.sponsor_rank for sort=sponsor, and the everify derivation
            # just below. The job page names all five too.
            "visa": vtags,
            # The ONE hedged route the card names. core.sponsor_likely is the single definition
            # and it runs on the ALREADY-NARROWED tuple, so a posting whose text closes a route
            # cannot show a chip for it. Also drives data-route, so the chip and the card's
            # colour are one field and cannot disagree.
            "visa_likely": vlikely,
            # stem_opt IS the E-Verify fact; the everify.txt path stays as a fallback for
            # anyone who built that file (it has never existed in this repo).
            "everify": ("stem_opt" in vtags) or core.is_everify(c, _EVERIFY_INDEX),
            "exp_years": exp_y if exp_y is not None else "", "exp_level": exp_lvl,
            "strength": strength, "strength_n": scount,
            "intern": bool(_INTERN_RE.search(j.get("title") or "")),
            # 'dev' (software/data/infra) vs 'mgmt' (project/product/ops) — the feed's one-click
            # career split. core.role_track is the single definition; the digest reads it too.
            "track": core.role_track(j.get("title") or ""),
            # SELF-HOSTED, resolved server-side from the harvest manifest. ONE url and no
            # fallback: a card either has a logo we verified and shipped or it renders the
            # monogram, and there is no second URL to walk. That is not a simplification, it is
            # the fix -- the old chain handed the client the SAME url twice when no logo.dev key
            # was set, so every failing logo was fetched twice before the img was removed.
            "logo": logo_url(c), "logo_ar": logo_ar(c), "logo_mono": logo_mono(c),
            "initials": initials(c)}


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


def _base_rows():
    """Every posting as a card row with score 0, built ONCE per corpus and shared by everyone.

    This is the impersonal 40/41ths of _build_row: sponsor tier, visa routes, logo, dates, pay
    label, badges. None of it is a fact about the reader, so none of it belongs in a per-user
    cache -- and rebuilding it per user is what made a cold feed render take 1,941 ms.

    Keyed on the jobs fingerprint rather than a TTL, so a scrape replaces it and nothing else
    does. get_jobs() FIRST: it is the call that refreshes the fingerprint, exactly as
    user_scores documents for the stored score file.
    """
    rows = get_jobs()
    fp = _jobs_cache.get("fp")
    # "DON'T KNOW" IS NOT A KEY, and the test for it is fp[0], not fp. db.jobs_fingerprint()
    # answers (None, "") when the probe is unavailable -- which is a non-empty tuple and so
    # perfectly TRUTHY. Keyed on that, two different unknown corpora compare equal and the
    # first one's rows are served for the life of the worker. get_jobs() guards its own
    # revalidation with `fp[0] is not None` twenty lines up; this is the same test.
    key = tuple(fp) if fp and fp[0] is not None else None
    hit = _base_rows_cache
    if key is not None and hit["rows"] is not None and hit["fp"] == key:
        return hit["rows"]
    built = [_build_row(j, 0) for j in rows if j.get("url")]
    # One entry, replaced not appended: a different fingerprint means the old corpus is gone.
    # An unusable key stores None, so the next call rebuilds rather than trusting this one.
    _base_rows_cache["fp"], _base_rows_cache["rows"] = key, built
    return built


def ranked_rows(username, resume):
    """The FULL corpus as card rows, sorted by this user's match score (desc), cached per
    (user, profile). Reuses user_scores; the master ordering for both the inline top-N and the
    server-paged /api/feed. Status is NOT baked in (overlaid per request) so the cache is shared
    and immutable. Cheap to filter in Python even at tens of thousands of rows."""
    key = (username, hashlib.md5((resume or "").encode("utf-8")).hexdigest())
    if key in _rows_cache:
        _rows_cache.move_to_end(key)         # a read is a use: see _score_cache
        return _rows_cache[key]
    scores = user_scores(username, resume)
    # Shallow copies over the shared base, so a per-user row can carry a per-user score without
    # writing into a dict every other user is reading. `score_pending` mirrors _build_row's own
    # rule at the point it sets "score": an unreadable JD is unscoreable, and 0 there is a
    # missing number rather than a false one.
    rows = [dict(r, score=(0 if r["score_pending"] else scores.get(r["url"], 0)))
            for r in _base_rows()]
    # DEDUPE AFTER THE OVERLAY, NOT BEFORE, and this is the one ordering constraint here.
    # _dupe_rank tie-breaks on r["score"] -- it prefers the copy that HAS a score -- so folding
    # duplicates in _base_rows() while every score is still 0 would pick a different survivor
    # than this user's scores imply, and the two feeds would disagree about which host's copy of
    # a Greenhouse posting they are showing.
    rows = _dedupe_rows(rows)
    rows.sort(key=lambda r: r["score"], reverse=True)
    # _rows_cache is the expensive one — it is what _ROW_CACHE_BYTES_PER_ROW was measured
    # against — so it gets the same derived limit rather than a second constant to keep in step.
    if len(_rows_cache) >= _cache_max():
        _rows_cache.popitem(last=False)      # least-recently-used, not oldest-inserted
    _rows_cache[key] = rows
    return rows


# --------------------------- similar roles, across employers ---------------------------
# The job page's rail only ever offered "more roles at THIS employer", which is the wrong axis:
# somebody reading a Technical Project Manager posting usually wants technical project management,
# not more Capgemini. This finds the same ROLE somewhere else.
#
# Similarity is cosine over IDF-WEIGHTED title tokens. Raw word overlap does not work on this
# corpus, and not marginally: "manager" is in 3,000+ titles and "senior" in more, so unweighted
# matching calls every Manager job equally similar to every other and the rail fills with noise.
# Weighting by inverse document frequency makes the defining words ("playwright", "pega", "scrum",
# "salesforce") carry the score while the filler carries almost none.
_TITLE_WORD = re.compile(r"[a-z0-9][a-z0-9+#.]*")
# Deliberately short. Seniority and level words ("senior", "ii", "lead") are NOT stopped: they are
# a real part of what makes two roles similar, and IDF already discounts them for being common.
_TITLE_STOP = frozenset((
    "the", "a", "an", "of", "and", "or", "for", "to", "in", "at", "with", "on", "by",
    "job", "jobs", "role", "roles", "position", "opening", "openings", "career", "careers",
    "new", "us", "usa", "u.s", "remote", "hybrid", "onsite", "f", "m", "d",
))
_title_idx = {"fp": None, "idf": None, "toks": None}


def _title_tokens(title):
    return frozenset(w for w in _TITLE_WORD.findall((title or "").lower())
                     if len(w) > 1 and w not in _TITLE_STOP)


def _title_index(rows):
    """({token: idf}, {url: tokens}) over every title in the corpus.

    Cached against the jobs fingerprint rather than per user: scores are personal, titles are
    not. Without the cache this tokenises ~19.5k titles on every job-page render.
    """
    fp = _jobs_cache.get("fp")
    if _title_idx["idf"] is not None and _title_idx["fp"] == fp:
        return _title_idx["idf"], _title_idx["toks"]
    toks, df = {}, collections.Counter()
    for r in rows:
        t = _title_tokens(r.get("title"))
        toks[r.get("url")] = t
        df.update(t)
    n = max(len(rows), 1)
    # +1 in the denominator so a token appearing in every title lands at 0 rather than negative.
    idf = {w: math.log(n / (1.0 + c)) for w, c in df.items()}
    _title_idx.update(fp=fp, idf=idf, toks=toks)
    return idf, toks


def _similar_roles(row, rows, k=6, min_sim=0.34):
    """Up to k postings whose TITLE is closest to this one, at OTHER employers, best first."""
    idf, toks = _title_index(rows)
    mine = toks.get(row.get("url")) or _title_tokens(row.get("title"))
    if not mine:
        return []
    w_mine = {t: idf.get(t, 0.0) for t in mine}
    norm_mine = math.sqrt(sum(v * v for v in w_mine.values()))
    if not norm_mine:
        return []
    home = db.block_key(row.get("company") or "")
    seen, scored = set(), []
    for r in rows:
        if r.get("url") == row.get("url") or r.get("closed"):
            continue
        # Same employer is excluded on purpose: those get their own rail immediately below, and
        # duplicating them would spend this list's six slots saying the same thing twice.
        if db.block_key(r.get("company") or "") == home:
            continue
        rt = toks.get(r.get("url"))
        if not rt:
            continue
        shared = mine & rt
        if not shared:
            continue
        # One shared word is a coincidence once titles get long. "Software Engineer, ML Tech
        # Transfer" and "Paying Transfer Agent Operations Specialist" share only "transfer", which
        # is rare enough that IDF alone scored it well above the threshold. Short titles are exempt:
        # "Scrum Master" has two tokens and must still match "Scrum Master".
        if len(shared) < 2 and len(mine) >= 3 and len(rt) >= 3:
            continue
        norm = math.sqrt(sum(idf.get(t, 0.0) ** 2 for t in rt))
        if not norm:
            continue
        # The dot product of two idf-weighted vectors is the sum of idf SQUARED over the shared
        # tokens. Summing plain idf here scored two identical titles at 0.30 instead of 1.00 --
        # every exact match fell under the threshold and the rail came back empty.
        sim = sum(w_mine[t] * w_mine[t] for t in shared) / (norm_mine * norm)
        if sim < min_sim:
            continue
        # One row per (title, employer). The corpus stores one posting per LOCATION, so without
        # this the rail is routinely six copies of one job in six cities. `rows` arrives in score
        # order, so the copy kept is the best-scoring one.
        key = (r.get("title", "").strip().lower(), db.block_key(r.get("company") or ""))
        if key in seen:
            continue
        seen.add(key)
        scored.append((sim, r.get("score") or 0, r))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    return [r for _sim, _sc, r in scored[:k]]


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
        return core.normalize_prefs((_profile_row(user) or {}).get("search_prefs"))
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
        "expstated": "1" if prefs.get("expstated") else "",
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


# --------------------------- search: typo-tolerant matching + relevance ---------------------------
# The search box used to be one `q in title+company+location` substring test, so a single slipped
# key returned nothing at all and there was no notion of a better or worse match — results came
# back in score order regardless of how well they answered what you typed.
#
# TWIN ALERT: app.js has byte-for-byte equivalents of searchHit/searchRank/_within, and
# scripts/feed_parity.py runs both over the same corpus. Change one, change the other.
_SEARCH_SPLIT_RE = re.compile(r"[^a-z0-9+#.]+")


def searchSplit(s):
    """Query/haystack -> terms. A function rather than an inline split so it reads the
    same as its JS twin, which has to be a function to survive feed_parity's lifting."""
    return [t for t in _SEARCH_SPLIT_RE.split(s) if t]


def _within(a, b, k):
    """Is the edit distance between a and b at most k? Bounded, with an early exit.

    Damerau (optimal string alignment), not plain Levenshtein, because an adjacent SWAP is the
    most common typo there is and plain Levenshtein charges two edits for it: "anaylst" is one
    transposition away from "analyst" but two substitutions, so at a 7-character term's tolerance
    of 1 it would not have matched. Same for "amazno", "teh", "recieve".
    """
    la, lb = len(a), len(b)
    if abs(la - lb) > k:
        return False
    if a == b:
        return True
    inf = k + 1
    prev2 = None
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [inf] * (lb + 1)
        cur[0] = i
        lo, hi = max(1, i - k), min(lb, i + k)
        for j in range(lo, hi + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                v = min(v, prev2[j - 2] + 1)          # the transposition
            cur[j] = v
        if min(cur[lo:hi + 1] or [inf]) > k:
            return False                       # no cell on this row can still reach k
        prev2, prev = prev, cur
    return prev[lb] <= k


def _search_tol(term):
    """How many typos to forgive, by term length. Short terms get none: at three letters an edit
    of one turns 'api' into 'app' and the results stop meaning anything."""
    n = len(term)
    if n >= 8:
        return 2
    if n >= 4:
        return 1
    return 0


# _within is pure and its arguments repeat enormously: over a 20k-row corpus the same handful of
# title words ("engineer", "manager", "analyst") is compared against the same query term tens of
# thousands of times. Measured on "software engineer": 17,622 calls, a few hundred distinct pairs.
# Bounded so a long-lived worker cannot grow without limit; cleared wholesale rather than evicted
# one at a time, because the contents are worth nothing once they stop being hot.
_WITHIN_MEMO = {}
_WITHIN_MEMO_MAX = 60000


def _within_memo(a, b, k):
    key = (a, b, k)
    hit = _WITHIN_MEMO.get(key)
    if hit is None:
        if len(_WITHIN_MEMO) >= _WITHIN_MEMO_MAX:
            _WITHIN_MEMO.clear()
        hit = _WITHIN_MEMO[key] = _within(a, b, k)
    return hit


def termHit(hay, words, term):
    """Does one query term appear in this haystack, allowing a typo? Shared by searchHit (which
    decides IF a row matches) and searchRank (which decides where it lands), so the two can never
    disagree about what counted as a match."""
    if term in hay:
        return True                            # covers prefixes and infixes for free
    tol = _search_tol(term)
    if not tol:
        return False
    for w in words:
        # Two cheap gates before the expensive part. The length window is free (edit distance is
        # at least the length difference). The first-letter gate is a real trade: a typo in the
        # FIRST character is not forgiven, which is the rare case, and in exchange ~95% of
        # candidate words are dropped before any distance is computed.
        if w[0] != term[0] or abs(len(w) - len(term)) > tol:
            continue
        if _within_memo(w, term, tol):
            return True
    return False


# The searchable text of every row, tokenised once per corpus rather than once per request. The
# split was measured at 133ms of a 488ms search over 20k rows -- pure repeated work, since the
# titles do not change between keystrokes. Keyed on the jobs fingerprint like _title_index.
_hay_idx = {"fp": None, "hay": None, "words": None}


def _row_haystack(row):
    """(searchable text, its words) for one row, cached corpus-wide."""
    fp = _jobs_cache.get("fp")
    # `is None` on the store, not just a fingerprint comparison: the fingerprint is itself None
    # before the first snapshot read, so comparing fingerprints alone leaves the maps unbuilt.
    if _hay_idx["hay"] is None or _hay_idx["fp"] != fp:
        _hay_idx.update(fp=fp, hay={}, words={})
    u = row.get("url")
    h = _hay_idx["hay"].get(u)
    if h is None:
        h = ((row.get("title") or "") + " " + (row.get("company") or "") + " " +
             (row.get("location") or "")).lower()
        _hay_idx["hay"][u] = h
        _hay_idx["words"][u] = searchSplit(h)
    return h, _hay_idx["words"][u]


def searchHit(hay, q, words=None):
    """Does this haystack answer the query? Substring first, then per-term, then typo-tolerant.

    `words` is the haystack already tokenised. Callers with a cache pass it; everyone else leaves
    it out and it is computed LAZILY, so a correctly spelled query -- where every term is a plain
    substring -- never tokenises anything at all.
    """
    if not q:
        return True
    if q in hay:
        return True                            # phrase match: the old behaviour, still first
    terms = searchSplit(q)
    if not terms:
        return False
    for term in terms:
        if term in hay:
            continue
        if words is None:
            words = searchSplit(hay)
        if not termHit(hay, words, term):
            return False
    return True


def searchRank(row, q):
    """Where a matching row belongs in the results, highest first.

    Two parts. The BAND says where the query was answered -- the title as a phrase beats the title
    as separate words, which beats the employer or city, which beats a match only reachable by
    forgiving a typo. The COVERAGE inside a band says how much of the title the query actually
    explains, which is what stops "Staff Scientist - Real World Evidence and Data" outranking
    "Senior Data Scientist" for "data scientst": both contain both words, but one of them is
    two-thirds about them and the other is two-sevenths.

    Integer arithmetic throughout, and floor division rather than rounding, because the JS twin
    has to produce the identical number and the two languages round halves differently.
    """
    if not q:
        return 0
    title = (row.get("title") or "").lower()
    hay = title + " " + (row.get("company") or "").lower() + " " + (row.get("location") or "").lower()
    terms = searchSplit(q)
    twords = searchSplit(title)
    if q in title:
        band = 4
    elif terms and all(termHit(title, twords, t) for t in terms):
        band = 3
    elif q in hay:
        band = 2
    elif terms and all(t in hay for t in terms):
        band = 1
    else:
        band = 0
    cov = 0
    if twords and terms:
        m = sum(1 for w in twords if any(termHit(w, (w,), t) for t in terms))
        cov = min((m * 100) // len(twords), 99)
    return band * 100 + cov


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
    exp_stated = (p.get("expstated") or "") in ("1", "true", "yes", "on")
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
            # HIDDEN AND APPLIED both drop off the default tab. Only `hidden` used to, so a
            # posting you had already applied to went on competing for space in the very feed
            # you use to decide what to apply to NEXT — while the Applied tab listed it and
            # _autolog_application had already written a tracker row. The app knew; the
            # recommendation surface did not consult it. ext_apply_queue reached the same
            # conclusion for the batch filler and added its own guard; this is the human half.
            # `liked` deliberately stays: saving something is a reason to keep seeing it.
            if st in ("hidden", "applied"):
                continue
            if not (searching or r["score"] >= minv):   # search bypasses the match floor
                continue
        # Search covers LOCATION too — "boston" and "remote" are things people type here.
        if searching:
            _hay, _words = _row_haystack(r)
            if not searchHit(_hay, q, _words):
                continue
        if cut:
            rdate = _row_date(r)
            if rdate and rdate < cut:
                continue
        if hide_no and r["sponsor_jd"] == "blocked":
            continue
        if verified_only and not r.get("date_trusted"):
            continue
        # Both of these open with `if not wanted: return True`, so an unset control made a
        # Python call per row to be told nothing. Gating on the parsed pref is exactly
        # equivalent and skips ~2 calls x the whole corpus on the common path. The condition
        # stays a single expression per line so the app.js twin still reads as a mirror.
        if want_roles and not core.roles_match(r.get("roles"), want_roles, r.get("jd_admit")):
            continue
        if want_visa and not core.visa_tags_match(r.get("visa"), want_visa):
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
        # "Only postings that state their years." OFF by default; see core.DEFAULT_PREFS for the
        # measurement behind it (72% of results under a years filter state no number at all).
        if exp_stated and (r["exp_years"] == "" or r["exp_years"] is None):
            continue
        if exp != "any":
            # exp_years is the HIGHEST year count the JD states (core.experience_years), so
            # "8+ years required; 2 years of SQL preferred" is an 8-year job and "<=2 yrs"
            # drops it. A JD that states no number is ALWAYS kept — many genuine entry-level
            # posts state none, and the card badge marks them so the two populations are
            # distinguishable. Mirrored in app.js matches() and core.prefs_match().
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
    # RELEVANCE FIRST while a search is active, the chosen sort within each band. A separate
    # stable pass rather than a compound key, so the sort you picked still fully decides the
    # order inside a band and this adds nothing at all when the box is empty.
    if searching:
        out.sort(key=lambda rs: -searchRank(rs[0], q))
    return out                                  # else already in score order (rows pre-sorted)


def _signed_out_response(reason):
    """How to say "you are signed out" to whoever is asking.

    A 302 to an HTML login page is the right answer for a browser navigation and the WRONG one
    for a fetch(). fetch follows the redirect, gets HTML with status 200, r.json() throws, and
    app.js's .catch renders "We couldn't load jobs. Try again." — so a user whose 30-day cookie
    lapsed sat on a permanently dead feed that never once said they had been signed out. On Load
    more it was worse: `reset` is false there, so the .catch body does nothing at all and the
    button simply stopped working.

    The neighbouring code already knows this. _require_csrf answers programmatic callers with
    JSON for exactly this reason ("A fetch() that gets a 302 to an HTML page fails silently in
    the console"), and app.js reads a 429 explicitly because a rate-limited reply once rendered
    as "No jobs match these filters". login_required never got the same treatment.
    """
    from flask import jsonify
    if request.path.startswith("/api/"):
        resp = jsonify({"ok": False, "error": "signed out", "signed_out": True,
                        "message": reason or "Your session expired. Sign in again.",
                        "login": url_for("login", next=request.path),
                        # app.js reads `rows`/`total`; without them an older cached copy of it
                        # renders this as an empty search rather than as a sign-out.
                        "rows": [], "total": 0, "has_more": False})
        resp.status_code = 401
        return resp
    return redirect(url_for("login", next=request.path))


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
            return _signed_out_response("")
        dead = _session_dead(user)
        if dead:
            session.clear()
            if request.path.startswith("/api/"):
                return _signed_out_response(dead)
            flash(dead)
            return redirect(url_for("login"))
        return f(*a, **k)
    return wrap


@app.context_processor
def _inject():
    return {"current_user": session.get("user"),
            "csp_nonce": getattr(g, "csp_nonce", "")}


# ----------------------------- error pages -----------------------------
# There were no error handlers at all, so /no-such-page returned Werkzeug's stock page: Times New
# Roman on white, no nav, no header, no link back, no dark mode, and the stack in the body. That
# is jarring precisely BECAUSE the rest of this app is careful — skip link, three-way theme
# toggle, a token system, contrast gated in CI — and one mistyped URL dropped the reader out of
# every bit of it.
#
# JSON for /api/*, same test login_required now uses: those callers are fetch(), and an HTML body
# with the wrong content type is how a 429 once rendered as "No jobs match these filters".
def _error_response(code, title, message, log=None):
    from flask import jsonify
    if log is not None:
        app.logger.exception("unhandled error on %s", request.path, exc_info=log)
    if request.path.startswith("/api/"):
        resp = jsonify({"ok": False, "error": title, "message": message,
                        # app.js reads these; without them an error renders as an empty search.
                        "rows": [], "total": 0, "has_more": False})
        resp.status_code = code
        return resp
    # render_template can itself fail (a broken base.html, a missing static file), and a 500
    # handler that 500s is a blank page. Fall back to plain text rather than to nothing.
    try:
        return render_template("error.html", code=code, title=title, message=message), code
    except Exception:
        return Response("%d %s\n%s\n" % (code, title, message), status=code,
                        mimetype="text/plain")


@app.errorhandler(404)
def _handle_404(_e):
    return _error_response(
        404, "Page not found",
        "That link doesn't lead anywhere. It may have moved, or the address has a typo in it.")


@app.errorhandler(500)
def _handle_500(e):
    return _error_response(
        500, "Something broke on our side",
        "That is our fault, not yours. Nothing you were doing was lost — try again, and if it "
        "keeps happening the details are in the server log.", log=e)


# --- company logo helpers (Google favicon by domain, with a letter-avatar fallback;
#     logo.clearbit.com is DEAD — Clearbit sunset the free logo API) ---
_DOMAIN_MAP = {
    "affirm": "affirm.com", "airbnb": "airbnb.com", "alixpartners": "alixpartners.com",
    "amazon": "amazon.com", "analog devices": "analog.com", "analogdevices": "analog.com",
    # No rule reaches these: the posting host is a platform tenant ("seic.wd1...",
    # "flagstar.wd5...") or the real domain resembles nothing in the name (umn.edu).
    "flagstar bank": "flagstar.com", "sei investments": "seic.com",
    "university of minnesota": "umn.edu",
    "anaplan": "anaplan.com", "aurora innovation": "aurora.tech",
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
# THE EIGHT-COLOUR HASH PALETTE IS GONE, 2026-08-22. It picked a tile colour from
# sum(ord(c)) % 8, which made it the single largest chromatic spend in the product and put it
# squarely against the rule at the top of static/style.css: colour means sponsorship, everything
# else is ink. A monogram is ink on a neutral plate now; core.initials owns the two letters.
#


# Hosts belonging to a hiring PLATFORM rather than to the employer. A posting on one of these
# says nothing about the company's own domain — "seic.wd1.myworkdayjobs.com" is a Workday tenant
# name, not a website — so the URL is only trusted when its host is none of them.
_PLATFORM_HOSTS = (
    "myworkdayjobs.com", "greenhouse.io", "lever.co", "ashbyhq.com", "smartrecruiters.com",
    "icims.com", "jobvite.com", "workable.com", "bamboohr.com", "taleo.net", "successfactors.com",
    "sapsf.com", "avature.net", "jobdiva.com", "ultipro.com", "paylocity.com", "oraclecloud.com",
    "eightfold.ai", "recruitics.com", "rippling.com", "isolvedhire.com", "apploi.com",
    "phenompeople.com", "peoplefluent.com", "silkroad.com", "brassring.com", "dayforcehcm.com",
)


_VERIFIED_DOMAINS_PATH = "company_domains.json"
_verified_cache = None


def _verified_domains():
    """{company lowercased: domain} from company_domains.json, or {} if it is not deployed.

    Loaded once per worker and never reloaded — it is a build artefact, like idf.json, and a
    file that changes under a running process is a source of two workers disagreeing.
    """
    global _verified_cache
    if _verified_cache is None:
        try:
            with open(_VERIFIED_DOMAINS_PATH, encoding="utf-8") as fh:
                _verified_cache = (json.load(fh) or {}).get("domains") or {}
        except Exception:
            _verified_cache = {}
    return _verified_cache


@app.template_filter("companydomain")
def company_domain(name, url=None):
    """The domain to ask Google's favicon service for.

    THE JOB'S OWN URL BEATS ANY GUESS, when it is the employer's site. The old rule was
    name -> strip non-alphanumerics -> append ".com", which is right for "Tesla" and wrong for
    most things with more than one word. Measured against the live corpus it produced
    northwestern.com for a university whose postings sit on careers.northwestern.edu,
    universityofminnesota.com for one on hr.myu.umn.edu, flagstarbank.com for flagstar.com, and
    seiinvestments.com for seic.com — each a 404 from the favicon service, and on the job page a
    404 leaves a white square over the coloured initial rather than falling back to it.

    There are 52 distinct universities and colleges in the corpus, and the naive rule gets
    essentially all of them wrong while their careers pages sit on the .edu domain that answers
    the question. So: if the posting is hosted on something that is not a hiring platform, take
    the registrable domain from it. If it IS on a platform, the URL is a tenant name and tells us
    nothing, so fall back to the map and then to the guess.

    _DOMAIN_MAP still wins over both — it is the place to record the cases no rule can reach,
    like Analog Devices, whose Workday tenant is "analogdevices" while the site is analog.com.
    """
    key = (name or "").strip().lower()
    if key in _DOMAIN_MAP:
        return _DOMAIN_MAP[key]
    # THE VERIFIED MAP, ahead of every rule below it: the rules guess, and this file does not.
    # Absent file = absent entry = the old behaviour exactly.
    #
    # KEYED ON core.norm_company, NOT ON THE RAW NAME, and that is a fix rather than a detail.
    # The file used to be written and read under two different keys: this lookup used the raw
    # lowercased name while scripts/build_companies.py rebucketed the same file through
    # core.norm_company. It held BOTH 'apple' -> apple.com and 'apple, inc.' -> appleinc.com,
    # both normalising to 'apple', and the rebucket kept whichever it read last -- so the file
    # gave two different answers to one question and both were live. company_domain("Apple")
    # returned apple.com while the /companies tile rendered appleinc.com, a parked domain.
    # Seven keys had that conflict: apple, block, gap, uline, skydio, aldridge, lonza.
    hit = _verified_domains().get(core.norm_company(name) or key) or _verified_domains().get(key)
    if hit:
        return hit
    host = ""
    try:
        host = (urlsplit(url or "").hostname or "").lower()
    except Exception:
        host = ""
    base = re.sub(r"[^a-z0-9]", "", key)        # join words -> best-effort guess
    labels = [p for p in host.split(".") if p]
    if base and len(labels) >= 2 and not any(p in host for p in _PLATFORM_HOSTS):
        # THE URL ONLY CORROBORATES, IT NEVER OVERRIDES. Taking the posting's domain whenever it
        # was not on a known platform list was measured over all 1,780 companies in the corpus
        # and CHANGED 178 — but a large share of those were regressions, because the list of
        # hiring platforms has an unenumerable tail: Udemy went udemy.com -> careerpuck.com,
        # Maximus -> equest.com, Capgemini -> talentnet.community, Stashinvest -> comparably.com.
        # Each replaced a correct guess with a confidently wrong one. "Not on my list" does not
        # mean "the employer's own site".
        #
        # So the domain is accepted only when it AGREES with the company name — one is a prefix
        # of the other. That keeps every real fix where the name already matched
        # (cornelluniversity -> cornell.edu, northwestern -> northwestern.edu, caddellconstruction
        # -> caddell.com) and rejects every platform host, because no ATS is named after its
        # client. Cases where the true domain resembles nothing in the name (University of
        # Minnesota -> umn.edu) stay wrong, which is no worse than before and is what _DOMAIN_MAP
        # is for.
        root = labels[-2]
        if root and (root.startswith(base) or base.startswith(root)):
            return ".".join(labels[-2:])
    return (base or "example") + ".com"


# WHY THE LOGOS ARE OURS NOW, AND NOT A SERVICE'S.
#
# This used to be a three-tier chain: logo.dev when LOGODEV_KEY was set, else gstatic's
# faviconV2, else a coloured letter. LOGODEV_KEY was never set in production, so every tile in
# the product came from the favicon service -- and it was asked with fallback_opts=TYPE,SIZE,URL,
# which tells Google to GENERATE an icon when the domain has none. That guarantees HTTP 200, so
# no error handler could ever fire and no fallback chain could help.
#
# Measured over 150 companies that had a stored domain: 46.7% usable, 22.7% a solid brand-colour
# block with no mark in it, 14.0% a monochrome browser-chrome glyph, 14.0% under 48px upscaled
# into a 48px tile, 2.0% a 404, 0.7% a blank 200 that painted an opaque white square OVER the
# letter it was supposed to fall back to. 53% was not a usable brand logo.
#
# So scripts/build_logos.py harvests them once, judges them on their PIXELS, and commits them to
# static/logos/. Nothing is fetched from a third party at request time, which is also why the CSP
# above no longer allows any remote image origin: Clearbit's free logo API -- the one every
# tutorial still recommends -- was switched off on 2025-12-08, and a page whose images come from
# somebody else's free tier breaks on somebody else's schedule.
_LOGO_MANIFEST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "static", "logos", "index.json")
_logo_cache = None


def _logo_manifest():
    """{'v': int, 'ar': {slug: [ext, aspect, mono]}, 'alias': {norm_name: slug}}.

    Loaded once per worker and never reloaded, exactly like _verified_domains above: it is a
    build artefact, and a file that changes under a running process is a source of two workers
    disagreeing. Read by ABSOLUTE path rather than relative to the cwd, so a process started from
    the wrong directory gets the real manifest instead of silently getting none.
    """
    global _logo_cache
    if _logo_cache is None:
        try:
            with open(_LOGO_MANIFEST_PATH, encoding="utf-8") as fh:
                blob = json.load(fh) or {}
            _logo_cache = {"v": blob.get("v") or 0, "ar": blob.get("ar") or {},
                           "alias": blob.get("alias") or {}}
        except Exception:
            _logo_cache = {"v": 0, "ar": {}, "alias": {}}
    return _logo_cache


def _logo_slug(name):
    """The manifest key for a company, or "" .

    Two lookups, because the corpus and the sponsor data spell employers differently: the direct
    slug, then the normalised name through the manifest's alias map. That alias map is what makes
    "Accenture LLP" find Accenture's logo, and it is the same class of fix as the corpus-spelling
    ladder /companies already uses for its ?c= links.
    """
    man = _logo_manifest()
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    if slug in man["ar"]:
        return slug
    alias = man["alias"].get(core.norm_company(name) or "")
    return alias if alias and alias in man["ar"] else ""


@app.template_filter("logourl")
def logo_url(name):
    """The company's logo path, or "" when it has none.

    ?v= is one manifest-wide integer rather than static_v()'s per-file mtime: static_v stats the
    file on every call, and /companies renders up to 2,695 tiles. web.py's fingerprint check keys
    on the presence of ?v=, so this still earns the long immutable Cache-Control for free.
    """
    slug = _logo_slug(name)
    if not slug:
        return ""
    man = _logo_manifest()
    return "/static/logos/%s.%s?v=%d" % (slug, man["ar"][slug][0], man["v"])


@app.template_filter("logomono")
def logo_mono(name):
    """1 when this logo is a single DARK ink, else 0.

    The card inverts a mono mark in dark mode instead of putting a light plate behind it, which
    is the difference between a logo that sits in the card and a white rectangle stuck on it.
    Only meaningful because the harvester guarantees darkness: judge() composites onto white and
    rejects a blank, so an accepted raster mono logo cannot be a white knockout, and svg_meta
    checks the ink's luminance for the one source that composites nothing.
    """
    slug = _logo_slug(name)
    if not slug:
        return 0
    row = _logo_manifest()["ar"][slug]
    return 1 if (len(row) > 2 and row[2]) else 0


@app.template_filter("logoar")
def logo_ar(name):
    """The logo's intrinsic aspect ratio, or 0. The card reserves width from it, so a wide
    wordmark does not reflow the tile when it loads."""
    slug = _logo_slug(name)
    return (_logo_manifest()["ar"][slug][1] or 0) if slug else 0


@app.template_filter("initials")
def initials(name):
    """Two letters for the monogram tile. core.initials owns the rule.

    It lives in core rather than here because scripts/build_logos.py records the same value
    in the harvest ledger and scripts/test_logos.py freezes it, and for a while this file and
    the harvester each had their own identical copy.
    """
    return core.initials(name)


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
            # NO flash() HERE. login.html already renders an inline error beside the password
            # field, and it is the better of the two: it sits next to the inputs, and it carries
            # role="alert", aria-invalid and aria-describedby on BOTH fields (deliberately both,
            # so it cannot leak which half was wrong). The banner said the same thing in
            # different words ~450px away, and inserting it pushed the card down so the form
            # visibly jumped on submit. The flashes above this — "Too many sign-in attempts",
            # "Couldn't reach the database", "That account has been disabled" — stay, because
            # the template has no field to attach those to.
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
    # The default ("Recommended") view under the user's saved search. This pass used to run for
    # its len() alone and then be thrown away — after which the browser spent a whole round trip
    # asking /api/feed to compute the very same thing. Keep the rows: the first page of THIS is
    # exactly what /api/feed?offset=0 would have answered, so shipping it inline removes a
    # serial network round trip (~650 ms on this host) from first paint.
    default_rows = _filter_rows(rows, statuses, _prefs_as_params(prefs))
    default_total = len(default_rows)
    if paged:
        # _filter_rows hands back (row, status) PAIRS in display order. Unpacked exactly the way
        # api_feed unpacks its own page, so the bootstrap is the same shape and the same rows
        # that /api/feed?offset=0 would have returned — that equivalence is the whole point.
        feed_rows = [dict(r, status=st) for (r, st) in default_rows[:_FEED_TOPN]]
    else:
        # Unpaged corpora still inline EVERYTHING, unfiltered: app.js filters those client-side
        # and needs the whole set to do it.
        feed_rows = [dict(r, status=statuses.get(r["url"], "")) for r in rows]
    # Work-authorization nudge. visa_alert returns None unless something is actually close, so
    # a user with no dates entered — or with months of runway — sees nothing at all.
    try:
        vprof = _profile_row(user) or {}
        vtl = core.visa_timeline(vprof)
        visa = core.visa_alert(vtl)
        visa_ctx = _visa_badge_context(vprof, vtl)
    except Exception:
        visa, visa_ctx = None, {}
    return render_template("feed.html", feed_rows=feed_rows, has_resume=bool(resume),
                           total=total, default_total=default_total, counts=counts,
                           default_min=default_min, paged=paged,
                           # NO metros=/states= here. feed.html never read either one — the
                           # location box takes free text — and each was a full pass over the
                           # whole corpus (a Counter, then a set-and-sort) on the hottest route
                           # in the app. /welcome is the only consumer and it builds its own
                           # from _onboard_rows(). Re-adding them means re-adding the passes.
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
# Employers we have already looked up and found nothing for: {(domain guesses) -> when}. Bounded
# so a long-lived worker cannot grow it without limit; the contents are worthless once cold.
_research_miss = {}
_RESEARCH_MISS_MAX = 4000


def _research_for(display):
    """The Resume Brain research record for an employer, or {}.

    Exact domain first — that is how the KB is keyed. Failing that, match on the record's own
    `name`, because company_domain() guesses a domain from the feed's spelling and the two rarely
    agree: the corpus says "BYD America" (-> bydamerica.com) where the crawler filed "BYD" under
    byd.com. The name index is cached, since the miss path is the common one until the KB fills
    up and it would otherwise re-read the table on every company page view.
    """
    # NEGATIVE CACHE. Measured on a warm GET /job: this function made TWO Supabase round trips,
    # 49 ms + 43 ms, to fetch 180 bytes each and discover that the employer has no record — and it
    # did that on every render of every job at that employer, forever. Most employers have no
    # record, so the miss path is the common one. A hit is still read live; only the ABSENCE is
    # remembered, and only for _RESEARCH_TTL, so a crawl that lands mid-window is picked up within
    # five minutes rather than never.
    doms = tuple(d for d in (company_domain(display), _research_domain(display)[0]) if d)
    miss_at = _research_miss.get(doms)
    if miss_at is not None and time.time() - miss_at < _RESEARCH_TTL:
        doms = ()                       # known-absent and still fresh: skip both round trips
    # BOTH domain guesses, because they disagree and each is the right key some of the time.
    # research._norm_name strips inc|llc|ltd|corp|co|company|the before building a domain and
    # company_domain does not, and their hand-written domain maps are different sets. So the crawler
    # files "Amazon.com Services LLC" under one spelling while this lookup asks for the other,
    # and on-demand research would appear to silently do nothing for a whole class of employers.
    for dom in doms:
        try:
            rec = db.get_brain_company(dom)
            if rec:
                _research_miss.pop(doms, None)
                return rec
        except Exception:
            pass
    if doms:                            # both guesses missed — remember that, not the emptiness
        if len(_research_miss) >= _RESEARCH_MISS_MAX:
            _research_miss.clear()      # bounded; cheap to refill, worthless once cold
        _research_miss[doms] = time.time()
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
    # The same three exclusions /job's keyword panel makes, because this chip list is captioned
    # "Ranked by how heavily this employer's own descriptions weight them" and was therefore
    # ranking "applied materials" FIRST on Applied Materials' own page, alongside bare noise like
    # "type", "target", "website" and "trend". A company's name is not a skill it hires for, and
    # neither is its dental plan.
    skills, seen_urls = collections.Counter(), {r["url"] for r in open_rows}
    skill_stop = set(_SKILL_STOP) | _KEYWORD_STOP | core.PERK_TERMS
    try:
        skill_stop.update(w for w in core.norm_company(display or "").split() if len(w) > 2)
    except Exception:
        pass
    for j in get_jobs():
        if j.get("url") not in seen_urls:
            continue
        a = job_analysis(j)
        for t, w in (a.get("weight") or {}).items():
            low = (t or "").lower()
            if low in skill_stop or len(low) < 2:
                continue
            if all(p in skill_stop for p in low.split()):
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
        # KEYED ON THE NAME, so the indirection this used to need is gone. It read the domain
        # back out of rows[0] because company_domain() accepts a posting URL as corroboration and
        # this call site never passed one, which made 219 companies show a DIFFERENT logo on the
        # employer page from the cards listed underneath it. A name cannot disagree with itself.
        "logo": logo_url(display), "logo_ar": logo_ar(display),
        "logo_mono": logo_mono(display),
        "initials": initials(display),
    }
    analytics.emit(user, getattr(g, "sid", ""), "page_view", page="company",
                   company=display, n=len(open_rows))
    return render_template("company.html", info=info, company_arg=display,
                           about=_company_profile(display, key, rows, open_rows),
                           # Same ladder /companies renders from, so a card and the page it
                           # opens can never disagree about where somebody applies.
                           links=_company_links(display),
                           # _feedgrid.html reads this to decide whether to draw a % ring.
                           # Omit it and every card here would suppress its score, including
                           # for users who do have a résumé.
                           has_resume=bool(current_profile()),
                           # Seeds the sort control, which used to hardcode "Best match" here —
                           # so a user whose saved sort was Newest or Sponsorship silently got
                           # score order on this page only. app.js's filter memory covers the
                           # feed -> company path; this covers a cold load straight to /company.
                           prefs=_user_prefs(user),
                           # The shared filter bar (_filterbar.html) reads these. /company used
                           # to render only #q and #sort, so an employer with 500 openings had no
                           # way to narrow them without leaving the page. _filter_rows already
                           # runs over the company-narrowed rows in api_feed, so this is markup
                           # and template context only — no new server behaviour.
                           visa_tag_controls=_VISA_TAG_CONTROLS,
                           default_min=0,          # start unfiltered: you came here for THIS
                                                   # employer, not for your feed's match floor
                           default_total=len(open_rows),
                           role_groups=core.role_families_grouped(),
                           role_counts=role_counts(), role_max=ROLE_PICK_MAX,
                           visa_labels=core.VISA_TAG_LABELS,
                           visa_tips=core.VISA_TAG_TIPS)


# --------------------------- one job ---------------------------
_SKILL_SHOWN = 18            # matched/missing chips; /api/job used 30 and nothing needed more
# INLINE highlights are capped far lower than the chip lists, and this is the whole difference
# between a useful page and an unreadable one. core.score_against returns terms sorted by
# descending IDF weight, so the head of the list is the signal and the tail is words like
# "least", "based", "services" and "source" that happen to be in the description. At 24 terms a
# real Capital One posting came back with 71 marks and read as a highlighter accident; the top
# ten of each carry the meaning. The chips below the description can afford to be longer because
# a list is scanned, not read through.
_HL_TERMS = 10


# Words that are never a skill but score well because a job description repeats them. Grown from
# a real Capital One posting, which offered "regarding criminal", "background inquiries",
# "applicable federal", "york", "posted", "state" and "laws" as keywords to add to a résumé. The
# boilerplate rule below catches most of that class by shape; these are the leftovers that sit in
# ordinary prose.
_KEYWORD_STOP = frozenset("""
posted posting position role job company employer candidate applicant applicants
state states city york county country federal laws law legal notice notices least
website site email phone contact address information available provide provided
please based employment technology technologies tools services service solutions
business teams environment opportunity support various including needs help
""".split())


def _useful_terms(terms, company, jd, cap):
    """Keywords worth showing a reader, weight order preserved.

    Four things get dropped, in cheapness order:
      * the generic-skill stoplist _company_profile already uses, plus the ones above
      * the employer's own name. It is genuinely one of the highest-weighted terms in any
        description and says nothing: "Capital One" was marked six times in one posting.
      * anything under three characters, or a multi-word term made only of stopwords
      * TERMS THAT ONLY EVER APPEAR IN LEGAL BOILERPLATE. analyze_jd reads the whole
        description, EEO notice included, so the raw list contains phrases from it. Subtracting
        the boilerplate text is a property of this posting rather than a blacklist to maintain,
        and it is what stops the page advising somebody to put "regarding criminal" on a résumé.
    """
    # PERKS AND BENEFITS. text_halves below separates the LEGAL notice, which is a different
    # thing: an EEO paragraph is boilerplate by shape, while a benefits section is ordinary prose
    # sitting in the body, so the "in the notice and nowhere else" rule never touched it. That is
    # why /job offered "retirement", "dental", "tuition" and "flexible time" as keywords worth
    # adding to a résumé — the most visible way this panel can lose a reader's trust, because
    # the error is obvious to them while the rest of it is not verifiable at a glance.
    stop = set(_SKILL_STOP) | _KEYWORD_STOP | core.PERK_TERMS
    stop.update(w for w in re.split(r"\W+", (company or "").lower()) if len(w) > 2)
    # The company as the CORPUS spells it, not only as this row does. A row mislabelled "Amat"
    # subtracted nothing from a description that opens "Applied Materials is a global leader",
    # which is how the employer's own name came to be marked red under a legend reading "Red is
    # one worth adding". canonical_url now folds the Workday casing that caused that split
    # (see the note there), and this covers the rows already stored under the alias.
    try:
        stop.update(w for w in core.norm_company(company or "").split() if len(w) > 2)
    except Exception:
        pass
    try:
        body, boiler = jdrender.text_halves(jd or "")
    except Exception:
        body, boiler = (jd or "").lower(), ""
    out = []
    for t in terms:
        low = (t or "").strip().lower()
        if len(low) < 3 or low in stop:
            continue
        if all(w in stop or len(w) < 3 for w in low.split()):
            continue
        # In the notice but not in the rest of the posting: it is a legal phrase, not a skill.
        if boiler and low in boiler and low not in body:
            continue
        out.append(t)
        if len(out) >= cap:
            break
    return out


def _company_brief(display, open_rows):
    """The company block on a JOB page: what they do, plus the facts that exist for everyone.

    Deliberately NOT _company_profile(). That one iterates the ENTIRE get_jobs() corpus and calls
    job_analysis() on every one of this employer's postings to rank their skills, which is fine
    once per company page and not fine on a page somebody opens for one job. web.py's own
    docstring at the streaming unpack measures a full feed's scoring at ~1.05s, so an employer
    with 500 openings would put tens of milliseconds of pure CPU on every job view for a chip row
    that /company already shows one click away.

    So: no jd_terms aggregation and no per-year history bars. Everything here is either already
    on the rows we were handed or a dict lookup.
    """
    try:
        research = _research_for(display) or {}
    except Exception:
        research = {}
    strength, scount = core.sponsor_strength(display, sponsor_counts())
    states = collections.Counter(r["loc_state"] for r in open_rows if r.get("loc_state"))
    return {
        "research": research,
        "researched": bool(research.get("what_they_do") or research.get("about")
                           or research.get("mission")),
        # Same key name and the same cleaning as the company page, for the reason spelled out in
        # _company_profile: Jinja resolves `about.values` to dict.values() and hands the template
        # a bound method that then fails to iterate.
        "company_values": _clean_research_list(research.get("values"), no_digits=True),
        "initiatives": _clean_research_list(research.get("initiatives"), hi=160, cap=6),
        "tech_stack": _clean_research_list(research.get("tech_stack"), hi=28, cap=12),
        "n_open": len(open_rows),
        "states": states.most_common(4),
        "remote": sum(1 for r in open_rows if r.get("remote")),
        "strength": strength, "strength_n": scount,
        "site": (research.get("pages") or [None])[0],
        "fetched_at": research.get("fetched_at"),
    }


def _and_list(items):
    """['a','b','c'] -> 'a, b and c'. Jinja's join() can only repeat one separator, so
    `narrowed|join(' and ')` rendered "H-1B and Green Card and E-3 and H-1B1"."""
    items = [str(i) for i in (items or []) if str(i).strip()]
    if len(items) <= 1:
        return items[0] if items else ""
    return "%s and %s" % (", ".join(items[:-1]), items[-1])


def _route_of(row):
    """The data-route value, server-side. Mirrors the one expression in app.js cardHTML.

    Duplicated deliberately rather than shared: it is one ternary, and the alternative is
    shipping a computed field the card does not need. scripts/test_job_page.py asserts the page
    and the card agree for every combination.
    """
    if (row.get("sponsor_jd") or "") == "blocked":
        return "blocked"
    return row.get("visa_likely") or "none"


@app.route("/job")
@login_required
def job_page():
    """One posting, in full: routes, the company, the description and the keywords.

    Replaces the slide-in modal. A job now has a URL, so it can be linked, bookmarked, reopened
    from history and left with the Back button, none of which an overlay could do.

    The identifier travels as ?u= for the reason /company documents at length: job URLs are full
    of slashes and on cPanel/Passenger behind Apache a %2F inside a path segment is 404'd before
    Flask ever sees it. ?url= is accepted too, because that is what /api/job used and somebody
    will have it in a bookmark.

    The row comes from ranked_rows, NOT from get_jobs() plus a fresh _build_row. Three reasons,
    and the third is a correctness bug rather than a preference: it is the same cached list the
    feed and /company read, so the score, the chip and the route are the same objects the card
    showed; the scan is a linear pass over ~19k dicts comparing one string, which /company
    already pays twice per render; and ranked_rows applies _dedupe_rows, which DROPS rows, so a
    page built from get_jobs() would happily render a posting the feed deliberately collapsed
    away, complete with a live Apply button.
    """
    url = (request.args.get("u") or request.args.get("url") or "").strip()
    if not url:
        return redirect(url_for("feed"))
    user = session["user"]
    rows = ranked_rows(user, current_profile())
    row = next((r for r in rows if r.get("url") == url), None)

    if row is None:
        # Not in the ranked list. Either it was deduped away (the same posting reached us from
        # two hosts and the feed kept the other copy) or it is gone. Send the duplicate to its
        # survivor so an old bookmark lands on the employer's own host rather than a 404, and
        # 302 rather than 301 because that preference is decided at render time and reversible.
        raw = _job_for(url)
        if raw is not None:
            key = _dupe_key(_build_row(raw, 0))
            twin = next((r for r in rows if key is not None and _dupe_key(r) == key), None)
            if twin is not None:
                return redirect(url_for("job_page", u=twin["url"]))
        # 404, not a redirect with a flash: a redirect breaks the Back button and hides the
        # reason. 404 rather than 410 because some caches treat Gone as permanent, and the
        # 30-day pruner is not a permanent judgment about a URL.
        #
        # No analytics event here on purpose. Firing job_open with a flag, or inventing a
        # near-duplicate name, is exactly how this project's usage numbers were distorted
        # before; "how often do people land on dead jobs" deserves its own event or nothing.
        return render_template("job.html", gone=True, gone_url=url), 404

    company = row.get("company") or ""
    jd = db.get_job_jd(url) or ""
    # The card's OWN analysis wherever we have it, so the number here is the number the card
    # showed and the keywords are the terms that produced it. Only a job the scorer never
    # reached falls back to analysing the JD we just fetched.
    resume = current_profile()
    raw = _job_for(url) or {}
    analyzed = job_analysis(raw)
    if not analyzed.get("terms"):
        analyzed = jd_meta({"url": url, "jd": jd}, core.load_idf())["analyzed"]
    # THE PAGE HAS THE DESCRIPTION IN ITS HANDS, SO IT MUST NOT SAY "not scored yet".
    #
    # This is the defect the owner reported: open a job, read a full description, and the rail
    # above it announces "Not scored yet -- no full description has been read for this posting."
    # The line below was already computing a real score into `_score` and throwing it away,
    # while the rail rendered row.score_pending -- which is derived from the jd_terms COLUMN. A
    # row whose description arrived after the last scoring run has the text and not the column,
    # so this page could analyse it, score it, list its keywords, and still call itself unread.
    # 169 active rows were in exactly that state when this was measured.
    #
    # THIN IS NOT UNSCORED and keeps its pending note. core.analyze_jd flags a description that
    # is a loading shell or a truncated teaser, and a number derived from six generic terms a
    # broad resume fully covers is the fake ~100% that flag exists to prevent. An honest "we
    # have not read this" beats a confident wrong number.
    #
    # NOTHING IS MUTATED. `row` comes out of ranked_rows' cache and is shared with the feed and
    # /company, so the live number travels to the template as its own variable rather than being
    # written back into a cached dict. jd_meta() has already cached the analysis under this url
    # (it caches whenever there was JD text), and _row_pending reads _jdmeta before it reads the
    # column -- so the CARD for this row stops saying "JD pending" too, in this worker.
    have, missing, live_score = [], [], None
    if resume and analyzed.get("terms"):
        jd_score, have, missing = core.score_against(resume.lower(), analyzed)
        if row.get("score_pending") and not analyzed.get("thin"):
            live_score = int(jd_score)
    # Filtered, not just truncated. The chip lists and the inline marks share one filter and
    # differ only in how many they keep: a list is scanned, so it can be longer, while forty
    # marks in a description is a highlighter accident rather than a signal.
    have = _useful_terms(have, company, jd, _SKILL_SHOWN)
    missing = _useful_terms(missing, company, jd, _SKILL_SHOWN)

    vtags = row.get("visa") or ()
    # EMPLOYER-level routes, so the page can say "they have filed for Green Card, but this posting
    # rules it out" — information visa_tags_for_posting destroys at card level with no way to
    # recover it.
    #
    # TWO STRINGS, not a five-row table with a paragraph of explanation per route. The table
    # answered "what does H-1B mean", which is not the question somebody who opened a job
    # description is asking, and it pushed the description itself below the fold. What is left is
    # the filing history as one statement; anyone who wants the per-route detail has the company
    # page, which is built for it.
    all_tags = core.visa_tags(company, visa_index())
    filed = _and_list([core.VISA_TAG_LABELS[k] for k in all_tags])
    narrowed = _and_list([core.VISA_TAG_LABELS[k] for k in all_tags if k not in vtags])
    # The band's headline. "No Record on File" is only true when there is genuinely nothing:
    # a blocked posting at an employer with four filings on record would otherwise announce
    # exactly the opposite of what the five rows underneath it say.
    chip_label = core.SPONSOR_LIKELY_LABELS.get(row.get("visa_likely") or "")
    if not chip_label:
        chip_label = ("Ruled Out by This Posting" if (row.get("sponsor_jd") == "blocked"
                                                      and all_tags)
                      else "No Record on File")

    # block_key(company) hoisted: it is loop-invariant, and leaving it inside the comprehension
    # re-derived the SAME string once per corpus row (block_key -> normalize_label -> three
    # re.sub), doubling the regex cost of the scan for nothing. _similar_roles below already
    # does it this way.
    ckey = db.block_key(company)
    same_company = [r for r in rows
                    if db.block_key(r.get("company") or "") == ckey
                    and not r.get("closed")]
    # Other roles at this employer for the rail, BEST MATCH FIRST. ranked_rows is already in score
    # order, so this needs no sort of its own — it just drops the posting being read and takes the
    # head. Six because the rail has to stay shorter than the description beside it.
    similar = [r for r in same_company if r.get("url") != url][:6]
    # The same ROLE elsewhere, which is usually the more useful sideways jump of the two — so it
    # sits ABOVE the employer list in the rail. Cheap: the title index is cached corpus-wide.
    similar_roles = _similar_roles(row, rows)
    brief = _company_brief(company, same_company)
    # Offer the crawl only when there is nothing on file AND it is actually startable: the
    # decision needs the index, the guessed domain, the failure cooldown, the Supabase check and
    # the kill switch, none of which jobpage.js can see. It does not start here — the page must
    # not wait on a 12-second crawl — jobpage.js POSTs and then polls.
    research_pending = bool(not brief["researched"] and _research_eligible(company))

    # Byte-identical name and props to the event /api/job used to fire, so the two eras of this
    # metric stay comparable. Do not rename, do not add fields.
    #
    # NOT on a prefetch. app.js prefetches this page when the pointer settles on a card title,
    # so without this guard a slow scan down the feed would report six job_opens for six jobs
    # nobody opened — and hovering is the most common thing anyone does here. The browser states
    # its intent in Sec-Purpose (verified in a real browser: rel=prefetch sends "prefetch" plus
    # Sec-Fetch-Dest: empty, where a real navigation sends neither), so the page is still
    # rendered and still cached — only the event is withheld.
    #
    # live_score folded in, and no field added or renamed. A page that renders a real percentage
    # must not report the open as pending: "how many of the jobs I open have no score" is one of
    # the few numbers here worth trusting, and letting it count rows this page just scored is the
    # same class of defect as the two that inflated every usage figure until 2026-08-09.
    pending = bool(row.get("score_pending")) and live_score is None
    shown_score = live_score if live_score is not None else int(row.get("score") or 0)
    prefetching = "prefetch" in (request.headers.get("Sec-Purpose") or "").lower()
    if not prefetching:
        analytics.emit(user, getattr(g, "sid", ""), "job_open", job_url=url,
                       company=company, source=_host(raw or row),
                       score=0 if pending else int(shown_score), pending=pending)
    resp = app.make_response(render_template(
        "job.html", row=row, route=_route_of(row), filed=filed, narrowed=narrowed,
        similar=similar, similar_roles=similar_roles,
        jd_html=jdrender.render_jd(jd, have=have[:_HL_TERMS], missing=missing[:_HL_TERMS]),
        jd_jumps=jdrender.jump_sections(jd), has_jd=bool(jd.strip()),
        sec_labels=jdrender.SEC_LABELS,
        have=have, missing=missing, has_resume=bool(resume), live_score=live_score,
        about=brief, researching=research_pending, research_pending=research_pending,
        chip_label=chip_label, absence_note=core.VISA_ABSENCE_NOTE))
    # NO Cache-Control here, and it is a deliberate refusal. `private, max-age=30` makes the
    # prefetched copy serve the click outright — measured in a real browser as transferSize
    # 352 -> 0 and TTFB 0, so the open really is free. But a navigation served from cache never
    # reaches the server, and job_open is emitted right above. The only way to keep the metric
    # is to emit it from the browser through /api/ev, which (a) duplicates server-derived props
    # like `source` in JS and (b) moves the one number telling you which jobs get opened onto a
    # channel a client can forge. This project has already had usage figures distorted twice by
    # events drifting. Move job_open client-side FIRST, then add the header.
    return resp


# --------------------------- on-demand company research ---------------------------
# The crawler is synchronous and budgeted at 12 wall-clock seconds (research._CRAWL_BUDGET), and
# cPanel/Passenger typically runs a pool of 2 to 6 processes. Running it inside a page request
# means two people opening two unresearched employers can stall the app, which is exactly what
# /brain does today and why the tailor page hangs. So: a daemon thread, with the same reasoning
# analytics._start() already documents for this platform. Under Passenger the worst case is a
# recycled idle worker losing one crawl, and the next view retries; db.put_brain_company is a
# single idempotent upsert, so nothing is left half-written.
_research_lock = threading.Lock()
_research_inflight = {}          # domain -> started_at
_research_fail = {}              # domain -> don't-retry-until
_RESEARCH_MAX = 2                # concurrent crawls per worker
# NOT optional. resolve_domain GUESSES a domain from the company name, so a large share of
# employers resolve to one that does not exist; without a negative cooldown every visitor to
# every posting at that employer starts a doomed crawl forever, which is an outbound request
# storm from a shared host.
_RESEARCH_COOLDOWN = int(os.environ.get("RESEARCH_COOLDOWN", 6 * 3600))
# The one feature that makes this app fetch arbitrary third-party sites on a user action, so the
# off switch must not require a deploy.
_RESEARCH_ON = (os.environ.get("RESEARCH_ON_DEMAND", "1") or "1").lower() not in ("0", "false", "no")


def _research_domain(company, url=None):
    """(domain, verified) for company research.

    ASK THE VERIFIED MAP FIRST. company_domain() consults company_domains.json — written by
    scripts/build_logos.py, which judges every candidate domain on the employer's own words and
    its actual pixels — and it also takes the registrable domain off the POSTING's own URL when
    that URL is not a hiring platform. research.resolve_domain does neither: outside its ~35-name
    allowlist it strips the punctuation out of the name, appends ".com", and hands back whatever
    answers there.

    That is how researching Actalent (the staffing firm) returned a Spanish athlete-representation
    agency — actalent.com belongs to somebody else — and then cached it, shared it across the
    team and fed it to the AI tailor as grounding for a user's application.

    A name-mention check would NOT have caught that one: the wrong site says "ACTALENT" on it.
    Only a source that was verified against something other than the name can. So the guess is
    still available as a last resort, but it comes back flagged, and the crawl that follows has
    to corroborate it.
    """
    try:
        from resume_brain import research
    except Exception:
        return "", False
    try:
        dom = (company_domain(company, url) or "").strip().lower()
        if dom:
            return dom, True
    except Exception:
        pass
    try:
        dom, source = research.resolve_domain(company, "", with_source=True)
        return dom, source in ("url", "map")
    except Exception:
        return "", False


def _research_eligible(company):
    """Should this page offer to crawl `company`? Returns the domain, or ""."""
    if not _RESEARCH_ON or not company:
        return ""
    # Only against Supabase. The local fallback (db.py's *_local.json path) is an unlocked
    # read-modify-write of a whole JSON file, so two workers crawling two employers can lose a
    # record. One condition removes the only data-loss path this feature has.
    try:
        if not db.has_remote_db():
            return ""
    except Exception:
        return ""
    dom, _verified = _research_domain(company)
    if not dom:
        return ""
    now = time.time()
    with _research_lock:
        if _research_fail.get(dom, 0) > now:
            return ""
        if dom in _research_inflight:
            return dom
    return dom


def _research_crawl(domain, company):
    """The crawl itself, on a background thread.

    Takes PLAIN ARGUMENTS and touches no request-scoped state: no flask.g, no session, no
    request. That is the classic bug in this pattern, and db.put_brain_company needs no app
    context (it posts through a module-level requests session).
    """
    ok = False
    try:
        from resume_brain import research
        # verify= is the inverse of "we trust where this domain came from". An unverified guess
        # has to prove the site is actually this employer's before anything is written to the
        # SHARED cache; a verified one has already proved it.
        _dom2, verified = _research_domain(company)
        pages = research.crawl_company(domain, company_name=company, verify=not verified)
        if pages:
            rec = research.extract_company_knowledge(pages, company)
            rec["domain"] = domain
            db.put_brain_company(domain, rec)
            ok = True
    except Exception:
        ok = False
    finally:
        with _research_lock:
            _research_inflight.pop(domain, None)
            if ok:
                # Bust the 300-second name index, or a successful crawl keeps reading as a miss
                # for five minutes and the poll gives up on work that already finished.
                _research_cache.update({"by_name": None, "at": 0})
                # Same reasoning for the negative cache: this employer was just recorded as absent
                # by whatever page triggered the crawl, and without this the poll would keep being
                # told "no record" for the rest of the TTL — for work that finished a second ago.
                _research_miss.clear()
            else:
                _research_fail[domain] = time.time() + _RESEARCH_COOLDOWN


def _research_start(company):
    """Launch a crawl for `company` unless one is already running or the slots are full."""
    dom = _research_eligible(company)
    if not dom:
        return False
    with _research_lock:
        if dom in _research_inflight:
            return True
        if len(_research_inflight) >= _RESEARCH_MAX:
            return False
        _research_inflight[dom] = time.time()
    t = threading.Thread(target=_research_crawl, args=(dom, company), daemon=True)
    t.start()
    return True


def _research_fragment(company):
    """The research block for `company`, as HTML, for both the page and the poll."""
    dom, _verified = _research_domain(company)
    running = False
    with _research_lock:
        running = dom in _research_inflight
    ckey = db.block_key(company)          # hoisted: loop-invariant, see /job
    rows = [r for r in ranked_rows(session["user"], current_profile())
            if db.block_key(r.get("company") or "") == ckey
            and not r.get("closed")]
    return render_template("_jobresearch.html", about=_company_brief(company, rows),
                           row={"company": company, "url": (rows[0]["url"] if rows else "")},
                           researching=running)


@app.route("/job/research", methods=["GET", "POST"])
@login_required
def job_research():
    """Start a company crawl (POST), or read the section back (GET).

    The GET returns the SAME Jinja partial the page rendered rather than JSON, so the markup and
    its escaping live in one template and the two cannot drift.
    """
    company = (request.args.get("c") or request.form.get("c") or "").strip()
    if not company:
        return "", 400
    if request.method == "POST":
        if not _check_csrf():
            return "", 403
        _research_start(company)
        return "", 204
    return _research_fragment(company), 200, {"Content-Type": "text/html; charset=utf-8"}


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
    ok, msg = _save_profile(user, {"search_prefs": prefs})
    if not ok:
        return jsonify({"ok": False, "error": msg[:200]}), 200
    # No cache bust here. _rows_cache holds the corpus scored and sorted for this user with NO
    # filters applied — prefs are neither in its key nor in its contents. The first-paint count
    # IS derived from prefs, but it is recomputed per request from the freshly-read profile, so
    # clearing the corpus cache never changed it; it just charged every user on this worker a
    # rebuild for a saved search that was not theirs.
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


# /api/job lived here. It served the job-detail MODAL and had no other caller anywhere in the
# repo (static, templates, extension, scripts, web/src all checked). The modal was replaced by
# the server-rendered /job page, which reads the same row out of ranked_rows and the JD out of
# db.get_job_jd directly, so the endpoint had nothing left to do. Its analytics.emit for
# job_open moved into job_page() with the same name and the same five props, so the metric
# spans both eras.


# Where an action came from. A CLOSED SET, because api_action lets the browser choose one and it
# lands in the `via` dimension of every `action` event:
#   confirmed  the user answered "yes, I applied" to the prompt that follows an Apply click.
#              The ONLY value the open-to-apply funnel counts as an application.
#   card       a button on a feed card (Save / Hide / Mark applied) -- a deliberate press.
#   job        the same, from a job page's no-JS form. Predates the rest; see the /action route.
#   api        anything that did not say. Kept as the default so an older cached app.js, which
#              sends no `via` at all, still records its actions instead of being rejected.
_ACTION_VIA = ("confirmed", "card", "job", "api")


@app.route("/api/action", methods=["POST"])
@login_required
def api_action():
    """JSON like/hide/apply for the JS feed. Body: {url, status, via?} (status '' clears).

    `via` is the ORIGIN of the action, and for status='applied' it is the difference between a
    number that means something and the one this app reported until 2026-08-21. Clicking Apply
    used to write 'applied' on the spot, so opening a posting counted as applying to it and the
    tracker held 129 applications nobody had made. The feed now asks on return and sends
    via='confirmed'; anything else is a state change the user made deliberately somewhere else.
    Whitelisted rather than passed through: this is a browser writing a value that ends up in an
    aggregate, so an arbitrary string here would be a free dimension for anyone with a console.
    """
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    status = (data.get("status") or "").strip()
    via = data.get("via") or "api"
    if via not in _ACTION_VIA:
        via = "api"
    if not url:
        return {"ok": False, "error": "no url"}, 400
    # `via` was whitelisted and `status` was not, which had it backwards: via lands in an
    # analytics dimension, status decides whether a posting disappears from your feed. Rejected
    # here as a 400 so the client sees a real error rather than db.set_user_status' ValueError
    # arriving as a 500.
    if status and status not in db.USER_STATUSES:
        return {"ok": False, "error": "unknown status"}, 400
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
        _ev_action(user, url, prev, status, via)
        return {"ok": True, "status": status}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500


def _ev_action(user, url, prev, status, via):
    """One like/hide/apply, with the job's company, board and score attached."""
    try:
        job = _job_for(url) or {}
        try:
            score = int(job.get("match_score") or 0)
        except Exception:
            score = 0
        analytics.emit(user, getattr(g, "sid", ""), "action", job_url=url,
                       company=job.get("company"), source=_host(job), score=score,
                       to=status or "cleared", frm=prev or "none", via=via)
    except Exception:
        pass


@app.route("/reload", methods=["POST"])
@login_required
def reload_jobs():
    """Throw away every cache, globally, and re-read the corpus.

    POST + admin, where this used to be a GET open to any signed-in account. Three costs, none
    of which the caller pays:

      * a full ~12 MB corpus re-read per hit, against a 5 GB/month egress budget;
      * _scores_clear(), which deletes the on-disk score cache for EVERY user — the one the
        comment at the top of this file measures the alternative to at "5-15 SECONDS of CPU"
        per user per rebuild, and a 13.8 s LCP on the live site;
      * _account_cache and the rest, so the next request from everybody is a cold one.

    As a GET it was also CSRF-exempt by design, so a single
    <img src="https://stemjobs1.astrochakra.co/reload"> on any page a logged-in user happened to
    visit triggered the whole thing. The admin ?refresh=1 links already had the right shape;
    this route did not. CSRF now comes from the one before_request hook, which covers every
    non-GET by default.

    The admin test is INLINE rather than an @admin_required decorator only because that
    decorator is defined further down this file and a decorator is evaluated at import time —
    the module would not load. The check itself is the same one it makes.
    """
    if not is_admin():
        flash("Reloading the shared job cache is admin-only.", "error")
        return redirect(url_for("feed"))
    get_jobs(force=True)
    _score_cache.clear()
    _scores_clear()          # ...including the stored ones: this also re-pulls _jdmeta
    _rows_cache.clear()
    _base_rows_cache["fp"] = _base_rows_cache["rows"] = None   # the shared half of _rows_cache
    _profile_cache.clear()
    _resume_cache.clear()
    _status_cache.clear()
    _sponsor_cache.clear()
    _jdmeta.clear()
    # Same JDMETA gate as the import-time load above. Without it, /reload would pull 94 MB back
    # into the worker that served it and quietly undo the saving — and only for that one worker,
    # which is the kind of asymmetry that makes a memory graph impossible to read.
    if (os.environ.get("JDMETA") or "").strip() in ("1", "true", "yes"):
        _jdmeta.update(core.load_jdmeta())   # re-pull the cron's latest precompute from disk
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
        except Exception:
            body = {}
        sid = getattr(g, "sid", "")          # server-side, never taken from the request body
        # PER EVENT, not per batch. The try used to wrap the whole loop, so one malformed event
        # took the rest of the batch with it — and malformed is easy to reach by accident: a
        # props key colliding with a named parameter ("username", "sid", "event") raises
        # TypeError inside emit, and events 5 through 40 were then lost with it. A beacon sends
        # a batch precisely because the events are independent; the error handling should be too.
        for e in (body.get("ev") or [])[:40]:
            if not isinstance(e, dict):
                continue
            try:
                analytics.emit(user, sid, str(e.get("e") or ""), **(e.get("p") or {}))
            except Exception:
                continue
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
    if force:
        # Every caller that forces this is an admin who just created, disabled, deleted or
        # re-keyed an account. _account_state answers from its own per-user cache now, so
        # without this a disabled account would keep working for up to _ACCOUNT_TTL.
        _account_cache.clear()
    if force or c["map"] is None or time.time() - c["at"] > _ACCOUNTS_TTL:
        try:
            c["map"] = {(u.get("username") or ""): u for u in (db.list_users() or [])}
            c["at"] = time.time()
        except Exception:
            if c["map"] is None:
                return None
            c["at"] = time.time() - _ACCOUNTS_TTL + 10      # serve stale, retry in 10s
    return c["map"]


_ACCOUNT_TTL = 60
_ACCOUNT_CACHE_MAX = 2000
# ORDERED, AND EVICTED OLDEST-FIRST. It used to be a plain dict emptied wholesale at the cap,
# which handed an unauthenticated caller a way to charge every real user a database read: spray
# 2000 junk usernames at the CORS-open /api/ext/* routes and every genuine cached row goes with
# them. Evicting one entry at a time bounds the worker exactly as well and cannot be aimed at
# somebody else's entry.
_account_cache = collections.OrderedDict()   # username -> (row or None, fetched_at)


def _account_state(username):
    """The users row for `username`. None when the account is genuinely gone; {} when we can't
    tell. Callers fail CLOSED on None and OPEN on {} — see _session_dead.

    A POINT LOOKUP, not a table scan. login_required calls this on every single request, and it
    used to be answered out of _accounts(), which downloads the ENTIRE users table once a minute
    per worker process — a full-table read to answer one question about one person. At 1,000
    accounts that is roughly 1.15 GB/day against a 5 GB/month egress budget, and the refresh
    lands on whichever unlucky request happens to cross the 60 s boundary rather than on a
    background thread.

    Only the admin columns are selected. db.get_user's default `*` drags the résumé text and the
    brain_kb jsonb along with it, which is a great many bytes to answer "is this one disabled?".
    """
    now = time.time()
    hit = _account_cache.get(username)
    if hit is not None and now - hit[1] <= _ACCOUNT_TTL:
        _account_cache.move_to_end(username)   # true LRU: a user in active use is never evicted
        return hit[0]
    row, ok = None, False
    for cols in db._USER_COLS:          # widest-first, the same ladder list_users falls down
        try:
            row, ok = db.get_user(username, cols), True
            break
        except Exception:
            continue
    if not ok:
        # Serve the last good answer rather than invent one: a transient Supabase blip must not
        # read as "this account was deleted" and sign somebody out of their own app. {} when we
        # have never had an answer at all, which callers treat as "can't tell" and fail open.
        return hit[0] if hit is not None else {}
    while len(_account_cache) >= _ACCOUNT_CACHE_MAX:
        _account_cache.popitem(last=False)   # oldest out, one at a time; never the whole cache
    _account_cache[username] = (row, now)
    _account_cache.move_to_end(username)
    return row


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


@app.template_filter("ago")
def _ago_filter(value):
    """An ISO date -> the same relative wording the feed cards use ("3w ago").

    ONE presentation of one kind of fact. The app showed three: the feed card said "3w ago",
    /job's header said "Posted 2026-07-28", and an /applications row said "applied 2026-08-24"
    directly above a date input reading "08/24/2026" — ISO and US-numeric in the same row of the
    same card. app.js::formatDates() already solved this for the feed and nothing equivalent ran
    on the other two pages, because they are server-rendered and it is a client function.

    Mirrors formatDates' thresholds deliberately: today / Nd / Nw / Nmo, then the ISO date once
    a year has passed and "2 years ago" would be less useful than the date itself. Anything it
    cannot parse comes back untouched, which is the right answer for a string we did not write.
    """
    s = str(value or "")[:10]
    try:
        d = datetime.date.fromisoformat(s)
    except (TypeError, ValueError):
        return s
    days = (datetime.date.today() - d).days
    if days < 0:
        return s                                  # a future date is data, not recency
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 7:
        return "%dd ago" % days
    if days < 30:
        return "%dw ago" % (days // 7)
    if days < 365:
        return "%dmo ago" % (days // 30)
    return s


@app.template_global()
def sponsor_data_through():
    """The last fiscal year the sponsorship COUNTS cover, e.g. "FY2025".

    Every sponsorship figure in the product — the "~1,661 H-1B" chips, "top sponsor", the
    company modal's year chart, the sponsor-rank sort — is computed from a window that closed
    some time ago, and today is well past its end. The modal has always been honest about the
    range it draws; the card chip was not, and "top sponsor" on a 2026 feed reads as a claim
    about now.

    READS THE TIER WINDOW, not the history. Those differ: build_sponsor_counts reads every
    fiscal year on disk into sponsor_years (so the chart can show a long history) but SUMS only
    --years into the counts. The number this label sits next to is the windowed one, so quoting
    the history's last year would overstate it by however many partial or out-of-window years
    happen to be present.

    DERIVED FROM THE DATA, so refreshing the indexes moves every label at once. Falls back to
    the history, then to the shipped vintage — an unlabelled number is the defect.
    """
    try:
        yrs = [int(y) for y in (core.sponsor_meta().get("years") or []) if str(y).isdigit()]
        if yrs:
            return "FY%d" % max(yrs)
    except Exception:
        pass
    try:
        # Pre-#meta files (anything built before 2026-08-31) carry no window, so fall back to
        # the history. Safe now: core.load_sponsor_years pops "#meta", without which the int()
        # below would raise on its string keys and this whole function would return "FY2023".
        years = set()
        for per_year in (sponsor_years() or {}).values():
            years.update(int(y) for y in (per_year or {}))
        if years:
            return "FY%d" % max(years)
    except Exception:
        pass
    return "FY2023"


@app.template_global()
def sponsor_window():
    """The whole tier window as a label, e.g. "FY2021-2025".

    The templates used to hardcode the START of this range ("FY2019&ndash;{{ ... }}") next to a
    number summed over whatever --years the last build happened to use. Sliding the window then
    silently made the label a lie. Both ends come from the data now.
    """
    return core.sponsor_window() or "FY2019-2023"


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


# ----------------------------- deploy drift -----------------------------
# scripts/build_deploy_zip.py ships a deploy_manifest.json listing a sha256 for every file it
# put into templates/ and static/. Compare it with what is on disk and you learn the one thing
# nothing else here can tell you: whether the running app is the app we think we shipped.
#
# It is not a hypothetical. On 2026-08-24 production was serving a profile.html with an
# "Appearance" card that has never existed in any commit, a base.html missing the theme toggle
# added in 5bb4dc7, and a welcome.html rendering one callout twice — hand edits made in cPanel's
# File Manager, invisible to git, and due to be destroyed without a word by the next zip extract.
#
# CHANGED, MISSING and EXTRA are all reported, because they mean different things: changed is a
# hand edit about to be overwritten, missing is a broken deploy, extra is usually a stray backup
# (profile.html.bak) that File Manager left behind.
_DRIFT_MANIFEST = os.path.join(_APP_DIR, "deploy_manifest.json")
_drift_cache = {"at": 0.0, "data": None}
_DRIFT_TTL = 300


def template_drift(force=False):
    """{'state': ..., 'built_at': ..., 'changed': [...], 'missing': [...], 'extra': [...]}.

    state is 'clean', 'drift', or 'unknown' — the last meaning no manifest, which is every
    developer checkout and any server whose last deploy predates this check. 'unknown' is
    deliberately not an alarm: it says we cannot tell, which is honest and was the situation
    everywhere until now.

    Walking two directories of small files costs ~15 ms, and it is cached for 5 minutes on top,
    so /admin can ask on every render.
    """
    c = _drift_cache
    if not force and c["data"] is not None and time.time() - c["at"] < _DRIFT_TTL:
        return c["data"]
    out = {"state": "unknown", "built_at": "", "changed": [], "missing": [], "extra": [],
           "note": "No deploy_manifest.json — built before this check existed, or a dev checkout."}
    try:
        with open(_DRIFT_MANIFEST, encoding="utf-8") as fh:
            man = json.load(fh)
        want = man.get("files") or {}
        dirs = man.get("dirs") or ["templates", "static"]
        out["built_at"] = man.get("built_at") or ""
        have = {}
        for d in dirs:
            root_dir = os.path.join(_APP_DIR, d)
            for root, dirnames, filenames in os.walk(root_dir):
                dirnames[:] = [x for x in dirnames if x not in ("__pycache__", ".pytest_cache")]
                for name in sorted(filenames):
                    if os.path.splitext(name)[1] in (".pyc", ".pyo"):
                        continue
                    full = os.path.join(root, name)
                    rel = os.path.relpath(full, _APP_DIR).replace(os.sep, "/")
                    h = hashlib.sha256()
                    with open(full, "rb") as fh:
                        for chunk in iter(lambda: fh.read(65536), b""):
                            h.update(chunk)
                    have[rel] = h.hexdigest()
        out["changed"] = sorted(k for k in want if k in have and have[k] != want[k])
        out["missing"] = sorted(k for k in want if k not in have)
        out["extra"] = sorted(k for k in have if k not in want)
        bad = out["changed"] or out["missing"] or out["extra"]
        out["state"] = "drift" if bad else "clean"
        out["note"] = ("Matches the bundle built %s." % (out["built_at"] or "?")) if not bad else (
            "%d changed, %d missing, %d extra vs the bundle built %s. A changed file is a hand "
            "edit that the next deploy will overwrite with no warning — copy it off the server "
            "and commit it BEFORE deploying."
            % (len(out["changed"]), len(out["missing"]), len(out["extra"]), out["built_at"] or "?"))
    except FileNotFoundError:
        pass
    except Exception as e:
        out["state"] = "unknown"
        out["note"] = "Couldn't read the manifest: %s" % e
    c["data"], c["at"] = out, time.time()
    return out


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
        template_drift(force=True)      # re-hash too: this is the button you press after a deploy
        flash("Jobs reloaded.")
        return redirect(url_for("admin"))
    try:
        users = db.list_users() or []
    except Exception:
        users = []
    return render_template(
        "admin.html", stats=_admin_stats(), status=db.get_scrape_status() or {},
        runs=_gh_runs(), users=users, gh_token=bool(_gh_token()),
        gh_repo=GH_REPO, gh_workflow=GH_WORKFLOW, drift=template_drift(),
        admin_mode=("ADMIN_USERS" if _ADMIN_USERS else "sole-account"))


# ---- /admin/data — storage, growth, health ----------------------------------
# THERE IS NO 500 MB CAP ANY MORE, and reporting one was worse than reporting nothing. That
# number was Supabase's free tier; the database moved to Postgres on cPanel on 2026-08-15 and
# the account's disk quota is UNLIMITED — measured through cpanelapi on 2026-08-18,
# `megabyte_limit: "0.00"`, which is how cPanel spells "no limit". So the panel was showing 25%
# of a ceiling that does not exist, and a projection of the date it would hit it.
#
# What CAN actually stop this account is inodes: 44,305 used of a 200,000 limit on the same
# reading. That is a file count, so pruning old job rows does nothing for it, and nothing here
# had ever looked at it. See cpanelapi.account_usage.
#
# DB_SIZE_BUDGET_MB is opt-in and unset by default. Some shared hosts do cap a database even
# with unlimited disk; if yours does, set it and the growth projection aims at it again.
_DB_SIZE_BUDGET_MB = 0
try:
    _DB_SIZE_BUDGET_MB = int(os.environ.get("DB_SIZE_BUDGET_MB") or 0)
except (TypeError, ValueError):
    _DB_SIZE_BUDGET_MB = 0
_DB_SIZE_BUDGET_BYTES = _DB_SIZE_BUDGET_MB * 1024 * 1024
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
                   "resumes", "resume_files", "tailored_cache", "learned_answers", "boards")
# Every table keyed by username, for the orphan check. db.delete_user() historically removed
# only user_jobs + users, so anything else here can hold rows belonging to a deleted account.
_USER_SCOPED_TABLES = ("user_jobs", "profiles", "applications", "resumes", "resume_files",
                       "learned_answers")


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
    rate = per_day / 1048576.0
    # WITH NO BUDGET SET, REPORT THE RATE AND STOP. Inventing a ceiling to count down to is what
    # the 500 MB version did, and the countdown was the most prominent number on the page.
    if not _DB_SIZE_BUDGET_BYTES:
        return {"state": "growing", "per_day_mb": rate, "days_left": None,
                "verdict": "+%.1f MB/day · %.1f GB/year at this rate · disk is unlimited"
                           % (rate, rate * 365 / 1024.0)}
    days_left = (_DB_SIZE_BUDGET_BYTES - cur) / per_day
    if days_left <= 0:
        return {"state": "over", "per_day_mb": rate, "days_left": 0,
                "verdict": "Already over the %d MB budget you set." % _DB_SIZE_BUDGET_MB}
    when = datetime.date.today() + datetime.timedelta(days=min(int(days_left), 3650))
    return {"state": "growing", "per_day_mb": rate, "days_left": int(days_left),
            "verdict": "+%.1f MB/day · reaches your %d MB budget around %s (%d days)"
                       % (rate, _DB_SIZE_BUDGET_MB, when.isoformat(), int(days_left))}


_ADMIN_DB_TTL = 300
_admin_db_cache = {"data": None, "at": 0.0}
_HOST_TTL = 900                 # 15 min; a quota does not move faster than that
_host_cache = {"data": None, "at": 0.0}


def _cpanel_usage(force=False):
    """What the cPanel account is using, or None when CPANEL_* is not configured.

    Cached hard and failure-tolerant: this is decoration on a dashboard, and an admin page that
    hangs for 20 seconds because a stats API is slow is a worse page than one missing a tile.
    """
    c = _host_cache
    if not force and c["data"] is not None and (time.time() - c["at"]) < _HOST_TTL:
        return c["data"]
    try:
        import cpanelapi
        data = cpanelapi.account_usage(timeout=8)      # two calls; 16 s worst case, once per TTL
    except Exception as e:
        data = {"errors": ["client failed: %s" % str(e)[:120]]}
    c["data"], c["at"] = data, time.time()
    return data


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
           # Only a percentage when there is something real to be a percentage OF.
           "pct": (100.0 * nbytes / _DB_SIZE_BUDGET_BYTES)
                  if (_DB_SIZE_BUDGET_BYTES and nbytes) else None,
           "budget_mb": _DB_SIZE_BUDGET_MB or None,
           "backend": db.backend_name(),
           "tables": tables, "counts": counts,
           "host": _cpanel_usage(),
           "history": hist, "projection": _size_projection(hist)}
    c["data"], c["at"] = out, time.time()
    return out


def _newest_event_age():
    """(iso timestamp, hours ago) for the most recent analytics event, or ("", None)."""
    ts = db.newest_event_ts()
    if not ts:
        return "", None
    try:
        when = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        now = datetime.datetime.now(when.tzinfo)
        return str(ts)[:19], (now - when).total_seconds() / 3600.0
    except Exception:
        return str(ts)[:19], None


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

    # ANALYTICS ARE WRITING. This check exists because they stopped for three days and nothing
    # noticed: db.insert_events swallows failures so analytics can never break a request, which
    # is right, but it meant a primary-key collision (events_id_seq left behind by the cPanel
    # migration) was completely invisible. The freshest event's age is the signal that survives
    # a worker restart; the in-process counter catches a failure happening right now.
    fresh, age_h = _newest_event_age()
    eh = db.events_health()
    out.append(_check("Analytics writes",
                      eh["failures"] == 0 and age_h is not None and age_h < 48,
                      ("Newest event is %s (%.0f h ago)." % (fresh, age_h) if age_h is not None
                       else "No events recorded at all.")
                      + (" %d insert(s) failed in this worker: %s"
                         % (eh["failures"], eh["last_error"]) if eh["failures"] else "")
                      + (" Sequence was repaired." if eh["seq_repaired"] else ""),
                      "Events stop silently when events_id_seq falls behind max(id) — every "
                      "insert then collides with events_pkey. db.insert_events now repairs that "
                      "itself on the first failure; if this stays red, check the app error log.",
                      warn=True))

    tc = db.table_count("tailored_cache")
    out.append(_check("Unbounded tables", tc is not None and tc <= 5000,
                      "tailored_cache holds %s rows and has no expiry anywhere in the codebase."
                      % ("{:,}".format(tc) if tc is not None else "?"),
                      "Needs an age-based prune; nothing deletes from it today.", warn=True))

    out.append(_check("brain_companies table", db.table_count("brain_companies") is not None,
                      "Not present in %s. Resume Brain's company cache is local-file only, " % db.backend_name() +
                      "so it is empty on the deployed app and not shared between machines.",
                      "Deliberate: see MIGRATION_resume_files.sql for the DDL and why it is unapplied.",
                      warn=True))

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


@app.route("/api/db", methods=["POST"])
def api_db():
    """The scraper's way in, when the database is local to this machine and it is not.

    Deliberately thin: every decision — signature, freshness, table allowlist, and whether the
    operation is one this codebase ever performs — lives in dbproxy.handle and pgrest.build, so
    it can all be tested without a web server. See dbproxy.py for why this is not a remote SQL
    console.

    Off unless DB_PROXY_SECRET is set, so an install that has never heard of the proxy answers
    503 rather than exposing a route that merely fails authentication.
    """
    from flask import jsonify
    # local_ok gates on this app having a database of its own; handle() checks it AFTER the
    # signature, so an anonymous caller cannot use this endpoint to read our configuration.
    status, payload = dbproxy.handle(
        request.get_data(), request.headers.get("X-DB-Ts"), request.headers.get("X-DB-Sig"),
        os.environ.get("DB_PROXY_SECRET") or "", db._http, local_ok=bool(db.PG_DSN))
    return jsonify(payload), status


_BOARDS_TTL = 300
_admin_boards_cache = {"data": None, "at": 0.0}
# A board is DEPRECATING when it has returned zero postings, successfully, this many runs in a
# row. Successfully is the load-bearing word: a fetch that FAILED proves nothing about the board
# (that is the "failing" bucket below), while three clean fetches that each found nothing is a
# board that has been walled off, renamed, or emptied. Same threshold scraper.save_board_health
# uses for the line it prints to the run log, because two different answers to "is this board
# dead" would be worse than either.
_BOARD_SILENT_RUNS = 3


def _admin_boards(force=False):
    """What each board COSTS and whether it still returns anything.

    Reads the board_health kv blob the scraper writes at the end of every run. That blob has
    carried per-board outcomes for a while and per-board TIMINGS since 2026-08-21; until now the
    only place any of it was visible was the run log, which means "which boards are we still
    paying for and getting nothing from" was a question you answered by reading a scrape
    transcript. Everything here is derived, so a missing or old blob renders as empty panels
    rather than an error.
    """
    c = _admin_boards_cache
    if not force and c["data"] is not None and time.time() - c["at"] < _BOARDS_TTL:
        return c["data"]

    try:
        blob = db.get_kv("board_health") or {}
    except Exception:
        blob = {}
    boards = (blob.get("boards") or {})

    by_ats, costly, silent, failing, starved = {}, [], [], [], []
    timed = 0
    for url, r in boards.items():
        runs = r.get("runs") or []
        # STARVED is read off the board, not off its runs: a board the budget never started has
        # no run to read. Counted before the `continue` below so a board that has ONLY ever been
        # skipped still shows up somewhere -- it used to vanish from every panel here while
        # quietly costing a slot in the sweep.
        skips = int(r.get("skips") or 0)
        if skips:
            starved.append({"company": r.get("company") or "?", "ats": r.get("ats") or "?",
                            "url": url, "skips": skips,
                            "last_skip": r.get("last_skip") or ""})
        if not runs:
            continue
        secs = [x["secs"] for x in runs if x.get("secs") is not None]
        row = {"company": r.get("company") or "?", "ats": r.get("ats") or "?", "url": url,
               "last": runs[-1].get("n"), "ok": bool(runs[-1].get("ok")),
               "runs": len(runs),
               # What the board actually said. The panel could report a failure but not its
               # cause, so every row here used to end in a local re-run to find out why.
               "err": runs[-1].get("err") or "",
               # When that outcome is FROM. The sweep is budgeted, so the newest record for a
               # board is not necessarily from the newest run, and "failing" reads very
               # differently if the failure is four runs old.
               "at": runs[-1].get("at") or "",
               "secs": max(secs) if secs else None,
               "avg": (sum(secs) / len(secs)) if secs else None}
        if secs:
            timed += 1
            a = by_ats.setdefault(row["ats"], {"boards": 0, "secs": 0.0})
            a["boards"] += 1
            a["secs"] += secs[-1]
            costly.append(row)
        # Not `not row["ok"]`: that reads a board the budget never started as a broken one, and
        # legacy records (before per-board timings) cannot tell the two apart at all. The
        # predicate lives in scraper so this panel and the run log cannot drift.
        if sc.board_run_failed(runs[-1]):
            failing.append(row)
        elif (len(runs) >= _BOARD_SILENT_RUNS
              and all(x.get("n") == 0 and x.get("ok") for x in runs[-_BOARD_SILENT_RUNS:])):
            silent.append(row)

    spent = sum(a["secs"] for a in by_ats.values())
    out = {
        "have": bool(boards),
        "updated_at": blob.get("updated_at") or "",
        "tracked": len(boards),
        "timed": timed,
        "spent": spent,
        "silent_runs": _BOARD_SILENT_RUNS,
        "by_ats": sorted(
            ({"ats": k, "boards": v["boards"], "secs": v["secs"],
              "pct": (100.0 * v["secs"] / spent) if spent else 0.0,
              "each": v["secs"] / max(1, v["boards"])} for k, v in by_ats.items()),
            key=lambda r: -r["secs"]),
        "costly": sorted(costly, key=lambda r: -(r["secs"] or 0))[:15],
        "silent": sorted(silent, key=lambda r: (r["company"] or ""))[:40],
        "failing": sorted(failing, key=lambda r: (r["company"] or ""))[:40],
        # Not a health problem and deliberately not mixed in with one: these boards are fine,
        # the run just never got to them. Ordered by streak because a board skipped run after
        # run is the one the daily rotation is failing to cover.
        "starved_n": len(starved),
        "starved": sorted(starved, key=lambda r: (-r["skips"], r["company"] or ""))[:40],
    }
    _admin_boards_cache.update({"data": out, "at": time.time()})
    return out




@app.route("/admin/data")
@admin_required
def admin_data():
    """Storage against the free-tier cap, growth trend, and corpus/account health checks."""
    if request.args.get("refresh"):
        get_jobs(force=True)
        _admin_stats_cache["data"] = None
        _admin_db_cache["data"] = None
        _admin_health_cache["data"] = None
        _admin_boards_cache["data"] = None
        flash("Jobs reloaded.")
        return redirect(url_for("admin_data"))
    return render_template("admin_data.html", dbi=_admin_db(), stats=_admin_stats(),
                           health=_admin_health_cache["data"], blocked=db.list_blocked(),
                           audit=db.list_audit(20), delete_max=ADMIN_DELETE_MAX,
                           boards=_admin_boards())


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
#
# NAMED _USAGE_STOP, not _TITLE_STOP, and the distinction is load-bearing. This was a second
# module-level binding of _TITLE_STOP, and since Python resolves globals at call time it won
# everywhere -- including in _title_tokens 2,500 lines above, which is written for the SHORT
# list and says so. The consequence: the similar-roles rail stopped engineer/manager/analyst/
# senior/lead (exactly the words that distinguish one role from another, which that list
# documents itself as deliberately keeping) and did NOT stop job/role/position/remote/hybrid/
# usa (which it documents itself as removing), so the rail ranked on boilerplate and location.
# Two different lists for two different jobs is correct; sharing one name was not.
_USAGE_STOP = frozenset("""
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


_ADMIN_USER_TTL = 300
_admin_user_cache = {}                   # username -> {"data": ..., "at": ts}


def _admin_usage_one(username, force=False):
    """The same behaviour rollup as _admin_usage, scoped to ONE account.

    A separate function rather than a filter argument threaded through the 200-line aggregate:
    the two share their INPUTS (the whole user_jobs and applications tables, which are small --
    126 rows between them today) and almost nothing else, because the questions differ. The
    aggregate asks "is the score separating good from bad across everyone"; this asks "what has
    this person actually done", which wants their timeline and their companies and no
    cross-user comparison at all.

    Cached per username on the same 5-minute clock, and cleared by the same ?refresh=1.
    """
    c = _admin_user_cache.get(username)
    if not force and c and time.time() - c["at"] < _ADMIN_USER_TTL:
        return c["data"]

    jobs = get_jobs()
    by_url = {j.get("url"): j for j in jobs if j.get("url")}
    flags = [r for r in _all_user_jobs() if (r.get("username") or "") == username]
    apps = [a for a in _all_applications() if (a.get("username") or "") == username]

    totals = {s: 0 for s in _STATUSES}
    scores = {s: [] for s in _STATUSES}
    companies = collections.defaultdict(lambda: {s: 0 for s in _STATUSES})
    hosts = collections.Counter()
    rows = []
    for r in flags:
        st = (r.get("status") or "").strip()
        if st not in _STATUSES:
            continue
        totals[st] += 1
        url = r.get("url") or ""
        h = _host({"url": url})
        if h:
            hosts[h] += 1
        j = by_url.get(url)
        if not j:
            # A posting pruned at 30 days. Counted in the totals above -- the action happened --
            # but it cannot contribute a company or a score, and saying so is more useful than
            # a silently short table.
            rows.append({"status": st, "url": url, "title": "", "company": "", "score": None})
            continue
        co = (j.get("company") or "").strip()
        if co:
            companies[co][st] += 1
        try:
            sc = int(j.get("match_score") or 0)
        except (TypeError, ValueError):
            sc = 0
        if sc:
            scores[st].append(sc)
        rows.append({"status": st, "url": url, "title": j.get("title") or "",
                     "company": co, "score": sc or None})

    # The applications timeline. applications.created_at is still the only real timestamp this
    # app records for a user action, which is why this is the one genuine time series here.
    by_day = collections.Counter()
    by_hour = collections.Counter()
    outcomes = collections.Counter()
    resumes_used = collections.Counter()
    auto_logged = 0
    for a in apps:
        created = str(a.get("created_at") or "")
        if len(created) >= 10:
            by_day[created[:10]] += 1
        if len(created) >= 13 and created[11:13].isdigit():
            by_hour[int(created[11:13])] += 1
        outcomes[(a.get("status") or "applied").strip() or "applied"] += 1
        resumes_used[(a.get("resume_name") or "(none)").strip() or "(none)"] += 1
        # Same signature /admin/usage uses, and the same one scripts/reset_autologged_applies.py
        # deletes on: applied on its creation day with nothing typed. Surfaced per user because
        # this is where a suspicious Applied count gets explained.
        if (a.get("applied_date") or "")[:10] == created[:10] and not (a.get("notes") or "").strip():
            auto_logged += 1

    days = sorted(by_day.items())[-30:]
    top_co = sorted(companies.items(),
                    key=lambda kv: -sum(kv[1].values()))[:12]
    out = {
        "user": username,
        "totals": totals,
        "flag_rows": sum(totals.values()),
        "app_rows": len(apps),
        "auto_logged": auto_logged,
        "matched_pct": (100.0 * sum(1 for r in rows if r["company"]) / len(rows)) if rows else 0.0,
        "scores": [{"status": s, "n": len(v), "median": _median(v),
                    "mean": (sum(v) / len(v)) if v else 0.0} for s, v in scores.items()],
        "companies": [{"name": k, "liked": v["liked"], "applied": v["applied"],
                       "hidden": v["hidden"], "n": sum(v.values())} for k, v in top_co],
        "hosts": hosts.most_common(10),
        "by_day": days,
        "by_day_max": max([n for _, n in days] or [1]),
        "by_hour": [(h, by_hour.get(h, 0)) for h in range(24)],
        "by_hour_max": max(list(by_hour.values()) or [1]),
        "outcomes": outcomes.most_common(),
        "resumes": resumes_used.most_common(8),
        # Newest first, and capped: this is a profile, not an export.
        "recent_apps": apps[:25],
        "actions": rows[:60],
    }
    _admin_user_cache[username] = {"data": out, "at": time.time()}
    return out


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
                if len(tok) > 2 and tok not in _USAGE_STOP:
                    hidden_tokens[tok] += 1

    # Title-token lift: how much more often a word appears in what someone hid than in the
    # corpus at large. A word at 3x+ is a filter or a scoring penalty waiting to be written.
    corpus_tokens = collections.Counter()
    for j in jobs:
        for tok in set(re.split(r"[^a-z0-9+#]+", (j.get("title") or "").lower())):
            if len(tok) > 2 and tok not in _USAGE_STOP:
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
        _admin_user_cache.clear()
        flash("Jobs reloaded.")
        return redirect(url_for("admin_usage"))
    return render_template("admin_usage.html", u=_admin_usage(), ev=_admin_ev(),
                           evstats=analytics.stats())


@app.route("/admin/usage/user/<username>")
@admin_required
def admin_usage_user(username):
    """One account's usage. Its own ROUTE, not a panel on /admin/usage, for the reason stated at
    the top of admin_base.html: each admin page pays only for its own queries, and /admin/usage
    already walks the whole corpus.

    Unknown usernames 404 rather than rendering an empty profile -- an all-zero page for a typo
    reads as "this user does nothing", which is a different and wrong claim.
    """
    if request.args.get("refresh"):
        _admin_user_cache.pop(username, None)
        return redirect(url_for("admin_usage_user", username=username))
    known = {(u.get("username") or "") for u in (db.list_users() or [])}
    if known and username not in known:
        abort(404)
    prof = None
    try:
        prof = db.get_profile(username) or {}
    except Exception:
        prof = {}
    return render_template("admin_usage_user.html", d=_admin_usage_one(username),
                           prof=prof, acct=_account_state(username) or {})


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
            flash("Couldn't change that against %s. Is the admin schema loaded? (%s)"
                  % (db.backend_name(), e))
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
    has_remote_db() is false, and most of those files do not exist on the deployed box. So
    with Supabase briefly unreachable a delete would walk an empty local file, report
    "0 removed", and leave the real rows untouched. Reading that as "there was nothing to
    delete" is precisely how you delete the wrong thing on the retry, so destructive actions
    refuse rather than no-op. The count probe doubles as the liveness check.
    """
    if not db.has_remote_db():
        return ("No database is configured. Refusing to run against the local-file "
                "fallback. Nothing here would touch the real database.")
    if db.table_count(db.TABLE) is None:
        return ("Can't reach Supabase right now. Refusing to run a destructive action. "
                "try again in a moment.")
    return ""


def _bust_job_caches():
    """Everything derived from the job rows, after they change under us."""
    get_jobs(force=True)
    _score_cache.clear()
    _scores_clear()
    _rows_cache.clear()
    _base_rows_cache["fp"] = _base_rows_cache["rows"] = None   # the shared half of _rows_cache
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
    """Save / Mark Applied / Hide as a real form POST, so they work with JavaScript off.

    No longer unreferenced: templates/job.html posts here. That is also why it now checks CSRF.
    It had none — protected only by SameSite=Lax and `form-action 'self'`, which was a defensible
    bar for a route nothing called, and is not the bar for one on every job page.

    `via` distinguishes this page from the feed's XHR. A new VALUE in the existing dimension, not
    a new event: 'api' and 'form' already exist, 'job' joins them, and the old modal's traffic
    simply stops appearing, which is interpretable rather than confusing.
    """
    if not _check_csrf():
        flash("That form expired. Reload the page and try again.")
        return redirect(request.referrer or url_for("feed"))
    url = request.form.get("url", "")
    status = (request.form.get("status") or "").strip()   # liked|hidden|applied|'' (clear)
    if status and status not in db.USER_STATUSES:         # the twin of the check in api_action
        flash("That isn't an action this app knows about.", "error")
        return redirect(request.referrer or url_for("feed"))
    via = "job" if request.form.get("via") == "job" else "form"
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
        _ev_action(user, url, prev, status, via)
    except Exception:
        flash("Couldn't save that action. Try again.")
    return redirect(request.referrer or url_for("feed"))


# ----------------------------- résumé -----------------------------
def save_active_resume(user, text, rid=None):
    """Write résumé text to the LIBRARY row and mirror it into users.resume. Returns ok.

    The single write path, because there used to be two and they drifted: this endpoint wrote only
    the legacy column while Resume Brain wrote only the library, and live that left two of four
    accounts with a résumé in one store and nothing in the other — so the feed scored their jobs
    against an empty string. Anything that saves résumé text goes through here.
    """
    text = text or ""
    try:
        target = None
        if rid:
            target = next((r for r in (db.list_resumes(user) or []) if r.get("id") == rid), None)
        target = target or db.get_active_resume(user)
        if target and target.get("id"):
            db.save_resume(user, {"id": target["id"], "content": text,
                                  "created_at": target.get("created_at")})
            db.set_active_resume(user, target["id"])      # also mirrors into users.resume
        else:
            ok, new_id = db.save_resume(user, {"name": "My résumé", "content": text,
                                               "active": True})
            if ok:
                db.set_active_resume(user, new_id)
            else:
                db.set_user_resume(user, text)            # pre-migration: cache only, still usable
        _resume_cache[user] = (text, time.time())         # not the cookie (size cap)
        # THIS USER, not everybody. A bare _score_cache.clear() here reclaimed nothing and cost
        # a great deal: the cache is keyed on (username, md5(resume)), so a new résumé is
        # already a new key and the stale entry is unreachable the moment the new one is
        # written. Clearing every OTHER user invalidated entries that were still correct and
        # charged each of those users a full corpus rebuild (~1 s apiece) on their next page —
        # measured at 12.2 s of worker CPU for one résumé save. _bust_profile documents exactly
        # this and is the per-user version; it just was not called from here.
        _bust_profile(user)
        return True
    except Exception:
        return False


@app.route("/resume", methods=["GET", "POST"])
@login_required
def resume():
    if request.method == "POST":
        ok = save_active_resume(session["user"], request.form.get("resume", ""))
        flash("Saved. Your match scores now include it." if ok else "Couldn't save. Try again.")
        return redirect(url_for("brain_home"))
    # The score, the résumé library, the stories and the ATS view all live on one page now, so this
    # URL is a door rather than a destination. Kept (rather than deleted) because it is linked from
    # templates/tailor.html and from anywhere a user bookmarked it.
    return redirect(url_for("brain_home"))


# ----------------------------- sealing a secret into the session -----------------------------
# SIGNING IS NOT ENCRYPTION, and this app spent a while acting as though it were. Flask's default
# SecureCookieSession signs its payload with itsdangerous so a client cannot TAMPER with it; the
# contents are plain base64 JSON that anyone holding the cookie can read with no secret at all.
# The user's own Gemini/Claude API key sat in that payload, and the UI reassured them it was safe
# because it was "never written to the database" — the wrong reassurance, since the database is
# server-side and access-controlled while the cookie jar is a file on their own disk, readable by
# any local malware, backup, profile sync or forensic read for the full 30 days. HttpOnly and
# Secure are both on, both good, and both irrelevant to confidentiality at rest.
#
# So the value is ENCRYPTED before it goes in. Encrypt-then-MAC with two keys derived from
# app.secret_key: a keystream from HMAC-SHA256 in counter mode, then a tag over the ciphertext.
#
# STDLIB ONLY, deliberately. Fernet from `cryptography` would be the obvious choice and is
# installed on this laptop — but it is NOT in requirements-cpanel.txt, and the production box
# installs only what that file lists, so reaching for it would mean the AI key silently stopped
# working on the one host that matters. This needs nothing that is not already imported above.
def _seal_keys(purpose):
    secret = str(app.secret_key).encode()
    return (hmac.new(secret, b"seal-enc|" + purpose, hashlib.sha256).digest(),
            hmac.new(secret, b"seal-mac|" + purpose, hashlib.sha256).digest())


def _seal_stream(enc_key, nonce, n):
    out = bytearray()
    counter = 0
    while len(out) < n:
        out += hmac.new(enc_key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:n])


def seal(value, purpose=b"ai-key"):
    """A short string -> an opaque base64url blob only this server can read."""
    if not value:
        return ""
    enc_key, mac_key = _seal_keys(purpose)
    raw = value.encode("utf-8")
    nonce = secrets.token_bytes(16)
    ct = bytes(a ^ b for a, b in zip(raw, _seal_stream(enc_key, nonce, len(raw))))
    tag = hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(nonce + ct + tag).decode("ascii")


def unseal(blob, purpose=b"ai-key"):
    """The reverse. Empty string on anything that does not verify — a rotated app secret, a
    truncated cookie, a tampered blob. Never raises: the caller's fallback is "no key
    configured", which is a working page, not a 500."""
    if not blob:
        return ""
    try:
        raw = base64.urlsafe_b64decode(blob.encode("ascii"))
        if len(raw) < 32:
            return ""
        nonce, ct, tag = raw[:16], raw[16:-16], raw[-16:]
        enc_key, mac_key = _seal_keys(purpose)
        if not hmac.compare_digest(
                hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()[:16], tag):
            return ""
        return bytes(a ^ b for a, b in zip(
            ct, _seal_stream(enc_key, nonce, len(ct)))).decode("utf-8")
    except Exception:
        return ""


# ----------------------------- tailor (keyword gaps + optional AI) -----------------------------
def _ai_key_for(_user=None):
    """The AI key to use. Precedence: server ANTHROPIC_API_KEY (Claude — preferred when configured,
    since it's the deliberate server config and Gemini quotas run out), then a per-user key SEALED
    into this session (encrypted, HttpOnly, ~30 days), then GEMINI_API_KEY from the env. The key's
    prefix (sk-ant-… vs AIza…) selects the provider downstream in resume_brain/ai.py."""
    sess_key = ""
    try:
        sess_key = unseal(session.get("ai_key_sealed") or "")
        if not sess_key and session.get("gemini_key"):
            # A session issued before this was sealed. Re-seal it in place rather than logging
            # somebody out of their own key, and drop the readable copy on the way past.
            sess_key = session.pop("gemini_key") or ""
            if sess_key:
                session["ai_key_sealed"] = seal(sess_key)
    except Exception:
        sess_key = ""                                  # outside a request context (a test/script)
    return os.environ.get("ANTHROPIC_API_KEY") or sess_key or os.environ.get("GEMINI_API_KEY")


def _save_ai_key(key):
    """Encrypted, not merely signed — see seal() for why that distinction was worth a finding."""
    session.pop("gemini_key", None)
    session["ai_key_sealed"] = seal(key)
    session.permanent = True       # ride the 30-day login cookie


@app.route("/tailor")
@login_required
def tailor():
    url = request.args.get("url", "")
    job = _job_for(url)
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
    job = _job_for(url)
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
    job = _job_for(url)
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
def _csvf(s):
    return [x.strip() for x in (s or "").replace("\n", ",").split(",") if x.strip()]


def _render_brain(user, inputs, data):
    return render_template("brain_tailor.html", inputs=inputs, data=data,
                           have_resumes=bool(rb.list_resumes(user)),
                           have_key=bool(_ai_key_for(user)),
                           intensities=rb_voice.intensity_choices(),
                           default_intensity=rb_voice.DEFAULT_INTENSITY)


@app.route("/brain")
@login_required
def brain_home():
    """Resume Brain — the review panel. Résumés on the left, the graded document on the right.

    This used to be the tailor-to-a-job FORM, which is why the nav felt like it went to the wrong
    place: the library (résumés, stories, lessons) was one more click away behind "Teach Your Brain",
    and the score lived on a third page that nothing linked to. One page now owns all of it; the
    tailor form kept its own URL below.
    """
    user = session["user"]
    _ensure_resume_migrated(user)        # keeps the two résumé stores in step, both directions
    # Old feed/job links pass ?job=<url> expecting the tailor form. Redirect rather than break them.
    job_url = request.args.get("job", "")
    if job_url:
        return redirect(url_for("brain_tailor_page", job=job_url))

    resumes = rb.list_resumes(user) or []
    want = request.args.get("r") or ""
    chosen = next((r for r in resumes if r.get("id") == want), None) or db.get_active_resume(user)
    text = (chosen or {}).get("content") or ""
    level = request.args.get("level", "mid")
    level = level if level in resume_score.LEVELS else "mid"
    report = resume_score.score_resume(text, level) if text.strip() else None

    # Every résumé carries its own score in the rail. Cheap enough to do inline — the rubric is pure
    # regex over a few KB, no network and no model — and a library of scores is the thing that makes
    # "which of my résumés is strongest" answerable at a glance.
    cards = []
    for r in resumes:
        body = r.get("content") or ""
        try:
            s = resume_score.score_resume(body)["score"] if body.strip() else None
        except Exception:
            s = None
        cards.append({"id": r.get("id"), "name": r.get("name") or "Untitled résumé",
                      "chars": len(body), "score": s, "active": bool(r.get("active")),
                      "open": bool(chosen) and r.get("id") == chosen.get("id")})

    kw = None
    if report:
        try:
            _h, secs = resume_score.split_sections(text)
            items, _ = resume_score._experience_items(secs)
            kw = resume_keywords.evaluate(text,
                                          evidence_text=" ".join(i["text"] for i in items))
        except Exception:
            kw = None
    # Per-bullet review. Separate from the rubric because the rubric grades the document and this
    # answers the next question the user actually has: which line, and what do I write instead.
    bullets = resume_bullets.report(text) if report else None
    try:
        files = db.list_resume_files(user, (chosen or {}).get("id")) or []
    except Exception:
        files = []                       # table not migrated yet -> no Original tab, no crash

    return render_template(
        "brain_home.html",
        resumes=cards, chosen=chosen, report=report, level=level, bullets=bullets,
        levels=resume_score.LEVELS, keywords=kw, files=files,
        doc_html=resume_score.annotate_html(text, report["checks"]) if report else "",
        stories=rb.list_stories(user) or [], lessons=rb.list_lessons(user) or [],
        have_key=bool(_ai_key_for(user)))


@app.route("/brain/tailor", methods=["GET"])
@login_required
def brain_tailor_page():
    """The tailor-to-a-job form, prefilled + auto-run when ?job=<url> arrives from the feed.

    Same view this served at /brain before the review panel took that URL. Registered GET-only so it
    shares the rule with the POST handler below without either having to grow a method branch.
    """
    user = session["user"]
    _ensure_resume_migrated(user)
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
    """OPTIONAL AI layer: re-derive the plan, then have the model write the finished résumé +
    cover letter. Reuses the app's existing key (session, GEMINI_API_KEY or ANTHROPIC_API_KEY).
    `intensity` (voice.INTENSITY) decides how far it may depart from the original."""
    user = session["user"]
    key_in = (request.form.get("api_key") or "").strip()
    if key_in:
        _save_ai_key(key_in)
    key = _ai_key_for(user)
    inputs = {"company": (request.form.get("company") or "").strip(),
              "company_url": (request.form.get("company_url") or "").strip(),
              "job_url": (request.form.get("job_url") or "").strip(),
              "jd": (request.form.get("jd") or "").strip(),
              "intensity": (request.form.get("intensity") or "").strip()}
    if not key:
        flash("Add an AI key to use AI rewrite (the field on the tailor page).")
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
        out = rb_ai.rewrite(ctx, key, intensity=inputs["intensity"])
    except Exception as e:
        err = str(e)[:250]
    return render_template("brain_rewrite.html", out=out, error=err, ctx=ctx, inputs=inputs,
                           intensity=rb_voice.intensity_label(inputs["intensity"]))


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


# A PDF build spawns a Tectonic process that build_pdf allows up to 180 SECONDS, so a handful of
# concurrent presses is the whole worker pool. Every other expensive route here is limited; this
# one was not, and it is a plain cookie-authenticated POST anyone signed in can repeat.
_PDF_TIERS = ((3, 60), (30, 3600))


@app.route("/brain/export/resume.pdf", methods=["POST"])
@login_required
def brain_export_resume_pdf():
    hit = _rate_hit(("pdf", session["user"]), _PDF_TIERS)
    if hit:
        retry, cap, window = hit
        flash("That's %d PDF builds in %ds. Each one runs a LaTeX compile, so give it %d s."
              % (cap, window, retry), "error")
        return redirect(request.referrer or url_for("brain_home"))
    return _pdf_response(request.form.get("content", ""), session["user"], "Tailored_Resume")


@app.route("/brain/pdf_diag")
@login_required
def brain_pdf_diag():
    """Which document libraries does this host actually have? Reports the READ side (uploads) and the
    WRITE side (export), because a missing library on either shows up to the user as a vague
    "paste the text instead" or a silent fallback to .txt, and neither names the cause.

    Read side added after a live host answered "This server can't read .pdf files yet (missing
    library)" — pypdf was in requirements-cpanel.txt but the Python App's Run Pip Install had never
    been run, and nothing anywhere said so. Every reader is imported lazily by design, so the app
    boots fine and only the feature is missing; this is the page that tells you which one.

    Open it logged in and paste the JSON. The upload and export paths both swallow the real error;
    this doesn't.
    """
    from flask import jsonify
    import platform, subprocess
    info = {"os": platform.system(),
            "code_has_autobootstrap": hasattr(rb_latex, "_bootstrap_tectonic"),
            "repo_root": getattr(rb_latex, "_REPO_ROOT", "?")}
    # READ side: what resume_text_from_upload needs, per format. A False here is exactly what the
    # user sees as "this server can't read X files yet".
    readers = {}
    for fmt, mod in (("pdf", "pypdf"), ("docx", "docx")):
        try:
            m = __import__(mod)
            readers[fmt] = {"module": mod, "ok": True,
                            "version": getattr(m, "__version__", "?")}
        except Exception as e:
            readers[fmt] = {"module": mod, "ok": False, "error": str(e)[:120],
                            "fix": "cPanel -> Setup Python App -> Run Pip Install "
                                   "(requirements-cpanel.txt), then touch tmp/restart.txt"}
    readers["txt"] = readers["md"] = readers["tex"] = {"module": None, "ok": True}
    info["upload_readers"] = readers
    info["uploads_working"] = sorted(k for k, v in readers.items() if v.get("ok"))
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
    keep = {}
    uploaded, err = _uploaded_resume_text(keep=keep)
    if err:
        flash(err)
    content = uploaded or request.form.get("content", "")
    back = request.form.get("back") or "brain_teach"
    back = back if back in ("brain_teach", "brain_home") else "brain_teach"
    if not (content or "").strip():
        flash(err or "Nothing to save. Attach a file or paste the text.")
        return redirect(url_for(back))
    name = (request.form.get("name") or "").strip()
    if not name and uploaded:
        # Name it after the file rather than "Untitled résumé", so a library of several
        # uploads stays tellable apart without anyone having to type a label.
        name = os.path.splitext(keep.get("filename") or "")[0].strip()
    rid = rb.save_resume(user, {"id": request.form.get("id", ""),
                                "name": name or "Untitled résumé",
                                "content": content})
    # A freshly uploaded résumé becomes the live one. Uploading and then finding the feed still
    # scoring the previous file is the confusing half of having a library at all.
    try:
        if rid:
            db.set_active_resume(user, rid)
    except Exception:
        pass
    _store_resume_file(user, rid, keep)
    _resume_cache.pop(user, None)
    _bust_profile(user)
    flash(("Read %d characters from that file. " % len(content) if uploaded else "")
          + "Résumé saved. Your match scores now include it.")
    return redirect(url_for(back, r=rid) if back == "brain_home" else url_for(back))


# The optional trailing name is COSMETIC and ignored server-side. Chrome's built-in PDF
# viewer titles the document from the last path segment, so a URL ending in the file id
# displayed a bare uuid ("8de350e11eb14b71a499f2186460930a") across the top of the user's
# own résumé. The real filename still comes from the database row, never from the URL.
@app.route("/brain/resume/file/<fid>")
@app.route("/brain/resume/file/<fid>/<path:name>")
@login_required
def brain_resume_file(fid, name=None):
    """Stream one stored artifact back. Scoped to the signed-in user by the query itself, not by a
    check afterwards, so a guessed id returns nothing rather than someone else's résumé."""
    import base64
    rec = db.get_resume_file(session["user"], fid)
    if not rec or not rec.get("b64"):
        # 404, NOT a redirect. This URL is the src of an iframe, and redirecting it to an HTML page
        # made the browser complain that framing was blocked by frame-ancestors — a confusing report
        # of a missing file, pointing at the wrong cause entirely.
        return Response("No such file.", status=404, mimetype="text/plain")
    try:
        body = base64.b64decode(rec["b64"])
    except Exception:
        return Response("That stored file could not be decoded.", status=404, mimetype="text/plain")
    # Quotes AND control characters. Werkzeug rejects a header carrying a bare CR/LF and 500s
    # rather than splitting the response, so this was a self-inflicted error page rather than
    # response splitting — but the filename is user-supplied, and a response header is the wrong
    # place to discover that. Strip everything that cannot legally appear in one.
    fname = re.sub(r'[\r\n"\x00-\x1f\x7f]', "",
                   rec.get("filename") or ("resume." + (rec.get("kind") or "bin"))) or "resume.bin"
    # inline, not attachment: the Original tab embeds this in an <iframe> to show the real document.
    #
    # BOTH framing headers are overridden here, and that is the whole reason the preview was blank.
    # The site-wide policy sets `frame-ancestors 'none'` and `X-Frame-Options: DENY`, which is right
    # for HTML pages — and it applies to THIS response too, so the PDF refused to be framed by its
    # own site. `frame-ancestors` governs who may embed a document, including a same-origin parent;
    # it is not only about other people's sites.
    #
    # Set explicitly rather than by loosening the global policy: the after_request hook uses
    # setdefault, so these win, and clickjacking protection stays absolute everywhere else. The
    # policy here is also STRICTER than the site's for everything but framing — a stored document is
    # untrusted content and has no business loading anything at all.
    # 'self' alone is one redirect away from failing. The site answers on both stemjobs1... and
    # www.stemjobs1..., and if the host canonicalises one to the other then the iframe's request
    # redirects to a DIFFERENT origin than the page framing it, and 'self' no longer matches — the
    # same blank preview, from a cause with no relation to this code. Naming both spellings of the
    # current host costs one line and removes that entire class of failure.
    scheme = "https" if (request.is_secure or request.headers.get(
        "X-Forwarded-Proto", "").lower() == "https") else "http"
    host = request.host
    twin = host[4:] if host.startswith("www.") else "www." + host
    ancestors = "'self' %s://%s %s://%s" % (scheme, host, scheme, twin)
    return Response(body, mimetype=rec.get("mime") or "application/octet-stream",
                    headers={"Content-Disposition": 'inline; filename="%s"' % fname,
                             "X-Content-Type-Options": "nosniff",
                             # X-Frame-Options has no multi-origin form, so it stays SAMEORIGIN;
                             # browsers that support CSP prefer frame-ancestors over it anyway.
                             "X-Frame-Options": "SAMEORIGIN",
                             "Content-Security-Policy":
                                 "default-src 'none'; object-src 'none'; frame-ancestors " + ancestors,
                             "Cache-Control": "private, max-age=300"})


@app.route("/brain/resume/activate", methods=["POST"])
@login_required
def brain_resume_activate():
    """Make one résumé the live one -- the thing that decides every match % in the feed.

    db.set_active_resume has existed since the library did, but nothing ever called it as a user
    ACTION: all three callers were side effects of saving, so the only way to change which
    résumé was live was to upload it again. The rail rendered a `live` badge nobody could move.

    No cache to invalidate on the score path, and that is by construction rather than luck:
    _score_cache and the persisted score files are keyed on (username, resume_md5), so a
    different live résumé is a different key and reads a different entry. What DOES need busting
    is the profile text those keys are derived from.
    """
    user = session["user"]
    rid = (request.form.get("id") or "").strip()
    row = db.set_active_resume(user, rid) if rid else None
    if row:
        _bust_profile(user)
        _resume_cache.pop(user, None)
        flash("“%s” is now your live résumé. Match percentages are scored against it."
              % (row.get("name") or "That résumé"))
    else:
        # set_active_resume returns None for an id that is not this user's, and ALSO if
        # resumes.active does not exist on the table yet -- see MIGRATION_resume_files.sql,
        # which is still unapplied. Say so rather than reporting a success that did nothing.
        flash("Couldn't switch résumé. If this keeps happening the `resumes.active` column is "
              "missing — run MIGRATION_resume_files.sql.")
    return redirect(request.referrer or url_for("brain_home"))


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
    """Job search for the in-Brain picker, by title/company, restricted to jobs with a stored JD.

    The cap was a flat 20, which for a query like "engineer" silently hid almost everything and
    made the picker look broken -- so people pasted the JD by hand instead, which is the input
    burden this picker exists to remove. `limit` is now a query arg (default 50, hard max 200) so
    the client can ask for more without this becoming an unbounded scan of the corpus.
    """
    q = (request.args.get("q") or "").strip().lower()
    try:
        limit = min(200, max(1, int(request.args.get("limit") or 50)))
    except (TypeError, ValueError):
        limit = 50
    out = []
    if q:
        have_jd = db.urls_with_jd()      # which jobs have a stored description (feed rows omit it)
        for j in get_jobs():
            hay = (j.get("title", "") + " " + j.get("company", "")).lower()
            if q in hay and j.get("url") in have_jd:
                out.append({"url": j.get("url", ""), "title": j.get("title", ""),
                            "company": j.get("company", "")})
                if len(out) >= limit:
                    break
    return {"jobs": out, "capped": len(out) >= limit}


# ----------------------------- the company directory -----------------------------
# companies.json is ~127 KB / 2.1 k rows, built by scripts/build_companies.py. Deferred to
# first use for the same reason as sponsor_counts above: Passenger's cold start is where
# shared-hosting memory is tightest, and /companies is not on the path to the feed.
_companies_cache = None


def companies_blob():
    """companies.json, or a shaped empty blob. Never raises: a bad file must not 500 the nav."""
    global _companies_cache
    if _companies_cache is None:
        blob = {}
        try:
            with open("companies.json", encoding="utf-8") as fh:
                blob = json.load(fh) or {}
        except Exception:
            blob = {}
        blob.setdefault("sectors", [])
        blob.setdefault("rows", [])
        blob.setdefault("prefix", {})
        blob.setdefault("li_kw", {})
        _companies_cache = blob
    return _companies_cache


_BOARDS_TTL = 120
_boards_cache = {"data": None, "at": 0.0}


def _recent_boards():
    """[(company, url)] from the boards table — both scraped boards and apply-direct rows.

    Read here rather than left to the next build so a company added through /add shows up in
    the directory immediately. Cached, and never raises: no database means no extra rows,
    which is the same contract scraper.custom_sources already has.
    """
    c = _boards_cache
    if c["data"] is not None and time.time() - c["at"] < _BOARDS_TTL:
        return c["data"]
    out = []
    try:
        for b in db.list_boards() or []:
            name = (b.get("company") or "").strip()
            if name:
                out.append((name, (b.get("url") or "").strip()))
    except Exception:
        out = []
    c["data"], c["at"] = out, time.time()
    return out


_CO_STATS_TTL = 120
_co_stats_cache = {"data": None, "at": 0.0}


def _company_stats():
    """{norm_key: (open_roles, corpus_spelling)} across the whole corpus.

    Two things the shipped file deliberately does NOT carry, because both go stale: the scrape
    runs four times a day and build_companies.py runs by hand.

    The spelling matters as much as the count. /company?c= filters on db.block_key, which does
    NOT strip legal suffixes, so a card linking the registry spelling "Accenture" lands on a
    page that finds nothing while the corpus stores "Accenture LLP". Six employers differ that
    way today — Accenture, Meta Platforms, DoorDash, HP, Array and Mican — and the failure is
    invisible by eye, because the card looks right and only the destination is empty.

    User-independent, so unlike ranked_rows this can be cached process-wide. One pass over the
    already-in-memory get_jobs() list; core.norm_company is memoized, so this is ~1.2 k
    normalizations and ~22 k dict hits.
    """
    c = _co_stats_cache
    if c["data"] is not None and time.time() - c["at"] < _CO_STATS_TTL:
        return c["data"]
    counts = collections.Counter()
    spellings = collections.defaultdict(collections.Counter)
    for j in get_jobs():
        name = (j.get("company") or "").strip()
        if not name:
            continue
        key = core.norm_company(name)
        # `is not False` rather than `not ...`: is_active is None on an un-migrated row, and
        # only an explicit False means "we checked and the posting is gone" — same rule as
        # _admin_stats above.
        if j.get("is_active") is not False:
            counts[key] += 1
        spellings[key][name] += 1
    out = {k: (counts.get(k, 0), v.most_common(1)[0][0]) for k, v in spellings.items()}
    c["data"], c["at"] = out, time.time()
    return out


def _linkedin_url(name):
    """A United-States-filtered LinkedIn job search. Reproduces the rule the old
    scraper/make_careers.py used, which is the only link that resolves for every employer —
    including the ~450 with no careers page we can name."""
    kw = companies_blob().get("li_kw", {}).get(name, name)
    return ("https://www.linkedin.com/jobs/search/?keywords=%s&location=United%%20States"
            % quote(kw))


def _expand_careers(val, prefix):
    """'gh|samsara' -> the full Greenhouse URL. Board hosts are stored as a prefix code because
    four of them cover about a third of every board URL in SOURCES."""
    if not val:
        return ""
    tag, sep, rest = val.partition("|")
    return (prefix.get(tag, "") + rest) if sep and tag in prefix else val


_companies_by_key = None


def _company_links(name):
    """{careers, kind, linkedin} for one employer, read off companies.json.

    Shared with /companies rather than reimplemented, so the directory card and the employer
    page can never disagree about where somebody applies. A name the file does not hold (a
    company that appeared since the last build) still gets its LinkedIn search, which is why
    every card has somewhere to go.
    """
    global _companies_by_key
    blob = companies_blob()
    if _companies_by_key is None:
        # Keyed on core.norm_company rather than stored in the file, so the key follows
        # scraper._norm_name if that ever changes.
        _companies_by_key = {}
        for row in blob.get("rows", []):
            _companies_by_key.setdefault(core.norm_company(row[0]), row)
    row = _companies_by_key.get(core.norm_company(name))
    return {"careers": _expand_careers(row[2], blob.get("prefix", {})) if row else "",
            "kind": row[3] if row else 0,
            "linkedin": _linkedin_url(name)}


@app.route("/companies")
@login_required
def companies():
    """Every employer we scrape, plus every sponsor we know of and don't.

    Wholly client-rendered from one inline JSON block, the same shape _feedgrid.html uses. That
    is deliberate: 2.1 k rows is ~38 KB gzipped, well inside _FEED_INLINE_MAX, and a server-side
    ?q= would add a fourth member to the filter family that web.py::_filter_rows,
    app.js::matches and core.prefs_match already have to keep in agreement.
    """
    blob = companies_blob()
    stats = _company_stats()
    # Attach the two volatile fields per row: open roles, and the spelling /company must be
    # linked with. Cheap — one dict lookup each — and it keeps them out of the shipped file.
    rows, seen = [], set()
    for name, sec, careers, kind, domain, h1b, mask in blob.get("rows", []):
        key = core.norm_company(name)
        seen.add(key)
        live, spelling = stats.get(key, (0, ""))
        rows.append([name, sec, careers, kind, domain, h1b, mask, live, spelling or name])
    # Two kinds of employer the shipped file cannot know about, both of which would otherwise
    # be invisible until somebody remembered to re-run build_companies.py: one added through
    # /add, and one the scraper started returning since the last build. The scrape runs four
    # times a day and the build is manual, so "until the next build" means "for days".
    # Sector stays unknown (-1) until a build classifies it, which is honest — the page files
    # those under Unsorted rather than guessing.
    extra = [(n, u) for n, u in _recent_boards()]
    extra += [(spelling, "") for _k, (_live, spelling) in stats.items() if spelling]
    for name, url in extra:
        key = core.norm_company(name)
        if key in seen:
            continue
        seen.add(key)
        live, spelling = stats.get(key, (0, ""))
        rows.append([name, -1, url, 2 if url else 0, "",
                     int(sponsor_counts().get(key) or 0),
                     int(visa_index().get(key) or 0)
                     | (32 if core.is_cap_exempt(name) else 0)
                     | (64 if core.is_agency(name) else 0),
                     live, spelling or name])
    rows.sort(key=lambda r: r[0].lower())
    # No analytics.emit here on purpose. The _ev_page_view after_request hook already reports
    # every HTML 200 with ep=<endpoint>, so an explicit page_view would be the SECOND one for
    # this route -- and inflated usage numbers are a mistake this app has already made twice.
    # THE LOGO MANIFEST RIDES IN cometa, NOT IN THE ROW. scripts/test_companies_page.py freezes
    # the served row at nine fields, and the logo set is rebuilt on a different cadence than the
    # directory anyway -- so coupling them would mean a build_companies.py run against a stale
    # logo directory silently blanking every tile. Only the entries this page can actually use
    # are sent: the slugs, at about 28 bytes each, which gzips to a few KB.
    #
    # THE ALIAS MAP IS DELIBERATELY NOT AMONG THEM. It is keyed on core.norm_company, and
    # companies.js looked it up by SLUG -- two different spellings of one key, so the lookup
    # could never hit for any name, and measured over all 2,695 rows it cost zero tiles. It is
    # not fixable client-side for the same reason the monograms below are computed here, so the
    # ~16 KB stopped being sent rather than being sent and ignored. _logo_slug still uses it:
    # the feed resolves server-side and genuinely needs it.
    man = _logo_manifest()
    # THE MONOGRAMS ARE COMPUTED SERVER-SIDE, index-parallel to rows, and this deletes a twin
    # rather than creating one. The rule needs core.norm_company -- which strips Technologies,
    # Group, Labs and the legal suffixes -- and a JavaScript copy cannot have that without
    # duplicating the suffix list. Measured: a raw-name version disagreed on 225 of 2,695 names
    # and collapsed every "<X> Technologies" employer onto AT. ~13 KB of two-letter strings,
    # which gzip flattens, in exchange for one fewer thing that can drift.
    mono = [initials(r[0]) for r in rows]
    return render_template("companies.html", rows=rows, mono=mono,
                           sectors=blob.get("sectors", []),
                           prefix=blob.get("prefix", {}),
                           li_kw=blob.get("li_kw", {}),
                           logos={"v": man["v"], "ar": man["ar"]},
                           visa_labels=core.VISA_TAG_LABELS)


@app.route("/careers")
@login_required
def careers():
    """Kept as a redirect rather than deleted: the URL was in the nav for months, so it is in
    histories and bookmarks, and scripts/smoke_app.py builds its surface from app.url_map and
    would silently stop covering the page."""
    return redirect(url_for("companies"), code=301)


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
        # BEFORE anything is detected or stored. Every sibling write path already does this —
        # ext_save runs is_http_url + canonical_url, bulk_jobs drops non-http rows — but the
        # apply-direct branch below took the raw form value and handed it to db.add_board, and
        # /companies then renders it as href="…" for every user. CSP currently blocks a
        # javascript: navigation, so this was defence in depth rather than live XSS; it was
        # still the one input path in the app that skipped a validator all its neighbours apply.
        if url and not scraper.is_http_url(url):
            result = ("err", "That needs to be an http:// or https:// link to a careers page.")
            url = ""                    # nothing below runs; the page still lists your boards
        if url:
            det = (scraper.detect_board(url) or scraper.detect_paylocity(url)
                   or scraper.detect_jibe(url)
                   or scraper.detect_phenom(url) or scraper.detect_successfactors(url)
                   or scraper.detect_linked_ats(url) or scraper.detect_jsonld(url))
            if not det:
                # No readable board. This used to dead-end here, telling the user to edit
                # sponsors.txt -- a file on the server they have no way to reach from a
                # browser. Record it as apply-direct instead, so the company still reaches
                # the directory with its link.
                #
                # 'direct' is deliberately NOT one of scraper.SCRAPERS' 32 keys, which is what
                # makes this safe with no extra guard anywhere: custom_sources() filters on
                # `t in SCRAPERS`, so the scraper never sees this row, and neither does
                # build_careers_md.py's coverage check. db.list_boards() selects * and still
                # returns it, which is what /companies reads.
                if not name:
                    result = ("err", "That isn't a readable job board, so it can only be "
                              "listed as apply-direct — which needs a company name. Add one "
                              "and submit again.")
                else:
                    # Same normalization every detect_* branch already returns, so an
                    # apply-direct row is stored in the one shape the rest of the app expects
                    # rather than whatever was pasted in.
                    ok, msg = db.add_board(scraper.canonical_url(url), "direct", name,
                                           added_by=session["user"])
                    if ok:
                        result = ("ok", "Added %s as apply-direct. It will show in Companies "
                                  "with your link; the scraper won't read it." % name)
                    elif "boards" in msg.lower() or "does not exist" in msg or "42P01" in msg:
                        result = ("sql", msg)
                    else:
                        result = ("err", msg)
            elif det[0] in {u for u, _, _ in scraper.SOURCES}:
                result = ("info", "%s is already a built-in source, so there is nothing to add." % det[2])
            else:
                burl, ats, guess = det
                n = scraper.probe_board(burl, ats)
                if n is None:
                    result = ("err", "Detected a %s board but couldn't read any postings." % ats)
                else:
                    # NEVER store detect_board's third value unchallenged. It is _name_from on
                    # the URL slug -- a suggestion for this form, not a fact -- and nine
                    # employers were recorded from it verbatim: World Fuel Services as
                    # "Wfscorp", Monogram Health as "Mon1026Monoh", and 131 Goldman Sachs
                    # postings as "Hdpc", an opaque Oracle tenant id that matches nothing in
                    # the filing data and so showed no sponsorship at all.
                    #
                    # sluglike() is true for "Samsara" too, because there the slug really is
                    # the company. So it can only decide whether to go LOOK, never to reject:
                    # ask the board what it calls itself, and only insist on a typed name when
                    # the board won't say and the guess adds nothing to the URL.
                    if not name and scraper.name_is_sluglike(guess, burl):
                        name = scraper.board_display_name(burl, ats)
                        if not name:
                            result = ("err", "That looks like a %s board, but its URL only "
                                      "carries a tenant code and the board doesn't publish a "
                                      "company name. Type the company name and submit again."
                                      % ats)
                if result is None and n is not None:
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
    """Remove a board YOU added — or anything, if you are an admin.

    `boards` is a SHARED table: one row removes an employer from the next scrape and from
    /companies, for everybody. This route had no ownership check at all, so any account could
    delete any board. The bare `except: pass` under it meant the redirect looked identical
    whether the delete worked, hit a database error, or named a row that does not exist — so a
    failure was indistinguishable from a success.
    """
    url = (request.form.get("url") or "").strip()
    if not url:
        flash("No board named.", "error")
        return redirect(url_for("add_board"))
    try:
        rows = db.list_boards() or []
    except Exception:
        flash("Couldn't reach the database, so nothing was deleted.", "error")
        return redirect(url_for("add_board"))
    row = next((b for b in rows if (b.get("url") or "") == url), None)
    if row is None:
        flash("That board is not in the list.", "error")
        return redirect(url_for("add_board"))
    if not is_admin() and (row.get("added_by") or "") != session["user"]:
        flash("Someone else added that board, so only an admin can remove it.", "error")
        return redirect(url_for("add_board"))
    try:
        db.delete_board(url)
    except Exception as e:
        flash("Delete failed: %s" % e, "error")
        return redirect(url_for("add_board"))
    flash("Removed %s." % (row.get("company") or url), "ok")
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
        job = _job_for(url)
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
        flash("One-time setup needed. Run the SQL at the bottom of this page against %s,"
              " then try again." % db.backend_name())
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


# Excel and Sheets execute a cell that opens with any of these, so a scraped job title of
# `=HYPERLINK("http://evil","Click")` runs the moment the export is opened. `company` and `title`
# come from job boards (and from /api/ext/bulk_jobs, which any token holder can write); `notes` is
# free user text. None of it is ours, and an export is exactly where untrusted text becomes code.
_CSV_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def _csv_cell(v):
    s = "" if v is None else str(v)
    return ("'" + s) if s.startswith(_CSV_FORMULA_LEAD) else s


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
        w.writerow([_csv_cell(a.get("company", "")), _csv_cell(a.get("title", "")),
                    _csv_cell(a.get("status", "")), _csv_cell(a.get("applied_date", "") or ""),
                    _csv_cell(a.get("url", "")), _csv_cell(a.get("notes", ""))])
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


# What a derived token can look like at all: <username>:<32 lowercase hex>. Anything else is
# rejected on shape, before any comparison and long before any query.
_EXT_TOKEN_RE = re.compile(r"^([^\s:]{1,64}):([0-9a-f]{32})$")

# Callers whose tokens keep failing, so a sprayer cannot buy an unbounded number of epoch
# lookups. See _ext_user for why this exists and why a real client never reaches it.
_EXT_BAD = collections.OrderedDict()      # rate key -> [failures, window_start]
_EXT_BAD_MAX_KEYS = 2000
_EXT_BAD_ALLOW = 5
_EXT_BAD_WINDOW = 900


def _ext_epoch_lookup_allowed():
    """True while this caller may still spend a DATABASE read on an unverified token.

    Only reached by a token that failed the epoch-0 HMAC, which for a real client happens
    exactly never: a client either holds a valid epoch-0 token (verified with no query) or a
    valid rotated one (verified with one query, which then succeeds and is not counted here).
    So the budget is spent only by guesses.
    """
    try:
        key = _ext_rate_key()
    except Exception:
        return True                        # outside a request context (a test, a script)
    now = time.time()
    rec = _EXT_BAD.get(key)
    if rec is None or now - rec[1] > _EXT_BAD_WINDOW:
        rec = [0, now]
    if rec[0] >= _EXT_BAD_ALLOW:
        _EXT_BAD[key] = rec
        _EXT_BAD.move_to_end(key)
        return False
    return True


def _ext_note_bad_token():
    try:
        key = _ext_rate_key()
    except Exception:
        return
    now = time.time()
    rec = _EXT_BAD.get(key)
    if rec is None or now - rec[1] > _EXT_BAD_WINDOW:
        rec = [0, now]
    rec[0] += 1
    while len(_EXT_BAD) >= _EXT_BAD_MAX_KEYS:
        _EXT_BAD.popitem(last=False)
    _EXT_BAD[key] = rec
    _EXT_BAD.move_to_end(key)


def _ext_user(token):
    """Username for a valid extension token, else None.

    THE HMAC IS VERIFIED BEFORE ANY DATABASE READ. That is what the docstring here has always
    claimed and what the code did not do: it called _ext_token(username), which resolves the
    user's token_epoch through _account_state, which is a query. So five invalid tokens for five
    invented usernames were five lookups — unauthenticated, on CORS-open routes, and on a shared
    host that has been suspended for load once already.

    Three gates, cheapest first:

      1. SHAPE. <username>:<32 hex>, bounded length. Costs a regex.
      2. EPOCH 0, which is every account that has never rotated its token and therefore almost
         all of them. _ext_token(username, epoch=0) needs no state at all, so a wrong signature
         dies here having touched nothing.
      3. Only a token that survived neither — i.e. one claiming to belong to a user who HAS
         rotated — is worth a query, and only while that caller still has failure budget. A real
         rotated client spends none of it, because its lookup succeeds.
    """
    m = _EXT_TOKEN_RE.match((token or "").strip())
    if not m:
        return None
    token, username = m.group(0), m.group(1)
    if hmac.compare_digest(_ext_token(username, epoch=0), token):
        st = _account_state(username)
        if st is None or st.get("disabled_at"):
            return None
        # An epoch-0 signature from a user who has since rotated is a REVOKED token, which is
        # the whole point of the epoch. Check it here rather than trusting the match above.
        try:
            if int((st or {}).get("token_epoch") or 0):
                return None
        except (TypeError, ValueError):
            return None
        return username
    if not _ext_epoch_lookup_allowed():
        return None
    st = _account_state(username)
    if st is None or st.get("disabled_at") or not hmac.compare_digest(
            _ext_token(username, epoch=(st or {}).get("token_epoch") or 0), token):
        _ext_note_bad_token()
        return None
    return username


# ----------------------------- CSRF on cookie-authenticated writes -----------------------------
# A token check existed and was applied to six routes; twenty-two others — every one of them
# cookie-authenticated and state-changing — had none. The worst was POST /profile, which rebuilds
# all 39 profile fields from the submitted form and BLANKS everything it omits, so a single-field
# cross-site POST wiped the profile. /board/delete, /application/delete and seven /brain/*/delete
# routes were in the same set.
#
# SameSite=Lax and CSP form-action 'self' were already real mitigations, which is why this was a
# moderate finding rather than a critical one. They are not a reason to skip the token: Lax still
# permits top-level cross-site GET navigations, and one browser quirk away it is the only thing
# standing between a link and a wiped profile.
#
# Enforced HERE, in one hook, rather than by decorating twenty-two routes — the same argument
# admin_required already makes for centralising it. A route added next year is covered by default
# and has to opt OUT deliberately, which is the safe direction for this kind of check.
_CSRF_EXEMPT = frozenset((
    "/login",      # no session exists yet; enforcing here breaks a legitimate sign-in, and
                   # login CSRF is a nuisance rather than a compromise
    "/api/ev",     # public, no-ops without a session, writes nothing a forger benefits from
    "/logout",     # clearing your own session is not worth a token; see the note in the report
    "/api/db",     # HMAC-signed, never cookie-authenticated — a cross-site page cannot forge a
                   # signature, and the CSRF check below would never fire on it anyway (no
                   # session). Listed explicitly so that stays true if the guard changes.
))


@app.before_request
def _require_csrf():
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    p = request.path
    # Bearer-token routes are not cookie-authenticated, so a cross-site page cannot attach the
    # victim's credential and CSRF does not apply. They have their own limiter above.
    if p.startswith("/api/ext/") or p in _CSRF_EXEMPT:
        return None
    if not session.get("user"):
        return None                # nothing to forge on behalf of an anonymous caller
    if _check_csrf():
        return None
    from flask import jsonify
    # Answer in the shape the caller can actually read. A fetch() that gets a 302 to an HTML page
    # fails silently in the console; a browser form that gets JSON shows the user raw text. Treat
    # anything programmatic — an /api/ path, a JSON body, an XHR marker, or a caller that tried to
    # send the header at all — as wanting JSON.
    programmatic = (request.path.startswith("/api/") or request.is_json
                    or request.headers.get("X-CSRF-Token") is not None
                    or request.headers.get("X-Requested-With") == "XMLHttpRequest"
                    or "application/json" in (request.headers.get("Accept") or ""))
    if programmatic:
        return jsonify({"ok": False,
                        "error": "That page has been open a while and its security token expired. "
                                 "Reload and try again."}), 400
    flash("That page has been open a while and its security token expired. Reload and try again.")
    return redirect(request.referrer or url_for("feed"))


# ----------------------------- extension API rate limiting -----------------------------
# The /api/ext/* routes are CORS-open and bearer-token authenticated, and until now NOTHING
# throttled them. A leaked token — and these never expire, and travel in query strings on GET
# routes, so leaking is realistic — bought unbounded spend on the OPERATOR's Anthropic/Gemini
# keys, unbounded rows into the shared jobs table, and unbounded appends to an unrotated
# ext_debug_log.jsonl. Login has had a limiter for exactly this reason; these did not.
#
# ONE before_request hook rather than a decorator on each of the sixteen routes, for the same
# reason admin_required centralises CSRF: a route added next year cannot forget to opt in.
#
# Limits are per (class, token) and deliberately generous for normal use — the batch filler
# legitimately walks 50 jobs in a sitting. They bite on the abuse shapes, not on real work.
# ONE BUCKET PER NAMESPACE, and each bounded on its own. This was a single dict shared by the
# extension limiter and the feed limiter and emptied wholesale at the cap, so 5001 unique
# ?token= values sprayed at the CORS-open /api/ext/* routes reset every logged-in user's FEED
# limiter as a side effect — an anonymous caller disabling the brake that protects the worker
# pool. Separate dicts mean one namespace can never evict another's, and oldest-first eviction
# means a caller cannot aim the eviction at anybody in particular.
_ext_hits = collections.defaultdict(collections.OrderedDict)   # ns -> {key: [timestamps]}
_EXT_MAX_KEYS = 5000                 # per namespace


def _rate_hit(key, tiers):
    """Record one call against `key`. Returns (retry_seconds, cap, window) if a tier is now
    exceeded, else None.

    TIERS, plural, because a single window is not a brake. 240-per-60s permits all 240 landing
    inside one second, which is exactly the saturation event a limiter is for. A short tier
    bounds the burst and a long one bounds the sustained rate.

    Rejected calls are NOT recorded, so a client that keeps hammering while limited does not
    push its own retry time further out forever.
    """
    now = time.time()
    ns, who = key
    bucket = _ext_hits[ns]
    while len(bucket) > _EXT_MAX_KEYS:
        bucket.popitem(last=False)       # oldest key out, one at a time
    longest = max(w for _, w in tiers)
    hist = [t for t in bucket.get(who, ()) if now - t < longest]
    for cap, window in tiers:
        recent = [t for t in hist if now - t < window]
        if len(recent) >= cap:
            bucket[who] = hist
            bucket.move_to_end(who)
            return int(window - (now - recent[0])) + 1, cap, window
    hist.append(now)
    bucket[who] = hist
    bucket.move_to_end(who)
    return None
# TIERS, PLURAL — the thing _rate_hit's own docstring says a limiter must have, and the thing
# this one did not. A lone 40-per-3600s permits all 40 landing inside one second, so the AI
# class accepted forty runs that each spend the operator's Anthropic key and spawn a LaTeX
# process, simultaneously; the default class accepted nine hundred. The burst tiers below are
# sized from what the extension actually does — the batch filler walks a job at a time behind a
# network round trip, so it cannot reach even the smallest of them.
_EXT_CLASSES = (
    # (path suffixes, tiers, label)
    (("tailor", "answer", "vision"), ((3, 20), (40, 3600)),
     "AI calls — these spend the server's API keys, and tailor also spawns a LaTeX process"),
    (("bulk_jobs", "jds", "debug", "detect_board"), ((10, 20), (120, 3600)),
     "bulk writes into shared tables and the unrotated debug log"),
)
_EXT_DEFAULT = (((30, 10), (900, 3600)), "extension API")


def _ext_rate_key():
    """Throttle by TOKEN where we have one, else by client address so an unauthenticated
    sprayer is bounded too. The token is not validated here — that is _ext_user's job; this
    only needs a stable string to count against."""
    tok = (request.args.get("token") or "").strip()
    if not tok:
        try:
            body = request.get_json(silent=True) or {}
            tok = str(body.get("token") or "").strip()
        except Exception:
            tok = ""
    return ("t:" + tok[:64]) if tok else ("ip:" + (request.remote_addr or "?"))


@app.before_request
def _ext_rate_limit():
    if not request.path.startswith("/api/ext/") or request.method == "OPTIONS":
        return None                  # CORS preflight carries no credentials and does no work
    from flask import jsonify
    leaf = request.path.rsplit("/", 1)[-1]
    tiers, label = _EXT_DEFAULT
    for names, tl, lbl in _EXT_CLASSES:
        if leaf in names:
            tiers, label = tl, lbl
            break
    hit = _rate_hit((label, _ext_rate_key()), tiers)
    if hit:
        retry, cap, window = hit
        # Say which tier bit and in the unit it happened in. With a burst tier in play,
        # "try again in 1 min" for a 20-second window was both wrong and needlessly alarming.
        wait = ("%d min" % max(retry // 60, 1)) if retry >= 60 else ("%d s" % max(retry, 1))
        resp = jsonify({"ok": False,
                        "error": "Rate limit reached for %s (%d in %ds). Try again in %s."
                                 % (label, cap, window, wait)})
        resp.status_code = 429
        resp.headers["Retry-After"] = str(retry)
        return _cors(resp)           # CORS-open route: the browser must be able to READ the 429
    return None


# ----------------------------- feed API rate limiting -----------------------------
# /api/feed was the one unguarded route that can saturate the pool, and it does not need a
# stolen token or any malice to do it -- measured at 173 ms per request with a search term,
# 3.4x any other route in the app. One person typing occupies most of a worker; a runaway
# fetch loop in a stale tab occupies all of them. _ext_rate_limit did not cover it, because
# it returns early on anything outside /api/ext/.
#
# SIZED FROM THE CLIENT, not from a guess. app.js debounces search, location and the match
# slider at 250 ms (debouncedRender) and pages behind a button, so the fastest a real browser
# can go is 4 requests/second, and only while someone types without pausing. 240/60s is
# exactly that ceiling, so normal use cannot reach it; 30/5s allows a 6/second burst, which is
# above anything the UI produces and far below what a loop produces.
#
# Per USERNAME, not per IP: one household behind one address must not throttle each other,
# and every caller here is logged in. Anonymous requests fall back to the address so an
# unauthenticated sprayer is still bounded before it reaches login_required.
_FEED_TIERS = ((30, 5), (240, 60))


@app.before_request
def _feed_rate_limit():
    if request.path != "/api/feed":
        return None
    # In-process harnesses page the WHOLE corpus for every filter case as fast as they can --
    # scripts/feed_parity.py alone walks several hundred requests -- which is precisely the burst
    # shape this blocks. app.testing is the right gate rather than an env var or a header: it is
    # set by the harness inside the process (feed_parity.py, smoke_app.py and three suites already
    # set it) and there is no way for a client to turn it on. Production never does.
    #
    # This does NOT leave the limiter untested. scripts/test_feed_ratelimit.py deliberately does
    # not set TESTING, and asserts that it has not been set, so the guard cannot be voided by
    # someone adding the flag to that file later.
    if app.testing:
        return None
    from flask import jsonify
    who = session.get("user") or ("ip:" + (request.remote_addr or "?"))
    hit = _rate_hit(("feed", who), _FEED_TIERS)
    if not hit:
        return None
    retry, cap, window = hit
    # A JSON body with the same keys the route normally returns, because app.js reads
    # `rows`/`total` and an absent `rows` used to render as "no jobs match" -- a rate limit
    # that looks like an empty search is worse than no rate limit. app.js also reads the 429
    # explicitly now and retries; this body is the fallback for anything that does not.
    resp = jsonify({"rows": [], "total": 0, "has_more": False, "limited": True,
                    "error": "Too many feed updates (%d in %ds). Retrying shortly."
                             % (cap, window)})
    resp.status_code = 429
    resp.headers["Retry-After"] = str(retry)
    return resp


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
    ok, msg = _save_profile(user, {"extra": json.dumps(extra)})
    analytics.set_optout(user, off)          # take effect now, not in five minutes
    flash("Usage recording is now %s for your account." % ("off" if off else "on")
          if ok else "Couldn't save that: " + msg[:120])
    return redirect(url_for("profile"))


# Rate-limited like the login route, and for the same reason: the CURRENT password is checked
# here, so an unattended session is otherwise an offline-free oracle for guessing it.
_PWCHANGE_TIERS = ((5, 300), (20, 3600))


@app.route("/profile/password", methods=["POST"])
@login_required
def profile_password():
    """Change your OWN password.

    The only password route in this app was /admin/user/password, so a user could not rotate
    their own credential after sharing it, typing it into the wrong window, or exposing it — and
    a user who forgot it was locked out until an admin intervened out of band, which with a
    single admin is a hard dependency on one person. It sat oddly beside /profile/revoke_token,
    whose reasoning applies verbatim: "Needing an admin to rotate a credential you leaked
    yourself is the kind of friction that means it doesn't get done."

    Forgot-password is still absent and needs an email sender this app does not have. An
    admin-issued reset remains the path for that; this covers everything short of it.
    """
    if not _check_csrf():
        flash("That form expired. Reload and try again.", "error")
        return redirect(url_for("profile"))
    user = session["user"]
    hit = _rate_hit(("pwchange", user), _PWCHANGE_TIERS)
    if hit:
        flash("Too many password attempts. Wait a few minutes and try again.", "error")
        return redirect(url_for("profile"))
    cur = request.form.get("current_password") or ""
    new = request.form.get("new_password") or ""
    again = request.form.get("confirm_password") or ""
    try:
        rec = db.get_user(user)
    except Exception:
        flash("Couldn't reach the database. Try again.", "error")
        return redirect(url_for("profile"))
    if not (rec and auth.verify_password(cur, rec.get("password_hash", ""))):
        flash("That isn't your current password.", "error")
        return redirect(url_for("profile"))
    if new != again:
        flash("The two new passwords don't match.", "error")
        return redirect(url_for("profile"))
    problem = auth.password_problem(new)
    if problem:
        flash(problem, "error")
        return redirect(url_for("profile"))
    if new == cur:
        flash("That is the password you already have.", "error")
        return redirect(url_for("profile"))
    try:
        db.set_user_password(user, auth.hash_password(new))
    except Exception as e:
        flash("Couldn't change your password: %s" % str(e)[:120], "error")
        return redirect(url_for("profile"))
    # The session stays signed in — you just proved you are you. The résumé cache is dropped for
    # the same reason the admin route drops it: it was populated at sign-in against the old login.
    _resume_cache.pop(user, None)
    flash("Password changed.", "ok")
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


def _uploaded_resume_text(field="resume_file", keep=None):
    """(text, error) for an uploaded résumé, or ('', '') when no file was attached.

    The upload is a CONVENIENCE over the textarea, never a replacement: every caller falls back
    to pasted text, because a scanned PDF has nothing to extract and no amount of parsing fixes
    that. Flask's MAX_CONTENT_LENGTH rejects an oversized body before it reaches here; the size
    check in core is the second line for anything that slips past it.

    `keep` is an out-dict that receives {filename, mime, raw} when supplied. f.read() is the only
    moment the original bytes exist in this app — everything downstream works on the extracted
    text — so a caller that wants to STORE the file has to be handed them right here or they are
    gone when the request ends.
    """
    try:
        f = request.files.get(field)
        if not f or not (f.filename or "").strip():
            return "", ""
        raw = f.read()
        if keep is not None:
            keep.update({"filename": os.path.basename(f.filename or ""),
                         "mime": f.mimetype or "", "raw": raw})
        return core.resume_text_from_upload(f.filename, raw)
    except Exception:
        return "", "Couldn't read that upload. Paste the text below instead."


_FILE_MIME = {"pdf": "application/pdf", "tex": "application/x-tex", "txt": "text/plain",
              "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}


def _store_resume_file(user, rid, keep):
    """Persist the uploaded bytes beside the résumé row. Best effort, never fatal.

    Base64 in a text column, not bytea: pgrest.jsonify() decodes any bytes it returns with
    .decode("utf-8", "replace"), so a bytea column would come back corrupted on read with no error
    anywhere. Same encoding the tailored-résumé cache already uses.

    Silent on failure by design — this table arrives with MIGRATION_resume_files.sql, and a user on
    an un-migrated database must still be able to upload and score a résumé.
    """
    if not (keep or {}).get("raw") or not rid:
        return
    ext = os.path.splitext(keep.get("filename") or "")[1].lower().lstrip(".")
    if ext not in db.RESUME_FILE_KINDS:
        return
    try:
        import base64
        db.save_resume_file(user, {
            "resume_id": rid, "kind": ext, "filename": keep.get("filename") or ("resume." + ext),
            "mime": keep.get("mime") or _FILE_MIME.get(ext, "application/octet-stream"),
            "b64": base64.b64encode(keep["raw"]).decode("ascii"), "size": len(keep["raw"])})
    except Exception:
        pass


def _extra(user, fresh=False):
    """The profile's `extra` jsonb as a dict, whatever shape it is stored in.

    Reads through _profile_row, i.e. the same 60 s per-worker cache the rest of web.py uses.
    This used to call db.get_profile directly, and _needs_onboarding calls it on EVERY feed
    render — so the fix at _profile_row (a duplicate profiles fetch worth ~100 ms of a 197 ms
    request) never reached the hottest caller of all.

    `fresh=True` forces a re-read, and every READ-MODIFY-WRITE caller must pass it: merging
    updates into a 60-second-old `extra` is exactly how the ev_off key _save_extra warns about
    gets dropped. Cheap reads are cached; writes are not.
    """
    try:
        if fresh:
            _profile_row_cache.pop(user, None)
        e = (_profile_row(user) or {}).get("extra")
        if isinstance(e, str):
            e = json.loads(e or "{}")
        return e if isinstance(e, dict) else {}
    except Exception:
        return {}


def _save_extra(user, updates):
    """MERGE into extra, never replace it. db.save_profile overwrites the whole jsonb value, so
    writing {'onboarded': True} on its own would silently drop ev_off (the analytics opt-out)."""
    e = _extra(user, fresh=True)          # read-modify-write: never merge into a cached copy
    e.update(updates)
    return _save_profile(user, {"extra": e})


# Usernames this worker has already established are NOT empty accounts. A LATCH, not a TTL
# cache, and the direction is the whole point: "does not need the wizard" is permanent (nothing
# un-sets a name, a saved search or a résumé), while "does need it" must stay fresh or a user
# who just finished setup gets bounced back into it by a worker holding a stale profile row.
# So the False answer is remembered forever and the True answer is never cached at all.
#
# This is what keeps _needs_onboarding off the profile table on every feed render without
# reintroducing the staleness _profile_row would: an established account — which is every
# account, almost always — costs one set lookup.
_onboarded_ok = set()


def _needs_onboarding(user):
    """A genuinely EMPTY account — no contact details, no saved search, no résumé.

    All three, not just contact details. Accounts predate this wizard, and plenty of them were
    used for months without anyone typing a name: testing contact fields alone would have
    ambushed a long-standing user with a setup flow for an app they already knew. Any one of
    the three is proof the account has been used.

    The flag is checked first so finishing or skipping is final.
    """
    if user in _onboarded_ok:
        return False
    # fresh=True, deliberately: this runs BEFORE the redirect decision, and it is the one read
    # in the request that must not be a minute old. It repopulates _profile_row_cache, so the
    # _profile_row call below and _user_prefs later in the same render share this one fetch.
    if _extra(user, fresh=True).get("onboarded"):
        _onboarded_ok.add(user)
        return False
    prof = _profile_row(user) or {}
    if any((prof.get(k) or "").strip()
           for k in ("first_name", "last_name", "name", "email", "phone")):
        _onboarded_ok.add(user)
        return False
    if prof.get("search_prefs"):                 # they have saved a search
        _onboarded_ok.add(user)
        return False
    try:
        if (current_profile() or "").strip():             # ...or a résumé / brain story
            _onboarded_ok.add(user)
            return False
        return True
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
    e = _extra(user, fresh=True)          # feeds _save_extra below: must not be stale

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
            _save_profile(user, {"search_prefs": core.normalize_prefs(
                dict(_user_prefs(user), roles=picked))})
            _rows_cache.clear()                # the first-paint count is derived from prefs
            answered = bool(picked) or f.get("all_roles") == "1"
        elif step == 3:
            payload = dict(SPONSORSHIP_ANSWERS.get(f.get("sponsorship") or "", {}))
            if payload:
                ok, msg = _save_profile(user, payload)
                if not ok:
                    flash("Couldn't save: " + msg[:120])
            answered = bool(payload)
        elif step == 4:
            loc = "" if f.get("anywhere") == "1" else (f.get("location") or "").strip()
            if loc or f.get("anywhere") == "1":
                ok, msg = _save_profile(user, {"location": loc[:120]})
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
        ok, msg = _save_profile(user, payload)
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


# The extension is side-loaded unpacked: no update_url, no Web Store listing, no stable id, so
# Chrome will never update it. What it CAN do is notice. EXT_MIN_VERSION is bumped by hand here
# whenever a change to the /api/ext/* contract makes an older build wrong; the popup compares it
# against its own manifest version and says so. Deliberately not a hard block -- an extension
# that refuses to work because a number moved is worse than one that fills a form imperfectly.
EXT_MIN_VERSION = "1.36.0"

# A build identifier the extension can show, so "which copy of the app am I talking to" is
# answerable without a deploy log. web.py's own mtime is the cheapest honest answer: the deploy
# mechanism is a zip extraction, which rewrites it.
def _app_build():
    try:
        return time.strftime("%Y-%m-%dT%H:%M",
                             time.gmtime(os.path.getmtime(os.path.abspath(__file__))))
    except Exception:
        return "unknown"


def _vtuple(v):
    """"1.35.0" -> (1, 35, 0), padded, so 1.9.0 sorts BELOW 1.35.0 rather than above it.
    String comparison gets that backwards and would tell half the installs they are current."""
    parts = [p for p in re.split(r"[^0-9]+", str(v or "")) if p != ""][:4]
    nums = [int(p) for p in parts] + [0] * (4 - len(parts))
    return tuple(nums[:4])


@app.route("/api/ext/version", methods=["GET", "OPTIONS"])
def ext_version():
    """What the app expects of the extension. UNAUTHENTICATED, deliberately.

    A stale extension may be stale precisely because its token contract moved, so requiring a
    valid token to discover that would hide the message from the installs that most need it.
    Nothing here is private: a build identifier and a version number the extension already has.
    """
    from flask import jsonify
    if request.method == "OPTIONS":
        return _cors(app.make_response(("", 204)))
    have = request.args.get("v", "")
    stale = bool(have) and _vtuple(have) < _vtuple(EXT_MIN_VERSION)
    # No update_url. There used to be one, pointing at <host>/extension, and three things were
    # wrong with it: no route serves that path so it 404'd, popup.js never read the field (it
    # shows `how`), and EXT_MIN_VERSION's own comment 40 lines up says the extension is
    # side-loaded unpacked with "no update_url" precisely because Chrome can never update it.
    # `how` is the whole answer, and it is the part that was already true.
    return _cors(jsonify({
        "ok": True,
        "min_version": EXT_MIN_VERSION,
        "app_build": _app_build(),
        "stale": stale,
        "how": ("Pull the repo and reload the extension at chrome://extensions."
                if stale else ""),
    }))


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


# Every ATS the scraper feeds (keep in sync with scraper/__init__.py detect_board + popup.js applyAts).
# workday/oracle/icims/greenhouse/lever/ashby/smartrecruiters have tuned filler.js adapters; the rest
# rely on the GENERIC adapter (best-effort). Workday/Oracle/iCIMS are account-walled multi-step
# wizards: the tab opens, you sign in, and the filler fills each step as you advance it.
# This list is no longer the gate for what may be queued — _queue_fillable admits any non-aggregator
# host — but it still marks the ones we can identify from the URL alone.
_FILLABLE_HOSTS = (
    "greenhouse.io", "lever.co", "ashbyhq.com", "smartrecruiters.com",
    "recruitee.com", "breezy.hr", "personio.com", "workable.com",
    "ultipro.com", "bamboohr.com", "pinpointhq.com", "rippling.com",
    "avature.net", "jobdiva.com", "myworkdayjobs.com", "myworkdaysite.com",
    "oraclecloud.com", "jibeapply.com", "icims.com", "successfactors.",
    "phenompeople.com", "jobvite.com",
    # Scrapers that existed in SOURCES but were never listed here, plus the platforms filler.js can
    # now fingerprint. amazon.jobs alone is 1,479 rows — the largest single host in the corpus.
    "amazon.jobs", "paylocity.com", "metacareers.com",
    "taleo.net", "brassring.com", "dayforcehcm.com", "workforcenow.adp.com",
    # SuccessFactors' RCM applicant portal — where the actual form is — runs on sapsf.com and
    # successfactors.EU as well as .com (verified live: career41.sapsf.com, career5.successfactors.eu),
    # so match the bare "successfactors." rather than the .com host only.
    "sapsf.com",
    # Salesforce Experience Cloud: Allegis (Actalent/TEKsystems/Aerotek) runs its whole apply flow
    # there. apply.actalentservices.com alone is 1,368 rows, the 2nd largest host in the corpus.
    "force.com", "my.site.com", "actalentservices.com",
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


def _queue_fillable(url, wide=True):
    """Is this a page the filler should open?

    The default is now YES for anything that isn't an aggregator, because of how the corpus is
    built: every URL here was produced by scraping an employer's own career board, so a row that
    isn't an aggregator redirect IS an application page by construction. Measured on the live
    snapshot, 5,750 of 19,529 jobs (29%) sit on employer vanity hostnames — careers.airbnb.com,
    jobs.sap.com, careers-inc.nttdata.com — that front a stock ATS. No host list can ever enumerate
    those, and the extension identifies the platform from the PAGE anyway (filler.js jmDetectAts),
    so gating them on a hostname match here only hid a third of the feed from the runner.

    `wide=False` restores the old behaviour — known ATS hosts only — for callers that want just the
    high-confidence pages.
    """
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
    # Employer career domains are IN by default (see _queue_fillable). `all=0` opts back down to the
    # tuned ATS host list; `all=1` is still accepted so older extension builds keep working.
    _all = (request.args.get("all") or "").strip().lower()
    wide = _all not in ("0", "false", "no", "off")
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
    # BOUND THE RECORD, NOT THE JSON. This used to truncate the serialised object at 9000
    # characters, which produces a line no parser can read — and `fields` carries up to 40 form
    # descriptors, so records really do exceed it. Every oversized entry silently corrupted the
    # log this endpoint exists to produce. Drop whole fields instead, and record that we did.
    line = json.dumps(rec)
    while len(line) > 9000 and rec["fields"]:
        rec["fields"] = rec["fields"][:len(rec["fields"]) // 2]
        rec["fields_truncated"] = True
        line = json.dumps(rec)
    if len(line) > 9000:                 # nothing left to drop: the scalars alone are too big
        for k in ("reason", "url", "company", "ai"):
            rec[k] = (rec.get(k) or "")[:60]
        rec["fields_truncated"] = True
        line = json.dumps(rec)
    try:
        with open("ext_debug_log.jsonl", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
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
        if blocked and db.is_blocked(company, blocked):
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
    of the page would never see. With add=true the found board is saved to the boards
    table and joins the next scrape.

    Body: {token, url, candidates?, add?, board_url?, ats?, name?}.
    Always answers with `error` set when it did not add, because it used not to.

    This route is the extension's half of /add-board and it did NOT get the hardening that
    page received on 2026-08-22, which cost the whole feature. Three rules it now shares:

      * A FALSY PROBE COUNT IS NOT A REASON TO SAY NOTHING. The guard was `if add and n`, so
        n=None (unreadable) and n=0 (reachable but empty) both skipped db.add_board and
        returned {"ok": true, "added": false} with no `error` key — the popup fell through to
        its generic "try the ➕ Add board page" for every failure it has. Reproduced against
        smurfitwestrockta.wd1: found=true, count=None, add silently dropped. Now None is
        refused with the reason and 0 is ADDED, exactly as /add-board treats them.
      * THE ADD CLICK MUST NOT RE-DETECT. Half the chain below is a live fetch, so the second
        click could miss a board the first click had found and report "couldn't add" for a
        board that is right there. It now pins board_url + ats from the check click and
        re-validates them through the URL rules only, which are offline and deterministic.
      * NEVER STORE detect_board's THIRD VALUE UNCHALLENGED. It is _name_from on the URL slug,
        which is how the boards table got "Wfscorp" for World Fuel Services and "Hdpc" for
        Goldman Sachs — names that match nothing in the filing data, so the postings carried
        no sponsorship signal at all. The Workday tenant `smurfitwestrockta` title-cases into
        "Smurfitwestrockta" and was about to be written verbatim.
    """
    import scraper
    from flask import jsonify
    if request.method == "OPTIONS":
        return _cors(app.make_response(("", 204)))
    data = request.get_json(silent=True) or {}
    user = _ext_user(data.get("token", ""))
    if not user:
        return _cors(jsonify({"ok": False, "error": "Invalid token"})), 401
    page = (data.get("url") or "").strip()
    want_add = bool(data.get("add"))
    typed = (data.get("name") or "").strip()[:200]

    det = None
    pinned, pinned_ats = (data.get("board_url") or "").strip(), (data.get("ats") or "").strip()
    if want_add and pinned and pinned_ats in scraper.SCRAPERS:
        # detect_board is pure URL rules — no network — so this re-validates the pinned URL
        # instead of taking the client's word for it, and still cannot flake. It returns None
        # for the probe-detected platforms (jibe, phenom, successfactors, paylocity), whose
        # normalized URL the check click already computed; keep those, but only once
        # is_http_url has cleared them. Everything reaching boards.url before this branch
        # existed came out of a detect_* function and so was a normalized https URL; a pinned
        # value is the first one a client supplies, and /companies renders it as an href.
        det = scraper.detect_board(pinned) or (
            (pinned, pinned_ats, "") if scraper.is_http_url(pinned) else None)
    if det is None and page:
        det = (scraper.detect_board(page) or scraper.detect_paylocity(page)
               or scraper.detect_jibe(page)
               or scraper.detect_phenom(page) or scraper.detect_successfactors(page)
               or scraper.detect_linked_ats(page) or scraper.detect_jsonld(page))
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
        return _cors(jsonify({"ok": True, "found": False,
                              "error": "No scrapeable board behind this site."}))
    burl, ats, guess = det
    if burl in {u for u, _, _ in scraper.SOURCES}:
        return _cors(jsonify({"ok": True, "found": True, "ats": ats, "name": guess,
                              "builtin": True,
                              "error": "%s is already scraped daily." % (guess or ats)}))
    try:
        n = scraper.probe_board(burl, ats)
    except Exception:
        n = None

    # Resolve the employer on whichever click got here, so the button the user reads names the
    # company rather than the tenant code, and a typed name is only ever asked for once.
    #
    # sluglike() gates the LOOKUP, never the answer: it is true for "Samsara" as well, where the
    # slug really IS the company, so re-testing its own result would demand a typed name for a
    # perfectly good board. Only a board that publishes nothing needs the user.
    #
    # `n is not None` first because board_display_name costs up to two HTTP requests and an
    # unreadable board is refused below regardless of what it calls itself.
    name, need_name = typed or guess, False
    if n is not None and not typed and scraper.name_is_sluglike(name, burl):
        resolved = scraper.board_display_name(burl, ats, timeout=8)
        if resolved:
            name = resolved
        else:
            need_name = True

    added, err = False, ""
    if want_add:
        if n is None:
            err = ("Detected a %s board but couldn't read a single posting from it, so it "
                   "would join the scrape and return nothing. Check the board URL." % ats)
        elif need_name:
            err = ("That's a readable %s board, but its URL only carries a tenant code and "
                   "the board won't say which employer it is. Type the company name." % ats)
        else:
            try:
                ok, msg = db.add_board(burl, ats, name, added_by=user)
                added, err = ok, ("" if ok else msg)
            except Exception as e:
                err = str(e)[:200]
    return _cors(jsonify({"ok": True, "found": True, "board_url": burl, "ats": ats,
                          "name": name, "count": n, "added": added,
                          "need_name": need_name, "error": err}))


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
        # REQUIRED, not belt-and-braces: update_job_fields moves neither half of
        # jobs_fingerprint(), so the stored score files would still look current while
        # holding scores computed before these descriptions existed.
        _scores_clear()
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
    visitors don't pay the cold-start re-import + cache refill. See docs/OPERATIONS.md.

    It keeps a worker ALIVE and that is all it can do — it builds nothing, so on its own it
    never removed a single millisecond from a first feed render. /warm below is the half that
    actually fills the caches."""
    return Response("ok", mimetype="text/plain")


# How long a /warm response is allowed to claim it did nothing. Purely cosmetic: the JSON
# reports per-stage milliseconds so a cron log says which stage is slow, not just that it ran.
@app.route("/warm")
def warm():
    """Build the caches that are the SAME for everybody, so the first real visitor doesn't.

    /healthz cannot do this. It has no user and touches no data, which docs/OPERATIONS.md
    records as the reason the keep-warm cron "prevents a cold worker, it cannot warm one".
    The per-user half is already solved by the stored score files (see user_scores); what was
    left was the shared half, and since _base_rows() made that a single corpus-wide build, one
    unauthenticated call can now do all of it:

        get_jobs()        the corpus, from the snapshot or the database
        sponsor_counts()  ~3.3 MB of filing counts
        visa_index()      ~2.9 MB of visa routes
        _logo_manifest()  the logo lookup
        _base_rows()      every card row except the score

    NO LOGIN, and deliberately not on `/`: OPERATIONS.md warns against pointing the cron at the
    feed because that needs a session and does per-user work. This does neither.

    GATED ON A SHARED SECRET, and 404 rather than 403 when it is unset or wrong. It is several
    seconds of CPU on a shared host, so an open URL would be a free way to pin a worker at
    100%; and answering 404 means an unconfigured deployment does not advertise that the route
    exists at all. Set WARM_TOKEN in .env and put the same value in the cron URL.
    """
    want = os.environ.get("WARM_TOKEN") or ""
    if not want or not hmac.compare_digest(request.args.get("t") or "", want):
        abort(404)
    out, t0 = {}, time.time()

    def _stage(name, fn):
        a = time.time()
        try:
            n = len(fn() or ())
        except Exception as e:                     # a warm-up must never be the thing that pages
            out[name] = {"error": str(e)[:120]}
            return
        out[name] = {"ms": int((time.time() - a) * 1000), "n": n}

    _stage("jobs", get_jobs)
    _stage("sponsor_counts", sponsor_counts)
    _stage("visa_index", visa_index)
    _stage("logo_manifest", lambda: _logo_manifest().get("ar") or {})
    _stage("base_rows", _base_rows)
    # THE PER-USER HALF, and it is the one that was actually hurting. Skippable with &users=0.
    if (request.args.get("users") or "1") != "0":
        a = time.time()
        out["users"] = _warm_user_scores()
        out["users"]["ms"] = int((time.time() - a) * 1000)
    out["total_ms"] = int((time.time() - t0) * 1000)
    return out


# Bounded on purpose: this is real CPU on a shared host, and an account list that grows without
# anyone noticing should degrade into "some users warmed" rather than into a timeout.
_WARM_USER_MAX = int(os.environ.get("WARM_USER_MAX") or 50)


def _warm_user_scores():
    """Write every live account's score file, so no user's first render pays the scoring pass.

    THIS IS THE POINT OF /warm, and until now it could not be done. The stored score files are
    keyed on (user, résumé md5) WITH the corpus fingerprint inside, so a scrape invalidates all
    of them — and the next person to open the feed paid a full pass over every row: measured at
    8.7 s before the scorer work and 2.1 s after, at 38,805 rows. Three scrapes a weekday plus
    Passenger recycling workers freely is why 46% of live feed renders were over two seconds.
    Reading a file instead is 0.08 s.

    Safe to call on every keep-warm tick, and that is deliberate: user_scores returns from the
    process dict or the stored file whenever the fingerprint still matches, so this costs almost
    nothing except in the one window it exists for — right after a scrape moved the corpus.

    `db.profile_text(u)` is EXACTLY what current_profile() would return for that user; the
    session only supplies the username. If that ever stops being true the md5 keys diverge and
    every file written here is ignored — silently, with no error and no slow path fixed. That
    equivalence is asserted in scripts/test_speed_caches.py.
    """
    out = {"accounts": 0, "computed": 0, "already_warm": 0, "no_resume": 0, "failed": 0}
    try:
        users = db.list_users() or []
    except Exception as e:
        return {"error": str(e)[:140]}
    # Reported, not assumed. An empty list is a real answer worth seeing in the cron log --
    # "db.list_users() returning nothing is not 'nothing to test'" is a lesson this repo has
    # already paid for once, and the same applies to "nothing to warm".
    out["accounts"] = len(users)
    fp = _jobs_cache.get("fp")
    for rec in users[:_WARM_USER_MAX]:
        u = ((rec or {}).get("username") or "").strip()
        if not u or (rec or {}).get("disabled_at"):
            continue
        try:
            _ensure_resume_migrated(u)
            txt = db.profile_text(u) or ""
            if not txt.strip():
                out["no_resume"] += 1        # no résumé means no meaningful score to precompute
                continue
            rmd5 = hashlib.md5(txt.encode("utf-8")).hexdigest()
            fresh = _scores_read(u, rmd5, fp) is None
            user_scores(u, txt)              # computes AND persists, or returns the stored copy
            out["computed" if fresh else "already_warm"] += 1
        except Exception:
            out["failed"] += 1               # one bad account must not stop the rest
    return out


# WSGI alias. cPanel's generated stub does `application = wsgi.<entry point>`, and its
# "Application Entry point" field defaults to `application` while Flask convention names the
# object `app` — which is an AttributeError at startup, not a 404 you can debug from the page.
# Exporting both names means the app starts whichever value that field happens to hold, and
# also satisfies any generic WSGI server that looks for `application`.
application = app

if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
