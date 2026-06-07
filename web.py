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
import time
import html
import hashlib
import functools
import subprocess

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
    "harvard university": "harvard.edu", "university of washington": "washington.edu",
    "university of wisconsin system": "wisc.edu", "northeastern university": "northeastern.edu",
    "rochester institute of technology": "rit.edu", "dana-farber cancer institute": "dana-farber.org",
    "h&r block": "hrblock.com", "scale ai": "scale.com", "notion": "notion.so",
    "new relic": "newrelic.com", "people tech group": "peopletech.com",
    "avery dennison": "averydennison.com", "s&p global": "spglobal.com",
    "toyota motor north america": "toyota.com", "university of south florida": "usf.edu",
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
        rows.append({"title": j.get("title", ""), "company": j.get("company", ""),
                     "location": j.get("location", ""), "url": u,
                     "sponsors_h1b": j.get("sponsors_h1b", ""),
                     "found_date": j.get("found_date", ""),
                     "score": scores.get(u, 0), "status": st})
    rows.sort(key=lambda r: r["score"], reverse=True)
    return render_template("feed.html", jobs=rows, has_resume=bool(resume),
                           total=len(rows), counts=counts, default_min=45 if resume else 0,
                           scraping=scrape_running())


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
        return {"ok": True, "status": status}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500


@app.route("/reload")
@login_required
def reload_jobs():
    get_jobs(force=True)
    _score_cache.clear()
    flash("Reloaded jobs from the database.")
    return redirect(url_for("feed"))


SCRAPE_LOCK = "scrape.lock"


def scrape_running():
    """True if a scrape was kicked off in the last 20 min (lock not yet cleared)."""
    try:
        return os.path.exists(SCRAPE_LOCK) and (time.time() - os.path.getmtime(SCRAPE_LOCK) < 1200)
    except Exception:
        return False


@app.route("/scrape", methods=["POST"])
@login_required
def scrape_now():
    """Kick off scraper.py + score_jobs.py in a DETACHED background process (survives
    this request) using the same venv Python. Guarded so two can't overlap."""
    if scrape_running():
        flash("A scrape is already running — check back in a few minutes, then hit 🔄 Reload.")
        return redirect(url_for("feed"))
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        with open(SCRAPE_LOCK, "w") as f:
            f.write(str(time.time()))
        cmd = ("import subprocess,sys,os;"
               "subprocess.run([sys.executable,'scraper.py']);"
               "subprocess.run([sys.executable,'score_jobs.py']);"
               "os.path.exists('scrape.lock') and os.remove('scrape.lock')")
        subprocess.Popen([sys.executable, "-c", cmd], cwd=here,
                         stdout=open(os.path.join(here, "scrape.log"), "a"),
                         stderr=subprocess.STDOUT, start_new_session=True)
        flash("🛰️ Scrape started in the background (~3–5 min). Hit 🔄 Reload when it's done.")
    except Exception as e:
        try:
            os.remove(SCRAPE_LOCK)
        except Exception:
            pass
        flash("Couldn't start the scrape: %s" % e)
    return redirect(url_for("feed"))


@app.route("/action", methods=["POST"])
@login_required
def action():
    url = request.form.get("url", "")
    status = request.form.get("status", "")          # liked|hidden|applied|'' (clear)
    try:
        db.set_user_status(session["user"], url, status)
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


# ----------------------------- tailor (keyword gaps) -----------------------------
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
                           have=have, missing=missing, has_resume=bool(resume))


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


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
