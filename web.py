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
import hashlib
import functools

import requests

from flask import (Flask, request, session, redirect, url_for,
                   render_template, flash)

import core
import db
import auth
import scraper

app = Flask(__name__)
# Session signing key: explicit APP_SECRET env var, else derived from the Supabase key,
# else a dev fallback. Stable across restarts so logins persist.
app.secret_key = (os.environ.get("APP_SECRET")
                  or hashlib.sha256((db._creds()[1] or "dev-secret").encode()).hexdigest())
app.permanent_session_lifetime = 60 * 60 * 24 * 30      # 30-day login


# ----------------------------- caches -----------------------------
_jobs_cache = {"rows": None, "at": 0}
_score_cache = {}            # (username, resume_md5) -> {url: score}
_sponsor_cache = {}          # url -> (verdict, reason) read from the JD (same for everyone)
_SPONSOR_COUNTS = core.load_sponsor_counts()      # {} until sponsor_counts.json is built


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
    return {"current_user": session.get("user")}


# --- company logo helpers (Clearbit logo by domain, with a letter-avatar fallback) ---
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
@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user"):
        return redirect(url_for("feed"))
    if request.method == "POST":
        u = (request.form.get("username") or "").strip()
        p = request.form.get("password") or ""
        rec = None
        try:
            rec = db.get_user(u)
        except Exception:
            flash("Couldn't reach the database. Try again.")
        if rec and auth.verify_password(p, rec.get("password_hash", "")):
            session.permanent = True
            session["user"] = u
            session["resume"] = rec.get("resume", "") or ""
            return redirect(request.args.get("next") or url_for("feed"))
        if rec is not None:
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
    resume = session.get("resume", "")
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
        rows.append({"title": j.get("title", ""), "company": j.get("company", ""),
                     "location": j.get("location", ""), "url": u,
                     "sponsors_h1b": j.get("sponsors_h1b", ""),
                     "found_date": j.get("found_date", ""),
                     "score": scores.get(u, 0), "status": st,
                     "sponsor_jd": sv, "sponsor_reason": sreason,
                     "cap_exempt": core.is_cap_exempt(j.get("company", "")),
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
    resume = session.get("resume", "")
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
    return {"ok": True, "title": job.get("title", ""), "company": job.get("company", ""),
            "location": job.get("location", ""), "date": (job.get("found_date") or "")[:10],
            "url": url, "sponsors_h1b": job.get("sponsors_h1b", ""), "score": int(score or 0),
            "sponsor_jd": sv, "sponsor_reason": sreason,
            "cap_exempt": core.is_cap_exempt(job.get("company", "")),
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
            session["resume"] = txt
            _score_cache.clear()
            flash("Saved ✓ Your match scores now reflect this résumé.")
        except Exception:
            flash("Couldn't save — try again.")
        return redirect(url_for("resume"))
    return render_template("resume.html", resume=session.get("resume", ""))


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
    resume = session.get("resume", "")
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
    resume = session.get("resume", "")
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
    resume = session.get("resume", "")
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
                   or scraper.detect_jsonld(url))
            if not det:
                result = ("err", "That isn't a readable job board (Greenhouse, Lever, Ashby, "
                          "SmartRecruiters, Workday, iCIMS/Jibe, Recruitee, Breezy, Personio, or "
                          "a page with embedded job data). Add the company to sponsors.txt instead.")
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


def _autolog_application(user, url):
    """When a feed job is marked 'applied', record it in the tracker (deduped by url),
    capturing the résumé currently on file. Never raises."""
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
            "source": "JobMatch", "resume_used": session.get("resume", "") or "", "notes": ""})
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
    return render_template("applications.html", apps=apps, statuses=APP_STATUSES,
                           counts=counts, total=len(apps), week=week, sql=db.APPLICATIONS_SQL)


@app.route("/application/save", methods=["POST"])
@login_required
def application_save():
    f = request.form
    rec = {"id": f.get("id", "").strip(), "company": f.get("company", "").strip(),
           "title": f.get("title", "").strip(), "url": f.get("url", "").strip(),
           "status": f.get("status", "applied"), "applied_date": f.get("applied_date", "").strip(),
           "source": (f.get("source", "").strip() or "Other"), "notes": f.get("notes", "").strip()}
    if not rec["company"] and not rec["title"]:
        flash("Add at least a company or a title.")
        return redirect(url_for("applications"))
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
    w.writerow(["company", "title", "status", "applied_date", "source", "url", "notes"])
    for a in apps:
        w.writerow([a.get("company", ""), a.get("title", ""), a.get("status", ""),
                    a.get("applied_date", "") or "", a.get("source", ""),
                    a.get("url", ""), a.get("notes", "")])
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


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
