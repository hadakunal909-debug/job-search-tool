#!/usr/bin/env python3
"""Harvest real brand logos into static/logos/, and repair company_domains.json on the way.

    python scripts/build_logos.py --audit-domains   # CSV of stored vs P856. Writes no JSON.
    python scripts/build_logos.py --write-domains   # rewrite company_domains.json from P856
    python scripts/build_logos.py                   # resume the harvest
    python scripts/build_logos.py --report          # census of the ledger. Writes nothing.
    python scripts/build_logos.py --check           # the CI gate

WHY THIS EXISTS. /companies used to ask Google's faviconV2 for every tile, with
fallback_opts=TYPE,SIZE,URL -- which asks Google to GENERATE an icon, so a domain with no
favicon still answers HTTP 200 and no error handler can fire. Measured over 150 companies that
had a stored domain (75 highest H-1B volume + 75 random): 46.7% usable, 22.7% a solid colour
block (Apple, Capgemini, Intel, IBM), 14.0% a monochrome browser-chrome glyph, 14.0% under 48px
upscaled into a 48px tile, 2.0% a 404, 0.7% a blank 200. 53% was not a usable brand logo.

So the logos are harvested ONCE, verified, and committed as static files. Not hotlinked:
Clearbit's free logo API -- the one every tutorial still recommends -- was shut off on
2025-12-08, and a page whose images come from a free third-party service breaks on someone
else's schedule. static/logos/ needs no deploy change: build_deploy_zip.py walks static/ with
os.walk and .cpanel.yml does `cp -rf "$SRC/static"`.

WHY WIKIDATA. Measured: property P154 (logo image) resolves a real vector brand logo for 24 of
the top 25 H-1B employers, at 660 B to 10 KB, with no API key. And property P856 (official
website) is a CURATED domain, which is the thing company_domains.json never had -- see
--audit-domains below.

Run from the app directory: companies.json, company_domains.json and static/ are all read
relative to the cwd, exactly as web.py reads them.
"""
import argparse
import collections
import csv
import datetime
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core                                                    # noqa: E402

COMPANIES_JSON = "companies.json"
DOMAINS_JSON = "company_domains.json"
LEDGER = "logo_harvest.json"
AUDIT_CSV = "company_domains_audit.csv"
DISCOVER_CSV = "company_domains_discovered.csv"
DOMAINS = {}                 # norm_company -> domain; filled by run_harvest, see load_domains
LOGO_DIR = os.path.join("static", "logos")
MANIFEST = os.path.join(LOGO_DIR, "index.json")

WD_API = "https://www.wikidata.org/w/api.php"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"

# Wikimedia's UA policy asks for a contact, and a generic UA is part of why the first threaded
# attempt at this looked like a dead data source. See PACE below.
UA = ("JobMatch-logo-harvest/1.0 "
      "(https://stemjobs1.astrochakra.co; hada.k@northeastern.edu)")
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# PACE, AND WHY THERE IS NO --workers FLAG. Measured on the same 45 companies: a 12-thread pool
# returned 84% MISS; sequential, with the descriptive UA above and this delay, returned 96%.
# A throttled fetch is indistinguishable from "this company has no logo" at the call site, and
# a miss gets written to the ledger as a VERDICT -- so a flag that exists is a flag somebody
# uses once and poisons the ledger for every later run. The batching below is what buys the
# speed instead: wbgetentities and imageinfo both take 50 titles per request.
PACE = 0.25
BATCH = 50

# THE ENTITY GATE. Measured failures that made this non-optional rather than defensive:
# "EY" resolves to Q37484767, a FAMILY NAME. "Arctic Wolf" resolves to Q216441, the animal.
# "Waymark" resolves to Q10145, a place. Harvesting P154 off a name-only match would paint a
# wolf on a cybersecurity company, and a wrong logo is the one outcome worse than no logo.
#
# Scanning the description string for words like "company" is what produced the EY result, so
# this walks P31 (instance of) up P279 (subclass of) to a real organisation root instead.
ORG_ROOTS = {
    "Q43229",       # organization
    "Q4830453",     # business
    "Q3918",        # university
    "Q16917",       # hospital
    "Q327333",      # government agency
    "Q163740",      # nonprofit organization
    "Q4287745",     # medical organization
    "Q2385804",     # educational institution
    "Q783794",      # company
    "Q891723",      # public company
}
HARD_REJECT = {
    "Q5",           # human
    "Q101352",      # family name
    "Q202444",      # given name
    "Q11424",       # film
    "Q482994",      # album
    "Q7366",        # song
    "Q4167410",     # Wikimedia disambiguation page
    "Q13406463",    # Wikimedia list article
    "Q486972",      # human settlement
    "Q3957",        # town
    "Q532",         # village
    "Q16521",       # taxon, which is what "Arctic Wolf" hit
}
WALK_DEPTH = 6

# A P154 statement can point at a HISTORICAL logo. Measured: Intel's first claim is
# "Intel logo (1968-2006).svg" and Visa's is "Visa Inc. logo (1992-1999).svg".
YEAR_RANGE = re.compile(r"\((?:19|20)\d{2}\s*[-–—]\s*((?:19|20)\d{2}|present)?\)", re.I)
STALE_WORD = re.compile(r"histor|old\s*logo|former|\bprevious\b", re.I)

# Corporate suffixes that never appear in a domain, longest first so "corporation" is stripped
# before "corp" can match its head. Lifted from scripts/build_company_domains.py, which this
# script replaces.
SUFFIXES = ("incorporated", "corporation", "technologies", "international", "limited",
            "holdings", "company", "group", "corp", "inc", "llc", "ltd", "plc", "lp", "co",
            "usa", "us", "na")
_SLD = ("co", "com", "ac", "org", "net", "gov", "edu")

# Hosts belonging to a hiring PLATFORM rather than to an employer. Kept for the no-entity tail;
# the real defence is the one-domain-one-company invariant in --check, because this list has an
# unenumerable tail and always will. Measured today: adp.com, appcast.io, careerplug.com,
# usajobs.gov and governmentjobs.com are each assigned to two or three DIFFERENT employers in
# companies.json, and none of them was on any earlier version of this list.
PLATFORM_HOSTS = (
    "myworkdayjobs.com", "myworkdaysite.com", "myworkday.com", "greenhouse.io", "lever.co",
    "ashbyhq.com", "smartrecruiters.com", "smartrecruiters.net", "icims.com", "jobvite.com",
    "workable.com", "bamboohr.com", "taleo.net", "successfactors.com", "sapsf.com",
    "avature.net", "jobdiva.com", "ultipro.com", "paylocity.com", "oraclecloud.com",
    "eightfold.ai", "recruitics.com", "rippling.com", "isolvedhire.com", "apploi.com",
    "phenompeople.com", "peoplefluent.com", "silkroad.com", "brassring.com", "dayforcehcm.com",
    "adzuna.com", "indeed.com", "linkedin.com", "glassdoor.com", "ziprecruiter.com",
    "workatastartup.com", "applytojob.com", "careerpuck.com", "talentnet.community",
    "comparably.com", "equest.com", "jobs.net", "jazz.co", "jazzhr.com", "breezy.hr",
    "recruitee.com", "appcast.io", "careerplug.com", "usajobs.gov", "governmentjobs.com",
    "adp.com", "paycomonline.net", "paycor.com", "clearcompany.com", "kula.ai",
)

# A brand no rule can reach. Every heuristic in this repo has one of these, and shipping it on
# day one rather than after the first wrong logo lands is the point.
# slug -> a Commons file title, or "" to force the monogram.
#
# linkedin: wbsearchentities("LinkedIn") does not return LinkedIn itself in the top seven hits.
# It returns LinkedIn Learning, LinkedIn Ireland and LinkedIn News, and Learning carries a real
# logo -- so the loose subset match in label_ok shipped LinkedIn Learning's logo for LinkedIn
# until that was tightened. The right entity is Q213660 and its logo is this file.
# city-of-new-york: the employer IS a municipal government, so is_place() below correctly
# refuses to guess an entity for it -- and correctly costs us the one Commons file in this
# corpus where a city's own mark is the right answer. Measured: of the 13 entities is_place
# rejects among the 1,148 we already ship, this is the only one that loses a correct logo and
# has no domain to fall back to. So it is named here rather than weakening the rule.
# rochester-institute-of-technology: the other side of the same coin, and the reason the list
# is TWO entries and not a loosened rule. is_place refuses its entity, and its stored domain is
# a name-exact guess (rochesterinstituteoftechnology.com) that declares no icon -- so tier 2
# cannot save it either. 105 H-1B filings, so it is worth a line. Wisconsin loses its logo to
# the same rule and does NOT get a line: 0 open roles, 0 filings, and what it lost was
# wisconsin.gov's favicon.
# A DOMAIN NO RULE CAN REACH, which is a different list from LOGO_OVERRIDE below because the
# failure is different: the name is real, the guess is plausible, and it belongs to somebody else.
#
# 43% of the employers with no domain have a single distinctive token, and they carry 45% of the
# filings in that cohort -- so they cannot be refused wholesale. But a one-word name cannot be
# disambiguated from a page: measured on the head of the discovery pass, "Alphabet" (23,240 H-1B
# filings, so unambiguously Google's parent) corroborates PERFECTLY against alphabet.com, which is
# BMW's fleet-management business. Both the address and the page agree; they just agree about the
# wrong company. No heuristic available here separates the two, so the answer is a named entry and
# a human skim of company_domains_discovered.csv, which is sorted by filings for exactly that.
#
# keyed on core.norm_company. "" means "we have no domain and guessing is worse than not".
DOMAIN_OVERRIDE = {
    "alphabet": "abc.xyz",
    # HAND-READ 2026-08-23, and this list is the ANSWER to --discover-domains rather than a
    # supplement to it. Three tightening passes took the guess from ~1-in-3 wrong to ~1-in-6, and
    # then it stopped converging, because the ways a guessed domain lies are open-ended: a broker
    # with novel copy (crusoe.com sells "Strategic-Grade domain names"), an acquisition
    # (altair.com now redirects to siemens.com, and Altair's own mark is what a card wants), a
    # homonym (nikola.com is Nikola Engineering, not Nikola Motor; titan.com is a wealth manager;
    # paradigm.com sells loudspeakers; maplebear.com is a Canadian school franchise, not
    # Instacart's legal entity), a .io squat with no title at all (apexsystems.io), or simply a
    # different company of the same name (persistentsystems.com is a US radio maker, not the
    # Indian IT firm). None of those is reachable by reading a page harder.
    #
    # So the pass stays, its output does not ship, and its CSV is a WORKLIST. These twenty were
    # opened and checked by eye; they carry ~19k H-1B filings between them, which is most of the
    # value the whole pass was chasing. Four are the redirect TARGET rather than the guess,
    # because that is where the icons actually live.
    "birlasoft": "birlasoft.com",
    "catalent": "catalent.com",
    "centraprise": "centraprise.com",
    "coforge": "coforge.com",
    "cotiviti": "cotiviti.com",
    "flatiron health": "flatiron.com",
    "highmark health": "highmark.com",
    "itc infotech": "itcinfotech.com",
    "intraedge": "intraedge.com",
    "ltimindtree": "ltm.com",
    "marlabs": "marlabs.com",
    "mastech digital": "mastechdigital.com",
    "national veterinary associates": "nva.com",
    "open avenues foundation": "openavenuesfoundation.org",
    "paycom payroll": "paycom.com",
    "qualcomm": "qualcomm.com",
    "samsung electronics america": "samsung.com",
    "tech mahindra": "techmahindra.com",
    "visionet systems": "visionet.com",
    "west pharmaceutical services": "westpharma.com",
    # "" means we have no domain and guessing is worse than not. Each of these was guessed
    # plausibly and checked, and each guess was somebody else.
    "altair": "",
    "apex systems": "",
    "crusoe": "",
    "maplebear": "",
    "nikola": "",
    "paradigm": "",
    "persistent systems": "",
    "titan": "",
}

LOGO_OVERRIDE = {
    "linkedin": "LinkedIn Logo.svg",
    "city-of-new-york": "NYC Logo Wolff Olins.svg",
    "rochester-institute-of-technology": "RIT 2018 logo short orange.svg",
}


def slugify(name):
    """The asset filename stem for a company. Mirrored in static/companies.js::slug.

    ASCII, lowercase, [a-z0-9-] only, so there is nothing to URL-escape and no case collision
    between a case-insensitive dev box and a case-sensitive host.
    """
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-") or "x"


# Tokens that carry no identity. Dropped ONLY when longer than one character: core.norm_company
# turns "U.S. Bank" into "u s bank", so a single letter is an acronym part and has to survive or
# that tile reads UB. "Amazon.com Services LLC" normalises to "amazon com services", which is
# why "com" has to go or that tile reads AC.
_MONO_SKIP = {"com", "net", "org", "the", "and", "of", "for", "www"}


def initials(name):
    """Two letters for the monogram. Mirrored in static/companies.js::initials.

    Built from the NORMALISED name, so core's legal-suffix strip runs first and Inc/LLC/Group
    never reach the tile. Measured against the corpus: "Amazon.com Services LLC" is AS,
    "U.S. Bank" is US, "Ernst & Young" is EY, "10x Genomics" is 10.
    """
    key = core.norm_company(name) or (name or "")
    words = [w for w in re.split(r"[^0-9A-Za-z]+", key) if w
             and not (len(w) > 1 and w in _MONO_SKIP)]
    if not words:
        return "?"
    if not words[0][0].isalpha():
        return words[0][:2].upper()
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[1][0]).upper()






