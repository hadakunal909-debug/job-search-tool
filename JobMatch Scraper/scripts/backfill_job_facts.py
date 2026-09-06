#!/usr/bin/env python3
"""backfill_job_facts.py — copy the derived columns from jobs into public.job_facts.

    python scripts/backfill_job_facts.py              # how much is left
    python scripts/backfill_job_facts.py --apply      # copy, in batches
    python scripts/backfill_job_facts.py --verify     # compare both stores, column for column
    python scripts/backfill_job_facts.py --stamp      # declare the table authoritative

Same shape as backfill_job_descriptions.py, and the same rule: db.job_facts_ready() reads
data_versions['job_facts'], nothing believes the new table until that is stamped, and --stamp
refuses while anything is missing or differs.

WHY A PARTIAL COPY IS PARTICULARLY BAD HERE, worse than for the descriptions. A missing row in
job_descriptions reads as "no description", which puts a posting back in the fetch queue -- waste,
and visible. A missing row in job_facts reads as "no experience floor, no pay, no sponsorship
verdict" -- and web._filter_rows KEEPS a row it has no number for, deliberately, because many
genuine entry-level posts state none. So a half-copied facts table does not narrow the feed or
empty it. It silently WIDENS every filter, which is the precise defect this whole revamp began
with: nine postings asking three to ten years turning up in a "0 to 2 Years" search.

That is why --verify compares every column of every row rather than counting them.
"""
import argparse
import collections
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("EV_OFF", "1")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import db

BATCH = 400
# url plus the nine that move, read from `jobs` while it is still authoritative.
SRC_COLS = "url," + ",".join(db.JOB_FACTS_COLS)


def _norm(col, v):
    """Compare the two stores the way the database will, not the way JSON happens to spell it.

    booleans arrive as True/'t'/'true' and integers as 3/'3' depending on transport and column
    type. A verify that reported those as differences would cry wolf over all 47,000 rows and
    teach everyone to pass --stamp anyway, which is the one thing it exists to prevent.
    """
    if v is None or v == "":
        return None
    if col == "remote":
        return str(v).strip().lower() in ("1", "true", "t", "yes")
    if col in ("salary_min", "salary_max", "exp_max_years"):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    return str(v)


def _source_rows():
    rows = db.load_jobs(cols=SRC_COLS)
    # A row with nothing derived yet has no facts to copy; it is not a gap.
    return {r["url"]: r for r in rows
            if r.get("url") and any(_norm(c, r.get(c)) is not None for c in db.JOB_FACTS_COLS)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--stamp", action="store_true")
    a = ap.parse_args()

    if not db.has_remote_db():
        print("No database configured. Set PG_DSN, or DB_PROXY_URL + DB_PROXY_SECRET.")
        return 2
    try:
        db._fetch_all(db.JOB_FACTS_TABLE, {"select": "url", "limit": 1})
    except Exception as e:
        if not db._table_missing(e):
            raise
        print("public.job_facts does not exist. Run MIGRATION_job_facts.sql.")
        return 2
    if db.job_facts_ready():
        print("NOTE: job_facts is already stamped authoritative, so db.load_jobs now MERGES it")
        print("      back over `jobs`. --verify still compares the two stores directly.")

    if a.verify or a.stamp:
        rc = verify()
        if not a.stamp or rc:
            return rc
        db.set_data_version("job_facts", "backfilled-%s" % time.strftime("%Y%m%d"))
        print("\nSTAMPED. db.job_facts_ready() is now true; load_jobs merges job_facts.")
        return 0

    src = _source_rows()
    mirrored = {r["url"] for r in db._fetch_all(db.JOB_FACTS_TABLE, {"select": "url"})
                if r.get("url")}
    todo = sorted(set(src) - mirrored)
    print("jobs with derived values: %d   already mirrored: %d   remaining: %d"
          % (len(src), len(mirrored), len(todo)))

    if not a.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return 0

    t0 = time.time()
    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        # Through the same mirror the live writers use, so the backfill cannot disagree with
        # steady-state traffic about which columns move or how they are keyed.
        db.mirror_job_facts([src[u] for u in batch])
        if (i // BATCH) % 5 == 0:
            print("  %6d/%d copied (%.0fs)" % (min(i + BATCH, len(todo)), len(todo),
                                               time.time() - t0))
    print("\ncopied %d row(s) in %.0fs. Re-run until 0 remaining, then --verify, then --stamp."
          % (len(todo), time.time() - t0))
    return 0


def verify():
    """Every derived value in `jobs` must equal the one in job_facts, column for column."""
    print("verifying both stores, column for column...")
    src = _source_rows()
    new = db._facts_rows()
    missing = collections.Counter()
    differ = collections.Counter()
    examples = []
    for u, r in src.items():
        got = new.get(u)
        if got is None:
            missing["row"] += 1
            continue
        for c in db.JOB_FACTS_COLS:
            if _norm(c, r.get(c)) != _norm(c, got.get(c)):
                differ[c] += 1
                if len(examples) < 8:
                    examples.append((c, u, r.get(c), got.get(c)))
    print("\n  rows with derived values : %d" % len(src))
    print("  rows in job_facts        : %d" % len(new))
    print("  absent from the copy     : %d" % missing["row"])
    print("  present but DIFFERENT    : %d  %s" % (sum(differ.values()), dict(differ)))
    for c, u, a_, b_ in examples:
        print("     %-14s jobs=%-14r facts=%-14r %s" % (c, a_, b_, u[:60]))
    ok = not (missing["row"] or sum(differ.values()))
    print("\n%s" % ("VERIFIED — the two stores agree." if ok else "MISMATCH — do not stamp."))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
