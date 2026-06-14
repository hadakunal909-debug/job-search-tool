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
import functools

import requests

from flask import (Flask, request, session, redirect, url_for,
                   render_template, flash, g)

import core
import db
import auth
import scraper

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
    return resp


# ----------------------------- caches -----------------------------
_jobs_cache = {"rows": None, "at": 0}
_score_cache = {}            # (username, resume_md5) -> {url: score}
_sponsor_cache = {}          # url -> (verdict, reason) read from the JD (same for everyone)
_SPONSOR_COUNTS = core.load_sponsor_counts()      # {} until sponsor_counts.json is built
_EVERIFY_INDEX = core.load_everify()              # None until everify.txt is built
_resume_cache = {}           # username -> (resume_text, fetched_at)
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


def sponsor_signal(job):
    """(verdict, reason) for a job's JD sponsorship signal, computed once per URL."""
    u = job.get("url") or ""
    if u not in _sponsor_cache:
        _sponsor_cache[u] = core.sponsorship_from_jd(job.get("jd") or "")
    return _sponsor_cache[u]


def get_jobs(force=False):
    """All jobs from Supabase, cached ~5 min (or force-reloaded)."""
    if force or _jobs_cache["rows"] is None or time.time() - _jobs_cache["at"] > 300:
        try:
            _jobs_cache["rows"] = db.load_jobs() or []
        except Exception:
            _jobs_cache["rows"] = _jobs_cache["rows"] or []
        _jobs_cache["at"] = time.time()
    return _jobs_cache["rows"]


def user_scores(username, resume):
    """{url: match%} for this user. Scores each job's stored JD against the résumé
    (core.skill_match); falls back to the precomputed baseline when no résumé/JD."""
    key = (username, hashlib.md5((resume or "").encode("utf-8")).hexdigest())
    if key in _score_cache:
        return _score_cache[key]
    idf = core.load_idf()
    scores = {}
    for j in get_jobs():
        u = j.get("url")
        if not u:
            continue
        jd = j.get("jd") or ""
        if resume and jd:
            try:
                scores[u] = int(core.skill_match(resume, jd, idf)[0])
            except Exception:
                scores[u] = 0
        else:
            try:
                scores[u] = int(j.get("match_score") or 0)
            except Exception:
                scores[u] = 0
    _score_cache[key] = scores
    return scores


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
    """Render EVERY job once with data-* attributes; the client-side JS (static/app.js)
    does tab/search/min-match filtering + actions with no page reloads. Falls back to
    plain server rendering when JS is off (all cards just show)."""
    user = session["user"]
    resume = current_resume()
    scores = user_scores(user, resume)
    try:
        statuses = db.get_user_statuses(user)
    except Exception:
        statuses = {}

    rows = []
    counts = {"liked": 0, "applied": 0, "hidden": 0}
    for j in get_jobs():
        u = j.get("url")
        st = statuses.get(u, "")
        if st in counts:
            counts[st] += 1
        sv, sreason = sponsor_signal(j)
        strength, scount = core.sponsor_strength(j.get("company", ""), _SPONSOR_COUNTS)
        exp_y = core.experience_min_years(j.get("jd") or "")
        rows.append({"title": j.get("title", ""), "company": j.get("company", ""),
                     "location": j.get("location", ""), "url": u,
                     # safe value for the Apply href; the raw `url` stays the action key.
                     "apply_url": u if (u or "").startswith(("http://", "https://")) else "#",
                     "sponsors_h1b": j.get("sponsors_h1b", ""),
                     "found_date": j.get("found_date", ""),
                     "score": scores.get(u, 0), "status": st,
                     "sponsor_jd": sv, "sponsor_reason": sreason,
                     "cap_exempt": core.is_cap_exempt(j.get("company", "")),
                     "everify": core.is_everify(j.get("company", ""), _EVERIFY_INDEX),
                     "exp_years": exp_y if exp_y is not None else "",
                     "exp_level": core.experience_level(j.get("jd") or ""),
                     "strength": strength, "strength_n": scount})
    rows.sort(key=lambda r: r["score"], reverse=True)
    return render_template("feed.html", jobs=rows, has_resume=bool(resume),
                           total=len(rows), counts=counts, default_min=45 if resume else 0,
                           scraping=False)


