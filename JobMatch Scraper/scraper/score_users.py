#!/usr/bin/env python3
"""Compute every user's match score for every job and STORE it, so the feed and the job page
serve one number instead of each deriving its own.

    cd "JobMatch Scraper"
    python -m scraper.score_users                 # score whatever is missing, every user
    python -m scraper.score_users --user Kunal08singh
    python -m scraper.score_users --full          # rewrite every row, not just the gaps
    python -m scraper.score_users --dry-run       # count the work, write nothing
    python -m scraper.score_users --budget-min 8  # stop cleanly after N minutes

IN scraper/ AND NOT scripts/, and invoked as a module, because .cpanel.yml copies scraper/ but
NOT scripts/ -- so on the box the module form is the only one that exists. scraper.reposts is in
this file's position for the same reason and says so. Guarded by __main__ below: importing
anything under scraper/ must not do work, which scraper/make_careers.py learned the hard way by
rewriting careers_us.md on import.

Run it from the app directory. db.py resolves .env RELATIVE TO THE WORKING DIRECTORY, so a run
started anywhere else silently gets the local CSV backend and reports a cheerful success having
written nothing -- DB_REQUIRE makes that fail loudly instead, and this script sets it when a
proxy is configured.

WHY "MISSING" IS THE WHOLE ALGORITHM, and why there is no --new-only flag. A stored score is
stamped with the md5 of the profile it was computed against and every read filters on it, so
"rows this user has no CURRENT score for" already covers all three cases that matter with one
query: jobs scraped since the last run, jobs whose analysis was re-done (the scoring pass
deletes those rows), and a user who edited their resume (their hash moved, so every row is
missing at once and the whole corpus is re-scored). A flag per case would be three ways to get
the same answer and three ways to get it wrong.

COST, measured on the 48,372-row corpus with 5 profiled users:

    scoring          0.074 ms/row  ->  3.5 s per user for the whole corpus, 17.6 s for all five
    stored rows      239,225       ->  ~28 MB before indexes

So the compute is not the expensive part and never was; the reads and writes are. The steady
state after a backfill is a few hundred new jobs per user per run.
"""
import os
import sys
import time
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("EV_OFF", "1")             # analytics reads this at import, once

import core                                       # noqa: E402
import db                                         # noqa: E402


SCORE_BATCH_SIZE = 500


def _expired(deadline):
    return deadline is not None and time.monotonic() >= deadline


def _profiles(only=None, deadline=None):
    """[(username, profile_text, fp)] for users who have something to score against.

    A user with no resume gets no score anywhere in the product -- the feed shows an empty ring
    rather than a number derived from somebody else's CV -- so scoring them would write 47,845
    zeroes that mean "we cannot answer this" and are indistinguishable from "0% match".
    """
    out = []
    for u in db.list_users():
        if _expired(deadline):
            break
        name = u.get("username") if isinstance(u, dict) else u
        if only and name != only:
            continue
        try:
            text = db.profile_text(name) or ""
        except Exception as e:
            print("  ! %s: could not read the profile (%r)" % (name, e))
            continue
        if not text.strip():
            print("  - %-16s no resume, nothing to score against" % name)
            continue
        out.append((name, text, db.resume_fp(text)))
    return out


