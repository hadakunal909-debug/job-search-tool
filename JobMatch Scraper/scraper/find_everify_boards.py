#!/usr/bin/env python3
"""
find_everify_boards.py — discover scrapeable career boards for big legit employers
----------------------------------------------------------------------------------
Drives the EXISTING detect chain over a list of large, legitimate US employers (the
kind that are overwhelmingly E-Verify enrolled — what an F-1/STEM-OPT student wants)
and prints paste-ready SOURCES tuples for the ones that have a readable board.

Covers ALL ATS types (find_boards.py only did Greenhouse/Lever/Ashby/SR): it tries the
simple-ATS slug guesses first, then the careers-page detect chain (detect_linked_ats,
detect_phenom -> often a Workday board, detect_successfactors, detect_jibe), validating
every hit with probe_board so dead/parked boards are dropped.

Run:
    python -m scraper.find_everify_boards                # the curated majors
    python -m scraper.find_everify_boards everify.txt    # + names from a file (1/line)
    python -m scraper.find_everify_boards --sponsors     # + sponsors.txt not-yet-scraped

Body-shop guard: names matching build_everify._BODYSHOP_RE are skipped (we never add an
OPT/H-1B mill). Slug-guessed simple-ATS hits are marked LOW-CONFIDENCE (collision risk —
e.g. greenhouse/rpa is the wrong "RPA"); confirm identity before pasting into SOURCES.
"""
import os
import re
import sys
import concurrent.futures

import requests
import scraper
from scraper.build_everify import _BODYSHOP_RE

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Discovery probes GUESSED hosts, so we want FAST-FAIL: a plain session with NO retries
# and short timeouts. (scraper.SESSION retries 3x w/ backoff — great for real boards,
# disastrous when a guessed marketing domain behind a bot-wall hangs.) We monkeypatch
# scraper.SESSION to this for the duration of the run so the detect_* functions inherit it.
def _fast_session():
    s = requests.Session()
    s.headers.update(scraper.HEADERS)
    return s


def _reachable(url, timeout=5):
    """Cheap 'does this host answer at all?' check, so we don't run 4 detect functions
    (each a 10-15s fetch) against a dead/parked/hanging guessed host."""
    try:
        r = scraper.SESSION.get(url, headers=_H, timeout=timeout, allow_redirects=True, stream=True)
        r.close()
        return r.status_code < 500
    except Exception:
        return False

# Large US employers / federal contractors NOT already in SOURCES and overwhelmingly
# E-Verify enrolled. The tool dedupes against SOURCES, so harmless overlap is fine.
CURATED_MAJORS = (
    # Aerospace & defense (federal contractors -> E-Verify mandatory)
    "Boeing", "Lockheed Martin", "RTX", "Raytheon", "Northrop Grumman",
    "General Dynamics", "L3Harris", "Booz Allen Hamilton", "Leidos", "SAIC", "CACI",
    "Parsons", "Textron", "Honeywell", "GE Aerospace", "Collins Aerospace",
    # Pharma / medical device / health
    "Johnson & Johnson", "Pfizer", "AbbVie", "Eli Lilly", "Bristol Myers Squibb",
    "Abbott", "Abbott Laboratories", "Medtronic", "Stryker", "Boston Scientific",
    "Gilead Sciences", "Regeneron", "Moderna", "Genentech", "Zoetis", "Becton Dickinson",
    "Thermo Fisher Scientific", "UnitedHealth Group", "Optum",
    # Semiconductors / hardware
    "Intel", "Qualcomm", "Texas Instruments", "Micron", "Applied Materials",
    "Lam Research", "KLA", "Analog Devices", "Broadcom", "AMD", "Marvell", "Microchip",
    "Dell Technologies", "HP", "Hewlett Packard Enterprise", "NetApp", "Seagate",
    # Industrials / autos
    "Caterpillar", "3M", "Emerson Electric", "Eaton", "Parker Hannifin", "Illinois Tool Works",
    "General Motors", "Ford Motor Company", "John Deere", "Paccar", "Stanley Black & Decker",
    # Finance
    "Wells Fargo", "Goldman Sachs", "Morgan Stanley", "American Express", "Capital One",
    "Charles Schwab", "Fidelity Investments", "BlackRock", "MetLife", "Prudential Financial",
    "Truist", "PNC Financial Services", "U.S. Bank", "TD Bank", "Fifth Third Bank",
    "Northwestern Mutual", "State Street", "BNY Mellon", "Discover Financial",
    # Tech / software / internet
    "Intuit", "PayPal", "Workday", "ADP", "ServiceNow", "Autodesk", "VMware", "Dropbox",
    "Cloudflare", "Atlassian", "DoorDash", "Coinbase", "Roblox",
    # Retail / consumer / travel / telecom / media
    "Walmart", "Target", "The Home Depot", "Lowe's", "Costco Wholesale", "Best Buy",
    "PepsiCo", "The Coca-Cola Company", "General Mills", "Mondelez", "Kraft Heinz",
    "Procter & Gamble", "Kimberly-Clark", "Colgate-Palmolive", "Estee Lauder",
    "FedEx", "United Parcel Service", "Delta Air Lines", "United Airlines",
    "American Airlines", "Marriott", "Hilton", "The Walt Disney Company", "Comcast",
    "AT&T", "Verizon", "T-Mobile", "Charter Communications",
    # Pro services / other
    "Intuitive Surgical", "Waymo", "Cruise", "Palo Alto Networks", "CrowdStrike",
)

