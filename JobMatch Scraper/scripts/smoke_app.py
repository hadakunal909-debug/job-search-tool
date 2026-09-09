#!/usr/bin/env python3
"""
smoke_app.py — exercise every user-facing route and report what works and what doesn't.

There is no single place that says "the app is healthy". The suites in CI each prove one
narrow thing (parity, contrast, the job page) and none of them walk the whole surface, so a
route can 500 for weeks without a red tick anywhere. This walks it.

Uses Flask's test client, not a browser: it needs no dev server and no Chromium, and it still
executes the real view functions, the real DB layer and the real templates. What it cannot see
is client-side JS — feed_parity.py + test_card_meta.py already cover that half.

Reports PASS / FAIL / SKIP per route with the status code and a size, so "200 but empty" is
distinguishable from "200 with a page". Read-only: every request is a GET unless the route only
exists as a POST, and nothing is written.

    python scripts/smoke_app.py
    python scripts/smoke_app.py -v          # show a response excerpt for failures
"""
import os
import re
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv

# Query strings worth exercising on top of the bare route.
#
# The filters are exercised against /api/feed, NOT "/". The "/" shell is the same ~95 KB for
# every query — filtering happens client-side from the embedded payload — so comparing "/"
# responses tells you nothing except that the CSRF nonce changed. /api/feed returns the
# filtered `total`, which is the number that actually proves a filter ran.
#
# Param names are exact and unforgiving: the sponsor filter is `hidenospon`, not `sponsor`;
# `exp` takes a NUMBER of years or "senior" (so `exp=entry` silently matches everything);
# ordering is `sort`, not `tab`. Each of those reads as a broken filter if you guess the name.
FEED_FILTERS = ["?limit=3&q=engineer", "?limit=3&hidenospon=1", "?limit=3&track=dev",
                "?limit=3&track=mgmt", "?limit=3&intern=only", "?limit=3&remote=1",
                "?limit=3&hideagency=1", "?limit=3&verifiedonly=1", "?limit=3&exp=2",
                "?limit=3&date=7", "?limit=3&minsal=100000", "?limit=3&loc=boston",
                "?limit=3&visatags=h1b", "?limit=3&roles=swe", "?limit=3&sort=newest"]

EXTRA_QUERIES = {
    "/": ["?page=2", "?q=engineer"],
    "/api/feed": ["?limit=5"] + FEED_FILTERS,
}

# Routes that legitimately need something we are not supplying (a job url, an id, a payload).
# Listed so a 400/404 from them reads as "not exercised" rather than "broken".
NEEDS_ARGS = {"/job", "/company", "/job/research", "/application/resume", "/brain/pdf_diag"}

# Side-effectful even on GET — never called by this script.
SKIP = {"/logout", "/reload", "/scrape", "/admin/user/delete"}
# /warm answers 404 BY DESIGN when WARM_TOKEN is unset -- web.py refuses to advertise a
# route it cannot authenticate -- and the token is a GitHub secret, so it is absent on a
# laptop. Reporting that as FAIL not found made a correct refusal look like a broken route.
# Checked when the token is present, skipped when it is not.
if not (os.environ.get("WARM_TOKEN") or "").strip():
    SKIP = SKIP | {"/warm"}


def _excerpt(body, n=220):
    txt = re.sub(r"<[^>]+>", " ", body[:4000])
    return re.sub(r"\s+", " ", txt).strip()[:n]


