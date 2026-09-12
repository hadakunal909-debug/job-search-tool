"""
core.py — all the logic for the job-match app, kept free of Streamlit so it can
be tested on its own. app.py imports from here and only handles the UI.
"""

import bisect
import csv
import html
import logging
import os
import re
import json
import math
import sys
import mmap
import zlib
import array
import struct
import hashlib
import datetime
from io import BytesIO
from collections import Counter
from functools import lru_cache



# THE WEB APP DOES NOT SCRAPE, AND IT WAS PAYING FOR THE SCRAPER ANYWAY.
#
# `import requests` costs ~370 ms and `import bs4` ~130 ms, measured with -X importtime. Both
# sat at module scope here, so every Flask worker paid half a second of start-up for libraries
# the feed never touches: between them there are four `requests.` call sites (fetch_jd and the
# three AI-tailoring functions) and two BeautifulSoup ones (html_to_text, fetch_jd), and not
# one of them is on the path that renders a job card.
#
# Same shape as db.py's _LazyHTTP, and for the same reason it gives there. Every call site
# below is unchanged -- `requests.get(...)` and `BeautifulSoup(raw, "lxml")` both still read
# exactly as they did -- so this is an import-time change and nothing else.
class _LazyModule(object):
    """Imports the real module on first attribute access, then gets out of the way."""
    def __init__(self, name):
        self._name, self._mod = name, None

    def __getattr__(self, attr):
        if self._mod is None:
            import importlib
            self._mod = importlib.import_module(self._name)
        return getattr(self._mod, attr)


requests = _LazyModule("requests")


def BeautifulSoup(*args, **kwargs):
    """bs4's parser, imported on first parse. A function rather than a _LazyModule because
    every call site uses it as a CALLABLE, not as an attribute of a module."""
    from bs4 import BeautifulSoup as _BeautifulSoup
    return _BeautifulSoup(*args, **kwargs)


HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/124.0 Safari/537.36"}

# Default model for AI tailoring; swap to any model string your key can access.
AI_MODEL = "claude-opus-4-8"


# ------------------------------------------------------------
# Jobs
# ------------------------------------------------------------
SAMPLE_JOBS = [
    {"found_date": "", "title": "Project Coordinator", "company": "Sample Co",
     "location": "Boston, MA", "url": "https://example.com/jobs/1", "sponsors_h1b": "unknown"},
    {"found_date": "", "title": "Associate Program Manager", "company": "Sample Co",
     "location": "Remote (US)", "url": "https://example.com/jobs/2", "sponsors_h1b": "unknown"},
    {"found_date": "", "title": "Operations Analyst", "company": "Sample Co",
     "location": "New York, NY", "url": "https://example.com/jobs/3", "sponsors_h1b": "unknown"},
]


def load_jobs(path="jobs.csv"):
    """Read the scraper's output. Returns sample rows ONLY when jobs.csv doesn't
    exist yet (so the UI is explorable on first launch). If the file exists but is
    empty, returns [] so the app can explain that filters removed everything."""
    if not os.path.exists(path):
        return list(SAMPLE_JOBS)
    with open(path, newline="", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if r.get("url")]


# ------------------------------------------------------------
# Keyword extraction + resume<->JD matching
# ------------------------------------------------------------
STOPWORDS = set("""
a an and or but the of to in for on with at by from as is are was were be been being
this that these those it its their our your you we they he she them his her not no nor
will would can could should may might must do does did done have has had having about
into over under again further then once here there all any both each few more most other
some such only own same so than too very up down out off above below between during who
whom which what when where why how if because while per via etc within across upon also
us am i me my mine ours yours job role position company team work
""".split())

# Words that appear in nearly every JD and carry little signal for matching.
JD_BOILERPLATE = set("""
experience experiences working ability abilities years year skills skill strong excellent
required requirement requirements responsibility responsibilities preferred plus including
include includes related ideal candidate candidates opportunity opportunities environment
help support ensure provide drive build manage manages managing lead leads leading deliver
delivers cross functional looking join growth fast paced paced ll re ve day days week weeks
new like using use used able high level levels world class etc apply applicants applicant
benefits equal employer diversity inclusive
salary base compensation pay hourly bonus equity range eligible eligibility insurance
medical dental vision pto holidays veteran gender race disability accommodation reasonable
location locations remote hybrid onsite office travel sponsorship visa authorization citizen
status eeo position week weekly month monthly annual annually
""".split())

# Perks, benefits and compensation vocabulary that must never be SUGGESTED AS A RÉSUMÉ KEYWORD.
#
# Advising a candidate to add "retirement" and "dental" to their CV is the most visible way the
# keyword panel can lose someone's trust, because the error is obvious to them while the rest of
# the panel is not verifiable at a glance. JD_BOILERPLATE above already drops much of this at
# EXTRACTION time; this set is the display-side backstop for what still gets through, and it
# deliberately covers the leave/retirement/wellness vocabulary that set does not.
#
# SEPARATE FROM JD_BOILERPLATE ON PURPOSE. Adding these there would change which terms
# core_terms() is computed from, i.e. every match score in the corpus. That may well be the
# better fix, but it is a rescoring and wants measuring first; suppressing a suggestion needs
# neither. See docs/QA_AUDIT.md U15.
PERK_TERMS = set("""
retirement 401k 403b pension wellness wellbeing tuition reimbursement stipend sabbatical
parental maternity paternity bereavement leave vacation sick pto holiday holidays
flexible flexibility hybrid remote-first commuter childcare daycare gym fitness discount
discounts perks perk benefit benefits insurance medical dental vision life disability
compensation salary bonus equity rsu rsus espp payroll paid unpaid
""".split())


WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+#./-]{1,}")


def _tokens(text):
    return [w.lower() for w in WORD_RE.findall(text or "")]


_SEGMENT_RE = re.compile(r"[.,;:/()\[\]{}\n\t\u2022|]+")


def extract_keywords(text, top_n=28, max_bigrams=8, extra_skip=None, idf=None):
    """Pull the most signal-bearing terms from a job description: meaningful
    single words plus two-word phrases. Phrases are formed only WITHIN a clause
    (text is split on punctuation first) so we never glue together words from
    different sentences. `extra_skip` lets the caller drop more words (e.g. the
    company name) so they don't show up as résumé 'gaps'."""
    skip = STOPWORDS | JD_BOILERPLATE | (set(extra_skip) if extra_skip else set())
    uni_counter, bi_counter = Counter(), Counter()
    for segment in _SEGMENT_RE.split(text or ""):
        raw = [t.strip("-.+#/") for t in _tokens(segment)]
        raw = [t for t in raw if t]
        uni_counter.update(t for t in raw if len(t) > 2 and t not in skip)
        for a, b in zip(raw, raw[1:]):          # only truly adjacent words
            if a in skip or b in skip or len(a) <= 2 or len(b) <= 2:
                continue
            bi_counter[f"{a} {b}"] += 1

    # TIE-BREAK ON INFORMATION, NOT ON STRING LENGTH. `len(kv[0])` ranked every count-1 bigram
    # by how many characters it had, which handed the eight available slots to the EEO
    # paragraph: "consideration regarding", "criminal background" and "background inquiries"
    # outranked "security clearance" and "program management" on nothing but character count.
    #
    # Do NOT simply invert it. Measured over 800 postings, preferring SHORT changes the slots on
    # 100% of them and admits "paid time", "fair chance", "local law", "lie detector" and "los
    # angeles" while dropping "artificial intelligence", "software engineering" and "continuous
    # improvement". Length correlates with informativeness in both directions at once, so it is
    # not the statistic. idf is, and analyze_jd already has it in hand; an unseen phrase takes
    # _UNSEEN_W here for the same reason it does in analyze_jd's weighting.
    def _rank(kv):
        return (kv[1], min(idf.get(kv[0], _UNSEEN_W), _RARE_W_CAP) if idf else 0.0, kv[0])

    bigrams = [k for k, _ in sorted(bi_counter.items(), key=_rank, reverse=True)][:max_bigrams]
    bigram_words = {w for bg in bigrams for w in bg.split()}
    unigrams = [k for k, _ in sorted(uni_counter.items(), key=_rank, reverse=True)]

    keywords = list(bigrams)
    for u in unigrams:
        if u in bigram_words:           # already covered by a chosen phrase
            continue
        keywords.append(u)
        if len(keywords) >= top_n:
            break
    return keywords[:top_n]


def match_resume(resume_text, jd_text, top_n=30, extra_skip=None):
    """Return (score 0-100, matched_keywords, missing_keywords)."""
    keywords = extract_keywords(jd_text, top_n=top_n, extra_skip=extra_skip)
    resume_low = (resume_text or "").lower()
    matched, missing = [], []
    for kw in keywords:
        (matched if kw in resume_low else missing).append(kw)
    score = round(100 * len(matched) / len(keywords)) if keywords else 0
    return score, matched, missing


# ------------------------------------------------------------
# Skill-based (semantic-ish) matching — more meaningful than raw word overlap.
# Each canonical skill has aliases; a job's skills = the canonical skills whose
# aliases appear in the JD, and the score = how many of THOSE your resume covers.
# ------------------------------------------------------------
SKILLS = {
    "project management": ("project management", "project manager", "manage projects",
                           "project delivery", "project lifecycle", "project coordination", "pmo", "pmp"),
    "program management": ("program management", "program manager"),
    "agile / scrum": ("agile", "scrum", "kanban", "sprint", "safe"),
    "waterfall": ("waterfall",),
    "stakeholder management": ("stakeholder", "stakeholders", "cross-functional",
                               "cross functional", "relationship management"),
    "risk management": ("risk management", "risk assessment", "risk mitigation", "risk register"),
    "budgeting & cost": ("budget", "budgeting", "cost management", "cost planning",
                         "forecasting", "financial planning", "financial analysis", "p&l"),
    "scheduling": ("scheduling", "timeline", "milestone", "gantt", "ms project",
                   "microsoft project", "primavera", "critical path"),
    "process improvement": ("process improvement", "process optimization", "operational efficiency",
                            "continuous improvement", "lean", "six sigma", "kaizen"),
    "change management": ("change management", "organizational change"),
    "vendor & procurement": ("vendor", "procurement", "supplier", "sourcing", "contract management"),
    "requirements / business analysis": ("requirements gathering", "business analysis",
                                         "business analyst", "brd", "user stories",
                                         "functional specification", "use cases", "gap analysis"),
    "data analysis": ("data analysis", "data analytics", "analytics", "quantitative"),
    "reporting & dashboards": ("reporting", "dashboard", "kpi", "metrics", "visualization"),
    "sql": ("sql", "queries"),
    "python": ("python",),
    "excel": ("excel", "spreadsheet", "pivot table", "vlookup"),
    "power bi": ("power bi", "powerbi"),
    "tableau": ("tableau",),
    "jira": ("jira",),
    "confluence": ("confluence",),
    "asana / trello / wrike": ("asana", "trello", "wrike", "monday.com", "smartsheet", "clickup"),
    "crm / salesforce": ("crm", "salesforce", "hubspot"),
    "erp systems": ("erp", "sap", "netsuite", "workday"),
    "automation": ("automation", "power automate", "workflow automation", "rpa", "zapier"),
    "operations": ("operations", "operational", "logistics", "supply chain"),
    "customer success": ("customer success", "client success", "customer experience",
                         "account management", "onboarding"),
    "quality assurance": ("quality assurance", "quality control", "quality management"),
    "documentation / SOPs": ("documentation", "sop", "standard operating procedure"),
    "leadership": ("leadership", "team lead", "mentoring", "people management", "coaching"),
    "communication": ("communication", "presentation", "presentations"),
    "problem solving": ("problem solving", "problem-solving", "troubleshooting", "analytical thinking"),
    "compliance & audit": ("compliance", "regulatory", "audit", "governance"),
    "product & roadmap": ("product management", "roadmap", "backlog", "prioritization"),
    "go-to-market": ("go-to-market", "gtm", "campaign"),
    "execution & delivery": ("execution", "delivery", "deliverables", "milestones", "on-time", "on time"),
    "prioritization": ("prioritization", "prioritize", "trade-offs", "tradeoffs", "triage"),
    "metrics & kpis": ("kpi", "kpis", "okr", "okrs", "data-driven", "metrics-driven", "key performance"),
    "client-facing": ("client-facing", "client facing", "customer-facing", "liaison", "partner management"),
    "process design": ("process design", "process mapping", "workflow", "operational excellence", "streamline"),
}

_SKILL_RES = {c: re.compile(r"\b(?:%s)\b" % "|".join(re.escape(a) for a in aliases), re.I)
              for c, aliases in SKILLS.items()}


def skills_in(text):
    """Canonical skills whose aliases appear in `text`."""
    text = text or ""
    return {c for c, rx in _SKILL_RES.items() if rx.search(text)}


# Broad skills that show up in almost every JD — down-weighted so matching them
# doesn't inflate the score the way specific skills (SQL, risk management, Jira) do.
SKILL_WEIGHTS = {
    "operations": 0.4, "communication": 0.4, "leadership": 0.4, "problem solving": 0.5,
    "reporting & dashboards": 0.6, "documentation / SOPs": 0.5, "customer success": 0.6,
    "quality assurance": 0.6, "go-to-market": 0.6, "product & roadmap": 0.7,
}

_IDF_PATH = "idf.json"


def _candidate_terms(text):
    """All meaningful unigrams + adjacent bigrams in a text (for IDF / matching)."""
    skip = STOPWORDS | JD_BOILERPLATE
    terms = set()
    for segment in _SEGMENT_RE.split(text or ""):
        raw = [t.strip("-.+#/") for t in _tokens(segment)]
        raw = [t for t in raw if t]
        for t in raw:
            if len(t) > 2 and t not in skip:
                terms.add(t)
        for a, b in zip(raw, raw[1:]):
            if a not in skip and b not in skip and len(a) > 2 and len(b) > 2:
                terms.add("%s %s" % (a, b))
    return terms


def build_idf(texts):
    """Inverse document frequency over a corpus of JDs: common terms get a low
    weight, rare/important terms a high one."""
    texts = list(texts)
    n = len(texts) or 1
    df = Counter()
    for t in texts:
        for term in _candidate_terms(t):
            df[term] += 1
    return {term: round(math.log((n + 1) / (c + 1)) + 1.0, 4) for term, c in df.items()}


# In-process memo for the default idf.json: it's large (tens of thousands of terms) and was
# being re-read + re-parsed on every scoring pass and every detail/tailor open. Cache the
# parsed dict once; _reset_idf_cache() lets a long-lived worker pick up a rebuilt file.
_idf_cache = {"idf": None, "loaded": False}


def _reset_idf_cache():
    # CLOSES an open index rather than just dropping the reference. The mmap would
    # otherwise live until the garbage collector got to it, which on Windows is enough
    # to make build_idf_index's os.replace fail on a file "in use by another process".
    cur = _idf_cache.get("idf")
    if isinstance(cur, _IdfIndex):
        cur.close()
    _idf_cache["idf"] = None
    _idf_cache["loaded"] = False


def save_idf(idf, path=_IDF_PATH):
    # SORTED, and the reason is the one jd_terms already taught this project. idf is built from
    # a dict keyed by str, so its iteration order is randomised per process by PYTHONHASHSEED:
    # two runs over an UNCHANGED corpus wrote two byte-different 27 MB files holding the
    # identical object. idf.json is TRACKED and is in the deploy bundle, so every scoring run
    # left it modified in git and `git add -A` committed a 27 MB no-op diff -- one of which
    # went in today before anyone looked at what had actually changed. Sorting makes "nothing
    # changed" produce no diff, which is the only way a data file in git can be reviewed.
    try:
        json.dump(idf, open(path, "w", encoding="utf-8"), sort_keys=True)
        if path == _IDF_PATH:                 # keep the in-process cache in step with the file
            _idf_cache["idf"] = idf
            _idf_cache["loaded"] = True
    except Exception:
        pass


def load_idf(path=_IDF_PATH, eager=False):
    """The idf table: the mmap'd index below, a plain dict, or None if there is no file.

    `eager=True` forces the dict for a caller that wants one. Nothing in the app does, and
    the lazy map is both faster to open and smaller to hold -- see the note under it.
    """
    # Only the default path is memoized; an explicit path always re-reads.
    if path == _IDF_PATH and _idf_cache["loaded"]:
        cached = _idf_cache["idf"]
        if not (eager and isinstance(cached, _IdfIndex)):
            return cached
    idf = None if eager else _open_idf_index(path)
    if idf is None and os.path.exists(path):
        try:
            idf = json.load(open(path, encoding="utf-8"))
        except Exception:
            idf = None
    if path == _IDF_PATH:
        _idf_cache["idf"] = idf
        _idf_cache["loaded"] = True
    return idf


# --------------------------------------------------------------------------------------
# WHY THERE IS A BINARY SIDECAR NEXT TO idf.json
#
# idf.json is a flat {term: float} map and it is now 1,015,658 terms / 29.4 MB. Every
# consumer only ever asks `idf.get(term, default)` -- there is not one call site in the app
# that iterates it -- and yet `json.load` built the whole 1M-entry dict in every Passenger
# worker, on that worker's FIRST feed render. Measured on the real file:
#
#     json.load                     1,130 ms    +113 MB resident, 174 MB peak
#     open the .idx sidecar            14 ms    ~0 (file-backed, shared via the page cache)
#     verify its stamp                 66 ms    one blake2b pass over the 29.4 MB
#
# THE 113 MB MATTERS MORE THAN THE SECOND. Production workers sit at ~797 MB against a
# ~1.2 GB account cap and stderr.log is a list of "Child process ... killed by signal: 9";
# every kill produces a fresh worker, whose first visitor pays the 1,130 ms again. The dict
# was feeding the cycle that kept making cold workers.
#
# A FASTER FORMAT WAS NOT THE ANSWER: the cost is building a million Python objects, not
# parsing. Measured on the same data, marshal.load is 1,570-1,704 ms -- SLOWER than json --
# and pickle.load is 574 ms. Anything that materialises the dict pays for the dict. So this
# does not materialise it. The sidecar is an open-addressed hash table, mmap'd, and a lookup
# is one probe into it (0.47 extra probes per key at the load factor below).
#
# THE STAMP IS OVER CONTENT, NOT mtime, and a mismatch is REFUSED rather than repaired --
# the two rules web.py's row_cache learned the hard way, because a deploy is a zip extract,
# so every file is rewritten and not a byte changes. A refused sidecar falls straight back
# to json.load, which is exactly today's behaviour: the worst case here is the status quo.
#
# NOTHING ON A REQUEST PATH WRITES IT. build_idf_index() is called by /warm and by scripts,
# never by load_idf() -- the same rule as the row file and for the same reason: the build is
# ~2.7 s, and charging it to whoever loads the feed next is the regression this removes.
# --------------------------------------------------------------------------------------
_IDX_MAGIC = b"IDFIDX02"
_IDX_HDR = struct.Struct("<IIII")      # nslots, nterms, blob bytes, stamp length
_IDX_U32 = struct.Struct("<I")
_IDX_U16 = struct.Struct("<H")
_IDX_F64 = struct.Struct("<d")
_IDX_KEY_MAX = 0xFFFF                  # the key-length field; a longer term cannot be stored
_IDX_MEMO_MAX = 200000                 # ~20 MB; see _IdfIndex._remember
_MISSING = object()


def _idx_path(path):
    """idf.json -> idf.json.idx. Named off the SOURCE file, like jobs_snapshot.json.gz.fp.json,
    so an explicit non-default path gets its own sidecar instead of poisoning the shared one."""
    return path + ".idx"


def _idf_stamp(path):
    """blake2b-128 of the file's bytes + its size. Content, not mtime -- see the note above."""
    h = hashlib.blake2b(digest_size=16)
    size = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
            size += len(chunk)
    return h.digest() + struct.pack("<Q", size)


def _idx_slot(kb):
    """A DETERMINISTIC hash of a key's bytes.

    NOT Python's hash(): for str it is randomised per process by PYTHONHASHSEED, so a table
    built in one worker would read as empty in the next -- silently, and only for some keys.
    That is the same hazard save_idf's `sort_keys=True` note describes one screen up.
    """
    h = zlib.crc32(kb) & 0xFFFFFFFF
    h ^= h >> 15
    h = (h * 0x2545F491) & 0xFFFFFFFF
    return h ^ (h >> 13)


class _IdfIndex(object):
    """A read-only {term: float} map backed by an mmap'd open-addressed hash table.

    Implements the whole Mapping surface the app uses -- get / [] / in / len / bool -- plus
    iteration, which nothing here needs but which a future caller would otherwise get a
    silently EMPTY answer from rather than an error.

    A HASH COLLISION CANNOT RETURN A WRONG VALUE: the key's bytes are stored beside its value
    and compared on every probe. That is what makes a 32-bit hash safe here.

    Looked-up terms are memoised in a plain dict, so the second ask for a term costs 0.27 us
    against the mmap's 5.0 us -- faster, as it happens, than .get on the 1M-entry dict this
    replaces (0.39 us), because the memo stays small and cache-resident. The memo is also why
    a corpus-wide consumer does not need `eager=True`: it converges on the terms that are
    actually asked for instead of paying for a million that are not.
    """
    __slots__ = ("_fh", "_mm", "_n", "_slots", "_mask", "_blob_at", "_end",
                 "_memo", "_memo_get", "stamp")

    def __init__(self, path):
        self._fh = open(path, "rb")
        try:
            self._mm = mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ)
        except Exception:
            self._fh.close()
            raise
        if self._mm[:8] != _IDX_MAGIC:
            self.close()
            raise ValueError("not an idf index")
        nslots, self._n, blob_len, stamp_len = _IDX_HDR.unpack_from(self._mm, 8)
        hdr = 8 + _IDX_HDR.size
        self.stamp = bytes(self._mm[hdr:hdr + stamp_len])
        self._slots = hdr + stamp_len
        self._mask = nslots - 1
        self._blob_at = self._slots + nslots * 4
        # THE LENGTH IS PART OF THE CONTRACT. Without this a TRUNCATED sidecar opened
        # cleanly -- magic intact, header parses, stamp matches -- and every lookup then
        # probed a slot table that stopped early. Python clamps an out-of-range slice, so
        # it did not raise; terms simply went missing. Caught by scripts/test_idf_index.py
        # writing half a file, which is why that check exists.
        if nslots < 1 or (nslots & self._mask) or len(self._mm) != self._blob_at + blob_len:
            self.close()
            raise ValueError("idf index is truncated or damaged")
        self._end = self._blob_at + blob_len - 10        # the last offset an entry can start at
        self._memo = {}
        self._memo_get = self._memo.get

    def get(self, key, default=None):
        v = self._memo_get(key, _MISSING)
        if v is not _MISSING:
            return default if v is None else v
        try:
            kb = key.encode("utf-8")
        except AttributeError:
            return default
        mm, base, mask = self._mm, self._slots, self._mask
        i = _idx_slot(kb) & mask
        while True:
            off = _IDX_U32.unpack_from(mm, base + (i << 2))[0]
            if not off or off < self._blob_at or off > self._end:
                # An empty slot ends the probe chain. An out-of-range one cannot happen
                # after the length check in __init__ and is treated the same way anyway:
                # a damaged table must degrade to "not found", never to a wrong weight.
                self._remember(key, None)
                return default
            kl = _IDX_U16.unpack_from(mm, off)[0]
            if mm[off + 2:off + 2 + kl] == kb:
                v = _IDX_F64.unpack_from(mm, off + 2 + kl)[0]
                self._remember(key, v)
                return v
            i = (i + 1) & mask

    def _remember(self, key, value):
        """Cache a resolved term, BOUNDED.

        Unbounded, a worker that analyses enough descriptions would memoise its way
        back to the 113 MB dict this replaced -- slowly, so it would read as a leak
        rather than as a cache, on the box where a worker at the LVE cap is killed
        with signal 9. Clearing outright rather than evicting one entry: this is a
        pure cache, a miss costs 5 us, and the cap is far above any real working set
        (a JD contributes tens of distinct terms, not thousands), so the branch below
        is not expected to fire at all in a web worker. It is here so that if it ever
        does, the ceiling is ~20 MB and not the whole table.
        """
        if len(self._memo) >= _IDX_MEMO_MAX:
            self._memo.clear()
        self._memo[key] = value

    def __getitem__(self, key):
        v = self.get(key, _MISSING)
        if v is _MISSING:
            raise KeyError(key)
        return v

    def __contains__(self, key):
        return self.get(key, _MISSING) is not _MISSING

    def __len__(self):
        return self._n

    def __bool__(self):
        return self._n > 0

    __nonzero__ = __bool__

    def items(self):
        """A full walk of the blob. Nothing in the app calls this; it exists so that a caller
        who does gets the right answer instead of an empty one."""
        mm, pos, end = self._mm, self._blob_at, len(self._mm)
        while pos < end:
            kl = _IDX_U16.unpack_from(mm, pos)[0]
            yield (mm[pos + 2:pos + 2 + kl].decode("utf-8"),
                   _IDX_F64.unpack_from(mm, pos + 2 + kl)[0])
            pos += 2 + kl + 8

    def keys(self):
        return (k for k, _ in self.items())

    def values(self):
        return (v for _, v in self.items())

    def __iter__(self):
        return self.keys()

    def close(self):
        try:
            self._mm.close()
        except Exception:
            pass
        try:
            self._fh.close()
        except Exception:
            pass


def _open_idf_index(path):
    """`path`'s sidecar, or None if it is absent, unreadable, or stale."""
    idx_path = _idx_path(path)
    if not (os.path.exists(idx_path) and os.path.exists(path)):
        return None
    idx = None
    try:
        idx = _IdfIndex(idx_path)
        if idx.stamp != _idf_stamp(path):        # refused, never repaired
            idx.close()
            return None
        return idx
    except Exception:
        if idx is not None:
            idx.close()
        return None


