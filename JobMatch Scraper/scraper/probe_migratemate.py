#!/usr/bin/env python3
"""
probe_migratemate.py — turn the employers harvested by mine_migratemate into scrapeable boards.

Input is migratemate_companies.csv (2,958 sponsor-friendly employers, one request to their
public directory). ~2,770 of them aren't in SOURCES. This filters that list down to plausible
targets, runs the EXISTING discovery chain over the survivors, and — the part that did not
exist in code before — verifies that the board we found actually belongs to the company we
asked for.

WHY THE VERIFICATION MATTERS. find_everify_boards.discover() guesses a slug from the name and
never checks the answer. Probing 771 employers by hand earlier produced a board called
"charles" binding to Charles Schwab, "Four Seasons" resolving to Four Seasons Garage Doors,
and three separate universities all matching a board named "Sonja Inc.". At ~1,000 names that
is the dominant error source, so every hit is graded:

    confirmed   the board reports a name and it matches (whole-string ratio >= 85)
    unverified  the board exposes no name to check — open the URL yourself
    guess       a single-token slug with no name to check — most of these are wrong

Only `confirmed` entries are emitted uncommented into SOURCES_lines.txt.

    python -m scraper.probe_migratemate --limit 100     # sample first; check the hit rate
    python -m scraper.probe_migratemate                 # full run
"""
import argparse
import collections
import concurrent.futures
import csv
import os
import re
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rapidfuzz import fuzz

import scraper
from scraper import find_everify_boards as feb
from scraper.build_everify import _BODYSHOP_RE
from scraper.probe_everify_xlsx import NONTARGET

IN_CSV = "migratemate_companies.csv"
OUT_CSV = "migratemate_boards.csv"
OUT_LINES = "SOURCES_lines.txt"
# Every company we've ever probed, hit or miss. Without this a second pass re-probes the
# thousands of MISSES from the first one — the hits exclude themselves (they become sources),
# but the misses are invisible and would silently cost another hour.
PROBED_LOG = "migratemate_probed.txt"

# Board APIs that report the employer's own name, so a hit can be verified rather than trusted.
_NAME_ENDPOINTS = {
    "greenhouse": ("https://boards-api.greenhouse.io/v1/boards/%s", "name"),
    "smartrecruiters": ("https://api.smartrecruiters.com/v1/companies/%s", "name"),
}


def board_reported_name(board_url, ats):
    """What the board calls itself, or '' when the platform doesn't say."""
    ep = _NAME_ENDPOINTS.get(ats)
    if not ep:
        return ""
    slug = board_url.rstrip("/").rsplit("/", 1)[-1]
    url, field = ep
    try:
        r = scraper.SESSION.get(url % slug, headers=scraper.HEADERS, timeout=10)
        if r.status_code == 200:
            return (r.json().get(field) or "").strip()
    except Exception:
        pass
    return ""


# Words a board adds to its OWN name that say nothing about which company it is. Stripped
# before comparing, because "Kaseya Careers", "Backblaze External Website" and
# "CLEAR - Corporate" are all the company they claim to be — while the decoration alone was
# enough to push them under the whole-string threshold and get them wrongly rejected.
_BOARD_DECOR = re.compile(
    r"\b(careers?|career site|job board|jobs?|external|internal|website|web site|site|"
    r"corporate|corp site|opportunities|general|portal|hiring|recruiting|talent|"
    r"employment)\b", re.I)


def _strip_decor(s):
    return scraper._norm_name(re.sub(r"\s+", " ", _BOARD_DECOR.sub(" ", s or "")).strip())


