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
from urllib.parse import urlparse

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


def _resolve_origin(url, timeout=5):
    """(reachable, the origin worth probing) -- the redirect TARGET, not what we guessed.

    _reachable() already follows redirects and then throws the answer away, and that discard
    was the single biggest class of missed board here: a guessed careers host very often 301s
    to the real one on a DIFFERENT hostname. Measured 2026-09-09 -- jobs.lowes.com 301s to
    talent.lowes.com, and detect_phenom on the target returns Lowe's real Workday board with
    4,239 postings, while the same call against jobs.lowes.com gets a Cloudflare 301, is not
    200, and reads as "no ATS lives here". 1,506 H-1B filings behind that one redirect.

    The ORIGIN, deliberately, not the final URL: every detector appends its own path
    (/widgets, /api/...) to scheme+host, so carrying a redirect's path through would aim them
    at the wrong place.
    """
    try:
        r = scraper.SESSION.get(url, headers=_H, timeout=timeout,
                                allow_redirects=True, stream=True)
        r.close()
        if r.status_code >= 500:
            return False, url
        p = urlparse(r.url or url)
        if not p.scheme or not p.netloc:
            return True, url
        return True, "%s://%s" % (p.scheme, p.netloc)
    except Exception:
        return False, url

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


# Trailing legal/generic words, ANCHORED and word-bounded. Both properties are load-bearing and
# the previous version had neither: it did `b.replace(" co", "")` on the whole string, so any
# name containing " co" as a SUBSTRING was corrupted -- "Johnson Controls" became
# "johnsonntrols", "Lowe's Companies, Inc." became "lowesmpanies" and "Verizon Communications"
# became "verizonmmunications". Measured 2026-09-09: those three and their class are part of why
# 646 of 771 probed employers resolved no careers page at all. A corrupted slug cannot resolve,
# so the failure looked like "this company has no board" rather than like a bug.
_TRAILING_GENERIC = re.compile(
    r"[\s,.&-]*\b(?:inc|incorporated|corp|corporation|co|company|companies|group|holding|"
    r"holdings|technologies|technology|solutions|systems|enterprises|industries|plc|llc|l\.l\.c|"
    r"llp|pllc|ltd|limited|sa|nv|ag|gmbh|usa|us)\b[\s,.]*$")


def _nospace(name):
    """A domain-shaped slug: lowercase, trailing legal/generic words removed, punctuation gone.

    Stripped REPEATEDLY, because the tail is often two words deep and the short form is usually
    the domain: "Lowe's Companies, Inc." -> inc -> companies -> **lowes**, which is the real
    careers.lowes.com. One pass would have stopped at "lowescompanies" and resolved nothing.

    Only the END is touched, so a generic word that is part of the actual name survives --
    "Johnson Controls" keeps controls, "Verizon Communications" keeps communications. That is
    the whole difference between this and what it replaced.
    """
    b = (name or "").lower().replace("’", "").replace("'", "")
    b = re.sub(r"^the\s+", "", b)
    prev = None
    while prev != b and b:
        prev = b
        b = _TRAILING_GENERIC.sub("", b)
    return re.sub(r"[^a-z0-9]", "", b or (name or "").lower())


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


# ---- .edu domain resolution ------------------------------------------------------------------
# Universities are worth getting right: an H-1B filed by one is cap-exempt, so it skips the
# lottery. They were also the worst-served names here. The old guess -- strip "University of",
# glue the rest together, add ".edu" -- got 4 of the 32 universities in careers_us.md right
# (missouri, towson, kean, drexel) and missed the other 28, so none of them were reachable.
#
# The cause is that US institutions do not share one naming convention; they share about seven,
# and which one an institution uses is NOT derivable from its name:
#
#     University of Michigan          umich.edu       "u" + a TRUNCATION of the state
#     University of Oregon            uoregon.edu     "u" + the whole state
#     University of South Carolina    sc.edu          initials, no "u"
#     Old Dominion University         odu.edu         initials + "u"
#     Wichita State University        wichita.edu     the name with "State" DROPPED
#     Arkansas State University       astate.edu      first initial + "state"
#     Cal State University Fullerton  fullerton.edu   the CAMPUS alone
#     College of Charleston           cofc.edu        initials, keeping the "of" as a word
#
# So generate every convention and let the network say which one answers. The list is ordered
# by how often each convention wins, which is the tie-break _resolve_edu_domains falls back
# on when two domains score identically.
_EDU_WORDS = re.compile(r"\b(universit|college\b|institute of technology|school of)", re.I)
# Dropped when reducing a name to its distinguishing words. "state" is NOT here: it is generic
# in a name ("Kansas State") but load-bearing in a domain (kansasstate.edu), so it is removed
# only in the one variant that needs it gone.
_EDU_FILLER = ("the", "of", "at", "and", "university", "universities", "college", "institute",
               "school")


