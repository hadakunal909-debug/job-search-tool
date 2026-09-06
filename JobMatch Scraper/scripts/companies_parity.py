#!/usr/bin/env python3
"""companies_parity.py — a card built from public.companies must equal one built from the files.

    python scripts/companies_parity.py            # every employer in the corpus
    python scripts/companies_parity.py --rows 400 # ...and full feed rows for a sample

This is the gate the revamp plan calls "0 diffs before any read switches". web.company_facts has
two paths -- the table when there is a row, the original file lookups when there is not -- and
they are only allowed to exist together because they agree. Left unchecked this is the same
coupling that already forces _filter_rows / matches() / prefs_match to be kept in sync by hand,
and scripts/feed_parity.py exists because keeping them in sync by hand did not work.

It compares TWO WAYS, because they fail differently:

  1. per employer, company_facts(name) with the table vs with the table forced empty. Catches a
     builder that resolved a fact differently from the request path -- a normalisation mismatch,
     a logo alias that only the manifest knows, a sponsor tier boundary.
  2. per JOB ROW, the whole _build_row output both ways. Catches everything in (1) plus the
     wiring: a field read from the wrong key, a tuple that became a list on its way through
     JSON, visa_bits round-tripping to a different route order.

(2) is the one that matters and (1) is the one that tells you WHERE, so both run.
"""
import argparse
import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("EV_OFF", "1")
os.environ.setdefault("APP_SECRET", "companies-parity-not-a-session-key")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core
import db
import web


class _NoTable(object):
    """Force company_facts down its fallback path without touching the database.

    Swapping the MEMO rather than stubbing db.load_companies matters: companies_table() consults
    the memo first, so a db-level stub would be bypassed on the second call and the comparison
    would quietly become table-vs-table.
    """

    def __enter__(self):
        self.saved = dict(web._companies_memo)
        web._companies_memo.update(ver="", by_key={}, at=9e18, ver_ok=True)
        return self

    def __exit__(self, *a):
        web._companies_memo.clear()
        web._companies_memo.update(self.saved)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=400, help="how many full feed rows to compare")
    a = ap.parse_args()

    tbl = web.companies_table()
    if not tbl:
        print("public.companies is empty or not migrated -- nothing to compare.")
        print("Run MIGRATION_companies.sql, then scripts/build_companies_table.py --apply.")
        return 2
    print("companies table: %d employer(s), version %s"
          % (len(tbl), web._companies_memo.get("ver") or "(none)"))

    jobs = db.load_jobs(cols="url,title,company,location,first_seen,is_active,"
                             "sponsors_h1b,match_score,exp_max_years,sponsor_jd,sponsor_reason")
    active = [r for r in jobs if r.get("is_active") in (None, True, "true", "t", 1)]
    names = sorted({(r.get("company") or "").strip() for r in active if (r.get("company") or "").strip()})
    print("employers in the live corpus: %d" % len(names))

    # ---- 1. fact by fact ------------------------------------------------------------------
    with _NoTable():
        from_files = {n: web.company_facts(n) for n in names}
    from_table = {n: web.company_facts(n) for n in names}

    # A DIFFERENCE IS NOT AUTOMATICALLY A FAILURE, and conflating the two would either block
    # a real improvement or wave through a real loss.
    #
    # Keying employer facts on the NORMALISED name is the point of the table: every spelling
    # of one employer now gets the same answer. Where the file path resolved nothing for a
    # spelling carrying a legal suffix -- 'Apple Inc', 'Accenture LLP', 'Micron Technology'
    # (20 postings) -- the table supplies the logo that employer actually has. That is the
    # feature, not a discrepancy.
    #
    # What is NOT allowed is the table having LESS: a value that used to resolve and now does
    # not, or a different value where both are present. Measured on the first run: 11 of the
    # former and 2 of the latter (EchoStar, Lonza), and only those 2 were bugs.
    gained = collections.Counter()
    lost = collections.Counter()
    changed = collections.Counter()
    examples = collections.defaultdict(list)
    missing = 0
    for n in names:
        if core.norm_company(n) not in tbl:
            missing += 1
            continue                      # a row the builder has not seen yet: falls back
        f, t = from_files[n], from_table[n]
        for k in sorted(f):
            if f[k] == t[k]:
                continue
            if not f[k] and t[k]:
                gained[k] += 1
                bucket = 'gained'
            elif f[k] and not t[k]:
                lost[k] += 1
                bucket = 'LOST'
            else:
                changed[k] += 1
                bucket = 'CHANGED'
            if len(examples[bucket + ':' + k]) < 4:
                examples[bucket + ':' + k].append((n, f[k], t[k]))
    print()
    print("  employers with no row yet (fall back, not a failure): %d" % missing)
    print("  employers compared                                  : %d" % (len(names) - missing))
    print()
    print("  GAINED (table answers where the files did not): %d  %s"
          % (sum(gained.values()), dict(gained)))
    print("  LOST   (files answered, table does not)       : %d  %s"
          % (sum(lost.values()), dict(lost)))
    print("  CHANGED(both answer, differently)             : %d  %s"
          % (sum(changed.values()), dict(changed)))
    for bucket, rows_ in sorted(examples.items()):
        if bucket.startswith('gained'):
            continue                      # improvements are counted, not itemised
        print()
        print("  %s:" % bucket)
        for n, fv, tv in rows_:
            print("     %-28s files=%r  table=%r" % (n[:28], fv, tv))
    bad = collections.Counter()
    bad.update(lost)
    bad.update(changed)
    if not bad:
        print()
        print("  nothing lost and nothing changed.")

    # ---- 2. whole rows --------------------------------------------------------------------
    sample = active[:a.rows]
    with _NoTable():
        rows_files = [web._build_row(j, 0) for j in sample]
    rows_table = [web._build_row(j, 0) for j in sample]
    # Same three-way rule as above. A card that now shows a logo where it showed a monogram
    # is the improvement this table exists for; a card that LOST one is a regression.
    rowbad = collections.Counter()
    rowgain = collections.Counter()
    for rf, rt in zip(rows_files, rows_table):
        for k in sorted(rf):
            if rf.get(k) == rt.get(k):
                continue
            if not rf.get(k) and rt.get(k):
                rowgain[k] += 1
            else:
                rowbad[k] += 1
    print()
    print("  full rows compared: %d" % len(sample))
    print("  gained on the card: %d  %s" % (sum(rowgain.values()), dict(rowgain)))
    if rowbad:
        print("  ROW REGRESSIONS  : %d  %s" % (sum(rowbad.values()), dict(rowbad)))
    else:
        print("  no row lost or changed a value.")

    total = sum(bad.values()) + sum(rowbad.values())
    print("\n%s" % ("PARITY OK" if not total else "PARITY FAILED (%d difference(s))" % total))
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
