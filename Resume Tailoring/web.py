"""
web.py — Flask UI for the Resume Brain (standalone; cPanel/Passenger-ready).

Thin UI layer: routes read inputs, call brain/store/match, and render. All thinking lives in
brain.py + match.py (deterministic, self-training — NO AI). Single user, no login.

Run locally:   python web.py        (http://127.0.0.1:5055)
On cPanel:     passenger_wsgi.py exposes `application = web.app`
"""
import os
import hashlib
import platform
import secrets

from flask import (Flask, request, session, redirect, url_for,
                   render_template, flash, g, Response)

import brain
import store
import gemini
import export

app = Flask(__name__)


def _fallback_secret():
    seed = "resume-brain|%s|%s" % (platform.node(), os.path.abspath(__file__))
    return hashlib.sha256(seed.encode()).hexdigest()


app.secret_key = os.environ.get("APP_SECRET") or _fallback_secret()
app.permanent_session_lifetime = 60 * 60 * 24 * 30
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "").lower() in ("1", "true", "yes"),
)


@app.before_request
def _csp_nonce():
    g.csp_nonce = secrets.token_urlsafe(16)


_CSP_TEMPLATE = (
    "default-src 'self'; "
    "script-src 'self' 'nonce-%s'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self'; form-action 'self'; object-src 'none'; "
    "frame-ancestors 'none'; base-uri 'none'"
)


@app.after_request
def _security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Content-Security-Policy", _CSP_TEMPLATE % getattr(g, "csp_nonce", ""))
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if request.is_secure or request.headers.get("X-Forwarded-Proto", "").lower() == "https":
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return resp


def _csv(s):
    return [x.strip() for x in (s or "").replace("\n", ",").split(",") if x.strip()]


def _gemini_key():
    """Optional AI key — from the (signed, HttpOnly) session cookie or env. Never written to disk."""
    return (session.get("gkey") or os.environ.get("GEMINI_API_KEY") or "").strip()


# ----------------------------- tailor -----------------------------
@app.route("/")
def index():
    return redirect(url_for("tailor"))


@app.route("/tailor", methods=["GET", "POST"])
def tailor():
    inputs = {"company": "", "company_url": "", "job_url": "", "jd": ""}
    data = None
    if request.method == "POST":
        inputs = {
            "company": (request.form.get("company") or "").strip(),
            "company_url": (request.form.get("company_url") or "").strip(),
            "job_url": (request.form.get("job_url") or "").strip(),
            "jd": (request.form.get("jd") or "").strip(),
        }
        data = brain.run_tailor(jd_text=inputs["jd"], job_url=inputs["job_url"],
                                company_name=inputs["company"], company_url=inputs["company_url"],
                                force_research=(request.form.get("refresh") == "1"))
    return render_template("tailor.html", inputs=inputs, data=data,
                           have_resumes=bool(store.list_resumes()),
                           have_key=bool(_gemini_key()))


@app.route("/rewrite", methods=["POST"])
def rewrite():
    """OPTIONAL AI layer: re-derive the deterministic plan, then have Gemini write the finished
    tailored résumé + cover letter. Requires a key (session or env)."""
    posted_key = (request.form.get("gkey") or "").strip()
    if posted_key:
        session.permanent = True
        session["gkey"] = posted_key
    key = _gemini_key()
    inputs = {"company": (request.form.get("company") or "").strip(),
              "company_url": (request.form.get("company_url") or "").strip(),
              "job_url": (request.form.get("job_url") or "").strip(),
              "jd": (request.form.get("jd") or "").strip()}
    if not key:
        flash("Add your Gemini API key to use AI rewrite (it stays in your session only).")
        return render_template("tailor.html", inputs=inputs, data=None,
                               have_resumes=bool(store.list_resumes()), have_key=False)

    data = brain.run_tailor(jd_text=inputs["jd"], job_url=inputs["job_url"],
                            company_name=inputs["company"], company_url=inputs["company_url"],
                            record=False)
    ctx = brain.build_rewrite_context(data, request.form.getlist("story_ids"))
    if not ctx:
        flash("Nothing to rewrite — add a job description and a résumé first.")
        return redirect(url_for("tailor"))
    error, out = "", None
    try:
        out = gemini.rewrite(ctx, key)
    except Exception as e:                       # API/network/key failure — show it, don't crash
        error = str(e)
    return render_template("rewrite.html", out=out, error=error, ctx=ctx, inputs=inputs)


