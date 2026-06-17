#!/usr/bin/env python3
"""
verify_dates.py — recover the REAL posting date for jobs whose stored date is
DERIVED or a FALLBACK, using the free WhenThisJobWasPosted service.

Most boards (Greenhouse, Lever, Ashby, Adzuna, ...) hand us a clean `YYYY-MM-DD`
posting date and we trust it. But Workday/Oracle only give a relative "Posted X days
ago" string, and a few boards (Workable, Breezy, ...) give no date at all — for those
the scraper falls back to the SCRAPE TIMESTAMP (`%Y-%m-%d %H:%M`). Those cards then show
when we *pulled* the job, not when it was *posted*.

This script asks WhenThisJobWasPosted (https://whenthisjobwasposted.com) for the real
posting date of exactly those jobs and writes it to a SEPARATE column, `posted_verified`,
so the original `found_date` and the "New" (pulled-today) badge are left untouched. The
web feed prefers `posted_verified` when present and falls back to `found_date`.

INCREMENTAL / RESUMABLE: only jobs that are derived/fallback AND not already verified are
checked, so re-running (e.g. as the 3rd step after `scraper` + `score_jobs`) just picks up
the new ones.

Each lookup is latency-bound (~3-5s — the service fetches the live ATS page), so we run
several in PARALLEL but let only one START every SPACING seconds: global throughput stays
~54/min, safely under the service's 60 req/min/IP cap, regardless of worker count.

    python -m scraper.verify_dates                # backfill derived/fallback dates
    python -m scraper.verify_dates --limit 20     # cap calls this run (chunked backfill)
    python -m scraper.verify_dates --workers 6    # parallel lookups (default 6)
    python -m scraper.verify_dates --dry-run -v   # call the API, print, write nothing
    python -m scraper.verify_dates --all          # re-check EVERY job (ignore the filter)
"""
import re
import sys
import time
import threading
import concurrent.futures

# Windows cp1252 consoles crash on em dashes / accents in job titles + URLs.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import requests
import db

API = "https://mcp.whenthisjobwasposted.com/api/v1/check"
MIN_CONFIDENCE = {"high", "medium"}    # 'low' / null are skipped (found_date left as-is)
SPACING = 1.1                          # seconds between dispatches -> ~54/min (< 60/min cap)
TIMEOUT = 120                          # the service recommends a 120s timeout
BATCH = 50                             # flush DB writes every N accepted rows
WORKERS = 6                            # parallel lookups; the gate below keeps the rate safe
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}")

# Global dispatch gate: only one request may START per SPACING seconds, across all threads,
# so the in-flight parallelism hides per-call latency without ever exceeding the 60/min cap.
_gate_lock = threading.Lock()
_next_at = [0.0]
_local = threading.local()             # one requests.Session per thread (sharing one isn't safe)


def _gate():
    with _gate_lock:
        now = time.time()
        wait = _next_at[0] - now
        if wait > 0:
            time.sleep(wait)
            now = time.time()
        _next_at[0] = now + SPACING


def _session():
    s = getattr(_local, "s", None)
    if s is None:
        s = requests.Session()
        s.headers["User-Agent"] = "JobMatch-verify-dates/1.0"
        _local.s = s
    return s


def _is_clean_api_date(found_date):
    """True if the stored date already looks like a trustworthy clean ATS date — a bare
    YYYY-MM-DD with no time component. Workday/Oracle ('... HH:MM'), the scrape-timestamp
    fallback ('... HH:MM'), and empty dates all fail this and become candidates."""
    fd = (found_date or "").strip()
    return len(fd) == 10 and bool(_ISO.fullmatch(fd))


def _candidates(rows, do_all):
    out = []
    for r in rows:
        if not r.get("url"):
            continue
        if (r.get("posted_verified") or "").strip():     # already verified -> resumable skip
            continue
        if do_all or not _is_clean_api_date(r.get("found_date")):
            out.append(r)
    return out


