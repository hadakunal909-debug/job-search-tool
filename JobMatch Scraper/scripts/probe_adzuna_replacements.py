#!/usr/bin/env python3
"""probe_adzuna_replacements.py — can each Adzuna employer be scraped DIRECTLY instead?

Adzuna is an aggregator: its rows carry adzuna.com redirect URLs, a truncated description,
and an employer name we have to trust. Every one of those is worse than the same posting read
off the employer's own ATS. This asks, for each name in scraper.ADZUNA_BOARDS plus every
employer that only reaches the corpus through an adzuna-search phrase, whether a direct board
exists — reusing find_everify_boards.discover(), the same detect chain that found the E-Verify
boards, so a hit here is validated by scraper.probe_board and not just a guessed slug.

Writes a CSV for review. Adopting anything is a separate, deliberate edit to SOURCES.

    python scripts/probe_adzuna_replacements.py                 # ADZUNA_BOARDS only
    python scripts/probe_adzuna_replacements.py --with-corpus   # + adzuna-search employers
    python scripts/probe_adzuna_replacements.py --workers 12 --min-rows 5
"""
import csv
import gzip
import json
import os
import sys
import collections
import concurrent.futures

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import scraper
from scraper import find_everify_boards as feb

REPORT = "adzuna_replacements.csv"
COLS = ["employer", "adzuna_rows", "corpus_rows", "board_url", "ats_type",
        "job_count", "confidence", "verdict"]


def _arg(flag, default=None):
    for i, a in enumerate(sys.argv):
        if a == flag and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return default


def _corpus_counts():
    """(adzuna_rows, total_rows) per employer, from the local feed snapshot.

    The snapshot is what the site actually serves, so an employer's numbers here answer the
    question that matters — how much of its presence in the feed is Adzuna's — rather than how
    many ads Adzuna happens to hold today.
    """
    adz, tot = collections.Counter(), collections.Counter()
    try:
        with gzip.open("jobs_snapshot.json.gz", "rt", encoding="utf-8") as fh:
            rows = (json.load(fh) or {}).get("rows") or []
    except Exception:
        return adz, tot
    for r in rows:
        co = (r.get("company") or "").strip()
        if not co:
            continue
        tot[co] += 1
        if "//www.adzuna.com/" in (r.get("url") or ""):
            adz[co] += 1
    return adz, tot


def main():
    adz_rows, tot_rows = _corpus_counts()

    # The named company boards, using the SEARCH name (the third field) rather than the
    # "adzuna:X" label — that name is the employer as Adzuna spells it, which is also the best
    # string to hand a careers-page lookup.
    names = [c for _b, _a, c in scraper.ADZUNA_BOARDS]

    if "--with-corpus" in sys.argv:
        # Employers that exist in the feed ONLY because a phrase search found them. These are
        # the rows that vanish outright when Adzuna is switched off, so they are the ones worth
        # asking about — but only where there is enough of them to be worth a board.
        floor = int(_arg("--min-rows", "4"))
        known = {scraper._norm_name(n) for n in names}
        names += sorted(co for co, n in adz_rows.items()
                        if n >= floor and n == tot_rows[co]
                        and scraper._norm_name(co) not in known)

    seen, ordered = set(), []
    for n in names:
        k = scraper._norm_name(n)
        if k and k not in seen:
            seen.add(k)
            ordered.append(n)

    workers = int(_arg("--workers", "8"))
    print("probing %d employer(s) with %d workers -> %s" % (len(ordered), workers, REPORT))

    out = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for company, burl, ats, cnt, conf in ex.map(feb.discover, ordered):
            verdict = "DIRECT BOARD" if burl else "no direct board"
            out.append({"employer": company,
                        "adzuna_rows": adz_rows.get(company, 0),
                        "corpus_rows": tot_rows.get(company, 0),
                        "board_url": burl or "", "ats_type": ats or "",
                        "job_count": cnt or 0, "confidence": conf,
                        "verdict": verdict})
            print("  %-34s %-14s %s" % (company[:34], verdict,
                                        ("%s (%s, %s jobs)" % (burl, ats, cnt)) if burl else ats))

    out.sort(key=lambda r: (r["verdict"] != "DIRECT BOARD", -r["job_count"]))
    with open(REPORT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        w.writerows(out)

    hits = [r for r in out if r["board_url"]]
    print("\n%d of %d have a scrapeable direct board." % (len(hits), len(out)))
    print("Wrote %s" % REPORT)


if __name__ == "__main__":
    main()