def _squash(s):
    """'Aera Technology' -> 'aeratechnology'. Full name, punctuation and spaces removed, with
    NO legal-suffix stripping — so it can be compared against a board slug verbatim."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def grade(company, board_url, ats, reported, discover_conf=""):
    """(verdict, score) for one hit. Evidence is used in strength order.

    1. The board reports a name -> compare WHOLE STRINGS. fuzz.ratio, not token_set_ratio:
       the latter scores a subset as perfect, which is exactly how "Charles Schwab" got 100
       against a board called "charles" and "Four Seasons" matched Four Seasons Garage Doors.
       This test runs first precisely so it can veto the two weaker signals below.
    2. discover() reports 'high' -> the board was reached by resolving the COMPANY'S OWN
       domain (careers.<co>.com and friends), not by guessing a slug. careers.aflac.com is
       Aflac's board by construction; there is nothing left to verify.
    3. The slug IS the company name with punctuation removed ("Aera Technology" ->
       "aeratechnology"). A coincidence at that length is implausible.
    Anything else stays unproven: 'unverified' when the name is distinctive enough to be
    worth a look, 'guess' for short single-token slugs, which are mostly wrong.
    """
    nm = scraper._norm_name
    if reported:
        sc = max(fuzz.ratio(nm(company), nm(reported)),
                 fuzz.ratio(nm(company), _strip_decor(reported)))
        # Not "MISMATCH": a low score means unproven, not proven wrong. These are written to
        # the review block commented out, and several turn out to be genuine on sight
        # (Sony Music -> "Sony Music Global Job Board", Salesloft -> "Clari + Salesloft").
        return ("confirmed" if sc >= 85 else "review"), sc
    if (discover_conf or "").lower() == "high":
        return "confirmed", None
    slug = board_url.rstrip("/").rsplit("/", 1)[-1]
    # Compare the slug against the raw name AND the legal-suffix-stripped one. _squash keeps
    # suffixes verbatim, which is right for a hand-curated list but rejects every row of the
    # federal E-Verify export, where the legal form is always glued on: "Deepgram Inc" squashes
    # to "deepgraminc" and never equals the slug "deepgram". Requiring exact equality at five
    # characters or more keeps this strong evidence — a coincidence at that length is implausible.
    if len(slug) >= 5 and _squash(slug) in (_squash(company), _squash(nm(company))):
        return "confirmed", None
    single_token = " " not in nm(company) or len(slug) < 6
    return ("guess" if single_token else "unverified"), None


def load_probed(path=PROBED_LOG):
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as fh:
        return {ln.strip() for ln in fh if ln.strip()}


def load_targets(path, include_covered=False, probed=()):
    """Employers worth probing: not already a source, not public-sector, not a body shop,
    and not already probed in an earlier pass."""
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    tally = collections.Counter()
    probed = set(probed)
    out, seen = [], set()
    for r in rows:
        name = (r.get("company") or "").strip()
        if not name:
            continue
        if r.get("in_sources") == "yes" and not include_covered:
            tally["already a source"] += 1
            continue
        key = scraper._norm_name(name)
        if not key or key in seen:
            tally["duplicate"] += 1
            continue
        seen.add(key)
        if key in probed:
            tally["probed in an earlier pass"] += 1
            continue
        if _BODYSHOP_RE.search(name):
            tally["body shop"] += 1
            continue
        if NONTARGET.search(name):
            tally["public sector / local"] += 1
            continue
        out.append(name)
    return out, tally, len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default=IN_CSV)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--include-covered", action="store_true")
    a = ap.parse_args()

    already = load_probed()
    targets, tally, total = load_targets(a.inp, a.include_covered, already)
    print("%s: %d employers" % (a.inp, total))
    for k, n in tally.most_common():
        print("   -%-24s %5d" % (k, n))
    print("   = %d to probe" % len(targets))
    if a.limit:
        targets = targets[:a.limit]
        print("   (limited to %d)" % len(targets))
    if not targets:
        return

    # Guessed hosts, not real boards: fail fast instead of retrying a bot-walled marketing
    # domain three times with backoff. Restored in `finally` — it's a global monkeypatch.
    real_session = scraper.SESSION
    scraper.SESSION = feb._fast_session()
    results = []
    t0 = time.time()
    plog = open(PROBED_LOG, "a", encoding="utf-8")
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=a.workers) as ex:
            futs = {ex.submit(feb.discover, c): c for c in targets}
            for i, fut in enumerate(concurrent.futures.as_completed(futs), 1):
                company = futs[fut]
                try:
                    _c, url, ats, count, conf = fut.result(timeout=50)
                except Exception:
                    url, ats, count, conf = None, "error", 0, ""
                plog.write(scraper._norm_name(company) + "\n")   # hit or miss, so passes compose
                plog.flush()
                if url and count:            # probe_board returning 0 means "reachable but empty"
                    reported = board_reported_name(url, ats)
                    verdict, score = grade(company, url, ats, reported, conf)
                    results.append({"company": company, "ats": ats, "board_url": url,
                                    "jobs": count, "reported_name": reported or "",
                                    "verdict": verdict, "name_score": score if score is not None else "",
                                    "discover_confidence": conf})
                    print("HIT  %-38s %-16s %5s jobs  [%s]"
                          % (company[:38], ats, count, verdict), flush=True)
                if i % 50 == 0:
                    print("   ... %d/%d probed, %d hits (%.0fs)"
                          % (i, len(targets), len(results), time.time() - t0), flush=True)
    finally:
        scraper.SESSION = real_session
        plog.close()

    order = {"confirmed": 0, "unverified": 1, "guess": 2, "review": 3}
    results.sort(key=lambda r: (order.get(r["verdict"], 9), -(r["jobs"] or 0)))
    if results:
        with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=list(results[0].keys()))
            w.writeheader()
            w.writerows(results)

    with open(OUT_LINES, "w", encoding="utf-8") as fh:
        fh.write("# --- Added %s (migratemate.co company directory, probed + identity-verified) ---\n"
                 % time.strftime("%Y-%m-%d"))
        for r in results:
            if r["verdict"] == "confirmed":
                fh.write('    ("%s", "%s", "%s"),   # ~%s\n'
                         % (r["board_url"], r["ats"], r["company"], r["jobs"]))
        for label, key in (("UNVERIFIED — board reports no name; open the URL first", "unverified"),
                           ("GUESS — single-token slug, no name check; most are wrong", "guess"),
                           ("REVIEW — board name didn't match; several of these are genuine", "review")):
            block = [r for r in results if r["verdict"] == key]
            if not block:
                continue
            fh.write("\n\n# %s\n" % label)
            for r in block:
                fh.write('    # ("%s", "%s", "%s"),   # ~%s  board says: %s\n'
                         % (r["board_url"], r["ats"], r["company"], r["jobs"],
                            r["reported_name"] or "-"))

    by = collections.Counter(r["verdict"] for r in results)
    print("\nprobed %d in %.0f min — %d hit(s)" % (len(targets), (time.time() - t0) / 60, len(results)))
    for k in ("confirmed", "unverified", "guess", "review"):
        print("   %-11s %4d" % (k, by[k]))
    print("   postings behind confirmed: %d"
          % sum(r["jobs"] or 0 for r in results if r["verdict"] == "confirmed"))
    print("\n-> %s  |  %s" % (OUT_CSV, OUT_LINES))


if __name__ == "__main__":
    main()