def build_idf_index(path=_IDF_PATH, force=False):
    """Write `path`'s sidecar. True if it wrote one, False if it was already current.

    CALLED BY /warm AND BY SCRIPTS, NEVER BY A REQUEST -- see the note above _IDX_MAGIC.

    WRITTEN IN ONE STREAMING PASS, and that is a memory decision, not a tidiness one. The
    obvious shape -- accumulate every packed entry in a list, join it, build a list of
    nslots offsets, `struct.pack("<%dI" % nslots, *slots)` -- peaks around 250 MB on top of
    the parsed dict, because a 2M-element Python list of offsets is ~74 MB and the splat
    builds a 2M-element tuple as well. This runs inside a Passenger worker that is already
    near the account's LVE cap; a build that trips the cap would create exactly the cold
    worker this whole change exists to prevent. So the slot table is an array("I") (4 bytes
    an entry, 8 MB) written as raw bytes, and the blob is streamed straight to the file:
    peak is the parsed dict plus ~10 MB, which is less than the json.load it replaces.

    Two-pass over the file rather than in memory: the slot table sits BEFORE the blob, so
    a placeholder goes down first and is overwritten once the offsets are known.

    Written to a temp name and os.replace'd, because two /warm ticks can overlap and a
    reader must never see a half-written table. The replace does not disturb a reader that
    already has the old file mmap'd: the inode outlives the name.
    """
    if not os.path.exists(path):
        return False
    if not force:
        cur = _open_idf_index(path)
        if cur is not None:
            cur.close()
            return False
    try:
        idf = json.load(open(path, encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(idf, dict) or not idf:
        return False
    stamp = _idf_stamp(path)

    # Load factor 0.5. Linear probing degrades sharply past ~0.7, and the slot table costs
    # disk rather than anything scarce -- measured 0.47 extra probes per key, worst chain 37.
    nslots = 1
    while nslots < len(idf) * 2:
        nslots <<= 1
    mask = nslots - 1
    hdr_len = len(_IDX_MAGIC) + _IDX_HDR.size + len(stamp)
    blob_at = hdr_len + nslots * 4

    slots = array.array("I", bytes(nslots * 4))       # 0 == empty, and zeroed is the ground state
    tmp = "%s.%d.tmp" % (_idx_path(path), os.getpid())
    n = 0
    try:
        with open(tmp, "wb") as fh:
            fh.write(_IDX_MAGIC)
            fh.write(_IDX_HDR.pack(nslots, 0, 0, len(stamp)))   # nterms/blob_len patched below
            fh.write(stamp)
            fh.seek(blob_at)                                    # leave the slot table as a hole
            pos = 0
            pack_len, pack_val = _IDX_U16.pack, _IDX_F64.pack
            for term, weight in idf.items():
                kb = term.encode("utf-8")
                if len(kb) > _IDX_KEY_MAX:       # no real term is 64 KB; skip, never truncate
                    continue
                try:
                    packed_val = pack_val(float(weight))
                except (TypeError, ValueError):
                    continue
                i = _idx_slot(kb) & mask
                while slots[i]:
                    i = (i + 1) & mask
                slots[i] = blob_at + pos
                fh.write(pack_len(len(kb)))
                fh.write(kb)
                fh.write(packed_val)
                pos += 2 + len(kb) + 8
                n += 1
            if sys.byteorder == "big":           # the reader is little-endian by contract
                slots.byteswap()
            fh.seek(hdr_len)
            fh.write(slots.tobytes())
            fh.seek(len(_IDX_MAGIC))
            fh.write(_IDX_HDR.pack(nslots, n, pos, len(stamp)))
        os.replace(tmp, _idx_path(path))
    except Exception:
        try:
            os.remove(tmp)                       # never leave the residue the OOMs left behind
        except OSError:
            pass
        return False
    return True


def warm_idf(path=_IDF_PATH):
    """/warm's idf stage: build the sidecar if it is missing or stale, then load through it.

    The build is the expensive half and it happens once per idf.json -- in practice once per
    deploy, since the scrape does not rewrite the file. Every worker after that opens it in
    ~14 ms instead of parsing for ~1,130 ms.

    RETURNS EARLY WHEN THIS PROCESS ALREADY HAS THE INDEX OPEN, and that is not just a
    saving. build_idf_index's "is it current?" test re-hashes all 29.4 MB of idf.json, so
    without this the keep-warm cron re-read the file 1,152 times a day -- on a box where
    disk is the contended resource -- and, worse, the idf stage reported ~36 ms on every
    tick instead of 0. That number is documented above as the cheapest way to see whether
    a worker was cold; a stage that always reports the same thing has stopped being a
    signal. Measured on the box: four warm ticks in a row all said 36-43 ms while
    base_rows said 0, which is what gave it away.

    A worker that holds an open index does not notice idf.json being replaced under it.
    That is exactly what load_idf's memo already did with the parsed dict, /reload still
    calls _reset_idf_cache(), and the file only changes on a deploy, which restarts every
    worker anyway.
    """
    if path == _IDF_PATH and _idf_cache["loaded"] and isinstance(_idf_cache["idf"], _IdfIndex):
        return _idf_cache["idf"]
    if build_idf_index(path):
        _reset_idf_cache()
    return load_idf(path)


_REQ_HEADERS = ("minimum qualifications", "basic qualifications", "preferred qualifications",
                "qualifications", "requirements", "what you'll need", "what we're looking for",
                "who you are", "what you'll do", "responsibilities", "skills you")


def _requirements_text(jd_text):
    """The part of a JD from its first 'requirements/qualifications/responsibilities'
    header onward (where the real must-haves live). '' if none found."""
    low = (jd_text or "").lower()
    hits = [low.find(h) for h in _REQ_HEADERS if low.find(h) != -1]
    return jd_text[min(hits):] if hits else ""


# Hard skills, tools, methods, and certs an ATS literally scans for — weighted highest.
# The idf a term seen in roughly 30 postings of 20,000 earns. Anything rarer is capped
# here unless it is a known hard skill -- see the note in analyze_jd.
_RARE_W_CAP = 7.5

# THE WEIGHT A TERM WE HAVE NEVER SEEN TAKES, in the same idiom as the cap above: the idf a term
# in ~2% of postings earns. What this replaced was `sorted(idf.values())[len // 2]`, described in
# analyze_jd as "the MEDIAN weight, because not having seen a term is evidence it is noise". The
# intent was right and the statistic could not deliver it: 54.4% of idf.json's 849,382 entries
# sit at exactly max(idf) -- every term seen in one posting -- so the median of the DISTINCT
# VALUE LIST lands inside that block and equals the maximum. Measured: median 10.8216, max
# 10.8216. An unseen term was taking the highest weight in the table, which is the precise
# failure the note claimed to have fixed.
#
# A LITERAL, not a percentile, because no percentile of this distribution can be robust to that
# mass point -- even the 10th is 9.03. It is also scale-free: idf is log((n+1)/(c+1)) + 1, so
# "a term in 2% of postings" is log(50) + 1 regardless of how large the corpus grows.
#
# It also deletes an 849,382-element sort from the per-posting path. That sort was inside
# analyze_jd: measured 140.6 ms of a 140.6 ms/row analysis and ~7 MB of allocation churn per
# row, i.e. ~90 minutes of pure sorting over one full pass against a 45-minute CI timeout. The
# score pass dying with rc=137 is the symptom that was showing. Analysis is now 3.8 ms/row.
_UNSEEN_W = 4.91

# TOOLS, METHODS AND CERTS -- the things a posting NAMES rather than describes. Split out from
# the domain half below because "which tools does this employer lean on" is a question the app now
# answers (norms.company_tools), and it cannot be answered by a set that also contains
# "stakeholder", "reporting" and "operations": those are what every employer's boilerplate is
# made of, so they drown the answer. Measured -- restricted to this half, the answer for Northrop
# Grumman is "sap 31% against 4% expected for their role mix, jira 23% against 9%", and for
# Capital One "nosql 43% against 5%". Unrestricted it was "employees 94%" and "capabilities 69%".
#
# ATS_KEYWORDS stays the union, so every existing reader is unaffected; scripts/test_norms.py
# asserts the two halves are disjoint and that the union is unchanged.
ATS_TOOLS = {
    # tools
    "jira", "confluence", "asana", "trello", "smartsheet", "monday.com", "wrike", "clickup",
    "ms project", "microsoft project", "primavera", "sharepoint", "excel", "google sheets",
    "powerpoint", "visio", "lucidchart", "miro", "notion", "sql", "tableau", "power bi",
    "looker", "salesforce", "sap", "oracle", "netsuite", "workday", "servicenow", "python", "git",
    # NAMED BECAUSE THE BOUNDARY TEST BELOW WOULD OTHERWISE LOSE THEM. The substring rule earned
    # these by accident -- `git` matched "github"/"gitlab" (1,146 postings) and `sql` matched
    # "postgresql"/"mysql"/"nosql" (1,146) -- and _term_in will not, because _stem("github") is
    # not _stem("git"). They are real, distinct skills; naming them is explicit and testable.
    "github", "gitlab", "postgresql", "mysql", "nosql",
    # PUNCTUATED NAMES THE TOKENIZER CANNOT PRODUCE AT ALL. extract_keywords strips "-.+#/" and
    # then drops anything under three characters, so "c++" becomes "c" and vanishes; measured,
    # ci/cd appears in 12.0% of stored descriptions, c++ in 10.1% and c# in 5.5% and NONE of
    # them could ever become a keyword. _term_in matches a punctuated term as a phrase, so
    # naming them here fixes it with no tokenizer change and no idf rebuild.
    # "go" is deliberately absent: as a bare token it is ordinary English, not the language.
    "c++", "c#", "ci/cd", ".net",
    # Product-side tooling. Only the ones under _RARE_W_CAP: amplitude (7.92), mixpanel
    # (8.53), pendo (9.05), productboard (8.81) and optimizely (10.37) are all ABOVE it, and
    # wt() applies its x2.5 with no ceiling, so each would outweigh every term the curated
    # set has ever held. They wait for a ceiling on wt(). figma measured 6.378.
    "figma",
    # methods / frameworks
    "agile", "scrum", "kanban", "safe", "lean", "six sigma", "lean six sigma", "waterfall",
    "sdlc", "devops", "kaizen", "pmbok", "prince2", "itil", "okr", "okrs", "kpi", "kpis",
    "gantt", "sprint", "backlog", "retrospective", "scrum master", "product owner",
    # certifications
    "pmp", "capm", "csm", "psm", "cspo", "cbap", "green belt", "black belt",
}

# The domain half, split in two on 2026-09-08. Both are real skills written in the vocabulary
# employer boilerplate also uses; they are separated because they answer different questions
# about a posting, and because the PRODUCT half did not exist at all until now -- measured,
# all 40 core product-management terms were absent from the curated set while `jira`,
# `excel` and `project management` each collected the x2.5 hard-skill boost. In a Product
# Manager description the most role-defining phrases were scored as ordinary prose.
#
# ATS_DOMAIN STAYS THE UNION so every existing reader is unaffected, exactly as
# ATS_KEYWORDS did for the tools/domain split above. scripts/test_norms.py asserts it.
ATS_PROJECT_DOMAIN = {
    # PM / analyst / ops domain
    "project management", "program management", "project manager", "program manager",
    "project coordinator", "stakeholder management", "stakeholder", "risk management",
    "change management", "budget", "budgeting", "cost management", "resource allocation",
    "scope management", "requirements gathering", "business requirements", "user stories",
    "process improvement", "process mapping", "gap analysis", "data analysis", "reporting",
    "dashboards", "forecasting", "vendor management", "procurement", "milestones",
    "deliverables", "cross-functional", "roadmap", "status reporting", "project plan",
    "business analysis", "operations", "implementation", "onboarding", "sla", "metrics",}

# THE PRODUCT HALF. Every entry cleared three independent checks -- sub-word hiding, stem
# collision and sense -- and the ones that did not are named in the commit that added this, with
# the reason each was rejected. The rule is measurement, not completeness: `discovery`,
# `retention`, `pricing`, `segmentation`, `cohort`, `usability`, `personas`, `north star`, `rice`
# and `moscow` are all REFUSED, and the specific form is used instead where one exists
# (product discovery, customer discovery, pricing strategy, usability testing).
ATS_PRODUCT_DOMAIN = {
    # the work itself
    "product management", "product strategy", "product roadmap", "product lifecycle",
    "product backlog", "product launch", "product operations", "product discovery",
    "product analytics", "product marketing",
    # discovery and evidence
    # ADDED 2026-09-08 at the owner's request, both screened first. MEASURED on 2,575 real
    # product descriptions with core._names_term, the same predicate analyze_jd uses:
    #
    #   a/b test                        89 (3.5%)   idf unseen -> _UNSEEN_W 4.91
    #   product requirements document   41 (1.6%)   idf unseen -> _UNSEEN_W 4.91
    #
    # "a/b test" and NOT "a/b testing": _term_in stems, so the singular is a strict SUPERSET
    # (89 against 65) and catches "A/B tests" and "A/B test" as well. Bare "a/b" was measured
    # at 146 (5.7%) and refused -- it is two letters and a slash, and it hides in prose.
    # The slash is fine: _term_in matches a punctuated term as a phrase, the same way c++ and
    # ci/cd already do, and the 89 hits are the proof rather than the hope.
    #
    # "prd" IS NOT HERE and was asked for. Its idf is 7.9462, ABOVE _RARE_W_CAP (7.5), so
    # wt()'s x2.5 -- which has no ceiling -- would make it 19.87 and the heaviest term in any
    # posting that names it, while every non-ATS term clamps at 7.5. That is "rarity is not
    # importance" coming in by the same door amplitude/mixpanel/pendo/productboard/optimizely
    # are already held at. The spelled-out form above carries the same signal at 1.6% coverage
    # and a safe weight; prd becomes available the moment wt() gains a ceiling, and that one
    # change would unlock all six together.
    "a/b test", "product requirements document",
    "user research", "customer discovery", "customer journey", "market research",
    "competitive analysis", "design thinking", "wireframes",
    # measurement and outcomes
    "experimentation", "monetization", "pricing strategy", "churn", "nps", "mvp",
    # launch
    "go-to-market", "gtm",
}

ATS_DOMAIN = ATS_PROJECT_DOMAIN | ATS_PRODUCT_DOMAIN

ATS_KEYWORDS = ATS_TOOLS | ATS_DOMAIN


# A JD this short (chars) or this term-poor after boilerplate stripping is too thin to score
# honestly — e.g. the truncated Adzuna/Oracle blurbs that otherwise yield a handful of generic
# terms a broad résumé fully covers, producing a misleading ~100%. Flagged as "thin" so callers
# show "JD pending" instead of a confident number (see score_pending in web._build_row).
_MIN_JD_CHARS = 400
_MIN_JD_TERMS = 6


# Tags that END A LINE. Everything else is inline and stays joined by a space.
_BLOCK_TAGS = ("p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article",
               "h1", "h2", "h3", "h4", "h5", "h6", "dt", "dd", "blockquote")
# A sentinel rather than "\n", because get_text(strip=True) strips each text node and would
# throw a whitespace-only marker away. \x01 cannot occur in a description; jdrender.JD_CUT
# uses the same character for the same reason.
_CUT = "\x01"
# AND A SECOND ONE, FOR <li> ONLY. The bullet used to be written into the tree as _CUT + "\u2022 ",
# which put the GLYPH BETWEEN TWO SENTINELS for <li><p>text</p></li> -- the commonest ATS list
# shape -- because the inner <p> inserts a sentinel of its own. The collapse below cannot bridge
# that (the glyph is not whitespace), so the bullet became a line of its own and
# jdrender.jd_nodes read it as a paragraph: an empty bullet above its own sentence, on ~4% of
# stored rows. MARKING the <li> instead of DECORATING it means the glyph is emitted once, by the
# collapse, after every sentinel in the run has been consumed. \x02 is as impossible in a
# description as \x01, and both are guaranteed consumed: every occurrence is inside a matched
# run. jdrender.JD_ORPHAN_BULLET repairs the rows written before this.
_LI_CUT = "\x02"
_CUT_RUN = re.compile("(?: *[%s%s] *)+" % (_CUT, _LI_CUT))


def _soup_text(soup):
    r"""Structured plain text from a parsed tree: block boundaries become newlines.

    WHY STRUCTURE IS KEPT. This used to be `re.sub(r"\s{2,}", " ", soup.get_text(" ",
    strip=True))`, which turned every <li>, <p>, <br> and <h2> boundary into a single space and
    returned the whole description as one unbroken line. Everything downstream then had to guess
    the structure back: jdrender's JD_SECTION / JD_ITEM_RE / jd_flat_list and
    resume_brain.analyze._sentences exist for no other reason, core._requirements_text locates
    the requirements by str.find on that one line, and extract_keywords formed bigrams straight
    across list-item boundaries ("...recommendations Lead thoughtful..." yielded
    "recommendations lead").

    NOTHING NEEDED A MIGRATION FOR THIS. A newline in the jd column is an ALREADY SHIPPING
    shape -- measured, 9.4% of the 36,853 stored descriptions carry one and 10.2% carry a
    bullet, because the jobspy path stores markdown and several ATS fields are plain text. That
    is also why jdrender's MD_RULE / MD_ATX / MD_BOLD_LINE are ^$-anchored. jd_nodes is already
    a newline-driven parser; it was never guessing because structure is unknowable, only because
    this function had deleted the one signal it needs.

    Source-formatting whitespace is NOT structure: HTML is indented, so every run of whitespace
    is collapsed FIRST and only the sentinels become newlines.
    """
    for tag in soup(["script", "style"]):
        # Not decomposed before: an inline <style> or <script> inside an ATS description field
        # contributed its CSS or JS text to the stored description. Measured at 0.0% of the
        # corpus today, so this is defence rather than a fix -- but it is one line.
        tag.decompose()
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.insert_before(_LI_CUT if tag.name == "li" else _CUT)
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    # One bullet per RUN, not per sentinel, so nesting cannot multiply it: <li><ul><li>a
    # gave "\u2022\n\u2022 a" before and gives "\u2022 a" now.
    return _CUT_RUN.sub(lambda m: "\n\u2022 " if _LI_CUT in m.group(0) else "\n", text).strip()


def html_to_text(raw):
    """HTML (or already-plain) text -> clean text, with block boundaries kept as newlines.

    Lived in scraper/score_jobs.py as _text until the SWEEP needed it too: several ATS list
    feeds hand back the description alongside the listing, and scraper cannot import score_jobs
    because score_jobs imports scraper. One definition here; both callers delegate to it.

    html.unescape BEFORE the parse is load-bearing and nothing used to pin it: Greenhouse's
    `content` field is HTML-escaped HTML, so a bare parse would leave &lt;p&gt; in the text
    (scraper/__init__.py:2766 says so). scripts/test_html_to_text.py now holds that ordering.
    """
    if not raw:
        return ""
    return _soup_text(BeautifulSoup(html.unescape(raw), "lxml"))


# ---------------------------------------------------------------------------------------------
# READING A STORED DESCRIPTION: the one door
# ---------------------------------------------------------------------------------------------
# WHAT THIS IS FOR. fetch_jd's last resort stores the WHOLE PAGE when no _MAIN_SELECTORS region
# yields 250 characters, and a careers SPA with obfuscated class names and no semantic tags
# defeats every selector in that list -- so the site's own navigation gets stored as a job
# description. Measured over the 41,434 cached descriptions: 2,638 (6.4%) carry site furniture,
# and careers.google.com is 1,052 of its 1,099 rows. 790 of those are exactly 8,000 characters,
# which is fetch_jd's own `limit` truncating mid-navigation.
#
# NOTHING DOWNSTREAM COULD SEE IT, and that is the actual defect. The quality gate was
# ONE-SIDED: score_jobs.MIN_PAGE_JD_CHARS is a FLOOR, and this failure is a shell that is too
# LONG. _is_thin_jd is `0 < len < 400`, so an 8,000-char nav capture is never thin and the retry
# ledger never probes it; _accept_jd wants `len >= 3 * old`, so a correct 3,400-char description
# could never replace it. The verdict this returns is the ceiling that was missing.
#
# CLEANED ON READ, NOT ON WRITE. The stored column stays the archive, so this reaches all 41,434
# rows the moment it deploys -- no migration, no re-fetch -- and a better rule tomorrow reaches
# them again for free.

# WHAT COUNTS AS FURNITURE. Three kinds, none of them host-specific: icon ligature names, the
# labels on a careers site's own chrome, and the shape of a results list.
_CHROME_PARTS = (
    # MATERIAL ICONS LIGATURE NAMES. A careers SPA renders its icons as
    # <i class="material-icons">work_outline</i>, so the icon's NAME is real text and lands in
    # get_text() output. Nobody writes these words in prose, which makes them the cleanest
    # possible signal that we captured a page instead of a posting.
    r"work_outline|expand_more|expand_less|arrow_back|arrow_forward|corporate_fare|"
    r"info_outline|navigate_next|navigate_before|person_outline|noogler_hat|handyman|"
    r"bar_chart|keyboard_arrow_down|open_in_new|more_vert",
    # A RESULTS PAGE captured around the job, because the stored URL rendered a search.
    r"jobs? matched|jobs? search results|go to next page|back to (?:jobs? )?search|"
    r"return to search results",
    # THE SITE'S OWN MENU AND BUTTONS -- what sits between "Skip to main content" and the posting
    # on a careers site whose nav carries no icons: a run of menu labels with no sentence in it.
    # Without them the Amazon rows kept 300 characters of "Home Teams Locations Job categories My
    # profile ..." at the top of every description.
    r"skip to (?:main )?content|share this job|save this job|print this job|"
    r"email a friend|view all jobs|job categories|job alerts|"
    r"my (?:applications|profile|career)|sign out|"
    r"cookies? (?:polic(?:y|ies)|settings?|preferences?)|accept all cookies|we use cookies|"
    r"enable javascript|javascript is (?:disabled|required)",
)
_CHROME_RX = re.compile("|".join(_CHROME_PARTS), re.I)

# THE PREFILTER IS NOT AN OPTIMISATION DETAIL, it is what makes this affordable in the scorer.
# _CHROME_RX has to be scanned across the whole description and costs 2.9 ms/row -- 119 s over
# the corpus, on top of analyze_jd's own 3.8 ms/row. str.__contains__ is a tuned C substring
# search and a regex alternation is not, so a plain literal loop runs first and clears most rows.
# It reads only the HEAD, because furniture is a prefix and that is the only place a cut can
# start; the full scan that follows a hit still covers the whole text.
#
# EVERY LITERAL MUST BE REACHABLE FROM ONE OF THE _CHROME_PARTS ALTERNATIVES, and every
# alternative must have a literal: a pattern with no literal is silently disabled, a literal with
# no pattern silently costs a full scan. test_clean_jd.py checks both directions.
#
# THEY ARE ALL RARE ON PURPOSE. An earlier list used "rows", "showing", "matched", "profile" and
# "cookie", which are ordinary English: 30% of the corpus passed the prefilter and paid for the
# full scan anyway. Every literal here is a phrase a careers site writes and a job description
# does not.
_CHROME_LITERALS = (
    "_outline", "expand_", "arrow_", "corporate_fare", "navigate_", "noogler_hat", "handyman",
    "bar_chart", "keyboard_arrow", "open_in_new", "more_vert",
    "jobs matched", "job matched", "search results", "go to next page", "back to search",
    "back to job", "return to search",
    "skip to main", "skip to content", "share this job", "save this job", "print this job",
    "email a friend", "view all jobs", "job categor", "job alert",
    "my profile", "my application", "my career",
    # Both numbers of "cookie", because the pattern allows both and a literal that covers only
    # the singular silently disables the plural half of it -- which is what test_clean_jd.py's
    # correspondence check found the first time it ran.
    "sign out", "all cookies", "use cookies", "cookie polic", "cookies polic",
    "cookie setting", "cookies setting", "cookie preference", "cookies preference",
    "enable javascript", "javascript is",
)

# A PAGE THAT SAYS THERE IS NO POSTING. Read separately from furniture because it is a different
# answer: furniture means "the description is in here somewhere", this means "there is nothing to
# find". 1,090 rows in the cache say one of these and every one of them is over _MIN_JD_CHARS, so
# the thin machinery never saw them -- Actalent 713 (already known unrecoverable) and BrassRing
# 364, whose "shell" is 6,209 characters of cookie policy wrapped around a dead link.
_DEAD_SHELL_RX = re.compile(
    r"we(?:'|’)?re sorry, this link|sorry to interrupt|your session has expired|"
    r"no longer (?:active|available|accepting)|"
    r"this (?:job|position|posting) (?:is|has been) (?:closed|filled|expired)|"
    r"page not found|404 error", re.I)
_DEAD_SHELL_HEAD = 1500

# THE SHAPE OF A RESULTS LIST, which carries no furniture words of its own. Google's captured
# listing runs 1,400 characters PAST its last icon name -- rows of "Title City, ST, USA ; +2
# more" -- so a cut that stopped at the last icon left the listing in and a cut that jumped to
# the next heading took real prose out. COUNTED rather than matched: a genuine posting can name
# one office this way, and measured over 4,000 clean descriptions 0.8% contain the pattern once
# and NONE contain it three times. Below the threshold it is a sentence; at or above it is a list.
# Gated on ", USA" because that is what the corpus is; a non-US listing would not be caught, and
# there is no non-US corpus to measure one against.
_LISTING_RX = re.compile(r"\+\d+ more\b|\b[A-Z][a-z]+, [A-Z]{2}, USA\b")
_LISTING_MIN = 3

# WHERE THE POSTING ITSELF BEGINS. Deliberately NARROWER than jdrender.JD_HEAD, which answers a
# different question ("is this line a heading, anywhere in the body"). This one is only ever used
# to TIDY a cut the furniture run already decided, never to choose one -- see _strip_chrome.
_POSTING_START = re.compile(
    "(?:\\A|(?<=[\\s•.;:!?]))("
    "about (?:the|this|our) (?:job|role|position|opportunity|team)|"
    "job (?:summary|description|details|overview)|description|"
    "position (?:summary|overview|description|purpose)|role (?:summary|overview)|"
    "the (?:role|opportunity|position)|your (?:role|impact)|"
    "(?:minimum|basic|preferred|required|key|core|general) qualifications|qualifications|"
    "(?:key |primary |essential |core )?(?:job )?responsibilities|duties and responsibilities|"
    "essential (?:functions|duties)|day in the life|"
    "what you(?:'|’)?ll (?:do|need|bring)|what you will do|what you bring|"
    "what we(?:'|’)?re looking for|who you are|"
    "requirements|overview"
    ")\\b", re.I)

# How far into the text furniture can still be a PREFIX. Proportional as well as absolute,
# because a captured results list grows with the page it came from.
_CHROME_HEAD_CHARS = 3000
# How far apart two furniture matches can be and still belong to the SAME run. This is the one
# number that had to be measured rather than chosen, because both directions cost something real.
# Over the 2,496 rows a cut touches: at 200 only 0.7% lose posting text but 39.7% still OPEN on
# furniture; at 1,200 nothing opens on furniture but 13.8% lose posting text. 500 is the knee.
# What the losers lose is almost entirely Google's benefits block, which sits ABOVE the
# qualifications on its scraped page -- so the trade is a benefits list for a readable posting.
_CHROME_RUN_GAP = 500
# How far past the end of the run to look for a heading to start on. SMALL ON PURPOSE. Reaching
# further was the first version of this rule and it was wrong in the expensive direction:
# Amazon's nav is one marker at character 98 and its next heading is "Key job responsibilities"
# at 1,336, so jumping to the heading discarded the entire role summary -- measured, that threw
# away posting text on 37% of the rows it touched.
_SNAP_CHARS = 400
# When no heading is in reach, how far to look for the end of the sentence the cut landed inside.
_SENTENCE_END = re.compile("[.!?\\u2022\\n] +")
_SENTENCE_LOOK = 200
# FURNITURE LEFT WHERE THE POSTING SHOULD START. Judged on the OPENING of the body, not on the
# whole of it, and that distinction is the whole rule: a recovered description routinely ends
# with "share this job" and a Google page ends with three more icon names, so counting residue
# document-wide called 1,015 successfully-recovered rows unreadable -- the /job page then
# refused to render a description whose qualifications the block above it had just listed.
# The question this answers is only ever "does what is left OPEN as a job".
_CHROME_LEAD_WINDOW = 400


# MARKDOWN ESCAPES, AND THE READER HAS BEEN SEEING PAST THEM ALL ALONG. jdrender.strip_md
# removes these before anything is drawn, so a description that reads
#
#     5+ years of Project or Program Management experience in enterprise IT environments.
#
# on the job page is stored as "5\+ years ..." -- and _EXP_YEARS_RE needs the plus or the space
# immediately after the digit, so it matched NOTHING and the posting reported no requirement at
# all. That is the shape of the Applied Materials report: the SAME posting is stored twice, from
# its Workday host as plain text (reads 5) and from the employer's own host as markdown (read
# None), which is what made it look like the parser could not read a plain English sentence.
#
# Measured over the 42,419 stored descriptions: 2,327 (5.5%) carry a backslash escape and
# 820 (1.9% of the whole corpus) GAIN a year floor once it is removed. None loses one.
#
# RESTRICTED TO PUNCTUATION, the same restriction jdrender documents: a backslash before a
# letter or digit is not an escape, it is a Windows path or a regex (\S, \D, \b appear in 24 of
# the cached descriptions) and must survive untouched.
#
# TWO DEFINITIONS OF ONE RULE, and that is not an accident. jdrender imports the standard
# library and nothing else on purpose -- the scraper and the digest import core and render
# nothing, so presentation must not be in their import cost -- which leaves core unable to
# borrow jdrender.MD_ESCAPE. test_clean_jd.py asserts the two patterns agree, which is the
# guard that keeps them from drifting the way scripts/measure_jd_reading.py's private copy of
# the ATS matcher once did.
_MD_ESCAPE = re.compile(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\]^_`{|}~\\])")


def _furniture_spans(text):
    """Sorted [(start, end)] of every piece of site furniture in `text`.

    Computed ONCE and reused for both the cut and the residue check -- rescanning the body was
    a second full pass over the description for an answer already in hand.
    """
    spans = []
    head = text[:max(_CHROME_HEAD_CHARS, len(text) // 2)].lower()
    for lit in _CHROME_LITERALS:
        if lit in head:
            spans = [(m.start(), m.end()) for m in _CHROME_RX.finditer(text.lower())]
            break
    if ", USA" in text:
        listing = [(m.start(), m.end()) for m in _LISTING_RX.finditer(text)]
        if len(listing) >= _LISTING_MIN:
            spans += listing
    spans.sort()
    return spans


def _strip_chrome(text, spans):
    """(body, cut) -- site navigation removed from the front, or the text unchanged and cut 0.

    THE FURNITURE DECIDES THE CUT AND A HEADING ONLY TIDIES IT. The posting begins where the
    page's own chrome stops, so the cut point is the end of the first contiguous RUN of
    furniture -- matches no more than _CHROME_RUN_GAP apart, which keeps a menu and the results
    list under it together while leaving a footer at the far end of the document alone. Then,
    and only if a section heading starts within _SNAP_CHARS of that point, the cut moves forward
    to the heading so the description opens on a title rather than mid-clause.
    """
    head = max(_CHROME_HEAD_CHARS, len(text) // 2)
    lead = [s for s in spans if s[0] < head]
    if not lead:
        return text, 0
    cut = lead[0][1]
    for start, end in lead[1:]:
        if start - cut > _CHROME_RUN_GAP:
            break
        cut = max(cut, end)
    snap = _POSTING_START.search(text, cut, cut + _SNAP_CHARS)
    if snap is not None:
        cut = snap.start()
    else:
        # NO HEADING TIDIED THIS CUT, so make sure it at least lands on a boundary. A menu label
        # can occur inside a real sentence -- "you'll be invited to create a profile, which will
        # let you see your application status" -- and cutting at the label opened 73 descriptions
        # mid-clause. Advancing to the end of the sentence is bounded and never loses a section.
        edge = _SENTENCE_END.search(text, cut, cut + _SENTENCE_LOOK)
        if edge is not None:
            cut = edge.end()
    if cut <= 0 or len(text) - cut < _MIN_JD_CHARS:   # nothing recognisable survived; keep it all
        return text, 0
    return text[cut:].strip(), cut


def clean_jd(text):
    """(cleaned, verdict) -- a stored description, read the one way everything reads it.

    verdict is "ok"; "chrome-stripped" when site navigation was removed and a posting was left
    behind; or "not-a-posting" when what we are holding is a page rather than a job.

    LENGTH IS NOT JUDGED HERE. "Too short to score" is _MIN_JD_CHARS and it already has its own
    machinery -- score_jobs._is_thin_jd, the per-host retry ledger, refetch_thin_jds. Conflating
    the two would send a genuinely short description down the junk path and lose its repair route.
    """
    t = (text or "").strip()
    if not t:
        return "", "not-a-posting"
    # FIRST, so that every rule below and every consumer downstream reads the same characters
    # the reader sees. Cheap: `in` on a 6 KB string, and only 5.5% of the corpus pays the sub.
    if "\\" in t:
        t = _MD_ESCAPE.sub(r"\1", t)
    spans = _furniture_spans(t)
    body, cut = _strip_chrome(t, spans) if spans else (t, 0)
    if _DEAD_SHELL_RX.search(body[:_DEAD_SHELL_HEAD]):
        return body, "not-a-posting"
    if any(cut <= s[0] < cut + _CHROME_LEAD_WINDOW for s in spans):
        return body, "not-a-posting"   # the cut did not clear the furniture; still a page
    return body, ("chrome-stripped" if cut else "ok")


# A PLACE IS NOT A SKILL. Pay-transparency notices enumerate the states and cities a range
# applies in ("...in Colorado, Hawaii, Maine, Minnesota, Vermont and the District of Columbia"),
# and those sit in the BODY of the description rather than in the EEO paragraph -- so the
# "in the notice and nowhere else" rule in display_terms never touched them. Rendering a real
# Accenture Federal posting offered "maine", "cleveland", "vermont", "hawaii", "minnesota",
# "district" and "columbia" as keywords worth adding to a résumé.
#
# Measured over 2,500 stored descriptions: geography reaches core_terms on 13.2% of postings,
# carrying a median 3% of the scored weight and up to 42%. So it is not merely ugly on the
# panel, it moves the number -- which is why this is an extraction skip rather than another
# entry in the display filter.
#
# TOKENS, not full names, because extract_keywords skips a bigram when EITHER word is skipped:
# that is what stops "san francisco", "los angeles" and "angeles county" as well as the bare
# state. Deliberately excludes ambiguous ones -- no "phoenix" (the company), no "jordan",
# no "mobile" -- and keeps the list to geography that is never a qualification.
PLACE_TERMS = set("""
alabama alaska arizona arkansas california colorado connecticut delaware florida georgia
hawaii idaho illinois indiana iowa kansas kentucky louisiana maine maryland massachusetts
michigan minnesota mississippi missouri montana nebraska nevada hampshire jersey carolina
dakota ohio oklahoma oregon pennsylvania rhode tennessee texas utah vermont virginia
washington wisconsin wyoming york columbia district county counties
angeles francisco diego jose antonio cleveland chicago boston denver austin seattle portland
atlanta dallas houston philadelphia detroit charlotte nashville baltimore milwaukee sacramento
""".split())


# ACRONYMS WHOSE LOWERCASE FORM IS AN ORDINARY ENGLISH WORD, matched against the ORIGINAL case.
#
# A word boundary is necessary and not sufficient. `safe` is in ATS_KEYWORDS as SAFe, the Scaled
# Agile Framework, and once the boundary fix stopped it matching "safety" it still matched the
# adjective: measured over 6,000 stored descriptions, the word "safe" appears in 13.8% of them
# and only 15.5% of THOSE are the framework -- so 11.7% of the whole corpus was being credited
# with a hard skill it never named, at x2.5 and x1.6 again. "SAFe"/"SAFE" spelled exactly is
# 2.0%, and that is the real signal. Same shape for Lean: bare "lean" 5.3%, capitalised 3.5%,
# and the unambiguous forms (lean six sigma, kaizen, value stream) are separate keywords already.
#
# Case is the discriminator a lowercased pipeline threw away, so these two are asked of the
# original text. Keep this set SMALL -- it is for genuine collisions, not for tidiness.
_ATS_CASED = {
    "safe": re.compile(r"(?<![A-Za-z0-9])(?:SAFe|SAFE)(?![A-Za-z0-9])"),
    "lean": re.compile(r"(?<![A-Za-z0-9])Lean(?![A-Za-z0-9])"),
}


def _names_term(t, text, text_low, words):
    """Does this text NAME this term? Case-sensitive for the acronyms above, else _term_in."""
    cased = _ATS_CASED.get(t)
    if cased is not None:
        return bool(cased.search(text or ""))
    return _term_in(t, text_low, words, phrase_exact=True)


def analyze_jd(jd_text, idf=None):
    """The résumé-INDEPENDENT half of the ATS match: the JD's important keywords and each
    one's weight. Depends only on the JD text, idf, the ATS keyword set, and the JD's
    requirements section — NOT the résumé — so it can be computed once per job and reused
    for every résumé and every page render.

    Returns {"terms": [...], "weight": {term: w}, "total": float, "thin": bool, "verdict": str}.

    READ THROUGH clean_jd, which is why the verdict comes back with the terms. A description
    that is really a careers-site page scores as THIN rather than as a job -- "we could not read
    this" is the answer the whole pipeline already knows how to carry (score withheld, "JD
    pending" on the card, _row_pending in the feed), and the alternative was a new column
    carrying the same fact to the same places.
    """
    jd_text, verdict = clean_jd(jd_text)
    jd_low = jd_text.lower()
    # ORIGINAL CASE KEPT for both, because _names_term needs it -- see _ATS_CASED.
    req_text = _requirements_text(jd_text)
    req_low = req_text.lower()
    # Built once per posting, and judged by the SAME rule the résumé side is judged by. See
    # _term_in for why this is not _resume_wordset (its memo holds 8 entries, for one résumé).
    jd_words = _wordset(jd_low)
    req_words = _wordset(req_low) if req_low else (frozenset(), frozenset())

    # The JD's important keywords: its salient terms + any hard ATS keywords it names.
    # extra_skip rather than JD_BOILERPLATE: _candidate_terms shares that set and builds
    # idf.json from it, so adding geography there would re-weight the whole vocabulary as
    # a side effect of a display problem. This keeps idf comparable across the change.
    salient = extract_keywords(jd_text, top_n=30, idf=idf, extra_skip=PLACE_TERMS)
    # "thin" is judged on the JD's own substance (length + salient-term count), NOT on the ATS
    # keywords a broad résumé would trivially match — so a truncated blurb stays flagged.
    thin = (len(jd_low.strip()) < _MIN_JD_CHARS or len(salient) < _MIN_JD_TERMS
            or verdict == "not-a-posting")
    jd_terms = set(salient)
    # AS WORDS, NOT AS SUBSTRINGS. `{kw for kw in ATS_KEYWORDS if kw in jd_low}` -- what this
    # replaced -- invented a hard skill in 89% of postings: `visio` out of "division" and
    # "supervision" (59.5% of the corpus), `excel` out of "excellence" (37.1%), `sla` out of
    # "translate" (24.7%), `git` out of "digital" (24.0%), `safe` out of "safety" (20.6%),
    # `lean` out of "cleaning" (9.0%). Each phantom then took x2.5 for being a hard skill and
    # x1.6 again below, so it outweighed the terms the job actually named and landed inside
    # core_terms -- it moved the MATCH PERCENTAGE, not just the chip list. A median 17% of the
    # scored weight was noise. scripts/measure_jd_reading.py keeps its own copy of the old rule
    # so that number stays measurable now this one is correct.
    #
    # _term_in rather than a word-boundary regex, because a boundary alone is too strict in the
    # other direction: the substring rule was legitimately earning `stakeholders` (7,165
    # postings), `budgets`/`budgeting` (2,238), `roadmaps` (1,126), `kpis` (968) and
    # `implementations` (720), and \b on both sides throws all of those away. Stems keep them
    # and still refuse the phantoms -- _stem("division") is not _stem("visio").
    jd_terms |= {kw for kw in ATS_KEYWORDS
                 if _names_term(kw, jd_text, jd_low, jd_words)}
    if not jd_terms:
        return {"terms": [], "weight": {}, "total": 0.0, "thin": True, "verdict": verdict}

    # RARITY IS NOT IMPORTANCE, and treating it as such is why "caterpillar inc" outranked
    # "pmp" in the terms a job was scored on. idf gives a term seen in ONE posting ~10.2 and one
    # seen in a thousand ~4.0, and an UNKNOWN term used to take max(idf) -- the highest weight in
    # the whole table -- so a company name or a one-off turn of phrase dominated the core set.
    # Measured: 63% of the distinct terms being screened on appeared in exactly one posting, and
    # no resume will ever contain them.
    #
    # Two corrections. An unknown term now takes the MEDIAN weight, because not having seen a
    # term is evidence it is noise rather than evidence it is critical. And idf is capped for
    # anything that is not a known hard skill, so a genuine specialism in ATS_KEYWORDS keeps its
    # edge while boilerplate cannot buy one by being unusual.

    def wt(t):
        w = idf.get(t, _UNSEEN_W) if idf else 1.0
        if t in ATS_KEYWORDS:        # hard skill / tool / cert, what an ATS weights most
            w *= 2.5
        else:
            w = min(w, _RARE_W_CAP)
        # THE SAME SUBSTRING BUG, applied to EVERY term rather than only to ATS keywords, and
        # compounding on top of the x2.5 above: `t in req_low` gave "sla" the requirements boost
        # for a section that said "translate". Judged as a word now, by the same rule.
        if req_low and _names_term(t, req_text, req_low, req_words):
            w *= 1.6
        return w

    # SORTED, NOT list(). jd_terms is a set of STRINGS, so its iteration order is randomised
    # per process by PYTHONHASHSEED -- three runs over one description gave three different
    # pack_analyzed strings. That silently defeated the reason this column is TEXT and not jsonb
    # (see db.JOBS_DERIVED_SQL): score_jobs diffs the stored string against the one it just built
    # to decide whether to write, so a reordered rebuild never matched and the FULL pass
    # re-upserted the WHOLE corpus every day -- ~11 MB of jd_terms, on a host whose metered
    # egress has been overrun once. _persist_derived's "one small upsert instead of re-writing
    # the whole corpus" has never held for this column.
    #
    # Order only ever reached tie-breaks: core_terms and score_against both sort on -weight and
    # Python's sort is stable, so equal-weight terms kept insertion order. Sorting replaces one
    # arbitrary order with a repeatable one -- it removes nondeterminism rather than adding
    # change, and it is what makes a before/after measurement of this pipeline possible at all.
    terms = sorted(jd_terms)
    weight = {t: wt(t) for t in terms}
    total = sum(weight[t] for t in terms)
    return {"terms": terms, "weight": weight, "total": total, "thin": thin,
            "verdict": verdict}


# ---- matching the way a screening system does, not the way strcmp does -------------------
#
# WHAT WAS WRONG. Terms were compared as literal whole words, so `kpi` and `kpis` were two
# different skills (488 and 402 postings respectively in the live corpus), `budgeting` in a
# posting missed `budget` on a résumé, and "Project Manager" did not answer a JD asking for
# "project management". None of that is a qualification gap; it is a spelling gap, and no real
# applicant-tracking system screens that way.

# Suffixes stripped to reach a comparable stem, longest first so "-ations" beats "-s".
_SUFFIXES = ("ations", "ation", "ments", "ment", "ings", "ing", "ies", "ers", "er",
             "ors", "or", "ed", "es", "s")
# Words that must never be stemmed: short, or the stem collides with something unrelated.
_NO_STEM = {"sas", "aws", "ios", "cms", "ops", "sales", "less", "gas", "bus", "analysis",
            "business", "process", "access", "class", "series", "status", "campus",
            # Looker, the BI tool. Its stem is "look", so it matched "we are LOOKING for" --
            # which is in almost every posting, and it showed up on 99.9% of one employer's
            # openings as a tool they supposedly use. A sweep of every single-word ATS keyword
            # against the corpus vocabulary found this to be the ONLY wrong collision: the other
            # eleven (stakeholders, budgets, implementing, forecasts, roadmaps, kpis...) are
            # exactly what the stemmer is for.
            "looker"}


# maxsize is the headline number in this file. _stem was measured at 3.5 MILLION calls and
# 16.7s of a single 35s ranked_rows rebuild -- 41 million str.endswith calls -- to answer a
# question about a FIXED vocabulary of roughly 44k JD terms plus one resume. It is a pure
# string -> string function, so the memo is exact, and 65536 comfortably holds that vocabulary.
# This is the hottest function in the app by an order of magnitude; if it is ever changed to
# depend on anything but its argument, this decorator has to come off with it.
@lru_cache(maxsize=65536)
def _stem(word):
    """A conservative stem for matching. Deliberately NOT a full Porter stemmer: this only has
    to make morphological variants of the same skill compare equal, and every extra rule is
    another chance to collide two skills that are genuinely different.

    "kpis"->"kpi", "budgeting"->"budget", "management"/"managing"/"manager"->"manag",
    "analytics"->"analytic". A stem shorter than four characters is rejected and the original
    kept, which is what stops "ops"->"op" and similar.
    """
    w = (word or "").lower()
    if len(w) < 4 or w in _NO_STEM:
        return w
    for suf in _SUFFIXES:
        if not w.endswith(suf):
            continue
        # A plural may leave three characters ("kpis" -> "kpi"); a heavier suffix must leave
        # four, or "ration" would stem to "rat".
        floor = 3 if suf in ("s", "es") else 4
        if len(w) - len(suf) < floor:
            continue
        stem = w[:-len(suf)]
        if suf == "ies":
            stem += "y"
        # "planning" -> "plann" -> "plan": undo the doubled consonant English adds.
        elif suf in ("ing", "ings", "ed") and len(stem) > 4 and stem[-1] == stem[-2]                 and stem[-1] not in "aeiou":
            stem = stem[:-1]
        w = stem
        break
    # A TRAILING 'e' GOES LAST, AND UNCONDITIONALLY, because that is what unifies the family:
    # "management" strips to "manage" but "manager" strips to "manag", and without this they
    # stay two different skills — which is the exact bug being fixed. Applied to unstemmed
    # words too, so "deliverable" and "deliverables" also land on the same stem.
    if len(w) >= 5 and w.endswith("e"):
        w = w[:-1]
    return w


# Skills that are the same thing under two names. An ATS carries a synonym ring per skill; this
# is the short version, covering what actually appears in this corpus. Both sides are stemmed
# after mapping, so only the canonical form needs listing.
SKILL_ALIASES = {
    "js": "javascript", "ts": "typescript", "py": "python", "k8s": "kubernetes",
    "ms project": "microsoft project", "msproject": "microsoft project",
    "powerbi": "power bi", "ms excel": "excel", "microsoft excel": "excel",
    "ms office": "microsoft office", "gsheets": "google sheets",
    "pm": "project management", "project mgmt": "project management",
    "prog management": "program management", "sdlc": "software development lifecycle",
    "ci/cd": "cicd", "ci cd": "cicd", "postgres": "postgresql", "ms sql": "sql server",
    "gcp": "google cloud", "aws cloud": "aws", "rpa": "robotic process automation",
    "ba": "business analysis", "qa": "quality assurance", "ux": "user experience",
    "kanban board": "kanban", "agile methodology": "agile", "scrum master": "scrum",
}


# canonical skill -> every alias that names it. A JD asking for "microsoft project" has to be
# answered by a resume that wrote "MS Project", which the forward map alone cannot do.
_ALIAS_REVERSE = {}
for _a, _c in SKILL_ALIASES.items():
    _ALIAS_REVERSE.setdefault(_c, []).append(_a)


def _canon_phrase(term):
    """Alias -> canonical skill, unstemmed. The stemming happens per word at comparison time,
    so this stays readable and can be used for display."""
    return SKILL_ALIASES.get(term, term)


# 65536, not 16384: measured on the real corpus there are 60,242 distinct JD terms, so the
# smaller bound sat permanently full and evicted entries it was about to need again.
@lru_cache(maxsize=65536)
def _alias_forms(term):
    """Every spelling of a skill: the term, its canonical form, and every alias of that.

    A TUPLE, not a list, for the same reason visa_tags returns one: the value is memoized and
    handed to every row, so a mutable return would be an aliasing bug waiting to happen. Both
    call sites only iterate it.

    Memoized because _term_present calls it up to twice per JD term per row: 2.0 MILLION calls
    to rebuild one user's scores, each allocating a fresh list to describe a fixed vocabulary.
    """
    canon = _canon_phrase(term)
    return (term, canon) + tuple(_ALIAS_REVERSE.get(canon, ()))


def _wordset(text_low):
    """(whole word-tokens, their stems) for any lowercased text.

    Unmemoised, so analyze_jd can build one per posting. _resume_wordset below is the memoised
    view of this for the résumé, which is fixed for a whole scoring pass.
    """
    toks = frozenset(WORD_RE.findall(text_low))
    return toks, frozenset(_stem(w) for w in toks)


@lru_cache(maxsize=8)
def _resume_wordset(resume_low):
    """(whole word-tokens, their stems) for a lowercased résumé, memoized so user_scores can
    reuse it across every job in its loop instead of re-tokenizing per job.

    Returns a pair so the exact-match path stays exact — a stem is a fallback, not a
    replacement, and checking the literal token first keeps the common case free.
    """
    return _wordset(resume_low)


# THE RESUME IS FIXED FOR A WHOLE SCORING PASS, so this answers the same question over and over.
# Measured over the live corpus: 3.2 MILLION calls with only ~50k distinct answers, a 96.8% hit
# rate, and 76% of the pass spent in here. Memoising it took the pass from 211 to 54.8 us/row
# with scores identical on all 21,494 analysed rows.
#
# 65536 for the same reason _alias_forms uses it: there are ~50,752 distinct JD terms in this
# corpus, so a smaller bound sits full and evicts WITHIN a single pass, which is the one place
# the memo has to hold. Both arguments after `t` are safely hashable and cheap to hash --
# _resume_wordset is itself lru_cached, so it hands back the same (frozenset, frozenset) object
# every time rather than an equal-but-new one.
@lru_cache(maxsize=65536)
def _term_present(t, resume_low, words):
    """Whether a JD term is answered by the resume, the way a screening system would judge it.

    Three passes, most exact first:
      1. the literal term, whole-word (so "data" still does not match "database");
      2. its canonical form, if it is a known alias ("ms project" -> "microsoft project");
      3. stems, so "budgeting" is answered by "budget" and "project management" by a resume
         that says "managed projects".

    Stemming is a FALLBACK, never a replacement: an exact hit short-circuits, so the common
    case costs what it always did, and nothing here can loosen a comparison the literal test
    already settled.

    `words` is the (tokens, stems) pair from _resume_wordset.

    The judgement itself lives in _term_in so the JOB DESCRIPTION side can reuse it -- see
    analyze_jd. This wrapper is only the memo, and the memo is keyed on the second argument:
    fixed for a whole pass here, and 36,853 distinct descriptions there.
    """
    return _term_in(t, resume_low, words)


def _term_in(t, text_low, words, phrase_exact=False):
    """_term_present without the memo: is this term present in this text, ATS-style?

    Split out for analyze_jd, which asks the same question of the DESCRIPTION rather than of a
    résumé. Calling _term_present there would key its 65,536-entry cache on whole descriptions
    and pin them in memory; _resume_wordset is maxsize=8 for the same reason. Having one
    definition also means the two sides of the match cannot drift: a term the JD is judged to
    "name" is judged present by exactly the rule that later decides whether you hold it.
    """
    toks, stems = words if isinstance(words, tuple) else (words, frozenset())
    if " " in t or any(ch in t for ch in "+#./-"):
        if t in text_low:
            return True
        # AN ALIAS IS JUDGED THE WAY THE SINGLE-WORD BRANCH BELOW JUDGES ONE, and until
        # 2026-09-08 it was not: this was a bare `f in text_low`, a substring test with no
        # boundary, over an alias table that holds TWO-CHARACTER entries. Exactly two
        # ATS_KEYWORDS members are phrases with such an alias, and both are the heaviest
        # terms in a delivery posting:
        #
        #   business analysis   alias "ba"  fired on 99.7% of product-role postings
        #                                   against a 6.7% literal presence
        #                                   (based, Bachelor, backlog, feedback, global)
        #   project management  alias "pm"  fired on 82.9% against 12.6%
        #                                   (development, equipment, jpmorganchase)
        #
        # BOTH SIDES WERE POISONED. _term_present routes through here too, so a resume
        # reading "Based in Boston. Bachelor of Science. Led development" answered YES to
        # both -- a phantom matched against a phantom, each collecting the x2.5 hard-skill
        # weight. That is why "business analysis" was a core term in 118 of 120 real PM
        # descriptions and the heaviest scored term on 398 of 700 of them.
        #
        # It is the SAME bug class the 2026-09-02 repair removed for single words. It
        # survived here because scripts/measure_jd_reading.py builds its phantom list from
        # single unpunctuated words only (deliberately), so the instrument that gated that
        # repair is structurally blind to this branch -- and test_scoring.py pins the
        # phrase rule with `business requirements`, which has no short alias.
        #
        # A PHRASE alias keeps the substring test, because a substring test IS a phrase
        # test. A WORD alias has to be a whole token, which is what the branch at the end
        # of this function has always required. WORD_RE keeps "ba/bs" as one token, so the
        # bachelor-degree form does not fire either; a standalone "ba" occurs in 0.50% of
        # postings and "pm" in 3.85%, which is the real signal and it survives.
        for f in _alias_forms(t):
            if f == t:
                continue
            if " " in f or any(ch in f for ch in "+#./-"):
                if f in text_low:
                    return True
            elif f in toks:
                return True
        if phrase_exact:
            # ASKING A DIFFERENT QUESTION. Below, "every word of the phrase is present as a
            # stem" is the right rule for a RESUME -- "project management" should be answered by
            # "managed multiple projects", because the person did the thing. It is the wrong
            # rule for deciding whether a POSTING NAMES a skill, and measurably so: almost every
            # description contains "business" somewhere and "requirements" somewhere, so
            # `business requirements` fired on 63.8% of them -- and being absent from idf it
            # then took _UNSEEN_W x2.5 x1.6, making it one of the heaviest terms in the posting.
            # Same for "requirements gathering", "status reporting" and "project plan".
            #
            # A posting names a phrase when it SAYS the phrase (or an alias of it). Both branches
            # above already tested exactly that, so there is nothing further to try.
            return False
        canon = _canon_phrase(t)
        # A phrase matches when EVERY word of it is present as a stem -- "project management"
        # against "managed multiple projects". All of it, not any of it: "risk management" must
        # never be answered by the word "management" on its own.
        parts = [w for w in re.split(r"[^a-z0-9+#]+", canon) if w]
        return bool(parts) and all(_stem(w) in stems for w in parts)
    if t in toks:
        return True
    for f in _alias_forms(t):
        if f in toks or _stem(f) in stems or (" " in f and f in text_low):
            return True
    return False


# ---- which terms are worth SHOWING a reader ---------------------------------------------------
#
# THESE LIVED IN web.py, and that is why only one of three surfaces filtered. resume_brain cannot
# import web -- web imports resume_brain (web.py:114) and not the other way round -- so
# /brain/tailor had no way to reach the filter and rendered raw analyze_jd output. Worse, that
# raw list is what brain_tailor.html posts back into apply_feedback, so unfiltered noise became
# permanent trigger keys in users.brain_kb. One definition here, three callers.
#
# text_halves is NOT called from here: jdrender's header states why core must not import it
# (the scraper, the digest and score_jobs all import core and none of them renders anything), so
# the two halves are passed IN by whoever already has them.

# Words that are never a skill but score well because a description repeats them. Grown from a
# real Capital One posting, which offered "regarding criminal", "background inquiries",
# "applicable federal", "york", "posted", "state" and "laws" as keywords to add to a résumé.
KEYWORD_STOP = frozenset("""
posted posting position role job company employer candidate applicant applicants
state states city york county country federal laws law legal notice notices least
website site email phone contact address information available provide provided
please based employment technology technologies tools services service solutions
business teams environment opportunity support various including needs help
employees members people individuals others colleagues
""".split())

# Generic soft skills. Every posting says them, so naming them tells a reader nothing.
SKILL_STOP = frozenset("""
communication teamwork leadership collaboration interpersonal verbal written organizational
problem solving detail oriented time management customer service work experience team player
fast paced self starter multi task english degree bachelor master responsibilities requirements
qualifications preferred required ability able strong excellent knowledge understanding
""".split())

# ELIGIBILITY GATES, NOT SKILLS -- and the owner's own example of this panel naming the wrong
# thing. A clearance is not something you can add to a résumé by deciding to; it is a condition
# you either meet or you do not, so listing it under a heading that reads "worth adding" is
# advice nobody can act on. Deliberately NOT folded into PERK_TERMS, which is documented above
# as perks/benefits/compensation and is a different category; jdrender.FIELD_LABELS already
# treats "clearance" and "citizenship" as metadata FIELDS rather than as skills.
#
# These are still EXTRACTED -- analyze_jd keeps them, because a posting that requires a
# clearance is a posting we want to have understood. This set only governs what is offered to a
# reader as a skill.
ELIGIBILITY_TERMS = frozenset("""
clearance clearances polygraph citizenship naturalized
""".split()) | frozenset((
    "security clearance", "top secret", "ts sci", "public trust", "drug screen",
    "background check", "us citizen", "work authorization",
))


def display_terms(terms, company, cap, body="", boiler=""):
    """Keywords worth showing a reader, weight order preserved.

    Five things get dropped, in cheapness order:
      * the generic soft skills, the never-a-skill list, perks, and eligibility gates
      * the employer's own name. It is genuinely one of the highest-weighted terms in any
        description and says nothing: "Capital One" was marked six times in one posting.
      * anything under three characters that is not a known hard skill
      * a multi-word term made only of stopwords
      * TERMS THAT ONLY EVER APPEAR IN LEGAL BOILERPLATE. analyze_jd reads the whole
        description, EEO notice included, so the raw list contains phrases from it. Subtracting
        the boilerplate is a property of THIS posting rather than a blacklist to maintain, and
        it is what stops the page advising somebody to put "regarding criminal" on a résumé.
        Pass `body` and `boiler` from jdrender.text_halves; omitting them skips only this rule.
    """
    stop = (set(SKILL_STOP) | set(KEYWORD_STOP) | set(PERK_TERMS)
            | set(ELIGIBILITY_TERMS) | set(PLACE_TERMS))
    stop.update(w for w in re.split(r"\W+", (company or "").lower()) if len(w) > 2)
    # The company as the CORPUS spells it, not only as this row does. A row mislabelled "Amat"
    # subtracted nothing from a description opening "Applied Materials is a global leader".
    try:
        stop.update(w for w in norm_company(company or "").split() if len(w) > 2)
    except Exception:
        pass
    out = []
    for t in terms:
        low = (t or "").strip().lower()
        if not low or low in stop:
            continue
        # SHORT NAMES THAT ARE REAL SKILLS SURVIVE. A flat three-character floor hid c#, go, bi,
        # qa and ux -- every one of them in ATS_KEYWORDS, every one a thing an ATS scans for.
        if low not in ATS_KEYWORDS:
            if len(low) < 3:
                continue
            if all(w in stop or len(w) < 3 for w in low.split()):
                continue
        # A TERM WITH A PLACE IN IT IS A PLACE, however many other words it carries: "san
        # francisco", "angeles county", "york state". The all-stopwords rule above cannot say
        # that, because "san" is not a stopword on its own and so the whole bigram survived it.
        # analyze_jd already refuses these at extraction, so this only matters for a row whose
        # analysis predates that -- which is exactly when a display filter has to hold.
        if any(w in PLACE_TERMS for w in low.split()):
            continue
        # In the notice but not in the rest of the posting: a legal phrase, not a skill.
        if boiler and low in boiler and low not in body:
            continue
        out.append(t)
        if len(out) >= cap:
            break
    return out


# HOW MUCH OF A POSTING COUNTS AGAINST YOU. Raising this makes the score STRICTER, because a
# wider set means more terms you have to actually hold; lowering it is what makes a score
# flatter, since matching two or three headline words then carries everything.
#
# Re-measured after the matcher learned stems and aliases, because fixing false misses raised
# every score: a resume saying "budgets" was previously failing a JD asking for "budgeting", and
# that is a spelling gap, not a qualification gap. Share of postings scoring 70 or more:
#     0.70 -> 7.20%
#     0.80 -> 4.00%
#     0.90 -> 2.04%      <- here; the best match in 2,500 postings is 80
#     1.00 -> 1.48%      every term including the boilerplate
#
# 1.00 is barely stricter than 0.90 now, because the weighting fix below already stops
# boilerplate from carrying weight -- the two mechanisms had been doing the same job twice.
#
# 1.00 is the version this replaced, and its problem was not that it was strict but that it had
# no top: an excellent match and an average one were fifteen points apart and NOTHING read well,
# so the number could not tell you anything. 0.70 keeps the ceiling reachable in principle while
# making it genuinely rare in practice.
CORE_WEIGHT_FRACTION = 0.90


def core_terms(analyzed):
    """The keywords carrying the top half of a JD's weight — the ones the role leans on.

    WHY THE SCORE IS COMPUTED OVER THESE AND NOT OVER EVERY TERM. A job description names far
    more terms than any résumé will contain: the stack it uses, the benefits, the legal
    boilerplate, every adjacent technology. Scoring against all of them measures how exhaustive
    the posting is as much as how well you fit it, and the arithmetic showed it — across 6,000
    live postings the median was 28 and the 99th percentile 58, so a genuinely excellent match
    and a mediocre one were fifteen points apart at the bottom of a scale that never reached
    its own top.

    Restricting to the heavy part asks the question a person actually means: OF THE SKILLS THIS
    JOB EMPHASISES, how many do I have. A missing core skill costs real points instead of being
    diluted by forty pieces of boilerplate. Measured over 21,176 live postings at the current
    fraction: median 34, and only 0.4% score 70 or more, 0.1% score 80 or more.

    Terms are already weighted by idf, x2.5 for a hard ATS skill and x1.6 for appearing in the
    requirements section (see analyze_jd), so "heaviest" already means "most role-defining".
    """
    terms = analyzed.get("terms") or []
    weight = analyzed.get("weight") or {}
    if not terms:
        return []
    goal = sum(weight.get(t, 0.0) for t in terms) * CORE_WEIGHT_FRACTION
    ordered = sorted(terms, key=lambda x: -weight.get(x, 0.0))
    out, acc = [], 0.0
    for t in ordered:
        out.append(t)
        acc += weight.get(t, 0.0)
        # NEVER JUDGE A POSTING ON A HANDFUL OF WORDS. One term can carry the whole weight goal
        # when the analysis produced few terms or one dominates -- an ATS keyword is worth 2.5x
        # and 1.6x again in the requirements section -- and the result was a "Senior Delivery
        # Manager" reading 100% because the résumé held its single core term. 9% of postings were
        # being scored on three terms or fewer. A minimum makes the denominator honest: you are
        # measured against at least this many of the role's skills whenever it names that many.
        if acc >= goal and len(out) >= min(_MIN_JD_TERMS, len(ordered)):
            break
    return out


def score_against(resume_low, analyzed):
    """The résumé-DEPENDENT half: how much of what this JD EMPHASISES the résumé contains.
    `resume_low` must already be lowercased. Returns (score, keywords_have, keywords_to_add).

    The score covers core_terms() only — see the note there for why, and why the previous
    all-terms version could not tell a great match from an average one. The have/missing lists
    still span EVERY term, because they feed the job page's keyword panel and the résumé
    tailorer, which both want the full picture; they are weight-ordered, so the terms the score
    is actually made of are the ones at the top of each list.

    Matching is whole-word (not substring), so coverage isn't inflated by terms that merely
    sit inside unrelated résumé words. The score is floored, not rounded, so a partial match
    can never round UP to a misleading 100%."""
    terms = analyzed["terms"]
    if not terms:
        return 0, [], []
    weight = analyzed["weight"]
    words = _resume_wordset(resume_low)
    have = sorted((t for t in terms if _term_present(t, resume_low, words)), key=lambda t: -weight[t])
    missing = sorted((t for t in terms if not _term_present(t, resume_low, words)), key=lambda t: -weight[t])
    core = core_terms(analyzed)
    core_total = sum(weight[t] for t in core) or 1.0
    core_have = [t for t in core if _term_present(t, resume_low, words)]
    pct = 100.0 * sum(weight[t] for t in core_have) / core_total
    # CONFIDENCE CAP. A posting we could only extract a few keywords from cannot support a
    # strong claim about anybody: a "Senior Delivery Manager" whose analysis yielded ONE term
    # read 100% because the résumé happened to hold that term, and 9% of the corpus was being
    # judged on three terms or fewer. The ceiling rises with how much of the role we could
    # actually read — one term tops out at 16, three at 50, six or more is uncapped — so a thin
    # posting can still rank, it just cannot claim to be a strong match.
    cap = 100 if len(core) >= _MIN_JD_TERMS else int(100.0 * len(core) / _MIN_JD_TERMS)
    pct = min(pct, cap)
    # A JD too short or too sparse to analyse scores 0 HERE rather than in each caller: the cron
    # scorer already refused to score a thin analysis while the live per-user path in web.py did
    # not, so one job could carry two different numbers depending which reached it first. The
    # keyword lists are still returned — the job page's panel and the résumé tailorer both want
    # them, and "we cannot score this" is not "we found nothing in it".
    if analyzed.get("thin"):
        return 0, have, missing
    score = int(pct)                 # floor: 99.6% stays 99, never a phantom round-up to 100
    # 100 REQUIRES A CLEAN SWEEP OF THE WHOLE JD, not just of the core terms. Covering every
    # core term is already the top fraction of a percent of postings and it earns 99; reserving
    # the round number for "there is nothing in this posting you do not have" keeps it a claim
    # nobody has to squint at. Missing one boilerplate term to sit at 99 is the right cost.
    if score >= 100 and missing:
        score = 99
    return score, have, missing


def score_pct(resume_low, analyzed):
    """EXACTLY `score_against(resume_low, analyzed)[0]`, without building the two lists that
    caller throws away. Same arguments, same result, and that equivalence is the whole contract.

    Why it exists: web.user_scores calls score_against once per row and reads only `[0]`. But
    score_against also builds `have` and `missing` -- each a FULL SORT over every term in the
    JD, plus a third _term_present pass to compute the complement -- purely for the job page's
    keyword panel and the resume tailorer, neither of which is on this path. Across 38,805 rows
    that is ~78,000 sorts per user per scoring pass, discarded immediately. Skipping them took
    the pass from 54.8 to 19.9 us/row, i.e. 8.7 s to 0.77 s at that corpus size.

    THE ONE PLACE THE SCORE DEPENDS ON `missing` is score_against's final rule: a clean sweep of
    the core terms earns 100 only if nothing in the whole JD is absent. That needs a boolean, not
    an ordered list, and only when the score has already reached 100 -- which is a fraction of a
    percent of postings -- so it is computed lazily here and costs nothing on the common path.

    KEEP THE TWO IN STEP. scripts/test_speed_caches.py asserts equality over every analysed row
    in the local snapshot, not a sample: this is the number every match percentage in the product
    is made of, and a divergence would be invisible in the UI.
    """
    terms = analyzed["terms"]
    if not terms:
        return 0
    weight = analyzed["weight"]
    words = _resume_wordset(resume_low)
    core = core_terms(analyzed)
    core_total = sum(weight[t] for t in core) or 1.0
    pct = 100.0 * sum(weight[t] for t in core
                      if _term_present(t, resume_low, words)) / core_total
    cap = 100 if len(core) >= _MIN_JD_TERMS else int(100.0 * len(core) / _MIN_JD_TERMS)
    pct = min(pct, cap)
    if analyzed.get("thin"):
        return 0
    score = int(pct)                 # floor, exactly as score_against does
    if score >= 100 and any(not _term_present(t, resume_low, words) for t in terms):
        score = 99
    return score


def skill_match(resume_text, jd_text, idf=None):
    """ATS-style match: score = weighted % of the JD's important keywords present in the
    resume — hard skills / tools / certs and requirement-section terms weighted highest,
    exactly how a keyword-screening ATS works. Returns (score, keywords_have, keywords_to_add).

    Thin wrapper = score_against(resume, analyze_jd(jd)) so the hot paths can cache the
    expensive JD-invariant half; numeric output is unchanged."""
    return score_against((resume_text or "").lower(), analyze_jd(jd_text, idf))


# ---- precomputed per-job metadata (jdmeta.json) -------------------------------------------
# Everything about a job that's the SAME for every user (depends only on the JD text + idf):
# the analyzed keyword/weight structure, the experience floor + level, and the sponsorship
# signal. The cron scorer computes this once per job and writes jdmeta.json; the web app loads
# it at boot, so a cold render (after a restart / reload) skips ALL the regex + keyword work —
# including the expensive sponsorship scan — instead of recomputing it per request.
JDMETA_PATH = "jdmeta.json"


def job_meta(jd_text, idf=None):
    """One job's résumé-INDEPENDENT, JSON-serializable metadata. Used BOTH by the cron scorer
    (to build jdmeta.json) and by the web app on a cache miss — same function, so persisted
    values and any live-computed ones always agree."""
    # exp_years is the HIGHEST requirement stated (experience_years, not the old lenient
    # experience_min_years) — the key name is unchanged so every consumer keeps working.
    #
    # CLEANED ONCE, HERE, and passed to all four readers. analyze_jd cleans again internally
    # because it is also called directly, and clean_jd is idempotent -- a body with no furniture
    # left in it comes back unchanged -- but the year parser and the sponsorship reader have no
    # cleaning of their own, and a captured results list is exactly the kind of text that says
    # "3 years" about somebody else's job.
    clean, _verdict = clean_jd(jd_text)
    return {"analyzed": analyze_jd(clean, idf),
            "exp_years": experience_years(clean),
            "exp_level": experience_level(clean),
            "sponsor_jd": list(sponsorship_from_jd(clean))}


def load_jdmeta(path=JDMETA_PATH):
    """{url: job_meta} from disk, or {} if missing/unreadable (the web app then computes each
    job's meta live — slower first render, but never broken)."""
    if os.path.exists(path):
        try:
            return json.load(open(path, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_jdmeta(meta, path=JDMETA_PATH):
    try:
        json.dump(meta, open(path, "w", encoding="utf-8"))
    except Exception:
        pass


# ---- the wire form of analyze_jd(), for the jobs.jd_terms column --------------------------
# WHY THIS EXISTS: score_against() needs a job's keyword weights to score it against ANY résumé,
# and the feed has to do that for every row on every render. jdmeta.json holds them, but it is
# ~30 MB, gitignored, and built on an ephemeral GitHub Actions runner — so it never reached the
# live site, and every signed-in user was silently shown the stored match_score baseline (the
# repo's own resume.txt) instead of a score against their own profile. A column reaches
# production through Supabase with no file deploy.
#
# The packed form drops two of analyze_jd's four keys because both are derivable: `terms` is
# `weight`'s key order (analyze_jd builds weight from terms, one entry each), and `total` is the
# sum of the weights. That, plus rounding, is ~600 B/row against ~1,290 B for the raw dict —
# measured at 19,314 rows, and ~3.8 MB for the whole corpus once gzipped on the wire.
_ANALYZED_ROUND = 3          # weights only ever feed a ratio; 3 dp is far below a visible 1%


def pack_analyzed(analyzed):
    """analyze_jd() output -> the compact JSON STRING stored in jobs.jd_terms, or "" when there
    is nothing to store. A string (and a text column) rather than an object, so the value
    round-trips byte for byte — see the jd_terms note in db.JOBS_DERIVED_SQL."""
    w = (analyzed or {}).get("weight") or {}
    if not w:
        return ""
    return json.dumps({"w": {t: round(v, _ANALYZED_ROUND) for t, v in w.items()},
                       "n": 1 if (analyzed or {}).get("thin") else 0},
                      separators=(",", ":"))


def unpack_analyzed(packed, intern=None):
    """The inverse, rebuilding `terms` and `total`. Shaped exactly like analyze_jd's return so
    score_against can't tell the difference. Never raises — a malformed value scores as 0 rather
    than 500-ing the feed.

    `intern` is an optional {term: term} map the caller can pass to share one string object per
    term across the whole corpus — the vocabulary is ~44k distinct terms against ~640k (row,
    term) pairs, so interning is most of what keeps the rebuilt index affordable in memory.
    """
    empty = {"terms": [], "weight": {}, "total": 0.0, "thin": True}
    if not packed:
        return empty
    try:
        d = json.loads(packed) if isinstance(packed, str) else packed
        src = d.get("w") or {}
        if not src:
            return empty
        if intern is None:
            weight = dict(src)
        else:
            weight = {intern.setdefault(t, t): float(v) for t, v in src.items()}
        # dict order == the order pack_analyzed saw == analyze_jd's frozen term order, which is
        # the tie-break between equal-weight terms in score_against's have/missing lists.
        return {"terms": list(weight), "weight": weight,
                "total": float(sum(weight.values())), "thin": bool(d.get("n"))}
    except Exception:
        return empty


# ------------------------------------------------------------
# Sponsorship signal — the single biggest time-saver for an international student.
# A company can be a known H-1B sponsor yet post a role that explicitly WON'T work
# for a visa candidate (no sponsorship, or it needs citizenship / a clearance / a
# green card). We read that straight from the JD so those roles can be flagged/hidden.
# ------------------------------------------------------------
# Each entry carries the cheap substring "gate(s)" that MUST be present for its (expensive)
# regex to have any chance of matching: every alternative in the pattern contains one of
# these literals, so when none is present we can skip the regex entirely without changing
# the verdict. This is the whole optimization — most JDs name none of these, so a single
# .lower() + a few `in` checks replaces five full-text regex scans. (See sponsorship_from_jd.)
_SPONSOR_BLOCK = [
    ("no_sponsor", "JD says no visa sponsorship", ("sponsor",), re.compile(
        r"(?:will|are|is|can|do(?:es)?)?\s*(?:not|n't|unable|never)\b[^.]{0,40}\bsponsor"
        r"|\bno\b[^.]{0,15}\bsponsorship"
        r"|\bwithout[^.]{0,30}\bsponsorship"
        r"|\bsponsorship[^.]{0,20}\bnot\b[^.]{0,20}(?:available|offered|provided|considered)"
        r"|\bnot[^.]{0,15}(?:offer|provide|consider)[^.]{0,15}sponsorship"
        r"|\bdo(?:es)? not (?:require|need)[^.]{0,20}sponsorship"
        r"|authoriz(?:ed|ation) to work[^.]{0,70}without[^.]{0,25}sponsor", re.I)),
    ("citizen", "JD requires U.S. citizenship", ("citizen",), re.compile(
        r"\bmust be (?:a |an )?(?:u\.?s\.?\s*)?citizen"
        r"|\b(?:u\.?s\.?\s*)?citizenship\b[^.]{0,20}\b(?:is required|required|requirement|mandatory|only)"
        r"|\b(?:require[sd]?|requiring)\b.{0,25}?\bcitizenship", re.I)),
    ("clearance", "JD requires a security clearance",
     ("clearance", "ts/sci", "top secret", "public trust"), re.compile(
        r"\b(?:security|government)\b[^.]{0,15}clearance"
        r"|\bactive[^.]{0,20}clearance"
        r"|\bts/sci\b|\btop secret\b|\bsecret clearance\b|\bpublic trust\b"
        r"|\bclearance (?:is )?(?:required|eligible|active)", re.I)),
    ("greencard", "JD requires a green card / permanent residency",
     ("green card", "permanent residen"), re.compile(
        r"\b(?:green card|permanent residen(?:t|cy|ce))\b[^.]{0,25}(?:require|must|only|holder)"
        r"|must be (?:a )?(?:green card holder|permanent resident)", re.I)),
]
_SPONSOR_OPEN = re.compile(
    r"(?:visa|h-?1b|employment|work)?\s*sponsorship (?:is |may be |are |can be )?"
    r"(?:available|offered|provided|possible|considered|supported)"
    r"|(?:will|can|do|happy to|open to|able to|willing to|we)\s+(?:gladly |certainly )?sponsor\b"
    r"|(?:offer|provide|support)[^.]{0,20}(?:visa |h-?1b )?sponsorship"
    r"|\bh-?1b[^.]{0,15}sponsorship"
    r"|\bsponsor[^.]{0,15}(?:visa|h-?1b|work authorization)", re.I)


def sponsorship_from_jd(jd_text):
    """Read a JD for an explicit sponsorship signal. Returns (verdict, reason):
      'blocked' = the JD says it won't work for a visa candidate (no sponsorship, or it
                  requires U.S. citizenship / a security clearance / a green card)
      'open'    = the JD explicitly offers visa sponsorship
      ''        = no clear signal (most postings)
    Checks the 'blocked' phrasings first since those are the ones that waste your time.

    Each regex is gated behind a cheap substring test (a literal every one of its
    alternatives must contain): ~70% of JDs mention none of these words, so they return
    immediately instead of running five `[^.]{0,N}`-window regexes over the full text.
    The regexes themselves are unchanged, so the verdict is identical to scanning always."""
    jd = jd_text or ""
    if not jd:
        return "", ""
    low = jd.lower()
    for _cat, msg, gates, rx in _SPONSOR_BLOCK:
        if any(g in low for g in gates) and rx.search(jd):
            return "blocked", msg
    if "sponsor" in low and _SPONSOR_OPEN.search(jd):
        return "open", "JD offers visa sponsorship"
    return "", ""


# ------------------------------------------------------------
# H-1B cap-exempt employers (universities, nonprofit hospitals, research institutes).
# Cap-exempt = NO H-1B lottery — a major edge for an international student, so we badge it.
# ------------------------------------------------------------
_CAP_EXEMPT_RE = re.compile(
    r"\b(?:universit(?:y|ies)|college|polytechnic|institute of technology"
    r"|school of (?:medicine|public health|nursing|engineering|law)"
    r"|cancer (?:institute|center|centre)|medical (?:center|centre|college|school)"
    r"|health system|hospital|children'?s hospital|clinic"
    r"|national lab(?:oratory)?|research institute)\b", re.I)
_CAP_EXEMPT_NAMES = ("mayo clinic", "cleveland clinic", "kaiser permanente", "dana-farber",
                     "memorial sloan", "md anderson", "mass general", "brigham and women",
                     "national institutes of health")


def is_cap_exempt(company):
    """Heuristic: True if the employer is LIKELY H-1B cap-exempt (universities, nonprofit
    hospitals, research institutes) → no H-1B lottery. A hint to verify, not a guarantee."""
    c = (company or "").lower()
    if not c:
        return False
    if any(n in c for n in _CAP_EXEMPT_NAMES):
        return True
    return bool(_CAP_EXEMPT_RE.search(c))


# ------------------------------------------------------------
# Sponsor STRENGTH — turn the yes/no flag into a confidence tier using DOL filing
# VOLUME (a company that files thousands of H-1Bs is a far safer bet than one with two).
# Needs an optional sponsor_counts.json {normalized_name: count}; degrades to '' without it.
# ------------------------------------------------------------
_SPONSOR_META = {}          # path -> the "#meta" provenance block the loader popped


def _load_sponsor_json(path):
    """Load one of the sponsor indexes, lifting its "#meta" block out of the mapping.

    THE POP IS LOAD-BEARING, not tidiness. Both files carry a "#meta" dict describing the
    fiscal-year window they were built from (see scraper/build_sponsor_counts.py). Two callers
    walk these mappings rather than .get()-ing them, and one fails SILENTLY:

      web.py::sponsor_data_through iterates sponsor_years().values() and int()s each inner
      key, inside a bare `except Exception: pass` that falls through to a hardcoded "FY2023".
      A "#meta" value is a dict of strings, exactly like every real value there, so int("built")
      raises, the except swallows it, and the vintage label freezes at FY2023 — after a refresh
      whose entire purpose was to move it. No error, no log line.

      scraper/__init__.py::build_sponsor_index does set(sponsor_counts) into its `wide` index.

    Popping here fixes both, because every runtime reader goes through these two loaders.
    "#" cannot survive _norm_name, so no company lookup could ever collide with the key.
    """
    if not os.path.exists(path):
        return {}
    try:
        d = json.load(open(path, encoding="utf-8")) or {}
    except Exception:
        return {}
    if not isinstance(d, dict):
        return {}
    _SPONSOR_META[path] = d.pop("#meta", None) or {}
    return d


def sponsor_meta(path="sponsor_years.json"):
    """The provenance block: which fiscal years the shipped counts actually cover.

    Reads sponsor_years.json by default rather than sponsor_counts.json — the block is
    identical in both and that file is 0.2 MB against 2.9 MB, so asking for the vintage never
    drags the wide index into memory.
    """
    if path not in _SPONSOR_META:
        _load_sponsor_json(path)
    return _SPONSOR_META.get(path) or {}


def sponsor_window():
    """The tier window as a label, e.g. "FY2021-2025". "" when the data predates #meta."""
    yrs = [int(y) for y in (sponsor_meta().get("years") or []) if str(y).isdigit()]
    if not yrs:
        return ""
    return "FY%d-%d" % (min(yrs), max(yrs)) if min(yrs) != max(yrs) else "FY%d" % yrs[0]


def load_sponsor_counts(path="sponsor_counts.json"):
    """Optional {normalized_company: H1B_approval_count} built from the USCIS Data Hub.
    Returns {} when the file is absent (strength just isn't shown)."""
    return _load_sponsor_json(path)


def load_sponsor_years(path="sponsor_years.json"):
    """Optional {normalized_company: {fiscal_year: approvals}} — the per-year H-1B history
    behind the company panel's chart, built by scraper.build_sponsor_counts from the USCIS
    Data Hub bulk CSVs. Returns {} when the file is absent (the chart just isn't drawn).

    Small on purpose (~0.1 MB / ~2,200 employers): it covers only names in our own universe,
    because the panel can only be opened for an employer that is in the corpus. sponsor_counts
    stays the wide index, since the tier lookup has to resolve any spelling.
    """
    return _load_sponsor_json(path)


def _sponsor_key(company):
    """The lookup key both sponsor_counts.json and sponsor_years.json are written under."""
    try:
        import scraper                      # lazy: scraper imports core (avoid circular at load)
        return scraper._norm_name(company)
    except Exception:
        return re.sub(r"[^a-z0-9 ]+", " ", (company or "").lower()).strip()


def sponsor_history(company, years_index):
    """[(fiscal_year, approvals), ...] ascending, or [] when we have no history.

    GAPS ARE FILLED WITH ZERO between the first and last year on record. Garmin filed in 2009,
    2011 and 2013 but not 2010, 2012 or 2015; plotting only the years present would draw those
    as adjacent bars and imply continuous filing. A zero year is a fact about the employer, and
    the shape of the run is the whole reason to show a history rather than a total.
    """
    if not years_index or not company:
        return []
    hist = years_index.get(_sponsor_key(company)) or years_index.get((company or "").lower())
    if not hist:
        return []
    try:
        pairs = {int(y): int(n) for y, n in hist.items() if int(n) >= 0}
    except (TypeError, ValueError):
        return []
    if not pairs:
        return []
    return [(y, pairs.get(y, 0)) for y in range(min(pairs), max(pairs) + 1)]


_norm_company_cache = {}


# Tokens that carry no identity in a monogram. Dropped ONLY when longer than one character:
# norm_company turns "U.S. Bank" into "u s bank", so a single letter is an acronym part and has
# to survive or that tile reads UB. "Amazon.com Services LLC" normalises to "amazon com
# services", which is why "com" has to go or that tile reads AC.
_MONO_SKIP = {"com", "net", "org", "the", "and", "of", "for", "www"}


def initials(name):
    """Two letters for a company's monogram tile, e.g. "AS" for "Amazon.com Services LLC".

    LIVES HERE BECAUSE THREE CALLERS NEED THE SAME ANSWER: web.py renders it, 
    scripts/build_logos.py records it in the harvest ledger, and scripts/test_logos.py freezes
    it. Two of those had identical copies of this function for a while, which is the same twin
    problem as the filter triplet with a smaller blast radius.

    Built on norm_company so the legal suffixes and the noise words go first. Measured against
    the corpus: "U.S. Bank" is US, "Ernst & Young" is EY, "Agilent Technologies" is AG (not AT,
    which would collide with every other "<X> Technologies"), "10x Genomics" is 10.
    """
    key = norm_company(name) or (name or "")
    words = [w for w in re.split(r"[^0-9A-Za-z]+", key) if w
             and not (len(w) > 1 and w in _MONO_SKIP)]
    if not words:
        return "?"
    if not words[0][0].isalpha():
        return words[0][:2].upper()
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[1][0]).upper()

# THE HOSTS THAT ARE A HIRING PLATFORM RATHER THAN AN EMPLOYER. A posting URL on one of these
# carries a TENANT name, not a website -- "seic.wd1.myworkdayjobs.com" says nothing about
# seic.com -- so any rule that reads a company's domain off its own board URL has to refuse
# them first. Lived in web.py as _PLATFORM_HOSTS until 2026-09-04, when scripts/build_logos.py
# needed the same list; a second copy of it is exactly the kind of twin this repo keeps warning
# about, so it moved here and both callers import it.
#
# IT IS NOT COMPLETE AND CANNOT BE. Measured 2026-09-04 against the live board set, jibeapply.com
# is a platform serving three of our employers and was absent from this tuple. So a caller that
# trusts a board host must ALSO refuse any host claimed by more than one employer, which derives
# platform-ness from the corpus instead of from this hand list. Both are needed: the corpus rule
# misses a platform with a single tenant (jobdiva.com, one claimant), and this list misses a
# platform nobody has written down yet.
PLATFORM_HOSTS = (
    "myworkdayjobs.com", "greenhouse.io", "lever.co", "ashbyhq.com", "smartrecruiters.com",
    "icims.com", "jobvite.com", "workable.com", "bamboohr.com", "taleo.net", "successfactors.com",
    "sapsf.com", "avature.net", "jobdiva.com", "ultipro.com", "paylocity.com", "oraclecloud.com",
    "eightfold.ai", "recruitics.com", "rippling.com", "isolvedhire.com", "apploi.com",
    "phenompeople.com", "peoplefluent.com", "silkroad.com", "brassring.com", "dayforcehcm.com",
    "jibeapply.com",
)


def norm_company(company):
    """scraper._norm_name(company), memoized.

    The normalization is three re.sub calls and the CALLER is per-row: _build_row runs it once
    for every job in the corpus, so at ~25k rows that was ~75k substitutions per ranked_rows
    rebuild to re-derive a few thousand distinct answers. Distinct employers are a small
    fraction of rows, which is exactly when a memo pays.

    Keyed on the company string alone, which is safe because the answer depends on NOTHING
    else — unlike visa_tags/is_everify, which cache a result that also depends on their index
    argument. That is why sponsor_strength memoizes this rather than its own return value:
    test_jobspy_adapter.py calls it twice with the same company and different `counts` and
    expects different tiers.
    """
    hit = _norm_company_cache.get(company)
    if hit is not None:
        return hit
    try:
        import scraper                      # lazy: scraper imports core (avoid circular at load)
        key = scraper._norm_name(company)
    except Exception:
        key = re.sub(r"[^a-z0-9 ]+", " ", company.lower()).strip()
    _norm_company_cache[company] = key
    return key


def sponsor_strength(company, counts):
    """Tier a sponsor by filing VOLUME. Returns ('high'|'medium'|'low'|'', count).
    ('', 0) when there's no number for the company. Counts come from load_sponsor_counts()."""
    if not counts or not company:
        return "", 0
    key = norm_company(company)
    try:
        n = int(counts.get(key) or counts.get(company.lower()) or 0)
    except Exception:
        n = 0
    if n >= 1000:
        return "high", n
    if n >= 100:
        return "medium", n
    if n >= 1:
        return "low", n
    return "", 0


# NO per-state sponsor count here, on purpose. The USCIS H-1B Data Hub's State/City is the
# PETITIONER's mailing address, not the worksite: measured over FY2019-23, Google is 100% CA,
# Microsoft 100% WA, Infosys 100% TX and Deloitte 86% PA (its Hermitage processing centre).
# So "sponsored N H-1Bs in MA" would tell a student the opposite of the truth about where an
# employer actually hires. Worksite-level sponsorship needs the DOL LCA disclosure files
# (which carry WORKSITE_STATE) — see scraper/build_sponsors.py for that data source.


# ------------------------------------------------------------
# E-VERIFY — flag employers enrolled in E-Verify. This is the signal an F-1 student
# needs for the STEM-OPT 24-month extension (which REQUIRES an E-Verify employer) —
# separate from H-1B sponsorship. Mirrors the sponsor flag: a curated everify.txt
# (built by scraper/build_everify.py from a real E-Verify snapshot, already scoped to
# legitimate employers) -> normalized name match. Degrades to nothing without the file.
# ------------------------------------------------------------
def load_everify(path="everify.txt"):
    """Build a normalized index of E-Verify-enrolled company names from everify.txt
    (one name per line, '#' ignored). Returns {"raw":[...], "norm":{...}} or None when
    the file is absent/empty — so the badge simply doesn't render until it's built."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            names = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    except Exception:
        return None
    if not names:
        return None
    try:
        import scraper                  # lazy: scraper imports core (avoid circular at load)
        norm = {scraper._norm_name(n) for n in names}
    except Exception:
        norm = {re.sub(r"[^a-z0-9 ]+", " ", n.lower()).strip() for n in names}
    return {"raw": names, "norm": norm}


_everify_cache = {}

def is_everify(company, index):
    """True if `company` is in the E-Verify enrolled-employer index. Normalized exact
    match first; fuzzy fallback for a small curated list (so 'Amazon' still matches
    'Amazon.com Services'). Cached per company. ('' index / no match -> False.)"""
    if not index or not company:
        return False
    if company in _everify_cache:
        return _everify_cache[company]
    try:
        import scraper
        norm = scraper._norm_name(company)
    except Exception:
        norm = re.sub(r"[^a-z0-9 ]+", " ", company.lower()).strip()
    hit = bool(norm) and norm in index["norm"]
    if not hit and norm and len(index["raw"]) <= 5000:
        try:
            from rapidfuzz import fuzz
            hit = any(fuzz.token_set_ratio(norm, n) >= 90 for n in index["norm"])
        except ImportError:
            hit = any(norm == n or norm + " " in n + " " or n + " " in norm + " "
                      for n in index["norm"])
    _everify_cache[company] = hit
    return hit


# ------------------------------------------------------------
# VISA TAGS — which immigration routes has this employer actually filed for?
# Backed by visa_tags.json (see scraper/build_visa_tags.py), an index of
# {normalized employer name: bitmask} built from the DOL LCA + PERM disclosure files
# and the E-Verify employer export.
#
# Lookup here is a plain dict hit and nothing more. All the fuzzy name matching happens
# at BUILD time, where each decision is written to visa_tags_report.csv and can be
# reviewed — doing it at request time would mean silent wrong tags with no audit trail.
#
# A present tag means "this employer has filed for this route before". A MISSING tag means
# we have no record, NOT that they won't sponsor: the index covers whichever quarters were
# fed to the builder. Never render absence as a negative.
# ------------------------------------------------------------
VISA_TAGS = ("h1b", "green_card", "stem_opt", "e3", "h1b1")     # == render order
_VISA_BITS = {"h1b": 1, "green_card": 2, "stem_opt": 4, "e3": 8, "h1b1": 16}


def visa_tags_from_bits(mask):
    """A stored bitmask -> the same tuple visa_tags() returns, in VISA_TAGS order.

    public.companies stores the mask rather than the expanded names so that this table stays the
    only place that knows h1b is bit 1 -- adding a sixth route then needs no data migration, just
    an entry above. The inverse (`visa_tags`) reads the same map two lines up, so the round trip
    cannot drift.

    A TUPLE, matching visa_tags: the result is shared across every card for that employer and a
    mutable return would be an aliasing bug waiting to happen.
    """
    mask = int(mask or 0)
    return tuple(t for t in VISA_TAGS if mask & _VISA_BITS[t]) if mask else ()

# Labelled STEM-OPT, not "E-Verify": E-Verify is the evidence, STEM-OPT is the thing you're
# actually looking for. Note there is deliberately NO plain "OPT" filter — regular 12-month
# OPT needs nothing from the employer (you already hold the EAD), so every job would match
# and it would filter nothing. The 24-month STEM extension is different: the employer MUST be
# E-Verify enrolled, which is a real, checkable property of the company.
VISA_TAG_LABELS = {"h1b": "H-1B", "green_card": "Green Card", "stem_opt": "STEM-OPT",
                   "e3": "E-3", "h1b1": "H-1B1"}
VISA_TAG_TIPS = {
    "h1b": "This employer has certified H-1B labor condition applications. Past filings, "
           "not a promise.",
    "green_card": "This employer has certified PERM (green card) applications, so they sponsor "
                  "permanent residency, not just temporary work visas.",
    "stem_opt": "Listed as an enrolled E-Verify employer, which is required for the STEM-OPT "
                "24-month extension. Confirm at e-verify.gov before relying on it.",
    "e3": "This employer has filed E-3 applications (Australian nationals).",
    "h1b1": "This employer has filed H-1B1 applications (Chile / Singapore nationals).",
}

# The absence caveat, in ONE place. It shipped in two different wordings (feed.html and
# company.html said it differently), which is how a load-bearing legal sentence drifts: each
# template edited it locally and neither knew about the other. This is feed.html's wording,
# byte for byte, because it was the shorter of the two. Every surface renders THIS constant.
VISA_ABSENCE_NOTE = "No route shown means no record, not a refusal."

# What ONE hedged chip on a card is allowed to say. Deliberately not five labels and not a
# count: see sponsor_likely() below.
SPONSOR_LIKELY_LABELS = {"h1b": "H-1B Likely", "sponsor": "Sponsor Likely",
                         "stem_opt": "STEM-OPT Likely"}
# green_card, e3 and h1b1 all collapse into "sponsor". Naming them separately on a card was
# the problem: E-3 and H-1B1 are gated on nationality (Australia, Chile / Singapore), so for
# almost every reader they are noise, and a green card is real sponsorship evidence but a
# later-stage route than the one you get hired on. The job page names all five.
_SPONSOR_LIKELY_OTHER = frozenset(("green_card", "e3", "h1b1"))


def load_visa_tags(path="visa_tags.json"):
    """{normalized name: bitmask} from visa_tags.json. {} when the file is absent, so every
    badge and filter simply doesn't render until it's built."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return {}
    if not isinstance(d, dict):
        return {}
    d.pop("#meta", None)            # provenance block, never a company (a '#' can't survive _norm_name)
    return d


_visa_cache = {}


def visa_tags(company, index):
    """Tuple of tag keys for `company`, in VISA_TAGS order. () when unknown.

    Returns a TUPLE deliberately: the result is memoized and shared across every row for
    that employer, so a mutable return would be an aliasing bug waiting to happen.
    """
    if not index or not company:
        return ()
    hit = _visa_cache.get(company)
    if hit is not None:
        return hit
    try:
        import scraper                  # lazy: scraper imports core (avoid circular at load)
        norm = scraper._norm_name(company)
    except Exception:
        norm = re.sub(r"[^a-z0-9 ]+", " ", company.lower()).strip()
    mask = index.get(norm) or 0
    out = tuple(t for t in VISA_TAGS if mask & _VISA_BITS[t]) if mask else ()
    _visa_cache[company] = out
    return out


def visa_tag_labels(tags):
    """['H-1B', 'Green Card'] for display in the email digest and the card."""
    return [VISA_TAG_LABELS[t] for t in (tags or ()) if t in VISA_TAG_LABELS]


# A blocked JD whose reason mentions one of these rules out EVERY foreign candidate, including
# one who needs no sponsorship at all. Matched against the reason strings in _SPONSOR_BLOCK.
_BLOCKS_EVERYONE = ("citizenship", "clearance", "green card")


def visa_tags_for_posting(tags, sponsor_jd, reason=""):
    """Narrow an EMPLOYER's visa tags down to what THIS posting actually allows.

    The tags say what a company has sponsored in the past; the JD says what this particular
    role will do. When they disagree the JD wins, otherwise a card reads
    "H-1B · Green Card · E-3 · No sponsorship", which is nonsense and the exact false
    positive that makes the whole feature untrustworthy.

    The two cases are deliberately different:
      * "no visa sponsorship" removes the routes that REQUIRE the employer to sponsor
        (H-1B, green card, E-3, H-1B1) but KEEPS E-Verify. OPT and STEM-OPT are not
        sponsorship — the candidate already holds work authorization, and all the employer
        has to be is E-Verify enrolled. Those roles are still worth seeing.
      * citizenship / security clearance / "must already hold a green card" rule out a
        foreign candidate entirely, so every tag goes.
    """
    tags = tuple(tags or ())
    if sponsor_jd != "blocked" or not tags:
        return tags
    low = (reason or "").lower()
    if any(k in low for k in _BLOCKS_EVERYONE):
        return ()
    return tuple(t for t in tags if t == "stem_opt")


def sponsor_likely(tags):
    """The ONE hedged claim a card makes about sponsorship. Returns a key, or "".

    Pass the tags ALREADY narrowed by visa_tags_for_posting(), so a posting whose own text
    closes a route can never surface a chip for it.

    The card used to show up to three chips plus a "+2 more". Three chips is not three facts,
    it is one fact spread thin, and it read far more confidently than one quarter of federal
    filings can support — hence "Likely", which is the whole claim: this employer has a
    federal record for this route. Not that they will sponsor you, and not that they file
    this route more than the others.

    The order is by what a route is WORTH to a reader who needs sponsorship, not by filing
    volume (the index carries no counts, and volume across programs is not comparable anyway
    — an LCA is filed per position and often re-filed yearly, a PERM once per worker):

      h1b       an H-1B record is the route you actually get hired on.
      sponsor   any other certified filing is real sponsorship evidence.
      stem_opt  E-Verify enrolment, and the weakest of the three by a wide margin: only
                3.3% of the 32,012 enrolled employers file LCAs at all, the rest enrolled
                for I-9 compliance. It is necessary for the STEM-OPT extension and close to
                useless as evidence anyone hires international workers.
    """
    tags = frozenset(tags or ())
    if "h1b" in tags:
        return "h1b"
    if tags & _SPONSOR_LIKELY_OTHER:
        return "sponsor"
    if "stem_opt" in tags:
        return "stem_opt"
    return ""


def parse_visa_pref(s):
    """'h1b,junk,e3' -> ('h1b','e3'). Canonical order, junk dropped, duplicates collapsed."""
    if not s:
        return ()
    if not isinstance(s, str):
        s = ",".join(str(x) for x in s)
    want = {p.strip().lower() for p in s.split(",") if p.strip()}
    return tuple(t for t in VISA_TAGS if t in want)


def visa_tags_match(row_tags, wanted, blocked=False):
    """OR semantics: a row passes if it carries ANY wanted tag. No wanted tags == no filter.

    OR rather than AND on purpose -- five AND-ed checkboxes return almost nothing, and the
    question a user is asking is "H-1B *or* green card", not "both at once".

    ABSENCE IS NOT A REFUSAL, and until 2026-09-08 this returned False for it. An empty tag set
    means one of two completely different things:

      * we hold no federal filing record for this employer -- SILENCE. 3,596 active rows
        (10.4%), and 26 of them are CAP-EXEMPT employers, which is the sharpest case there is:
        a university files no H-1B petitions because it does not need to (no lottery), so "no
        record" there means the best route available. Ticking H-1B deleted exactly those.
      * this posting's own text rules sponsorship out, so visa_tags_for_posting stripped every
        tag (see _BLOCKS_EVERYONE). 8,244 rows (23.8%). That is the EMPLOYER answering the
        question, it stays removed, and `hidenospon` is the control built for it.

    Conflating the two is what made the old rule look defensible. Measured: ticking H-1B removed
    13,133 rows (37.9%) and now removes 9,537 (27.5%). core.py's sponsor_rank note argues the
    same thing for ranking; templates/_filterbar.html has been printing "No route shown means no
    record, not a refusal" directly above the checkboxes that did the opposite.

    `blocked` is the posting's sponsor_jd verdict. Callers that cannot see it get the safe,
    inclusive answer, which is the direction this whole change is in.
    """
    if not wanted:
        return True
    if set(wanted) & set(row_tags or ()):
        return True
    return not (row_tags or blocked)


# ------------------------------------------------------------
# STAFFING / CONSULTANCY ("agency") flag — mark body-shop / staffing-firm employers so the user
# can spot-and-skip them. They're KEPT in the feed (many are heavy H-1B sponsors), just badged —
# this is a hint, not a hard filter. Two signals: a generic body-shop NAME SHAPE (BODYSHOP_RE,
# the single source of truth also imported by scraper/build_everify.py) + a small set of named
# staffing/consultancy firms that are real companies the shape regex won't catch by name.
# ------------------------------------------------------------
# Real IT-services GIANTS (Infosys, Cognizant, HCL, TCS, Wipro, Accenture, Deloitte…) do NOT
# match these shapes, so they're unaffected.
BODYSHOP_RE = re.compile(
    r"\b(soft\s*systems?|tech\s*solutions?|software\s*solutions?|it\s*solutions?|"
    r"info(?:tech| systems?| solutions?)|tek\s*solutions?|consultancy services?|"
    r"staffing|technologies\s+inc|solutions\s+inc|systems\s+inc|infotech|"
    r"global\s+(?:it|tech|soft|systems?|solutions?))\b", re.I)

# Named staffing / recruiting / bench-consultancy firms: real companies (so BODYSHOP_RE doesn't
# flag them by shape) whose postings are agency/placement roles, not a direct employer's own
# team. Matched as a normalized substring of the company name.
_AGENCY_NAMES = (
    "actalent", "aerotek", "teksystems", "insight global", "apex systems", "kforce",
    "robert half", "randstad", "adecco", "manpower", "kelly services", "collabera",
    "eteam", "judge group", "beacon hill", "signature consultants", "experis", "yoh",
    "system one", "mastech", "diverse lynx", "compunnel", "artech", "cybercoders",
    "on-board", "us tech solutions", "pyramid consulting", "nesco resource", "roljobs",
    # MEASURED 2026-09-08 and it was the biggest single omission here: Michael Page is 453
    # active rows and 3.6% of the whole delivery-family feed -- the SECOND-largest employer in
    # it after Amazon -- and none of them were badged. PageGroup is a global recruitment firm,
    # so every one of those cards is a middleman listing. Nothing in _AGENCY_RE could catch it:
    # the name carries no staffing word at all, which is exactly why a named list exists
    # alongside the shape regex.
    #
    # Deliberately NOT added at the same time: "hays" (2 rows, and the bare substring would
    # fire on any name containing it), and the IT-services giants Cognizant/Infosys/HCL/TCS/
    # Wipro/Accenture/Deloitte, which BODYSHOP_RE's note above already rules out on purpose --
    # they hire directly and a wrong Agency badge costs more trust than a missing one.
    "michael page",
)


# Precise agency/body-shop NAME SHAPES for the live "Agency" badge. This deliberately does NOT
# reuse the broad BODYSHOP_RE: that pattern's bare "<x> technologies/systems/solutions inc" rules
# catch real DIRECT employers ("Keysight Technologies Inc", "Cadence Design Systems Inc"), and a
# wrong Agency badge is worse for the user's trust than missing one. So here we match only
# staffing-specific words + unambiguously body-shop "solutions/systems" shapes. (BODYSHOP_RE is
# unchanged and still used by scraper/build_everify.py's E-Verify curation, where a human reviews.)
_AGENCY_RE = re.compile(
    r"\b(?:staffing|recruit(?:ing|ment|ers)|placements?|consultanc(?:y|ies) services?"
    r"|(?:tech|software|it|hr)\s*solutions?"
    r"|soft\s*systems?|infotech|info\s*(?:systems?|solutions?)|tek\s*solutions?"
    r"|global\s+(?:it|tech|soft|systems?|solutions?)"
    r"|talent\s+(?:group|advisors?|partners?|acquisition|solutions?))\b", re.I)


def is_agency(company):
    """Heuristic: True if `company` looks like a staffing agency / IT body-shop / bench
    consultancy rather than a direct employer — a named staffing/recruiting firm (_AGENCY_NAMES)
    OR a precise body-shop name shape (_AGENCY_RE). A hint to help the user spot-and-skip; these
    rows are kept in the feed (for their H-1B sponsorship value), just badged."""
    c = (company or "").lower()
    if not c:
        return False
    if any(n in c for n in _AGENCY_NAMES):
        return True
    return bool(_AGENCY_RE.search(c))


# ------------------------------------------------------------
# LOCATION parsing — the boards spell the same place ~5 different ways ("Seattle, WA" /
# "Seattle, Washington, USA" / "US, WA, Seattle"), so the raw string can't be filtered on.
# Resolve it to a state code + metro so the feed can offer a real "where" filter.
# ------------------------------------------------------------
_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI",
    "wyoming": "WY", "district of columbia": "DC", "puerto rico": "PR",
}
_STATE_CODES = set(_STATES.values())
# Longest-first so "west virginia" is tried before "virginia" and "new york" before "york".
_STATE_NAMES_RE = re.compile(
    r"\b(" + "|".join(sorted((re.escape(n) for n in _STATES), key=len, reverse=True)) + r")\b")
# Split on real separators only. Deliberately NOT on the words "or"/"and": "Portland, OR"
# would lose Oregon to the delimiter.
_LOC_SPLIT_RE = re.compile(r"\s*(?:,|/|\||;| - )\s*")
# A token like "MA (Remote)", "CA United States" or "MN 55403" still leads with the code.
_LEAD_CODE_RE = re.compile(r"([A-Za-z]{2})\b")
# ...and some boards write the code last with no comma at all: "USA   Seattle WA".
_TRAIL_CODE_RE = re.compile(r"\b([A-Z]{2})\s*$")
# Washington DC must be tested BEFORE the state-name scan, or the bare word "Washington"
# inside it resolves to WA.
_DC_RE = re.compile(r"\bwashington,?\s*d\.?\s*c\.?|\bwashington\s+dc\b|\bdistrict of columbia\b", re.I)

# City (or suburb) -> the metro a student actually thinks in. Only the high-volume ones;
# anything unlisted just falls back to "<City>, ST" so the filter still works.
_METROS = {
    "Boston, MA": ("boston", "cambridge", "somerville", "waltham", "burlington", "quincy",
                   "newton", "woburn", "lexington", "needham", "marlborough", "framingham"),
    "New York, NY": ("new york", "manhattan", "brooklyn", "queens", "bronx", "new york city",
                     "jersey city", "newark", "hoboken", "white plains", "long island city"),
    "San Francisco Bay Area, CA": ("san francisco", "san jose", "palo alto", "mountain view",
                                   "sunnyvale", "santa clara", "cupertino", "menlo park",
                                   "oakland", "berkeley", "fremont", "redwood city", "milpitas",
                                   "san mateo", "foster city", "emeryville", "campbell"),
    "Seattle, WA": ("seattle", "bellevue", "redmond", "kirkland", "renton", "tukwila", "everett"),
    "Los Angeles, CA": ("los angeles", "santa monica", "pasadena", "burbank", "el segundo",
                        "culver city", "long beach", "irvine", "torrance", "glendale"),
    "San Diego, CA": ("san diego", "carlsbad", "la jolla"),
    "Austin, TX": ("austin", "round rock"),
    "Dallas, TX": ("dallas", "plano", "irving", "fort worth", "richardson", "frisco", "arlington, tx"),
    "Houston, TX": ("houston", "sugar land", "the woodlands"),
    "Chicago, IL": ("chicago", "evanston", "naperville", "schaumburg", "deerfield"),
    "Washington, DC": ("washington", "arlington", "alexandria", "bethesda", "reston", "mclean",
                       "herndon", "tysons", "rockville", "silver spring", "vienna"),
    "Atlanta, GA": ("atlanta", "alpharetta", "marietta", "sandy springs"),
    "Denver, CO": ("denver", "boulder", "aurora", "broomfield", "englewood", "louisville, co"),
    "Phoenix, AZ": ("phoenix", "tempe", "scottsdale", "chandler", "mesa", "gilbert"),
    "Philadelphia, PA": ("philadelphia", "king of prussia", "malvern", "wayne, pa"),
    "Minneapolis, MN": ("minneapolis", "saint paul", "st paul", "bloomington, mn", "eagan"),
    "Portland, OR": ("portland", "beaverton", "hillsboro"),
    "Raleigh-Durham, NC": ("raleigh", "durham", "cary", "chapel hill", "morrisville"),
    "Charlotte, NC": ("charlotte",),
    "Detroit, MI": ("detroit", "ann arbor", "dearborn", "troy, mi", "warren, mi", "auburn hills"),
    "Miami, FL": ("miami", "fort lauderdale", "boca raton", "coral gables"),
    "Orlando, FL": ("orlando", "lake mary"),
    "Tampa, FL": ("tampa", "st petersburg", "saint petersburg"),
    "Salt Lake City, UT": ("salt lake city", "lehi", "provo", "draper"),
    "Nashville, TN": ("nashville", "franklin, tn", "brentwood, tn"),
    "Pittsburgh, PA": ("pittsburgh",),
    "Columbus, OH": ("columbus",),
    "Cleveland, OH": ("cleveland",),
    "Cincinnati, OH": ("cincinnati",),
    "Indianapolis, IN": ("indianapolis",),
    "Kansas City, MO": ("kansas city", "overland park"),
    "St. Louis, MO": ("st louis", "saint louis"),
    "Milwaukee, WI": ("milwaukee",),
    "Madison, WI": ("madison",),
    "Baltimore, MD": ("baltimore", "columbia, md", "hanover, md"),
    "Richmond, VA": ("richmond",),
    "Sacramento, CA": ("sacramento", "folsom", "roseville"),
    "Las Vegas, NV": ("las vegas", "henderson"),
    "San Antonio, TX": ("san antonio",),
    "Boise, ID": ("boise", "meridian, id"),
    "New Orleans, LA": ("new orleans",),
    "Hartford, CT": ("hartford", "stamford", "shelton", "norwalk"),
    "Buffalo, NY": ("buffalo", "rochester, ny", "syracuse"),
}
_CITY_TO_METRO = {city: metro for metro, cities in _METROS.items() for city in cities}

# Metros that legitimately span state lines. Everything else is confined to the state in
# its own label, which is what stops "Newark, DE" resolving to the New York metro (Newark,
# NJ is in that list) and "Columbia, MD" / "Arlington, TX" landing in the wrong city.
_METRO_EXTRA_STATES = {
    "New York, NY": {"NJ", "CT", "PA"},
    "Washington, DC": {"VA", "MD"},
    "Philadelphia, PA": {"NJ", "DE"},
    "Kansas City, MO": {"KS"},
    "Portland, OR": {"WA"},
    "Chicago, IL": {"IN", "WI"},
    "St. Louis, MO": {"IL"},
    "Charlotte, NC": {"SC"},
    "Boston, MA": {"NH", "RI"},
    "Cincinnati, OH": {"KY", "IN"},
    "Memphis, TN": {"MS", "AR"},
}
_METRO_STATES = {
    m: {m.rsplit(", ", 1)[-1]} | _METRO_EXTRA_STATES.get(m, set()) for m in _METROS
}

_REMOTE_POS_RE = re.compile(
    r"\b(?:(?:fully|100%|entirely|permanently)\s+remote"
    r"|remote[- ]first|remote[- ]friendly"
    r"|work(?:ing)? from home|telecommut(?:e|ing)"
    r"|remote (?:position|role|opportunity|job|work arrangement))\b", re.I)
# "This is NOT a remote position" / "no telecommuting" must not read as remote.
_REMOTE_NEG_RE = re.compile(r"\b(?:not|non|no|isn'?t|aren'?t|cannot|can'?t|without|neither)\b", re.I)

_loc_cache = {}


def _metro_for(tokens, state):
    """Match the most specific city token to a metro. Tries '<city>, <st>' first so the
    'Arlington' / 'Columbia' / 'Madison' collisions resolve correctly, and rejects any metro
    that doesn't contain the state we resolved — otherwise 'Newark, DE' lands in New York."""
    for t in tokens:
        low = t.lower().strip()
        if not low:
            continue
        if state:
            m = _CITY_TO_METRO.get("%s, %s" % (low, state.lower()))
            if m:
                return m
        m = _CITY_TO_METRO.get(low)
        if m and (not state or state in _METRO_STATES.get(m, set())):
            return m
    return ""


_LOC_TIDY_COMMA = re.compile(r"\s*,\s*")
_LOC_TIDY_SPACE = re.compile(r"\s{2,}")


def tidy_location(raw):
    """A job's location string, punctuated the way the rest of the feed punctuates it.

    PRESENTATION ONLY — parse_location below is what the filters use, and it is untouched. One
    board writes "Santa Clara,CA" with no space after the comma, and that string was passed
    straight through to the card, so a single board's formatting defect shipped to the UI and sat
    beside neighbours reading "Redmond, WA, US", "Windsor, CT" and "Boston, MA".

    Deliberately conservative: separators and runs of whitespace, nothing else. It does not
    reorder, expand or re-case anything, because the raw string is frequently the only truthful
    thing we have about where a job is.
    """
    s = (raw or "").strip().strip(",;/ ")
    if not s:
        return ""
    s = _LOC_TIDY_COMMA.sub(", ", s)
    s = _LOC_TIDY_SPACE.sub(" ", s)
    return s.strip().strip(",")


def parse_location(raw, jd=""):
    """Normalize a job's free-text location into {city, state, metro, remote}.

    The boards give us ~4,100 distinct spellings for a few hundred real places, so this
    resolves what can be resolved and leaves the rest blank rather than guessing:
      state  — a bare 2-letter code token wins, else a full state name anywhere in the string
      metro  — a known city/suburb mapped to its metro, else '' (the state filter still works)
      city   — the first token that isn't a state, country, or the word 'remote'
      remote — 'remote' in the location, or an unambiguous remote phrase in the JD

    Cached on (raw, whether the JD looks remote) since the same string repeats thousands
    of times across the corpus.
    """
    raw = (raw or "").strip()
    jd_remote = bool(jd) and _jd_says_remote(jd)
    ck = (raw, jd_remote)
    if ck in _loc_cache:
        return _loc_cache[ck]

    low = raw.lower()
    out = {"city": "", "state": "", "metro": "", "remote": ("remote" in low) or jd_remote}

    tokens = [t for t in _LOC_SPLIT_RE.split(raw) if t.strip()]
    if _DC_RE.search(raw):
        out["state"] = "DC"
    # A 2-letter code is the most reliable signal, so look for one before place names.
    if not out["state"]:
        for t in tokens:
            m = _LEAD_CODE_RE.match(t.strip())
            if m and m.group(1).upper() in _STATE_CODES:
                out["state"] = m.group(1).upper()
                break
    if not out["state"]:
        m = _STATE_NAMES_RE.search(low)
        if m:
            out["state"] = _STATES[m.group(1)]
    if not out["state"]:
        m = _TRAIL_CODE_RE.search(raw)
        if m and m.group(1) in _STATE_CODES:
            out["state"] = m.group(1)

    skip = {"us", "usa", "u s", "u s a", "united states", "united states of america",
            "remote", "hybrid", "onsite", "on-site", "north america", "anywhere", "various",
            "multiple locations", "flexible"}
    for t in tokens:
        c = t.strip()
        cl = c.lower()
        if not c or cl in skip or cl in _STATES:
            continue
        # Skip a token that IS the state ("MA", "MA (Remote)", "CA United States").
        lead = _LEAD_CODE_RE.match(c)
        if lead and lead.group(1).upper() in _STATE_CODES:
            continue
        out["city"] = c
        break

    out["metro"] = _metro_for(tokens, out["state"])
    if not out["metro"] and out["city"] and out["state"]:
        out["metro"] = "%s, %s" % (out["city"], out["state"])

    _loc_cache[ck] = out
    return out


def _jd_says_remote(jd):
    """True when the JD unambiguously offers remote work. Every candidate phrase is
    rejected if a negation sits just before it, so 'this is not a remote position' —
    which is common — doesn't flip the flag on."""
    if not jd:
        return False
    for m in _REMOTE_POS_RE.finditer(jd[:20000]):
        before = jd[max(0, m.start() - 45):m.start()]
        if not _REMOTE_NEG_RE.search(before):
            return True
    return False


# ------------------------------------------------------------
# SALARY parsing — no board hands us a pay field we keep, but US pay-transparency laws
# mean ~a third of JDs state a range in the text. Pull it out of the JD we already store.
# ------------------------------------------------------------
# A money amount we trust: comma-grouped ($120,000) or K-suffixed ($120K / $120.5k).
_MONEY = r"\$\s?\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\$\s?\d{2,3}(?:\.\d)?\s?[kK]\b"
_SALARY_RANGE_RE = re.compile(r"(" + _MONEY + r")\s*(?:-|–|—|to|and|through)\s*(" + _MONEY + r")", re.I)
_HOURLY_RANGE_RE = re.compile(
    r"\$\s?(\d{1,3}(?:\.\d{1,2})?)\s*(?:-|–|—|to)\s*\$?\s?(\d{1,3}(?:\.\d{1,2})?)"
    r"\s*(?:per\s+hour|/\s?h(?:r|our)|an\s+hour|hourly)", re.I)
_HOURLY_HINT_RE = re.compile(r"\b(?:per\s+hour|/\s?h(?:r|our)|an\s+hour|hourly)\b", re.I)

# Plausibility gates. Below/above these a "$" figure is something else — a revenue
# number, a signing bonus, a 401(k) cap, a tuition figure.
_ANNUAL_MIN, _ANNUAL_MAX = 15000, 1000000
_HOURLY_MIN, _HOURLY_MAX = 7, 500


def _money_to_int(s):
    """'$120,000' -> 120000 · '$120K' -> 120000 · '$120.5k' -> 120500."""
    t = s.replace("$", "").replace(",", "").replace(" ", "").lower()
    try:
        if t.endswith("k"):
            return int(round(float(t[:-1]) * 1000))
        return int(round(float(t)))
    except ValueError:
        return 0


def parse_salary(jd):
    """Pull a pay range out of a job description.

    Returns {"min": int|None, "max": int|None, "period": "year"|"hour"|""}. Annual ranges
    are tried first and the first plausible one wins — JDs frequently mention other dollar
    figures (equity, bonuses, revenue) after the pay range, never before it.
    """
    empty = {"min": None, "max": None, "period": ""}
    if not jd:
        return empty
    head = jd[:40000]

    for m in _SALARY_RANGE_RE.finditer(head):
        lo, hi = _money_to_int(m.group(1)), _money_to_int(m.group(2))
        if lo > hi:
            lo, hi = hi, lo
        if not (lo and hi):
            continue
        # A comma-grouped pair this small is an hourly rate written oddly, or not pay at all.
        if _ANNUAL_MIN <= lo <= _ANNUAL_MAX and _ANNUAL_MIN <= hi <= _ANNUAL_MAX:
            # "$45,000 - $55,000 per hour" never means per hour; trust the magnitude.
            return {"min": lo, "max": hi, "period": "year"}

    for m in _HOURLY_RANGE_RE.finditer(head):
        try:
            lo, hi = float(m.group(1)), float(m.group(2))
        except ValueError:
            continue
        if lo > hi:
            lo, hi = hi, lo
        if _HOURLY_MIN <= lo <= _HOURLY_MAX and _HOURLY_MIN <= hi <= _HOURLY_MAX:
            return {"min": int(round(lo)), "max": int(round(hi)), "period": "hour"}

    return empty


def salary_label(smin, smax, period):
    """Card-ready text for a pay range: '$120k–$150k' or '$25–$35/hr'. '' when unknown."""
    if not smin and not smax:
        return ""
    if period == "hour":
        return "$%d–$%d/hr" % (smin, smax) if smax and smax != smin else "$%d/hr" % (smin or smax)

    def k(v):
        return "$%gk" % round(v / 1000.0, 1) if v < 1000000 else "$%.1fM" % (v / 1000000.0)
    if smin and smax and smin != smax:
        return "%s–%s" % (k(smin), k(smax))
    return k(smin or smax)


# ------------------------------------------------------------
# ROLE TRACK — "is this a builder job or a manager job?"
#
# The corpus carries two genuinely different careers since the scraper's title filter was
# widened to software engineering (2026-08-01): ~17k PM/program/analyst/ops roles and ~8k
# software/data/ML/infra roles. Nobody is job-hunting for both at once, so the feed offers a
# one-click split and this is the single definition it splits on. The email digest reads the
# same function, so "my search" can't mean two different things.
#
# Relationship to scraper.INCLUDE: that list decides what we COLLECT, this decides how a
# collected job is FILED. They're deliberately separate — widening what we scrape shouldn't
# silently re-file existing jobs, and this has to classify rows scraped before it existed.
#
# MANAGEMENT WINS TIES, and that ordering is the whole trick: "Technical Program Manager",
# "Machine Learning Product Manager" and "Engineering Program Manager" all carry software
# words but are management jobs. A title is only "dev" when it has a builder phrase and NO
# management phrase. Anything unrecognised falls to "mgmt" so the two buckets always add up
# to the whole feed — a job that matched neither (say "Operations Intern") must still appear
# somewhere, or turning the filter on would silently swallow it.
_MGMT_TITLE_RE = re.compile(r"""\b(?:
    (?:project|program|product|portfolio|delivery|engagement|release\s+train)\s+
        (?:manager|management|coordinator|specialist|administrator|owner|analyst|associate|lead)
  | project\s+controls? | cost\s+analyst | project\s+planner | planning\s+analyst
  | (?:project|master|program)\s+scheduler
  | scrum\s+master | agile\s+coach | pmo | chief\s+of\s+staff
  | business\s+analyst | business\s+operations | business\s+process
  | operations\s+(?:manager|analyst|coordinator|specialist|associate|lead)
  | (?:supply\s+chain|logistics|procurement)\s+(?:analyst|manager|coordinator|specialist)
  | implementation\s+(?:manager|specialist|consultant|analyst)
  | product\s+(?:strategist|strategy|operations)
)\b""", re.I | re.X)

_DEV_TITLE_RE = re.compile(r"""\b(?:
    software\s+(?:engineer\w*|developer|development|test|quality|architect)
  | (?:web|mobile|application|applications|game|salesforce|java|python|sql|etl|bi|rpa|ios|
       android|javascript|react|node|dotnet|net|c\#|full\s*stack|frontend|backend|cloud)\s+
       (?:developer|development|engineer)
  | (?:front|back)[-\s]?end | full[-\s]?stack | fullstack
  | sde | swe | sdet | dba | programmer
  | (?:data|analytics|platform|infrastructure|systems?|network|release|build|automation|test|
       qa|security|integration|api|cloud|devops|ml|ai|mobile|ios|android|firmware|embedded)\s+
       engineer\w*
  | data\s+(?:scientist|analyst|engineering|science)
  | machine\s+learning | deep\s+learning | computer\s+vision | artificial\s+intelligence
  | applied\s+scientist | ml\s*ops | mlops | nlp | prompt\s+engineer\w*
  | business\s+intelligence | analytics\s+engineer\w*
  | database\s+(?:administrator|engineer|developer)
  | dev\s?ops | dev\s?sec\s?ops | site\s+reliability | sre | kubernetes
  | quality\s+assurance\s+engineer\w* | test\s+automation | application\s+security
  | systems?\s+(?:analyst|development)
  | computer\s+science | embedded\s+software | engineering\s+manager
)\b""", re.I | re.X)


# ------------------------------------------------------------
# TITLES ARE WRITTEN IN SHORTHAND AND EVERY MATCHER HERE READS THEM LITERALLY.
#
# Applied Materials posts "Tech Proj/Prg Mgmt" -- four of them live on 2026-09-10, beside
# "Non-Tech Proj/Prg Mgmt" and "Tech Proj/Prog Manager IV". Every title matcher in this project
# is whole-phrase and re.escape'd, so "proj" is not "project", "prg" is not "program" and "mgmt"
# is not "management". That posting failed INCLUDE and then failed pm_title_gate -- and a title
# that fails BOTH is never fetched and never reconsidered, because fill_missing_jds only buys a
# description for titles the gate lets through. Measured over the 80 distinct US titles in that
# employer's OWN "Project/Program Management" job family: 17 dropped, 10 of them gate-blocked.
#
# THIS IS A SHADOW COPY, USED ONLY FOR MATCHING. The stored and displayed title is never
# touched -- a card still reads "Tech Proj/Prg Mgmt", which is what the employer called it.
#
# TWO RULES, AND WHICH ONE DOES THE WORK MATTERS. Punctuation-as-separator was measured on its
# own on 2026-08-20 and REFUSED: +8 rows, all junk (the note above scraper._REVERSED_RE). It is
# safe here only because it is not what earns the match -- the EXPANSION is, and the separator
# merely exposes the token. "Proj/Prg" has to become two words before either half can be
# recognised, and neither half matches anything on its own.
#
# THE COMMA IS NOT A SEPARATOR, deliberately. scraper._REVERSED_RE is anchored on it ("Manager,
# Projects"), so turning it into a space would delete the one structural pattern the title
# filter has. Leaving it alone also hands the reversed form the expansion for free: "Dir,
# Programs" normalises to "director, Programs" and matches where it never used to. The ASCII
# hyphen is left alone for a similar reason -- "co-op", "full-stack", "part-time" and "roll-out"
# are each a single INCLUDE / EXCLUDE / hint entry and splitting them would lose all four.
#
# WHAT IS DELIBERATELY ABSENT: discipline words. mech->mechanical, elec->electrical and
# chem->chemical would each make EXCLUDE fire MORE, since all three are bare EXCLUDE entries.
# That is a different change with the opposite risk, and this one is about jobs we are missing.
_TITLE_ABBR = {
    "proj": "project", "prj": "project", "projs": "projects",
    "prg": "program", "prgm": "program", "prog": "program", "pgm": "program",
    "progs": "programs",
    "mgmt": "management", "mgt": "management", "mgr": "manager", "mgrs": "managers",
    "coord": "coordinator", "coords": "coordinators", "spec": "specialist",
    "admin": "administrator", "asst": "assistant", "dir": "director",
    "sr": "senior", "jr": "junior", "assoc": "associate",
    "eng": "engineer", "engr": "engineer", "dev": "developer", "devs": "developers",
    "anlyst": "analyst", "anlst": "analyst", "bus": "business", "sys": "systems",
    "ops": "operations", "prod": "product",
}
# tech->technical WAS HERE AND WAS MEASURED OUT. On the 34,895 US postings of the 2026-09-10
# sweep it admitted nothing at all -- "Tech Proj/Prg Mgmt" matches on "program management", not
# on the word Tech -- while opening pm_title_gate for 294 rows that are almost entirely
# "Tech" AS A NOUN: 43 "Mechatronics & Robotics Tech", 21 "IT Support Associate II", plus DCO
# Tech, QC Tech, Controls Tech and "PCT (Patient Care Tech)". Those are technicians, EXCLUDE
# already names that word, and each one would have cost a description fetch out of a budget
# with better candidates in it. An expansion has to earn a match, not just widen a gate.
# The two dashes are the ones employers reach for as a comma substitute: "Director - Program
# Management" arrives with U+2013 about as often as with an ASCII hyphen.
# str.translate rather than a per-character generator: this runs over every distinct title in
# the corpus on a cold row build, and the table form measured 8.0 us/title against 10.8.
_TITLE_SEPS = frozenset("/&+|" + chr(92) + "\u2013\u2014")
_TITLE_SEP_MAP = {ord(c): " " for c in _TITLE_SEPS}
_TITLE_WORD_RE = re.compile(r"[A-Za-z]+")
_TITLE_WS_RE = re.compile(r"\s+")
# SHORTHAND THAT IS REALLY ONE WORD WITH A SPACE IN IT. Joined BEFORE expansion, because
# expanding either half destroys it: "dev ops" is itself an INCLUDE phrase, and dev->developer
# together with ops->operations turned "Dev Ops Engineer" into "developer operations Engineer",
# which matches nothing at all. Measured over the 25,973 distinct live titles, those were the
# ONLY four rows normalisation cost -- everything else it changed, it changed for the better.
_TITLE_COMPOUND = ((re.compile(r"\bdev\s+sec\s+ops\b", re.I), "devsecops"),
                   (re.compile(r"\bdev\s+ops\b", re.I), "devops"))


@lru_cache(maxsize=60000)
def normalize_title(title):
    """A title rewritten FOR MATCHING ONLY: separators split, known shorthand expanded.

    Bounded like _role_cache and for the same reason -- titles repeat heavily across a corpus,
    and this runs on every row of a feed render by way of roles_for_title.
    """
    if not title:
        return ""
    # WHITESPACE FIRST, and it is not cosmetic. scraper._dump_field's docstring already says
    # "job titles really do contain tabs and newlines"; nothing collapsed them before matching,
    # so a phrase list built with literal single spaces could not see across one. Found from a
    # real Amazon posting -- "Manufacturing System<TAB>Development Engineer" was dropped as
    # having no keyword while "system development" sat in INCLUDE the whole time.
    s = _TITLE_WS_RE.sub(" ", title.translate(_TITLE_SEP_MAP)).strip()
    for rx, whole in _TITLE_COMPOUND:
        s = rx.sub(whole, s)
    return _TITLE_WORD_RE.sub(
        lambda m: _TITLE_ABBR.get(m.group(0).lower(), m.group(0)), s)


# ------------------------------------------------------------
# ROLE FAMILIES — "what kind of job do you want", answerable
#
# role_track sorts every posting into dev or mgmt, which is two buckets for the 4,296 distinct
# title families measured in this corpus. That is enough to split a feed and nowhere near enough
# to say what someone is looking for.
#
# Each family is a set of phrases people would consider the SAME job. Grouped from the real
# title distribution rather than invented, because a role nobody is hiring for makes the feed
# look broken when it returns nothing. Counts from 2026-08-09 are in the comments as a record of
# why each earned a slot; they drift, and web.role_counts() shows the live number in the picker.
#
# A title may match several families on purpose — a "Technical Program Manager" is both, and
# someone who ticked either should see it. Matching is whole-phrase and case-insensitive, so
# "Project Management" does not make everything a Project Manager.
# Sections, so 22 options read as four short lists instead of one long one. Order is the order
# they render in.
ROLE_GROUPS = [("deliver", "Product, Program & Delivery"),
               ("eng", "Engineering"),
               ("data", "Data & AI"),
               ("biz", "Business & Operations")]
ROLE_FAMILIES = [
    # 2026-09-08: the 109 INCLUDE phrases below were admitted by the scraper and claimed by
    # no family, so a title matching only one of them came back from roles_for_title as ()
    # and was deleted by ANY role selection. Grouped by the family each was admitted for;
    # test_title_filter.test_every_include_phrase_is_claimed_by_a_family now enforces the
    # invariant over the whole list, with only the 15 LEVEL words ("intern", "new grad")
    # allowed to belong to no family -- those describe a rung, not a job.
    # 2026-08-20: each family below gained the phrases the title filter gained on the same day,
    # because these two vocabularies are read by different halves of the app and a title the
    # scraper now KEEPS but no family CLAIMS is invisible to anyone who ticks a role chip --
    # roles_for_title returns () and roles_match only ignores that when nothing is selected.
    # "Release Train Engineer" is the cautionary tale: ROLE_FAMILIES listed it under `scrum`
    # while EXCLUDE was dropping it outright, and the disagreement went unnoticed for weeks.
    ("pm",         "Project Manager",       "deliver",   # 954 + 169 + 43
     ("project manager", "project management", "construction project manager",
      "technical project manager", "project lead", "project controls",
      "project mgr", "proj mgr", "pmo", "epmo", "project management office",
      "project analyst", "project specialist", "project support", "project administrator",
      "project associate", "project portfolio", "project control", "project control analyst",
      "controls analyst", "cost controls", "cost control", "cost analyst", "scheduler",
      "project scheduler", "master scheduler", "planner scheduler", "project planner",
      "planning analyst", "resource planner", "proj manager", "portfolio manager")),
    ("program",    "Program Manager",       "deliver",   # 518 + 347 + 50
     ("program manager", "technical program manager", "program management", "tpm",
      "program mgr", "prog mgr", "pgm mgr", "program analyst", "program administrator",
      # British spelling, and the one misspelling that measured non-zero (3 Amazon postings).
      "programme manager", "programme management", "program manger",
      "program specialist", "program lead")),
    # 2026-09-08: was five phrases against pm's fifteen, and the five titles below were all
    # in scraper.INCLUDE -- STORED, and claimed by no family. Measured on the live corpus,
    # 465 product-titled rows came back from roles_for_title as (), and roles_match only
    # ignores that when nothing is selected: ticking this very chip deleted every one of them
    # from the feed AND from the email digest. The comment at the head of ROLE_FAMILIES
    # predicted exactly this, and the property test in test_title_filter.py now enforces it
    # over the whole INCLUDE list rather than by example.
    ("product",    "Product Manager",       "deliver",   # 905 + 57 + 52
     ("product manager", "technical product manager", "product owner",
      "associate product manager", "product management", "product managers",
      "product owners", "associate product owner", "product lead",
      # In INCLUDE since the product block was written; in no family until now.
      "product analyst", "product coordinator", "product operations",
      "product strategist", "product strategy", "product mgr", "prod mgr",
      "product mgmt")),
    ("coordinator", "Project / Program Coordinator", "deliver",   # 126 + 50
     ("project coordinator", "program coordinator", "operations coordinator",
      "project administrator", "projects coordinator", "programs coordinator",
      "programme coordinator")),
    ("scrum",      "Scrum Master / Agile",  "deliver",
     ("scrum master", "agile coach", "release train engineer", "agile delivery",
      "product owner",
      "release train")),
    ("consultant", "Implementation / Solutions Consultant", "deliver",
     ("implementation consultant", "implementation specialist", "implementation manager",
      "solutions consultant", "solutions architect", "technical consultant",
      "implementation")),
    # NEW 2026-08-20. Delivery and change work was reaching the corpus with no family to answer
    # to: "Product Delivery Manager" at JPMorgan, "Service Delivery Manager" at NetApp and
    # "Finance Manager - Transformation (PMO)" at Swissport all turned up in the description
    # rule's calibration sample wearing no chip at all.
    ("delivery",   "Delivery / Engagement Manager", "deliver",
     ("delivery manager", "delivery lead", "delivery analyst", "service delivery",
      "technical delivery", "engagement manager", "deployment manager",
      "integration manager")),
    ("transform",  "Change & Transformation", "deliver",
     ("change manager", "change management", "change analyst", "business transformation",
      "transformation manager", "process improvement", "process analyst",
      "strategic initiatives", "initiatives manager", "chief of staff")),

    ("swe",        "Software Engineer",     "eng",       # 2144 + 444 + 75 + 63 + 122 + 70 + 44
     ("software engineer", "software developer", "software development engineer",
      "software dev engineer", "sde", "full stack developer", "fullstack developer",
      "backend engineer", "back end engineer", "frontend engineer", "front end engineer",
      "embedded software engineer", "platform software engineer", "application developer",
      "web developer",
      "software engineering", "software development", "software dev", "swe", "programmer",
      "programmer analyst", "applications developer", "computer science", "web development",
      "web engineer", "front-end engineer", "front end developer", "front-end developer",
      "frontend developer", "front end software", "frontend software", "back-end engineer",
      "back end developer", "back-end developer", "backend developer", "backend software",
      "back end software", "back-end software", "full stack", "full-stack", "fullstack",
      "ui engineer", "ui developer", "javascript developer", "react developer",
      "mobile engineer", "mobile developer", "mobile software", "ios engineer", "ios developer",
      "android engineer", "android developer", "game developer", "embedded software",
      "java developer", "python developer", "net developer", "dotnet developer", "c# developer",
      "salesforce developer", "sql developer", "rpa developer", "api engineer",
      "integration engineer", "system development", "application development",
      "systems development", "systems development engineer")),
    ("devops",     "DevOps / SRE",          "eng",       # 114 + 129 + 41 + 41
     ("devops engineer", "site reliability engineer", "sre", "platform engineer",
      "infrastructure engineer", "cloud engineer", "devsecops engineer",
      "devops", "dev ops", "devsecops", "site reliability", "cloud developer",
      "cloud support engineer", "kubernetes", "release engineer", "build engineer")),
    ("qa",         "QA / Test Engineer",    "eng",       # 130 + 88
     ("qa engineer", "test engineer", "quality assurance engineer", "automation engineer",
      "test automation engineer", "sdet",
      "test automation", "qa analyst", "software test", "software quality")),
    ("security",   "Security Engineer",     "eng",       # 78
     ("security engineer", "application security", "information security analyst",
      "cybersecurity analyst", "security analyst")),
    ("systems",    "Systems Engineer",      "eng",       # 373
     ("systems engineer", "system engineer", "systems analyst", "solutions engineer")),
    ("network",    "Network Engineer",      "eng",       # 76
     ("network engineer", "network administrator", "systems administrator")),
    ("apps",       "Applications Engineer", "eng",       # 44
     ("applications engineer", "application engineer", "field applications engineer")),
    ("engmgr",     "Engineering Manager",   "eng",       # 50
     ("engineering manager", "software engineering manager", "development manager",
      "technical lead", "tech lead")),

    ("dataeng",    "Data Engineer",         "data",      # 282
     ("data engineer", "analytics engineer", "etl developer", "data platform engineer",
      "data engineering", "big data", "etl engineer", "database administrator", "dba",
      "database engineer", "database developer")),
    ("datasci",    "Data Scientist",        "data",      # 238 + 101
     ("data scientist", "applied scientist", "research scientist", "data science",
      "machine learning scientist")),
    ("dataanalyst", "Data Analyst",         "data",      # 86
     ("data analyst", "analytics analyst", "reporting analyst", "bi analyst",
      "business intelligence analyst",
      "bi developer", "business intelligence")),
    ("ml",         "Machine Learning / AI", "data",      # 169 + 73 + 60
     ("machine learning engineer", "ml engineer", "ai engineer", "deep learning engineer",
      "computer vision engineer", "nlp engineer", "mlops engineer",
      "artificial intelligence engineer",
      "machine learning", "mlops", "ai developer", "artificial intelligence", "deep learning",
      "computer vision", "prompt engineer")),

    ("ba",         "Business Analyst",      "biz",       # 198
     ("business analyst", "business systems analyst", "business process analyst",
      "requirements analyst")),
    ("ops",        "Operations Manager",    "biz",       # 381 + 70
     ("operations manager", "operations lead", "branch operations", "business operations",
      "operations supervisor",
      "operations analyst", "operations specialist", "operations management")),
    ("finance",    "Financial Analyst",     "biz",       # 328
     ("financial analyst", "finance analyst", "fp&a analyst", "budget analyst")),
    ("supply",     "Supply Chain / Logistics", "biz",    # 54
     ("supply chain manager", "supply chain analyst", "logistics manager",
      "procurement analyst", "supply chain",
      "logistics analyst")),
]
ROLE_KEYS = tuple(k for k, _l, _g, _p in ROLE_FAMILIES)
ROLE_LABELS = {k: lab for k, lab, _g, _p in ROLE_FAMILIES}

# ------------------------------------------------------------
# WHICH ROLE COMES FIRST WHEN TWO POSTINGS ARE OTHERWISE EQUAL.
#
# The feed sorted on ONE integer: `score` is int(pct), so ~39,000 active rows fell into at most
# 101 buckets -- about 400 rows a bucket -- and Python's sort is stable, so inside a bucket the
# input order survived untouched. That input order is db._fetch_all's `order=url` default, one
# employer is one ATS host, and URL order is therefore EMPLOYER order. That, and not any
# grouping rule, is why the feed showed walls of one company. "Newest" piled the same way for a
# different reason: a board is scraped in one pass, so dozens of rows share a date and the tie
# falls through to the same place.
#
# This is the tie-break that was missing. It is the owner's order, given 2026-09-10: project,
# then product, then program, then everything else, grouped the way ROLE_GROUPS already groups
# them. It ranks WITHIN the chosen sort and never over it -- "Newest" still means newest, and a
# 90% match still outranks an 80% one. See web._sort_key.
#
# A row matching several families takes its BEST rank: roles_for_title returns every family a
# title belongs to, and a "Technical Program Manager" is genuinely both.
ROLE_PRIORITY = (
    "pm", "product", "program", "coordinator", "scrum", "delivery", "transform", "consultant",
    "ba", "ops", "dataanalyst", "supply", "finance",
    "datasci", "dataeng", "ml",
    "swe", "engmgr", "devops", "qa", "systems", "apps", "network", "security",
)
# A family added to ROLE_FAMILIES and forgotten here would silently sort last for everyone,
# which looks like a ranking opinion rather than an omission. Same standard norms.py holds
# _meta.role_keys to: editing the vocabulary FAILS rather than quietly re-ranking the feed.
assert set(ROLE_PRIORITY) == set(ROLE_KEYS), (
    "ROLE_PRIORITY must name every ROLE_FAMILIES key exactly once: missing %s, unknown %s"
    % (sorted(set(ROLE_KEYS) - set(ROLE_PRIORITY)), sorted(set(ROLE_PRIORITY) - set(ROLE_KEYS))))
_ROLE_RANK = {k: i for i, k in enumerate(ROLE_PRIORITY)}
# One past the end, so "we could not tell what this is" sorts after everything we could.
# 16.0% of live titles are here, measured 2026-09-10 over 25,973 distinct active titles.
ROLE_RANK_NONE = len(ROLE_PRIORITY)


# Memoised on the role tuple, not stored on the row -- and that is a deliberate choice, not an
# oversight. row_cache/ is keyed on (jobs_fingerprint, _derived_signature) and _derived_signature
# hashes THREE DATA FILES, not this module, so a rank baked into a built row would survive an
# edit to ROLE_PRIORITY until the next scrape moved the corpus. That is the same staleness class
# CLAUDE.md refuses for `roles` and `track`. Measured over 39,000 rows there was nothing to buy
# anyway: the memo sorts in 25.9 ms against 24.9 ms for a pre-baked integer, because a corpus
# holds only a handful of distinct role tuples (8 in the live one).
_role_rank_memo = {}


def role_rank(roles):
    """Where a row's best role sits in ROLE_PRIORITY. Lower is earlier; no role sorts last.

    Takes the ROLES ALREADY ON THE ROW (_build_row emits them) rather than a title, so nothing
    on the sort path has to look at text.
    """
    key = tuple(roles or ())
    hit = _role_rank_memo.get(key)
    if hit is not None:
        return hit
    best = ROLE_RANK_NONE
    for k in key:
        r = _ROLE_RANK.get(k)
        if r is not None and r < best:
            best = r
    if len(_role_rank_memo) < 4096:        # bounded like _role_cache, same reason
        _role_rank_memo[key] = best
    return best
# One whole-phrase regex per family, alternatives longest-first so the most specific wins the
# match position. Built once: this runs over every row of the corpus on a feed render.
_ROLE_RES = {k: re.compile(r"\b(?:%s)\b" % "|".join(
    re.escape(p) for p in sorted(phr, key=len, reverse=True)), re.I)
    for k, _lab, _g, phr in ROLE_FAMILIES}
_role_cache = {}


def role_families_grouped():
    """[(group_key, group_label, [(key, label, phrases), ...]), ...] in render order."""
    return [(g, lab, [(k, l, p) for k, l, gg, p in ROLE_FAMILIES if gg == g])
            for g, lab in ROLE_GROUPS]


def roles_for_title(title):
    """Every role family this title belongs to, as a tuple of keys ('' -> ())."""
    t = (title or "").strip()
    if not t:
        return ()
    hit = _role_cache.get(t)
    if hit is None:
        # THE SHADOW TITLE, so "Tech Proj/Prg Mgmt" lands in pm/program exactly like the
        # spelled-out form does. Admitting a posting the scraper now keeps and then answering ()
        # here would bury it twice over: () is deleted by any role selection, and it sorts last
        # under the role-priority key. The cache stays keyed on the RAW title, which is what
        # every caller holds.
        hit = tuple(k for k in ROLE_KEYS if _ROLE_RES[k].search(normalize_title(t)))
        if len(_role_cache) < 60000:          # bounded: titles repeat heavily across the corpus
            _role_cache[t] = hit
    return hit


def parse_roles_pref(raw):
    """A stored/posted roles value -> a validated, canonically ordered tuple of keys.

    Same shape as parse_visa_pref: junk dropped, duplicates collapsed, order fixed so two
    equivalent selections can't produce two different stored strings.
    """
    if isinstance(raw, (list, tuple)):
        vals = [str(x) for x in raw]
    else:
        vals = str(raw or "").replace(" ", "").split(",")
    want = {v for v in vals if v in ROLE_LABELS}
    return tuple(k for k in ROLE_KEYS if k in want)


DELIVER_ROLE_KEYS = frozenset(k for k, _lab, grp, _p in ROLE_FAMILIES if grp == "deliver")


def roles_match(row_roles, wanted, jd_admit=False):
    """Does this posting belong to any family the user picked? Empty selection matches all.

    OR across the picks, like the visa filter: someone who ticks Project Manager and Data
    Analyst wants both, not the intersection (which would be almost nothing).

    `jd_admit` rows are the exception, and without it the description path is half-invisible. A
    posting kept because its DESCRIPTION reads like delivery work has no family, because families
    are read off the title and its title is the reason it needed rescuing -- a genuine
    "Coordinator II" comes back with (). So it is matched against any selection drawn ENTIRELY
    from the delivery group: the admission rule already established that it is delivery work, it
    just cannot say which sub-family. Tick "Data Analyst" alone and it stays hidden, because
    nothing established that.
    """
    if not wanted:
        return True
    if set(row_roles or ()) & set(wanted):
        return True
    return bool(jd_admit and wanted and set(wanted) <= DELIVER_ROLE_KEYS)


def role_track(title):
    """Which career track a posting belongs to: 'dev' (software/data/infra IC work) or
    'mgmt' (project/program/product/ops/analyst work).

    Never returns empty — every job lands in exactly one bucket, so the feed's two
    one-click filters partition the corpus instead of hiding the leftovers.
    """
    t = normalize_title(title or "")
    if _MGMT_TITLE_RE.search(t):
        return "mgmt"
    return "dev" if _DEV_TITLE_RE.search(t) else "mgmt"


# ------------------------------------------------------------
# "IS THIS A PROJECT-MANAGEMENT JOB?" -- answered from the DESCRIPTION, not the title.
#
# The title filter is a scrape-time gate with nothing but the title to go on, and plenty of
# employers title a delivery role "Coordinator II" or "Business Operations Specialist". This is
# the second opinion: it reads the posting and asks whether the WORK is project/programme
# delivery, whatever the title happens to say.
#
# IT ANSWERS "IS THIS THAT JOB", NOT "IS THIS A GOOD FIT FOR THE USER". Admission must not
# depend on a per-user score floor -- one live account stores min: 0, so a fit-based gate would
# admit everything for that account and less for a stricter one, and what gets STORED has to be
# the same for everybody. Ranking is score_against's job and stays per-user.
#
# TWO TIERS, because one word list cannot separate "runs the project" from "works on a team that
# happens to have sprints". A software JD says sprint, backlog, roadmap and cross-functional as a
# matter of course, so those can never be sufficient on their own: they are SUPPORT. The ANCHORS
# are phrases that describe OWNING the work, and at least PM_MIN_ANCHORS of them are required.
#
# DISTINCT phrases are counted, not occurrences. A JD that says "stakeholder" eleven times is
# one signal, not eleven, and counting hits would let a single repeated word carry a posting.
PM_ANCHORS = (
    "project management", "program management", "programme management", "portfolio management",
    "project manager", "program manager", "project coordinator", "program coordinator",
    "project plan", "project planning", "project schedule", "project scheduling",
    "project charter", "project lifecycle", "project delivery", "program delivery",
    "project governance", "project controls", "project budget", "project team",
    "project stakeholders", "project documentation", "project risks", "project status",
    "work breakdown structure", "statement of work", "risk register", "raid log",
    "gantt", "critical path", "change request", "change control", "steering committee",
    "stakeholder management", "scope management", "resource planning", "capacity planning",
    "milestone tracking", "status report", "status reports", "status reporting",
    "pmo", "pmp", "capm", "prince2", "csm", "scaled agile", "safe agile",
    "scrum master", "product owner", "product roadmap", "release planning", "sprint planning",
    "backlog prioritization", "backlog management", "product backlog",
    "vendor management", "contract management", "change management", "organizational change",
    "process improvement", "continuous improvement", "requirements gathering",
    "business requirements document", "cross-functional projects", "cross functional projects",
    "kickoff meeting", "kick-off meeting", "on time and within budget", "on time and on budget",
    # Added after the first calibration run, which showed real Program Managers and Product
    # Owners at Zimmer Biomet, J&J, U.S. Bank and JPMorgan being MISSED on one anchor apiece
    # while carrying 9-11 support words. The gate was not too strict; the anchor list was too
    # short. These are all OWNERSHIP phrases -- deliberately not "user stories", "acceptance
    # criteria", "definition of done", "daily standup" or "epics", which every software JD
    # carries and which belong in support if anywhere.
    "manage projects", "managing projects", "manage multiple projects", "project execution",
    "project initiation", "project closure", "project scope", "project timeline",
    "project timelines", "project deliverables", "project milestones", "project coordination",
    "project tracking", "project reporting", "project management office", "project managers",
    "program execution", "program governance", "program roadmap", "program managers",
    "portfolio of projects", "intake process", "resource allocation",
    "lessons learned", "dependency management", "risk and issue",
    "milestone plan", "scope creep", "change order", "project financials",
    "product requirements document", "product discovery", "product lifecycle",
    "product vision", "feature prioritization",
    "scope, schedule", "budget and timeline", "schedule and budget",
    # THE ROLE NAMES THEMSELVES. Missed on the first two passes and it cost most of the
    # remaining recall: "project manager" and "program manager" were anchors but
    # "product manager" was not, so Product Manager postings at Comcast, Disney, Capital One and
    # JPMorgan sat on a single anchor. A posting whose body repeatedly says "the product manager
    # will..." IS that job, whatever the title on the req says -- which is the entire premise of
    # reading the description in the first place.
    "product manager", "product managers", "product owners", "scrum masters",
    "delivery manager", "delivery lead", "engagement manager", "portfolio manager",
    "release train engineer", "program management office", "technical program manager",
    "technical project manager",
)
PM_SUPPORT = (
    "stakeholder", "stakeholders", "milestone", "milestones", "deliverable", "deliverables",
    "roadmap", "timeline", "timelines", "scope", "budget", "prioritize", "prioritization",
    "coordinate", "coordination", "escalation", "escalate", "dependencies", "governance",
    "kpi", "kpis", "jira", "confluence", "asana", "smartsheet", "ms project",
    "microsoft project", "agile", "scrum", "kanban", "waterfall", "sprint", "sprints",
    "backlog", "cross-functional", "cross functional", "risks", "requirements",
    "workflow", "raci", "reporting", "facilitate", "cadence",
)
# A THIRD TIER, AND IT WAS NOT OPTIONAL. Measured on 19 live ashby/lever/jibe/pinpoint boards,
# the two-tier rule rescued 292 postings -- and at Ramp almost every one was SALES OR MARKETING:
# "Account Manager | Commercial", "Senior Product Marketing Manager", "Channel Partner Manager",
# "Solutions Consultant, Enterprise", "Director, Product Design", "Senior Manager, Deal Desk".
#
# They fire because a sales JD legitimately says "partner with product managers", "go-to-market"
# and "cross-functional stakeholders". Two lessons, both applied above: "go-to-market",
# "product strategy", "business case" and the bare "* stakeholders" phrases were REMOVED as
# anchors (they are marketing and sales vocabulary, not delivery vocabulary), and the words that
# positively identify those functions get a veto here.
#
# The calibration sweep could never have caught this: its negative bucket was engineering and
# data titles, and sales/marketing titles are not in the corpus to sample. Only a sweep of raw
# board output showed it, which is why scripts/measure_jd_admission.py exists.
PM_VETO = (
    # sales
    "quota", "prospecting", "prospects", "book of business", "closing deals", "close deals",
    "sales cycle", "sales quota", "sales pipeline", "pipeline generation", "upsell",
    "cross-sell", "renewals", "account executive", "pre-sales", "presales", "commission",
    "territory", "new business", "deal desk", "win rate", "revenue targets", "sales targets",
    "customer acquisition", "channel partner", "partnerships",
    # marketing
    "demand generation", "lead generation", "brand awareness", "marketing campaign",
    "marketing campaigns", "content marketing", "product marketing", "field marketing",
    "messaging and positioning", "seo", "paid media", "brand strategy",
    # design
    # BACK IN THE HARD TIER at the owner's direction, 2026-09-08: "figma, wireframes, user
    # research these were good, add them back." They spent part of a day in PM_VETO_SOFT on
    # the argument that a product manager COLLABORATES on all three rather than owning them.
    # The call is that precision beats that recall here, and the reason holds up: these are
    # the three words a Product DESIGNER posting is made of, and the design flood is what
    # this block was written for in the first place.
    #
    # The cost, so nobody has to rediscover it: the description-rescue path can no longer
    # admit a product posting naming two of them, so a real product role wearing a title that
    # matched no keyword is dropped again. Bounded -- the rescue path only sees titles that
    # matched NOTHING, and every ordinary product title now matches an INCLUDE keyword.
    #
    # NOT a scoring change. figma is in ATS_TOOLS and user research / wireframes are in
    # ATS_PRODUCT_DOMAIN; this list governs ADMISSION only, so no match percentage moves.
    "figma", "wireframes", "user research",
    "visual design", "design system", "ux design", "interaction design",
    # accounting / tax. Second measured pass: with sales and marketing shut out, "Senior Tax
    # Manager, Mergers & Acquisitions" and "Tax Technology Automation Manager" were the clearest
    # remaining misses -- their JDs are full of engagements, deliverables and milestones.
    "tax returns", "tax compliance", "tax provision", "cpa", "audit engagements", "gaap",
    "financial statements", "month-end close", "general ledger", "reconciliations",
    # hardware / silicon lab. "Silicon Failure Analysis & Customer Debug" and "Component
    # Quality Development Eng." score on cross-functional milestone language; the bench work is
    # what identifies them.
    "semiconductor", "silicon", "wafer", "oscilloscope", "soldering", "schematic", "pcb",
    "failure analysis", "bench testing",
    # environment / health / safety. Added 2026-08-20 from the Greenhouse measurement, where an
    # industrial-services contractor supplied 58 of the 159 rows the un-gated rule admitted and
    # the survivors of the title gate were still "EH&S Coordinator II" and "EH&S Manager - Data
    # Center Operations". An EHS JD reads like delivery work because it IS coordination work --
    # programmes, audits, corrective actions, milestones -- it is simply a different profession.
    "ehs", "eh&s", "osha", "industrial hygiene", "safety program", "safety programs",
    "incident investigation", "hazard", "personal protective equipment", "job site safety",
    # wet lab / bench science. "Senior Scientist I, Cell Culture Process Development" cleared the
    # gate on "process" and the text on process-development vocabulary.
    "cell culture", "bioreactor", "assay", "in vitro", "in vivo", "pipette", "cell line",
    "upstream process", "downstream process",
)
# DELIBERATELY NOT VETOED: construction. "Construction Project Senior Manager" and Allan Myers'
# "Project Engineer" postings are genuine project delivery, and core.ROLE_FAMILIES has listed
# "construction project manager" under the pm family since long before this rule existed.
# Vetoing them here would put the description path at odds with the role filter, which is the
# exact class of contradiction that had "Release Train Engineer" dropped for two weeks.
# Whole-phrase, longest-first, same construction as _ROLE_RES above.
_PM_ANCHOR_RE = re.compile(r"\b(?:%s)\b" % "|".join(
    re.escape(p) for p in sorted(PM_ANCHORS, key=len, reverse=True)), re.I)
_PM_SUPPORT_RE = re.compile(r"\b(?:%s)\b" % "|".join(
    re.escape(p) for p in sorted(PM_SUPPORT, key=len, reverse=True)), re.I)
_PM_VETO_RE = re.compile(r"\b(?:%s)\b" % "|".join(
    re.escape(p) for p in sorted(PM_VETO, key=len, reverse=True)), re.I)

# SET FROM MEASUREMENT, not taste. scripts/calibrate_pm_rule.py sweeps both gates over real
# stored postings; run it before touching these. Measured 2026-08-20 on 300 rows a bucket
# (248 delivery-titled / 266 technical-titled with usable descriptions):
#
#   anchors  points   recall   tech-fire
#     2        4       85.5%     13.2%
#     2        6       85.5%     12.8%   <-- shipped: best spread, and recall is what we want
#     2        8       79.8%      9.4%
#     3        6       73.0%      3.0%   <-- the conservative alternative, 12.5pp less recall
#     4        8       56.9%      0.8%
#
# Why the recall-leaning point: "tech-fire" counts engineering JDs the rule claims, and those
# are LARGELY HARMLESS here -- an engineering posting with an unmatched title is a job this feed
# wants anyway, and retail/clinical/trades are vetoed by EXCLUDE long before this runs. What is
# not harmless is a missed delivery role, because the title already failed and this is the only
# other chance the posting gets. Tightening to 3/6 is a two-constant change if the badged rows
# turn out noisy in practice.
#
# ...AND ON 2026-08-20, LATER THE SAME DAY, IT TURNED OUT TO BE. The paragraph above is kept
# because its reasoning was sound for the population it was measured on and wrong outside it,
# which is the lesson. When the JD supply widened from 4 boards (ashby/lever/jibe/pinpoint, 244
# boards, mostly tech) to Greenhouse (409 boards, every industry), the same 2/6 rule was measured
# on a 30-board Greenhouse sample of 1,869 postings:
#
#   pre-filter      thresh   admitted   % of ALL postings   worst single board
#   none             2/6        159          8.5%           loenbro 58
#   none             3/6         95          5.1%           loenbro 40
#   none             4/8         62          3.3%           loenbro 24
#   delivery-word    2/6         30          1.6%           scopely 6
#   delivery-word    3/8         20          1.1%           forgen 4   <-- shipped
#
# 8.5% of every Greenhouse posting is not a second opinion, it is a second feed. What it admitted
# was "Director, Sales Enablement", "EHS Manager", "Travelling EHS Manager", "HRIS Manager",
# "Surveyor", "Senior Estimator", "Creative Marketing Manager", "DEI Partner" -- and 58 rows from
# one industrial-services contractor. The same shape as the Ramp sales flood that produced
# PM_VETO, and the same shape as the construction flood that got "project engineer" rejected as
# an INCLUDE keyword.
#
# TWO CHANGES, both measured above. The threshold went to 3/8, and -- doing far more work than
# the threshold -- the TITLE now has to hint at delivery before the description gets a vote at
# all (PM_TITLE_HINTS below). A description rule with no title gate is not reading a posting, it
# is scanning the whole board for vocabulary, and "EHS Manager" will always contain some.
# A FOURTH TIER, AND IT IS WHAT MADE THIS RULE USABLE FOR PRODUCT WORK. Added 2026-09-08.
#
# Measured: an ordinary Associate Product Manager description scored 3 anchors and 11 points --
# clearing PM_MIN_ANCHORS and PM_MIN_POINTS -- and reads_like_pm returned False anyway, on four
# vetoes: figma, go-to-market, user research, wireframes. The veto is tested first and
# short-circuits, so no amount of product vocabulary could argue back. A DELIVERY role wearing a
# useless title got a second chance; a PRODUCT role never did.
#
# These five phrases are not evidence of a different profession. They are what a product manager
# COLLABORATES on, and every real PM posting names them: you run user research, you review
# wireframes in Figma, you work with go-to-market partners on launch.
#
# The words a designer OWNS stay in PM_VETO and stay hard -- visual design, design system, ux
# design, interaction design. That is the pair this split turns on, and it is why the Ramp and
# Greenhouse floods the veto was built for do not come back: "Product Designer" is already
# dropped upstream on an EXCLUDE hit and never reaches this rule at all, while "Product Design
# Manager" and "Director, Product Design" -- which DO reach it, for want of a keyword -- are
# refused on the owned words. "product marketing" also stays hard: a PM posting mentions the
# function in passing, a product-marketing posting is made of it.
PM_VETO_SOFT = (
    # THREE OF THE ORIGINAL FIVE WENT BACK TO THE HARD TIER on 2026-09-08 -- see the note
    # there. These two stay because they are the weakest of the set: a product posting says
    # "go-to-market partners" in passing constantly, and where marketing really is the
    # subject "product marketing" already catches it as a hard veto.
    "design reviews", "go-to-market",
)
# The product-ownership phrases that overturn a soft veto. All of them are already in PM_ANCHORS;
# this names the subset that says "product management" rather than "project delivery".
PM_PRODUCT_ANCHORS = (
    "product manager", "product managers", "product owner", "product owners",
    "product roadmap", "product requirements document", "product discovery",
    "product lifecycle", "product vision", "feature prioritization", "product backlog",
    "backlog prioritization", "backlog management",
)
# How many distinct product anchors it takes to earn the override. Two, for the same reason
# PM_MAX_VETO is two: one phrase in passing is not a posting's subject.
PM_PRODUCT_MIN = 2
_PM_VETO_SOFT_RE = re.compile(r"\b(?:%s)\b" % "|".join(
    re.escape(p) for p in sorted(PM_VETO_SOFT, key=len, reverse=True)), re.I)
_PM_PRODUCT_RE = re.compile(r"\b(?:%s)\b" % "|".join(
    re.escape(p) for p in sorted(PM_PRODUCT_ANCHORS, key=len, reverse=True)), re.I)


def pm_product_anchors(text):
    """How many DISTINCT product-ownership phrases this text names."""
    if not text:
        return 0
    return len({m.group(0).lower() for m in _PM_PRODUCT_RE.finditer(text)})


PM_MIN_ANCHORS = 3
PM_MIN_POINTS = 8
PM_ANCHOR_WEIGHT = 2
# How many distinct PM_VETO phrases it takes to say "this is a different job". Two, not one:
# see the note on reads_like_pm.
PM_MAX_VETO = 2


def pm_signal(text):
    """(distinct anchors, distinct support, distinct veto phrases) in a posting's text.

    The veto count folds in PM_VETO_SOFT only when the text does not carry enough
    PM_PRODUCT_ANCHORS to overturn it -- see the note above PM_VETO_SOFT. Returning one
    number keeps every caller and every test that reads the third element unchanged.
    """
    if not text:
        return 0, 0, 0
    return (len({m.group(0).lower() for m in _PM_ANCHOR_RE.finditer(text)}),
            len({m.group(0).lower() for m in _PM_SUPPORT_RE.finditer(text)}),
            len({m.group(0).lower() for m in _PM_VETO_RE.finditer(text)})
            + (0 if pm_product_anchors(text) >= PM_PRODUCT_MIN
               else len({m.group(0).lower()
                         for m in _PM_VETO_SOFT_RE.finditer(text)})))


def pm_points(anchors, support):
    """The single number the threshold is applied to. Anchors count double."""
    return PM_ANCHOR_WEIGHT * anchors + support


def reads_like_pm(text, min_anchors=None, min_points=None):
    """Does this description describe project/programme/product delivery work?

    Three gates. Enough ANCHORS, so support words alone can never carry a posting; enough total
    POINTS, so two anchors in an otherwise unrelated JD is not enough either; and fewer than
    PM_MAX_VETO phrases that positively identify a different function.

    The veto is a floor of two, not one: a genuine delivery JD does say "partnerships" or
    "territory" in passing, and a single word should not overturn a posting that otherwise reads
    entirely like the job.
    """
    ma = PM_MIN_ANCHORS if min_anchors is None else min_anchors
    mp = PM_MIN_POINTS if min_points is None else min_points
    a, s, v = pm_signal(text)
    if v >= PM_MAX_VETO:
        return False
    return a >= ma and pm_points(a, s) >= mp


# ------------------------------------------------------------
# THE TITLE GATE ON THE DESCRIPTION RULE.
#
# reads_like_pm answers "does this text describe delivery work". That is not the same question as
# "is this posting worth rescuing", and conflating the two is what produced the Greenhouse flood
# measured above: an EHS Manager's JD genuinely is full of milestones, stakeholders, cross-
# functional coordination and compliance timelines, because that is genuinely the job.
#
# So the title still gets a say. Not the keep/drop say -- it already failed that, which is why we
# are here -- but a WEAKER one: does the title contain any word suggesting delivery, product or
# change work? "Coordinator II" and "Business Operations Specialist", the two cases this whole
# feature exists for, both pass. "Surveyor", "EHS Manager" and "DEI Partner" do not, and no
# amount of JD vocabulary can talk us into them.
#
# These are deliberately BARE WORDS, unlike INCLUDE's phrases. That is safe precisely because
# this is a gate and not an admission: a bare "operations" here only earns the posting the RIGHT
# to be judged on its description, where three anchors and eight points are still waiting.
PM_TITLE_HINTS = (
    "project", "program", "programme", "portfolio", "delivery", "deliver",
    "implementation", "deployment", "rollout", "roll-out", "launch",
    "transformation", "transition", "initiative", "initiatives", "pmo",
    "release", "migration", "integration", "governance", "change",
    "operations", "operational", "business systems", "process",
    "scrum", "agile", "product", "coordinator", "technical", "strategy", "strategic",
)
# MEASURED AND REFUSED, even though the hints above would let them through. Every entry here is a
# title family already rejected on numbers as an INCLUDE keyword, and the description path must
# not quietly re-admit what the title path measured and threw out -- that is the "Release Train
# Engineer" contradiction in reverse.
#
# "project engineer" is the whole list for now, and it earned its place twice: +349 rows as a
# candidate keyword, 43% of them from four construction contractors, and then again in the
# Greenhouse sample above, where Forgen and Loenbro supplied 7 of the 20 rows the shipped config
# admitted. It is a real job; it is not this feed's job.
PM_TITLE_REFUSE = ("project engineer", "project engineering")
_PM_HINT_RE = re.compile(r"\b(?:%s)\b" % "|".join(
    re.escape(p) for p in sorted(PM_TITLE_HINTS, key=len, reverse=True)), re.I)
_PM_REFUSE_RE = re.compile(r"\b(?:%s)\b" % "|".join(
    re.escape(p) for p in sorted(PM_TITLE_REFUSE, key=len, reverse=True)), re.I)


def pm_title_gate(title):
    """May this title's DESCRIPTION be read as a second opinion? Cheap, and text-free.

    Reads the SHADOW title, because of everything this gate touches it is the one that costs the
    most to get wrong: scraper.fill_missing_jds only buys a description for titles it lets
    through, so a title refused HERE is never fetched and can never be judged on its work. The
    two errors are not symmetric -- a false positive costs one HTTP request, a false negative
    costs the posting permanently. "Tech Proj/Prg Mgmt" was refused here before it was refused
    anywhere else.
    """
    if not title:
        return False
    t = normalize_title(title)
    if _PM_REFUSE_RE.search(t):
        return False
    return bool(_PM_HINT_RE.search(t))


def admits_on_description(title, text, min_anchors=None, min_points=None):
    """The whole rule: may this posting be kept on its DESCRIPTION alone?

    Title gate first, because it costs nothing and settles most of them; then the JD length floor
    that keeps a truncated teaser from ever being read as a complete description (Phenom serves a
    372-char one); then the three-tier text rule.

    THE CALLER STILL OWNS THE "no matching keyword" PRECONDITION. This must never overturn an
    EXCLUDE hit -- see the note at the call site in scraper.main().
    """
    if not pm_title_gate(title):
        return False
    if len((text or "").strip()) < _MIN_JD_CHARS:
        return False
    return reads_like_pm(text, min_anchors, min_points)


# ------------------------------------------------------------
# POSTING IDENTITY
#
# `jobs` is keyed on url, so one posting reachable at two URLs is two rows. canonical_url()
# bridges the cases where the two strings describe the same address; it cannot bridge the case
# where an employer hosts a job on Greenhouse AND an aggregator relists it on its own domain,
# because those are genuinely different addresses.
#
# This is the second identity, used for exactly that case. It lives here rather than in web.py
# because the feed applies it at render time and the scraper applies it before insert, and the
# two must not drift — the same reason scripts/feed_parity.py exists for the filter twins.
# ------------------------------------------------------------
AGGREGATOR_HOSTS = ("adzuna.", "indeed.", "linkedin.", "ziprecruiter.", "glassdoor.",
                    # jobright serves EVERY row as an interstitial on its own domain — its
                    # payload has no direct employer link at all — so without this entry
                    # fingerprint_duplicate's guard 1 would exempt them and we would double-
                    # list every posting we already hold from the employer's own board.
                    "jobright.")

_HOST_RE = re.compile(r"^[a-z]+://([^/?#]+)", re.I)


def url_host(url):
    """Host of a URL, lowercased, or "" — cheap and never raises, unlike urlparse on junk."""
    m = _HOST_RE.match(url or "")
    return (m.group(1) if m else "").lower()


def is_aggregator_url(url):
    """True when the URL belongs to a job board rather than to the employer that is hiring."""
    host = url_host(url)
    return any(h in host for h in AGGREGATOR_HOSTS)


_BARE_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def is_trusted_date(found_date, posted_verified=""):
    """Did anyone actually STATE this posting's date, or did we guess it?

    True when the lookup service confirmed it, or when found_date is a bare ISO date — the
    shape a publisher field lands in (Greenhouse first_published, Adzuna created, Lever
    createdAt, Amazon posted_date, Workday's CXS startDate).

    False for "YYYY-MM-DD HH:MM", which is this codebase's marker for a DERIVED value: either
    the scrape stamp or _workday_date() reading "Posted 30+ Days Ago" off a list view, where
    "30+" is a ceiling clamped to MAX_AGE_DAYS+1 rather than a measurement. Also false for a
    blank date, where the row is only aged by when WE first saw it.

    Deliberately broader than `date_verified`, which means the lookup service specifically and
    covers ~10% of the corpus — a filter on that alone would hide nearly everything. This is
    the honest reading of "show me jobs with a real posting date", and the same string-shape
    contract verify_dates._is_clean_api_date() uses to decide what to queue.
    """
    if posted_verified:
        return True
    return bool(_BARE_ISO_RE.match((found_date or "").strip()))


def sponsor_rank(row):
    """Ordering for sort=sponsor, LOWEST FIRST — "show me the jobs I can actually take".

    Lives in core because THREE surfaces need it: the feed (web._row_sponsor_rank), the client
    (app.js sponsorRank), and the email digest (scraper.notify). notify cannot import web — that
    would pull Flask into the scraper — so without this the digest would carry a fourth copy of
    a rule that is already mirrored twice.

    Ranking rather than filtering, deliberately. Measured 2026-08-09: filtering on sponsorship
    hides 442 of the 1,346 jobs above the default match floor, and 88 of those are employers with
    NO federal record at all — 41 Northrop Grumman postings and 4 at Penn State, a CAP-EXEMPT
    university and therefore the best H-1B route available. Absence from a DOL file means "not in
    this dataset", never "does not sponsor".

    `visa` must already be narrowed per posting by visa_tags_for_posting, which both _build_row
    and digest_row do, so a JD demanding citizenship has had its employer tags stripped first.
    """
    visa = row.get("visa") or ()
    if "h1b" in visa and "stem_opt" in visa:
        tier = 0                    # E-Verify AND files LCAs: STEM OPT now, H-1B later
    elif "h1b" in visa:
        tier = 1                    # files LCAs
    elif "stem_opt" in visa:
        tier = 2                    # E-Verify only — clears the STEM OPT gate, no H-1B evidence
    elif row.get("sponsor_jd") == "blocked":
        tier = 4                    # the JD itself rules you out; last, but still reachable
    else:
        tier = 3                    # no record either way
    # Then USCIS approval volume, then match score — both descending.
    return (tier, -(row.get("strength_n") or 0), -(row.get("score") or 0))


def _key_location(location):
    """The location half of posting_key, canonicalised just enough that two sources describing
    the same place agree.

    THE CITY IS KEPT. This is not the state-only key the docstring below rejects -- collapsing
    to the state merged 4,770 rows that were real inventory, and nothing here does that:
    "austin tx" and "dallas tx" stay as far apart as they were. What it fixes is that every
    aggregator writes "CA" while most ATS boards write "California", so the SAME posting keyed
    two different ways and every cross-source comparison silently failed:

        'Palo Alto, California' -> ('...', 'tesla', 'palo alto california')
        'Palo Alto, CA'         -> ('...', 'tesla', 'palo alto ca')

    Measured 2026-09-05 on 60 Indeed rows against the live corpus: 56 counted as net-new on the
    old key, 54 on this one -- so ~4% of what an aggregator offered as new was a posting already
    held under the other spelling. It also made every "does the aggregator carry this job"
    measurement read zero when the honest answer was not zero.

    The trailing country tag goes for the same reason: "Austin, TX, US" and "Austin, TX" are one
    place, and a US-only feed carries no information in it. Stripped only from the END, so a
    place whose name contains those letters is untouched.
    """
    s = (location or "").lower()
    # Full name -> code BEFORE slugging, while the word boundaries are still intact. Longest
    # first is already baked into _STATE_NAMES_RE, so "west virginia" cannot be read as
    # "virginia".
    s = _STATE_NAMES_RE.sub(lambda m: _STATES[m.group(1)].lower(), s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return re.sub(r"\s+(?:united states|usa|us)$", "", s).strip()


def posting_key(title, company, location, require_location=False):
    """Identity of a POSTING rather than of a URL: title + company + full location.

    Location is the RAW string, not just the state. Using the state collapsed 4,770 rows in
    this corpus, but almost all of them were real, distinct openings — Amazon genuinely lists
    431 "Operations Manager" roles and Walmart 144 store-level pharmacy internships. Those are
    inventory, not duplicates.

    Returns None when the key would be too weak to trust. `require_location` adds a blank
    location to that list: ("pm", "acme", "") collides with every unplaced Acme PM row. That is
    tolerable at render time, where nothing is deleted and the user still sees a card, but not
    ahead of an insert that would drop the posting for good.
    """
    t = re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()
    c = re.sub(r"[^a-z0-9]+", " ", (company or "").lower()).strip()
    if not (t and c):
        return None
    loc = _key_location(location)
    if require_location and not loc:
        return None
    return (t, c, loc)


# ------------------------------------------------------------
# SAVED SEARCH PREFERENCES
#
# The feed shipped ten controls that all reset to their defaults on every visit, so a student
# re-declared "PM roles, Boston or remote, entry level, E-Verify only" every single time. These
# are the saved answers — one shape, used both to seed the feed's controls and to decide what
# lands in the email digest, so the two can never mean different things by "my search".
# ------------------------------------------------------------
# THE SCALE VERSION of the `min` floor below, bumped whenever the score's MEANING moves —
# because a stored floor is a number on a scale, and reinterpreting it silently is how a saved
# search quietly starts matching something else.
#
#   v1  raw coverage of EVERY term in the JD. Topped out near 69, mode 30-39.
#   v2  the percentile of that value across the corpus. Briefly shipped, and wrong: it read as
#       "you are 96% qualified" while it meant "this job ranks above 96% of the others", so
#       ordinary matches displayed in the high nineties. Withdrawn.
#   v3  coverage of the terms carrying the heavy part of the JD's weight — the skills the role
#       actually emphasises. Absolute, not relative, and deliberately hard: measured over 21,176
#       live postings the best match in the whole corpus is 88, only 16 reach 80, and the median
#       is 34. See core_terms, and the confidence cap in score_against.
#
# NOT BUMPED for the 2026-08-21 profile narrowing, and the reason is measured rather than
# assumed. db.profile_text stopped concatenating every résumé in the library and now scores
# against the live one plus the user's stories, so the text on the other side of an unchanged
# formula got smaller and every score drifts down. The question was whether that is a change of
# MEANING (bump, which RESETS every stored floor to DEFAULT_PREFS["min"]) or of degree.
#
# Measured over the 21,982-row snapshot, one résumé against the same résumé duplicated:
#     one   p50 12   p90 27   >=20: 25.5%
#     two   p50 14   p90 29   >=20: 29.9%
# So roughly a 14% relative drop in the middle of the distribution. A stored floor still means
# what it meant; it just admits somewhat less. Against that, a bump resets a floor the user may
# have deliberately tuned DOWN, and the default it resets to (50) admits 2.7% of this corpus at
# the stored match_scores — so the bump would be the disruptive option, not the safe one. Left
# alone: the user sees a slightly shorter feed and can move the slider, which is visible and
# reversible. Revisit if the profile ever narrows further.
# 4: the 2026-09-02 JD-reading repair. Removing the phantom ATS terms and the top-of-table
# weight for an unseen one raised the share of the corpus scoring 50 or more from 18.2% to
# 29.2%, so a stored floor of 50 admits ~60% more rows than it did when it was chosen. That is
# a change of KIND, not of degree -- the filter silently stops filtering -- which is exactly
# what normalize_prefs' scale migration below exists for.
# 5: the 2026-09-08 ALIAS-PHANTOM repair (core._term_in). A two-character alias was being
#    substring-matched with no word boundary, so `business analysis` (alias "ba") fired on
#    99.7% of product-role postings against a 6.7% literal presence, and `project
#    management` (alias "pm") on 82.9% against 12.6%. Both sides were poisoned -- the
#    resume matcher routes through the same function -- so each was a free full-weight hit
#    on both halves of the comparison.
#
#    Measured on the owner's real resume.txt over 700 real product descriptions:
#        p50 35 -> 27 ;  share >= 50  11.4% -> 5.9% ;  share >= 30  ~60% -> 44.0%
#    The filter now filters roughly twice as hard at any given floor, which is a change of
#    KIND in exactly the sense v4 describes -- in the opposite direction. So the scale
#    moves, and every stored floor resets to DEFAULT_PREFS["min"], which is now 0. That is
#    the reason this bump is cheap where v4's would have been disruptive: resetting to a
#    NO-FLOOR default cannot silently over-filter anybody.
MIN_SCALE = 5

DEFAULT_PREFS = {
    # ZERO, at the owner's direction 2026-09-08: "it's okay, don't hide anything."
    #
    # It was 50, and the comment here said that "admits about a quarter of the corpus".
    # Measured on the live corpus it admits 9.3%, and on the population this reader is
    # actually looking for it is far worse: of the 328 entry-level product postings inside
    # the default date window, a floor of 50 removes 294 of them -- 90%. The sensitivity is
    # a cliff, not a slope (45 leaves 84, 30 leaves 274, 0 leaves 328), so there was no
    # value of this number that both filtered and kept the supply.
    #
    # NOTHING IS LOST FROM THE TOP OF THE FEED, which is why this is safe: `sort` defaults
    # to "score", so the best matches are still the first cards. The floor only ever
    # deleted the tail. And the slider is still there at max=75 for anyone who wants one.
    #
    # A STORED floor is untouched -- normalize_prefs only resets one when MIN_SCALE moves,
    # and MIN_SCALE means "the score changed meaning". Bumping it for a default change
    # would spend that version number on the wrong thing.
    "min": 0,             # minimum match %; 0 = no floor
    "min_scale": MIN_SCALE,
    "loc": "",            # metro / city / 2-letter state / "remote"
    "remote": False,
    "minsal": 0,          # annualized floor; 0 = any
    "hideagency": True,   # staffing agencies off by default (they flood the feed)
    # LEGACY. Superseded by "visatags" (stem_opt is the same fact). Kept in the dict so an
    # old saved search still round-trips and so tests asserting the key set keep passing;
    # normalize_prefs migrates a True into visatags and clears it. Nothing reads it.
    "everify": False,
    "visatags": "",       # csv subset of VISA_TAGS, e.g. "h1b,green_card"; "" = no filter
    "hidenospon": False,
    # Only postings whose date somebody STATED — see is_trusted_date. FEED ONLY: prefs_match
    # deliberately ignores it, for the same reason it ignores `date`. Every digest candidate is
    # a job we just discovered and therefore not yet verified, so applying this to the email
    # would silently empty it.
    "verifiedonly": False,
    # csv subset of ROLE_KEYS — "what kind of job do you want", the thing `track` only ever
    # answered two ways. Empty = every role, so an untouched account sees the whole corpus.
    "roles": "",
    "exp": "any",         # any | entry | 2 | 5   ("senior" is legacy)
    # Drop postings whose description states NO year count. Off by default, deliberately: the
    # keep-on-unknown rule below exists because many genuine entry-level posts state no number,
    # and dropping them silently would hide real jobs.
    #
    # It exists because the unknown rate makes the years control mean very little on its own.
    # Measured live on Recommended with the match floor at 0: "Experience = any" returned 20,618
    # jobs of which 18% of a 60-card sample carried no years badge; "0-2 yrs" returned 10,514 of
    # which 72% carried none. The comparison itself is CORRECT -- not one card stating more than
    # 2 years survived -- but roughly 7 in 10 results are "we could not tell", presented
    # indistinguishably from the ones that genuinely qualify. That is why a posting demanding 6+
    # years in its text shows up under a 0-2 filter: the requirement is in the JD,
    # experience_years missed it, and keep-on-unknown waved it through.
    #
    # Raising experience_years' recall is the real fix and is a separate measurement job. This
    # gives the reader a way to see only the population the filter can actually reason about,
    # and the card badge names the other one.
    # FEED ONLY, like verifiedonly: prefs_match ignores it, because every digest candidate is a
    # job we just discovered and applying this to the email would quietly shrink it.
    "expstated": False,
    "intern": "any",      # any | only | no
    "track": "any",       # any | dev (software/data) | mgmt (project/product/ops) — see role_track
    "date": "30",         # any | 1 | 7 | 30 | 90
    # score | newest | sponsor. "sponsor" ranks by how sponsorable a posting is (see
    # web._row_sponsor_rank) rather than filtering on it — filtering would hide employers the
    # federal files simply don't list, e.g. cap-exempt universities.
    # THE SEARCH BOX IS A PREFERENCE NOW. It was not in this dict, so normalize_prefs -- which
    # builds from DEFAULT_PREFS and validates every key against it -- dropped it, and a typed
    # search evaporated on reload, could not be saved as a default and could not drive the
    # email digest. For a reader whose real search is a PHRASE ("associate product manager")
    # that was the one expression of intent the app could not keep.
    #
    # It matters more than it looks: web._filter_rows reads
    # `if not (searching or r["score"] >= minv)`, so an active search BYPASSES the match
    # floor. The one control that made this app work for a narrow search was also the only
    # one that could not be persisted.
    "q": "",              # free-text title/company/location search
    "sort": "score",
    "alerts": "off",      # off | daily  — email digest of new matches
    "alert_min": 0,       # extra match floor for the email only; 0 = use `min`
}
_PREF_CHOICES = {
    # "entry" filters on the LEVEL (core.level_for), not on a year ceiling: the numeric
    # values answer "could I be considered", which is a different question from "is this
    # an entry-level job". Measured -- "0 to 2 Years" returns 417 product rows and 176 of
    # them are entry by description; the rest have no readable number and are kept, which
    # is right and is also why the ceiling cannot answer the level question.
    #
    # "senior" is LEGACY, kept so a saved search still round-trips. It filtered identically
    # to "5" -- both keep yrs <= 5 -- and no option has ever rendered it.
    "exp": ("any", "entry", "2", "5", "senior"),
    "intern": ("any", "only", "no"),
    "track": ("any", "dev", "mgmt"),
    "date": ("any", "1", "7", "30", "90"),
    "sort": ("score", "newest", "sponsor"),
    "alerts": ("off", "daily"),
}
# Keys whose value is a comma-separated subset of a fixed vocabulary. Validated separately
# from _PREF_CHOICES (which is one-of) so junk is dropped and the order is canonicalized.
_PREF_CSV = {"visatags": VISA_TAGS, "roles": ROLE_KEYS}


def _pref_bool(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on", "t")


def normalize_prefs(raw):
    """Coerce anything (a form post, a jsonb column, None) into a complete valid prefs dict.

    Every value is validated against DEFAULT_PREFS rather than trusted, because this comes
    from a browser and then gets used to build an email — an unvalidated `loc` or `min` would
    otherwise flow straight into the digest query.
    """
    out = dict(DEFAULT_PREFS)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except Exception:
            raw = {}
    if not isinstance(raw, dict):
        return out

    # SCALE MIGRATION, ONCE PER PROFILE. A dict that carries a `min` but no `min_scale` was
    # written against an older meaning of the score (see MIN_SCALE) and cannot be compared with
    # the current one. Reset it to the current default rather than translating it: a translated
    # floor faithfully preserves whatever the user was seeing, and what they were seeing is the
    # thing being fixed.
    #
    # Safe against clobbering a live slider move because every save path merges over
    # _user_prefs(), whose output has already been through here and so carries min_scale.
    # ZERO IS SCALE-INVARIANT and must survive. "No floor at all" means the same thing on
    # every scale, and it is the value the digest fixtures and any user who deliberately turned
    # the filter off are holding. Remapping it to 70 would silently switch a filter back on.
    try:
        _stored_min = int(float(str(raw.get("min")).strip() or 0))
    except (TypeError, ValueError):
        _stored_min = 0
    stale_scale = ("min" in raw and _stored_min > 0
                   and int(raw.get("min_scale") or 1) < MIN_SCALE)

    for key, default in DEFAULT_PREFS.items():
        if key not in raw or raw[key] is None:
            continue
        v = raw[key]
        if isinstance(default, bool):
            out[key] = _pref_bool(v)
        elif isinstance(default, int):
            try:
                out[key] = max(0, int(float(str(v).strip() or 0)))
            except (TypeError, ValueError):
                pass
        elif key in _PREF_CSV:
            out[key] = ",".join(parse_roles_pref(v) if key == "roles" else parse_visa_pref(v))
        elif key in _PREF_CHOICES:
            s = str(v).strip().lower()
            if s in _PREF_CHOICES[key]:
                out[key] = s
        else:
            out[key] = str(v).strip()[:80]
    if stale_scale:
        out["min"] = DEFAULT_PREFS["min"]
        out["alert_min"] = 0          # also a raw-scale floor; 0 means "use min"
    out["min_scale"] = MIN_SCALE
    out["min"] = min(out["min"], 100)
    # Migrate the retired "E-Verify only" checkbox onto the visa-tag filter. The clear is
    # load-bearing: save_prefs merges the posted body over the stored dict, so a browser
    # that no longer sends `everify` would leave a stale True behind and silently re-add
    # stem_opt every time the user unticked it.
    if out.get("everify"):
        out["visatags"] = ",".join(parse_visa_pref(out.get("visatags", "") + ",stem_opt"))
        out["everify"] = False
    return out


def _level_of(row):
    """A row's level, falling back to its title for a row built before `level` existed."""
    lv = row.get("level")
    if lv is None:
        lv = title_level(row.get("title") or "")
    return lv or ""


def prefs_match(row, prefs):
    """Does this job match the user's saved search?

    Scope note: this is the subset that means something for a job we just discovered — the
    "posted within" window is skipped because every candidate is new by definition, and search
    text isn't a saved preference. web.py::_filter_rows remains the authority for the live
    feed; this exists so the EMAIL agrees with it, and a test asserts the two agree on the
    filters they share.
    """
    p = prefs or DEFAULT_PREFS
    floor = p.get("alert_min") or p.get("min") or 0
    if (row.get("score") or 0) < floor:
        return False
    if p.get("hidenospon") and row.get("sponsor_jd") == "blocked":
        return False
    if not visa_tags_match(row.get("visa"), parse_visa_pref(p.get("visatags")),
                           row.get("sponsor_jd") == "blocked"):
        return False
    # Unlike verifiedonly, this one DOES belong in the digest: "I want Project Manager jobs" is
    # exactly as true of an email as of the feed, and a new posting's role is known the moment
    # we see its title — nothing has to be verified first.
    if not roles_match(row.get("roles") or roles_for_title(row.get("title")),
                       parse_roles_pref(p.get("roles"))):
        return False
    if p.get("hideagency") and row.get("agency"):
        return False
    if p.get("remote") and not row.get("remote"):
        return False
    if row.get("closed"):
        return False
    loc = (p.get("loc") or "").strip().lower()
    if loc and not location_matches(row, loc):
        return False
    if p.get("minsal"):
        sm = row.get("salary_min")
        if not sm or annualize_pay(sm, row.get("salary_period")) < p["minsal"]:
            return False
    intern = p.get("intern") or "any"
    if intern == "only" and not row.get("intern"):
        return False
    if intern == "no" and row.get("intern"):
        return False
    track = p.get("track") or "any"
    # Fall back to classifying the title: rows cached before `track` existed won't carry it.
    if track != "any" and (row.get("track") or role_track(row.get("title"))) != track:
        return False
    exp = p.get("exp") or "any"
    r_intern = bool(row.get("intern"))
    if exp == "entry":
        # THE LEVEL, not a ceiling -- see DEFAULT_PREFS. Kept when we could not tell, the
        # same keep-on-unknown contract the numeric values have. Falls back to reading the
        # title so a digest row cached before `level` existed still filters.
        lv = row.get("level")
        if lv is None:
            lv = title_level(row.get("title") or "")
        if (lv or "") not in ("", "entry"):
            return False
        return True
    if exp != "any":
        # exp_eff: the highest year count the DESCRIPTION states, or the floor the TITLE implies
        # when it states none. A posting with neither is always kept — same rule as
        # web._filter_rows and app.js matches(). Falls back to exp_years for a row built before
        # exp_eff existed, so an old cached digest row still filters rather than passing.
        ev = row.get("exp_eff", row.get("exp_years"))
        if ev not in ("", None):
            try:
                yrs = int(ev)
            except (TypeError, ValueError):
                yrs = None
            if yrs is not None:
                if r_intern:
                    pass                       # see web._filter_rows: an internship IS entry
                elif exp == "senior":
                    if yrs >= 6:
                        return False
                elif yrs > (int(exp) if str(exp).isdigit() else 99):
                    return False
        elif not r_intern:
            # No year count anywhere, so the LEVEL answers the ceiling. See web._filter_rows.
            floor = LEVEL_MIN_YEARS.get(_level_of(row))
            if floor is not None and floor > (5 if exp == "senior"
                                              else (int(exp) if str(exp).isdigit() else 99)):
                return False
    return True


HOURS_PER_YEAR = 2080          # 40 h/wk x 52; web.py imports this so one constant governs both


def annualize_pay(amount, period):
    """Put hourly and salaried pay on one scale so a single minimum works for both."""
    try:
        n = int(amount or 0)
    except (TypeError, ValueError):
        return 0
    return n * HOURS_PER_YEAR if period == "hour" else n


def location_matches(row, needle):
    """Does a row match a typed location? Metro, 2-letter state code, or the raw string.
    Mirrored by web.py::_loc_hit and app.js::locHit — keep the three in step."""
    if not needle:
        return True
    if needle == "remote":
        return bool(row.get("remote"))
    if len(needle) == 2:
        return needle.upper() == (row.get("loc_state") or "").upper()
    hay = ((row.get("loc_metro") or "") + " " + (row.get("loc_state") or "") + " " +
           (row.get("location") or "")).lower()
    return needle in hay


# INTERNSHIP / CO-OP DETECTION FROM THE TITLE. THE ONE DEFINITION, moved here 2026-09-08.
#
# There were two and they disagreed. This pattern lived in web.py; digest_row carried its own
# narrower inline copy with no plurals, no "summer analyst" and no "summer associate". So
# "Product Manager Summer Associate" and "Product Management Co-ops" were internships in the
# FEED and ordinary jobs in the EMAIL, which made the intern filter mean two different things
# depending on which surface you were reading -- in the one field whose entire purpose is to
# separate the level this reader is applying at from the rest of the corpus.
#
# Whole-word, so it never fires on "international" or "internal". The finance spellings
# "summer analyst" / "summer associate" are in because banks title APM-adjacent internships
# that way and nothing else in the app would catch them.
#
# web.py aliases this rather than importing it into a local name of its own -- see the note
# there. There is no client twin: `intern` is computed server-side and shipped as a bool.
INTERN_RE = re.compile(
    r"\b(?:intern(?:s|ship|ships)?|co[-\s]?ops?|summer analyst|summer associate)\b", re.I)


def digest_row(job, score, everify_index=None, visa_index=None, counts_index=None):
    """The row shape prefs_match wants, built from a RAW db job row.

    The email path has no access to web.py's _build_row (importing Flask into the scraper
    would be absurd), so this derives the same fields from the job itself using the same
    core helpers the feed uses.
    """
    company = job.get("company") or ""
    jd = job.get("jd") or ""
    # READ ONCE. The years parser is the most expensive thing on this path and three of the
    # fields below want its answer; it also has to see the CLEANED text, because a captured
    # results list states years about somebody else's job.
    _exp_years = experience_years(clean_jd(jd)[0]) if jd else None
    _exp_eff, _exp_src = _exp_years, ("stated" if _exp_years is not None else "")
    if _exp_eff is None:
        _exp_eff = title_experience_tier(job.get("title") or "")
        _exp_src = "inferred" if _exp_eff is not None else ""
    loc = parse_location(job.get("location") or "", jd)
    sal = parse_salary(jd)
    if job.get("salary_min"):
        sal = {"min": job.get("salary_min"), "max": job.get("salary_max"),
               "period": job.get("salary_period") or "year"}
    active = job.get("is_active")
    _sv, _sreason = (sponsorship_from_jd(jd) if jd else ("", ""))
    # Same narrowing the feed applies, so the email never claims a route the JD rules out.
    vtags = visa_tags_for_posting(visa_tags(company, visa_index) if visa_index else (),
                                  _sv, _sreason)
    _st, _sn = sponsor_strength(company, counts_index) if counts_index else ("", 0)
    return {
        "visa": vtags,
        "title": job.get("title") or "", "company": company,
        "url": job.get("url") or "", "location": job.get("location") or "",
        "score": score,
        "loc_state": job.get("loc_state") or loc["state"],
        "loc_metro": job.get("loc_metro") or loc["metro"],
        "remote": bool(job.get("remote")) or loc["remote"],
        "salary_min": sal["min"], "salary_max": sal["max"], "salary_period": sal["period"],
        "salary_label": salary_label(sal["min"], sal["max"], sal["period"]),
        "sponsors_h1b": job.get("sponsors_h1b") or "",
        "sponsor_jd": _sv,
        # USCIS approval volume — read only by sponsor_rank, so the digest can order by the same
        # ladder the feed does. Passed in rather than loaded here: this runs once per job per
        # recipient, and sponsor_counts.json is ~2.9 MB. Absent index -> ("", 0), which ranks the
        # employer as "no record" rather than erroring.
        "strength": _st, "strength_n": _sn,
        "roles": list(roles_for_title(job.get("title"))),
        "agency": is_agency(company), "cap_exempt": is_cap_exempt(company),
        # stem_opt IS the E-Verify fact, now sourced from the visa index; fall back to the
        # old everify.txt path for anyone who built that file.
        "everify": ("stem_opt" in vtags) or bool(
            everify_index and is_everify(company, everify_index)),
        # The HIGHEST year count stated, matching what the feed filters on — the digest and
        # the feed must not disagree about which jobs are "entry level".
        "exp_years": _exp_years if _exp_years is not None else "",
        # exp_eff / exp_src are web._build_row's twins, and prefs_match compares exp_eff — so
        # leaving them out would silently make the email a laxer filter than the feed.
        "exp_eff": _exp_eff if _exp_eff is not None else "", "exp_src": _exp_src,
        "intern": bool(INTERN_RE.search(job.get("title") or "")),
        "closed": active is False or str(active).strip().lower() == "false",
    }


# ------------------------------------------------------------
# WORK-AUTHORIZATION TIMELINE
#
# The job search of an F-1 student runs against a clock nobody else's does: the EAD expiry,
# the window to file the STEM extension, the annual H-1B registration, and the cap on days
# spent unemployed. No job tool tracks it, so people track it in their head and miss it.
#
# THIS IS A REMINDER, NOT ADVICE. Everything below is arithmetic on dates the user typed in.
# It asserts no eligibility, and every surface that renders it says to confirm with the
# school's international-student office (DSO) and uscis.gov, because the rules do change.
# ------------------------------------------------------------
# Post-completion OPT allows 90 days of unemployment; the 24-month STEM extension raises the
# aggregate allowance to 150. https://www.ice.gov/sevis/practical-training
UNEMPLOYMENT_LIMIT_OPT = 90
UNEMPLOYMENT_LIMIT_STEM = 150
# USCIS accepts the STEM extension I-765 up to 90 days before the current EAD expires, and it
# must be filed before that expiry.
STEM_FILE_WINDOW_DAYS = 90
# The H-1B registration period has opened in early March every recent year (exact dates are
# announced annually), so March 1 is an anchor for "how far away is it", never a claim.
H1B_REGISTRATION_MONTH = 3
H1B_REGISTRATION_DAY = 1


def _as_date(v):
    """Parse a YYYY-MM-DD-ish string (or pass a date through). None when unusable — these
    come from free-text profile fields, so anything unparseable is simply absent."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime.date):
        return v
    s = str(v).strip()[:10]
    try:
        return datetime.date.fromisoformat(s)
    except ValueError:
        return None


def _severity(days):
    """How loudly to render a deadline: past/urgent/soon/ok."""
    if days is None:
        return ""
    if days < 0:
        return "past"
    if days <= 30:
        return "urgent"
    if days <= 90:
        return "soon"
    return "ok"


def next_h1b_registration(today):
    """The next early-March H-1B registration anchor on or after `today`."""
    anchor = datetime.date(today.year, H1B_REGISTRATION_MONTH, H1B_REGISTRATION_DAY)
    if anchor < today:
        anchor = datetime.date(today.year + 1, H1B_REGISTRATION_MONTH, H1B_REGISTRATION_DAY)
    return anchor


def visa_timeline(prof, today=None):
    """Turn the visa dates on a user's profile into dated reminders.

    Returns {"has_data", "items": [...], "unemployment": {...} | None}. Each item is
    {key, label, date, days, severity, note}; `days` is signed (negative = already past).
    Reads only these profile keys, all optional: opt_type, opt_start_date, opt_end_date,
    program_end_date, stem_eligible, unemployment_days_used.
    """
    prof = prof or {}
    today = today or datetime.date.today()
    opt_type = (prof.get("opt_type") or "").strip().lower()
    opt_end = _as_date(prof.get("opt_end_date"))
    opt_start = _as_date(prof.get("opt_start_date"))
    prog_end = _as_date(prof.get("program_end_date"))
    stem_eligible = str(prof.get("stem_eligible") or "").strip().lower() in ("yes", "true", "1", "on")

    items = []

    if prog_end and prog_end >= today:
        items.append({
            "key": "program_end", "label": "Program end date", "date": prog_end.isoformat(),
            "days": (prog_end - today).days, "severity": "ok",
            "note": "OPT must be applied for within the window around this date."})

    if opt_end:
        d = (opt_end - today).days
        items.append({
            "key": "opt_end",
            "label": "STEM OPT EAD expires" if opt_type == "stem" else "OPT EAD expires",
            "date": opt_end.isoformat(), "days": d, "severity": _severity(d),
            "note": "Work authorization ends on this date unless something else is approved."})

        # The STEM filing window only makes sense while on post-completion OPT.
        if opt_type in ("", "opt", "post-completion opt") and stem_eligible:
            opens = opt_end - datetime.timedelta(days=STEM_FILE_WINDOW_DAYS)
            if today <= opt_end:
                open_now = today >= opens
                d2 = (opt_end - today).days if open_now else (opens - today).days
                items.append({
                    "key": "stem_window",
                    "label": "STEM extension filing window closes" if open_now
                             else "STEM extension filing window opens",
                    "date": (opt_end if open_now else opens).isoformat(),
                    "days": d2, "severity": _severity(d2) if open_now else "ok",
                    "note": ("USCIS must RECEIVE the I-765 before your EAD expires."
                             if open_now else
                             "You can file up to %d days before the EAD expires."
                             % STEM_FILE_WINDOW_DAYS)})

    # Only worth showing to someone who still needs sponsorship.
    if opt_end or prog_end:
        reg = next_h1b_registration(today)
        items.append({
            "key": "h1b_registration", "label": "H-1B registration (typically early March)",
            "date": reg.isoformat(), "days": (reg - today).days, "severity": "ok",
            "note": "An employer registers you; exact dates are announced by USCIS each year."})

    items.sort(key=lambda i: i["date"])

    unemployment = None
    used_raw = prof.get("unemployment_days_used")
    if used_raw not in (None, "") or opt_end:
        try:
            used = max(0, int(str(used_raw).strip() or 0))
        except (TypeError, ValueError):
            used = 0
        limit = UNEMPLOYMENT_LIMIT_STEM if opt_type == "stem" else UNEMPLOYMENT_LIMIT_OPT
        left = limit - used
        unemployment = {
            "used": used, "limit": limit, "left": left,
            "severity": "past" if left < 0 else "urgent" if left <= 15
                        else "soon" if left <= 30 else "ok",
            "note": "Counted only while on OPT, and only days you were not employed."}

    return {"has_data": bool(items or (unemployment and unemployment["used"])),
            "items": items, "unemployment": unemployment,
            "opt_start": opt_start.isoformat() if opt_start else ""}


def visa_alert(timeline):
    """The single most pressing item, for the slim feed strip — or None to show nothing.

    Deliberately quiet: only an item inside 90 days, or an unemployment allowance under 30
    days, is worth interrupting a job search for. Everything else lives on the profile page.
    """
    if not timeline or not timeline.get("has_data"):
        return None
    un = timeline.get("unemployment") or {}
    cands = []
    for it in timeline["items"]:
        if it["severity"] in ("past", "urgent", "soon") and it["key"] != "h1b_registration":
            cands.append((0 if it["severity"] == "past" else 1, it["days"], it))
    if un and un.get("severity") in ("past", "urgent", "soon"):
        cands.append((0 if un["severity"] == "past" else 1, un.get("left", 999), {
            "key": "unemployment",
            "label": "%d of %d unemployment days left" % (max(un["left"], 0), un["limit"]),
            "date": "", "days": un.get("left"), "severity": un["severity"],
            "note": un.get("note", "")}))
    if not cands:
        return None
    cands.sort(key=lambda c: (c[0], c[1]))
    return cands[0][2]


# ------------------------------------------------------------
# Experience requirement parsing (to keep only entry-level roles)
# ------------------------------------------------------------
# A year mention: '5 years', '5+ years', '5-7 years', '5 to 7 years', '5 yrs'.
# Group 1 = the FLOOR (the smaller number — what you actually need to qualify).
# THE SEPARATOR MAY BE A HYPHEN, and this is the compound-adjective form: "1-year experience",
# "2-year experience as a medical assistant". Found by scripts/audit_jd_reading.py rather than by
# reading, which is the point of that script -- the range branch below already consumed a hyphen
# but only when a SECOND number followed it, so "3-5 years" read and "3-year" did not.
_EXP_YEARS_RE = re.compile(
    r"(\d{1,2})\s*(?:\+|(?:\s*(?:-|–|—|to)\s*\d{1,2})\s*\+?)?\s*[-–—]?\s*(?:years?|yrs?)\b",
    re.I)

# The same mention SPELLED OUT, with or without the digit repeated in brackets beside it:
# "five years", "Minimum of eight (8) years", "two to three years".
#
# Measured 2026-09-03 over the 41,434 cached descriptions: 1,623 state their requirement ONLY
# in words, so a digits-only rule read every one of them as "states no requirement" -- and this
# parser's None means KEEP, so those senior roles sat in an entry-level feed. A missed floor is
# invisible from the outside: it looks exactly like a posting that never named one.
_WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
             "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15,
             "twenty": 20}
_EXP_WORD_YEARS_RE = re.compile(
    r"\b(" + "|".join(_WORD_NUM) + r")\b"
    r"\s*(?:\(\s*\d{1,2}\s*\))?"                       # "eight (8)"
    r"(?:\s*(?:-|–|—|to|or)\s*(?:" + "|".join(_WORD_NUM) + r"|\d{1,2})\b)?"   # "two to three"
    r"\s*(?:\+|plus)?\s*(?:years?|yrs?)\b", re.I)

# A RANGE, in any mix of the two notations, read as its FLOOR. This runs BEFORE the other two
# and claims the whole span, which is what stops them disagreeing about it. Without it each
# pattern could only read the combinations written in its own notation: the digit one needs
# digits on both sides, the word one needs a word on the left, and "1 to three years" satisfies
# neither -- so the word pattern matched "three years" alone and answered the CEILING of a
# range whose floor is one. Four combinations, one pattern, one answer.
_EXP_RANGE_RE = re.compile(
    r"\b(\d{1,2}|" + "|".join(_WORD_NUM) + r")\b"
    r"\s*(?:-|–|—|to)\s*"
    r"(?:\d{1,2}|" + "|".join(_WORD_NUM) + r")\b"
    r"\s*(?:\+|plus)?\s*(?:years?|yrs?)\b", re.I)

# Experience stated in MONTHS. Floor-divided, so "6 months" is 0 years and "18 months" is 1 --
# the honest reading for a filter whose only job is to separate entry-level from not.
_EXP_MONTHS_RE = re.compile(r"\b(\d{1,3})\s*months?\b", re.I)
# A DURATION IS NOT A REQUIREMENT. "a 12 month contract role in software development" reads as a
# one-year floor on the generic context words, and "you will complete a 6 month rotation through
# our engineering organisation" reads as zero. Zero is harmless for the comparison but NOT for
# the three-state answer this feeds -- it turns "states nothing" into a confident number, and the
# posting then survives "only postings that state their years" claiming to be entry level.
_EXP_DURATION_RE = re.compile(
    r"contract|temporar|assignment|rotation|internship|secondment|fixed[- ]term|"
    r"notice period|probation|duration|programme|term of", re.I)

# YEARS OF SCHOOLING, NOT YEARS OF WORK. Workday's degree picker writes its levels as
# "Bachelors Degree (± 16 years)" and "Masters Degree (± 18 years)" -- sixteen and eighteen years
# of EDUCATION -- and employers paste that straight into the qualifications section. An Abbott
# Project Coordinator whose real requirement is the "Minimum 2 years" two lines below it read as
# a SIXTEEN-year job and vanished from an entry-level feed. Only 18 descriptions in the corpus
# use the notation, but it is the expensive direction on exactly the roles this app is for.
# Anchored to the END of the before-window, so it only fires when the degree and the bracket sit
# immediately against the number.
_EXP_EDU_YEARS_RE = re.compile(
    r"(?:degree|diploma|equivalent|education)\s*\(\s*[±+–-]?\s*$", re.I)

# Words that mark a year-count as an EXPERIENCE requirement (vs. "5 years ago",
# "5-year plan", a tenure/age figure, etc.). Checked just around the match.
#
# The heading words at the end are here because a requirements LIST does not repeat the word
# "experience" on every bullet: "Basic Qualifications: 7+ years of security engineering" states
# a floor and names no experience word within reach of it. 800 descriptions were missed for
# exactly that reason.
_EXP_CTX_RE = re.compile(
    r"experien|\bexp\b|industry|professional|relevant|track record|"
    r"working|in a .{0,25}\brole|of work|background|hands-on|"
    r"qualificat|requirement|must have|you have|proven|demonstrated|"
    # A FIELD LABELLED "Years", which is a structured ATS block rather than prose. Oracle's
    # candidate page renders "Years: 3 to 5+ years" in its requisition field table, and with no
    # context word in reach that read as no requirement at all. The colon is what makes this
    # safe: it matches a LABEL, not the word "years" appearing in a sentence.
    r"\byears?\s*:", re.I)

# THE FOUR THAT ARE ALSO ORDINARY ENGLISH, kept apart from the list above because they need a
# guard the others do not. A bulleted requirement genuinely reads "7+ years in product
# management" with no other context word in reach, so they have to be admitted -- but they are
# also the vocabulary of a company describing itself, and "Acme has been delivering engineering
# services for 15 years" then states a fifteen-year floor. Measured over 8,000 descriptions, the
# DECIDING floor rests on one of these four alone in 1.6% of postings, and five of six sampled by
# hand were genuine requirements; the sixth was a vision statement. Small, but a false HIGH floor
# hides a job the reader qualifies for and they never learn it existed, so it gets the guard.
_EXP_CTX_GENERIC_RE = re.compile(r"engineering|development|management|leadership", re.I)
# A COMPANY TALKING ABOUT ITSELF. Only ever consulted when a generic word is the only context.
# STEMS, NOT WHOLE WORDS -- the same convention as _EXP_CTX_RE above. A trailing \b was the first
# version and it silently matched nothing useful: \bcelebrat\b cannot match "celebrates".
_EXP_TENURE_RE = re.compile(
    r"\b(?:has|have|had|been|since|founded|establish|celebrat|serv|deliver|"
    r"provid|histor|anniversar|grow|operat|proud|legacy|over the (?:past|last))", re.I)
# The tenure check reads FURTHER BACK than the context check does. "Acme has been delivering
# engineering services for 15 years" puts its generic context word ("engineering") 20 characters
# before the number and the phrase that gives it away ("has been delivering") at 50 -- outside
# _EXP_BEFORE, which is deliberately tight because a context word far from a number is weak
# evidence. Evidence AGAINST does not have the same problem, so it gets a wider window.
_EXP_TENURE_BEFORE = 140
_EXP_MIN_RE = re.compile(r"minimum|at\s+least|min\.?\b|no\s+less\s+than", re.I)

# A floor the employer is NOT insisting on. "10+ years preferred" next to "5 years required" is
# a five-year job; taking the maximum over both made it a ten-year job, and the entry-level
# filter then hid a role the reader qualifies for. That is the expensive direction of this
# error -- a job seeker never learns about the posting they were wrongly filtered out of.
_EXP_SOFT_RE = re.compile(
    r"preferred|preferable|nice[\s-]to[\s-]have|a plus|bonus|ideally|desirable|advantage", re.I)
# ...AND THE WORD THAT OUTRANKS IT WHEN BOTH ARE IN REACH. A clause boundary is [;.\n•|] and a
# COMMA is not one, so "10+ years of experience required, 12 years preferred" put both words in
# the same clause and marked the ten-year floor soft as well -- the two floors then both landed
# in `soft` and the posting read as a twelve-year job with no requirement at all. Whichever word
# comes FIRST after the number is the one describing it, which is how the sentence reads aloud.
_EXP_HARD_RE = re.compile(r"required|require\b|must have|mandatory|minimum|at least", re.I)

# How far either side of a match to read for those words. `after` was 45 and that was short by
# about a clause: "5+ years software engineering and/or production experience" puts its only
# context word at character 52.
_EXP_BEFORE, _EXP_AFTER = 30, 80


# Where one requirement stops and the next begins. Bullet lists arrive as newlines from
# core._soup_text, which is why that character is in here alongside the punctuation.
_CLAUSE_SPLIT_RE = re.compile(r"[;.\n•|]")

# THE DEGREE LADDER, and it is the reason a maximum is not simply "the strict reading".
# Measured on Amgen, and the shape is everywhere in big-company reqs:
#
#   "Doctorate degree OR Master's degree and 2 years of experience OR Bachelor's degree and
#    4 years OR Associate's degree and 8 years OR High school diploma and 10 years"
#
# Those are ALTERNATIVES, not a stack. Someone holding a Master's qualifies at two years, so
# the honest floor for this posting is 2 -- and both the old maximum (10) and a naive
# required-only maximum (8) describe a job that does not exist. Read as 8, an entry-level
# filter hides a posting its reader is qualified for, which is this parser's costly direction.
#
# A line only collapses to its minimum when it BOTH offers alternatives and names the degrees
# they trade against; "8+ years of Python and 2 years of SQL" has no degree words and keeps
# its maximum, which is the AND-list the strictness was built for.
_DEGREE_RE = re.compile(
    r"bachelor|master|doctorate|ph\.?\s?d|associate|diploma|\bged\b|high school|degree", re.I)
_ALTERNATIVE_RE = re.compile(r"\bor\b", re.I)


def _clause_after(s):
    """`s` up to the first clause boundary."""
    return _CLAUSE_SPLIT_RE.split(s, 1)[0]


def _clause_before(s):
    """`s` back to the last clause boundary."""
    return _CLAUSE_SPLIT_RE.split(s)[-1]


def _reads_as_preferred(clause_before, clause_after):
    """Is this year count one the employer merely PREFERS, rather than insists on?

    "preferred" is read on BOTH sides -- employers write it either way round, as "preferred: 5
    years" and as "5 years ... preferred". Read across a clause boundary it inverts the answer
    it exists to give, so both windows are clause-scoped before they get here.

    THE NEAREST WORD WINS, measured in characters from the count itself, because a COMMA is not
    a clause boundary and both words routinely share one clause. "10+ years of experience
    required, 12 years preferred" reads soft for the ten if you only look for "preferred", and
    "Minimum 5 years of experience, 10 years preferred" reads soft for the five if you only look
    forward. Distance settles both the way the sentence does: "Minimum" is one character before
    the five, "preferred" is twenty-nine after it.
    """
    best, preferred = None, False
    for m, soft in ((_EXP_SOFT_RE.search(clause_after), True),
                    (_EXP_HARD_RE.search(clause_after), False)):
        if m is not None and (best is None or m.start() < best):
            best, preferred = m.start(), soft
    for rx, soft in ((_EXP_SOFT_RE, True), (_EXP_HARD_RE, False)):
        hits = rx.findall(clause_before)
        if hits:                       # distance BACKWARDS from the count
            d = len(clause_before) - clause_before.lower().rfind(hits[-1].lower()) - len(hits[-1])
            if best is None or d < best:
                best, preferred = d, soft
    return preferred


def _experience_floors_split(text):
    """(hard, soft) — the floors the employer insists on, and the ones it merely prefers.

    A year count only counts when an experience-ish word sits near it, so '10-key' and '401k
    vesting after 3 years' don't masquerade as a requirement. Three notations are read: digits
    ('5+ years'), words ('eight (8) years') and months ('18 months', floor-divided to 1).
    """
    text = text or ""
    hard, soft = [], []
    seen = set()                      # a span can match both the digit and the word pattern
    # RANGES FIRST, then words, then digits. Order is load-bearing: whichever pattern matches a
    # span first claims it, and a range is the case where the three of them would otherwise
    # disagree about where it starts and therefore about which end of it is the floor.
    for rx, kind in ((_EXP_RANGE_RE, "r"), (_EXP_WORD_YEARS_RE, "w"),
                     (_EXP_YEARS_RE, "d"), (_EXP_MONTHS_RE, "m")):
        for m in rx.finditer(text):
            # A TRUE OVERLAP TEST. `s <= m.start() < e` only asks whether the new match STARTS
            # inside a claimed span, and on a MIXED range the two notations claim different
            # halves of it: on "two to 3 years" the digit pass runs first and claims "3 years",
            # then the word pass matches the whole range starting BEFORE that span, so both
            # landed in the list and max() answered 3 for a job whose floor is two. Rare, and
            # again the expensive direction -- it reads a range as its ceiling.
            if any(m.start() < e and s < m.end() for s, e in seen):
                continue
            before = text[max(0, m.start() - _EXP_BEFORE):m.start()]
            after = text[m.end():m.end() + _EXP_AFTER]
            # CLAUSE-SCOPED, like the soft check below and for the same reason. A raw window
            # reads straight across a full stop, and the four generic context words this parser
            # needs in order to read a bulleted requirements list ("engineering", "development",
            # "management", "leadership") are ordinary enough in company prose that "Acme has
            # been delivering engineering services for 15 years." became a fifteen-year floor.
            clause_before, clause_after = _clause_before(before), _clause_after(after)
            ctx = (_EXP_CTX_RE.search(clause_after) or _EXP_CTX_RE.search(clause_before)
                   or _EXP_MIN_RE.search(clause_before))
            if not ctx and (_EXP_CTX_GENERIC_RE.search(clause_after)
                            or _EXP_CTX_GENERIC_RE.search(clause_before)):
                ctx = not _EXP_TENURE_RE.search(
                    _clause_before(text[max(0, m.start() - _EXP_TENURE_BEFORE):m.start()]))
            if kind == "m" and _EXP_DURATION_RE.search(clause_before + " " + clause_after):
                continue                # a contract length, a rotation, a notice period
            if _EXP_EDU_YEARS_RE.search(before):
                continue                # "Bachelors Degree (± 16 years)" -- schooling, not work
            if kind == "r":                       # either notation; group(1) is the floor
                g = m.group(1).lower()
                n = _WORD_NUM[g] if g in _WORD_NUM else int(g)
            elif kind == "d":
                n = int(m.group(1))
            elif kind == "w":
                n = _WORD_NUM[m.group(1).lower()]
            else:
                n = int(m.group(1)) // 12
                if n <= 0:            # under a year: a real floor, and it is zero
                    n = 0
            if n > 20:                # noise ('30 years combined', a company-tenure stat)
                continue
            seen.add((m.start(), m.end()))
            # "preferred" is read on BOTH sides -- employers write it either way round, as
            # "preferred: 5 years" and as "5 years ... preferred" -- but only WITHIN THE SAME
            # CLAUSE. Read across a clause boundary it inverts the answer it exists to give:
            # "5 years of experience required; 10+ years preferred" marked the five-year floor
            # soft as well, both floors became soft, and the maximum came back as ten -- the
            # exact ten-year reading this rule was added to prevent.
            bucket = soft if _reads_as_preferred(clause_before, clause_after) else hard
            bucket.append((m.start(), m.end(), n, bool(ctx)))

    hard = _collapse_ladders(text, hard)
    soft = _collapse_ladders(text, soft)
    return hard, soft


# How much text may sit between two rungs of the same ladder. Amgen's widest gap is ~70
# characters ("... OR Associate's degree and 8 years of experience in Computer Science, IT or
# related fields OR High school diploma / GED and 10 years ...").
_LADDER_GAP = 120


def _collapse_ladders(text, hits):
    """[(start, end, years, had_context)] -> [years], each run of rungs reduced to its lowest.

    ADJACENCY, not lines. The obvious grouping is per line, and it is wrong here: 89.9% of the
    41,434 cached descriptions arrive as ONE blob with no newline in it, so a per-line rule
    puts every year mention in a document into a single group and collapses the lot to its
    minimum the moment the text says "or" and "degree" anywhere — which is nearly always. That
    is precisely the "a senior req hides behind its most junior line item" failure that made
    experience_years take a maximum in the first place; measured, it moved 9,958 descriptions.

    So two hits join a ladder only when they are NEIGHBOURS and the text between them is the
    alternation itself: an "or", a degree word, and less than _LADDER_GAP characters.

    A RUNG DOES NOT HAVE TO REPEAT "OF EXPERIENCE", which is why hits arrive carrying whether
    they had a context word of their own rather than having been filtered on it already.
    Employers drop the phrase on the trailing rung all the time -- "Bachelor's degree and 10
    years of relevant experience, or a Master's degree and 5 years" -- and filtering first threw
    the 5 away, leaving the 10 alone in its group to answer TEN. That is the same ten-versus-five
    error this function exists to prevent, surviving in the commonest phrasing of it; the corpus
    has "...Master's with 6 years, or 12 years in lieu of degree" reading as twelve.

    So a context-less mention may JOIN a ladder but may never start one, and a group that never
    had a context word in it is not a requirement at all and is dropped.
    """
    if not hits:
        return []
    hits = sorted(hits)
    out, group = [], []

    def flush():
        if any(ctx for _s, _e, _n, ctx in group):
            out.append(min(n for _s, _e, n, _c in group))

    for cur in hits:
        if group:
            between = text[group[-1][1]:cur[0]]
            # THE DEGREE MAY BE NAMED JUST AFTER THE SECOND RUNG, and the alternation may not.
            # "Master's with 6 years, or 12 years in lieu of degree" puts only ", or " between
            # the two counts and the word that proves it is a degree ladder immediately after
            # the second -- so a strictly-between test read it as a twelve-year job. The "or"
            # still has to sit BETWEEN, because that is what makes the two counts alternatives
            # rather than a list; only the degree evidence gets the wider window.
            # MEASURED BEFORE ADOPTING, because this rule is the risky one: 40 of 9,000 sampled
            # descriptions change answer (0.44%), against the 9,958 that a per-line grouping
            # moved when it was tried.
            near = text[group[-1][1]:cur[1] + _LADDER_GAP]
            # ...and within ONE sentence. Without this, "9 years of leadership experience.
            # Separately, our CEO has a degree or two and 2 years here" reads as a ladder and
            # answers 2. A real alternation is punctuated with commas and slashes, never a stop.
            if (len(between) <= _LADDER_GAP and not _CLAUSE_SPLIT_RE.search(between)
                    and _ALTERNATIVE_RE.search(between) and _DEGREE_RE.search(near)):
                group.append(cur)
                continue
        flush()
        group = [cur]
    flush()
    return out


def _experience_floors(text):
    """Every stated floor, hard or soft. Kept as the flat list experience_min_years reads."""
    hard, soft = _experience_floors_split(text)
    return hard + soft


def experience_years(text):
    """The HIGHEST experience requirement the text states, or None when it states none.

    STRICT on purpose, and this is the one the FEED reads. "8+ years of engineering
    experience; 2 years of SQL preferred" has floors [8, 2] — it is an 8-year job, and
    reading the floor instead let a senior req hide behind its most junior line item, so
    "Entry · <=2 yrs" returned eight-year roles. A JD that states no year count at all
    returns None and is always KEPT by the filter (many genuine entry-level posts state none).

    REQUIRED beats PREFERRED, which is the 2026-09-03 correction and the only loosening here.
    Taking the maximum over both readings turned "5 years required, 10+ preferred" into a
    ten-year job and hid a role the reader qualifies for — and that is the expensive direction
    of this error, because nobody ever learns about the posting they were wrongly filtered out
    of. When a text states ONLY soft floors the maximum of those still stands: an employer who
    says "10+ years preferred" and nothing else is not describing an entry-level job.
    """
    hard, soft = _experience_floors_split(text)
    fl = hard or soft
    return max(fl) if fl else None


def required_years(text):
    """The HIGHEST experience requirement mentioned (0 if none). Used by the scraper to
    hard-drop roles demanding more than MAX_YEARS where it has the JD (e.g. Amazon)."""
    return experience_years(text) or 0


def experience_min_years(text):
    """The LOWEST experience requirement stated — i.e. the years you need to QUALIFY
    ('3-5 years' -> 3, '5+ years' -> 5). The LENIENT reading: 'what's the floor to be
    considered', not 'the most they'd want'. Deliberately NOT what the feed filters on any
    more — see experience_years — but kept because it answers a real, different question."""
    fl = _experience_floors(text)
    return min(fl) if fl else None


def exp_level_for(years):
    """Coarse bucket from a year COUNT rather than from text, so web._build_row can label a
    stored column without re-reading the JD. Mirrored client-side in app.js's detail modal."""
    if years is None or years == "":
        return ""
    try:
        y = int(years)
    except (TypeError, ValueError):
        return ""
    if y <= 2:
        return "entry"
    if y <= 5:
        return "mid"
    return "senior"


def experience_level(text):
    """Coarse bucket for the feed filter: 'entry' (<=2 yrs), 'mid' (3-5), 'senior' (6+),
    or '' when the JD never states years."""
    return exp_level_for(experience_years(text))


# ------------------------------------------------------------
# THE TITLE AS A FLOOR OF LAST RESORT
# ------------------------------------------------------------
# WHY THIS EXISTS. experience_years reads the DESCRIPTION, and 26% of postings state no number in
# theirs. None means "states no requirement", which the filter reads as KEEP -- so measured on the
# live corpus, "0 to 2 Years" showed 18,331 of 40,294 rows and 14,672 of them (80%) were in only
# because nothing could be read. 4,405 of those had a title that said Senior, Sr, Staff,
# Principal, Lead, Director or VP: a quarter of an entry-level feed was roles announcing in their
# own name that they are not entry level.
#
# THE TITLE IS FREE AND IT IS ON EVERY ROW. Calibrated against the 12,576 postings whose title
# carries one of these words AND whose description does state a floor: 95.7% of them state THREE
# OR MORE years, median 6. Per word -- Director 99.2%, VP 98.6%, Principal 97.0%, Staff 96.7%,
# Senior 95.5%, Sr 95.6%, Lead 94.1%.
#
# WHAT IS DELIBERATELY LEFT OUT, each with the number that excludes it:
#   * "manager" (87.1%) -- it names a FUNCTION, not a level, and it is half of what this app's
#     owner searches for. Including it would cut a project-manager feed in half to fix a
#     seniority problem those rows do not have.
#   * "ii" / "iii" (71.5% / 81.1%), "specialist" (73.8%), "analyst" (75.0%) -- too weak, and
#     "Manager II" is ordinary in this corpus.
#   * bare "associate" (64.1%) -- but "Associate Director" is median 8 years and 78% senior while
#     "Associate <anything else>" is median 3 and 14%, so the compound is listed and the bare
#     word is not.
#
# THE COST IS REAL AND IT IS THE EXPENSIVE DIRECTION: 4.3% of senior-titled postings genuinely
# state two years or fewer, and where the description says so it WINS -- this is only ever
# consulted when there is no stated floor at all. See web._build_row's exp_eff / exp_src.
_TITLE_SENIOR_RE = re.compile(
    r"\b(?:senior|sr|staff|principal|distinguished|fellow|architect|director|lead|"
    r"vp|svp|evp|vice\s+president|head\s+of|chief)\b"
    # c[tefoi]o USED TO SIT IN THE LINE ABOVE and it matched an ORG NAME, not a level.
    # Measured 2026-09-08: "Associate Product Manager, CFO Technology" came back 6,
    # likewise "Technical Product Manager - CTO Office", "Product Manager, CIO
    # Organization" and "Product Manager - COO Office" -- so those rows were hidden
    # from BOTH "0 to 2 Years" and "3 to 5 Years" and badged "senior role" on the card.
    # Six of the eight senior-inferred rows in the real product corpus were this bug.
    # A c-suite TITLE is the person; a c-suite ORG is where the work sits.
    r"|\bc[tefoi]o\b(?!\s*(?:office|organi[sz]ation|org|team|group|technology))"
    r"|\bassociate\s+(?:director|vice\s+president|vp|partner|principal)\b", re.I)
# A junior word VETOES a senior one, because the pair means the junior rung of a senior ladder:
# "Junior Architect", "Associate Director Intern", "Early Career Leadership Program".
_TITLE_JUNIOR_RE = re.compile(
    r"\b(?:intern|interns|internship|co-?op|new\s+grad(?:uate)?|university\s+grad(?:uate)?|"
    r"entry[-\s]level|junior|jr|apprentice|trainee|campus|early\s+career|"
    r"rotational?\s+program(?:me)?)\b", re.I)
# The floor a senior title implies. 6 is the bottom of exp_level_for's "senior" band and the
# median of what these postings actually state, so it reads as "senior" wherever a number is
# shown and excludes the row from both "0 to 2" and "3 to 5".
_TITLE_SENIOR_YEARS = 6


# ------------------------------------------------------------
# THE OTHER NUMBER EVERY POSTING STATES: the degree
# ------------------------------------------------------------
# 68.2% of the 41,434 cached descriptions name a degree, which makes this the second most
# answerable question about a posting after the years -- and, like the years, it was being read
# (as _DEGREE_RE, to detect a ladder) and then thrown away rather than shown.
#
# RANKED, because "Bachelor's or equivalent, Master's preferred" has to come out as two
# different answers and a set of strings cannot say which is the floor. The required degree is
# the LOWEST named, matching the ladder rule in _collapse_ladders: an alternation of degrees is
# a list of ways to qualify, not a stack of demands.
_DEGREE_LEVELS = (
    (1, "High school", r"high school|\bged\b|secondary school"),
    (2, "Associate's", r"associate'?s?\s+degree|\ba\.?a\.?s?\b"),
    (3, "Bachelor's", r"bachelor|\bb\.?s\.?\b|\bb\.?a\.?\b|undergraduate degree|four[- ]year degree"),
    (4, "Master's", r"master'?s|\bm\.?s\.?\b|\bm\.?b\.?a\.?\b|\bmba\b|graduate degree"),
    (5, "Doctorate", r"doctorate|\bph\.?\s?d\.?\b|doctoral"),
)
_DEGREE_RXS = tuple((rank, name, re.compile(pat, re.I)) for rank, name, pat in _DEGREE_LEVELS)


def education_floors(text):
    """(required, preferred) degree names, either of which may be None.

    Same soft/hard split as the years: "Bachelor's degree required, Master's preferred" is a
    bachelor's job, and reading the maximum over both would describe one that does not exist.
    """
    text = text or ""
    hard, soft = [], []
    for rank, name, rx in _DEGREE_RXS:
        for m in rx.finditer(text):
            before = text[max(0, m.start() - _EXP_BEFORE):m.start()]
            after = text[m.end():m.end() + _EXP_AFTER]
            bucket = soft if (_EXP_SOFT_RE.search(_clause_after(after))
                              or _EXP_SOFT_RE.search(_clause_before(before))) else hard
            bucket.append((rank, name))
            break                      # one mention of a level is enough; position is not used
    req = min(hard)[1] if hard else None
    # The highest PREFERRED level, and only when it actually asks for more than the floor --
    # "Bachelor's required, Bachelor's preferred" is one fact written twice.
    pref = None
    if soft:
        top = max(soft)
        floor = min(hard)[0] if hard else 0
        pref = top[1] if top[0] > floor else None
    return req, pref


def experience_floors(text):
    """(required, preferred) year counts, either of which may be None.

    The public form of what _experience_floors_split has computed since 704a290 and no caller
    has ever been able to see: 67.5% of descriptions state a required floor, 4.9% state both and
    3.4% state only a preferred one. experience_years still answers with ONE number because that
    is what a filter can compare; this is for showing the reader what the posting actually said.
    """
    hard, soft = _experience_floors_split(text)
    req = max(hard) if hard else None
    pref = max(soft) if soft else None
    # A PREFERRED FLOOR THAT DOES NOT EXCEED THE REQUIRED ONE IS NOT A SECOND FACT. Google's
    # posting says "5 years of experience in program management" under Minimum and "5 years
    # ... managing cross-functional projects" under Preferred; rendering that as
    # "5+ years required · 5+ preferred" reads like a distinction and there is none.
    if pref is not None and req is not None and pref <= req:
        pref = None
    return req, pref


# ------------------------------------------------------------
# LEVEL -- "how senior is this job", which is NOT "how many years does it state"
#
# Read from the DESCRIPTION first and the title only as a fallback, the same way the years are
# (experience_years, then title_experience_tier, which is documented "Never overrides a
# description"). A level is a claim about the job, so it comes from where the employer described
# the job.
#
# WHY THIS IS A SEPARATE FUNCTION FROM title_experience_tier. That one answers "what YEARS does
# this title imply" and test_experience_years.py records the measurement that keeps it honest:
# bare "associate" is only 64.1% predictive of a low year count, and "Associate Director" is
# median 8 years, so inventing a years FLOOR from it would cut an entry-level project feed in
# half. A LEVEL carries no number and needs no such bar -- "Associate Product Manager" is an
# associate-level posting whatever years its text turns out to state, and where the two disagree
# the text wins because the text is what the employer wrote about THIS req.
#
# MEASURED, 2026-09-08, over 2,575 stored product-role descriptions:
#   * 2,130 state a year count, and exp_level_for already turns that into a level.
#   * 14 more say it only in WORDS, with no number anywhere. Small -- 0.5% -- and correct.
#   * 431 say nothing either way and stay "".
# The value is not the 14 rows. It is that the level then exists as a thing the CARD can draw:
# of the 417 rows the "0 to 2 Years" filter correctly returns, 241 print no level word at all,
# so the reader falls back to the title -- and on those cards the title is wrong 95 times
# ("Product Manager II", "Product Owner I", four Capital One "Senior Associate, Product Manager"
# reqs, where that grade IS the early-career rung).
_JD_ENTRY_RE = re.compile(
    r"\b(?:entry[-\s]?level|new\s+grad(?:uate)?s?|recent\s+grad(?:uate)?s?|"
    r"students?\s+graduating|graduating\s+(?:seniors?|students?)|campus\s+hire|"
    r"no\s+(?:prior\s+)?(?:work\s+)?experience\s+(?:is\s+)?"
    r"(?:necessary|required|needed|expected)|"
    r"early\s+in\s+(?:your|their)\s+career|rotational?\s+program(?:me)?)\b", re.I)
# A LEVEL CLAIM ABOUT SOMEBODY ELSE IS NOT A LEVEL CLAIM ABOUT THIS REQ. Same guard shape as
# _jd_says_remote's _REMOTE_NEG_RE: "you will mentor recent graduates" and "partner with our
# new grad cohort" describe who the hire works WITH.
_JD_ENTRY_NEG_RE = re.compile(
    r"\b(?:mentor(?:ing|s)?|manage|managing|lead(?:ing|s)?|coach(?:ing|es)?|supervis\w+|"
    r"partner\s+with|support(?:ing|s)?|onboard(?:ing|s)?|train(?:ing|s)?|hire|hiring|"
    r"recruit\w*|our|a\s+cohort\s+of)\s+(?:\w+\s+){0,3}$", re.I)
_JD_ENTRY_LOOKBACK = 60


def jd_level(text):
    """"entry" | "mid" | "senior" | "" for a DESCRIPTION.

    The number first, because 83% of stored descriptions state one and exp_level_for is already
    the tested way to bucket it. The words only where there is no number at all -- a posting that
    says "0-2 years" and also says "recent graduates welcome" is answered by its number either
    way, and letting the words override it would be the mistake experience_years' docstring
    warns about in the other direction.
    """
    if not text:
        return ""
    y = experience_years(text)
    if y is not None:
        return exp_level_for(y)
    for m in _JD_ENTRY_RE.finditer(text):
        before = text[max(0, m.start() - _JD_ENTRY_LOOKBACK):m.start()]
        if not _JD_ENTRY_NEG_RE.search(before):
            return "entry"
    return ""


# The TITLE fallback. Bare "associate" and the numeral rung live here and NOT in
# title_experience_tier -- see the note above.
_TITLE_ENTRY_RE = re.compile(
    r"\b(?:associate|assoc|assistant|apm|junior|jr|entry[-\s]?level|new\s+grad(?:uate)?|"
    r"university\s+grad(?:uate)?|graduate|intern|interns|internship|co-?op|apprentice|"
    r"trainee|campus|early\s+career|rotational?)\b"
    r"|\b(?:i|1)\b\s*$|\(\s*(?:l|level)\s*1\s*\)", re.I)
# ...and the leak the senior regex never closed. Measured: "Group Product Manager", "Product
# Manager II/III/IV", "Manager II, Product Management", "Product Manager (L5)" and "Advanced
# Product Manager" all returned tier None and appeared under "0 to 2 Years". These are LEVEL
# claims, not year counts, which is exactly why they belong here rather than in
# title_experience_tier -- 1,098 of 1,106 product titles get None from that function and only 8
# get a number, so a level is the only honest place to put them.
_TITLE_SENIOR_LEVEL_RE = re.compile(
    r"\b(?:group|advanced|expert|master)\s+(?:\w+\s+){0,2}"
    r"(?:manager|management|owner|analyst|engineer|lead)\b"
    r"|\b(?:iii|iv|v|vi)\b|\(\s*(?:l|level)\s*[3-9]\d*\s*\)"
    # ...and the same words written AFTER the noun: "Product Manager - Expert".
    r"|\b(?:manager|management|analyst|engineer|lead|owner)\b[\s,\-]*(?:expert|advanced|master)\b"
    r"|\b(?:l|level)\s*[3-9]\d*\b", re.I)
# ...AND THE SECOND RUNG IS NOT THE SIXTH. "ii" sat in the line above with iii-vi until
# 2026-09-10, which made "Software Engineer II" (70 live rows), "Project Manager II" and
# "Coordinator II" read as SENIOR. It is the weakest numeral in the set -- 71.5% against 81.1%
# for iii, the number the comment above _TITLE_SENIOR_RE already records -- and it is not what
# the word means: II is the rung above entry. Measured on the live corpus, 1,183 active rows
# (3.0%) were senior for no reason but a bare "ii", and they are the on-target ones: Project
# Manager II, Program Manager II, Product Manager II, Technical Program Manager II.
#
# "mid" is not a new vocabulary -- exp_level_for has returned it for 3-5 years all along, and
# web._build_row already documents `level` as "entry" | "mid" | "senior" | "". This just lets a
# TITLE reach the band a year count could already reach.
_TITLE_MID_LEVEL_RE = re.compile(r"\bii\b|\(\s*(?:l|level)\s*2\s*\)|\b(?:l|level)\s*2\b", re.I)
# The lowest year count each level implies, and the exact inverse of exp_level_for's bands
# (entry <= 2, mid 3-5, senior 6+). The experience filter is a CEILING on what an employer
# asks, so a level answers it whenever no year count can: drop the row when the floor its
# level implies is above the ceiling the reader chose. "" means we could not tell and is kept.
LEVEL_MIN_YEARS = {"entry": 0, "mid": 3, "senior": 6}


# "ASSOCIATE" IS TWO DIFFERENT WORDS and this is where they are separated.
#
# "Associate Director" / "Associate VP" / "Associate Partner" is the junior rung of a SENIOR
# ladder -- title_experience_tier already answers 6 for it, and test_experience_years.py freezes
# that with the measurement behind it (median 8 years). Bare "associate" firing first read
# "Associate Director PMO" as entry, which is the opposite of true.
_TITLE_SENIOR_ASSOC_RE = re.compile(
    r"\bassociate\s+(?:director|vice \s*president|vp|svp|partner|principal|"
    r"general \s*counsel|dean|provost)\b", re.I)
# ...and "Senior Associate" cannot be settled from a title at all. At Capital One that grade IS
# the early-career rung -- four such reqs in the measured product corpus read entry from their
# own descriptions -- and in consulting and banking it is mid-level. So the title says nothing
# and jd_level decides, which is the point of reading the description first.
_TITLE_AMBIG_ASSOC_RE = re.compile(r"\b(?:senior|sr)\.?\s+assoc(?:iate)?\b", re.I)


def title_level(title):
    """"entry" | "senior" | "" from a TITLE alone. Only consulted where jd_level returned "".

    PRECEDENCE IS THE WHOLE FUNCTION, in this order:
      1. an unambiguous junior word wins outright, even inside a senior ladder -- the rule
         _TITLE_JUNIOR_RE already encodes ("Associate Director Intern", "Junior Architect");
      2. then the senior-associate forms, so bare "associate" cannot claim them;
      3. then "Senior Associate", which is a genuine coin flip and answers nothing;
      4. then the entry vocabulary, which is where bare "associate" and the numeral rung live;
      5. then seniority, by word or by level number;
      6. then the MIDDLE rung, last of all, so "Senior Engineer II" is senior and a bare
         "Engineer II" is mid.
    """
    t = title or ""
    if not t:
        return ""
    if _TITLE_JUNIOR_RE.search(t):
        return "entry"
    if _TITLE_SENIOR_ASSOC_RE.search(t):
        return "senior"
    if _TITLE_AMBIG_ASSOC_RE.search(t):
        return ""
    if _TITLE_ENTRY_RE.search(t):
        return "entry"
    if _TITLE_SENIOR_RE.search(t) or _TITLE_SENIOR_LEVEL_RE.search(t):
        return "senior"
    if _TITLE_MID_LEVEL_RE.search(t):
        return "mid"                       # the second rung -- see _TITLE_MID_LEVEL_RE
    return ""


# The order of the rungs, lowest first, so a veto can ask "is the title's claim HIGHER".
_LEVEL_ORDER = {"": -1, "entry": 0, "mid": 1, "senior": 2}


def senior_title_veto(level, title):
    """A higher TITLE rung overrules a lower level. THE one rule, shared by both level paths.

    It only compared against "senior" until 2026-09-10, which was all it could do while
    title_level's only rungs were entry and senior. Now that a bare "II" answers "mid" -- see
    _TITLE_MID_LEVEL_RE -- "Product Manager II" stating two years would have fallen all the way
    back to entry, because the veto could not see a claim it had no word for. The rule was
    never about the word "senior"; it is that for a LEVEL question the employer's own name for
    the rung beats a year count. So it compares rungs.

    Years and level are different claims. "2+ years" is the FLOOR an employer will accept;
    "Senior" is the employer's own name for the rung, and for a LEVEL question that is the more
    direct evidence. Without this, a Senior Product Manager whose description happens to state
    "2+ years" reads as entry -- measured on the live feed 2026-09-08, 86 of the 716 rows an
    "Entry Level / Associate" filter returned were senior-titled, 12.0%.

    Same precedence title_level already applies internally, where a junior word beats a senior
    ladder. It only ever moves a level UP to senior, so it cannot manufacture an entry role.

    "Senior Associate" is deliberately unaffected: title_level returns "" for it, because at
    Capital One and the banks that grade IS the early-career rung and the phrase is a genuine
    coin flip.
    """
    if not level:
        return level
    tl = title_level(title)
    if _LEVEL_ORDER.get(tl, -1) > _LEVEL_ORDER.get(level, -1):
        return tl                          # only ever UP, so it cannot manufacture an entry role
    return level


def level_from_exp(exp_eff, exp_src, title):
    """(level, source) for a surface that has the YEARS but not the description.

    The feed is exactly that surface: get_jobs skips the jd column on purpose, so the card and
    the filter cannot call level_for. This keeps the derivation in one place with the veto
    applied, instead of open-coded in web._build_row where it drifted away from level_for.

    Coarser than level_for BY CONSTRUCTION -- a year band is not a level -- which is why the
    veto matters more here, not less.
    """
    level = exp_level_for(exp_eff) if exp_eff is not None else ""
    src = "stated" if (level and exp_src == "stated") else ("inferred" if level else "")
    if not level:
        level = title_level(title)
        src = "inferred" if level else ""
    vetoed = senior_title_veto(level, title)
    if vetoed != level:
        return vetoed, "inferred"          # the TITLE settled it, whatever the years said
    return level, src


def level_for(jd_text, title):
    """The one definition every surface reads: (level, source).

    source is "stated" when the description settled it, "inferred" when the title did, and ""
    when neither could -- the same three-way distinction exp_eff/exp_src already draws, so the
    card can print a word and /job can say where it came from.
    """
    lv = jd_level(jd_text)
    if lv:
        # THE SAME VETO the card path gets. A description that says '2+ years' inside a
        # Senior Product Manager posting is stating a floor, not a level.
        vetoed = senior_title_veto(lv, title)
        return (vetoed, "inferred") if vetoed != lv else (lv, "stated")
    lv = title_level(title)
    return (lv, "inferred") if lv else ("", "")


def title_experience_tier(title):
    """The years a TITLE implies, or None when it implies nothing. Never overrides a description.

    Only ever consulted where experience_years returned None -- a posting that states its own
    floor is believed even when its title disagrees, because it is the employer's own number.
    """
    t = title or ""
    if _TITLE_JUNIOR_RE.search(t):
        return None
    return _TITLE_SENIOR_YEARS if _TITLE_SENIOR_RE.search(t) else None


# ------------------------------------------------------------
# Fetch a job description page (best-effort; paste fallback in the UI)
# ------------------------------------------------------------
# WHERE A JOB PAGE KEEPS THE POSTING, most specific first. Tried in order; the first region
# with enough text wins, and if none does we fall back to the whole document exactly as before.
_MAIN_SELECTORS = ("[itemprop=description]", "[data-automation-id=jobPostingDescription]",
                   "[class*=job-description]", "[id*=job-description]",
                   "[class*=jobDescription]", "main", "article", "[role=main]")
# Below this a "region" is a heading or a spinner, not a description. Same number as
# score_jobs.MIN_PAGE_JD_CHARS, which gates the whole branch one level up.
_MAIN_MIN_CHARS = 250


def _main_region(soup):
    """The element holding the posting, or None to mean "use the whole document"."""
    for sel in _MAIN_SELECTORS:
        try:
            el = soup.select_one(sel)
        except Exception:              # a selector lxml's CSS layer will not accept
            continue
        if el is not None and len(el.get_text(" ", strip=True)) >= _MAIN_MIN_CHARS:
            return el
    return None


def fetch_jd(url, limit=8000):
    """Last-resort page scrape: the branch score_jobs.detail_jd reaches for hosts with no API.

    THE ONLY STEP IN THAT CHAIN THAT CAN SUCCEED AT READING THE WRONG THING -- see the note at
    score_jobs.py:988. It used to take get_text over the ENTIRE document with no notion of a
    content region, so cookie bars, breadcrumbs, "Related jobs", "Share this job" and
    "Click the link below" (whose link get_text drops, leaving the sentence pointing at nothing)
    all became the description. Measured across the stored corpus: skip-to-content 3.4%,
    share-this-job 1.7%, related-jobs 1.1%, click-the-link-below 0.6%.
    """
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        # aside/noscript/svg and aria-hidden were not in the list; a "Related jobs" rail is
        # almost always an <aside>, and an aria-hidden node is furniture by its own admission.
        for tag in soup(["nav", "header", "footer", "form", "aside", "noscript", "svg"]):
            tag.decompose()
        for tag in soup.select('[aria-hidden="true"]'):
            tag.decompose()
        region = _main_region(soup)
        # _soup_text handles script/style and the block boundaries, so a page scrape now carries
        # the same structure an ATS HTML field does. NOT html_to_text: the response body is real
        # HTML, whose entities the parser decodes itself, and unescaping it again would decode
        # an intended "&amp;lt;" twice.
        return _soup_text(region if region is not None else soup)[:limit]
    except Exception:
        return ""


# ------------------------------------------------------------
# Read an UPLOADED resume back into plain text
#
# Everything downstream — the match score, the digest, Resume Brain — works on the plain text
# from db.profile_text(), so the file itself is never stored. It is parsed in memory and
# discarded. That keeps this feature out of the questions that come with holding user documents
# (where they live, who can read them, how they get deleted), and there is nothing to back up.
# ------------------------------------------------------------
RESUME_UPLOAD_MAX_BYTES = 4 * 1024 * 1024      # a resume is a few pages; 4 MB is generous
_RESUME_PDF_MAX_PAGES = 40                     # bound the work a crafted file can ask for
RESUME_UPLOAD_EXTS = (".pdf", ".docx", ".txt", ".md", ".tex")


def _readable_formats_phrase(exclude=""):
    """Which upload formats this host can ACTUALLY read, named in a sentence.

    The refusal message used to be hardcoded as "upload a plain-text or PDF copy" — and it fires
    when the PDF extractor is missing, so it named the exact format it had just refused. That is
    the state a fresh cPanel deploy is in until Run Pip Install has been pressed (web.py records
    a live host hitting it), so the one person most likely to see this got the least useful
    sentence available.

    Derived by probing the imports rather than listing them, so it cannot drift from reality.
    """
    have = [".txt", ".md"]                     # stdlib decode; always available
    try:
        import pypdf                           # noqa: F401
        have.append(".pdf")
    except Exception:
        pass
    try:
        import docx                            # noqa: F401
        have.append(".docx")
    except Exception:
        pass
    have.append(".tex")                        # pure-Python parser in this module
    names = {".pdf": "a PDF", ".docx": "a Word .docx", ".txt": "a plain-text",
             ".md": "a Markdown", ".tex": "a LaTeX"}
    opts = [names[e] for e in have if e != (exclude or "").lower() and e in names]
    if not opts:
        return "a plain-text copy"
    if len(opts) == 1:
        return opts[0] + " copy"
    return ", ".join(opts[:-1]) + " or " + opts[-1] + " copy"


def _docx_to_text(data):
    from docx import Document                  # already a dependency: core writes .docx too
    doc = Document(BytesIO(data))
    out = [p.text for p in doc.paragraphs]
    # Plenty of resumes lay dates and employers out in a borderless table, and those cells are
    # NOT in doc.paragraphs — miss them and half the work history silently disappears.
    for t in doc.tables:
        for row in t.rows:
            out.append("\t".join(c.text.strip() for c in row.cells))
    return "\n".join(out)


_PDF_SPLIT_HYPHEN_RE = re.compile(r"(\w) -(\w)")


def _fix_pdf_artifacts(text):
    """Undo the spacing damage PDF text extraction does.

    Extraction reads glyph positions, so kerning around a hyphen becomes a real space: a résumé
    reading "Excel-based" comes back as "Excel -based", and "RFID-based" as "RFID -based". Left
    alone it breaks keyword matching (the compound no longer matches), trips the spacing check, and
    reads as sloppy writing in a panel that is telling the user their writing is sloppy.

    Only the no-space-after case is touched, so a real spaced dash (" - ") is left alone.
    """
    return _PDF_SPLIT_HYPHEN_RE.sub(r"\1-\2", text or "")


def _pdf_to_text(data):
    from pypdf import PdfReader
    reader = PdfReader(BytesIO(data))
    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")                 # many resumes are "protected" with an empty owner
        except Exception:                      # password; a real one is a clear error below
            return ""
    return _fix_pdf_artifacts("\n".join((p.extract_text() or "")
                                        for p in reader.pages[:_RESUME_PDF_MAX_PAGES]))


_TEX_ITEM_RE = re.compile(r"^\s*\\item\s*", re.M)
_TEX_CMD_ARG_RE = re.compile(r"\\(?:section|subsection|textbf|textit|emph|underline|href|texttt)"
                             r"\*?(?:\[[^\]]*\])?\{([^{}]*)\}")
_TEX_CMD_RE = re.compile(r"\\[A-Za-z@]+\*?(?:\[[^\]]*\])?")
_TEX_COMMENT_RE = re.compile(r"(?<!\\)%.*$", re.M)


def tex_to_text(src):
    """LaTeX source -> the prose inside it.

    Résumés written in LaTeX are a real input (resume_brain already RENDERS to .tex), but scoring
    the source directly is meaningless: every \\textbf and \\begin{itemize} would read as prose,
    the bullet glyphs are \\item rather than a dash, and the rubric would report a résumé made
    almost entirely of unquantified non-verb lines.

    Deliberately a stripper, not a parser. It keeps the argument of the few commands that wrap
    VISIBLE text, turns \\item into a dash so the bullet detector sees bullets, and drops the rest.
    A full TeX parser is not worth carrying to grade a document.
    """
    s = _TEX_COMMENT_RE.sub("", src or "")
    s = re.sub(r"\\begin\{[^}]*\}(?:\[[^\]]*\])?|\\end\{[^}]*\}", "\n", s)
    for _ in range(3):                       # nested \textbf{\href{..}{..}} needs a few passes
        s, n = _TEX_CMD_ARG_RE.subn(r"\1", s)
        if not n:
            break
    s = _TEX_ITEM_RE.sub("- ", s)
    s = _TEX_CMD_RE.sub(" ", s)
    # Escaped specials come back as themselves BEFORE the command stripper runs, and \$ matters
    # most: dropping it turns "\$1.2M of licence cost" into ".2M" and the quantified-impact check
    # loses the one number in the bullet.
    for esc, plain in (("\\$", "$"), ("\\&", "&"), ("\\%", "%"), ("\\#", "#"), ("\\_", "_")):
        s = s.replace(esc, plain)
    s = s.replace("~", " ").replace("\\\\", "\n")
    s = re.sub(r"[{}]", "", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def resume_text_from_upload(filename, data):
    """(text, error) from an uploaded resume. Never raises, never touches disk.

    A scanned PDF parses fine and yields nothing — that is not an error the user can debug from
    a stack trace, so it gets its own message naming the likely cause.
    """
    name = (filename or "").strip().lower()
    if not data:
        return "", "That file was empty."
    if len(data) > RESUME_UPLOAD_MAX_BYTES:
        return "", ("That file is %.1f MB. The limit is %d MB."
                    % (len(data) / 1048576.0, RESUME_UPLOAD_MAX_BYTES // 1048576))
    ext = os.path.splitext(name)[1]
    if ext == ".doc":
        return "", ("Old-style .doc isn't supported. Re-save it as .docx or PDF, "
                    "or paste the text below.")
    if ext not in RESUME_UPLOAD_EXTS:
        return "", "Upload a PDF, Word or plain text file, or paste the text below."
    try:
        if ext == ".pdf":
            text = _pdf_to_text(data)
        elif ext == ".docx":
            text = _docx_to_text(data)
        elif ext == ".tex":
            text = tex_to_text(data.decode("utf-8", "replace"))
        else:
            text = data.decode("utf-8", "replace")
    except ImportError:
        # Names the fix, because "missing library" is the server's problem and the user cannot act
        # on it — but whoever runs the server can, and they are usually the same person here.
        # The cPanel remedy ("Setup Python App, Run Pip Install") used to be in this sentence.
        # It is an instruction for whoever runs the server, shown to whoever uploaded a file, and
        # only one of those people can act on it. Logged for the operator, plain text for the user.
        logging.warning("resume upload: no extractor installed for %s files", ext)
        return "", ("This server can't read %s files yet. Paste the text below instead, or "
                    "upload %s." % (ext, _readable_formats_phrase(exclude=ext)))
    except Exception:
        # Malformed, encrypted, or not really the format its extension claims.
        return "", ("Couldn't read that %s. It may be password-protected or corrupted. "
                    "Try paste instead." % ext)
    text = re.sub(r"[ \t]+\n", "\n", (text or "").replace("\r\n", "\n").replace("\r", "\n"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) < 40:
        return "", ("That file had almost no readable text. A scanned or image-only PDF has "
                    "none to extract, so paste the text below instead.")
    return text, ""


# ------------------------------------------------------------
# Export the (edited) resume to .docx
# ------------------------------------------------------------
def resume_to_docx_bytes(text):
    """Turn plain-text resume into a simple .docx. ALL-CAPS short lines become
    bold headings; lines starting with - or • become bullets."""
    from docx import Document  # python-docx
    doc = Document()
    for raw in (text or "").split("\n"):
        s = raw.strip()
        if not s:
            doc.add_paragraph("")
        elif s.isupper() and len(s) <= 40:
            p = doc.add_paragraph()
            p.add_run(s).bold = True
        elif s[0] in "-•*":
            doc.add_paragraph(s.lstrip("-•* ").strip(), style="List Bullet")
        else:
            doc.add_paragraph(s)
    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ------------------------------------------------------------
# Optional: tailor the resume to a JD with Claude
# (needs `pip install anthropic` and ANTHROPIC_API_KEY set)
# ------------------------------------------------------------
def ai_available():
    """True if an AI key is configured server-side (Gemini preferred, Anthropic optional)."""
    return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"))


def _tailor_prompt(resume_text, jd_text, intensity=None):
    """The /tailor + /api/tailor + extension prompt. Style rules come from resume_brain.voice,
    the same module resume_brain.ai._rewrite_prompt uses — this file used to carry its own copy
    of the guidance, and both copies said "strong action verbs", which is precisely how you get
    a résumé full of Spearheaded and Leveraged. Import is local so core.py stays importable
    even if the package is absent (the scraper imports core and never needs the prompt)."""
    from resume_brain import voice
    return (
        "Tailor this candidate's resume to ONE specific job. Produce the strongest TRUE version "
        "of THIS candidate's resume for THIS job.\n\n"
        "Do this:\n"
        "1. Lead with the experience, projects and skills most relevant to what the job asks "
        "for.\n"
        "2. Use the job's own terminology (titles, tools, methods) wherever the candidate "
        "genuinely has that experience — that is what an ATS keyword screen looks for. Use their "
        "TERMS, not their tone: a resume written in a job ad's voice reads like a job ad.\n"
        "3. Keep and surface every quantified result already in the resume. Where a bullet makes "
        "a claim with no number, sharpen the claim rather than inventing one.\n"
        "4. Fold the job's must-have skills that the candidate actually has into the Skills "
        "section.\n"
        "5. Keep every real section (contact, summary, experience, education, skills) and all "
        "true content. Plain text, standard headings.\n\n"
        "Hard rules (must follow):\n"
        "- NEVER invent or exaggerate experience, employers, titles, dates, degrees, metrics, "
        "or skills the candidate does not have. Truthful reorder/reword only.\n"
        "- Do not list skills the resume doesn't support.\n"
        "- Output ONLY the finished resume text — no preamble, notes, or explanation.\n\n"
        + voice.writing_rules(intensity) +
        "\n=== TARGET JOB DESCRIPTION ===\n%s\n\n"
        "=== CANDIDATE'S CURRENT RESUME ===\n%s\n\n"
        "=== TAILORED RESUME (output only this) ===" % (jd_text, resume_text)
    )


# ---- Gemini (Google AI Studio) via REST — no SDK needed, just `requests` ----
# Default to Gemini Flash 3.5 (best Flash quality). If that exact id isn't on the key it
# auto-falls back to the best available Flash. Override with the GEMINI_MODEL env var.
GEMINI_DEFAULT_MODEL = "gemini-3.5-flash"


def _gemini_list_models(api_key):
    """Model short-names that support generateContent (e.g. 'gemini-3.5-flash')."""
    r = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
                     params={"key": api_key}, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return [(m.get("name") or "").split("/")[-1] for m in r.json().get("models", [])
            if "generateContent" in (m.get("supportedGenerationMethods") or [])]


def _gemini_discover(api_key):
    """Best available stable Flash (then Pro) model — used only if the preferred id 404s."""
    try:
        def ok(m):
            bad = ("vision", "tts", "image", "audio", "embedding", "exp",
                   "preview", "learnlm", "aqa", "gemma")
            return bool(m) and not any(b in m for b in bad)
        models = _gemini_list_models(api_key)
        flash = sorted([m for m in models if "flash" in m and ok(m)], reverse=True)
        pro = sorted([m for m in models if "pro" in m and ok(m)], reverse=True)
        return (flash or pro or [m for m in models if ok(m)] or [GEMINI_DEFAULT_MODEL])[0]
    except Exception:
        return GEMINI_DEFAULT_MODEL


def tailor_with_gemini(resume_text, jd_text, api_key, model=None, intensity=None):
    """Rewrite the résumé for a JD with Google's Gemini API (REST). Truthful reorder/reword
    only. Uses Gemini Flash 3.5 with dynamic 'thinking' ON for a stronger result (slower +
    more tokens — by design). `api_key` = a Google AI Studio key (starts 'AIza')."""
    if not api_key:
        raise RuntimeError("No Gemini API key provided.")
    prompt = _tailor_prompt(resume_text, jd_text, intensity)
    mdl = model or os.environ.get("GEMINI_MODEL") or GEMINI_DEFAULT_MODEL

    def _call(m, think=True):
        gen = {"maxOutputTokens": 8192, "temperature": 0.45}
        if think:
            gen["thinkingConfig"] = {"thinkingBudget": -1}      # dynamic: reason as long as helpful
        body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": gen}
        return requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % m,
            params={"key": api_key}, json=body, timeout=120)

    r = _call(mdl)
    if r.status_code == 404:                                # preferred model not on this key
        alt = _gemini_discover(api_key)
        if alt and alt != mdl:
            mdl = alt
            r = _call(mdl)
    if r.status_code == 400 and "think" in (r.text or "").lower():
        r = _call(mdl, think=False)                         # model doesn't accept thinkingConfig
    if r.status_code >= 400:
        raise RuntimeError("Gemini API %s: %s" % (r.status_code, (r.text or "")[:200]))
    data = r.json()
    cands = data.get("candidates") or []
    if not cands:
        raise RuntimeError("Gemini returned no text (possibly blocked): %s"
                           % str(data.get("promptFeedback") or "")[:150])
    parts = ((cands[0].get("content") or {}).get("parts")) or []
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
    if not text:
        raise RuntimeError("Gemini returned an empty response (try again).")
    return text


def tailor_with_ai(resume_text, jd_text, model=AI_MODEL, api_key=None, intensity=None):
    """Anthropic/Claude variant of tailor_with_gemini. Calls the Messages REST API with `requests`
    (no `anthropic` SDK needed — same pattern as the Gemini path). Model: ANTHROPIC_MODEL env, else
    the passed model (default AI_MODEL)."""
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("No Anthropic API key provided.")
    body = {
        "model": os.environ.get("ANTHROPIC_MODEL") or model or AI_MODEL,
        "max_tokens": 8192,                    # headroom for a full résumé rewrite
        "messages": [{"role": "user", "content": _tailor_prompt(resume_text, jd_text, intensity)}],
    }
    headers = {"content-type": "application/json", "x-api-key": api_key, "anthropic-version": "2023-06-01"}
    r = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=body, timeout=120)
    if r.status_code >= 400:
        try:
            detail = (r.json().get("error") or {}).get("message") or r.text
        except Exception:
            detail = r.text
        raise RuntimeError("Claude API %s: %s" % (r.status_code, (detail or "")[:200]))
    blocks = r.json().get("content") or []
    text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text").strip()
    if not text:
        raise RuntimeError("Claude returned an empty response (try again).")
    return text


def tailor(resume_text, jd_text, api_key, intensity=None):
    """Tailor with whichever provider the key implies: Claude for an `sk-ant-…` key (or
    AI_PROVIDER=claude), else Gemini. Both go over REST — no SDK / extra package."""
    if not api_key:
        raise RuntimeError("No AI API key provided.")
    if str(api_key).startswith("sk-ant-") or os.environ.get("AI_PROVIDER") == "claude":
        return tailor_with_ai(resume_text, jd_text, api_key=api_key, intensity=intensity)
    return tailor_with_gemini(resume_text, jd_text, api_key, intensity=intensity)
