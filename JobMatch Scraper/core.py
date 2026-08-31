"""
core.py — all the logic for the job-match app, kept free of Streamlit so it can
be tested on its own. app.py imports from here and only handles the UI.
"""

import csv
import html
import logging
import os
import re
import json
import math
import datetime
from io import BytesIO
from collections import Counter
from functools import lru_cache

import requests
from bs4 import BeautifulSoup

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


def extract_keywords(text, top_n=28, max_bigrams=8, extra_skip=None):
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

    bigrams = [k for k, _ in sorted(bi_counter.items(),
                                    key=lambda kv: (kv[1], len(kv[0])), reverse=True)][:max_bigrams]
    bigram_words = {w for bg in bigrams for w in bg.split()}
    unigrams = [k for k, _ in sorted(uni_counter.items(),
                                     key=lambda kv: (kv[1], kv[0]), reverse=True)]

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
    _idf_cache["idf"] = None
    _idf_cache["loaded"] = False


def save_idf(idf, path=_IDF_PATH):
    try:
        json.dump(idf, open(path, "w", encoding="utf-8"))
        if path == _IDF_PATH:                 # keep the in-process cache in step with the file
            _idf_cache["idf"] = idf
            _idf_cache["loaded"] = True
    except Exception:
        pass


def load_idf(path=_IDF_PATH):
    # Only the default path is memoized; an explicit path always re-reads.
    if path == _IDF_PATH and _idf_cache["loaded"]:
        return _idf_cache["idf"]
    idf = None
    if os.path.exists(path):
        try:
            idf = json.load(open(path, encoding="utf-8"))
        except Exception:
            idf = None
    if path == _IDF_PATH:
        _idf_cache["idf"] = idf
        _idf_cache["loaded"] = True
    return idf


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

ATS_KEYWORDS = {
    # tools
    "jira", "confluence", "asana", "trello", "smartsheet", "monday.com", "wrike", "clickup",
    "ms project", "microsoft project", "primavera", "sharepoint", "excel", "google sheets",
    "powerpoint", "visio", "lucidchart", "miro", "notion", "sql", "tableau", "power bi",
    "looker", "salesforce", "sap", "oracle", "netsuite", "workday", "servicenow", "python", "git",
    # methods / frameworks
    "agile", "scrum", "kanban", "safe", "lean", "six sigma", "lean six sigma", "waterfall",
    "sdlc", "devops", "kaizen", "pmbok", "prince2", "itil", "okr", "okrs", "kpi", "kpis",
    "gantt", "sprint", "backlog", "retrospective", "scrum master", "product owner",
    # certifications
    "pmp", "capm", "csm", "psm", "cspo", "cbap", "green belt", "black belt",
    # PM / analyst / ops domain
    "project management", "program management", "project manager", "program manager",
    "project coordinator", "stakeholder management", "stakeholder", "risk management",
    "change management", "budget", "budgeting", "cost management", "resource allocation",
    "scope management", "requirements gathering", "business requirements", "user stories",
    "process improvement", "process mapping", "gap analysis", "data analysis", "reporting",
    "dashboards", "forecasting", "vendor management", "procurement", "milestones",
    "deliverables", "cross-functional", "roadmap", "status reporting", "project plan",
    "business analysis", "operations", "implementation", "onboarding", "sla", "metrics",
}


# A JD this short (chars) or this term-poor after boilerplate stripping is too thin to score
# honestly — e.g. the truncated Adzuna/Oracle blurbs that otherwise yield a handful of generic
# terms a broad résumé fully covers, producing a misleading ~100%. Flagged as "thin" so callers
# show "JD pending" instead of a confident number (see score_pending in web._build_row).
_MIN_JD_CHARS = 400
_MIN_JD_TERMS = 6


def html_to_text(raw):
    """HTML (or already-plain) text -> clean text.

    Lived in scraper/score_jobs.py as _text until the SWEEP needed it too: several ATS list
    feeds hand back the description alongside the listing, and scraper cannot import score_jobs
    because score_jobs imports scraper. One definition here; both callers delegate to it.
    """
    if not raw:
        return ""
    soup = BeautifulSoup(html.unescape(raw), "lxml")
    return re.sub(r"\s{2,}", " ", soup.get_text(" ", strip=True))