# ---------------------------------------------------------------- transport

class Net:
    """One paced session. Pacing is per HOST, so the Wikidata delay never taxes a site fetch.

    THE TRANSPORT/VERDICT DISTINCTION LIVES HERE. Everything this class raises is a transport
    failure, and the caller records it as `deferred` and retries it forever. Only a fetch that
    decoded into an image and then failed a rule earns `rejected`. Conflate the two and one
    rate-limited afternoon permanently blanks a thousand employers, with no way to tell that
    from a thousand employers who genuinely have no logo.
    """

    MAX_BYTES = 1 << 20        # 1 MB. Nothing legitimate here is close; a wrong URL can be.

    def __init__(self, pace=PACE):
        import requests
        self.s = requests.Session()
        self.pace = pace
        self.last = {}
        self.strikes = collections.Counter()
        self.calls = 0

    def _wait(self, host):
        gap = time.time() - self.last.get(host, 0)
        if gap < self.pace:
            time.sleep(self.pace - gap)
        self.last[host] = time.time()

    # A BENCH IS A COOLDOWN, NOT A DEATH SENTENCE. The first version benched a host for the rest
    # of the run after three throttles, so one slow patch near the start made every remaining
    # company fail. They were all recorded as `deferred` and so nothing was corrupted, but the
    # run was wasted and the output looked identical to "this source has no data".
    BENCH_S = 60

    def get(self, url, params=None, ua=UA, timeout=(5, 20), binary=False):
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        if self.strikes[host] >= 3:
            since = time.time() - self.last.get(host, 0)
            if since < self.BENCH_S:
                raise IOError("host %s cooling down, %ds left"
                              % (host, self.BENCH_S - int(since)))
            self.strikes[host] = 0
        self._wait(host)
        self.calls += 1
        r = self.s.get(url, params=params, headers={"User-Agent": ua},
                       timeout=timeout, stream=binary, allow_redirects=True)
        if r.status_code in (429, 503):
            self.strikes[host] += 1
            wait = r.headers.get("Retry-After")
            if wait and wait.isdigit():
                time.sleep(min(30, int(wait)))
            raise IOError("throttled %s on %s" % (r.status_code, host))
        self.strikes[host] = 0
        if r.status_code != 200:
            return None                        # a real answer: "not here". Not a transport fault.
        if not binary:
            return r
        buf = io.BytesIO()
        for chunk in r.iter_content(65536):
            buf.write(chunk)
            if buf.tell() > self.MAX_BYTES:
                return None                    # oversize is a verdict, not an error
        return buf.getvalue()

    def json(self, url, params):
        r = self.get(url, params=params)
        if r is None:
            raise IOError("non-200 from an API that should always answer: %s" % url)
        return r.json()


# ---------------------------------------------------------------- the ledger

def load_ledger():
    try:
        with open(LEDGER, encoding="utf-8") as fh:
            return (json.load(fh) or {}).get("rows") or {}
    except Exception:
        return {}


def save_ledger(rows, note=""):
    """Atomic, because a 30-minute run WILL be interrupted and a truncated ledger is worse
    than none. Same open-tmp-then-replace shape web.py::_snapshot_write uses."""
    blob = {"note": note or ("company slug -> why it has or lacks a logo. Built by "
                             "scripts/build_logos.py. Committed, never deployed."),
            "built_at": datetime.date.today().isoformat(),
            "rows": rows}
    tmp = LEDGER + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(blob, fh, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, LEDGER)


# ---------------------------------------------------------------- Wikidata

def wd_search(net, name):
    """Candidate QIDs for a name, best first. One request."""
    j = net.json(WD_API, {"action": "wbsearchentities", "search": name[:300],
                          "language": "en", "uselang": "en", "type": "item",
                          "limit": 7, "format": "json"})
    return [(h["id"], h.get("label") or "", h.get("description") or "")
            for h in (j.get("search") or [])]


def wd_entities(net, qids, props="claims|labels"):
    """Claims for up to 50 QIDs in ONE request. Measured: 50 ids came back in 2.6s.

    This is what makes the harvest 15 to 35 minutes instead of an hour -- the per-company cost
    is one search plus a share of a batch, not one request per claim lookup.
    """
    out = {}
    for i in range(0, len(qids), BATCH):
        chunk = [q for q in qids[i:i + BATCH] if q]
        if not chunk:
            continue
        j = net.json(WD_API, {"action": "wbgetentities", "ids": "|".join(chunk),
                              "props": props, "languages": "en", "format": "json"})
        out.update(j.get("entities") or {})
    return out


def claim_values(claims, prop):
    """The 'id'/string values of a property, preferred rank first, end-dated ones dropped.

    P582 (end time) is the signal that separates a current logo from a historical one, and it
    is why this returns statements in an order rather than just the first value.
    """
    stmts = (claims or {}).get(prop) or []
    live, ended = [], []
    for st in stmts:
        snak = (st.get("mainsnak") or {})
        if snak.get("snaktype") != "value":
            continue
        val = (snak.get("datavalue") or {}).get("value")
        if isinstance(val, dict):
            val = val.get("id") or val.get("text") or val.get("amount")
        if not isinstance(val, str):
            continue
        if st.get("rank") == "deprecated":
            continue        # Wikidata's own marker for "this statement is wrong". Google's P856
                            # carries a deprecated duplicate of its live one.
        rank = 0 if st.get("rank") == "preferred" else 1
        if (st.get("qualifiers") or {}).get("P582"):
            ended.append((rank, val))
        else:
            live.append((rank, val))
    # STABLE sort, so statement order survives inside a rank group. That order is Wikidata's own
    # newest-first convention for P154 and it is better than any tiebreak invented here.
    live.sort(key=lambda x: x[0])
    ended.sort(key=lambda x: x[0])
    return [v for _, v in live], [v for _, v in ended]


class ClassCache:
    """P279 parents per QID, persisted, because thousands of companies share a few hundred
    classes. After the first ~100 companies the subclass walk costs nothing."""

    PATH = "wikidata_classes.json"

    def __init__(self, net):
        self.net = net
        try:
            with open(self.PATH, encoding="utf-8") as fh:
                self.map = (json.load(fh) or {}).get("parents") or {}
        except Exception:
            self.map = {}
        self.dirty = False

    def warm(self, qids):
        """Fetch every unknown class in ONE batched call per 50.

        THIS IS NOT AN OPTIMISATION, IT IS THE DIFFERENCE BETWEEN WORKING AND NOT. The first
        version fetched one class per request, so a cold cache turned the subclass walk into
        hundreds of extra sequential calls to wikidata.org, which throttled, which benched the
        host, which made every remaining company in the run fail. A cold run looked exactly like
        a dead data source -- the same confusion the threading attempt produced.
        """
        miss = sorted({q for q in qids if q and q not in self.map})
        if not miss:
            return
        ents = wd_entities(self.net, miss, props="claims")
        for q in miss:
            cl = (ents.get(q) or {}).get("claims") or {}
            self.map[q], _ = claim_values(cl, "P279")
        self.dirty = True

    def parents(self, qid):
        if qid not in self.map:
            self.warm([qid])
        return self.map.get(qid) or []

    def flush(self):
        if not self.dirty:
            return
        tmp = self.PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump({"note": "QID -> its P279 (subclass of) parents. Cache for "
                               "scripts/build_logos.py's entity gate.",
                       "parents": self.map}, fh, ensure_ascii=False, indent=0, sort_keys=True)
        os.replace(tmp, self.PATH)
        self.dirty = False


# Properties only a POPULATED PLACE carries. This is the sharp instrument for the whole
# homonym class, and it was chosen by measurement rather than by reasoning: over the 508
# entities whose acceptance went through the organisation gate, P1082 rejects 13 and catches
# every one of the ten known-wrong rows, where widening HARD_REJECT through the subclass
# closure caught nine and P625 (coordinate location) rejected 129 -- a quarter of the corpus,
# because a company has a headquarters.
#
# What those 13 are, in full, because the cost matters as much as the catch: seven were
# SHIPPING a municipality's crest as a company's logo (Alma -> ville.alma.qc.ca, Hays ->
# haysusa.com which is the City of Hays, Kansas, Nice -> nice.fr, Heidelberg -> heidelberg.de,
# CHEP -> cheptainville.fr, Clera -> ville-clerac.fr, Wawa -> wawa.cc); four matched a place
# but had already fallen back to the right domain, so nothing changes for them; and two lose a
# correct Commons file -- Rochester Institute of Technology, which tier 2 can still reach on
# its own domain, and City of New York, which is in LOGO_OVERRIDE above.
PLACE_PROPS = ("P1082",)          # population


def is_place(claims):
    """Is this entity a populated place wearing a company's name?

    Checked BEFORE the organisation gate and before the exact-P856 bypass, because both of
    them pass a municipality: a US municipality genuinely subclasses to Q43229 organization,
    and a commune's own website is its own P856 so the two-source agreement the bypass looks
    for is real -- it is just agreement about the wrong entity. Snowflake, Arizona held the
    name "Snowflake" this way and handed tier 2 ci.snowflake.az.us while snowflake.com sat
    unused in companies.json.
    """
    return any(claims.get(prop) for prop in PLACE_PROPS)


def is_org(qid, claims, cache):
    """Does this entity's P31 reach an organisation root by P279?

    Returns (bool, path). Breadth-first, depth-capped, with a visited set, because the
    subclass graph has cycles and a naive walk hangs.

    NO P31 AT ALL IS A REJECT, not an accept-by-default. Accepting the unknown case is exactly
    how "EY" became a family name.
    """
    types, _ = claim_values(claims, "P31")
    if not types:
        return False, []
    if any(t in HARD_REJECT for t in types):
        return False, []
    seen, frontier, path = set(types), list(types), []
    for _ in range(WALK_DEPTH):
        hit = [q for q in frontier if q in ORG_ROOTS]
        if hit:
            return True, path + [hit[0]]
        cache.warm(frontier)            # one batched call for the whole level
        nxt = []
        for q in frontier:
            for p in cache.parents(q):
                # A HARD_REJECT ANYWHERE IN THE CLOSURE IS FATAL, not just at the frontier
                # edge. Dropping the branch and walking its SIBLINGS is what let Snowflake,
                # Arizona through: its P31 is Q15127012, whose parents are Q3957 (town, in
                # HARD_REJECT) and Q3327870 (municipality of the US) -- so town was skipped,
                # municipality survived, and municipality really does subclass to organization
                # four hops later. Depth 0 already worked this way; the walk did not.
                # Note the ORG_ROOTS test above runs FIRST at every level, so an entity that
                # reaches "organisation" sooner than it reaches "town" is still accepted.
                if p in HARD_REJECT:
                    return False, []
                if p not in seen:
                    seen.add(p)
                    nxt.append(p)
        if not nxt:
            break
        path = path + frontier[:1]
        frontier = nxt
    return False, []


def pick_logo_file(claims):
    """The current logo filename from P154, or "" .

    Rejects end-dated statements (P582) and filenames that name a year range or say they are
    historical. Measured cases: "Intel logo (1968-2006).svg", "Visa Inc. logo (1992-1999).svg".

    THEN TAKES THE FIRST SURVIVOR, PREFERRING VECTOR. Do not tiebreak on filename length: Q95
    (Google) lists six logos and the shortest is "Google.png", a raster, while the current one
    is "Google 2026 logo.svg" and is first in statement order.
    """
    live, _ended = claim_values(claims, "P154")
    ok = [f for f in live
          if not YEAR_RANGE.search(f) and not STALE_WORD.search(f)
          # A JPEG IS A PHOTOGRAPH. Commons brand logos are SVG or PNG; a P154 pointing at a
          # .JPG is somebody's photo of the building or the signage, as it is for Cognizant.
          # Cheaper and more certain than any pixel rule, so it runs before the fetch.
          and not f.lower().endswith(PHOTO_EXT)]
    if not ok:
        return ""
    svg = [f for f in ok if f.lower().endswith(".svg")]
    return (svg or ok)[0]


def _tokens(name):
    return [w for w in re.split(r"[^a-z0-9]+", (core.norm_company(name) or "").lower()) if w]


def domain_agrees(name, domain):
    """Does this domain plausibly belong to this company?

    NEEDED BECAUSE P856 IS NOT ALWAYS CLEAN. Measured: Q544847 is unambiguously Qualcomm -- right
    label, right description, P154 is Qualcomm-Logo.svg -- and its P856 is
    "consumerrights.wiki/w/Qualcomm". So a curated field still needs a sanity check, and the
    check has to be independent of the thing it is checking.

    Three ways to agree, all measured against real cases:
      a token of the name appears in the domain    hcl technologies -> hcltech.com
      a domain label is a prefix of the name       citibank         -> citi.com
      the name's acronym is the domain label       ernst young      -> ey.com
    """
    dom = (domain or "").lower()
    if not dom:
        return False
    toks = _tokens(name)
    if not toks:
        return False
    squash = "".join(toks)
    labels = [p for p in dom.split(".") if p and p not in ("www", "com", "net", "org", "co")]
    # The whole name IS a label. Checked first and with no length floor, because a two-letter
    # company cannot satisfy any of the rules below: "EY" against ey.com failed every one of
    # them, and EY is a top-twenty employer here.
    if squash and squash in labels:
        return True
    for t in toks:
        if len(t) >= 3 and t in dom:
            return True
    for lab in labels:
        if len(lab) >= 3 and (squash.startswith(lab) or lab.startswith(squash)):
            return True
    if len(toks) >= 2:
        acronym = "".join(t[0] for t in toks)
        if len(acronym) >= 2 and acronym in labels:
            return True
    return False


