"""
ats.py — lexical, deterministic ATS keyword coverage (NO AI).

Used for the always-available "keyword coverage before -> after" metric, and as the
key-free fallback when there's no Gemini key. It mirrors how a keyword-screening ATS
weights a JD: hard skills/tools/certs highest, requirement-section terms next.

Faithful copy of the relevant JobMatch core.py logic. `idf` is optional here (None ->
every term weighted 1.0 before the ATS/requirements multipliers), so this app needs no
idf.json corpus file.
"""
import re
from collections import Counter

STOPWORDS = set("""
a an and or but the of to in for on with at by from as is are was were be been being
this that these those it its their our your you we they he she them his her not no nor
will would can could should may might must do does did done have has had having about
into over under again further then once here there all any both each few more most other
some such only own same so than too very up down out off above below between during who
whom which what when where why how if because while per via etc within across upon also
us am i me my mine ours yours job role position company team work
""".split())

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
_SEGMENT_RE = re.compile(r"[.,;:/()\[\]{}\n\t•|]+")


def _tokens(text):
    return [w.lower() for w in WORD_RE.findall(text or "")]


def extract_keywords(text, top_n=28, max_bigrams=8, extra_skip=None):
    """Pull the most signal-bearing terms from a JD: meaningful single words plus two-word
    phrases (phrases formed only WITHIN a clause, so we never glue across sentences)."""
    skip = STOPWORDS | JD_BOILERPLATE | (set(extra_skip) if extra_skip else set())
    uni_counter, bi_counter = Counter(), Counter()
    for segment in _SEGMENT_RE.split(text or ""):
        raw = [t.strip("-.+#/") for t in _tokens(segment)]
        raw = [t for t in raw if t]
        uni_counter.update(t for t in raw if len(t) > 2 and t not in skip)
        for a, b in zip(raw, raw[1:]):
            if a in skip or b in skip or len(a) <= 2 or len(b) <= 2:
                continue
            bi_counter["%s %s" % (a, b)] += 1
    bigrams = [k for k, _ in sorted(bi_counter.items(),
                                    key=lambda kv: (kv[1], len(kv[0])), reverse=True)][:max_bigrams]
    bigram_words = {w for bg in bigrams for w in bg.split()}
    unigrams = [k for k, _ in sorted(uni_counter.items(),
                                     key=lambda kv: (kv[1], kv[0]), reverse=True)]
    keywords = list(bigrams)
    for u in unigrams:
        if u in bigram_words:
            continue
        keywords.append(u)
        if len(keywords) >= top_n:
            break
    return keywords[:top_n]


_REQ_HEADERS = ("minimum qualifications", "basic qualifications", "preferred qualifications",
                "qualifications", "requirements", "what you'll need", "what we're looking for",
                "who you are", "what you'll do", "responsibilities", "skills you")


def _requirements_text(jd_text):
    """The part of a JD from its first requirements/qualifications header onward."""
    low = (jd_text or "").lower()
    hits = [low.find(h) for h in _REQ_HEADERS if low.find(h) != -1]
    return jd_text[min(hits):] if hits else ""


# Hard skills, tools, methods, and certs an ATS literally scans for — weighted highest.
ATS_KEYWORDS = {
    "jira", "confluence", "asana", "trello", "smartsheet", "monday.com", "wrike", "clickup",
    "ms project", "microsoft project", "primavera", "sharepoint", "excel", "google sheets",
    "powerpoint", "visio", "lucidchart", "miro", "notion", "sql", "tableau", "power bi",
    "looker", "salesforce", "sap", "oracle", "netsuite", "workday", "servicenow", "python", "git",
    "agile", "scrum", "kanban", "safe", "lean", "six sigma", "lean six sigma", "waterfall",
    "sdlc", "devops", "kaizen", "pmbok", "prince2", "itil", "okr", "okrs", "kpi", "kpis",
    "gantt", "sprint", "backlog", "retrospective", "scrum master", "product owner",
    "pmp", "capm", "csm", "psm", "cspo", "cbap", "green belt", "black belt",
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
    """JD-invariant keyword weights. Returns {"terms", "weight", "total"}."""
    jd_low = (jd_text or "").lower()
    req_low = _requirements_text(jd_text).lower()
    jd_terms = set(extract_keywords(jd_text, top_n=30))
    jd_terms |= {kw for kw in ATS_KEYWORDS if kw in jd_low}
    if not jd_terms:
        return {"terms": [], "weight": {}, "total": 0.0}
    default_w = max(idf.values()) if idf else 1.0

    def wt(t):
        w = idf.get(t, default_w) if idf else 1.0
        if t in ATS_KEYWORDS:
            w *= 2.5
        if t in req_low:
            w *= 1.6
        return w

    terms = list(jd_terms)
    weight = {t: wt(t) for t in terms}
    total = sum(weight[t] for t in terms)
    return {"terms": terms, "weight": weight, "total": total}


def score_against(resume_low, analyzed):
    """Résumé-dependent half. `resume_low` must already be lowercased.
    Returns (score, keywords_have, keywords_to_add)."""
    terms = analyzed["terms"]
    if not terms:
        return 0, [], []
    weight = analyzed["weight"]
    have = sorted((t for t in terms if t in resume_low), key=lambda t: -weight[t])
    missing = sorted((t for t in terms if t not in resume_low), key=lambda t: -weight[t])
    score = round(100.0 * sum(weight[t] for t in have) / (analyzed["total"] or 1.0))
    return score, have, missing


def coverage(resume_text, jd_text, idf=None):
    """Convenience wrapper for the before/after metric.
    Returns {"score", "have", "missing"}."""
    score, have, missing = score_against((resume_text or "").lower(), analyze_jd(jd_text, idf))
    return {"score": int(score or 0), "have": have, "missing": missing}
