#!/usr/bin/env python3
"""
probe_everify_candidates.py — find scrapeable boards for the sponsors classify_everify picked.

This is the probe step that follows scraper.classify_everify. It reads everify_categorised.csv
rather than the raw USCIS export, so the candidate set is chosen by SPONSOR EVIDENCE (a
size-plausible H-1B filing record) instead of probe_everify_xlsx's blunt "workforce >= 500"
gate — that gate is why the previous run missed real mid-size sponsors like Exelixis (500-999,
100 filings) and HealthStream (1,000-2,499, 89).

Reuses the existing detect chain wholesale: find_everify_boards.discover() tries slug guesses
for the simple ATSes, then walks careers-page candidates through detect_linked_ats ->
detect_phenom -> detect_successfactors -> detect_jibe, and validates every hit with
scraper.probe_board so dead or parked boards never make it into the output.

REVIEW-FIRST: writes a CSV only. Adoption into the `boards` table is a separate, explicit step
(see adopt_everify_boards.py).

    python -m scraper.probe_everify_candidates                     # buckets 2+3
    python -m scraper.probe_everify_candidates --buckets 2         # candidates only
    python -m scraper.probe_everify_candidates --limit 40 -v       # smoke test
    python -m scraper.probe_everify_candidates --workers 24
    python -m scraper.probe_everify_candidates --buckets 4 --min-size 100 --max-size 499         --drop-public --out everify_board_probe_mid.csv
"""
import os
import re
import sys
import csv
import concurrent.futures

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import scraper
from scraper import find_everify_boards as feb
from scraper.classify_everify import REPORT_CSV as CATEGORISED_CSV
from scraper.probe_everify_xlsx import _size_lower, NONTARGET

REPORT = "everify_board_probe.csv"
COLS = ["employer", "bucket", "category", "size", "state", "h1b_filings", "bodyshop",
        "career_page", "ats_type", "job_count", "board_url", "confidence"]


def _already_probed():
    """Employer names any previous probe batch already covered.

    Each run writes its own CSV, and the bands are worked through in separate passes, so
    without this a "100+" run would re-probe every company the "500+" run already did — pure
    duplicated network against the same hosts.
    """
    import glob
    done = set()
    for path in glob.glob("everify_board_probe*.csv"):
        try:
            with open(path, encoding="utf-8", newline="") as f:
                for r in csv.DictReader(f):
                    k = scraper._strict_norm_name(r.get("employer", ""))
                    if k:
                        done.add(k)
        except OSError:
            continue
    return done


def load_candidates(path, buckets, limit=None, min_size=0, drop_public=False,
                    max_size=0, skip_probed=True):
    """Rows from everify_categorised.csv in the requested buckets, best sponsors first.

    Deduped on the normalised employer name: the export lists "Equinox Holdings, Inc" and
    "Equinox Holdings, Inc." as separate rows, and probing each twice is wasted network.

    `min_size` gates on the lower bound of the USCIS workforce band, and is what makes bucket 4
    tractable. Bucket 4 is "no H-1B filing record", which is NOT the same as "does not sponsor" —
    the federal indexes are incomplete enough that Northrop Grumman is absent from both — so a
    large employer with no record is still worth a board. Below ~100 staff it stops paying:
    those are single-site local businesses that overwhelmingly have no ATS at all.

    `drop_public` removes school districts, cities, credit unions and local hospitals via the
    NONTARGET pattern. Universities are deliberately NOT matched by it — they are cap-exempt.
    """
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    want = {"%s_" % b for b in buckets}
    done = _already_probed() if skip_probed else set()
    out, seen = [], set()
    for r in rows:
        if not any(r["bucket"].startswith(w) for w in want):
            continue
        if min_size and _size_lower(r.get("size")) < min_size:
            continue
        if max_size and _size_lower(r.get("size")) > max_size:
            continue
        if drop_public and NONTARGET.search(r["employer"]):
            continue
        key = scraper._strict_norm_name(r["employer"])
        if not key or key in seen or key in done:
            continue
        seen.add(key)
        try:
            r["h1b_filings"] = int(r.get("h1b_filings") or 0)
        except ValueError:
            r["h1b_filings"] = 0
        out.append(r)
    # Sponsors first, then biggest employer first — so a truncated run keeps the best leads.
    out.sort(key=lambda r: (-r["h1b_filings"], -_size_lower(r.get("size"))))
    return out[:limit] if limit else out