@app.route("/api/job")
@login_required
def api_job():
    """Full job detail for the slide-in panel: JD text + matched/missing skills."""
    url = request.args.get("url", "")
    job = next((j for j in get_jobs() if j.get("url") == url), None)
    if not job:
        return {"ok": False}, 404
    resume = current_resume()
    jd = job.get("jd", "") or ""
    if resume and jd:
        score, have, missing = core.skill_match(resume, jd, core.load_idf())
    else:
        try:
            score = int(job.get("match_score") or 0)
        except Exception:
            score = 0
        have, missing = [], []
    sv, sreason = core.sponsorship_from_jd(jd)
    exp_y = core.experience_min_years(jd)
    return {"ok": True, "title": job.get("title", ""), "company": job.get("company", ""),
            "location": job.get("location", ""), "date": (job.get("found_date") or "")[:10],
            "url": url, "sponsors_h1b": job.get("sponsors_h1b", ""), "score": int(score or 0),
            "sponsor_jd": sv, "sponsor_reason": sreason,
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
    _sponsor_cache.clear()
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
    Needs GH_TOKEN in .env (a fine-grained PAT with Actions: read+write)."""
    gh = _trigger_github_action()
    if gh is None:
        flash("To enable this button, add GH_TOKEN to .env (a GitHub token with Actions "
              "read+write). You can also run the scrape from the repo's Actions tab, or via cron.")
    elif gh[0]:
        flash("🛰️ Scrape started on GitHub Actions (~3–5 min). Watch the Actions tab, then hit 🔄 Reload.")
    else:
        flash("Couldn't start the GitHub Action — " + gh[1])
    return redirect(url_for("feed"))


@app.route("/action", methods=["POST"])
@login_required
def action():
    url = request.form.get("url", "")
    status = request.form.get("status", "")          # liked|hidden|applied|'' (clear)
    try:
        db.set_user_status(session["user"], url, status)
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
            flash("Saved ✓ Your match scores now reflect this résumé.")
        except Exception:
            flash("Couldn't save — try again.")
        return redirect(url_for("resume"))
    return render_template("resume.html", resume=current_resume())


# ----------------------------- tailor (keyword gaps + optional AI) -----------------------------
def _ai_key_for(_user=None):
    """The Gemini key to use: the one saved in THIS user's session (a signed, HttpOnly
    cookie that lasts ~30 days, so it survives app restarts), else GEMINI_API_KEY from
    the server env. Enter it once — no need to retype it every time."""
    return session.get("gemini_key") or os.environ.get("GEMINI_API_KEY")


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
    if resume and (job.get("jd") or ""):
        score, have, missing = core.skill_match(resume, job.get("jd", ""), core.load_idf())
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
    jd = job.get("jd", "") or ""
    if resume and jd:
        score, have, missing = core.skill_match(resume, jd, core.load_idf())
    else:
        score, have, missing = job.get("match_score") or 0, [], []
    tailored, ai_err = "", ""
    if not resume:
        ai_err = "Add your résumé first (📄 My résumé), then tailor it here."
    elif not jd:
        ai_err = "No job description stored for this role yet — open Apply to read it on the company site."
    elif not key:
        ai_err = "Paste a Google Gemini API key below (or set GEMINI_API_KEY on the server) to enable AI tailoring."
    else:
        try:
            tailored = core.tailor_with_gemini(resume, jd, api_key=key)
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
        return {"ok": False, "error": "Add your résumé first (📄 My résumé), then tailor it here."}
    if not jd:
        return {"ok": False, "error": "No job description is stored for this role yet — open Apply to read it on the company site."}
    if not key:
        return {"ok": False, "error": "Add a Google Gemini API key (the field below, or GEMINI_API_KEY on the server)."}
    try:
        return {"ok": True, "tailored": core.tailor_with_gemini(resume, jd, api_key=key)}
    except Exception as e:
        return {"ok": False, "error": "Tailoring failed: %s" % str(e)[:250]}


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
        flash("Saved ✓")
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
        ok, msg = db.save_profile(user, {k: f.get(k, "").strip() for k in
            ("name", "email", "phone", "location", "linkedin",
             "work_authorized", "needs_sponsorship", "default_resume", "notes")})
        flash("Saved ✓" if ok else ("Couldn't save — " + msg[:120]))
        return redirect(url_for("profile"))
    try:
        prof = db.get_profile(user) or {}
    except Exception:
        prof = {}
    return render_template("profile.html", prof=prof, token=_ext_token(user))


@app.route("/api/ext/save", methods=["POST", "OPTIONS"])
def ext_save():
    """Extension -> log a job to the tracker. Token-authenticated; CORS-open (the token
    is the secret). Deduped by URL."""
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


@app.route("/api/ext/bulk_jobs", methods=["POST", "OPTIONS"])
def ext_bulk_jobs():
    """Extension -> bulk-add postings READ FROM A PAGE in the user's own browser into the
    shared jobs feed. This is how we get jobs from sites that block server-side scraping
    (e.g. Tesla's Akamai bot-wall): the user's real, already-trusted browser can read the
    listings the page loaded, so the extension hands them to us. We apply the SAME
    title + US-location filter as the scraper, flag H1B sponsors, and de-dupe by URL —
    so a bulk import looks identical to a scraped board in the feed."""
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
        seen = db.existing_urls()
    except Exception:
        seen = set()

    kept, scanned = [], 0
    # Tally WHY jobs were dropped — when an import adds 0, this is the diagnosis.
    dropped = {"dup": 0, "title": 0, "us": 0, "bad": 0}
    # Already-imported jobs that still have NO stored description: re-running an import
    # returns them as needs_jd so the extension can backfill their JDs (a first import
    # may have failed mid-fetch, or predates JD support).
    no_jd = {j.get("url") for j in get_jobs() if not (j.get("jd") or "")}
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
        if url in seen:
            dropped["dup"] += 1
            if url in no_jd and len(needs_jd) < 25:
                needs_jd.append(url)
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
    ➕ Add board over the page URL plus candidates collected from the LIVE DOM (iframe
    srcs + ATS-host links) — which catches JS-injected embeds that a server-side fetch
    of the page would never see. Body: {token, url, candidates?, add?}. With add=true
    the found board is saved to the boards table and joins the next scrape."""
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
    return _cors(jsonify({"ok": True, "stored": len(clean), "patched": len(patches),
                          "removed_nonus": len(removed)}))


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
