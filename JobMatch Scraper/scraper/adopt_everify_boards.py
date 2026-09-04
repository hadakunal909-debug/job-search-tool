#!/usr/bin/env python3
"""
adopt_everify_boards.py — verify the probed E-Verify+ boards, then add them to the scraper.

probe_everify_candidates.py guesses slugs, so most of its hits arrive "low" confidence and a
slug guess is wrong more often than it looks: greenhouse/boston is "Boston North Station", not
"Boston Inc."; greenhouse/qt is "Quality Trusted Commercial", claimed by both "Qt Company" and
"QT Corporation". Adding those unchecked would put another company's postings in the feed under
our employer name, which is worse than missing the board entirely.

So every hit is graded first, reusing probe_migratemate.board_reported_name + grade():
    confirmed   board reports a matching name, OR discover() resolved the company's own
                domain, OR the slug IS the name — safe to add
    review      the board reports a DIFFERENT name — never added, listed for eyeballing
    unverified  platform exposes no name and the slug is not conclusive — not added
    guess       short single-token slug — mostly wrong, not added

Only `confirmed` is written, and it goes to the `boards` DB table (via db.add_board), not into
scraper/__init__.py. custom_sources() merges that table into SOURCES at scrape time, so this is
reversible with a DELETE and never touches the 366 KB module.

    python -m scraper.adopt_everify_boards --dry-run     # grade + report, write nothing
    python -m scraper.adopt_everify_boards               # add the confirmed ones
    python -m scraper.adopt_everify_boards --include-bodyshops
    python -m scraper.adopt_everify_boards --no-yield-check   # skip the big-board sampling
    python -m scraper.adopt_everify_boards --csv discovered_board_probe.csv --added-by discover:2026-08 --out discovered_adoption.csv
"""
import os
import sys
import csv
import concurrent.futures

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import scraper
from scraper import find_everify_boards as feb
from scraper.probe_migratemate import board_reported_name, grade
from scraper.probe_everify_candidates import REPORT as PROBE_CSV

OUT = "everify_adoption.csv"
ADDED_BY = "everify+:2026-08"
COLS = ["employer", "bucket", "size", "state", "h1b_filings", "bodyshop", "ats_type",
        "job_count", "kept_of_fetched", "us_of_fetched", "board_url", "confidence",
        "reported_name", "verdict", "score", "added"]


def verify(rec):
    """Attach reported_name / verdict / score to one probed hit."""
    reported = board_reported_name(rec["board_url"], rec["ats_type"])
    verdict, score = grade(rec["employer"], rec["board_url"], rec["ats_type"],
                           reported, rec.get("confidence", ""))
    rec["reported_name"] = reported
    rec["verdict"] = verdict
    rec["score"] = "" if score is None else int(score)
    return rec


# Below this many postings a useless board costs nothing to keep, so don't spend a fetch
# checking it. Above it, one board can dominate a whole sweep.
YIELD_CHECK_MIN_POSTINGS = 500

# The location check needs no cost gate the way the yield check does -- one fetch per
# CONFIRMED board is a few dozen requests a run. It needs no SAMPLE-SIZE gate either, and
# that is worth spelling out because the obvious guess is wrong: _sample() pages the WHOLE
# board, so len(rows) is the board's entire content, not a sample of it. A board whose one
# and only posting is in Coimbatore is 100% foreign, not a thin sample -- which is exactly
# ROBERTBOSCHLLC, and a threshold of 3 let it through. The floor exists solely to separate
# 'fetched nothing' (unfetchable, no verdict) from 'fetched something, none of it US'.
FOREIGN_CHECK_MIN_POSTINGS = 1


# Sampling a board costs one fetch, and TWO independent checks want it -- the title-filter
# yield below and the location check under it. Memoise on the record so a board is never
# fetched twice, and so a board rejected by the first check never pays for the second.
def _sample(rec):
    """The board's postings, fetched at most once per record. [] if unfetchable."""
    if "_rows" in rec:
        return rec["_rows"]
    rows = []
    fn = scraper.SCRAPERS.get(rec["ats_type"])
    if fn:
        try:
            rows = fn(rec["board_url"]) or []
        except Exception:
            rows = []
    rec["_rows"] = rows
    return rows


