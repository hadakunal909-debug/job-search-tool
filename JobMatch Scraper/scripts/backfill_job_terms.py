#!/usr/bin/env python3
"""backfill_job_terms.py — copy jobs.jd_terms into public.job_terms, resumably.

    python scripts/backfill_job_terms.py            # how much is left
    python scripts/backfill_job_terms.py --apply    # copy, in batches
    python scripts/backfill_job_terms.py --verify   # compare both stores, byte for byte
    python scripts/backfill_job_terms.py --stamp    # declare the table authoritative

Same gate as the other two backfills: db.job_terms_ready() reads data_versions['job_terms'],
nothing believes the new table until it is stamped, and --stamp refuses while anything differs.

WHY BYTE-FOR-BYTE AND NOT "both present". jd_terms is a PACKED STRING whose KEY ORDER is
load-bearing twice over, and both failures are silent. score_jobs diffs the stored value against
the one it just built to decide whether to write, so a value that round-tripped through anything
order-normalising would diff as changed on every row of every run -- the whole-corpus re-upsert
COLS_SCORE's docstring warns about. And the order IS analyze_jd's frozen term order, which breaks
ties between equal-weight terms in the skill lists on /job. A copy that is "equivalent" is not
good enough; it has to be identical.

READ IN BATCHES BY URL rather than pulling the column corpus-wide. It is 38.7 MB and this runs
against a box whose Passenger workers share a ~1.2 GB account cap while serving the site.
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

BATCH = 300


def _source(urls):
    """{url: {jd_terms, facts_fp}} from `jobs` for these urls.

    THE STAMP COMES WITH IT. Selecting the text alone would have the mirror write
    facts_fp NULL for the entire corpus -- throwing away, on the one pass whose job is
    to populate this table, the exact fact the column exists to carry.
    """
    out = {}
    for batch in db._url_batches(list(urls)):
        rows = db._fetch_all(db.TABLE, {"select": "url,jd_terms,facts_fp",
                                        "url": db._in_list(batch)})
        for r in rows:
            if r.get("url"):
                out[r["url"]] = {"jd_terms": r.get("jd_terms") or "",
                                 "facts_fp": r.get("facts_fp")}
    return out


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
        db._fetch_all(db.JOB_TERMS_TABLE, {"select": "url", "limit": 1})
    except Exception as e:
        if not db._table_missing(e):
            raise
        print("public.job_terms does not exist. Run MIGRATION_job_terms.sql.")
        return 2

    # Which rows HAVE an analysis, url-only so this stays cheap.
    with_terms = {r["url"] for r in db._fetch_all(db.TABLE, {"select": "url",
                                                            "jd_terms": "not.is.null"})
                  if r.get("url")}
    mirrored = {r["url"] for r in db._fetch_all(db.JOB_TERMS_TABLE, {"select": "url",
                                                                     "n_terms": "gt.0"})
                if r.get("url")}
    todo = sorted(with_terms - mirrored)
    print("jobs with an analysis: %d   already mirrored: %d   remaining: %d"
          % (len(with_terms), len(mirrored), len(todo)))

    if a.verify or a.stamp:
        rc = verify(with_terms)
        if not a.stamp or rc:
            return rc
        if todo:
            print("\nREFUSING to stamp: %d row(s) still missing." % len(todo))
            return 1
        db.set_data_version("job_terms", "backfilled-%s" % time.strftime("%Y%m%d"))
        print("\nSTAMPED. db.job_terms_ready() is now true; load_jobs merges job_terms, and")
        print("jobs_fingerprint counts THIS table -- so the row cache will rebuild once.")
        return 0

    if not a.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return 0

    t0 = time.time()
    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        src = _source(batch)
        # Through the same mirror the live writers use, so a backfilled row and a freshly
        # written one cannot disagree about shape, keying or the n_terms convention.
        db.mirror_job_terms([{"url": u, "jd_terms": v["jd_terms"],
                              "facts_fp": v["facts_fp"]} for u, v in src.items()])
        if (i // BATCH) % 5 == 0:
            print("  %6d/%d copied (%.0fs)" % (min(i + BATCH, len(todo)), len(todo),
                                               time.time() - t0))
    print("\ncopied %d row(s) in %.0fs. Re-run until 0 remaining, then --verify, then --stamp."
          % (len(todo), time.time() - t0))
    return 0


def verify(with_terms=None):
    """Every packed string must be byte-identical in both stores."""
    print("verifying both stores, byte for byte...")
    if with_terms is None:
        with_terms = {r["url"] for r in db._fetch_all(db.TABLE, {"select": "url",
                                                                 "jd_terms": "not.is.null"})
                      if r.get("url")}
    urls = sorted(with_terms)
    missing = differ = checked = 0
    examples = []
    t0 = time.time()
    for i in range(0, len(urls), BATCH):
        batch = urls[i:i + BATCH]
        old = _source(batch)
        new = db._terms_rows(batch)
        for u, rec in old.items():
            packed = rec["jd_terms"]
            if not packed:
                continue
            checked += 1
            if u not in new:
                missing += 1
            elif new[u] != packed:
                differ += 1
                if len(examples) < 5:
                    examples.append((u, len(packed), len(new[u])))
        if (i // BATCH) % 10 == 0:
            print("  %6d/%d (%.0fs)" % (min(i + BATCH, len(urls)), len(urls), time.time() - t0))
    print("\n  analyses checked     : %d" % checked)
    print("  absent from the copy : %d" % missing)
    print("  present but DIFFERENT: %d" % differ)
    for u, a_, b_ in examples:
        print("     jobs=%5d chars  job_terms=%5d chars  %s" % (a_, b_, u[:70]))
    ok = not (missing or differ)
    print("\n%s" % ("VERIFIED — the two stores agree byte for byte."
                    if ok else "MISMATCH — do not stamp."))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