def _edu_words(company):
    """The distinguishing words of an institution name, lowercased and filler-free.
    "University of South Carolina" -> ["south", "carolina"]."""
    n = re.sub(r"[^A-Za-z0-9 ]", " ", company or "").lower()
    return [w for w in n.split() if w and w not in _EDU_FILLER]


def _edu_domain_candidates(company):
    """Ordered .edu second-level-domain guesses for an institution name.

    Pure string work, no network: test_edu_domains.py asserts that the real domain of every
    university in careers_us.md appears in this list, which is the half of the fix that can
    be frozen in CI (the resolver half needs the internet).
    """
    w = _edu_words(company)
    if not w:
        return []
    allw = [x for x in re.sub(r"[^A-Za-z0-9 ]", " ", (company or "")).lower().split()
            if x and x not in ("the", "at", "and")]
    noof = [x for x in allw if x != "of"]
    base = "".join(w)
    out = [
        base,                                    # towson, drexel, missouri, kansasstate
        "u" + base,                              # uoregon, uidaho, utulsa, uakron
        "".join(x[0] for x in w) + "u",          # odu, jmu, usu, wku, shsu, ksu, ndsu
        "".join(x[0] for x in noof),             # unh, usm, siue, bc
        "".join(x[0] for x in w),                # sc
        "".join(x for x in w if x != "state"),   # wichita, weber, montclair
        # "of" survives as a WORD, not a letter: College of Charleston is cofc, not cc.
        "".join(x if x == "of" else x[0] for x in allw),
        w[-1],                                   # fullerton, lafayette
        "".join(x[0] for x in noof[:-1]) + w[-1],   # csuchico, ucdenver, udmercy
        w[0],                                    # louisiana (from "... at Lafayette")
        "u" + base[:4],                          # umich
        "u" + base[:3],                          # udel
        w[0][0] + "state",                       # astate
        w[0][0] + "-state",                      # k-state
    ]
    seen, uniq = set(), []
    for d in out:
        d = re.sub(r"[^a-z0-9-]", "", d).strip("-")
        if len(d) > 1 and d not in seen:
            seen.add(d)
            uniq.append(d)
    return uniq


# A homepage that answers but tells us nothing: an explicit bot-wall status, or a 200 whose
# body is a JS challenge shell. wichita.edu returns 200 and 212 bytes of Incapsula, which
# scored as "no title" and lost to a wrong school; treat it as what it is -- proof the host
# exists, no proof of whose it is.
_WALL_STATUS = (401, 403, 406, 429, 451)
_WALL_BODY = re.compile(r"_incapsula_|distil_r_|/cdn-cgi/challenge|are you a robot|"
                        r"enable javascript to continue", re.I)
# Words a homepage title adds that say nothing about WHICH institution it is, so they must not
# count against an otherwise exact match ("Home - Boston College").
_TITLE_FILLER = frozenset(("home", "homepage", "welcome", "official", "site", "website", "the",
                           "of", "at", "and", "a", "an", "to", "for", "in", "index", "main",
                           "page", "us", "edu", "www", "login", "portal"))