def p856_domain(claims, name=""):
    """The official-website HOST, minus www. "" when it does not corroborate the name.

    THE HOST, NOT THE REGISTRABLE DOMAIN. Reducing aws.amazon.com to amazon.com made Amazon Web
    Services and Amazon share one domain, which is both wrong and a --check failure. A host works
    equally well for the "Website" link and for the site-icon fetch, so there is nothing to gain
    by truncating it.
    """
    live, _ = claim_values(claims, "P856")
    for url in live:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        if not host or any(p in host for p in PLATFORM_HOSTS):
            continue
        host = host[4:] if host.startswith("www.") else host
        if "." not in host:
            continue
        if name and not domain_agrees(name, host):
            continue
        return host
    return ""


# ---------------------------------------------------------------- the acceptance test
#
# EVERY RULE HERE JUDGES THE RESULT, NEVER THE REQUEST. That is the whole lesson of the 53%:
# the old chain's only test was "did something come back", and Google always sent something.
#
# Thresholds are not guesses. Each was set against the 150-company sample that produced the
# census in the module docstring, and each is frozen in scripts/test_logos.py against labelled
# fixture images, one per failure class -- so raising or lowering one cannot quietly re-admit
# the class it was written to kill.

# MEASURED, AND IT CHANGES WHAT THIS GATE IS FOR. Metrics for 12 real Commons brand logos
# against 7 of the favicons that produced the census above, all composited onto white first:
#
#                        long edge     ink        colours   edge density
#   12 real logos        176 to 250    .19 - .55   15 - 59   .03 - .23
#   7 bad favicons        30 to  64    .14 - .56    3 - 25   .12 - .51
#
# Ink, colour count and edge density DO NOT SEPARATE THEM. usbank.com's favicon -- the "just
# red, nothing else" tile -- measures ink .21, 25 colours, edge density .12, which sits inside
# the real-logo range on all three. The only clean separator is SIZE.
#
# So the honest account is: the solid-block and blank classes were artefacts of a favicon
# service rendering at 64px, and what kills them is that this script never asks a favicon
# service anything, plus the size floor below. The remaining pixel rules are there to catch a
# genuinely degenerate asset from Commons or from a site, and their thresholds sit an order of
# magnitude below the worst real logo so they cannot fire on a legitimate one.

MIN_EDGE = 128          # the biggest tile is 56 CSS px (.cohead .logo), so 128 is the floor
                        # below which even 2x cannot be filled without upscaling. This is the
                        # load-bearing rule: it separated the two populations perfectly.
MIN_INK = 0.02          # worst real logo measured .19, so 10x of margin.
MIN_COLOURS = 3         # worst real logo measured 15.
# AND A CEILING, for a failure class the census never contained: the asset is a real image but
# it is not a logo. Measured: Wikidata's P154 for Cognizant (Q1107035) is
# "Cognizant Technology Solutions - Kolkata 2011-08-29 4824.JPG", a PHOTOGRAPH of signage on
# their office wall, CC BY, by a named photographer. It passed every rule above because a photo
# scores WELL on richness: ink .85, 420 colours, edge density .38. Real logos measured 15 to 59
# colours, so 150 leaves 2.5x of headroom above the richest one and still rejects a photo by a
# factor of three. PHOTO_EXT below is the cheaper half of the same test.
MAX_COLOURS = 150
PHOTO_EXT = (".jpg", ".jpeg", ".tif", ".tiff")
MIN_EDGE_DENSITY = 0.01  # worst real logo measured .03.
# A banner, not a mark. This was 8.0 on a twelve-logo sample whose widest was Oracle at 7.6:1,
# and it then rejected two real ones out of the top 143 employers: American Airlines' wordmark is
# exactly 10:1 and Autodesk's is 9.6:1. 12 still rejects the 20:1 banner case, and the card
# clamps width at 180px anyway, so a 10:1 mark renders 180x18 inside the 36px plate.
MAX_AR = 12.0
MAX_BYTES_STORED = 40 * 1024
DIR_BUDGET = 12 * 1024 * 1024
TARGET_PX = 192         # covers the 56 CSS px tile at 3x.


def _pixels(im):
    """Every pixel of `im` as a list. Pillow renamed getdata() to get_flattened_data() and
    deprecated the old name for removal in Pillow 14, so ask for whichever this build has."""
    return list((getattr(im, "get_flattened_data", None) or im.getdata)())


def _trim(im):
    """Drop fully transparent border rows and columns before measuring.

    Amazon's SVG canvas carries padding; without this the aspect ratio and the ink fraction
    both describe the canvas rather than the artwork.
    """
    try:
        box = im.split()[-1].getbbox()
        return im.crop(box) if box else im
    except Exception:
        return im


def judge(raw):
    """(ok, why, meta) for candidate image bytes.

    `meta` carries w/h/ar/mono for the manifest even on a reject, so --report can show what a
    rejected candidate actually looked like.
    """
    from PIL import Image, ImageFilter
    meta = {}
    # A BYTE FLOOR IS ONLY FOR AN EMPTY RESPONSE, never for judging quality. This was 512 and
    # that was wrong: real brand logos get very small. Apple's is 656 bytes and Oracle's is 874,
    # and a flat two-colour mark compresses below 512 easily. Everything about whether the image
    # is any good is decided on its pixels below, after it has actually decoded.
    if not raw or len(raw) < 64:
        return False, "empty-body", meta
    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
        im = im.convert("RGBA")
    except Exception:
        return False, "undecodable", meta
    im = _trim(im)
    w, h = im.size
    meta["w"], meta["h"] = w, h
    meta["ar"] = round(w / h, 3) if h else 0
    if not w or not h:
        return False, "empty", meta
    if max(w, h) < MIN_EDGE:
        return False, "undersized", meta
    if meta["ar"] > MAX_AR or meta["ar"] < 1.0 / MAX_AR:
        return False, "aspect", meta

    # COMPOSITE ONTO WHITE BEFORE MEASURING ANYTHING. Every metric below was wrong without this,
    # and wrong in the direction that rejects the best logos: a black mark on a transparent
    # canvas converts to a UNIFORM L channel, so FIND_EDGES reported zero edges for Apple and
    # Uber, and an RGB distance against a transparent background reported zero ink for Apple.
    # Apple, Accenture and Deloitte were all rejected by this bug before it was found. White is
    # also the surface the card actually renders them on, so it is the honest background to
    # judge them against, and a white-on-transparent knockout mark correctly measures as blank.
    flat = Image.new("RGB", (w, h), (255, 255, 255))
    flat.paste(im, (0, 0), im)
    px = _pixels(flat)
    bg = collections.Counter(px).most_common(1)[0][0]

    # A UNIFORM IMAGE, SEPARATED FROM AN EMPTY ONE. Both have zero ink by definition -- ink is
    # measured against the modal colour -- so without this a solid brand-coloured square reports
    # "blank", which is true of the measurement and useless as a diagnosis. The distinction is
    # what makes --report and the ledger's `why` field worth reading.
    if len(set(px)) <= 2:
        near_white = min(bg[:3]) >= 246
        meta["ink"] = 0.0
        meta["colours"] = 1
        return False, ("blank" if near_white else "solid-block"), meta

    ink = [p for p in px
           if max(abs(p[0] - bg[0]), abs(p[1] - bg[1]), abs(p[2] - bg[2])) >= 24]
    frac = len(ink) / float(len(px))
    meta["ink"] = round(frac, 4)
    if frac < MIN_INK:
        # Also the knockout case: a white mark composited onto white leaves nothing.
        return False, "blank", meta

    quant = len(set((p[0] >> 4, p[1] >> 4, p[2] >> 4) for p in ink))
    meta["colours"] = quant

    # MONO IS RECORDED, NOT REJECTED. The census called 14% "monochrome black glyph" and the
    # obvious rule -- reject on low chroma -- would be a real regression: Uber's wordmark IS
    # black, Apple's mark is monochrome, Deloitte's is black plus one green dot. The defect was
    # never chroma, it was provenance, and a tab icon has no pixel signature. The card uses this
    # flag; the gate does not.
    grey = sum(1 for p in ink if max(p[:3]) - min(p[:3]) < 18)
    meta["mono"] = bool(grey > 0.93 * len(ink))

    try:
        edges = flat.convert("L").filter(ImageFilter.FIND_EDGES)
        density = sum(1 for v in _pixels(edges) if v > 32) / float(w * h)
    except Exception:
        density = 1.0
    meta["edges"] = round(density, 4)

    # FEW COLOURS IS ONLY DAMNING TOGETHER WITH NO STRUCTURE, and the conjunction matters. The
    # 15-to-59 colour range measured on real logos comes from ANTI-ALIASING in Wikimedia's
    # renders, not from the brands using many colours -- a crisp two-colour PNG from a site icon
    # quantises to 2 buckets and is a perfectly good logo. An unconditional floor here rejected a
    # flat black-and-red wordmark with ink .60 and edge density .14. A solid block fails BOTH:
    # its only edges are its own border. The truly uniform case is already gone above.
    if quant < MIN_COLOURS and density < MIN_EDGE_DENSITY:
        return False, "solid-block", meta
    if quant > MAX_COLOURS:
        return False, "photograph", meta
    if density < MIN_EDGE_DENSITY:
        return False, "no-structure", meta
    return True, "", meta


# ---------------------------------------------------------------- SVG hygiene
#
# An SVG is a document, not just a picture. Through <img src> its scripts never run, so the
# tiles are safe either way -- but anything under /static/ is directly navigable, and a static
# file served by Passenger carries NONE of the CSP that web.py sets on HTML responses. So the
# dangerous parts come out at harvest time, and then the OUTPUT is re-scanned and rejected if
# any of them survived. Strip-then-verify, not strip-and-hope.

SVG_KILL_TAGS = ("script", "foreignObject", "iframe", "object", "embed", "animate",
                 "animateTransform", "animateMotion", "set", "handler", "audio", "video", "a")
SVG_UNSAFE_OUT = re.compile(
    rb"<\s*(?:script|foreignObject|iframe|object|embed)\b|\son[a-zA-Z]+\s*=|javascript:"
    rb"|<!ENTITY|<!DOCTYPE", re.I)
SVG_EXTERNAL = re.compile(rb"""(?:href|src)\s*=\s*["']\s*(?:https?:|//)""", re.I)


def sanitise_svg(raw):
    """(bytes, why). Strips the executable surface and the bulk, then verifies."""
    from lxml import etree
    # A DOCTYPE OR AN ENTITY DECLARATION IS REFUSED ON THE WAY IN, not scrubbed. lxml drops the
    # DTD when it serialises the root element, so a post-hoc scan of the OUTPUT always looks
    # clean and would wave an entity-expansion payload straight through. Nothing legitimate in
    # this corpus needs a DTD.
    if re.search(rb"<!DOCTYPE|<!ENTITY", raw[:4096], re.I):
        return b"", "svg-doctype"
    try:
        parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False,
                                 remove_comments=True, remove_pis=True)
        root = etree.fromstring(raw, parser=parser)
    except Exception:
        return b"", "svg-unparseable"

    def local(tag):
        return etree.QName(tag).localname if isinstance(tag, str) else ""

    for el in list(root.iter()):
        if not isinstance(el.tag, str):
            continue
        name = local(el.tag)
        if name in SVG_KILL_TAGS or name in ("metadata", "title", "desc"):
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)
            continue
        for attr in list(el.attrib):
            low = attr.lower()
            val = (el.attrib.get(attr) or "").strip().lower()
            if low.startswith("on"):
                del el.attrib[attr]
            elif low.endswith("href") and not (val.startswith("#") or val.startswith("data:image/")):
                del el.attrib[attr]
            elif low == "style" and ("url(" in val or "expression(" in val
                                     or "javascript:" in val or "@import" in val):
                del el.attrib[attr]
            elif low.startswith("sodipodi:") or low.startswith("inkscape:"):
                del el.attrib[attr]

    out = etree.tostring(root, xml_declaration=False, encoding="utf-8")
    out = re.sub(rb">\s+<", b"><", out)
    if SVG_UNSAFE_OUT.search(out) or SVG_EXTERNAL.search(out):
        return b"", "svg-unsafe"
    return out, ""


# ---------------------------------------------------------------- storage

_HEXCOL = re.compile(rb"#[0-9a-fA-F]{3,8}")
_FUNCCOL = re.compile(rb"(?:rgb|rgba|hsl|hsla)\(", re.I)
_NUM = re.compile(r"-?[\d.]+")


def _hex_is_dark(col):
    """Is this #rrggbb (or #rgb) closer to black than to white? Relative luminance, not a naive
    channel average -- green carries most of the perceived brightness."""
    h = col.decode("ascii", "ignore").lstrip("#") if isinstance(col, bytes) else str(col).lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) < 6:
        return False
    try:
        r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    except ValueError:
        return False
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) < 0.5


