#!/usr/bin/env python3
"""
discover.py — keep finding new sponsor-friendly employers, automatically, on every scrape.

migratemate.co publishes a directory of ~3,000 employers that sponsor visas, and it grows.
This reads that list (ONE request), works out which employers we don't already scrape, probes
a bounded slice of them for a readable ATS board, and saves the confirmed ones to the `boards`
table — where scraper.custom_sources() picks them up, so they are scraped from then on with no
code edit and no deploy.

WHY IT ROTATES INSTEAD OF KEEPING A LOG
    Probing is slow (~0.7 companies/sec) so a run can only do a slice, and the obvious way to
    avoid repeating work is a list of everything already tried. That does not survive CI: the
    Actions runner is a fresh checkout every time, so a local file is always empty and the same
    first N names get re-probed forever.

    So instead the candidate list is sorted (stable across runs) and the window ROTATES by the
    day of the year. No state to store, no state to lose, and the whole list is covered over a
    cycle. Re-probing a past miss eventually is a feature, not waste: a company that had no
    readable board in March may have moved onto Greenhouse by August.

    Employers we already scrape are excluded every run, so the candidate pool shrinks as this
    succeeds and the cycle gets faster over time.

    python -m scraper.discover --limit 40 --dry-run     # see what it would add
"""
import argparse
import concurrent.futures
import datetime
import os
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# How many employers to probe per scrape. ~0.7/sec, so 60 costs about 90 seconds — small
# against a scrape that runs for the better part of an hour, and it compounds daily.
DEFAULT_LIMIT = 60


def _candidates():
    """Employers in the directory that we don't already scrape, filtered and sorted."""
    import db
    import scraper
    from scraper.build_everify import _BODYSHOP_RE
    from scraper.mine_migratemate import company_directory
    from scraper.probe_everify_xlsx import NONTARGET

    nm = scraper._norm_name
    known = set()
    for _u, _a, c in list(scraper.SOURCES) + list(scraper.custom_sources()):
        k = nm(c)
        if k:
            known.add(k)
            known.add(k.replace(" ", ""))     # "JPMorganChase" vs "JPMorgan Chase"

    out, seen = [], set()
    for _slug, name in company_directory():
        k = nm(name)
        if not k or k in seen or k in known or k.replace(" ", "") in known:
            continue
        if _BODYSHOP_RE.search(name) or NONTARGET.search(name):
            continue
        seen.add(k)
        out.append(name)
    return sorted(out)


def discover(limit=DEFAULT_LIMIT, dry_run=False, workers=12):
    """Probe a rotating slice of unknown employers; save confirmed boards. Returns added count.

    Never raises: this runs inside the scrape, and a discovery failure must not cost the run.
    """
    try:
        import db
        import scraper
        from scraper import find_everify_boards as feb
        from scraper.probe_migratemate import board_reported_name, grade
    except Exception as e:
        print("  discovery unavailable: %s" % str(e)[:90])
        return 0

    try:
        names = _candidates()
    except Exception as e:
        print("  discovery skipped (couldn't read the directory: %s)" % str(e)[:80])
        return 0
    if not names:
        print("  discovery: every listed employer is already a source — nothing to probe.")
        return 0

    # Rotate the window by day-of-year so consecutive runs look at different employers
    # without needing to remember anything.
    doy = datetime.date.today().timetuple().tm_yday
    start = (doy * limit) % len(names)
    window = [names[(start + i) % len(names)] for i in range(min(limit, len(names)))]
    print("  discovery: %d unknown employer(s) listed; probing %d (offset %d, rotates daily)"
          % (len(names), len(window), start))

    real_session = scraper.SESSION
    scraper.SESSION = feb._fast_session()      # fail fast on guessed hosts, don't retry them
    found, added = [], 0
    t0 = time.time()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(feb.discover, c): c for c in window}
            for fut in concurrent.futures.as_completed(futs):
                company = futs[fut]
                try:
                    _c, url, ats, count, conf = fut.result(timeout=50)
                except Exception:
                    continue
                if not (url and count):        # 0 == reachable but empty, not a hit
                    continue
                verdict, _score = grade(company, url, ats, board_reported_name(url, ats), conf)
                if verdict != "confirmed":     # unproven names are never auto-added
                    continue
                found.append((company, url, ats, count))
    finally:
        scraper.SESSION = real_session

    for company, url, ats, count in sorted(found, key=lambda r: -r[3]):
        if dry_run:
            print("     would add  %-30s %-15s %5d postings" % (company[:30], ats, count))
            continue
        try:
            ok, msg = db.add_board(url, ats, company, added_by="auto:migratemate")
        except Exception as e:
            ok, msg = False, str(e)[:70]
        print("     %-9s %-30s %-15s %5d postings%s"
              % ("added" if ok else "FAILED", company[:30], ats, count,
                 "" if ok else "  (%s)" % msg))
        added += 1 if ok else 0

    print("  discovery: %d probed in %.0fs, %d confirmed, %d saved"
          % (len(window), time.time() - t0, len(found), added if not dry_run else 0))
    return added


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    discover(a.limit, a.dry_run, a.workers)


if __name__ == "__main__":
    main()
