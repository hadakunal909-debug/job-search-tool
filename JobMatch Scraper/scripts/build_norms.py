#!/usr/bin/env python3
"""build_norms.py — count what a ROLE usually asks for and which TOOLS an EMPLOYER leans on.

Writes norms.json, which norms.py reads. This script only COUNTS; the ranking lives in norms.py
so the --show output below and what a reader sees on /job cannot drift apart.

THREE CORRECTIONS THE MEASUREMENT FORCED, each of which produced garbage without it:

1. EMPLOYER-TEMPLATE CAP. One employer held 328 of the 1,822 `systems` postings, which puts its
   own boilerplate above the family floor and turns its template into "what the role asks for".
   No employer may contribute more than norms.EMPLOYER_SHARE of a family's rows.
2. THE COMPANY LAYER STORES TOOLS ONLY (core.ATS_TOOLS). Over every term, "what is unusual about
   this employer's postings" is answered correctly and uselessly -- Northrop Grumman
   "employees 94%", JPMorgan "capabilities 69%", Deloitte "clients 72%", Amazon "onboarding 97%",
   every one a paragraph repeated in every posting. Restricted to the curated tools half it
   becomes Northrop "sap 31% against 4% expected", Capital One "nosql 43% against 5%".
3. THE COMPANY BASELINE IS THE EMPLOYER'S OWN ROLE MIX (stored as `fam`), not the corpus -- a
   company hiring mostly engineers uses more git than average, and calling that a fact about the
   company would just re-describe who they hire.

Run it by hand after a scoring pass, then commit norms.json: the server runs no build step, so
whatever is in the repo is what production reads.

    python scripts/build_norms.py                        # reads the live database
    python scripts/build_norms.py --rows rows.json.gz    # a cached pull, for iterating
    python scripts/build_norms.py --save-rows rows.json.gz
    python scripts/build_norms.py --show ops
    python scripts/build_norms.py --check
"""
import argparse
import collections
import datetime
import gzip
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core
import db
import norms

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(APP, "norms.json")

# STORAGE floors only -- anything rarer than this cannot clear norms.py's reading floors, so
# keeping it would grow the file without changing a single answer.
KEEP_FAM_DF = 8
KEEP_CORPUS_DF = 20


def _open(path, mode):
    return (gzip.open if path.endswith(".gz") else open)(path, mode, encoding="utf-8")


def _rows_from_db():
    rows = db.load_jobs(cols="url,title,company,jd_terms") or []
    return [{"t": r.get("title") or "", "c": r.get("company") or "", "j": r.get("jd_terms") or ""}
            for r in rows if r.get("url")]


def build(rows):
    """rows -> (blob, uncapped family sizes)."""
    parsed = []
    for r in rows:
        if not r["j"]:
            continue
        terms = frozenset(core.unpack_analyzed(r["j"]).get("terms") or [])
        if terms:
            parsed.append((core.roles_for_title(r["t"]), db.block_key(r["c"]), r["c"], terms))
    n = len(parsed)

    corpus = collections.Counter()
    fam_rows = collections.Counter()
    for keys, _ck, _cn, terms in parsed:
        corpus.update(terms)
        for k in keys:
            fam_rows[k] += 1

    cap = {k: max(1, int(norms.EMPLOYER_SHARE * v)) for k, v in fam_rows.items()}
    taken = collections.Counter()
    fam_n = collections.Counter()
    fam_df = collections.defaultdict(collections.Counter)
    for keys, ck, _cn, terms in parsed:
        for k in keys:
            if taken[(k, ck)] >= cap[k]:
                continue
            taken[(k, ck)] += 1
            fam_n[k] += 1
            fam_df[k].update(terms)

    co_n = collections.Counter()
    co_name, co_fam, co_tools = {}, collections.defaultdict(collections.Counter), \
        collections.defaultdict(collections.Counter)
    for keys, ck, cn, terms in parsed:
        if not ck:
            continue
        co_n[ck] += 1
        co_name.setdefault(ck, cn)
        co_tools[ck].update(terms & core.ATS_TOOLS)
        for k in keys:
            co_fam[ck][k] += 1

    blob = {
        "_meta": {
            "built": datetime.date.today().isoformat(),
            "postings": n,
            # Asserted equal to core.ROLE_KEYS by scripts/test_norms.py: editing ROLE_FAMILIES
            # silently changes every family size and every share, so it must force a rebuild
            # rather than quietly re-weighting what a reader is told.
            "role_keys": list(core.ROLE_KEYS),
            "min_family": norms.MIN_FAMILY, "employer_share": norms.EMPLOYER_SHARE,
            "fam_floor": norms.FAM_FLOOR, "corpus_df_min": norms.CORPUS_DF_MIN,
            "min_employer": norms.MIN_EMPLOYER,
            "note": "scripts/build_norms.py. Prevalence difference, not lift. Tools-only per company.",
        },
        "corpus": {"n": n, "df": {t: c for t, c in corpus.items() if c >= KEEP_CORPUS_DF}},
        "fam": {k: {"n": fam_n[k],
                    "df": {t: c for t, c in fam_df[k].items() if c >= KEEP_FAM_DF}}
                for k in fam_n if fam_rows[k] >= norms.MIN_FAMILY},
        "co": {ck: {"n": cn, "name": co_name.get(ck, ck), "fam": dict(co_fam[ck]),
                    "tools": dict(co_tools[ck])}
               for ck, cn in co_n.items() if cn >= norms.MIN_EMPLOYER},
    }
    return blob, fam_rows


