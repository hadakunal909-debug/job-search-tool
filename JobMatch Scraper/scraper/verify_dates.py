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

INCREMENTAL / RESUMABLE: only jobs that are derived/fallback AND not already ANSWERED are
checked, so re-running (e.g. as the 3rd step after `scraper` + `score_jobs`) just picks up
the new ones.

"Answered" means answered EITHER WAY. A row the service dated is skipped because
posted_verified is set; a row it looked at and could not date confidently is skipped
because posted_confidence records that verdict. Recording only the wins is what let this
step run forever: an undatable URL came back into the candidate list on every single run,
and since the answer for a given URL never changes, those calls could only ever be spent
again to be thrown away again. With ~7.6k candidates against the rate ceiling below that
is ~2.4 HOURS of calls inside a 45-minute CI job — the step never returned, so the digest
that runs after it never sent. Pass --all to re-ask anyway.

Each lookup is latency-bound (~3-5s — the service fetches the live ATS page), so we run
several in PARALLEL but let only one START every SPACING seconds: global throughput stays
~54/min, safely under the service's 60 req/min/IP cap, regardless of worker count. That
ceiling is the reason this step needs a WALL-CLOCK BUDGET rather than trusting the backlog
to be small: throughput is fixed, so the only thing that decides whether it finishes is how
many rows are waiting. --budget-min stops cleanly and banks everything checked so far.

Candidates are ordered NEWEST FIRST. Under a budget the tail is what gets dropped, and a
stale row is both the least useful to date (nobody is applying to it) and the most likely
to be pruned out of the table before the next run reaches it anyway.

    python -m scraper.verify_dates                # backfill derived/fallback dates
    python -m scraper.verify_dates --limit 20     # cap calls this run (chunked backfill)
    python -m scraper.verify_dates --budget-min 8 # stop after 8 minutes, bank what's done
    python -m scraper.verify_dates --workers 6    # parallel lookups (default 6)
    python -m scraper.verify_dates --dry-run -v   # call the API, print, write nothing
    python -m scraper.verify_dates --all          # re-check EVERY job (ignore the filter)