def _ensure_columns():
    """Fail FAST (before spending rate-limited API calls) if the Supabase jobs table is
    missing the verified-date columns. No-op for the local-CSV backend (columns are just
    dict keys there)."""
    if not db.using_supabase():
        return
    r = db._http.get(db._rest(db.TABLE), headers=db._headers(),
                     params={"select": "posted_verified", "limit": 1}, timeout=30)
    if r.status_code >= 400:
        print("\nThe `jobs` table is missing the verified-date columns. Run this once in the\n"
              "Supabase SQL editor, then re-run this script:\n\n"
              "  alter table jobs add column if not exists posted_verified   text;\n"
              "  alter table jobs add column if not exists posted_confidence text;\n")
        sys.exit(1)


def check(url):
    """One REST call -> (date 'YYYY-MM-DD' or '', confidence or '', note). Retries 429."""
    session = _session()
    for attempt in range(4):
        try:
            resp = session.get(API, params={"url": url}, timeout=TIMEOUT)
        except Exception as e:
            return "", "", "error:%s" % (e.__class__.__name__)
        if resp.status_code == 429:                       # rate limited -> back off + retry
            time.sleep((2 ** attempt) * 2)
            continue
        if resp.status_code >= 400:
            return "", "", "http:%d" % resp.status_code
        try:
            d = resp.json()
        except ValueError:
            return "", "", "bad-json"
        m = _ISO.search(d.get("most_probable_date") or "")
        date = m.group(0) if m else ""
        conf = (d.get("confidence") or "").lower()
        note = d.get("reason") or ("" if d.get("supported", True) else "unsupported")
        return date, conf, note
    return "", "", "rate_limited"


def _work(r):
    """Pool task: wait for a dispatch slot, then look the job up. -> (row, date, conf, note)."""
    _gate()
    return (r,) + check(r["url"])


def main():
    argv = sys.argv[1:]
    do_all = "--all" in argv
    dry = "--dry-run" in argv
    verbose = "--verbose" in argv or "-v" in argv
    limit = None
    if "--limit" in argv:
        try:
            limit = int(argv[argv.index("--limit") + 1])
        except (ValueError, IndexError):
            print("--limit needs an integer, e.g. --limit 20")
            return
    workers = WORKERS
    if "--workers" in argv:
        try:
            workers = max(1, int(argv[argv.index("--workers") + 1]))
        except (ValueError, IndexError):
            print("--workers needs an integer, e.g. --workers 6")
            return

    if not dry:
        _ensure_columns()

    rows = db.load_jobs(include_jd=False)
    cands = _candidates(rows, do_all)
    if limit is not None:
        cands = cands[:limit]
    n = len(cands)
    where = "Supabase" if db.using_supabase() else "jobs.csv"
    print("%d jobs total; %d to verify (%s)%s, %d workers -> %s"
          % (len(rows), n, "--all" if do_all else "derived/fallback",
             " [DRY RUN]" if dry else "", workers, where))
    if not cands:
        print("Nothing to verify.")
        return

    pending, accepted, skipped, errors = [], 0, 0, 0
    t0 = time.time()

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for i, (r, date, conf, note) in enumerate(ex.map(_work, cands), 1):
            url = r["url"]
            if date and conf in MIN_CONFIDENCE:
                pending.append({"url": url, "posted_verified": date, "posted_confidence": conf})
                accepted += 1
                if verbose:
                    print("  OK   %-6s %s  %s" % (conf, date, url))
            else:
                skipped += 1
                if note.startswith(("error", "http", "rate", "bad")):
                    errors += 1
                if verbose:
                    print("  skip %-6s %-12s %s" % (conf or "-", note or "no-date", url))

            # DB writes stay on the main thread (one writer) — flush in batches.
            if not dry and len(pending) >= BATCH:
                db.update_job_fields(pending)
                pending = []

            if i % 25 == 0 or i == n:
                rate = i / max(time.time() - t0, 1e-6) * 60
                print("  ... %d/%d (verified %d, skipped %d) ~%.0f/min"
                      % (i, n, accepted, skipped, rate))

    if not dry and pending:
        db.update_job_fields(pending)

    verb = "would verify" if dry else "verified"
    print("Done. %s %d, skipped %d (%d errors) -> %s."
          % (verb, accepted, skipped, errors, where))


if __name__ == "__main__":
    main()
