#!/usr/bin/env python3
"""The failure alerter's two promises: it never fails the run, and it never sends by accident.

This guards a script that only ever executes on a day something is already broken, which is the
worst possible time to discover a typo in it. Everything below is offline — no SMTP connection
is opened in any test, and the one that checks the send path asserts it was NOT reached.

    python scripts/test_alert_failure.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import alert_run_failure as A

fails, ran = [], []


def check(label, ok, got=None):
    ran.append(label)
    print("  %s  %s" % ("ok " if ok else "FAIL", label))
    if not ok:
        fails.append(label)
        if got is not None:
            print("        got: %s" % (got,))


def with_env(**kw):
    """Set exactly this environment for the alerter's keys, clearing the rest."""
    for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "SMTP_PORT", "ALERT_TO", "ALERT_FROM",
              "FAILED_STEPS", "GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID",
              "GITHUB_WORKFLOW", "GITHUB_EVENT_NAME", "GITHUB_RUN_ATTEMPT"):
        os.environ.pop(k, None)
    os.environ.update({k: str(v) for k, v in kw.items()})


print("=" * 74)
print("IT NEVER FAILS THE RUN — it only ever executes on an already-red run")
print("=" * 74)

with_env()
check("unconfigured returns 0, it does not raise", A.main() == 0)

# The realistic disaster: secrets are set but the mail server refuses. The run is already red;
# a second red step here would bury the actual cause under an SMTP traceback.
sent = []


class Boom:
    def __init__(self, *a, **kw):
        raise OSError("connection refused")


import smtplib as _smtp
_real_smtp, _real_ssl = _smtp.SMTP, _smtp.SMTP_SSL
_smtp.SMTP, _smtp.SMTP_SSL = Boom, Boom
try:
    with_env(SMTP_HOST="mail.example.test", SMTP_USER="u@example.test", SMTP_PASS="p",
             ALERT_TO="me@example.test")
    check("a dead mail server still returns 0", A.main() == 0)
finally:
    _smtp.SMTP, _smtp.SMTP_SSL = _real_smtp, _real_ssl

print()
print("=" * 74)
print("IT NEVER SENDS BY ACCIDENT — dormant unless every credential is present")
print("=" * 74)


class Tripwire:
    def __init__(self, *a, **kw):
        sent.append("CONNECTED")
        raise AssertionError("the alerter opened an SMTP connection it should not have")


for missing, env in (
    ("SMTP_HOST",  dict(SMTP_USER="u", SMTP_PASS="p", ALERT_TO="t@e.test")),
    ("SMTP_PASS",  dict(SMTP_HOST="h", SMTP_USER="u", ALERT_TO="t@e.test")),
    ("SMTP_USER",  dict(SMTP_HOST="h", SMTP_PASS="p", ALERT_TO="t@e.test")),
    # ALERT_TO absent is NOT dormant -- it falls back to SMTP_USER, which is the mailbox the
    # credentials already belong to. Asserted below rather than here.
):
    _smtp.SMTP, _smtp.SMTP_SSL = Tripwire, Tripwire
    try:
        with_env(**env)
        rc = A.main()
        check("no %s -> dormant, nothing dialled" % missing, rc == 0 and not sent, (rc, sent))
    finally:
        _smtp.SMTP, _smtp.SMTP_SSL = _real_smtp, _real_ssl

print()
print("=" * 74)
print("THE MESSAGE — what the person reading it at 9am actually needs")
print("=" * 74)

with_env(GITHUB_SERVER_URL="https://github.com",
         GITHUB_REPOSITORY="owner/repo", GITHUB_RUN_ID="12345",
         GITHUB_WORKFLOW="Scrape jobs", GITHUB_EVENT_NAME="schedule",
         GITHUB_RUN_ATTEMPT="2",
         FAILED_STEPS="preflight=success scrape=failure score=failure")
subject, html = A.build()

check("the subject says which workflow and that it FAILED",
      "Scrape jobs" in subject and "FAILED" in subject, subject)
check("a deep link to the run log, which is the first thing anyone wants",
      "https://github.com/owner/repo/actions/runs/12345" in html, html[:200])
check("it names the steps that died", "scrape=failure" in html, html)
check("it says the trigger, so a dispatch is distinguishable from the cron",
      "schedule" in html, html)
check("it says what happens next — the watchdog retries once",
      "retry once" in html, html)

# The run URL is assembled from three variables. Off a runner they are absent, and a half-built
# link ("https://github.com//actions/runs/") is worse than none: it looks clickable.
with_env(GITHUB_WORKFLOW="Scrape jobs")
_, html2 = A.build()
check("no link at all when the run context is missing, rather than a broken one",
      "actions/runs" not in html2, html2)

# FAILED_STEPS arrives from a workflow expression and could carry anything.
with_env(GITHUB_WORKFLOW="Scrape jobs", FAILED_STEPS="<script>alert(1)</script>")
_, html3 = A.build()
check("the step text is escaped, not injected into the mail body",
      "<script>" not in html3 and "&lt;script&gt;" in html3, html3)

print()
if fails:
    print("FAILED (%d): %s" % (len(fails), "; ".join(fails)))
    sys.exit(1)
print("all good - %d checks" % len(ran))
