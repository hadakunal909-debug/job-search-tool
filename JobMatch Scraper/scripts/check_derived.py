#!/usr/bin/env python3
"""check_derived.py — which rows' derived columns are readings of a text we no longer store?

    python scripts/check_derived.py                 # counts, and a breakdown by arrival day
    python scripts/check_derived.py --sample 20     # ...plus example rows
    python scripts/check_derived.py --csv out.csv   # every affected url, for a repair run
    python scripts/check_derived.py --requeue       # re-queue them so the next pass re-reads

THE QUESTION. exp_max_years, jd_terms, sponsor_jd and sponsor_reason are readings OF a specific
description. A description is normally immutable -- score_jobs queues a fetch only when the `jd`
column is empty -- so those readings are normally true. Three paths replace a description anyway:
the sweep storing a listing JD onto a posting it already holds, the two repair scripts, and the
extension's import. When that happens the readings stay, and nothing notices.

On 2026-09-04 that put nine postings asking 3 to 10 years into a "0 to 2 Years" search and hid
117 genuinely entry-level ones from it. Finding them took a 200-second scan that re-read every
description over the network. This script asks the same question by comparing two 32-character
columns, because MIGRATION_jd_fingerprints.sql made it a property of the row.

WHY THIS IS A SCRIPT AND NOT A TEST. It needs the live database, and a suite that finds nothing
because it is not connected is a green tick that proves nothing -- the mistake that kept the
extension contract test outside CI for months. The comparison itself IS unit-tested, in
test_jd_fingerprints.py, against fixtures that need no database.

WHY IT COMPARES IN PYTHON. The honest form of this is one line of SQL:

    select url from public.jobs where jd_fp is distinct from facts_fp;

`is distinct from` has no spelling in the PostgREST subset pgrest.py translates (see its `_OPS`),
and the proxy is the only route to this database from a laptop or from Actions. So the two
columns are read -- 47k rows at ~80 bytes is under 4 MB, against the ~55 MB the scan it replaces
had to pull -- and compared here. Run the SQL directly if you have a psql prompt; the partial
index jobs_facts_stale_idx is there for it.
"""
import argparse
import collections
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("EV_OFF", "1")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import db


# THE THREE STATES, kept apart because they want different actions and only one of them is a bug.
#
# A NULL facts_fp is NOT a mismatch. It means no scoring pass has read this row since the column
# existed -- true of the whole corpus the day the migration runs, and of every row fetched since
# the last pass. Reporting that as "lying to us" would make the first run of this script return
# 47,000 rows and teach everyone to ignore it.
STALE = "stale"          # both stamped, and they disagree -> the reading is about another text
UNKNOWN = "unknown"      # we hold text but no pass has stamped it -> not yet a claim either way
NOJD = "no-jd"           # no description at all -> nothing to read, nothing to be stale