def svg_meta(raw):
    """(ar, mono, why) for a SANITISED SVG, judged structurally instead of on pixels.

    Tier 1 never needs this: Commons renders a raster thumb for every file, and judge() measures
    that. A site's own favicon.svg arrives with no thumb, and PIL cannot rasterise one -- adding
    cairosvg would put a native dependency on a shared host for one gate. So the checks here are
    the ones that survive without a renderer: a declared geometry, a plausible aspect, and a
    colour count read off the markup.

    THIS IS A WEAKER GATE THAN judge() AND IT IS SAID SO OUT LOUD. It cannot see a solid colour
    block or a blurry upscale. What makes that acceptable is the source: a vector cannot BE a
    blurry upscale, and the file is served by the employer's own domain, which has already had
    to clear domain_agrees against the company name.
    """
    from lxml import etree
    try:
        root = etree.fromstring(raw, parser=etree.XMLParser(resolve_entities=False,
                                                            no_network=True, huge_tree=False))
    except Exception:
        return 0, 0, "svg-unparseable"
    vb = (root.get("viewBox") or "").strip()
    w = h = 0.0
    if vb:
        nums = _NUM.findall(vb)
        if len(nums) >= 4:
            w, h = abs(float(nums[2])), abs(float(nums[3]))
    if not (w and h):
        try:
            w = float((_NUM.findall(root.get("width") or "") or [0])[0])
            h = float((_NUM.findall(root.get("height") or "") or [0])[0])
        except Exception:
            w = h = 0.0
    if not (w and h):
        return 0, 0, "svg-no-geometry"
    ar = round(w / h, 3)
    if ar > MAX_AR or ar < 1.0 / MAX_AR:
        return ar, 0, "aspect"
    # MONO MEANS "ONE DARK INK, SAFE TO INVERT ON A DARK BACKGROUND", not merely "low chroma",
    # because that is what the card does with the flag. judge() gets the dark part for free: it
    # composites onto WHITE and rejects a blank, so an accepted raster mono logo cannot be a
    # white knockout. Nothing composites an SVG, so the darkness has to be read here -- a
    # fill="#fff" wordmark is monochrome and inverting it would paint it black on a dark card.
    cols = {c.lower() for c in _HEXCOL.findall(raw)}
    mono = 0
    if len(cols) == 1 and not _FUNCCOL.search(raw):
        mono = 1 if _hex_is_dark(next(iter(cols))) else 0
    return ar, mono, ""


def store(slug, raw, is_svg):
    """Write the asset, returning (filename, bytes, sha256, why).

    THE DIGEST IS OF WHAT WAS WRITTEN, not of what was fetched, and that distinction is the
    whole point. The caller used to hash the bytes it downloaded while this function wrote a
    re-encoded WebP, so every raster asset disagreed with its own recorded hash: --check reported
    a mismatch for all of them, and every harvest run re-fetched them from scratch. 525 rows were
    being redone on every pass because of it.

    SVG ships as SVG: crisp at every DPR, 3.6 KB median, and no rasteriser needed. Raster is
    re-encoded to lossless WebP at the long edge, NEVER upscaled -- upscaling is what made the
    old 16px favicons look like mush, and re-doing it here would defeat the point.
    """
    os.makedirs(LOGO_DIR, exist_ok=True)
    if is_svg:
        if len(raw) > MAX_BYTES_STORED:
            return "", 0, "", "oversize"
        fn = slug + ".svg"
        with open(os.path.join(LOGO_DIR, fn), "wb") as fh:
            fh.write(raw)
        return fn, len(raw), hashlib.sha256(raw).hexdigest()[:16], ""

    from PIL import Image
    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
        im = _trim(im.convert("RGBA"))
    except Exception:
        return "", 0, "", "undecodable"
    w, h = im.size
    if max(w, h) > TARGET_PX:
        scale = TARGET_PX / float(max(w, h))
        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    # BOTH ENCODERS, KEEP THE SMALLEST ONE THAT STILL PASSES ON ITS OWN BYTES.
    #
    # Lossless wins on a flat vector-derived mark; quality-90 wins on anything with a gradient or
    # a soft shadow, and at 192px the two are visually indistinguishable -- it took one real site
    # icon from 13.4 KB to a third of that, which matters across ~400 files.
    #
    # But the verdict has to be about the bytes that SHIP. Judging the source and storing a
    # re-encode is how 36 assets came to be accepted at harvest time and then rejected by --check
    # as "photograph": lossy encoding adds enough colour noise to cross the ceiling that separates
    # a logo from a photo. So each candidate is re-judged here, and the winner is the smallest one
    # that its own gate still accepts.
    cand = []
    for kw in ({"quality": 90}, {"lossless": True}):
        buf = io.BytesIO()
        im.save(buf, "WEBP", method=6, **kw)
        cand.append(buf.getvalue())
    cand.sort(key=len)
    data = next((c for c in cand if len(c) <= MAX_BYTES_STORED and judge(c)[0]), None)
    if data is None:
        return "", 0, "", "reencode-failed-gate"
    fn = slug + ".webp"
    with open(os.path.join(LOGO_DIR, fn), "wb") as fh:
        fh.write(data)
    return fn, len(data), hashlib.sha256(data).hexdigest()[:16], ""


# ---------------------------------------------------------------- the site-icon leg

ICON_MIN = 120          # the floor a DECLARED size must clear
ICON_TRIES = 6          # candidates fetched per domain, best-ranked first

# RANKS FOR CANDIDATES WHOSE SIZE THE PAGE NEVER STATED, and the reason this block exists.
# sizes= is OPTIONAL on apple-touch-icon and its de-facto size is 180x180, but an absent
# attribute used to score 0 and then fail `size >= ICON_MIN` -- so the most common
# high-resolution icon on the web was discarded unread. Measured by fetching them anyway:
# Skydio 180x180, Entergy 180x180, Biogen 180x180, Awardco 256x256, Astranis 256x256, Hex
# 128x128, all thrown away. Over 25 sampled employers that have a verified domain and no logo,
# domains yielding at least one candidate went from 8 to 15.
#
# An absent sizes= is not a claim that the asset is small, so it is a SORT KEY now and judge()'s
# 128px floor is what actually decides -- which is this file's own stated principle applied to
# its own input. An explicit sizes="16x16" is still a true statement and is still filtered out.
RANK_SVG = 512          # vector: resolution-independent, so it outranks everything
RANK_JSONLD = 260       # schema.org Organization.logo -- a brand logo, not an app icon
RANK_APPLE = 180        # apple-touch-icon's de-facto size when it is not stated
RANK_UNSIZED = 130      # any other unsized rel=icon
RANK_WELLKNOWN = 125    # nothing was declared; ask the conventional paths
WELL_KNOWN_ICONS = ("/apple-touch-icon.png", "/apple-touch-icon-precomposed.png",
                    "/favicon.svg")
# "logo": "<url>" or "logo": {"url": "<url>"}, the two shapes schema.org allows. Regex rather
# than a JSON parse because the block is often one of several in a page and frequently invalid.
JSONLD_LOGO = re.compile(
    r'"logo"\s*:\s*(?:"(https?://[^"]{4,400})"'
    r'|\{[^{}]{0,400}?"url"\s*:\s*"(https?://[^"]{4,400})")')
# og:image is deliberately absent. It is a social share card: measured over 22 sampled
# homepages it would have added three, all of them banners that judge() rejects as photographs.


def site_icons(net, domain):
    """Icon and logo URLs the site itself declares, best first.

    Measured hit rate on its own: 45% of 40 random stored domains, at 192x192 to 1024x1024.
    Second source rather than first because plenty of large sites serve no parseable link tags
    at all (mastercard.com, northropgrumman.com, telus.com all return none).
    """
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin
    for prefix in ("https://www.", "https://"):
        try:
            r = net.get(prefix + domain, ua=BROWSER_UA, timeout=(5, 12))
        except IOError:
            return []
        if r is None:
            continue
        try:
            soup = BeautifulSoup(r.text, "lxml")
        except Exception:
            return []
        cands = []
        for link in soup.find_all("link"):
            rel = " ".join(link.get("rel") or []).lower()
            href = (link.get("href") or "").strip()
            if "icon" not in rel or not href:
                continue
            m = re.match(r"(\d+)x", (link.get("sizes") or "").lower())
            if href.lower().split("?")[0].endswith(".svg"):
                size = RANK_SVG
            elif m:
                size = int(m.group(1))
            elif "apple-touch-icon" in rel:
                size = RANK_APPLE
            else:
                size = RANK_UNSIZED
            cands.append((size, urljoin(r.url, href)))
        for link in soup.find_all("link"):
            if "manifest" not in " ".join(link.get("rel") or []).lower():
                continue
            try:
                murl = urljoin(r.url, link.get("href") or "")
                mr = net.get(murl, ua=BROWSER_UA, timeout=(5, 10))
                for ic in ((mr.json() if mr is not None else {}).get("icons") or []):
                    m = re.match(r"(\d+)x", (ic.get("sizes") or ""))
                    cands.append((int(m.group(1)) if m else 0,
                                  urljoin(murl, ic.get("src") or "")))
            except Exception:
                pass
            break
        for mo in JSONLD_LOGO.finditer(r.text or ""):
            cands.append((RANK_JSONLD, urljoin(r.url, mo.group(1) or mo.group(2))))
        for wk in WELL_KNOWN_ICONS:
            cands.append((RANK_WELLKNOWN, urljoin(r.url, wk)))
        seen, out = set(), []
        for size, u in sorted(cands, key=lambda t: -t[0]):
            if size < ICON_MIN or u in seen:
                continue
            seen.add(u)
            out.append(u)
        return out[:ICON_TRIES]
    return []


# ---------------------------------------------------------------- identity

def label_ok(name, label, aliases=()):
    """Does this entity's label plausibly name this company?

    Weak evidence on its own -- "Arctic Wolf" matches the animal's label exactly -- so it is only
    trusted alongside the organisation gate, and it is deliberately asymmetric:

    A LABEL THAT IS MORE SPECIFIC THAN THE NAME IS A DIFFERENT COMPANY. Measured: searching
    "LinkedIn" does not return LinkedIn itself in the top seven hits, but it does return LinkedIn
    Learning, LinkedIn Ireland and LinkedIn News -- all organisations, one of them carrying a real
    logo. A subset test in both directions accepted "LinkedIn Learning" and shipped its logo for
    LinkedIn. So the name may be more specific than the label ("Meta Platforms" matching Meta),
    but never the reverse.
    """
    want = core.norm_company(name)
    if not want:
        return False
    for cand in (label,) + tuple(aliases):
        got = core.norm_company(cand or "")
        if not got:
            continue
        if got == want:
            return True
        wt, gt = set(want.split()), set(got.split())
        if wt and gt and gt < wt:
            return True
    return False


# Words naming a corporate FORM rather than a distinct thing. Only used to compare a company
# name against an entity label; core.norm_company already strips the commonest ones.
_FORM_WORDS = {"holding", "holdings", "nv", "sa", "ag", "plc", "lp", "llp", "pte", "bv", "as",
               "spa", "gmbh", "kk", "oyj", "ab", "aps", "srl", "cv", "co", "corp", "company",
               "international", "worldwide", "global", "america", "americas", "usa", "us"}


def same_employer(keys, dom=""):
    """Are these all spellings of ONE employer, rather than a genuine conflict?

    A shared domain is only a problem when the claimants are different companies. Measured on
    the real map: 'u s bank' and 'bank', 'openai' and 'openai opco', 'siemens' and 'siemens
    industry software', 'thermo fisher scientific' and 'thermofisher scientific' are all one
    employer twice -- and a naive "keep whichever has P856" handed usbank.com to a row named
    'bank' and took it away from U.S. Bank. Two tests, because a token subset misses the
    concatenated spellings: token containment, or one squashed form prefixing the other.
    """
    toks = [set(k.split()) for k in keys]
    squash = ["".join(k.split()) for k in keys]
    acro = ["".join(w[0] for w in k.split()) for k in keys]
    # A shared leading token that IS the domain: "pnc bank" and "pnc financial services" on
    # pnc.com. Gated on the domain label so it cannot merge "american airlines" with "american
    # express" -- for those the label is "americanexpress", which no first token matches.
    firsts = {k.split()[0] for k in keys if k.split()}
    label = (dom or "").split(".")[0]
    if len(firsts) == 1 and label and label == next(iter(firsts)) and len(label) >= 3:
        return True
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            if toks[i] <= toks[j] or toks[j] <= toks[i]:
                continue
            if squash[i].startswith(squash[j]) or squash[j].startswith(squash[i]):
                continue
            # The acronym case, the same one domain_agrees uses: "ernst young" and "ey" are
            # one employer, and so are "united services automobile association" and "usaa".
            if acro[i] == squash[j] or acro[j] == squash[i]:
                continue
            return False
    return True


def label_too_specific(name, label):
    """Is this entity a NARROWER thing than the company we asked about?

    The company's own products and subsidiaries live on the company's domain, so a domain match
    alone cannot tell them apart. Measured: searching "Google" with stored domain google.com
    matched Google Maps (whose official website is on google.com) ahead of Google itself, and
    shipped "Google Maps icon (2026).svg". Same shape as LinkedIn Learning for LinkedIn, and it
    would put Amazon Web Services' logo on Amazon.

    Strict-subset only, so "EY" against a label of "Ernst & Young" is still allowed -- neither
    token set contains the other, and EY needs that path.

    AND CORPORATE-FORM WORDS ARE DROPPED FIRST, because they do not make an entity narrower.
    core.norm_company strips Inc/LLC/Group but not Holding, NV, PLC or LP, so "ASML" against
    "ASML Holding" looked like a strict subset and was skipped -- which left ASML, Bloomberg,
    Amat and Barclays with no candidate at all. Measured on the top 143 employers.
    """
    def bag(s):
        # Single characters go too, symmetrically: "Bloomberg L.P." normalises to
        # {bloomberg, l, p} and those two stray letters were enough to make it a strict
        # superset of {bloomberg}. Dropping them from both sides preserves equality.
        return {w for w in (core.norm_company(s) or "").split()
                if len(w) > 1 and w not in _FORM_WORDS}

    want, got = bag(name), bag(label)
    return bool(want and got and want < got)


