#!/usr/bin/env python3
"""Quick speed test of the whole JobMatch stack.

Measures the two layers you control:
  * DATABASE  — the Supabase calls the feed makes (corpus fetch, single-row JD, statuses)
  * SERVER    — response time of the live/local Flask endpoints (TTFB-ish)

Usage (run from the project folder, with .env present so it hits Supabase):
    python speedtest.py                                   # DB layer only
    python speedtest.py http://127.0.0.1:5000             # + your LOCAL server
    python speedtest.py https://stemjobs.astrochakra.co   # + the LIVE server

No extra packages needed (stdlib + the project's db.py).
Note: the feed "/" needs login, so this hits the public /healthz + /login only — for the
logged-in feed itself, measure in the browser DevTools Network tab.
"""
import sys
import time
import statistics
import urllib.request


def _bench(label, fn, reps=5):
    xs = []
    for _ in range(reps):
        s = time.perf_counter()
        try:
            fn()
        except Exception as e:
            print("  %-38s ERROR  %s" % (label, str(e)[:70]))
            return
        xs.append((time.perf_counter() - s) * 1000.0)
    print("  %-38s median %8.1f ms   (min %.1f, max %.1f)"
          % (label, statistics.median(xs), min(xs), max(xs)))


def db_layer():
    import db
    print("=== DATABASE (Supabase over HTTPS) ===")
    jobs = db.load_jobs(include_jd=False)
    _bench("load_jobs(light)  [cold feed fetch]", lambda: db.load_jobs(include_jd=False), reps=3)
    if jobs:
        url = jobs[0]["url"]
        _bench("get_job_jd(1 row) [modal open]", lambda: db.get_job_jd(url))
    _bench("get_user_statuses [per request]", lambda: db.get_user_statuses("Kunalrana"))
    _bench("urls_with_jd()", lambda: db.urls_with_jd(), reps=2)
    print("  corpus rows: %d" % len(jobs))


def server_layer(base):
    base = base.rstrip("/")
    print("\n=== SERVER (%s) ===" % base)

    def hit(path):
        def go():
            req = urllib.request.Request(base + path, headers={"User-Agent": "jobmatch-speedtest"})
            with urllib.request.urlopen(req, timeout=60) as r:
                r.read()
        return go

    for path in ("/healthz", "/login"):
        _bench("GET %-9s [server response]" % path, hit(path), reps=5)
    print("  (/healthz = server only, no DB. The feed '/' needs login —")
    print("   measure that in the browser: F12 -> Network -> reload -> the '/' row's TTFB.)")


if __name__ == "__main__":
    db_layer()
    if len(sys.argv) > 1:
        server_layer(sys.argv[1])
    print("\nTip: run twice — the 1st run is 'cold' (server asleep / caches empty),")
    print("the 2nd is 'warm'. The gap is what the keep-warm pinger removes.")