def analyze_jd(jd_text, idf=None):
    """The résumé-INDEPENDENT half of the ATS match: the JD's important keywords and each
    one's weight. Depends only on the JD text, idf, the ATS keyword set, and the JD's
    requirements section — NOT the résumé — so it can be computed once per job and reused
    for every résumé and every page render.

    Returns {"terms": [term, ...], "weight": {term: w}, "total": float, "thin": bool}.
    """
    jd_low = (jd_text or "").lower()
    req_low = _requirements_text(jd_text).lower()

    # The JD's important keywords: its salient terms + any hard ATS keywords it names.
    salient = extract_keywords(jd_text, top_n=30)
    # "thin" is judged on the JD's own substance (length + salient-term count), NOT on the ATS
    # keywords a broad résumé would trivially match — so a truncated blurb stays flagged.
    thin = len(jd_low.strip()) < _MIN_JD_CHARS or len(salient) < _MIN_JD_TERMS
    jd_terms = set(salient)
    jd_terms |= {kw for kw in ATS_KEYWORDS if kw in jd_low}
    if not jd_terms:
        return {"terms": [], "weight": {}, "total": 0.0, "thin": True}

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
    known = sorted(idf.values()) if idf else []
    default_w = known[len(known) // 2] if known else 1.0

    def wt(t):
        w = idf.get(t, default_w) if idf else 1.0
        if t in ATS_KEYWORDS:        # hard skill / tool / cert, what an ATS weights most
            w *= 2.5
        else:
            w = min(w, _RARE_W_CAP)
        if t in req_low:             # stated in the requirements/qualifications section
            w *= 1.6
        return w

    terms = list(jd_terms)           # freeze the set's iteration order ONCE (see score_against)
    weight = {t: wt(t) for t in terms}
    total = sum(weight[t] for t in terms)
    return {"terms": terms, "weight": weight, "total": total, "thin": thin}


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
            "business", "process", "access", "class", "series", "status", "campus"}


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