def resolve(net, cache, name, stored):
    """Pick the Wikidata entity for a company, or explain why not.

    THE ACCEPTANCE RULE IS AGREEMENT BETWEEN TWO INDEPENDENT SOURCES. An entity is taken when
    its P856 official website matches the domain we already derived, or -- with no domain to
    check against -- when it clears the organisation gate AND its label matches the name. Either
    signal alone is a guess: P856 alone would accept any organisation with a similar name, and a
    label alone accepted a wolf.
    """
    try:
        hits = wd_search(net, name)
    except IOError:
        raise
    if not hits:
        return {"why": "no-entity"}
    ents = wd_entities(net, [q for q, _l, _d in hits])
    ranked = []
    for order, (qid, label, desc) in enumerate(hits):
        ent = ents.get(qid) or {}
        claims = ent.get("claims") or {}
        if is_place(claims):
            continue                    # a town with the company's name, not the company
        dom = p856_domain(claims, name)
        logo = pick_logo_file(claims)
        # THE DOMAIN IS A BETTER ARBITER THAN THE LABEL, so it is consulted first and can excuse
        # a label that looks too specific. Measured: our corpus calls the employer "lululemon"
        # while Wikidata's entity -- the one carrying the logo -- is labelled "Lululemon
        # Athletica", so the subset rule below read the real company as a subsidiary of itself
        # and threw it away. Its P856 is shop.lululemon.com, which is our own stored
        # lululemon.com wearing a subdomain.
        #
        # ASYMMETRIC ON PURPOSE. Accepting a bare registrable match in both directions would let
        # amazon.com answer for a company stored as aws.amazon.com, which is the confusion
        # p856_domain's docstring exists to prevent. Only the entity being a SUBDOMAIN of what we
        # stored counts: shop.lululemon.com under lululemon.com, never the reverse.
        under = bool(dom and stored and (dom == stored or dom.endswith("." + stored)))
        if not under and label_too_specific(name, label):
            continue                    # a product or subsidiary, not the employer
        # AN EXACT DOMAIN MATCH OUTRANKS THE ORGANISATION GATE, and is allowed to skip it.
        # The gate exists to catch a match made on a NAME alone; when the entity's own official
        # website is the domain we already had, that is the two-source agreement the gate is a
        # proxy for. Measured: Q483959 is unambiguously PayPal -- p856 paypal.com, P154
        # "PayPal 2024.svg" -- and its P31 chain does not reach an organisation root inside six
        # hops, so the gate rejected the right entity and left a logo-less stub to win.
        agreed = under
        ok, path = (True, ["p856"]) if agreed else is_org(qid, claims, cache)
        if not ok:
            continue
        rec = {"qid": qid, "label": label, "desc": desc, "p856": dom,
               "p279_path": path, "logo_file": logo}
        if agreed:
            rec["identity"], tier = "domain-agree", 0
        elif label_ok(name, label, ()):
            rec["identity"], tier = "label-match", 2
        elif dom:
            # An organisation whose label did not match and whose site we could not corroborate.
            # Good enough for the DOMAIN, because P856 is curated and it cleared domain_agrees.
            # Not good enough for the LOGO.
            rec["identity"], tier = "org-only", 4
            rec["logo_file"] = ""
        else:
            continue
        # HAVING A LOGO BREAKS THE TIE WITHIN A CONFIDENCE TIER. Measured: searching "PayPal"
        # returns a bare stub entity ahead of the real one -- right description, no English
        # label, no P856, no P154 -- so taking the first org-gated label match threw away the
        # only candidate that actually had a logo.
        ranked.append((tier + (0 if rec["logo_file"] else 1), order, rec))
    if not ranked:
        return {"why": "not-an-org"}
    ranked.sort(key=lambda x: (x[0], x[1]))
    return ranked[0][2]


def commons_asset(net, filename):
    """(original_url, thumb_url, native_wh, licence, mime) for a Commons file, or Nones.

    HOW AN SVG GETS JUDGED WITHOUT A RASTERISER. cairosvg is not installed and a headless
    browser is not worth 150 MB here, but the same imageinfo call that gives the original URL
    and the licence also renders a PNG at iiurlwidth on Wikimedia's own servers. So every
    shipped asset is judged on real pixels even when what ships is vector.

    COMMONS ONLY, NEVER A LOCAL WIKIPEDIA FILE REPO. Commons forbids fair use, so a file hosted
    there is free-licensed. The same logo on English Wikipedia is routinely NON-FREE fair use,
    and following a P154 value there is how a non-free file gets committed into a git repo.
    """
    title = filename if filename.lower().startswith("file:") else "File:" + filename
    j = net.json(COMMONS_API, {"action": "query", "prop": "imageinfo", "titles": title,
                               "iiprop": "url|size|mime|extmetadata",
                               "iiurlwidth": TARGET_PX, "format": "json"})
    pages = (j.get("query") or {}).get("pages") or {}
    for _pid, page in pages.items():
        if "missing" in page:
            continue
        ii = (page.get("imageinfo") or [{}])[0]
        em = ii.get("extmetadata") or {}
        lic = ((em.get("LicenseShortName") or {}).get("value") or "").strip()
        artist = re.sub(r"<[^>]+>", "", ((em.get("Artist") or {}).get("value") or "")).strip()
        return (ii.get("url"), ii.get("thumburl"),
                (ii.get("width"), ii.get("height")), lic, ii.get("mime") or "", artist)
    return (None, None, (0, 0), "", "", "")


NONFREE = re.compile(r"fair\s*use|non[- ]free|copyright|all rights reserved", re.I)


def domain_candidates(name, stored, p856):
    """Every domain worth asking for this employer's own icon, best first.

    STORED COMES FIRST, and that ordering is the fix for a whole class of wrong logo. This was
    `p856 or stored`, so a homonymous entity's website OVERWROTE a domain we had already
    verified -- and Wikidata is full of places that share a company's name. Measured against
    the ledger this replaces: Snowflake harvested ci.snowflake.az.us while snowflake.com sat
    in companies.json unused, Appian took an Italian comune's site, KLA took Klagenfurt's.

    stored came from --write-domains, which records provenance per entry and is gated by
    --check's one-domain-one-company rule. An in-run P856 is whatever entity wbsearchentities
    ranked first. So stored is the better witness and P856 is the fallback, not the override --
    and BOTH are tried, because the first one to yield an asset that passes judge() wins.
    """
    forced = DOMAIN_OVERRIDE.get(core.norm_company(name) or "")
    if forced is not None:
        return [forced] if forced else []
    out = []
    for d in (stored or "", p856 or ""):
        d = (d or "").strip().lower()
        if d and d not in out and domain_agrees(name, d):
            out.append(d)
    return out


def harvest_one(net, cache, name, stored, tier):
    """Everything for one company. Returns a ledger entry.

    Raises IOError for a transport failure so the caller can record `deferred` and retry it,
    which is the distinction that stops a throttled run from permanently blanking employers.
    """
    slug = slugify(name)
    ent = {"name": name, "slug": slug, "checked": datetime.date.today().isoformat()}

    override = LOGO_OVERRIDE.get(slug)
    info = {}
    if override is None:
        info = resolve(net, cache, name, stored)
        ent.update({k: v for k, v in info.items()
                    if k in ("qid", "p856", "identity", "p279_path", "label")})

    cands = domain_candidates(name, stored, info.get("p856") or "")
    ent["domain"] = cands[0] if cands else ""
    if not cands and (stored or info.get("p856")):
        ent["why"] = "domain-unverified"

    # ---- tier 1: the Commons brand logo
    fn = override if override else info.get("logo_file") or ""
    if fn and tier in ("all", "wikidata"):
        orig, thumb, native, lic, mime, artist = commons_asset(net, fn)
        if orig:
            ent["file"] = fn
            ent["license"] = lic
            ent["artist"] = artist[:120]
            if NONFREE.search(lic or ""):
                ent.update(verdict="rejected", why="license")
                return ent
            probe = net.get(thumb or orig, binary=True)
            ok, why, meta = judge(probe)
            ent.update({k: meta[k] for k in ("w", "h", "ar", "ink", "colours", "mono", "edges")
                        if k in meta})
            if ok:
                is_svg = "svg" in (mime or "") or orig.lower().endswith(".svg")
                body = net.get(orig, binary=True) if is_svg else probe
                lost = ""
                if body:
                    if is_svg:
                        body, lost = sanitise_svg(body)
                    if body:
                        out, size, sha, owhy = store(slug, body, is_svg)
                        if out:
                            ent.update(verdict="accepted", tier="wikidata", asset=out,
                                       bytes=size, sha256=sha, src=orig, why="")
                            # The stored SVG keeps the ORIGINAL aspect ratio, which the thumb
                            # also carries, so meta's ar is right either way.
                            return ent
                        lost = lost or owhy
                # THE THUMB ALREADY PASSED judge(), SO DO NOT THROW IT AWAY. Everything above
                # this point is about the SVG ORIGINAL, and it can be refused for reasons that
                # say nothing about the artwork: 40 KB is a page-weight cap, and a DOCTYPE is
                # refused because lxml drops the DTD on serialise so a post-hoc scan of the
                # output cannot see an entity payload. Both are correct rules. But the 192px
                # PNG Wikimedia rendered from that same file is already in hand, already
                # cleared the pixel gate above, and goes through the raster path -- re-encoded
                # to WebP under the same 40 KB cap. Measured: 21 employers were being dropped
                # this way, every one of them tier 1, including Harvard, TD Bank, Bloomberg,
                # Grainger, Kaiser Permanente and Cardinal Health.
                if is_svg and probe:
                    out, size, sha, owhy = store(slug, probe, False)
                    if out:
                        ent.update(verdict="accepted", tier="wikidata", asset=out, bytes=size,
                                   sha256=sha, src=thumb or orig, why="",
                                   raster_fallback=lost or "svg-unusable")
                        return ent
                    lost = lost or owhy
                if lost:
                    ent.update(verdict="rejected", why=lost, tier="wikidata")
                    return ent
            else:
                ent.update(why=why, tier="wikidata")

    # ---- tier 2: the company's own high-resolution site icon
    #
    # THE DOMAIN HAS TO CORROBORATE THE NAME BEFORE ITS ICON IS TRUSTED, and domain_candidates
    # above is where that happens now -- every candidate it returns has already passed
    # domain_agrees, so there is nothing left to filter here. Tier 1 gets the same property for
    # free, because p856_domain checks P856 against the name. Measured before that check
    # existed: of 207 tier-2 acceptances, 2 were the employer's JOB BOARD rather than the
    # employer -- GardaWorld Security Services took appcast.io's logo and iPolarity took
    # careerplug.com's. One percent, and exactly the confidently-wrong class that is worse than
    # a monogram. --check asserts the property on the stored `domain` too, which is why the
    # accepting branches below overwrite it with the candidate that actually won rather than
    # leaving the first one we tried.
    tried = 0
    for domain in (cands if tier in ("all", "site") else []):
        try:
            urls = site_icons(net, domain)
            tried += len(urls)
            for url in urls:
                raw = net.get(url, ua=BROWSER_UA, binary=True, timeout=(5, 12))
                if not raw:
                    continue
                is_svg = url.lower().split("?")[0].endswith(".svg")
                if is_svg:
                    # TIER 2 USED TO REFUSE SVG ENTIRELY, to hold the sanitisation surface to a
                    # single source (Commons). Measured cost of that rule: Axon serves a good
                    # favicon.svg and its two PNG icons 404, so it got no logo at all -- and
                    # modern sites overwhelmingly ship an SVG favicon and nothing else. It was
                    # rejecting the best asset the domain had.
                    #
                    # sanitise_svg is not Commons-specific: it refuses a DOCTYPE or an ENTITY on
                    # the way IN, parses with no_network and no entity resolution, drops the
                    # script/handler surface, and is already the only thing standing between a
                    # Commons SVG and static/. Running a site's SVG through the same function is
                    # the same guarantee, applied to a source that has already had to agree with
                    # the company name.
                    body, swhy = sanitise_svg(raw)
                    if not body:
                        ent.setdefault("why", swhy)
                        continue
                    sar, smono, swhy = svg_meta(body)
                    if swhy:
                        ent.setdefault("why", swhy)
                        continue
                    out, size, sha, owhy = store(slug, body, True)
                    if out:
                        ent.update(verdict="accepted", tier="site", asset=out, bytes=size,
                                   sha256=sha, src=url, why="", license="site", artist="",
                                   ar=sar, mono=smono, domain=domain)
                        return ent
                    ent.setdefault("why", owhy)
                    continue
                ok, why, meta = judge(raw)
                if not ok:
                    ent.setdefault("why", why)
                    continue
                out, size, sha, owhy = store(slug, raw, False)
                if out:
                    ent.update({k: meta[k] for k in ("w", "h", "ar", "mono") if k in meta})
                    ent.update(verdict="accepted", tier="site", asset=out, bytes=size,
                               sha256=sha, src=url, why="", license="site", artist="",
                               domain=domain)
                    return ent
                ent.setdefault("why", owhy)
        except IOError:
            raise

    ent["site_tried"] = tried
    if not ent.get("why"):
        # WHY USED TO LIE BY OMISSION. When tier 2 found no candidate at all, nothing set a
        # reason, so the entry fell through to tier 1's -- and Truist read "not-an-org" when
        # the real story was that truist.com declares no icon this chain can use. A pass aimed
        # at "no Wikidata entity" is a different pass from one aimed at "no site icon", so the
        # two are named apart and the count of candidates actually fetched is recorded.
        if cands and not tried:
            ent["why"] = "no-site-icon"
        else:
            ent["why"] = info.get("why") or ("no-logo-claim" if info.get("qid")
                                             else "no-entity")
    ent["verdict"] = "no-candidate" if ent["why"] in ("no-entity", "no-logo-claim",
                                                      "not-an-org",
                                                      "no-site-icon") else "rejected"
    return ent