def check(path):
    if not os.path.exists(path):
        print("no %s -- run scripts/build_norms.py" % os.path.basename(path))
        return 1
    blob = json.load(open(path, encoding="utf-8"))
    meta = blob.get("_meta") or {}
    bad = []
    if list(meta.get("role_keys") or []) != list(core.ROLE_KEYS):
        bad.append("ROLE_FAMILIES has changed since this was built -- rebuild it")
    for k, fam in (blob.get("fam") or {}).items():
        if k not in core.ROLE_KEYS:
            bad.append("unknown family %r" % k)
        if fam.get("n", 0) < norms.MIN_FAMILY:
            bad.append("%s kept below MIN_FAMILY" % k)
        for term, _pf, _pc in norms.role_norm(k, 40, blob=blob):
            if term in core.PLACE_TERMS or term in core.ELIGIBILITY_TERMS:
                bad.append("the %s norm offers %r" % (k, term))
    for ck, co in (blob.get("co") or {}).items():
        for term in co.get("tools") or {}:
            if term not in core.ATS_TOOLS:
                bad.append("%s stores a non-tool %r" % (ck, term))
                break
    if bad:
        print("FAILED: " + "; ".join(sorted(set(bad))[:6]))
        return 1
    print("ok: %d postings, %d families, %d employers, built %s"
          % (meta.get("postings"), len(blob.get("fam") or {}),
             len(blob.get("co") or {}), meta.get("built")))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", help="a cached pull instead of the database")
    ap.add_argument("--save-rows", help="write the pull here for re-use")
    ap.add_argument("--show", help="print one family's norm and stop")
    ap.add_argument("--company", help="print one employer's tools and stop")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args()

    if a.check:
        return check(a.out)

    if a.rows:
        with _open(a.rows, "rt") as f:
            rows = json.load(f)
        src = a.rows
    else:
        rows = _rows_from_db()
        src = db.backend_name()
    print("%d rows (%s)" % (len(rows), src))
    if a.save_rows:
        with _open(a.save_rows, "wt") as f:
            json.dump(rows, f)
        print("saved the pull to %s" % a.save_rows)

    blob, fam_rows = build(rows)

    if a.show:
        for t, pf, pc in norms.role_norm(a.show, 14, blob=blob):
            print("   %-28s %5.1f%% of %s vs %4.1f%% corpus" % (t, 100 * pf, a.show, 100 * pc))
        return 0
    if a.company:
        ck = db.block_key(a.company)
        top, unusual = norms.company_tools(ck, blob=blob)
        print("   %s (%s)" % (a.company, ck))
        print("   most used: " + " · ".join("%s %.0f%%" % (t, 100 * s) for t, s in top))
        print("   unusual  : " + " · ".join("%s %.0f%% vs %.0f%% expected"
                                            % (t, 100 * s, 100 * e) for t, s, e in unusual))
        return 0

    json.dump(blob, open(a.out, "w", encoding="utf-8"))
    print("\nwrote %s  (%.0f KB)" % (os.path.basename(a.out), os.path.getsize(a.out) / 1024.0))
    print("  %d postings, %d families kept of %d, %d employers"
          % (blob["_meta"]["postings"], len(blob["fam"]), len(fam_rows), len(blob["co"])))
    small = sorted((v, k) for k, v in fam_rows.items() if v < norms.MIN_FAMILY)
    if small:
        print("  below MIN_FAMILY=%d, so no norm: %s"
              % (norms.MIN_FAMILY, ", ".join("%s(%d)" % (k, v) for v, k in small)))
    print("\nwhat these roles usually ask for:")
    for k in sorted(blob["fam"], key=lambda x: -blob["fam"][x]["n"])[:5]:
        got = norms.role_norm(k, 8, blob=blob)
        print("  %-12s %s" % (k, " · ".join("%s %.0f%%" % (t, 100 * pf) for t, pf, _pc in got)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