def classify(row):
    """One row -> STALE / UNKNOWN / NOJD / None (None meaning 'agrees')."""
    jd_fp = (row.get("jd_fp") or "") or None
    facts_fp = (row.get("facts_fp") or "") or None
    if jd_fp is None:
        # No stored description. facts_fp should be NULL too; if it is not, the row was read
        # from a text that has since been cleared (close_dead_jds does exactly that), and its
        # readings are about a document the table no longer has. That IS stale.
        return NOJD if facts_fp is None else STALE
    if facts_fp is None:
        return UNKNOWN
    return None if jd_fp == facts_fp else STALE


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0, help="print N example stale rows")
    ap.add_argument("--csv", help="write every stale url to this file")
    ap.add_argument("--requeue", action="store_true",
                    help="clear match_score on the stale rows so the next pass re-reads them")
    a = ap.parse_args()

    if not db.has_remote_db():
        print("No database configured. Set PG_DSN, or DB_PROXY_URL + DB_PROXY_SECRET.")
        return 2

    # PROBE FIRST, one row, because db.load_jobs' cols= path is best-effort: a select naming a
    # column that has not been migrated does not raise, it falls back to a wider one. Without
    # this the whole corpus would come back with neither fingerprint present, every row would
    # classify as "no description at all", and the report would confidently say 100% of a
    # healthy corpus was fine when in fact the columns did not exist. A tool whose failure mode
    # is a reassuring number is worse than one that crashes.
    try:
        db._fetch_all(db.TABLE, {"select": "url,jd_fp,facts_fp", "limit": 1})
    except Exception as e:
        if not (db._column_missing(e, "jd_fp") or db._column_missing(e, "facts_fp")):
            raise
        print("jobs.jd_fp / jobs.facts_fp do not exist yet.")
        print("Run MIGRATION_jd_fingerprints.sql once in the SQL editor, then re-run this.")
        print("Nothing is wrong with the data -- there is simply nothing to compare yet.")
        return 2

    # url + two fingerprints + the two fields the report groups by. Narrow on purpose: this
    # script exists because the audit it replaces cost ~55 MB, and it would be an odd tool that
    # spent the saving on its own report.
    rows = db.load_jobs(cols="url,company,first_seen,is_active,jd_fp,facts_fp")
    active = [r for r in rows if r.get("is_active") in (None, True, "true", "t", 1)]
    print("rows: %d total, %d active  (backend: %s)" % (len(rows), len(active), db.backend_name()))

    buckets = collections.defaultdict(list)
    for r in active:
        buckets[classify(r)].append(r)

    n = len(active) or 1
    ok = len(buckets[None])
    print("\n  agree (the reading is about the text we hold) : %6d  %5.1f%%" % (ok, 100.0 * ok / n))
    print("  no description at all                        : %6d  %5.1f%%"
          % (len(buckets[NOJD]), 100.0 * len(buckets[NOJD]) / n))
    print("  not stamped yet (no pass has read them)      : %6d  %5.1f%%"
          % (len(buckets[UNKNOWN]), 100.0 * len(buckets[UNKNOWN]) / n))
    stale = buckets[STALE]
    print("  STALE -- reading is of a text we replaced    : %6d  %5.1f%%"
          % (len(stale), 100.0 * len(stale) / n))

    if buckets[UNKNOWN] and not stale:
        print("\n  Nothing is stale. The unstamped rows are not a defect -- they are rows no")
        print("  scoring pass has read since the fingerprint columns were added. A full pass")
        print("  (python -m scraper.score_jobs) stamps every readable row.")

    if stale:
        byday = collections.Counter((r.get("first_seen") or "?")[:10] for r in stale)
        print("\n  stale rows by arrival day (worst 10):")
        for d, c in byday.most_common(10):
            print("     %s  %5d" % (d, c))
        byco = collections.Counter(r.get("company") or "?" for r in stale)
        print("\n  ...and by employer (worst 8):")
        for co, c in byco.most_common(8):
            print("     %-34s %5d" % (co[:34], c))

    for r in stale[:a.sample]:
        print("     %-26s %s" % ((r.get("company") or "")[:26], r["url"][:96]))

    if a.csv and stale:
        with open(a.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["url", "company", "first_seen", "jd_fp", "facts_fp"])
            for r in stale:
                w.writerow([r["url"], r.get("company"), r.get("first_seen"),
                            r.get("jd_fp"), r.get("facts_fp")])
        print("\nwrote %d row(s) to %s" % (len(stale), a.csv))

    if a.requeue and stale:
        # db.requeue_analysis clears match_score, which score_jobs already reads as "no run has
        # ever scored this row" -- so the next pass re-reads the description and _persist_derived
        # rewrites every derived column AND the stamp. Nothing else is touched: blanking jd_terms
        # would make the feed render "JD pending" for a row holding a good description.
        n_q = db.requeue_analysis([r["url"] for r in stale])
        print("\nre-queued %d row(s); the next scrape re-reads them." % n_q)
    elif a.requeue:
        print("\nnothing to re-queue.")

    # Exit code so this can gate a scheduled run: 1 means "the corpus is lying about itself".
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
