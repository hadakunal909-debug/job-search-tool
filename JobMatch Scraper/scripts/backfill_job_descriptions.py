#!/usr/bin/env python3
"""backfill_job_descriptions.py — copy jobs.jd into public.job_descriptions, resumably.

    python scripts/backfill_job_descriptions.py             # how much is left
    python scripts/backfill_job_descriptions.py --apply     # copy, in batches
    python scripts/backfill_job_descriptions.py --verify    # compare both stores, row for row
    python scripts/backfill_job_descriptions.py --stamp     # declare the table authoritative

THE STAMP IS THE SWITCH, and it is deliberately a separate step. db.jd_table_ready() reads
data_versions['job_descriptions'] and every reader in db.py keys off it: until it is set, jobs.jd
is the source of truth and this table is a shadow that update_jds keeps current. Once it is set,
get_job_jd / urls_with_jd / urls_missing_jd / load_jobs all read the new table instead.

A HALF-BACKFILLED TABLE IS WORSE THAN NO TABLE, which is why the flip cannot happen by accident.
A url absent from job_descriptions is indistinguishable from a job that has no description, so
reading from a partial copy would put thousands of already-fetched postings back into the scrape's
fetch queue -- and score_jobs would re-fetch text it already had, against hosts that rate-limit.
So: --apply until nothing is left, --verify until it is clean, and only then --stamp.

IT COPIES IN BATCHES for a reason that is not politeness. The column is 263 MB across ~46,850
rows and the box has a ~1.2 GB account-wide memory cap shared with the Passenger workers serving
the site. One statement that materialises the whole column is how a scrape gets SIGKILLed at
rc=137, which this project has already had happen on four of six score-step runs.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("EV_OFF", "1")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import db

BATCH = 200          # urls fetched and written per round trip pair


def _todo():
    """URLs whose text is in jobs.jd but not yet in job_descriptions."""
    have_text = db.urls_with_jd()                       # reads jobs.jd while unstamped
    try:
        mirrored = {r["url"] for r in db._fetch_all(db.JD_TABLE, {"select": "url"})
                    if r.get("url")}
    except Exception as e:
        if db._table_missing(e):
            print("public.job_descriptions does not exist. Run MIGRATION_job_descriptions.sql.")
            sys.exit(2)
        raise
    return sorted(have_text - mirrored), len(have_text), len(mirrored)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--stamp", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="stop after N rows (for a first run)")
    a = ap.parse_args()

    if not db.has_remote_db():
        print("No database configured. Set PG_DSN, or DB_PROXY_URL + DB_PROXY_SECRET.")
        return 2
    if db.jd_table_ready():
        print("NOTE: job_descriptions is already stamped authoritative. urls_with_jd() now reads")
        print("      THAT table, so 'remaining' below is a comparison of the new store with")
        print("      itself and will read 0 whatever jobs.jd holds. Use --verify.")

    if a.verify:
        return verify()

    todo, n_text, n_mirror = _todo()
    print("jobs with text: %d   already mirrored: %d   remaining: %d"
          % (n_text, n_mirror, len(todo)))

    if a.stamp:
        if todo:
            print("\nREFUSING to stamp: %d row(s) are still missing." % len(todo))
            print("A partial table read as authoritative puts already-fetched postings back")
            print("into the scrape's fetch queue. Finish --apply, then --verify, then --stamp.")
            return 1
        rc = verify()
        if rc:
            return rc
        db.set_data_version("job_descriptions", "backfilled-%s" % time.strftime("%Y%m%d"))
        print("\nSTAMPED. db.jd_table_ready() is now true; readers use job_descriptions.")
        return 0

    if not a.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return 0

    if a.limit:
        todo = todo[:a.limit]
    # READ THROUGH _fetch_all, NOT load_jobs_by_urls, and the difference is the whole reason
    # the first run of this script lied. load_jobs_by_urls SKIPS a failed batch rather than
    # raising -- correct for the digest and the scorer, where losing a few baseline scores
    # beats taking the run down -- so a transient proxy failure silently dropped 200 rows and
    # the loop carried on. Measured on the first run: 25,376 of 46,903 copied, scattered gaps
    # after a 19,801-row clean prefix, and the script exited 0 reporting success.
    #
    # A backfill has the opposite requirement to a feed: it must notice.
    def _read(batch):
        rows = db._fetch_all(db.TABLE, {"select": "url,jd", "url": db._in_list(batch)})
        return {r["url"]: (r.get("jd") or "") for r in rows
                if r.get("url") and (r.get("jd") or "")}

    t0 = time.time()
    total_done = 0
    # PASSES, because the failures this is guarding against are transient by nature -- the
    # proxy has been observed answering 200 with HTML. Each pass recomputes what is left, so
    # a pass that dropped rows is simply followed by one that picks them up. Bounded, and it
    # stops early when a pass makes no progress: that is a real failure, not a blip, and
    # spinning on it would just hide it again.
    for attempt in range(1, 6):
        moved = 0
        for i in range(0, len(todo), BATCH):
            batch = todo[i:i + BATCH]
            try:
                jds = _read(batch)
            except Exception as ex:
                print("  batch %d failed (%s); will retry in the next pass"
                      % (i // BATCH, str(ex)[:70]))
                continue
            if jds:
                db._mirror_jds([{"url": u, "jd": t} for u, t in jds.items()])
                moved += len(jds)
            if (i // BATCH) % 25 == 0:
                print("  pass %d: %6d/%d (%.0fs)"
                      % (attempt, moved, len(todo), time.time() - t0))
        total_done += moved
        left, _, _ = _todo()
        print("  pass %d done: %d copied, %d still missing" % (attempt, moved, len(left)))
        if not left:
            break
        if not moved:
            print("  a whole pass copied NOTHING -- stopping rather than spinning.")
            break
        todo = left
    left, _, _ = _todo()
    print("")
    print("copied %d description(s) in %.0fs; %d still missing."
          % (total_done, time.time() - t0, len(left)))
    if left:
        print("Re-run --apply; it resumes from what is missing.")
        return 1
    print("Nothing left. Next: --verify, then --stamp.")
    return 0


def verify():
    """Every description in jobs.jd must be byte-identical in job_descriptions."""
    print("verifying both stores, row for row...")
    urls = sorted(u for u in db.existing_urls() if u)
    checked = missing = differ = 0
    t0 = time.time()
    for i in range(0, len(urls), BATCH):
        batch = urls[i:i + BATCH]
        old = {r["url"]: (r.get("jd") or "")
               for r in db._fetch_all(db.TABLE, {"select": "url,jd", "url": db._in_list(batch)})
               if r.get("url")}
        new = db._jd_rows(batch)
        for u, text in old.items():
            if not text:
                continue                      # nothing to mirror
            checked += 1
            if u not in new:
                missing += 1
            elif new[u] != text:
                differ += 1
        if (i // BATCH) % 10 == 0:
            print("  %6d/%d (%.0fs)" % (min(i + BATCH, len(urls)), len(urls), time.time() - t0))
    print("\n  descriptions checked : %d" % checked)
    print("  absent from the copy : %d" % missing)
    print("  present but DIFFERENT: %d" % differ)
    ok = not (missing or differ)
    print("\n%s" % ("VERIFIED — the two stores agree." if ok else "MISMATCH — do not stamp."))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