def _edu_name_words(company):
    """The institution's own name as comparable words, filler stripped but "university" and
    "state" KEPT -- those are exactly what separates "University of Arkansas" from
    "Arkansas State University"."""
    return [x for x in re.sub(r"[^A-Za-z0-9 ]", " ", (company or "")).lower().split()
            if x and x not in ("the", "of", "at", "and")]


def _edu_title_match(title, company):
    """(fraction of the name's words present, count of UNEXPLAINED words, names-it) for a
    homepage title. Pure string work, so the picking rule is testable without the network.

    Three numbers, because no one of them decides it:

      * the fraction alone cannot separate an institution from a DIFFERENT institution whose
        name contains it. "Boston College" is 2/2 words of "Boston Baptist College"
        (boston.edu) and also 2/2 of "Home - Boston College" (bc.edu).
      * the fraction is also routinely LOW for the right school, because homepages are titled
        the way people speak: drexel.edu says "Drexel Home", csuchico.edu says "Chico State".
        Demanding 0.75 threw away both correct answers.
      * so the deciding number is what the title contains that the NAME CANNOT EXPLAIN.
        "baptist" is unexplained; "home" is filler; and "University of Denver" is entirely
        unexplained by "Drexel University" even though both share a word.
    """
    words = _edu_name_words(company)
    if not words or not title:
        return 0.0, 99, False
    toks = set(t for t in re.split(r"[^a-z0-9]+", (title or "").lower()) if t)
    hit = sum(1 for w in words if w in toks)
    extra = len([t for t in toks if t not in words and t not in _TITLE_FILLER
                 and not t.isdigit() and len(t) > 1])
    # Does the title name THIS institution -- a word unique to it, not "university"/"state"?
    named = any(w in toks for w in _edu_words(company) if w != "state")
    return float(hit) / len(words), extra, named


def _edu_root(d, timeout=10):
    """Fetch a guessed .edu root. Returns (title, host, walled).

    Tries the bare domain then www: fullerton.edu serves a certificate valid only for
    www.fullerton.edu, and csuchico.edu answers on www alone -- both are the right domain
    failing for a reason that has nothing to do with whose it is.
    """
    for url in ("https://%s.edu" % d, "https://www.%s.edu" % d):
        try:
            r = scraper.SESSION.get(url, headers=_H, timeout=timeout, allow_redirects=True)
        except Exception:
            continue
        m = re.match(r"https?://([^/]+)", r.url or url)
        host = re.sub(r":\d+$", "", re.sub(r"^www\.", "", (m.group(1) if m else "").lower()))
        if not host:
            continue
        body = r.text or ""
        if r.status_code in _WALL_STATUS or _WALL_BODY.search(body[:4000]):
            return None, host, True
        if r.status_code != 200:
            continue
        t = re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I)
        if not t:      # some roots are a JS shell whose only name is in a meta/heading
            t = re.search(r'<meta[^>]+property=["\']og:(?:site_name|title)["\'][^>]+'
                          r'content=["\']([^"\']+)', body, re.I) or \
                re.search(r"<h1[^>]*>(.*?)</h1>", body, re.S | re.I)
        title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", t.group(1))).strip() if t else ""
        if not title:
            return None, host, True
        return title, host, False
    return None, None, False


