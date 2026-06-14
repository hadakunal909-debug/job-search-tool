"""
core.py — all the logic for the job-match app, kept free of Streamlit so it can
be tested on its own. app.py imports from here and only handles the UI.
"""

import csv
import os
import re
import json
import math
from io import BytesIO
from collections import Counter

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


def analyze_jd(jd_text, idf=None):
    """The résumé-INDEPENDENT half of the ATS match: the JD's important keywords and each
    one's weight. Depends only on the JD text, idf, the ATS keyword set, and the JD's
    requirements section — NOT the résumé — so it can be computed once per job and reused
    for every résumé and every page render.

    Returns {"terms": [term, ...], "weight": {term: w}, "total": float}.
    """
    jd_low = (jd_text or "").lower()
    req_low = _requirements_text(jd_text).lower()

    # The JD's important keywords: its salient terms + any hard ATS keywords it names.
    jd_terms = set(extract_keywords(jd_text, top_n=30))
    jd_terms |= {kw for kw in ATS_KEYWORDS if kw in jd_low}
    if not jd_terms:
        return {"terms": [], "weight": {}, "total": 0.0}

    default_w = max(idf.values()) if idf else 1.0

    def wt(t):
        w = idf.get(t, default_w) if idf else 1.0
        if t in ATS_KEYWORDS:        # hard skill / tool / cert — what an ATS weights most
            w *= 2.5
        if t in req_low:             # stated in the requirements/qualifications section
            w *= 1.6
        return w

    terms = list(jd_terms)           # freeze the set's iteration order ONCE (see score_against)
    weight = {t: wt(t) for t in terms}
    total = sum(weight[t] for t in terms)
    return {"terms": terms, "weight": weight, "total": total}


def score_against(resume_low, analyzed):
    """The résumé-DEPENDENT half: which of the JD's weighted keywords appear in the résumé.
    `resume_low` must already be lowercased. Returns (score, keywords_have, keywords_to_add).

    Iterates the SAME `terms` list analyze_jd froze, so equal-weight sort ties keep their
    order and the result is byte-identical to the original single-function skill_match."""
    terms = analyzed["terms"]
    if not terms:
        return 0, [], []
    weight = analyzed["weight"]
    have = sorted((t for t in terms if t in resume_low), key=lambda t: -weight[t])
    missing = sorted((t for t in terms if t not in resume_low), key=lambda t: -weight[t])
    score = round(100.0 * sum(weight[t] for t in have) / (analyzed["total"] or 1.0))
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
    return {"analyzed": analyze_jd(jd_text, idf),
            "exp_years": experience_min_years(jd_text),
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
def load_sponsor_counts(path="sponsor_counts.json"):
    """Optional {normalized_company: H1B_filing_count} built from DOL LCA data.
    Returns {} when the file is absent (strength just isn't shown)."""
    if not os.path.exists(path):
        return {}
    try:
        return json.load(open(path, encoding="utf-8")) or {}
    except Exception:
        return {}


def sponsor_strength(company, counts):
    """Tier a sponsor by filing VOLUME. Returns ('high'|'medium'|'low'|'', count).
    ('', 0) when there's no number for the company. Counts come from load_sponsor_counts()."""
    if not counts or not company:
        return "", 0
    try:
        import scraper                      # lazy: scraper imports core (avoid circular at load)
        key = scraper._norm_name(company)
    except Exception:
        key = re.sub(r"[^a-z0-9 ]+", " ", company.lower()).strip()
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


def required_years(text):
    """The HIGHEST experience requirement mentioned (0 if none). Used by the scraper to
    hard-drop roles demanding more than MAX_YEARS where it has the JD (e.g. Amazon)."""
    fl = _experience_floors(text)
    return max(fl) if fl else 0


def experience_min_years(text):
    """The LOWEST experience requirement stated — i.e. the years you need to QUALIFY
    ('3-5 years' -> 3, '5+ years' -> 5). None when the JD never states one (many genuine
    entry-level posts don't). Powers the feed's experience filter / badge, so it leans
    lenient: it answers 'what's the floor to be considered', not 'the most they'd want'."""
    fl = _experience_floors(text)
    return min(fl) if fl else None


def experience_level(text):
    """Coarse bucket for the feed filter: 'entry' (<=2 yrs or unstated-but-short),
    'mid' (3-5), 'senior' (6+), or '' when the JD never states years."""
    y = experience_min_years(text)
    if y is None:
        return ""
    if y <= 2:
        return "entry"
    if y <= 5:
        return "mid"
    return "senior"


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


def _tailor_prompt(resume_text, jd_text):
    return (
        "You are an expert resume writer and career coach. Tailor the candidate's resume "
        "to ONE specific job and produce the strongest possible version of THIS "
        "candidate's resume for THIS job.\n\n"
        "Do this:\n"
        "1. Lead with and emphasize the experience, projects, and skills most relevant to "
        "the job's requirements.\n"
        "2. Mirror the job description's exact terminology and keywords (titles, tools, "
        "methodologies, skills) wherever the candidate genuinely has that experience — this "
        "helps pass ATS keyword screening.\n"
        "3. Rewrite bullet points to start with strong action verbs; keep and surface any "
        "quantified results already in the resume.\n"
        "4. Fold the job's must-have skills that the candidate actually has into the Skills "
        "section.\n"
        "5. Keep every real section (contact, summary, experience, education, skills) and all "
        "true content; keep it concise and ATS-friendly (plain text, standard headers).\n\n"
        "Hard rules (must follow):\n"
        "- NEVER invent or exaggerate experience, employers, titles, dates, degrees, metrics, "
        "or skills the candidate does not have. Truthful reorder/reword only.\n"
        "- Do not list skills the resume doesn't support.\n"
        "- Output ONLY the finished resume text — no preamble, notes, or explanation.\n\n"
        "=== TARGET JOB DESCRIPTION ===\n%s\n\n"
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


def tailor_with_gemini(resume_text, jd_text, api_key, model=None):
    """Rewrite the résumé for a JD with Google's Gemini API (REST). Truthful reorder/reword
    only. Uses Gemini Flash 3.5 with dynamic 'thinking' ON for a stronger result (slower +
    more tokens — by design). `api_key` = a Google AI Studio key (starts 'AIza')."""
    if not api_key:
        raise RuntimeError("No Gemini API key provided.")
    prompt = _tailor_prompt(resume_text, jd_text)
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


def tailor_with_ai(resume_text, jd_text, model=AI_MODEL, api_key=None):
    """Anthropic/Claude variant — used only if you switch to an Anthropic key."""
    from anthropic import Anthropic
    client = Anthropic(api_key=api_key) if api_key else Anthropic()  # else reads the env var
    msg = client.messages.create(
        model=model,
        max_tokens=4096,                       # headroom for a full résumé rewrite
        messages=[{"role": "user", "content": _tailor_prompt(resume_text, jd_text)}],
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
