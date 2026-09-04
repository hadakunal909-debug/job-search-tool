#!/usr/bin/env python3
"""Email when a scheduled scrape fails. Runs as the last step of scrape.yml, on failure only.

WHY THIS EXISTS. The 2026-09-03 scrape failed at 16:49 UTC and nobody knew until somebody
happened to look at the Actions tab the next day. GitHub does send its own notification for a
failed scheduled run, but only to the account that last touched the cron, only if that account
has the setting enabled, and it arrives in the same firehose as every other GitHub email. A
scrape that silently stops means a feed that silently goes stale, which is indistinguishable
from a quiet job market until you check the dates.

IT SHARES NO DEPENDENCY WITH WHAT IT REPORTS ON. No db, no core, no scraper package, no
requests -- only smtplib and os. That is deliberate and it is the whole design: the most likely
reason a run failed is that the database was unreachable, and an alerter that imports db.py
would be broken by exactly the outage it is supposed to tell you about. It is also why the
message carries no row counts.

DORMANT BY DESIGN, the same convention scraper/notify.py uses: with no SMTP_* configured it
prints what it would have sent and exits 0. A fork, a local run, or a repository whose secrets
were never set gets a readable log line rather than a second failure stacked on the first.

IT NEVER EXITS NON-ZERO. An alerter that fails the job it is reporting on turns one red run
into two and buries the real cause. Every failure path here prints and returns 0.

    python scripts/alert_run_failure.py

Environment (all optional; the GitHub ones are set automatically on a runner):
    SMTP_HOST, SMTP_USER, SMTP_PASS   as scraper/notify.py
    SMTP_PORT (default 587), ALERT_FROM (default SMTP_USER)
    ALERT_TO                          where to send; falls back to SMTP_USER
    FAILED_STEPS                      free text naming what died, from the workflow
    GITHUB_SERVER_URL, GITHUB_REPOSITORY, GITHUB_RUN_ID, GITHUB_WORKFLOW,
    GITHUB_EVENT_NAME, GITHUB_RUN_ATTEMPT
"""
import datetime
import os
import smtplib
import ssl
import sys
from email.mime.text import MIMEText

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def run_url():
    server = os.environ.get("GITHUB_SERVER_URL") or "https://github.com"
    repo = os.environ.get("GITHUB_REPOSITORY") or ""
    rid = os.environ.get("GITHUB_RUN_ID") or ""
    if not (repo and rid):
        return ""
    return "%s/%s/actions/runs/%s" % (server, repo, rid)


def build(now=None):
    """(subject, html). Pure, so the test can assert on it without sending anything."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    wf = os.environ.get("GITHUB_WORKFLOW") or "Scrape jobs"
    event = os.environ.get("GITHUB_EVENT_NAME") or "?"
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT") or "1"
    steps = (os.environ.get("FAILED_STEPS") or "").strip()
    url = run_url()

    subject = "[JobMatch] %s FAILED (%s)" % (wf, now.strftime("%a %d %b, %H:%M UTC"))
    rows = [
        ("Workflow", wf),
        ("Triggered by", event),
        ("Attempt", attempt),
        ("Time", now.strftime("%Y-%m-%d %H:%M:%S UTC")),
    ]
    if steps:
        rows.append(("Steps that failed", steps))

    body = ["<div style=\"font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;color:#111\">",
            "<p><strong>The scheduled scrape did not complete.</strong> "
            "The feed will go stale until a run succeeds.</p>",
            "<table cellpadding=\"4\" style=\"border-collapse:collapse\">"]
    for k, v in rows:
        body.append("<tr><td style=\"color:#666\">%s</td><td><strong>%s</strong></td></tr>"
                    % (_esc(k), _esc(v)))
    body.append("</table>")
    if url:
        body.append("<p><a href=\"%s\">Open the run log</a></p>" % _esc(url))
    # Say what happens next, so the reader knows whether to act. The watchdog retries once per
    # day and then stops; without this line "it failed" reads as "you must fix it now".
    body.append("<p style=\"color:#666\">The watchdog will retry once today. If that also fails "
                "it stops, to avoid burning the Actions allowance, and this is the mail to act "
                "on.</p>")
    body.append("</div>")
    return subject, "".join(body)


def main():
    subject, html = build()
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASS")
    to = (os.environ.get("ALERT_TO") or user or "").strip()

    if not (host and user and pw and to):
        print("Failure alerts not configured (SMTP_HOST/SMTP_USER/SMTP_PASS/ALERT_TO) - skipping.")
        print("Would have sent: %s" % subject)
        return 0

    port = int(os.environ.get("SMTP_PORT", "587") or 587)
    sender = os.environ.get("ALERT_FROM") or user
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    try:
        ctx = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
                s.login(user, pw)
                s.sendmail(sender, [to], msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.starttls(context=ctx)
                s.login(user, pw)
                s.sendmail(sender, [to], msg.as_string())
        print("Failure alert sent to %s" % to)
    except Exception as e:
        # Swallowed on purpose -- see the module docstring. The run is already red; the log line
        # is what tells you the alert path itself needs looking at.
        print("Could not send the failure alert (%s: %s)" % (type(e).__name__, str(e)[:200]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