def relevance_yield(rec):
    """(kept, fetched) after the scraper's own title filter, or (None, None) if not checked.

    A board's SIZE says nothing about its worth. Whataburger advertises 4,640 postings and the
    title filter keeps ZERO of them; Family Dollar keeps 16 of 3,000, and those are "Assistant
    Operations Manager" and "Store Construction Project Manager" — store-floor roles, which can
    never satisfy STEM-OPT because the job itself has to relate to the degree. Fetching 7,640
    postings a run to store nothing is the single most expensive mistake this pipeline can make,
    so big boards are sampled BEFORE adoption rather than discovered by eye afterwards.
    """
    try:
        n = int(rec.get("job_count") or 0)
    except (TypeError, ValueError):
        return None, None
    if n < YIELD_CHECK_MIN_POSTINGS:
        return None, None
    rows = _sample(rec)
    if not rows:
        # FETCHED NOTHING is not the same as NOT CHECKED, and conflating them adopted a
        # dead board: AutoZone's probe reported 10,855 postings from its careers page while
        # scrape_oracle read 0 rows off the same URL, relevance_yield returned (None, None),
        # and 10,855 phantom postings entered the sweep to be re-fetched forever for nothing.
        # A board the probe calls big and the scraper cannot read at all is unusable TODAY,
        # whatever the careers page claims, so report it as a zero rather than a silence.
        return 0, 0
    kept = sum(1 for r in rows if scraper.title_verdict(r.get("title", ""))[0])
    return kept, len(rows)


def _names_non_us(loc):
    """True only when the location NAMES a non-US place. Unrecognisable -> False.

    A VETO, deliberately, and NOT scraper.is_us_location inverted. That distinction is the whole
    correctness of the check below, and getting it wrong is not hypothetical -- the first version
    of this guard used `not is_us_location(...)` and rejected five cap-exempt universities:
    University of North Dakota (0 of 420 postings 'US'), UNLV (0/133), University of Louisville
    (0/126), NJIT (0/77) and Clemson (0/9). Their boards report BUILDING names -- 'Tiernan Hall',
    'UNLV1-Main Campus, Las Vegas', 'Clemson University' -- which carry no country signal at all,
    and is_us_location answers False for anything it cannot place, not just for things abroad.
    scraper.title_says_non_us' docstring makes the same point about the same function.
    """
    low = scraper._fold(loc or "")
    if not low or not scraper._NON_US_RE.search(low):
        return False
    # ...and a US namesake is not "somewhere abroad". Same list is_us_location uses: without
    # it a board whose every posting sits in Lima OH or London OH reads as 100% foreign and
    # this veto rejects a real US employer -- the failure mode the docstring above warns
    # about, arriving through the regex instead of through is_us_location.
    return not scraper._us_namesake_only(low, loc)


def foreign_share(rec):
    """(explicitly_foreign, fetched) over the board's postings, or (None, None) if unfetchable.

    IMPOSTOR BOARDS. Slug guessing on the JSON-API ATSes finds squatted accounts whose name
    matches the employer and whose postings have nothing to do with it. Measured 2026-08-31:
    jobs.smartrecruiters.com/CITIBANKNA served 7 postings, all Jakarta and Bekasi, with titles
    like "Lowongan kerja Operator Produksi PT Asmo Indonesia"; .../ROBERTBOSCHLLC served one in
    Coimbatore; .../MERKLEINC one in Chennai. All three were graded "confirmed".

    Nothing else could see them. relevance_yield only runs above YIELD_CHECK_MIN_POSTINGS, on the
    reasoning that a small useless board is cheap to keep -- true for a board that is merely thin,
    false for one that will never serve a US posting. And a NAME check cannot help: those postings
    DO report "Citibank N.A" and "Robert Bosch LLC" as the company. Location is the only tell.

    Rejects only when EVERY posting explicitly names somewhere abroad. Two separate asymmetries,
    both load-bearing:
      * ALL, not a majority. HCL America is 6 of 10 US and is a real board.
      * explicitly-foreign, not un-provably-US. See _names_non_us.

    It reads the SCRAPER'S composed location rather than a raw API field, because that string is
    "Chennai, TN, India" where the raw city is just "Chennai" -- and note the "TN" there is Tamil
    Nadu, not Tennessee, which is why the veto keys on the country and never on a state code.
    """
    rows = _sample(rec)
    if len(rows) < FOREIGN_CHECK_MIN_POSTINGS:
        return None, None
    foreign = sum(1 for r in rows if _names_non_us(r.get("location")))
    return foreign, len(rows)