"""
import os
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
BUDGET_MIN = 0                         # 0 = no wall-clock limit; CI sets VERIFY_BUDGET_MIN
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}")

# Notes THIS FILE produces when the call itself failed, so we never got the service's
# opinion. Those rows stay candidates and are retried next run; anything else is the service
# having ANSWERED, and its answer for a fixed URL is stable, so it gets recorded once and
# never asked again.
#
# Matched exactly / by a punctuated prefix rather than by bare words, because the other
# notes in this field are the service's own free-text `reason`. Testing for "http" or
# "error" as loose prefixes would silently reclassify a reason like "error parsing the
# posting" as transient — and a row wrongly called transient is retried on every run
# forever, which is the exact failure this whole mechanism exists to stop.
_TRANSIENT_EXACT = {"rate_limited", "bad-json", "budget"}
_TRANSIENT_PREFIX = ("error:", "http:")


def _is_transient(note):
    note = note or ""
    return note in _TRANSIENT_EXACT or note.startswith(_TRANSIENT_PREFIX)

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
    """Rows still worth a lookup, NEWEST FIRST.

    Two resumable skips, and the second one is what keeps the backlog finite: a row the
    service already answered "can't date this" for carries that verdict in
    posted_confidence, and asking again would spend a rate-limited call to receive the
    same answer. --all ignores that and re-asks. Rows we DID date are skipped either way.
    """
    out = []
    for r in rows:
        if not r.get("url"):
            continue
        if (r.get("posted_verified") or "").strip():     # already dated -> resumable skip
            continue
        if not do_all and (r.get("posted_confidence") or "").strip():
            continue                                     # already answered, unusably -> skip
        if do_all or not _is_clean_api_date(r.get("found_date")):
            out.append(r)
    # Newest first. found_date is ISO-prefixed in both shapes we store ('YYYY-MM-DD' and
    # 'YYYY-MM-DD HH:MM'), so a plain string sort is a date sort. Matters because --limit
    # and --budget-min both drop the TAIL: spend the run's calls on the postings someone
    # might actually apply to, not on rows the 30-day prune is about to delete.
    out.sort(key=lambda r: (r.get("found_date") or ""), reverse=True)
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


_deadline = [0.0]                      # 0 = unlimited; set from --budget-min / env


def _out_of_time():
    return bool(_deadline[0]) and time.time() >= _deadline[0]


def _work(r):
    """Pool task: wait for a dispatch slot, then look the job up. -> (row, date, conf, note).

    The budget is checked BEFORE the dispatch gate, so once time is up the rest of the
    queue drains instantly instead of each task sleeping its way to the front. 'budget'
    is a transient note: nothing is recorded, and the row is a candidate again next run.
    """
    if _out_of_time():
        return r, "", "", "budget"
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
    # Wall-clock budget. Throughput here is FIXED by the rate gate (~54/min), so a backlog
    # of any size translates directly into minutes and there is no amount of tuning that
    # makes a big one fit. CI passes a budget so this step returns on time and the notify
    # step after it actually runs; the rest of the backlog drains next run.
    budget_min = BUDGET_MIN
    try:
        budget_min = float(os.environ.get("VERIFY_BUDGET_MIN") or BUDGET_MIN)
    except ValueError:
        budget_min = BUDGET_MIN
    if "--budget-min" in argv:
        try:
            budget_min = float(argv[argv.index("--budget-min") + 1])
        except (ValueError, IndexError):
            print("--budget-min needs a number of minutes, e.g. --budget-min 8")
            return
    _deadline[0] = (time.time() + budget_min * 60) if budget_min > 0 else 0.0

    if not dry:
        _ensure_columns()

    # _candidates reads only url/posted_verified/posted_confidence/found_date, and _work below
    # takes r["url"] — 4.4 MB a call instead of 11.6 MB at 19k rows.
    rows = db.load_jobs(cols=db.COLS_VERIFY)
    cands = _candidates(rows, do_all)
    queued = len(cands)                           # backlog BEFORE --limit trims it
    if limit is not None:
        cands = cands[:limit]
    n = len(cands)
    where = "Supabase" if db.using_supabase() else "jobs.csv"
    budget = ("%g min budget" % budget_min) if budget_min > 0 else "no budget"
    print("%d jobs total; %d to verify (%s)%s, %d workers, %s -> %s"
          % (len(rows), n, "--all" if do_all else "derived/fallback",
             " [DRY RUN]" if dry else "", workers, budget, where))
    # The rate gate makes this arithmetic, not an estimate — worth printing every run so a
    # backlog that has grown past the budget is visible in the log before it is a problem.
    print("  backlog %d row(s); at the ~%.0f/min ceiling the whole queue is %.0f min of calls."
          % (queued, 60.0 / SPACING, queued / (60.0 / SPACING)))
    if not cands:
        print("Nothing to verify.")
        return

    pending, accepted, skipped, errors, unspent = [], 0, 0, 0, 0
    t0 = time.time()

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for i, (r, date, conf, note) in enumerate(ex.map(_work, cands), 1):
            url = r["url"]
            if date and conf in MIN_CONFIDENCE:
                pending.append({"url": url, "posted_verified": date, "posted_confidence": conf})
                accepted += 1
                if verbose:
                    print("  OK   %-6s %s  %s" % (conf, date, url))
            elif note == "budget":
                unspent += 1                      # never dispatched; still a candidate next run
                continue                          # (no progress line — these drain in bulk)
            else:
                skipped += 1
                if _is_transient(note):
                    errors += 1                   # the CALL failed -> retry next run
                else:
                    # The service answered and its answer is unusable ('low' confidence, or
                    # no date at all). That verdict is a property of the URL, so record it
                    # and stop paying for it every run. `or "none"` because a bare skip can
                    # come back with an empty confidence, and an empty string would read as
                    # "never asked" and put the row straight back in the queue.
                    pending.append({"url": url, "posted_confidence": conf or "none"})
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
    print("Done in %.1f min. %s %d, skipped %d (%d transient errors, retried next run) -> %s."
          % ((time.time() - t0) / 60, verb, accepted, skipped, errors, where))
    if unspent:
        print("  Budget reached: %d of %d left unchecked. They stay queued — next run starts "
              "there, and it is a SHORTER list because this one recorded its answers."
              % (unspent, n))


if __name__ == "__main__":
    main()
