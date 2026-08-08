#!/usr/bin/env python3
"""
ev_maintain.py — roll analytics events up into daily counts, then prune the raw rows.

    python scripts/ev_maintain.py              # dry run: report only
    python scripts/ev_maintain.py --apply
    python scripts/ev_maintain.py --apply --days 90 --cache-days 30

Why this exists: `tailored_cache` was created as a cache, has no expiry anywhere in the
codebase, and has grown ever since. This is the step that table never got. Without it the
events table is the same mistake with a faster fill rate.

Wired into .github/workflows/scrape.yml on the heavy pass, so it runs once a weekday. The
rollup is idempotent — it RECOMPUTES the last few days and replaces those rows rather than
adding to them, so running twice cannot double-count.

Modelled on scripts/prune_stale.py: dry-run by default, --apply to commit.
"""
import os
import sys
import json
import argparse
import datetime
import collections

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402

ROLLUP_DAYS = 3        # recompute this many recent days; covers a missed run and any late writes
RETAIN_DAYS = 90       # raw events kept
CACHE_DAYS = 30        # tailored_cache entries kept


def _iso(d):
    return d.isoformat()


def fetch_window(start_day):
    """Raw events from start_day. Pages explicitly by id — _fetch_all defaults to order=url,
    a column this table does not have, and PostgREST 400s on it."""
    rows, last_id, page = [], 0, 1000
    while True:
        try:
            r = db._http.get(
                db._rest(db.EVENTS_TABLE), headers=db._headers(),
                params={"select": "id,ts,username,event", "ts": "gte.%s" % start_day,
                        "id": "gt.%d" % last_id, "order": "id", "limit": page}, timeout=40)
            if r.status_code >= 400:
                print("  fetch failed: %s %s" % (r.status_code, r.text[:160]))
                return rows
            batch = r.json() or []
        except Exception as e:
            print("  fetch failed: %s" % e)
            return rows
        rows.extend(batch)
        if len(batch) < page:
            return rows
        last_id = batch[-1]["id"]


def rollup(days, apply_changes):
    start = datetime.date.today() - datetime.timedelta(days=days - 1)
    rows = fetch_window(_iso(start))
    print("rollup: %d raw events since %s" % (len(rows), _iso(start)))
    counts = collections.Counter()
    for r in rows:
        ts = str(r.get("ts") or "")[:10]
        if not ts:
            continue
        counts[(ts, r.get("username") or "?", r.get("event") or "?", "")] += 1
    payload = [{"day": d, "username": u, "event": e, "dim": dim, "n": n}
               for (d, u, e, dim), n in counts.items()]
    print("        -> %d daily rows" % len(payload))
    if not payload or not apply_changes:
        return len(payload)
    # merge-duplicates on the full primary key REPLACES n rather than adding to it, which is
    # what makes re-running safe.
    for i in range(0, len(payload), 200):
        try:
            resp = db._http.post(
                db._rest(db.EVENTS_DAILY_TABLE),
                headers=db._headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
                params={"on_conflict": "day,username,event,dim"},
                data=json.dumps(payload[i:i + 200]), timeout=30)
            if resp.status_code >= 400:
                print("        write failed: %s %s" % (resp.status_code, resp.text[:160]))
                return len(payload)
        except Exception as e:
            print("        write failed: %s" % e)
            return len(payload)
    print("        written.")
    return len(payload)


def prune(retain_days, apply_changes):
    cutoff = _iso(datetime.date.today() - datetime.timedelta(days=retain_days))
    n = db.table_count(db.EVENTS_TABLE, {"ts": "lt.%s" % cutoff})
    print("prune:  %s raw event(s) older than %s"
          % ("{:,}".format(n) if n is not None else "?", cutoff))
    if not apply_changes:
        return
    if n:
        print("        %s" % ("deleted." if db.prune_events(cutoff) else "delete FAILED."))
    # Deleting doesn't return space to the OS — autovacuum reclaims it for reuse — so the table
    # plateaus rather than shrinking. That is the intended outcome, not a failed prune.


def prune_cache(cache_days, apply_changes):
    """The other unbounded table. put_tailored() has never had a deleter."""
    cutoff = _iso(datetime.date.today() - datetime.timedelta(days=cache_days))
    n = db.table_count("tailored_cache", {"created_at": "lt.%s" % cutoff})
    print("cache:  %s tailored_cache row(s) older than %s"
          % ("{:,}".format(n) if n is not None else "?", cutoff))
    if not apply_changes or not n:
        return
    try:
        r = db._http.delete(db._rest("tailored_cache"),
                            headers=db._headers({"Prefer": "return=minimal"}),
                            params={"created_at": "lt.%s" % cutoff}, timeout=60)
        print("        %s" % ("deleted." if r.status_code < 400 else "delete FAILED %s" % r.status_code))
    except Exception as e:
        print("        delete FAILED: %s" % e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually write/delete")
    ap.add_argument("--days", type=int, default=RETAIN_DAYS, help="days of raw events to keep")
    ap.add_argument("--rollup-days", type=int, default=ROLLUP_DAYS)
    ap.add_argument("--cache-days", type=int, default=CACHE_DAYS)
    a = ap.parse_args()

    if not db.using_supabase():
        print("No Supabase credentials — nothing to do.")
        return 0
    if db.table_count(db.EVENTS_TABLE) is None:
        print("No `events` table — run SUPABASE_EVENTS_MIGRATION.sql first. Nothing to do.")
        return 0

    print("mode: %s\n" % ("APPLY" if a.apply else "dry run (use --apply to commit)"))
    rollup(a.rollup_days, a.apply)
    prune(a.days, a.apply)
    prune_cache(a.cache_days, a.apply)
    total = db.table_count(db.EVENTS_TABLE)
    print("\nevents table now: %s row(s)" % ("{:,}".format(total) if total is not None else "?"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
