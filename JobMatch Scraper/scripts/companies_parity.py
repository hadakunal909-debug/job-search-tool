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

    bad = collections.Counter()
    examples = collections.defaultdict(list)
    missing = 0
    for n in names:
        if core.norm_company(n) not in tbl:
            missing += 1
            continue                      # a row the builder has not seen yet: falls back, fine
        f, t = from_files[n], from_table[n]
        for k in sorted(f):
            if f[k] != t[k]:
                bad[k] += 1
                if len(examples[k]) < 4:
                    examples[k].append((n, f[k], t[k]))
    print("\n  employers with no row yet (fall back, not a failure): %d" % missing)
    print("  employers compared                                  : %d" % (len(names) - missing))
    if bad:
        print("\n  FIELD MISMATCHES:")
        for k, c in bad.most_common():
            print("     %-14s %5d" % (k, c))
            for n, fv, tv in examples[k]:
                print("        %-28s files=%r  table=%r" % (n[:28], fv, tv))
    else:
        print("  every field agrees.")

    # ---- 2. whole rows --------------------------------------------------------------------
    sample = active[:a.rows]
    with _NoTable():
        rows_files = [web._build_row(j, 0) for j in sample]
    rows_table = [web._build_row(j, 0) for j in sample]
    rowbad = collections.Counter()
    for rf, rt in zip(rows_files, rows_table):
        for k in sorted(rf):
            if rf.get(k) != rt.get(k):
                rowbad[k] += 1
    print("\n  full rows compared: %d" % len(sample))
    if rowbad:
        print("  ROW MISMATCHES:", dict(rowbad))
    else:
        print("  every key of every row agrees.")

    total = sum(bad.values()) + sum(rowbad.values())
    print("\n%s" % ("PARITY OK" if not total else "PARITY FAILED (%d difference(s))" % total))
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