# ---------------------------------------------------------------- corpus + manifest

def load_rows():
    with open(COMPANIES_JSON, encoding="utf-8") as fh:
        return (json.load(fh) or {}).get("rows") or []


def write_manifest(rows, ledger):
    """static/logos/index.json -- what the page reads.

    IT LIVES UNDER static/ ON PURPOSE. It rides build_deploy_zip.py's DIRS walk and
    .cpanel.yml's `cp -rf static`, so nothing has to be added to FILES or to the cp line; and
    web.py reads it through app.static_folder rather than the cwd, so a script run from the
    wrong directory cannot hand a worker an empty manifest.

    AND companies.json IS NOT TOUCHED. scripts/test_companies_page.py freezes the served row at
    nine fields; a tenth would fail it. The logo set is also rebuilt on a different cadence than
    the directory, so coupling them means a build_companies.py run with a stale logo directory
    silently blanks every logo.
    """
    ar, alias = {}, {}
    for r in rows:
        name = r[0]
        slug = slugify(name)
        ent = ledger.get(slug) or {}
        if ent.get("verdict") != "accepted" or not ent.get("asset"):
            continue
        ext = ent["asset"].rsplit(".", 1)[-1]
        ar[slug] = [ext, ent.get("ar") or 1, 1 if ent.get("mono") else 0]
        key = core.norm_company(name)
        if key and key != slug:
            alias[key] = slug
    blob = {"v": int(time.time()), "ar": ar, "alias": alias}
    os.makedirs(LOGO_DIR, exist_ok=True)
    tmp = MANIFEST + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(blob, fh, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    os.replace(tmp, MANIFEST)
    return blob


# ---------------------------------------------------------------- the harvest pass

def load_domains():
    """norm_company -> domain, this script's own output. Empty map if it is missing, which
    degrades to companies.json's copy rather than to a crash."""
    try:
        with open(DOMAINS_JSON, encoding="utf-8") as fh:
            return (json.load(fh) or {}).get("domains") or {}
    except Exception:
        return {}


def run_harvest(args):
    rows = load_rows()
    ledger = load_ledger()
    global DOMAINS
    DOMAINS = load_domains()
    net = Net(pace=args.delay)
    cache = ClassCache(net)

    if args.only:
        want = {n.strip().lower() for n in args.only.split(",") if n.strip()}
        rows = [r for r in rows if r[0].lower() in want]
    refresh = {s.strip() for s in (args.refresh or "").split(",") if s.strip()}
    if refresh:
        # --refresh MEANS ONLY THESE. It used to add them to the pending queue rather than
        # replace it, so asking to redo two companies quietly started a run over the other
        # 1,400 -- and if another harvest was already going, both wrote the same ledger and the
        # last one to finish clobbered the other's progress.
        rows = [r for r in rows if slugify(r[0]) in refresh]

    todo = []
    for r in rows:
        slug = slugify(r[0])
        ent = ledger.get(slug)
        if slug in refresh:
            todo.append(r)
            continue
        if ent and ent.get("verdict") == "accepted" and ent.get("asset"):
            path = os.path.join(LOGO_DIR, ent["asset"])
            if os.path.exists(path):
                try:
                    with open(path, "rb") as fh:
                        if hashlib.sha256(fh.read()).hexdigest()[:16] == ent.get("sha256"):
                            continue            # done, and the bytes on disk are the bytes we judged
                except Exception:
                    pass
            todo.append(r)
            continue
        # A rejection is sticky; a DEFERRAL never is. That is the whole point of the split.
        if ent and ent.get("verdict") == "rejected" and not args.refetch_rejected:
            continue
        if ent and ent.get("verdict") == "no-candidate" and not args.refetch_rejected:
            continue
        todo.append(r)

    if args.limit:
        todo = todo[:args.limit]
    print("companies: %d   pending: %d   ledger: %d" % (len(rows), len(todo), len(ledger)))
    if not todo:
        write_manifest(load_rows(), ledger)
        print("nothing to do. manifest refreshed.")
        return 0

    t0 = time.time()
    tally = collections.Counter()
    attempted = accepted = 0
    debug_left = 3
    for i, r in enumerate(todo, 1):
        # THE DOMAIN MAP OUTRANKS companies.json's COPY OF IT. r[4] is a snapshot taken by
        # whichever build_companies.py run last happened, and this script rewrites the map
        # itself -- so the copy is stale by construction, and --check already reports the
        # divergence as a note (38 rows when this was written). Reading the map directly also
        # means --discover-domains takes effect immediately: no rebuild in between, and no
        # database needed to pick up a domain that was just resolved.
        name = r[0]
        stored = DOMAINS.get(core.norm_company(name) or "") or (r[4] or "")
        slug = slugify(name)
        try:
            ent = harvest_one(net, cache, name, stored, args.tier)
        except IOError as exc:
            ledger[slug] = {"name": name, "slug": slug, "verdict": "deferred",
                            "why": str(exc)[:120],
                            "checked": datetime.date.today().isoformat()}
            tally["deferred"] += 1
        except Exception as exc:
            # Unexpected shape, not a network fault. Keep the raw reason on the FIRST few so a
            # source changing shape is diagnosable from the committed artefact months later.
            ent = {"name": name, "slug": slug, "verdict": "deferred",
                   "why": "%s: %s" % (type(exc).__name__, str(exc)[:100]),
                   "checked": datetime.date.today().isoformat()}
            if debug_left > 0:
                ent["debug"] = repr(exc)[:400]
                debug_left -= 1
            ledger[slug] = ent
            tally["deferred"] += 1
        else:
            ledger[slug] = ent
            tally[ent["verdict"]] += 1
            if ent["verdict"] != "deferred":
                attempted += 1
                if ent["verdict"] == "accepted":
                    accepted += 1
        if i % 25 == 0 or i == len(todo):
            save_ledger(ledger)
            cache.flush()
            rate = i / max(0.001, time.time() - t0)
            print("  %5d/%d  %.1f/s  accepted %d  eta %dm  (%s)"
                  % (i, len(todo), rate, tally["accepted"],
                     (len(todo) - i) / max(0.01, rate) / 60, name[:34]))

    save_ledger(ledger)
    cache.flush()
    write_manifest(load_rows(), ledger)
    print()
    for k, v in tally.most_common():
        print("  %-13s %5d" % (k, v))
    print("  requests      %5d" % net.calls)

    # A SOURCE CHANGING SHAPE LOOKS EXACTLY LIKE A RATE LIMIT unless you look at the RATIO.
    # The 84%-miss threading result is the proof that this confusion is easy to make, so the
    # ratio is a hard failure rather than a log line.
    if attempted >= 50:
        rate = accepted / float(attempted)
        print("  accept rate   %5.1f%% of %d judged candidates" % (100 * rate, attempted))
        if rate < 0.20:
            print("\nFAIL accept rate below 20%. That is a source changing shape, not a "
                  "corpus of companies without logos. Nothing was deleted; existing assets "
                  "still serve. Check the first 'debug' entry in %s." % LEDGER)
            return 1
    return 0


# ---------------------------------------------------------------- domain repair

def run_audit_domains(args):
    """Write a reviewable CSV of stored-vs-P856. Touches no company_domains.json.

    WHY THIS RUNS BEFORE ANYTHING IS OVERWRITTEN. company_domains.json's only verification was
    `status_code == 200 and len(content) > 100` against an icon service, which accepts any
    registered domain -- including a parked one. Measured wrong entries include Apple ->
    appleinc.com, ID Logistics -> adp.com, World Wide Technology -> adp.com, Washington
    University in St. Louis -> washington.edu and Starkey -> starkey.com. P856 gets all five
    right. But "a curated source disagrees" is a reason to LOOK, not a licence to overwrite
    1,446 entries unseen.
    """
    rows = load_rows()
    ledger = load_ledger()
    net = Net(pace=args.delay)
    cache = ClassCache(net)
    if args.limit:
        rows = sorted(rows, key=lambda r: -r[5])[:args.limit]

    n = 0
    for i, r in enumerate(rows, 1):
        slug = slugify(r[0])
        ent = ledger.get(slug) or {}
        if "p856" in ent or ent.get("verdict") == "accepted":
            continue
        try:
            info = resolve(net, cache, r[0], r[4] or "")
        except Exception as exc:
            # A TRANSPORT FAILURE MUST NOT BE RECORDED AS "no official website". Writing p856=""
            # here would make the next run skip this company forever, which is the same
            # verdict-vs-transport confusion the harvest pass is built to avoid. Leave it absent.
            ledger[slug] = dict(ent, name=r[0], slug=slug, verdict="deferred",
                                why="%s: %s" % (type(exc).__name__, str(exc)[:90]))
            continue
        ent.update({"name": r[0], "slug": slug,
                    "p856": info.get("p856", ""), "qid": info.get("qid", ""),
                    "identity": info.get("identity", ""),
                    "logo_file": info.get("logo_file", ""),
                    "why": info.get("why", ""),
                    "checked": datetime.date.today().isoformat()})
        ledger[slug] = ent
        n += 1
        if n % 25 == 0:
            save_ledger(ledger)
            cache.flush()
            print("  resolved %d (%s)" % (n, r[0][:40]))
    save_ledger(ledger)
    cache.flush()

    with open(AUDIT_CSV, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["company", "norm_key", "h1b", "stored", "p856", "verdict", "qid",
                    "identity"])
        agree = differ = only_new = neither = 0
        for r in sorted(rows, key=lambda r: -r[5]):
            ent = ledger.get(slugify(r[0])) or {}
            stored, new = (r[4] or ""), (ent.get("p856") or "")
            if stored and new:
                verdict = "same" if stored == new else "CHANGED"
                agree += stored == new
                differ += stored != new
            elif new:
                verdict = "new"
                only_new += 1
            elif stored:
                verdict = "kept"
                neither += 1
            else:
                verdict = "none"
            w.writerow([r[0], core.norm_company(r[0]), r[5], stored, new, verdict,
                        ent.get("qid", ""), ent.get("identity", "")])
    print("\nwrote %s" % AUDIT_CSV)
    print("  agree %d   CHANGED %d   new (had none) %d   kept (no P856) %d"
          % (agree, differ, only_new, neither))
    return 0


# ---------------------------------------------------------------- domain discovery
#
# For the employers with NO domain at all -- 772 of them when this was written -- there is
# nothing for either tier to ask. Wikidata has no entity, so no P856, and companies.json has no
# r[4]. Two sources were measured and rejected before this one:
#
#   * the careers URL in companies.json. It is almost always an ATS host, and gating it on the
#     name recovered 2 of 772. Dead end.
#   * status_code == 200 on a guessed domain. That was scripts/build_company_domains.py's
#     ENTIRE verification, and it is why Apple had appleinc.com and why two employers who
#     merely post through ADP both had adp.com. This script exists partly to undo it.
#
# So: guess the domain, then make the PAGE prove it belongs to this employer. Measured yield on
# 24 sampled employers with a crude generator: 12 resolved, 7 then produced an icon >=128px.
DISCOVER_TLDS = (".com", ".org", ".io")
EDU_WORDS = ("university", "college", "school", "academy")
# A NON-.com HIT NEEDS THE STRONG CORROBORATION PATH, and that is a measured rule. In the first
# full pass 60 of 351 hits were a full-name match on .org or .net, and the bad ones were bad in
# a specific way -- they were all ONE-WORD employers, where the only evidence is that a domain
# spelling the word exists and its page uses the word. citadel.org is The Citadel, a military
# college in South Carolina, not the hedge fund; vastek.org, natsoft.org and donato.net are not
# the IT firms that share those names.
#
# Banning the tld outright was the first attempt and it was wrong: it also lost mountsinai.org,
# which is right, and every hospital and foundation. The tld is not the discriminator -- the
# WEAKNESS OF THE MATCH is. So .org stays a candidate and the single-token path is confined to
# .com and .edu, where a squatter is at least paying for the privilege.
SINGLE_TOKEN_TLDS = (".com", ".edu")
# Legal FORM only. Deliberately much shorter than SUFFIXES above, because these two lists answer
# different questions and sharing one was the defect: SUFFIXES is for building a domain, where
# "Technologies" and "Corporation" never appear, but for VERIFYING IDENTITY they are the name.
# core.norm_company strips them, so "Boston Technology Corporation" became "boston" -- and
# boston.com, which is The Boston Globe, corroborated it perfectly. Same for "Quantum
# Technologies LLC" against Quantum Corporation's quantum.com, and "Quadrant Technologies"
# against quadrant.org.
LEGAL_FORMS = {"inc", "incorporated", "llc", "ltd", "limited", "plc", "lp", "llp", "corp",
               "corporation", "co", "company", "the", "of", "and", "a", "an"}
# WIDENED BY WHAT IT MISSED. deutschebanksecurities.com's title is literally
# "deutschebanksecurities.com for sale | Spaceship.com" -- the old pattern needed the words
# "domain ... for sale" adjacent, so a marketplace that leads with the domain name walked
# straight through and was recorded as Deutsche Bank Securities' website. A bare "for sale" in a
# TITLE is never a real employer homepage.
PARKED = re.compile(r"\bfor sale\b|\bparked\b|godaddy|sedo\b|hugedomains|buy this domain"
                    r"|namecheap|afternic|dan\.com|spaceship|squadhelp|brandbucket|atom\.com"
                    r"|this domain is (?:available|for)", re.I)
_TITLE = re.compile(r"<title[^>]*>(.{0,300}?)</title>", re.S | re.I)
_OGSITE = re.compile(r"""og:site_name["'][^>]*content=["']([^"']{0,160})""", re.I)
_LDNAME = re.compile(r'"name"\s*:\s*"([^"]{0,120})"')


def raw_tokens(name):
    """The employer's own words, with only the legal FORM removed.

    This is the identity vocabulary, and it is not _tokens(). _tokens goes through
    core.norm_company, which strips Technologies / Corporation / Group / Holdings because those
    never appear in a domain -- correct for building a guess, wrong for checking one.
    """
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    # A LEGAL FORM TRAILS A NAME, IT NEVER LEADS ONE. Stripping positionally-blind cost
    # "LP Analyst" its first word -- "lp" is in the list -- so the name became "analyst" and
    # analyst.com corroborated it. The first word is always part of the name.
    return words[:1] + [w for w in words[1:] if w not in LEGAL_FORMS]


def discover_candidates(name):
    """Domains worth ASKING about for an employer we have no domain for at all."""
    t = _tokens(name)
    if not t:
        return []
    sq = "".join(t)
    low = (name or "").lower()
    tlds = list(DISCOVER_TLDS)
    if any(w in low for w in EDU_WORDS):
        tlds.insert(0, ".edu")
    out = [sq + x for x in tlds[:3]]
    if len(t) > 1:
        out.append("".join(t[:2]) + ".com")
        out.append("".join(w[0] for w in t) + ".com")        # the acronym: nva.com
        out.append("".join(t[:-1]) + ".com")                 # drop a trailing generic word
    # A DNS LABEL IS 63 OCTETS, and a guess built by squashing an employer's name blows past
    # that easily: "Encompass Health Rehabilitation Hospital A Partner Of Washington Regional"
    # squashes to 65 characters. urllib3 raises LocationParseError for it, which is not an
    # IOError, so it took the whole pass down 24 employers in and lost every domain it had
    # found. Refused here as well as caught below, because an unresolvable guess is not worth a
    # request.
    seen = set()
    return [d for d in out
            if 3 <= len(d.split(".")[0]) <= 63 and not (d in seen or seen.add(d))][:6]


def name_corroborated(name, html, domain):
    """(bool, why). Does the PAGE say it belongs to this employer?

    EVERY distinctive token must appear, not all-but-one. Measured while sizing this pass: an
    n-1 rule accepted "Future Secure AI" -> future.com, which is Future plc, a UK media company
    that serves a perfectly good SVG icon -- so the pass would have SHIPPED a stranger's logo
    under a real employer's name. That is the GardaWorld/appcast.io class and it is strictly
    worse than a monogram, so the rule is deliberately tuned for precision over yield.

    Two accept paths, and each needs agreement from TWO places:
      * the domain's own label is the squashed name AND the page repeats it. Both the address
        and the content agree, which is the strongest evidence available without a registry.
      * two or more distinctive tokens all appear. One token is a coincidence -- "future" --
        and two independent ones are not.
    """
    ti = (_TITLE.search(html or "") or [None, ""])[1]
    if PARKED.search(ti or "") or PARKED.search((html or "")[:3000]):
        return False, "parked"
    parts = [ti or "", (_OGSITE.search(html or "") or [None, ""])[1]]
    parts += _LDNAME.findall(html or "")[:4]
    hay = re.sub(r"[^a-z0-9]", "", " ".join(parts).lower())
    if not hay:
        return False, "no-identity-text"
    # RAW tokens, not normalised ones -- see raw_tokens(). Every distinctive one must appear.
    raw = raw_tokens(name)
    dist = [w for w in raw if len(w) >= 4]
    hit = bool(dist) and all(w in hay for w in dist)
    # A US EDUCATION EMPLOYER IS ON .edu OR IT IS NOT THEM, and this has to be tested BEFORE the
    # strong path below can return. universityofflorida.org, universityofsouthflorida.com and
    # universityofnewhampshire.com each corroborated on two tokens and none of them is the
    # university (ufl.edu, usf.edu, unh.edu) -- the squashed legal name is what a squatter
    # registers, which is exactly why two tokens agreeing is not enough here.
    if any(w in (name or "").lower() for w in EDU_WORDS) and not (domain or "").endswith(".edu"):
        return False, "edu-not-on-edu"
    # TWO independent words agreeing is the strong path, and it is the only one allowed to
    # accept any tld. One word is a coincidence waiting to happen.
    if hit and len(dist) >= 2:
        return True, "all-tokens-in-page"
    # THE WEAK PATHS ARE CONFINED TO .com AND .edu. A one-word employer cannot be told apart
    # from another organisation of the same name by reading a page -- so the tld is used as the
    # tie-breaker it actually is: the brand is on .com, and citadel.org is a military college
    # while citadel.com is the hedge fund. This does not refuse the employer, it refuses the
    # wrong ADDRESS for it.
    weak_ok = any((domain or "").endswith(t) for t in SINGLE_TOKEN_TLDS)
    # IF WE ARE LEANING ON ONE WORD, THAT WORD HAS TO BE THE WHOLE NAME. The weak path only sees
    # tokens of 4+ characters, so a two-word employer with a short second word collapsed to one
    # word and then matched a domain that is only its FIRST word: "First Tek" -> first.com,
    # "Lead IT" -> lead.com, "New Era Technology" -> new.com, "SMBC US" -> smbc.com, "Concord
    # USA" -> concord.com, "Phantom AI" -> phantom.com. None of those is the employer. The
    # strong two-word path is unaffected, which is what keeps paycom.com and highmark.com.
    if hit and weak_ok and (domain or "").split(".")[0] == "".join(raw):
        return True, "single-token-in-page"
    # And the last resort, for a name with no 4+ character word at all: the domain label spells
    # the squashed name and the page repeats it. The CSV marks these, because they are the
    # Alphabet case -- see DOMAIN_OVERRIDE.
    sq = "".join(_tokens(name))
    label = (domain or "").split(".")[0]
    if sq and len(sq) >= 4 and label == sq and sq in hay and not dist and weak_ok:
        return True, "domain-and-page"
    return False, "no-corroboration"


def run_discover_domains(args):
    """Resolve a domain for employers that have none, by asking the page who it belongs to.

    MERGES rather than rebuilds. Every entry it adds is new -- a key already in the map is left
    alone, because that map is --write-domains' output and has recorded provenance. Discovered
    entries are tagged so they stay separable and revocable, and the one-domain-one-company
    invariant --check enforces is applied here too rather than being discovered later.
    """
    rows = load_rows()
    try:
        with open(DOMAINS_JSON, encoding="utf-8") as fh:
            blob = json.load(fh) or {}
    except Exception:
        blob = {}
    out = blob.get("domains") or {}
    prov = blob.get("provenance") or {}
    taken = {v: k for k, v in out.items()}

    todo = []
    for r in rows:
        key = core.norm_company(r[0])
        if not key or out.get(key) or key in DOMAIN_OVERRIDE:
            continue
        todo.append((r[0], key, int(r[5] or 0)))
    todo.sort(key=lambda t: -t[2])           # biggest sponsors first, so a --limit is useful
    if args.limit:
        todo = todo[:args.limit]
    print("employers with no domain: %d" % len(todo))

    net = Net(pace=args.delay)
    found, review, n = {}, [], 0
    for name, key, h1b in todo:
        n += 1
        sys.stdout.write("\r  %d/%d  found %d  (%s)%s"
                         % (n, len(todo), len(found), name[:28], " " * 12))
        sys.stdout.flush()
        for dom in discover_candidates(name):
            if dom in taken or dom in found.values():
                continue                     # already another employer's, by construction
            if any(h in dom for h in PLATFORM_HOSTS):
                continue
            try:
                r = net.get("https://" + dom + "/", ua=BROWSER_UA, timeout=(5, 10))
            except Exception:
                # DELIBERATELY BROADER THAN IOError, and the opposite of the harvest's rule.
                # There, a transport failure must propagate so the row is recorded `deferred`
                # and retried rather than being written off. Here the request is a GUESS about
                # a domain that may not exist, be malformed, or have a broken certificate --
                # every one of those is an answer, not an outage, and none of them is worth
                # discarding the other 800 employers' results for.
                continue
            if r is None:
                continue
            ok, why = name_corroborated(name, r.text or "", dom)
            if not ok:
                continue
            if not domain_agrees(name, dom):
                continue                     # the same gate every other domain here passes
            found[key] = dom
            review.append((name, key, dom, why, h1b))
            break
    print()

    if not found:
        print("nothing discovered.")
        return 0
    with open(DISCOVER_CSV, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        # distinctive_tokens IS THE RISK COLUMN. A multi-token name needs two independent
        # words to agree and is hard to get wrong; a single-token one is the Alphabet case and is
        # what the skim is for. Sorted by filings so the rows that matter are at the top.
        w.writerow(["name", "norm_key", "domain", "corroborated_by", "h1b_filings",
                    "distinctive_tokens"])
        for row in sorted(review, key=lambda t: -t[4]):
            w.writerow(list(row) + [len([w2 for w2 in _tokens(row[0]) if len(w2) >= 4])])
    print("wrote %s -- %d rows. READ IT before committing any asset this unlocks; it is the one "
          "step no test can do." % (DISCOVER_CSV, len(review)))
    if args.dry_run:
        print("dry run. would add %d entries to %s." % (len(found), DOMAINS_JSON))
        return 0
    out.update(found)
    for k in found:
        prov[k] = "discovered"
    blob["domains"] = out
    blob["provenance"] = prov
    blob["built_at"] = datetime.date.today().isoformat()
    tmp = DOMAINS_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(blob, fh, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, DOMAINS_JSON)
    print("added %d discovered entries to %s (now %d)." % (len(found), DOMAINS_JSON, len(out)))
    print("NEXT: build_logos.py --refetch-rejected picks them up; the harvest reads this map "
          "directly, so companies.json does not have to be rebuilt first.")
    return 0


def run_write_domains(args):
    """Rewrite company_domains.json from the ledger's P856 values.

    KEYED ON core.norm_company, which is the fix for a defect that had nothing to do with
    logos: the old file was keyed on the RAW lowercased name, and web.py::_verified_domains
    looked it up that way while scripts/build_companies.py rebucketed it through
    core.norm_company. It held both 'apple' -> apple.com and 'apple, inc.' -> appleinc.com, and
    the rebucket kept whichever it read last -- so the file gave two different answers to the
    same question and both were live. Seven keys had that conflict.
    """
    rows = load_rows()
    ledger = load_ledger()
    try:
        with open(DOMAINS_JSON, encoding="utf-8") as fh:
            old = (json.load(fh) or {}).get("domains") or {}
    except Exception:
        old = {}

    out, prov = {}, {}
    for r in rows:
        key = core.norm_company(r[0])
        if not key:
            continue
        ent = ledger.get(slugify(r[0])) or {}
        stored, new = (r[4] or ""), (ent.get("p856") or "")
        # A STORED DOMAIN WHOSE ROOT IS EXACTLY THE COMPANY NAME OUTRANKS P856. Measured: this
        # keeps google.com over Wikidata's about.google, qualcomm.com over its polluted P856, and
        # amazonwebservices.com over aws.amazon.com (which would collide with Amazon's own row).
        # It still replaces appleinc.com, because "appleinc" is not "apple" -- which is the whole
        # class of defect this pass exists to fix.
        if stored and stored.split(".")[0] == "".join(_tokens(r[0])):
            out[key], prov[key] = stored, "name-exact"
        elif new:
            out[key], prov[key] = new, "p856"
        elif stored and domain_agrees(r[0], stored):
            # AN UNVERIFIED DOMAIN IS ONLY KEPT IF IT AT LEAST CORROBORATES THE NAME. These came
            # from the old guess-and-probe builder, and build_companies.py turns each one into the
            # tile's "Website" link -- so a survivor that shares nothing with the employer's name
            # is a link to somebody else. GardaWorld Security Services kept appcast.io this way,
            # which is its job board, and it kept it even after the shared-domain check stopped
            # firing because the other claimant had moved to its own site.
            out[key], prov[key] = stored, "kept-unverified"
    # Carry over anything the current corpus no longer lists, so knowledge is not lost when an
    # employer drops out of the scrape. SAME corroboration test as above: this loop is how
    # GardaWorld kept appcast.io even after the rows loop correctly refused it.
    for k, v in old.items():
        nk = core.norm_company(k)
        if nk and nk not in out and domain_agrees(nk, v):
            out[nk], prov[nk] = v, "kept-unverified"

    # THE ONE-DOMAIN-ONE-COMPANY INVARIANT. Two distinct employers on one domain means at most
    # one of them is right, and every measured instance was an ATS host that no blocklist had.
    # Verified entries win; unverified duplicates are dropped rather than guessed at.

    byd = collections.defaultdict(list)
    for k, v in out.items():
        byd[v].append(k)
    dropped = 0
    for dom, keys in byd.items():
        if len(keys) < 2 or same_employer(keys, dom):
            continue
        # P856 wins because it is curated. Failing that, the row whose own name IS the domain
        # keeps it: without this, adp.com was taken from ADP because a second employer that
        # merely posts through ADP claimed it too and neither had a curated answer.
        verified = [k for k in keys if prov[k] == "p856"]
        exact = [k for k in keys if prov[k] == "name-exact"]
        keep = set(verified or exact)
        for k in keys:
            if k not in keep:
                out.pop(k, None)
                dropped += 1
        print("  conflict %-28s %s  -> kept %s"
              % (dom, keys[:4], sorted(keep)[:2] or "none"))

    blob = {"note": "company (core.norm_company) -> domain. Built by scripts/build_logos.py "
                    "--write-domains, from Wikidata P856 (official website) where available. "
                    "Keyed on core.norm_company, NOT the raw name: the old raw keying let "
                    "'apple' and 'apple, inc.' disagree, and build_companies.py kept whichever "
                    "it read last. web.py::_verified_domains must look up the same way.",
            "built_at": datetime.date.today().isoformat(),
            "provenance_counts": dict(collections.Counter(prov.values())),
            "domains": out}
    if args.dry_run:
        print("\ndry run. would write %d entries (%s), dropping %d shared-domain guesses."
              % (len(out), blob["provenance_counts"], dropped))
        return 0
    tmp = DOMAINS_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(blob, fh, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, DOMAINS_JSON)
    print("\nwrote %s: %d entries (%s), dropped %d shared-domain guesses."
          % (DOMAINS_JSON, len(out), blob["provenance_counts"], dropped))
    print("DEPLOY IT: web.py loads this at first request, so a map that stays on this laptop "
          "changes nothing live.")
    return 0


# ---------------------------------------------------------------- the gate

# COVERAGE, AS TWO SHARE FLOORS RATHER THAN ONE ABSOLUTE RULE.
#
# The first version of this gate demanded a logo for EVERY employer with 1,000+ H-1B filings.
# Measured against the real corpus that is not achievable and never was: a large part of that
# cohort is named the way a federal filing names it, not the way a brand does -- "Aetna
# Resources", "At&T Services", "Atos Syntel INC", "Boston Technology Corporation" -- and no
# amount of resolver work turns those into a Wikidata entity. Several of them have no logo
# anywhere to find.
#
# So the gate is calibrated to the measured achievable rate with a margin below it, and its job
# is to catch a REGRESSION rather than to assert perfection. That is what protects against the
# 53% coming back, and unlike a floor that cannot be met it will actually be believed.
# Measured 2026-08-22 after the first full harvest: 96/143 = 67% of the 1,000+ cohort and
# 427/702 = 60% of the 100+ cohort. RE-BASELINED 2026-08-23 after the homonym fix, the wider
# site chain and the thumb fallback: 102/143 = 71.3% and 70.5%, over 1,472 logos against 1,148.
# The floors sit ~7 points below each, which is wide enough that a source having a bad day does
# not fail the build and tight enough that losing a hundred logos does. Raise them when a
# harvest beats them by more than that margin -- a floor that no longer bites is not a gate.
HEAD_HARD = 1000        # 143 rows, 71.3% covered
HARD_FLOOR = 0.64
HEAD_SOFT = 100         # 702 rows, 70.5% covered
SOFT_FLOOR = 0.63


def run_check(args):
    """Fail the build on anything that would let the 53% back in.

    EVERY CONDITION IS ABOUT THE RESULT ON DISK, not about whether the harvest reported success.
    That is the difference between this and the chain it replaces.
    """
    rows = load_rows()
    ledger = load_ledger()
    try:
        with open(MANIFEST, encoding="utf-8") as fh:
            man = json.load(fh) or {}
    except Exception:
        man = {}
    ar = man.get("ar") or {}
    fails = []

    def fail(msg):
        fails.append(msg)
        print("FAIL " + msg)

    # 1. coverage, a per-row floor AND a share ceiling. The floor alone cannot catch everything
    #    sagging just under the bar.
    hard = [r for r in rows if r[5] >= HEAD_HARD]
    hard_have = sum(1 for r in hard if slugify(r[0]) in ar)
    hard_share = hard_have / float(len(hard) or 1)
    if hard_share < HARD_FLOOR:
        missing = sorted(r[0] for r in hard if slugify(r[0]) not in ar)
        fail("only %.1f%% of the %d employers with %d+ H-1B filings have a logo, floor is %.0f%%. "
             "Missing: %s" % (100 * hard_share, len(hard), HEAD_HARD, 100 * HARD_FLOOR,
                              ", ".join(missing[:8])))
    soft = [r for r in rows if r[5] >= HEAD_SOFT]
    have = sum(1 for r in soft if slugify(r[0]) in ar)
    share = have / float(len(soft) or 1)
    if share < SOFT_FLOOR:
        fail("only %.1f%% of the %d employers with %d+ filings have a logo, floor is %.0f%%"
             % (100 * share, len(soft), HEAD_SOFT, 100 * SOFT_FLOOR))

    # 2. a manifest entry whose bytes are not the bytes we judged. This is what makes shipping
    #    with NO remote fallback safe.
    total = 0
    for slug, (ext, _a, _m) in sorted(ar.items()):
        path = os.path.join(LOGO_DIR, "%s.%s" % (slug, ext))
        ent = ledger.get(slug) or {}
        if not os.path.exists(path):
            fail("manifest lists %s but the file is missing" % path)
            continue
        with open(path, "rb") as fh:
            raw = fh.read()
        total += len(raw)
        if len(raw) > MAX_BYTES_STORED:
            fail("%s is %d bytes, over the %d cap" % (path, len(raw), MAX_BYTES_STORED))
        if ent.get("sha256") and hashlib.sha256(raw).hexdigest()[:16] != ent["sha256"]:
            fail("%s does not match the sha256 in the ledger" % path)
        # 5. re-apply the acceptance test to the bytes that actually SHIP. Raster only: a
        #    stored SVG has no pixels here, and re-rendering it would need the network. Its
        #    safety scan below is the equivalent guarantee.
        if ext != "svg":
            ok, why, _meta = judge(raw)
            if not ok:
                fail("%s no longer passes the acceptance test (%s)" % (path, why))
        else:
            # 6. the sanitiser's output, re-verified.
            if SVG_UNSAFE_OUT.search(raw) or SVG_EXTERNAL.search(raw):
                fail("%s contains script, an on* handler, an entity or an external URL" % path)

    # 2b. A SITE ICON WHOSE DOMAIN DOES NOT CORROBORATE ITS COMPANY. This is the one condition
    #     here about the identity of an asset rather than its bytes, and it earns its place: the
    #     two it caught were an employer's job-board vendor, so the tile showed a real, sharp,
    #     entirely wrong logo. Tier 1 cannot fail this way because P856 is checked against the
    #     name before it is used.
    for slug, ent in sorted(ledger.items()):
        if ent.get("verdict") != "accepted" or ent.get("tier") != "site":
            continue
        if slug in ar and not domain_agrees(ent.get("name") or "", ent.get("domain") or ""):
            fail("%s took its icon from %s, which does not corroborate the name"
                 % (ent.get("name"), ent.get("domain")))

    # 3. an orphan asset with no manifest entry
    if os.path.isdir(LOGO_DIR):
        for fn in sorted(os.listdir(LOGO_DIR)):
            if fn == "index.json" or fn.endswith(".tmp"):
                continue
            slug, _, ext = fn.rpartition(".")
            if slug not in ar or ar[slug][0] != ext:
                fail("%s is not in the manifest (run --prune)" % os.path.join(LOGO_DIR, fn))

    # 4. the directory budget
    if total > DIR_BUDGET:
        fail("static/logos is %.1f MB, budget is %.0f MB"
             % (total / 1048576.0, DIR_BUDGET / 1048576.0))

    # 7. ONE DOMAIN, ONE COMPANY -- asserted against the MAP, which is the source of truth and the
    #    file --write-domains owns. companies.json's own domain column is DERIVED from it by
    #    scripts/build_companies.py, which needs live database credentials, so asserting there
    #    would make this gate unfixable from a laptop and unfixable in CI. Staleness is reported
    #    below instead, with the command that clears it.
    try:
        with open(DOMAINS_JSON, encoding="utf-8") as fh:
            dmap = (json.load(fh) or {}).get("domains") or {}
    except Exception:
        dmap = {}
    byd = collections.defaultdict(set)
    for k, v in dmap.items():
        if v:
            byd[v].add(k)
    # SAME RULE THE WRITER USES. Two spellings of one employer sharing a domain is benign and
    # --write-domains keeps both on purpose; without this the gate rejected its own output, and
    # 21 of the 21 flagged pairs were things like amazon / amazon com and citi / citibank.
    shared = {d: ks for d, ks in byd.items()
              if len(ks) > 1 and not same_employer(sorted(ks), d)}
    if shared:
        fail("%d domains in %s are claimed by DIFFERENT employers, so at most one is right: %s"
             % (len(shared), DOMAINS_JSON,
                "; ".join("%s -> %s" % (d, sorted(k)[:3]) for d, k in sorted(shared.items())[:4])))

    # And the derived column, as a NOTE. A wrong entry here is a wrong "Website" link on the
    # tile as well as a wrong logo domain, so it matters -- but it is cleared by a rebuild that
    # needs the database, not by anything this script can do.
    stale = sorted({r[4] for r in rows
                    if r[4] and dmap.get(core.norm_company(r[0])) not in (None, r[4])})
    if stale:
        print("note: %d rows in %s carry a domain the map has since corrected (e.g. %s). "
              "Clear with DB_REQUIRE=proxy python scripts/build_companies.py"
              % (len(stale), COMPANIES_JSON, ", ".join(stale[:4])))

    print()
    print("logos %d   bytes %.1f MB   head(%d+) %d/%d = %.1f%%   head(%d+) %.1f%%"
          % (len(ar), total / 1048576.0, HEAD_HARD, hard_have, len(hard), 100 * hard_share,
             HEAD_SOFT, 100 * share))
    if fails:
        print("\n%d FAILED" % len(fails))
        return 1
    print("OK")
    return 0


def run_report(args):
    rows = load_rows()
    ledger = load_ledger()
    tally = collections.Counter()
    why = collections.Counter()
    tiers = collections.Counter()
    for r in rows:
        ent = ledger.get(slugify(r[0]))
        if not ent:
            tally["not-attempted"] += 1
            continue
        tally[ent.get("verdict") or "?"] += 1
        if ent.get("verdict") == "accepted":
            tiers[ent.get("tier") or "?"] += 1
        elif ent.get("why"):
            why[ent["why"]] += 1
    n = len(rows) or 1
    print("companies %d" % len(rows))
    for k, v in tally.most_common():
        print("  %-15s %5d  %5.1f%%" % (k, v, 100.0 * v / n))
    if tiers:
        print("\naccepted by source")
        for k, v in tiers.most_common():
            print("  %-15s %5d" % (k, v))
    if why:
        print("\nwhy not")
        for k, v in why.most_common(14):
            print("  %-15s %5d" % (k, v))
    wide = [(ent.get("ar"), ent.get("name")) for ent in ledger.values()
            if ent.get("verdict") == "accepted" and (ent.get("ar") or 0) >= 4]
    print("\nwordmarks at 4:1 or wider: %d" % len(wide))
    for a, nm in sorted(wide, reverse=True)[:6]:
        print("  %4.1f:1  %s" % (a, nm))
    return 0


def run_prune(args):
    rows = load_rows()
    ledger = load_ledger()
    man = write_manifest(rows, ledger)
    ar = man.get("ar") or {}
    gone = 0
    for fn in sorted(os.listdir(LOGO_DIR)) if os.path.isdir(LOGO_DIR) else []:
        if fn == "index.json":
            continue
        slug, _, ext = fn.rpartition(".")
        if slug not in ar or ar[slug][0] != ext:
            os.remove(os.path.join(LOGO_DIR, fn))
            gone += 1
    print("manifest %d entries; removed %d orphan assets" % (len(ar), gone))
    return 0


# ---------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default="", help="comma-separated exact company names")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--tier", choices=("all", "wikidata", "site"), default="all")
    ap.add_argument("--refresh", default="", help="comma-separated slugs to re-harvest")
    ap.add_argument("--refetch-rejected", action="store_true",
                    help="retry companies whose last verdict was a rejection")
    ap.add_argument("--delay", type=float, default=PACE, help="seconds between calls per host")
    ap.add_argument("--report", action="store_true", help="census of the ledger, writes nothing")
    ap.add_argument("--check", action="store_true", help="the CI gate")
    ap.add_argument("--prune", action="store_true", help="drop assets with no manifest entry")
    ap.add_argument("--audit-domains", action="store_true",
                    help="CSV of stored vs P856. Writes no company_domains.json.")
    ap.add_argument("--discover-domains", action="store_true",
                    help="guess a domain for employers that have none, verified by identity")
    ap.add_argument("--write-domains", action="store_true",
                    help="rewrite company_domains.json from P856")
    ap.add_argument("--dry-run", action="store_true", help="with --write-domains, print only")
    args = ap.parse_args()

    if not os.path.exists(COMPANIES_JSON):
        print("no %s here. Run this from the app directory." % COMPANIES_JSON)
        return 2
    if args.check:
        return run_check(args)
    if args.report:
        return run_report(args)
    if args.prune:
        return run_prune(args)
    if args.audit_domains:
        return run_audit_domains(args)
    if args.discover_domains:
        return run_discover_domains(args)
    if args.write_domains:
        return run_write_domains(args)
    return run_harvest(args)


if __name__ == "__main__":
    sys.exit(main())