@lru_cache(maxsize=8)
def _resume_wordset(resume_low):
    """(whole word-tokens, their stems) for a lowercased résumé, memoized so user_scores can
    reuse it across every job in its loop instead of re-tokenizing per job.

    Returns a pair so the exact-match path stays exact — a stem is a fallback, not a
    replacement, and checking the literal token first keeps the common case free.
    """
    toks = frozenset(WORD_RE.findall(resume_low))
    return toks, frozenset(_stem(w) for w in toks)


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
    """
    toks, stems = words if isinstance(words, tuple) else (words, frozenset())
    if " " in t or any(ch in t for ch in "+#./-"):
        if t in resume_low:
            return True
        if any(f != t and f in resume_low for f in _alias_forms(t)):
            return True
        canon = _canon_phrase(t)
        # A phrase matches when EVERY word of it is present as a stem -- "project management"
        # against "managed multiple projects". All of it, not any of it: "risk management" must
        # never be answered by the word "management" on its own.
        parts = [w for w in re.split(r"[^a-z0-9+#]+", canon) if w]
        return bool(parts) and all(_stem(w) in stems for w in parts)
    if t in toks:
        return True
    for f in _alias_forms(t):
        if f in toks or _stem(f) in stems or (" " in f and f in resume_low):
            return True
    return False


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
    return {"analyzed": analyze_jd(jd_text, idf),
            "exp_years": experience_years(jd_text),
            "exp_level": experience_level(jd_text),
            "sponsor_jd": list(sponsorship_from_jd(jd_text))}


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


def visa_tags_match(row_tags, wanted):
    """OR semantics: a row passes if it carries ANY wanted tag. No wanted tags == no filter.

    OR rather than AND on purpose — five AND-ed checkboxes return almost nothing, and the
    question a user is asking is "H-1B *or* green card", not "both at once".
    """
    if not wanted:
        return True
    return bool(set(wanted) & set(row_tags or ()))


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
      "project analyst", "project specialist", "project support", "project administrator")),
    ("program",    "Program Manager",       "deliver",   # 518 + 347 + 50
     ("program manager", "technical program manager", "program management", "tpm",
      "program mgr", "prog mgr", "pgm mgr", "program analyst", "program administrator",
      # British spelling, and the one misspelling that measured non-zero (3 Amazon postings).
      "programme manager", "programme management", "program manger")),
    ("product",    "Product Manager",       "deliver",   # 905 + 57 + 52
     ("product manager", "technical product manager", "product owner",
      "associate product manager", "product management")),
    ("coordinator", "Project / Program Coordinator", "deliver",   # 126 + 50
     ("project coordinator", "program coordinator", "operations coordinator",
      "project administrator", "projects coordinator", "programs coordinator",
      "programme coordinator")),
    ("scrum",      "Scrum Master / Agile",  "deliver",
     ("scrum master", "agile coach", "release train engineer", "agile delivery",
      "product owner")),
    ("consultant", "Implementation / Solutions Consultant", "deliver",
     ("implementation consultant", "implementation specialist", "implementation manager",
      "solutions consultant", "solutions architect", "technical consultant")),
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
      "web developer")),
    ("devops",     "DevOps / SRE",          "eng",       # 114 + 129 + 41 + 41
     ("devops engineer", "site reliability engineer", "sre", "platform engineer",
      "infrastructure engineer", "cloud engineer", "devsecops engineer")),
    ("qa",         "QA / Test Engineer",    "eng",       # 130 + 88
     ("qa engineer", "test engineer", "quality assurance engineer", "automation engineer",
      "test automation engineer", "sdet")),
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
     ("data engineer", "analytics engineer", "etl developer", "data platform engineer")),
    ("datasci",    "Data Scientist",        "data",      # 238 + 101
     ("data scientist", "applied scientist", "research scientist", "data science")),
    ("dataanalyst", "Data Analyst",         "data",      # 86
     ("data analyst", "analytics analyst", "reporting analyst", "bi analyst",
      "business intelligence analyst")),
    ("ml",         "Machine Learning / AI", "data",      # 169 + 73 + 60
     ("machine learning engineer", "ml engineer", "ai engineer", "deep learning engineer",
      "computer vision engineer", "nlp engineer", "mlops engineer",
      "artificial intelligence engineer")),

    ("ba",         "Business Analyst",      "biz",       # 198
     ("business analyst", "business systems analyst", "business process analyst")),
    ("ops",        "Operations Manager",    "biz",       # 381 + 70
     ("operations manager", "operations lead", "branch operations", "business operations",
      "operations supervisor")),
    ("finance",    "Financial Analyst",     "biz",       # 328
     ("financial analyst", "finance analyst", "fp&a analyst", "budget analyst")),
    ("supply",     "Supply Chain / Logistics", "biz",    # 54
     ("supply chain manager", "supply chain analyst", "logistics manager",
      "procurement analyst", "supply chain")),
]
ROLE_KEYS = tuple(k for k, _l, _g, _p in ROLE_FAMILIES)
ROLE_LABELS = {k: lab for k, lab, _g, _p in ROLE_FAMILIES}
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
        hit = tuple(k for k in ROLE_KEYS if _ROLE_RES[k].search(t))
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
    t = title or ""
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
    "go-to-market", "messaging and positioning", "seo", "paid media", "brand strategy",
    # design
    "figma", "wireframes", "user research", "visual design", "design system", "ux design",
    "interaction design", "design reviews",
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
PM_MIN_ANCHORS = 3
PM_MIN_POINTS = 8
PM_ANCHOR_WEIGHT = 2
# How many distinct PM_VETO phrases it takes to say "this is a different job". Two, not one:
# see the note on reads_like_pm.
PM_MAX_VETO = 2


def pm_signal(text):
    """(distinct anchors, distinct support, distinct veto phrases) in a posting's text."""
    if not text:
        return 0, 0, 0
    return (len({m.group(0).lower() for m in _PM_ANCHOR_RE.finditer(text)}),
            len({m.group(0).lower() for m in _PM_SUPPORT_RE.finditer(text)}),
            len({m.group(0).lower() for m in _PM_VETO_RE.finditer(text)}))


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
    """May this title's DESCRIPTION be read as a second opinion? Cheap, and text-free."""
    if not title:
        return False
    if _PM_REFUSE_RE.search(title):
        return False
    return bool(_PM_HINT_RE.search(title))


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
AGGREGATOR_HOSTS = ("adzuna.", "indeed.", "linkedin.", "ziprecruiter.", "glassdoor.")

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
    loc = re.sub(r"[^a-z0-9]+", " ", (location or "").lower()).strip()
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
MIN_SCALE = 3

