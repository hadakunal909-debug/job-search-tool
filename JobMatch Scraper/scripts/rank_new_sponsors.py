#!/usr/bin/env python3
"""
rank_new_sponsors.py — rank employers by recent H-1B approval VOLUME and emit a one-column CSV
for scripts/discover_companies.py --channel csv.

This is stage 0 of the discovery pipeline. The other four already exist and are not reimplemented
here: 1 harvest + 2 screen = scripts/discover_companies.py, 3 probe = scripts/probe_discovered.py,
4 adopt = scraper/adopt_everify_boards.py.

WHY IT READS THE RAW CSVs RATHER THAN sponsor_counts.json. That file is keyed by _norm_name, so
its keys are already lowercased and suffix-stripped: "tata consultancy svcs", "amazon com
services". Those are lookup keys, not names. Feeding them downstream degrades every stage that
works off the spelling — find_everify_boards' slug guesses, probe_migratemate.board_reported_name,
and adopt's grade(). The Hub CSVs still carry the employer as filed, so rank from those.

WHY IT DOES NOT EXCLUDE NAMES WE ALREADY SCRAPE. discover_companies.screen() already joins
against BOTH scraper.SOURCES and the boards table (via find_everify_boards._known_sources and
classify_everify.build_sources_matcher), and probe_discovered.load_candidates drops
in_sources == "yes". Doing a half-version of that join here — SOURCES but not the boards table —
would re-probe boards that were adopted last month. Rank here, filter there.

What it DOES drop is body shops and unusable names, because those are never worth a probe slot
however much they file: the top of any H-1B volume ranking is staffing firms.

    python scripts/rank_new_sponsors.py --top 2000 --out top_sponsors.csv

Then:

    python scripts/discover_companies.py --channel csv --in top_sponsors.csv --out disc.csv
    python scripts/probe_discovered.py --csv disc.csv --out disc_probe.csv --workers 12
    python -m scraper.adopt_everify_boards --csv disc_probe.csv --added-by sponsors:2026-08 \\
           --out disc_adoption.csv --dry-run
"""
import argparse
import collections
import csv
import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core
from scraper.build_sponsor_counts import DEFAULT_DIRS, parse_years


def _num(v):
    s = str(v or "").replace(",", "").strip()
    if not s:
        return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def read_raw(directory, years):
    """{employer_as_filed: approvals} over the window, keeping the ORIGINAL spelling."""
    tot = collections.Counter()
    files = 0
    for path in sorted(glob.glob(os.path.join(directory, "*.csv"))):
        files += 1
        fy_from_name = re.search(r"(20\d\d)", os.path.basename(path))
        with open(path, newline="", encoding="utf-8", errors="ignore") as f:
            r = csv.DictReader(f)
            cols = {(c or "").strip().lower(): c for c in (r.fieldnames or [])}
            emp = cols.get("employer")
            if not emp:
                print("  ! %s has no Employer column - skipped" % os.path.basename(path))
                continue
            fy_c = cols.get("fiscal year")
            appr = [cols[k] for k in cols if k.endswith("approval") or k.endswith("approvals")]
            for row in r:
                name = (row.get(emp) or "").strip()
                if not name:
                    continue
                fy = _num(row.get(fy_c)) if fy_c else 0
                if not fy and fy_from_name:
                    fy = int(fy_from_name.group(1))
                if years and fy not in years:
                    continue
                n = sum(_num(row.get(c)) for c in appr)
                if n > 0:
                    tot[name] += n
    print("  read %d file(s); %s distinct employer spellings" % (files, format(len(tot), ",")))
    return tot


def main():
    ap = argparse.ArgumentParser(description="Rank H-1B sponsors for the discovery pipeline.")
    ap.add_argument("directory", nargs="?", default=None, help="folder of h1b_YYYY.csv")
    ap.add_argument("--years", default="2024-2025",
                    help="fiscal years to rank on (default 2024-2025 — who is sponsoring NOW, "
                         "which is a different question from the 5-year tier window)")
    ap.add_argument("--top", type=int, default=2000, help="how many names to emit")
    ap.add_argument("--min", type=int, default=25, help="drop employers below this many approvals")
    ap.add_argument("--out", default="top_sponsors.csv")
    args = ap.parse_args()

    directory = args.directory or next((d for d in DEFAULT_DIRS if os.path.isdir(d)), None)
    if not directory or not os.path.isdir(directory):
        sys.exit("  Could not find the USCIS CSV folder. Pass it explicitly.")

    years = parse_years(args.years)
    print("Ranking %s from %s" % (args.years, directory))
    tot = read_raw(directory, years)
    if not tot:
        sys.exit("  No approvals parsed — check the folder and column names.")

    kept, dropped = [], collections.Counter()
    for name, n in tot.most_common():
        if n < args.min:
            break                                   # most_common is descending
        if core.is_agency(name) or core.BODYSHOP_RE.search(name or ""):
            dropped["body shop / staffing"] += 1
            continue
        kept.append((name, n))
        if len(kept) >= args.top:
            break

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["company", "h1b_recent"])
        w.writerows(kept)

    print("  dropped: %s" % (", ".join("%s %d" % (k, v) for k, v in dropped.items()) or "none"))
    print("  wrote %s (%d names, %s..%s approvals)"
          % (args.out, len(kept), format(kept[0][1], ",") if kept else "-",
             format(kept[-1][1], ",") if kept else "-"))
    print("\nNext: python scripts/discover_companies.py --channel csv --in %s --out disc.csv"
          % args.out)


if __name__ == "__main__":
    main()
