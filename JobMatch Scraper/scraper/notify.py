#!/usr/bin/env python3
"""
notify.py — email a digest of the jobs added in the latest scrape.

Reads last_new_jobs.json (written by scraper.py) so it emails ONLY this run's new
jobs, looks up their scores from the DB (run after score_jobs.py), drops anything the
JD says won't sponsor, and sends an HTML digest over SMTP.

DORMANT BY DESIGN: if the SMTP_* env vars aren't set it just prints a note and exits 0,
so it never breaks the scrape. To turn it on, set these (env vars / GitHub secrets):
    SMTP_HOST, SMTP_USER, SMTP_PASS, ALERT_TO      (required)
    SMTP_PORT (default 587), ALERT_FROM (default SMTP_USER), ALERT_MIN_SCORE (default 0)
Your cPanel host's mailbox works (SMTP_HOST = mail.<yourdomain>, port 465 or 587).
"""
import os
import json
import ssl
import smtplib
import html as _html
from email.mime.text import MIMEText

import core
import db

NEW_FILE = "last_new_jobs.json"


def _esc(s):
    return _html.escape(s or "")


def main():
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASS")
    to = os.environ.get("ALERT_TO") or user
    if not (host and user and pw and to):
        print("Alerts not configured (set SMTP_HOST/SMTP_USER/SMTP_PASS/ALERT_TO) - skipping.")
        return

    try:
        new = json.load(open(NEW_FILE, encoding="utf-8"))
    except Exception:
        new = []
    if not new:
        print("No new jobs this run — no email sent.")
        return

    min_score = int(os.environ.get("ALERT_MIN_SCORE", "0") or 0)
    by_url = {j.get("url"): j for j in (db.load_jobs() or [])}     # for score + jd

    items = []
    for r in new:
        u = r.get("url")
        full = by_url.get(u, r)
        try:
            sc = int(full.get("match_score") or 0)
        except Exception:
            sc = 0
        if sc < min_score:
            continue
        if core.sponsorship_from_jd(full.get("jd") or "")[0] == "blocked":
            continue                                              # skip roles that won't sponsor
        items.append((sc, r.get("title", ""), r.get("company", ""), r.get("location", ""),
                      u, r.get("sponsors_h1b", ""), core.is_cap_exempt(r.get("company", ""))))
    if not items:
        print("New jobs found but none passed the alert filter — no email sent.")
        return
    items.sort(reverse=True)

    rows = "".join(
        "<tr><td align='right' valign='top'><b>%d%%</b></td>"
        "<td>&nbsp;<a href='%s'>%s</a><br><small>%s &middot; %s%s%s</small></td></tr>"
        % (sc, _esc(u), _esc(t), _esc(co), _esc(loc),
           " &middot; H1B" if spon == "yes" else "",
           " &middot; \U0001F393 no lottery" if capx else "")
        for (sc, t, co, loc, u, spon, capx) in items)
    html = ("<h2>%d new job%s for you</h2>"
            "<table cellpadding='6' style='border-collapse:collapse'>%s</table>"
            "<p><small>From your job-search tool. Sorted by match score.</small></p>"
            % (len(items), "" if len(items) == 1 else "s", rows))

    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = "%d new sponsor-friendly job%s" % (len(items), "" if len(items) == 1 else "s")
    msg["From"] = os.environ.get("ALERT_FROM") or user
    msg["To"] = to

    port = int(os.environ.get("SMTP_PORT", "587") or 587)
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
            s.login(user, pw)
            s.sendmail(msg["From"], [to], msg.as_string())
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(user, pw)
            s.sendmail(msg["From"], [to], msg.as_string())
    print("Sent alert: %d job(s) to %s" % (len(items), to))


if __name__ == "__main__":
    main()