def _docx_response(text, title, filename):
    try:
        body = export.build_docx(text, title)
        return Response(body, mimetype=export.DOCX_MIME,
                        headers={"Content-Disposition": 'attachment; filename="%s.docx"' % filename})
    except ImportError:
        return Response(text or "", mimetype="text/plain; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="%s.txt"' % filename})


@app.route("/export/resume.docx", methods=["POST"])
def export_resume():
    return _docx_response(request.form.get("content", ""),
                          request.form.get("title", "Tailored Résumé"), "Tailored_Resume")


@app.route("/export/cover.docx", methods=["POST"])
def export_cover():
    return _docx_response(request.form.get("content", ""), "", "Cover_Letter")


@app.route("/feedback", methods=["POST"])
def feedback():
    jd_terms = _csv(request.form.get("jd_terms"))
    story_ids = request.form.getlist("story_ids")
    brain.apply_feedback(jd_terms, story_ids, request.form.get("feedback", ""),
                         request.form.get("company", ""))
    flash("Learned ✓ The brain will weight these for similar jobs from now on.")
    return redirect(url_for("tailor"))


# ----------------------------- teach (the knowledge base) -----------------------------
@app.route("/teach")
def teach():
    return render_template("teach.html", profile=store.get_profile(),
                           resumes=store.list_resumes(), stories=store.list_stories(),
                           lessons=store.list_lessons())


@app.route("/profile/save", methods=["POST"])
def profile_save():
    store.save_profile({"name": request.form.get("name", ""), "email": request.form.get("email", "")})
    flash("Saved ✓")
    return redirect(url_for("teach"))


@app.route("/resume/save", methods=["POST"])
def resume_save():
    store.save_resume({"id": request.form.get("id", ""),
                       "name": (request.form.get("name") or "Untitled résumé").strip(),
                       "tags": _csv(request.form.get("tags")),
                       "content": request.form.get("content", "")})
    flash("Résumé saved ✓")
    return redirect(url_for("teach"))


@app.route("/resume/delete", methods=["POST"])
def resume_delete():
    store.delete_resume(request.form.get("id", ""))
    flash("Résumé deleted")
    return redirect(url_for("teach"))


@app.route("/story/save", methods=["POST"])
def story_save():
    store.save_story({"id": request.form.get("id", ""),
                      "title": (request.form.get("title") or "Untitled story").strip(),
                      "tags": _csv(request.form.get("tags")),
                      "skills": _csv(request.form.get("skills")),
                      "text": request.form.get("text", "")})
    flash("Story saved ✓")
    return redirect(url_for("teach"))


@app.route("/story/delete", methods=["POST"])
def story_delete():
    store.delete_story(request.form.get("id", ""))
    flash("Story deleted")
    return redirect(url_for("teach"))


@app.route("/lesson/save", methods=["POST"])
def lesson_save():
    store.save_lesson({"text": (request.form.get("text") or "").strip(),
                       "triggers": _csv(request.form.get("triggers")),
                       "boost_story_ids": [], "boost_terms": _csv(request.form.get("boost_terms")),
                       "weight": 1.0, "source": "manual"})
    flash("Lesson saved ✓")
    return redirect(url_for("teach"))


@app.route("/lesson/delete", methods=["POST"])
def lesson_delete():
    store.delete_lesson(request.form.get("id", ""))
    flash("Lesson deleted")
    return redirect(url_for("teach"))


# ----------------------------- companies (accumulated research) -----------------------------
@app.route("/companies")
def companies():
    c = store.list_companies()
    items = sorted(c.values(), key=lambda x: x.get("fetched_at", ""), reverse=True)
    return render_template("companies.html", companies=items)


if __name__ == "__main__":
    app.run(debug=True, use_reloader=False, port=int(os.environ.get("PORT", 5055)))