# Simple-ATS API probes (inlined from find_boards.py — that module runs code at import,
# so we can't import from it). Each returns a posting count or None.
_H = {"User-Agent": scraper.HEADERS["User-Agent"], "Accept": "application/json"}


def _nospace(name):
    b = name.lower()
    for s in (" inc", " corporation", " corp", " group", " technologies", " company",
              " holdings", " co", " plc", " sa", " the "):
        b = b.replace(s, "")
    return re.sub(r"[^a-z0-9]", "", b)


def _pascal(name):
    b = name
    for s in (" Inc", " Corporation", " Corp", " Group", " Company", " Holdings"):
        b = b.replace(s, "")
    return re.sub(r"[^A-Za-z0-9]", "", b)


def _simple_ats(company):
    """Try Greenhouse/Lever/Ashby (nospace slug) + SmartRecruiters (Pascal slug).
    Returns (board_url, ats, count) or None. LOW confidence — slug collisions happen."""
    slug, ps = _nospace(company), _pascal(company)
    probes = [
        ("greenhouse", "https://job-boards.greenhouse.io/%s" % slug,
         "https://boards-api.greenhouse.io/v1/boards/%s/jobs" % slug, "jobs"),
        ("lever", "https://jobs.lever.co/%s" % slug,
         "https://api.lever.co/v0/postings/%s?mode=json" % slug, "list"),
        ("ashby", "https://jobs.ashbyhq.com/%s" % slug,
         "https://api.ashbyhq.com/posting-api/job-board/%s" % slug, "jobs"),
        ("smartrecruiters", "https://jobs.smartrecruiters.com/%s" % ps,
         "https://api.smartrecruiters.com/v1/companies/%s/postings?limit=1" % ps, "sr"),
    ]
    for ats, board, api, shape in probes:
        try:
            r = scraper.SESSION.get(api, headers=_H, timeout=8)
            if r.status_code != 200:
                continue
            d = r.json()
            if shape == "jobs":
                n = len(d.get("jobs", []))
            elif shape == "list":
                n = len(d) if isinstance(d, list) else 0
            else:
                n = d.get("totalFound", 0)
            if n:
                return (board, ats, n)
        except Exception:
            continue
    return None


# No closing \b after "universit": the word continues into "university"/"universities", so a
# trailing boundary can never match and this silently detected nothing.
_EDU_WORDS = re.compile(r"\b(universit|college\b|institute of technology|school of)", re.I)
# "University of X" and "X University" both reduce to X, which is the .edu second-level
# domain for most name-brand institutions (duke, purdue, rice, tufts, brown, columbia).
_EDU_STRIP = re.compile(r"^(the\s+)?university\s+of\s+|\s+(university|college|"
                        r"institute\s+of\s+technology)\s*$", re.I)


