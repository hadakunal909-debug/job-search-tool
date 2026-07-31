#!/usr/bin/env python3
"""
notify.py — email each user a digest of the new jobs that match THEIR saved search.

Reads last_new_jobs.json (written by the scraper) so it only ever mentions this run's new
jobs, scores them against each user's own résumé/profile text, filters them through that
user's saved search (core.prefs_match), and sends one email per user.

WHY PER-USER: this used to send one identical digest to a single ALERT_TO address, which is
useless on a multi-user app — and the app had no other reason for anyone to come back. A
digest is only worth opening if it reflects what that person actually asked for.

OPT-IN: a user gets email only when their profile has search_prefs.alerts == "daily" AND an
email address. Nobody is mailed by default.

DORMANT BY DESIGN: with no SMTP_* configured this prints what it would do and exits 0, so it
never breaks the scrape. To turn it on (env vars / GitHub secrets):
    SMTP_HOST, SMTP_USER, SMTP_PASS            (required)
    SMTP_PORT (default 587), ALERT_FROM (default SMTP_USER)
    ALERT_DRY_RUN=1                            build + print digests, send nothing
    ALERT_TO                                   legacy single-recipient fallback, used only
                                               when no user has opted in
Your cPanel host's mailbox works (SMTP_HOST = mail.<yourdomain>, port 465 or 587).

Run:  python -m scraper.notify [--dry-run]
"""
import os
import sys
import json
import ssl
import smtplib
import html as _html
from email.mime.text import MIMEText

import core
import db

# Force UTF-8 stdout (Windows cp1252 consoles crash on em dashes / accents in titles), but
# only when stdout actually supports it: under Passenger and some cron wrappers sys.stdout is
# a substitute object with no reconfigure(), and an unguarded call there is an AttributeError
# at import. Same guard as scraper/__init__.py and score_jobs.py.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

NEW_FILE = "last_new_jobs.json"
MAX_ROWS_PER_EMAIL = 40          # a digest nobody scrolls is a digest nobody reads


def _esc(s):
    return _html.escape(s or "")


def _smtp_config():
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASS")
    if not (host and user and pw):
        return None
    return {"host": host, "user": user, "pw": pw,
            "port": int(os.environ.get("SMTP_PORT", "587") or 587),
            "from": os.environ.get("ALERT_FROM") or user}


def _send(cfg, to, subject, html):
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = cfg["from"]
    msg["To"] = to
    ctx = ssl.create_default_context()
    if cfg["port"] == 465:
        with smtplib.SMTP_SSL(cfg["host"], cfg["port"], context=ctx, timeout=30) as s:
            s.login(cfg["user"], cfg["pw"])
            s.sendmail(cfg["from"], [to], msg.as_string())
    else:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=30) as s:
            s.starttls(context=ctx)
            s.login(cfg["user"], cfg["pw"])
            s.sendmail(cfg["from"], [to], msg.as_string())


def render_digest(rows, prefs, app_url=""):
    """The email body. Each row is a core.digest_row dict with a score."""
    def chips(r):
        out = []
        if r.get("salary_label"):
            out.append(r["salary_label"])
        if r.get("remote"):
            out.append("Remote")
        if r.get("sponsors_h1b") == "yes":
            out.append("H1B")
        if r.get("cap_exempt"):
            out.append("no lottery")
        if r.get("everify"):
            out.append("E-Verify")
        if r.get("exp_years") not in ("", None):
            out.append("%s+ yrs" % r["exp_years"])
        return " &middot; ".join(_esc(c) for c in out)

    items = "".join(
        "<tr>"
        "<td align='right' valign='top' style='padding:6px 8px;font-weight:700'>%d%%</td>"
        "<td style='padding:6px 8px'>"
        "<a href='%s' style='color:#0e8a5f;text-decoration:none;font-weight:600'>%s</a><br>"
        "<span style='color:#667;font-size:12px'>%s &middot; %s</span>"
        "%s</td></tr>"
        % (r["score"], _esc(r["url"]), _esc(r["title"]), _esc(r["company"]),
           _esc(r["location"] or "n/a"),
           ("<br><span style='color:#667;font-size:12px'>%s</span>" % chips(r)) if chips(r) else "")
        for r in rows)

    where = prefs.get("loc") or "anywhere"
    floor = prefs.get("alert_min") or prefs.get("min") or 0
    return (
        "<div style=\"font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:640px\">"
        "<h2 style='margin:0 0 4px'>%d new match%s</h2>"
        "<p style='color:#667;font-size:13px;margin:0 0 14px'>"
        "Matching your saved search: %s, %d%%+ match.</p>"
        "<table cellpadding='0' cellspacing='0' style='border-collapse:collapse;width:100%%'>%s</table>"
        "%s"
        "<p style='color:#889;font-size:12px;margin-top:18px'>"
        "Sorted by how well each one matches your résumé. Change what you get by updating "
        "your default search in the app, or set alerts to Off on your profile.</p>"
        "</div>"
        % (len(rows), "" if len(rows) == 1 else "es", _esc(where), floor, items,
           ("<p style='color:#667;font-size:12px'>Showing the top %d.</p>" % MAX_ROWS_PER_EMAIL)
           if len(rows) >= MAX_ROWS_PER_EMAIL else "",
           ))