def _resolve_edu_domains(company, limit=2, cap=11):
    """Which guessed .edu domains actually BELONG to this institution, best match first.

    That a domain resolves is no evidence, and neither is its page mentioning the name.
    Scored on body text, three of the 32 institutions in careers_us.md resolved to a
    DIFFERENT school outright and three more carried one as a second candidate: mu.edu
    redirects to Marquette, whose homepage says "Michigan" somewhere, so "University of
    Michigan" resolved to marquette.edu; uark.edu claimed Arkansas State; boston.edu
    (Boston Baptist) claimed Boston College; utah.edu, colorado.edu and hampshire.edu
    each rode along behind the right answer. The title is the one string on a university
    homepage that is reliably the institution's own name.

    Two tiers, because the correct domain is often the one that refuses to talk: umich.edu
    and missouri.edu both answer 403 and wichita.edu serves a bot challenge, while the WRONG
    domains answer 200 happily. A verified title always wins; a walled host is kept as a
    fallback rather than losing to a school it isn't, and probe_board is still the last gate.
    """
    strong, weak = [], []
    for i, d in enumerate(_edu_domain_candidates(company)[:cap]):
        title, host, walled = _edu_root(d)
        if not host or host in [h for _a, _b, _c, h in strong] + [h for _a, h in weak]:
            continue
        if walled:
            weak.append((-i, host))
            continue
        score, extra, named = _edu_title_match(title, company)
        # Either the title is mostly the name, or everything in it is explained BY the
        # name and it names this school. The second clause is what keeps "Drexel Home"
        # and "Chico State"; `named` is what still rejects "University of Denver".
        if score >= 0.75 or (extra == 0 and named):
            strong.append((score, -extra, -i, host))
    strong.sort(reverse=True)
    if strong:
        # Only the BEST-TIED candidates, never merely the first `limit` to qualify. This is
        # the rule that drops the near-miss school outright: boston.edu (Boston Baptist,
        # 1.00/1 extra) loses to bc.edu (1.00/0) for "Boston College", and uark.edu
        # (University of Arkansas, 0.67) loses to astate.edu (1.00) for "Arkansas State".
        best = strong[0][:2]
        return [h for s, e, _i, h in strong if (s, e) == best][:limit]
    weak.sort(reverse=True)
    return [h for _i, h in weak[:limit]]


def _careers_candidates(company):
    # Only the LIGHT ATS-style subdomains (careers./jobs.). We deliberately DROP
    # www.<co>.com/careers -- big-company marketing roots sit behind bot-walls that hang.
    slug = _nospace(company)
    # The suffix-KEEPING form too, because both shapes are real domains and which one an
    # employer uses is not predictable: careers.lowes.com needs the stripped slug, while a
    # company whose generic word IS part of its domain needs this one. _strict_norm_name is
    # the cautious stripper (inc/llc/corp only), reused rather than hand-rolled -- see the
    # note beside it about why the aggressive one must not be pointed at arbitrary names.
    # Stripped first: it is the likelier of the two, and _reachable() stops at the first hit.
    kept = scraper._strict_norm_name(company).replace(" ", "")
    hyph = re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-")
    out = []
    for s in dict.fromkeys([slug, kept, hyph]):
        if s:
            out += ["https://careers.%s.com" % s, "https://jobs.%s.com" % s]

    # EDUCATION IS A .EDU, AND NOTHING ABOVE WOULD EVER FIND IT. "Duke University" became
    # careers.dukeuniversity.com, so every university silently failed this probe.
    #
    # Unlike the .com branch, this one costs network time before it returns: resolving the
    # domain IS the hard part for an institution, so it happens here rather than being guessed
    # per-URL. The bare root is also included, which the .com branch excludes -- corporate
    # marketing roots bot-wall crawlers, .edu roots generally do not, and Brown is only
    # reachable as brown.edu/careers with no careers./jobs. subdomain at all.
    if _EDU_WORDS.search(company or ""):
        for dom in _resolve_edu_domains(company):
            out += ["https://careers.%s" % dom, "https://jobs.%s" % dom,
                    "https://%s/careers" % dom, "https://employment.%s" % dom,
                    "https://hr.%s/careers" % dom]
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
    # Deduped on the RESOLVED origin, not on the guessed URL: the extra slug variants mean
    # several candidates now land on the same host, and running five detectors against it
    # twice is pure network time. This is what pays for the wider candidate list.
    seen_origins = set()
    for url in _careers_candidates(company):
        ok, url = _resolve_origin(url)
        if not ok or url in seen_origins:
            continue
        seen_origins.add(url)
        # detect_eightfold is last: it is the only one that can be true for a host no other
        # detector claims, and Eightfold gates most tenants, so it fails often and cheaply.
        for fn in (scraper.detect_linked_ats, scraper.detect_phenom,
                   scraper.detect_successfactors, scraper.detect_jibe,
                   scraper.detect_eightfold):
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