def _careers_candidates(company):
    # Only the LIGHT ATS-style subdomains (careers./jobs.). We deliberately DROP
    # www.<co>.com/careers — big-company marketing roots sit behind bot-walls that hang.
    slug = _nospace(company)
    hyph = re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-")
    out = []
    for s in dict.fromkeys([slug, hyph]):
        if s:
            out += ["https://careers.%s.com" % s, "https://jobs.%s.com" % s]

    # EDUCATION IS A .EDU, AND NOTHING ABOVE WOULD EVER FIND IT. "Duke University" became
    # careers.dukeuniversity.com, so every university silently failed this probe — which is
    # why none of them were ever discovered despite being large, standing H-1B sponsors.
    # They matter disproportionately: a university is cap-exempt, so it skips the lottery.
    #
    # The bare root IS included here, unlike the .com branch above. That exclusion exists
    # because corporate marketing roots bot-wall crawlers; .edu roots generally do not, and
    # Brown is only reachable as brown.edu/careers with no careers./jobs. subdomain at all.
    if _EDU_WORDS.search(company or ""):
        base = _EDU_STRIP.sub("", (company or "").strip())
        edu = re.sub(r"[^a-z0-9]", "", base.lower())
        if edu:
            out += ["https://careers.%s.edu" % edu, "https://jobs.%s.edu" % edu,
                    "https://%s.edu/careers" % edu, "https://employment.%s.edu" % edu,
                    "https://hr.%s.edu/careers" % edu]
    return out


def discover(company):
    """(company, board_url, ats, count, confidence) or (company, None, reason, 0, '')."""
    # 1) simple-ATS slug guess FIRST — these are real APIs that fail fast for a bad slug
    #    (no slow page fetch). LOW confidence (slug collisions) — verify before trusting.
    hit = _simple_ats(company)
    if hit:
        return (company, hit[0], hit[1], hit[2], "low")
    # 2) careers-page detect chain on the company's OWN domain — HIGH confidence.
    #    Precheck reachability so a dead/hanging guessed host isn't fetched 4x.
    for url in _careers_candidates(company):
        if not _reachable(url):
            continue
        for fn in (scraper.detect_linked_ats, scraper.detect_phenom,
                   scraper.detect_successfactors, scraper.detect_jibe):
            try:
                det = fn(url)
            except Exception:
                det = None
            if det:
                burl, ats, _name = det
                try:
                    cnt = scraper.probe_board(burl, ats)
                except Exception:
                    cnt = None
                if cnt and cnt > 0:
                    return (company, burl, ats, cnt, "high")
    return (company, None, "needs-deeper-lookup", 0, "")


# Which built-in list each ats_type belongs in (for the paste-ready output).
_LIST_FOR = {"greenhouse": "EXTRA_BOARDS", "lever": "EXTRA_BOARDS", "ashby": "EXTRA_BOARDS",
             "smartrecruiters": "EXTRA_BOARDS", "workday": "WORKDAY_BOARDS",
             "oracle": "ORACLE_BOARDS", "successfactors": "SF_BOARDS", "jibe": "JIBE_BOARDS",
             "phenom": "PHENOM_BOARDS"}


