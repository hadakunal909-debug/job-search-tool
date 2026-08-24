#!/usr/bin/env python3
"""Find a scrapeable board for each net-new company discover_companies.py turned up.

STAGE 3 of four, and deliberately thin: the probe itself is
scraper.probe_everify_candidates.probe (slug guesses for the JSON-API ATSes, then the
careers-page detect chain, every hit validated by scraper.probe_board), and the output is
written in that module's own COLS order. Stage 4 -- scraper.adopt_everify_boards -- therefore
needs no change at all to consume this: same 12 columns, same meanings.

The only thing this file really decides is WHICH companies to probe and in what order:
in_sources=no, ranked by the sponsor evidence discover_companies already worked out.

REVIEW-FIRST: writes a CSV and nothing else. Adoption stays a separate, explicit step.

    python scripts/probe_discovered.py
    python scripts/probe_discovered.py --csv discovered_companies.csv --workers 12
    python scripts/probe_discovered.py --limit 40 -v            # smoke test
    python scripts/probe_discovered.py --include-bodyshops
    python scripts/probe_discovered.py --min-pm 1               # only real PM volume

Then:
    python -m scraper.adopt_everify_boards --csv discovered_board_probe.csv --dry-run
"""
import argparse
import concurrent.futures
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import scraper
from scraper import find_everify_boards as feb
from scraper import probe_everify_candidates as pec

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import discover_companies as dc

REPORT = "discovered_board_probe.csv"


def load_candidates(path, limit=0, include_bodyshops=False, min_pm=0):
    """The net-new rows, de-duplicated, in the order discover_companies ranked them.

    Keyed on _strict_norm_name, not _norm_name: the aggressive stripper collapses distinct
    employers ("Target Labs INC" -> "target"), and collapsing two companies into one here means
    silently never probing the second.
    """
    if not os.path.exists(path):
        raise SystemExit("no such file: %s (run discover_companies.py first)" % path)
    out, seen = [], set()
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if (r.get("in_sources") or "").strip().lower() == "yes":
                continue
            if not include_bodyshops and (r.get("bodyshop") or "").strip().lower() == "yes":
                continue
            name = (r.get("company") or "").strip()
            key = scraper._strict_norm_name(name)
            if not key or key in seen:
                continue
            try:
                pm = int(r.get("pm_titles_kept") or 0)
            except ValueError:
                pm = 0
            if min_pm and pm < min_pm:
                continue
            seen.add(key)
            try:
                h1b = int(r.get("h1b_filings") or 0)
            except ValueError:
                h1b = 0
            # pec.probe() wants `employer`; the rest are pec.COLS fields carried through so the
            # report reads the same as the E-Verify one. bucket/category record WHERE this lead
            # came from, which is the useful thing to know here -- size/state a posting feed has
            # no opinion about, and a blank is honest.
            out.append({"employer": name,
                        "bucket": "discovered",
                        "category": (r.get("channel") or "").strip(),
                        "size": "",
                        "state": "",
                        "h1b_filings": h1b,
                        "bodyshop": (r.get("bodyshop") or "no").strip(),
                        "_pm": pm,
                        "_stem": (r.get("stem_opt") or "no").strip()})
    # discover_companies already sorted the file; re-apply it so a hand-edited CSV still probes
    # the best leads first when --limit truncates the run.
    out.sort(key=lambda r: (-r["h1b_filings"], r["_stem"] != "yes", -r["_pm"],
                            r["employer"].lower()))
    return out[:limit] if limit else out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=dc.REPORT, help="discover_companies.py output")
    ap.add_argument("--out", default=REPORT)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--min-pm", type=int, default=0,
                    help="require this many title-filter-passing PM postings")
    ap.add_argument("--include-bodyshops", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    cands = load_candidates(a.csv, a.limit, a.include_bodyshops, a.min_pm)
    print("probing %d net-new compan%s from %s"
          % (len(cands), "y" if len(cands) == 1 else "ies", a.csv), flush=True)
    if not cands:
        print("nothing to probe.")
        return 0

    orig = scraper.SESSION
    scraper.SESSION = feb._fast_session()        # no retries, short timeouts
    results = []
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=a.workers) as ex:
            futs = {ex.submit(pec.probe, c): c for c in cands}
            done = 0
            for fut in concurrent.futures.as_completed(futs):
                try:
                    rec = fut.result(timeout=60)
                except Exception:
                    rec = dict(futs[fut], career_page="", ats_type="", job_count="",
                               board_url="", confidence="")
                results.append(rec)
                done += 1
                if rec.get("board_url"):
                    print("HIT  %-34s %-15s %-6s %s"
                          % (rec["employer"][:34], rec["ats_type"], rec["job_count"],
                             str(rec["board_url"])[:52]), flush=True)
                elif a.verbose:
                    print("  -- %-34s %s" % (rec["employer"][:34],
                                             rec.get("career_page") or "nothing"), flush=True)
                if done % 100 == 0:
                    print("  ... %d/%d" % (done, len(cands)), flush=True)
    finally:
        scraper.SESSION = orig

    def keyf(r):
        jc = int(r["job_count"]) if str(r.get("job_count")).isdigit() else 0
        return (0 if r.get("board_url") else 1,
                0 if r.get("confidence") == "high" else 1,
                -r.get("h1b_filings", 0), -jc)
    results.sort(key=keyf)

    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=pec.COLS, extrasaction="ignore")
        w.writeheader()
        for r in results:
            w.writerow({k: r.get(k, "") for k in pec.COLS})

    hits = [r for r in results if r.get("board_url")]
    hi = [r for r in hits if r.get("confidence") == "high"]
    cp_only = sum(1 for r in results if not r.get("board_url") and r.get("career_page"))
    by_ats = {}
    for r in hits:
        by_ats[r["ats_type"]] = by_ats.get(r["ats_type"], 0) + 1
    print("\n=== %d probed -> %d boards (%d high-confidence), %d careers-page-only, %d nothing ==="
          % (len(results), len(hits), len(hi), cp_only, len(results) - len(hits) - cp_only))
    if by_ats:
        print("by ATS:", ", ".join("%s %d" % kv for kv in
                                   sorted(by_ats.items(), key=lambda x: -x[1])))
    # The number that decides whether this whole approach beats the one it replaces. The
    # E-Verify+ run got 3.2% on sponsors and 1.0% on everything else; a hiring-signal-first
    # queue should do markedly better, and if it does not, that is the finding.
    if results:
        print("board-discovery rate : %.1f%%   (E-Verify+ baseline: 3.2%% sponsors / 1.0%% rest)"
              % (100.0 * len(hits) / len(results)))
    print("wrote", os.path.abspath(a.out))
    print("\nnext: python -m scraper.adopt_everify_boards --csv %s --dry-run" % a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