def main():
    print("importing the app...", flush=True)
    t0 = time.time()
    try:
        import web
    except Exception:
        print("FATAL: `import web` raised — the app cannot start at all.\n")
        traceback.print_exc()
        return 1
    app = getattr(web, "app", None)
    if app is None:
        print("FATAL: web.py has no `app` object.")
        return 1
    print("imported in %.1fs" % (time.time() - t0))

    # Sample data so the routes that need an argument get a REAL one.
    from urllib.parse import quote
    job_url = company = ""
    try:
        import db
        rows = db.load_jobs(cols=["url", "company"])[:1]
        if rows:
            job_url, company = rows[0].get("url", ""), rows[0].get("company", "")
    except Exception as e:
        print("note: could not read a sample job (%s: %s)" % (type(e).__name__, str(e)[:60]))

    # Build the GET surface from the app itself, so a new route is covered without editing this.
    paths = []
    for rule in app.url_map.iter_rules():
        r = str(rule.rule)
        if "GET" not in rule.methods or r.startswith("/static") or r in SKIP:
            continue
        if "<" in r:                                  # needs a path param we don't have
            continue
        paths.append(r)
    paths = sorted(set(paths))

    targets = []
    for p in paths:
        if p == "/job" and job_url:
            targets.append(("/job", "/job?url=" + quote(job_url, safe="")))
        elif p == "/company" and company:
            # ?c=, not ?name=. The route reads request.args["c"] (company names contain
            # slashes, so the name cannot travel in the path), and with the wrong parameter it
            # redirected to the feed — which this script scored as a PASS, so /company has
            # never actually been smoke-tested.
            targets.append(("/company", "/company?c=" + quote(company, safe="")))
        else:
            targets.append((p, p))
        for q in EXTRA_QUERIES.get(p, []):
            targets.append((p, p + q))

    app.config["TESTING"] = True
    client = app.test_client()

    # LOG IN. Without this every guarded route 302s to /login and the run proves only that the
    # auth guard works — the feed, the filters, the company panel and the whole Brain surface
    # are never actually rendered. That is the trap this script exists to avoid.
    user = os.environ.get("SMOKE_USER") or ""
    if not user:
        try:
            import db as _db
            users = [u.get("username") for u in (_db.list_users() or []) if u.get("username")]
            user = users[0] if users else ""
        except Exception:
            user = ""
    if user:
        with client.session_transaction() as s:
            s["user"] = user
        print("authenticated as %r (set SMOKE_USER to change)" % user)
    else:
        print("WARNING: no user found — guarded routes will only show their redirect.")

    ok, bad, thin = [], [], []
    print("\n%-34s %-8s %-9s %s" % ("route", "status", "bytes", "verdict"))
    print("-" * 78)
    for base, path in targets:
        t = time.time()
        try:
            resp = client.get(path)
            body = resp.get_data(as_text=True)
            code, size = resp.status_code, len(body)
        except Exception as e:
            bad.append((path, "EXC %s: %s" % (type(e).__name__, str(e)[:110])))
            print("%-34s %-8s %-9s %s" % (path[:34], "EXC", "-", type(e).__name__))
            if VERBOSE:
                traceback.print_exc()
            continue
        el = time.time() - t
        slow = "  %.1fs" % el if el > 2.0 else ""
        if code >= 500:
            verdict = "FAIL server error"
            bad.append((path, "HTTP %d — %s" % (code, _excerpt(body))))
        elif code == 404 and base in NEEDS_ARGS:
            verdict = "skip (needs args)"
            ok.append(path)
        elif code == 404:
            verdict = "FAIL not found"
            bad.append((path, "HTTP 404 — route advertised by url_map but returns 404"))
        elif code in (302, 303):
            loc = resp.headers.get("Location", "?")
            verdict = "redirect -> %s" % loc[:30]
            (bad if "/login" in loc else ok).append(path)
            if "/login" in loc:
                bad[-1] = (path, "still redirects to /login while authenticated")
        elif code == 200 and size < 200:
            verdict = "THIN (200, %d bytes)" % size
            thin.append((path, size))
            ok.append(path)
        else:
            verdict = "ok"
            ok.append(path)
        print("%-34s %-8s %-9s %s%s" % (path[:34], code, size, verdict, slow))

    print("-" * 78)
    print("PASS %d   FAIL %d" % (len(ok), len(bad)))
    if thin:
        print("\nThin responses (200 but nearly empty — check these render something):")
        for p, n in thin:
            print("  %-40s %d bytes" % (p, n))
    if bad:
        print("\nProblems:")
        for path, why in bad:
            print("  %s" % path)
            print("      %s" % why)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