def _load_extra(args):
    names = []
    for a in args:
        if a == "--sponsors":
            try:
                names += [n for n in (scraper.load_sponsors() or [])]
            except Exception:
                pass
        elif os.path.exists(a):
            with open(a, encoding="utf-8") as f:
                names += [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    return names


def _known_sources():
    """(names, urls) for everything we already scrape — the built-in list AND the boards table.

    custom_sources() is the half this used to miss. Boards added through the app or a probe
    live ONLY in the `boards` table, never in SOURCES, so every one of them read as unknown:
    the sweep re-probed them, paid the network time, and reported them as fresh discoveries.
    scraper.discover._candidates() has always consulted both; this now matches it.

    Names AND urls, because neither alone is sufficient:
      * name only — "Raytheon" probes to the URL already in SOURCES under RTX, and
        "Deere & Company" to John Deere's. Different labels, same board.
      * url only  — Mount Sinai's Oracle site is a different URL from the Jibe board we
        already scrape it through. Same employer, two platforms.
    """
    names, urls = set(), set()
    try:
        rows = list(scraper.SOURCES) + list(scraper.custom_sources())
    except Exception:
        rows = list(scraper.SOURCES)       # no DB is not a reason to skip the sweep
    for u, _a, c in rows:
        k = scraper._norm_name(c or "")
        if k:
            names.add(k)
            names.add(k.replace(" ", ""))  # "JPMorganChase" vs "JPMorgan Chase"
        if u:
            urls.add(u.rstrip("/").lower())
    return names, urls


def main():
    targets = list(CURATED_MAJORS) + _load_extra(sys.argv[1:])
    have, have_urls = _known_sources()
    todo, seen = [], set()
    for c in targets:
        nm = scraper._norm_name(c)
        if not nm or nm in seen:
            continue
        seen.add(nm)
        if nm in have or nm.replace(" ", "") in have:
            continue                       # already scraped
        if _BODYSHOP_RE.search(c):
            continue                       # body-shop guard
        todo.append(c)

    # Fast-fail session for the whole run (no retries, short timeouts) — restored after.
    _orig_session = scraper.SESSION
    scraper.SESSION = _fast_session()

    print("Probing %d employers (not already in SOURCES or the boards table)...\n" % len(todo),
          flush=True)
    found, lowconf, misses, dupes = [], [], [], []
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
            futs = {ex.submit(discover, c): c for c in todo}
            for fut in concurrent.futures.as_completed(futs):
                company = futs[fut]
                try:
                    company, url, ats, cnt, conf = fut.result(timeout=45)  # cap a slow host
                except Exception:
                    url, ats, cnt, conf = None, "timeout", 0, ""
                if url and url.rstrip("/").lower() in have_urls:
                    # The name was unknown but the BOARD is not. Reporting this as a discovery
                    # is what put Raytheon (RTX's URL) and Deere & Company (John Deere's) in a
                    # paste-ready block: nine "hits", one genuinely new.
                    dupes.append((company, url, ats))
                    print("  ==  %-28s already scraped under another name  %s"
                          % (company[:28], url[:52]), flush=True)
                elif not url:
                    misses.append(company)
                    print("  --  %-28s needs deeper lookup (web-search Workday/Oracle tenant)"
                          % company[:28], flush=True)
                elif conf == "high":
                    found.append((company, url, ats, cnt))
                    print("HIT  %-28s %-15s %-6s %s" % (company[:28], ats, cnt, url[:60]), flush=True)
                else:
                    lowconf.append((company, url, ats, cnt))
                    print("?LOW %-28s %-15s %-6s %s  (verify identity)"
                          % (company[:28], ats, cnt, url[:60]), flush=True)
    finally:
        scraper.SESSION = _orig_session

    print("\n%d HIGH-confidence, %d low-confidence, %d already-scraped board(s) under another "
          "name, %d need deeper lookup (of %d).\n"
          % (len(found), len(lowconf), len(dupes), len(misses), len(todo)))
    if dupes:
        # Named, not just counted: a repeat offender here is a missing alias in SOURCES,
        # which is worth fixing at the source rather than re-filtering every sweep.
        print("# Already scraped under a different label — NOT new:")
        for company, url, ats in dupes:
            print("#   %-28s %-14s %s" % (company[:28], ats, url[:58]))
        print()

    # paste-ready, grouped by destination list
    groups = {}
    for company, url, ats, cnt in found + lowconf:
        groups.setdefault(_LIST_FOR.get(ats, "EXTRA_BOARDS"), []).append((url, ats, company, cnt))
    for lst in sorted(groups):
        print("# ---> %s" % lst)
        for url, ats, company, cnt in groups[lst]:
            print('    ("%s", "%s", "%s"),   # ~%s' % (url, ats, company, cnt))
        print()
    if misses:
        print("# Needs web-search tenant lookup (likely Workday/Oracle/Brassring) or Adzuna fallback:")
        print("#   " + ", ".join(misses))


if __name__ == "__main__":
    main()