def probe(rec):
    """Detect + validate a board for one company. Mirrors probe_everify_xlsx.probe so the two
    reports stay comparable; falls back to recording a reachable careers page when no board
    is readable, because that is still a lead worth eyeballing."""
    _co, url, ats, cnt, conf = feb.discover(rec["employer"])
    if url:
        rec.update(career_page="yes", ats_type=ats, job_count=cnt,
                   board_url=url, confidence=conf)
        return rec
    cp = ""
    try:
        for cand in feb._careers_candidates(rec["employer"]):
            if feb._reachable(cand):
                cp = cand
                break
    except Exception:
        pass
    rec.update(career_page=cp, ats_type="", job_count="", board_url="", confidence="")
    return rec


def _arg(flag, default=None, cast=str):
    if flag in sys.argv:
        try:
            return cast(sys.argv[sys.argv.index(flag) + 1])
        except (ValueError, IndexError):
            print("%s needs a value" % flag)
            sys.exit(1)
    return default


def main():
    src = _arg("--csv", CATEGORISED_CSV)
    buckets = [b.strip() for b in _arg("--buckets", "2,3").split(",") if b.strip()]
    workers = _arg("--workers", 24, int)
    limit = _arg("--limit", None, int)
    min_size = _arg("--min-size", 0, int)
    max_size = _arg("--max-size", 0, int)
    drop_public = "--drop-public" in sys.argv
    skip_probed = "--reprobe" not in sys.argv
    out = _arg("--out", REPORT)
    verbose = "-v" in sys.argv or "--verbose" in sys.argv
    if not os.path.exists(src):
        print("not found: %s — run `python -m scraper.classify_everify` first." % src)
        return 1

    cands = load_candidates(src, buckets, limit, min_size, drop_public, max_size, skip_probed)
    band = ""
    if min_size or max_size:
        band = " (%s staff)" % ("%d-%d" % (min_size, max_size) if min_size and max_size
                                else (">=%d" % min_size if min_size else "<=%d" % max_size))
    print("probing %d companies from buckets %s%s%s%s"
          % (len(cands), ",".join(buckets), band,
             " minus public-sector" if drop_public else "",
             "" if skip_probed else " (re-probing already-seen)"), flush=True)
    if not cands:
        print("nothing to probe.")
        return 0

    orig = scraper.SESSION
    scraper.SESSION = feb._fast_session()          # no retries, short timeouts
    results = []
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(probe, c): c for c in cands}
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
                             rec["board_url"][:52]), flush=True)
                elif verbose:
                    print("  -- %-34s %s" % (rec["employer"][:34],
                          rec["career_page"] or "nothing"), flush=True)
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

    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore")
        w.writeheader()
        for r in results:
            w.writerow({k: r.get(k, "") for k in COLS})

    hits = [r for r in results if r.get("board_url")]
    hi = [r for r in hits if r.get("confidence") == "high"]
    by_ats = {}
    for r in hits:
        by_ats[r["ats_type"]] = by_ats.get(r["ats_type"], 0) + 1
    cp_only = sum(1 for r in results if not r.get("board_url") and r.get("career_page"))
    print("\n=== %d probed -> %d boards (%d high-confidence), %d careers-page-only, %d nothing ==="
          % (len(results), len(hits), len(hi), cp_only, len(results) - len(hits) - cp_only))
    if by_ats:
        print("by ATS:", ", ".join("%s %d" % kv for kv in
                                   sorted(by_ats.items(), key=lambda x: -x[1])))
    print("wrote", os.path.abspath(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