def score_user(name, text, fp, rows, full=False, dry=False, deadline=None):
    """Write this user's missing scores. Returns (written, skipped, unscoreable)."""
    if _expired(deadline):
        return 0, 0, 0
    have = {} if full else db.get_user_scores(name, fp)
    had = len(have)
    resume_low = text.lower()
    todo, unscoreable, written = {}, 0, 0
    visited = set(have)
    # The local fallback replaces a profile's JSON map instead of upserting it.
    # Carry its already stored scores forward when banking several batches.
    local_store = None if db.has_remote_db() else dict(have)

    def bank():
        if not todo or _expired(deadline):
            return 0
        if dry:
            count = len(todo)
        else:
            payload = todo if local_store is None else dict(local_store, **todo)
            count = db.save_user_scores(name, fp, payload, chunk=SCORE_BATCH_SIZE)
            if local_store is not None:
                if count != len(payload):
                    raise RuntimeError("local user score write was incomplete")
                local_store.update(todo)
                count = len(todo)
        todo.clear()
        return count

    for index, j in enumerate(rows):
        if _expired(deadline):
            print("    budget reached with %d still to look at" % (len(rows) - index))
            break
        url = j.get("url")
        if not url or url in visited:
            continue
        # CLOSED JOBS ARE NOT STORED. The feed hides them by default, and they were 59,445 of
        # 239,225 rows -- a quarter of the table, and of every index over it -- for postings
        # nobody is shown. Nothing is lost by leaving them out: web.user_scores seeds from this
        # table and computes whatever is missing, so a reader who switches "show closed" on gets
        # the same number, derived. Measured 149 MB -> ~112 MB.
        if j.get("is_active") is False or str(j.get("is_active")).lower() == "false":
            continue
        analyzed = core.unpack_analyzed(j.get("jd_terms")) if j.get("jd_terms") else {}
        if not analyzed.get("terms"):
            # No readable analysis: the feed shows "Not scored" for exactly these, and a stored
            # 0 would be a claim rather than an absence. Left out of the table on purpose.
            unscoreable += 1
            continue
        try:
            todo[url] = core.score_pct(resume_low, analyzed)
            visited.add(url)
        except Exception:
            unscoreable += 1
        if len(todo) >= SCORE_BATCH_SIZE:
            written += bank()
            if written and written % 5000 == 0:
                print("    %s %d" % ("would write" if dry else "wrote", written))
    # No new write starts after the deadline. Anything left in todo is still absent
    # from storage and is picked up by the next run's missing-score query.
    if dry:
        written += len(todo)
    else:
        written += bank()
    if written:
        print("    %s %d" % ("would write" if dry else "wrote", written))
    return written, had, unscoreable


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--user", help="only this username")
    ap.add_argument("--full", action="store_true", help="rewrite every row, not just the gaps")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--budget-min", type=float, default=0,
                    help="stop cleanly after N minutes (0 = no limit)")
    a = ap.parse_args()

    if os.environ.get("DB_PROXY_URL") and not os.environ.get("DB_REQUIRE"):
        os.environ["DB_REQUIRE"] = "proxy"       # never fall through to the local CSV unnoticed
    print("backend: %s" % db.backend_name())
    if not db.has_remote_db():
        print("WARNING: no remote database -- scores go to %s and reach nobody."
              % db.USER_SCORES_FILE)

    t0 = time.monotonic()
    deadline = (t0 + a.budget_min * 60) if a.budget_min > 0 else None
    users = _profiles(a.user, deadline=deadline)
    if _expired(deadline):
        print("budget reached while reading profiles; missing scores remain queued")
        return 0
    if not users:
        print("no users with a resume; nothing to do")
        return 0
    print("scoring for: %s" % ", ".join(n for n, _t, _f in users))

    rows = db.load_jobs(include_jd=False, cols="url,jd_terms,is_active")
    print("corpus: %d rows in %.1f s" % (len(rows), time.monotonic() - t0))

    total = 0
    for name, text, fp in users:
        if _expired(deadline):
            print("budget reached; the rest are still missing and the next run picks them up")
            break
        t1 = time.monotonic()
        try:
            wrote, had, bad = score_user(name, text, fp, rows, a.full, a.dry_run, deadline)
        except RuntimeError as e:
            print("  ! %-16s %s" % (name, e))
            return 1
        total += wrote
        print("  %-16s wrote %-6d had %-6d unscoreable %-5d  %.1f s"
              % (name, wrote, had, bad, time.monotonic() - t1))
        if _expired(deadline):
            print("budget reached; the rest are still missing and the next run picks them up")
            break
    print("\n%s %d row(s) in %.1f s"
          % ("would write" if a.dry_run else "wrote", total, time.monotonic() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