def _score_for(user_text_low, job, idf, fallback):
    """This user's match % for a job. Falls back to the stored baseline when we can't
    analyse the JD (no description yet, or the user has no résumé on file)."""
    jd = job.get("jd") or ""
    if not (user_text_low and jd):
        return fallback
    try:
        m = core.job_meta(jd, idf)
        if m["analyzed"].get("thin"):
            return 0
        return core.score_against(user_text_low, m["analyzed"])[0]
    except Exception:
        return fallback


def recipients():
    """[(username, email, prefs)] for users who opted in and have somewhere to send to."""
    out = []
    try:
        users = db.list_users() or []
    except Exception as e:
        print("Could not list users: %s" % str(e)[:120])
        return out
    for u in users:
        name = u.get("username")
        if not name:
            continue
        try:
            prof = db.get_profile(name) or {}
        except Exception:
            prof = {}
        prefs = core.normalize_prefs(prof.get("search_prefs"))
        email = (prof.get("email") or "").strip()
        if prefs.get("alerts") != "daily":
            continue
        if not email or "@" not in email:
            print("  %s opted in but has no email on their profile — skipped." % name)
            continue
        out.append((name, email, prefs))
    return out


def main():
    dry = "--dry-run" in sys.argv or os.environ.get("ALERT_DRY_RUN", "") in ("1", "true", "yes")
    cfg = _smtp_config()
    if not cfg and not dry:
        print("Alerts not configured (set SMTP_HOST/SMTP_USER/SMTP_PASS) - skipping.")
        return

    try:
        new = json.load(open(NEW_FILE, encoding="utf-8"))
    except Exception:
        new = []
    if not new:
        print("No new jobs this run — nothing to send.")
        return

    # Full rows (with jd) for scoring; last_new_jobs.json only carries the 6 scraped fields.
    try:
        by_url = {j.get("url"): j for j in (db.load_jobs() or []) if j.get("url")}
    except Exception as e:
        print("Could not load jobs: %s" % str(e)[:120])
        return
    jobs = [by_url.get(r.get("url")) or r for r in new]
    idf = core.load_idf()
    everify = core.load_everify()

    people = recipients()
    if not people:
        legacy = (os.environ.get("ALERT_TO") or "").strip()
        if not legacy:
            print("No users have opted into email alerts (profile -> alerts = daily). "
                  "Nothing to send.")
            return
        # Keep a pre-existing single-recipient setup working rather than silently going quiet.
        print("No opted-in users; falling back to legacy ALERT_TO=%s" % legacy)
        people = [(None, legacy, core.normalize_prefs(
            {"min": os.environ.get("ALERT_MIN_SCORE", "0"), "hideagency": False}))]

    print("%d new job(s) this run · %d recipient(s)" % (len(jobs), len(people)))
    sent = 0
    for username, email, prefs in people:
        text_low = ""
        if username:
            try:
                text_low = (db.profile_text(username) or "").lower()
            except Exception:
                text_low = ""
        rows = []
        for job in jobs:
            try:
                base = int(job.get("match_score") or 0)
            except (TypeError, ValueError):
                base = 0
            score = _score_for(text_low, job, idf, base)
            row = core.digest_row(job, score, everify)
            if core.prefs_match(row, prefs):
                rows.append(row)
        rows.sort(key=lambda r: -r["score"])
        rows = rows[:MAX_ROWS_PER_EMAIL]

        who = username or email
        if not rows:
            print("  %-16s 0 of %d matched their search — no email." % (who, len(jobs)))
            continue
        html = render_digest(rows, prefs)
        subject = "%d new match%s for your job search" % (len(rows), "" if len(rows) == 1 else "es")
        if dry or not cfg:
            print("  %-16s WOULD SEND to %s: %s" % (who, email, subject))
            for r in rows[:5]:
                print("        %3d%%  %-44s %-22s %s"
                      % (r["score"], r["title"][:44], r["company"][:22], r["location"][:26]))
            if len(rows) > 5:
                print("        ... and %d more" % (len(rows) - 5))
            continue
        try:
            _send(cfg, email, subject, html)
            sent += 1
            print("  %-16s sent %d job(s) to %s" % (who, len(rows), email))
        except Exception as e:
            print("  %-16s SEND FAILED (%s)" % (who, str(e)[:120]))

    if dry:
        print("DRY RUN — no mail was sent.")
    else:
        print("Done. %d email(s) sent." % sent)


if __name__ == "__main__":
    main()