DEFAULT_PREFS = {
    # "I have at least half the skills this job emphasises." On the v3 scale that admits about
    # a quarter of the corpus, against 5.7% for the old 45 — so the feed is far wider than it
    # was without the number ever flattering anyone.
    "min": 50,            # minimum match %
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
    "exp": "any",         # any | 2 | 5 | senior
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
    "sort": "score",
    "alerts": "off",      # off | daily  — email digest of new matches
    "alert_min": 0,       # extra match floor for the email only; 0 = use `min`
}
_PREF_CHOICES = {
    "exp": ("any", "2", "5", "senior"),
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
    if not visa_tags_match(row.get("visa"), parse_visa_pref(p.get("visatags"))):
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
    if exp != "any":
        # The HIGHEST year count the JD states (core.experience_years). A JD that states none
        # is always kept — same rule as web._filter_rows and app.js matches().
        ev = row.get("exp_years")
        if ev not in ("", None):
            try:
                yrs = int(ev)
            except (TypeError, ValueError):
                yrs = None
            if yrs is not None:
                if exp == "senior":
                    if yrs >= 6:
                        return False
                elif yrs > (int(exp) if str(exp).isdigit() else 99):
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


def digest_row(job, score, everify_index=None, visa_index=None, counts_index=None):
    """The row shape prefs_match wants, built from a RAW db job row.

    The email path has no access to web.py's _build_row (importing Flask into the scraper
    would be absurd), so this derives the same fields from the job itself using the same
    core helpers the feed uses.
    """
    company = job.get("company") or ""
    jd = job.get("jd") or ""
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
        "exp_years": experience_years(jd) if jd else "",
        "intern": bool(re.search(r"\b(intern|internship|co-?op)\b", job.get("title") or "", re.I)),
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
_EXP_YEARS_RE = re.compile(
    r"(\d{1,2})\s*(?:\+|(?:\s*(?:-|–|—|to)\s*\d{1,2})\s*\+?)?\s*(?:years?|yrs?)\b", re.I)
# Words that mark a year-count as an EXPERIENCE requirement (vs. "5 years ago",
# "5-year plan", a tenure/age figure, etc.). Checked just around the match.
_EXP_CTX_RE = re.compile(
    r"experien|\bexp\b|industry|professional|relevant|track record|"
    r"working|in a .{0,25}\brole|of work|background|hands-on", re.I)
_EXP_MIN_RE = re.compile(r"minimum|at\s+least|min\.?\b|no\s+less\s+than", re.I)


def _experience_floors(text):
    """Every experience-requirement floor (in years) stated in the text. A bare year
    count only counts when an experience-ish word sits right next to it (so '10-key'
    or '401k vesting after 3 years' don't masquerade as a requirement)."""
    out = []
    for m in _EXP_YEARS_RE.finditer(text or ""):
        before = text[max(0, m.start() - 20):m.start()]
        after = text[m.end():m.end() + 45]
        if _EXP_CTX_RE.search(after) or _EXP_CTX_RE.search(before) or _EXP_MIN_RE.search(before):
            n = int(m.group(1))
            if n <= 20:                  # >20 is noise ('30 years combined', a tenure stat)
                out.append(n)
    return out


def experience_years(text):
    """The HIGHEST experience requirement the text states, or None when it states none.

    STRICT on purpose, and this is the one the FEED reads. "8+ years of engineering
    experience; 2 years of SQL preferred" has floors [8, 2] — it is an 8-year job, and
    reading the floor instead let a senior req hide behind its most junior line item, so
    "Entry · <=2 yrs" returned eight-year roles. A JD that states no year count at all
    returns None and is always KEPT by the filter (many genuine entry-level posts state none).
    """
    fl = _experience_floors(text)
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
# Fetch a job description page (best-effort; paste fallback in the UI)
# ------------------------------------------------------------
def fetch_jd(url, limit=8000):
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        for tag in soup(["script", "style", "nav", "header", "footer", "form"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
        return re.sub(r"\s{2,}", " ", text)[:limit]
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