def _arg(flag, default=None, cast=str):
    if flag in sys.argv:
        try:
            return cast(sys.argv[sys.argv.index(flag) + 1])
        except (ValueError, IndexError):
            print("%s needs a value" % flag)
            sys.exit(1)
    return default


def main():
    src = _arg("--csv", PROBE_CSV)
    workers = _arg("--workers", 12, int)
    dry = "--dry-run" in sys.argv
    keep_bodyshops = "--include-bodyshops" in sys.argv
    skip_yield_check = "--no-yield-check" in sys.argv
    skip_location_check = "--no-location-check" in sys.argv
    # Tag rows with the run that produced them. Adoption is meant to be reversible with a
    # DELETE, and one tag for every batch ever adopted makes "undo the LinkedIn sweep"
    # impossible to express -- so the caller names its own batch.
    added_by = _arg("--added-by", ADDED_BY)
    # Same reason --added-by exists: OUT was hardcoded, so a second pipeline through this
    # script silently overwrote the first one's review artifact. Measured by doing it.
    out = _arg("--out", OUT)
    if not os.path.exists(src):
        print("not found: %s — run `python -m scraper.probe_everify_candidates` first." % src)
        return 1

    with open(src, encoding="utf-8", newline="") as f:
        hits = [r for r in csv.DictReader(f) if r.get("board_url")]
    print("verifying %d probed boards..." % len(hits), flush=True)

    orig = scraper.SESSION
    scraper.SESSION = feb._fast_session()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            hits = list(ex.map(verify, hits))
    finally:
        scraper.SESSION = orig

    # Never add the same board URL twice, and never re-add something already in SOURCES.
    # SOURCES *and* the boards table. custom_sources() is the half this missed: a board
    # adopted by any earlier run lives only in that table, never in SOURCES, so it read as
    # unknown and was re-verified and re-added on every subsequent run. db.add_board upserts
    # on url so nothing was duplicated, but the counts were inflated and the yield check
    # spent real network time on boards we already had. Same bug find_everify_boards
    # ._known_sources() documents; see its docstring.
    known = {u for u, _a, _c in scraper.SOURCES}
    try:
        known |= {u for u, _a, _c in scraper.custom_sources()}
    except Exception:
        pass                                   # no DB is not a reason to skip the run

    # Honour the admin blocklist. The zero-yield check below catches boards that are provably
    # worthless (Whataburger: 0 of 4,640 survive the title filter), but it cannot catch a board
    # whose survivors are simply the WRONG KIND of job: Family Dollar keeps 16 of 3,000 and
    # Dollar General keeps 13 of 2,000 — statistically identical, yet Dollar General's are
    # Principal Data Engineer and Product Manager (Data & AI) while Family Dollar's are
    # Assistant Operations Manager and Store Construction PM. Only the titles distinguish them,
    # so that call stays human — and blocked_companies is where it is recorded, once.
    try:
        import db as _db
        blocked = {b.get("name_key") for b in (_db.list_blocked() or []) if b.get("name_key")}
    except Exception:
        blocked = set()
    order = {"confirmed": 0, "unverified": 1, "guess": 2, "review": 3}
    hits.sort(key=lambda r: (order.get(r["verdict"], 9),
                             -(int(r["job_count"]) if str(r["job_count"]).isdigit() else 0)))

    added, skipped_dupe, skipped_body, skipped_yield, skipped_blocked = 0, 0, 0, 0, 0
    skipped_foreign = 0
    seen = set()
    for r in hits:
        r["added"] = "no"
        r["kept_of_fetched"] = ""
        r["us_of_fetched"] = ""
        if r["verdict"] != "confirmed":
            continue
        if r["board_url"] in known or r["board_url"] in seen:
            skipped_dupe += 1
            continue
        try:
            if blocked and _db.is_blocked(r["employer"], blocked):
                r["added"] = "no — company is on the admin blocklist"
                skipped_blocked += 1
                continue
        except Exception:
            pass
        if r.get("bodyshop") == "yes" and not keep_bodyshops:
            skipped_body += 1
            continue
        if not skip_yield_check:
            kept, fetched = relevance_yield(r)
            if (kept, fetched) == (0, 0):
                r["kept_of_fetched"] = "unreadable"
                r["added"] = "no — probe claimed %s postings, scraper reads 0" % r["job_count"]
                skipped_yield += 1
                continue
            if fetched:
                r["kept_of_fetched"] = "%d/%d" % (kept, fetched)
                if kept == 0:
                    # Not "few" — ZERO. A board that survives the title filter with nothing is
                    # pure scrape cost forever. Anything above zero is left to the reviewer.
                    r["added"] = "no — title filter keeps 0 of %d" % fetched
                    skipped_yield += 1
                    continue
        if not skip_location_check:
            foreign, fetched = foreign_share(r)
            if fetched:
                r["us_of_fetched"] = "%d/%d foreign" % (foreign, fetched)
                if foreign == fetched:
                    # Every posting NAMES somewhere abroad -> this is not the employer we
                    # think it is. See foreign_share() for the boards that proved it.
                    r["added"] = "no — all %d postings name a non-US place" % fetched
                    skipped_foreign += 1
                    continue
        seen.add(r["board_url"])
        if dry:
            r["added"] = "would-add"
            added += 1
            continue
        try:
            import db
            db.add_board(r["board_url"], r["ats_type"], r["employer"], added_by=added_by)
            r["added"] = "yes"
            added += 1
        except Exception as e:
            r["added"] = "ERROR: %s" % str(e)[:80]

    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore")
        w.writeheader()
        for r in hits:
            w.writerow({k: r.get(k, "") for k in COLS})

    tally = {}
    for r in hits:
        tally[r["verdict"]] = tally.get(r["verdict"], 0) + 1
    print()
    for v in ("confirmed", "unverified", "guess", "review"):
        print("  %-12s %3d" % (v, tally.get(v, 0)))
    print()
    print("  %s: %d board(s)%s" % ("would add" if dry else "ADDED", added,
                                   "  [dry run]" if dry else ""))
    if skipped_dupe:
        print("  skipped, already a source: %d" % skipped_dupe)
    if skipped_body:
        print("  skipped, body-shop name: %d  (pass --include-bodyshops to keep)" % skipped_body)
    if skipped_yield:
        print("  skipped, title filter keeps ZERO of the board: %d  (--no-yield-check to keep)"
              % skipped_yield)
    if skipped_foreign:
        print("  skipped, every posting abroad (impostor board): %d  (--no-location-check to keep)"
              % skipped_foreign)
    if skipped_blocked:
        print("  skipped, on the admin blocklist: %d" % skipped_blocked)
    conf = [r for r in hits if r["verdict"] == "confirmed"]
    jobs = sum(int(r["job_count"]) for r in conf if str(r["job_count"]).isdigit())
    print("  postings behind confirmed boards: %s" % format(jobs, ","))
    print("\nwrote %s" % os.path.abspath(out))
    if [r for r in hits if r["verdict"] == "review"]:
        print("\nREVIEW — board reports a different name, not added:")
        for r in hits:
            if r["verdict"] == "review":
                print("   %-30s slug says %-34s (score %s)"
                      % (r["employer"][:30], (r["reported_name"] or "?")[:34], r["score"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
