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

# Same guard as scraper.py/score_jobs.py: the Windows console is cp1252 and this script prints
# em-dashes, which would otherwise come out as replacement characters.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# Bytes and request count per call, not just latency.
#
# This exists because latency alone hid the bug that blew the free-tier egress cap: db.py's
# docstrings said the feed fetch was "~1 MB" and the JD column "~20 MB", both written when the
# corpus was ~2,600 rows. At 19,268 rows the real figures are ~10.7 MB and ~122 MB, and nothing
# re-measured them for 18,000 rows. A byte count in the output is what makes that visible.
_io = {"n": 0, "bytes": 0}


def _instrument_db(db):
    """Count every HTTP response db.py receives. Wraps the session verbs in place."""
    for verb in ("get", "post", "patch", "delete", "head"):
        orig = getattr(db._http, verb, None)
        if orig is None:
            continue

        def wrap(orig):
            def f(*a, **kw):
                r = orig(*a, **kw)
                _io["n"] += 1
                # requests transparently gunzips, so this is the DECODED size. PostgREST does
                # honour Accept-Encoding (measured: 83% smaller on feed columns, 60% with jd),
                # so the bytes on the wire are lower — but the decoded figure is the one that
                # tracks what a query actually asks for, and Supabase appears to meter
                # pre-compression. Report it and note the distinction.
                _io["bytes"] += len(r.content)
                return r
            return f
        setattr(db._http, verb, wrap(orig))


def _bench(label, fn, reps=5):
    xs, io = [], []
    for _ in range(reps):
        _io["n"] = _io["bytes"] = 0
        s = time.perf_counter()
        try:
            fn()
        except Exception as e:
            print("  %-38s ERROR  %s" % (label, str(e)[:70]))
            return
        xs.append((time.perf_counter() - s) * 1000.0)
        io.append((_io["n"], _io["bytes"]))
    reqs = statistics.median([n for n, _ in io])
    byts = statistics.median([b for _, b in io])
    print("  %-38s median %8.1f ms  %3d req  %10s B%s"
          % (label, statistics.median(xs), reqs, "{:,}".format(int(byts)),
             "  (%.1f MB)" % (byts / 1e6) if byts >= 1e6 else ""))


def db_layer():
    import db
    _instrument_db(db)
    print("=== DATABASE (Supabase over HTTPS) — bytes are DECODED, gzip is smaller ===")
    jobs = db.load_jobs(include_jd=False)
    _bench("load_jobs(light)  [cold feed fetch]", lambda: db.load_jobs(include_jd=False), reps=3)
    if jobs:
        url = jobs[0]["url"]
        _bench("get_job_jd(1 row) [modal open]", lambda: db.get_job_jd(url))
        # The two reads the scraper/digest used to make wholesale. load_jobs(include_jd=True) is
        # deliberately NOT benchmarked: it is ~122 MB a go, and measuring it costs more egress
        # than the thing it measures. Extrapolate from the per-row figure this prints instead.
        sample = [j["url"] for j in jobs[:180] if j.get("url")]
        _bench("load_jobs_by_urls(180, jd) [digest]",
               lambda: db.load_jobs_by_urls(sample), reps=2)
    _bench("get_user_statuses [per request]", lambda: db.get_user_statuses("Kunalrana"))
    _bench("urls_with_jd()", lambda: db.urls_with_jd(), reps=2)
    _bench("jobs_fingerprint() [cache probe]", lambda: db.jobs_fingerprint(), reps=3)
    print("  corpus rows: %d" % len(jobs))

    # What a full-JD read would cost, priced from a small sample rather than paid for in full.
    if jobs:
        _io["n"] = _io["bytes"] = 0
        db.load_jobs_by_urls([j["url"] for j in jobs[:100] if j.get("url")])
        per_row = _io["bytes"] / 100.0
        print("  full-JD read would be ~%.0f MB decoded (%.0f B/row x %d rows) — "
              "this is what notify/score_jobs no longer do per run"
              % (per_row * len(jobs) / 1e6, per_row, len(jobs)))


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
