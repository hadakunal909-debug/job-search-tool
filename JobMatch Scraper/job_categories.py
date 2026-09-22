"""Posting domains, separate from role family and seniority.

Actual duties win; a specific title fills a gap; employer background is only a
fallback. Unknown and mixed evidence stay visible rather than being forced into IT.
This module has no network calls and does not load descriptions on the feed path.
"""
import html
import json
import os
import re
import unicodedata
from functools import lru_cache

CATEGORY_VERSION = VERSION = "2026-09-22.1"
CATEGORY_LABELS = {
    "construction": "Construction & Built Environment",
    "it": "IT & Software",
    "data": "Data & Analytics",
    "engineering": "Engineering & Manufacturing",
    "healthcare": "Healthcare & Life Sciences",
    "finance": "Finance & Accounting",
    "operations": "Operations & Supply Chain",
    "business": "Business & General Management",
    "marketing": "Marketing & Sales",
    "education": "Education & Research",
    "other": "Other / Unclear",
}
CATEGORY_FIELDS = ("category", "category_label", "category_source",
                   "category_confidence", "category_evidence", "category_version")


def normalize_category(value):
    return value if value in CATEGORY_LABELS else "any"


def _rx(text):
    return re.compile(r"\b(?:" + text + r")\b", re.I)


# Distinct phrases, not repeated keyword counts. Ordinary office software, agile
# meetings, budgets and stakeholders occur in every domain and are not IT evidence.
_DUTIES = {
    "construction": (
        r"construction (?:projects?|management|activities|sites?|documents?|schedules?|budgets?)",
        r"(?:commercial|residential|civil|electrical|industrial|building|highway|road|bridge) construction",
        r"(?:subcontractors?|general contractors?)", r"(?:job ?sites?|construction sites?)",
        r"(?:building permits?|building codes?|construction drawings?|blueprints?)",
        r"(?:preconstruction|pre-construction|earthwork|concrete|sitework|MEP|BIM)",
        r"(?:RFIs?|submittals?|punch[- ]lists?|change orders)", r"(?:capital projects?|capital improvements?|facilities construction)",
        r"(?:civil engineering|structural engineering|architectural design)",
    ),
    "it": (
        r"(?:software|application|web|mobile|platform) (?:development|engineering|delivery|implementation|deployment|installations?)",
        r"(?:IT|information technology|technology|enterprise systems?) (?:projects?|programs?|infrastructure|implementation|operations|delivery)",
        r"(?:software development life ?cycle|SDLC|software releases?|software products?|software testing)",
        r"(?:cloud (?:infrastructure|migration|platforms?|consumption)|network(?:ing)? (?:infrastructure|administration|security|projects?))",
        r"(?:cybersecurity|cyber security|information security|DevOps|CI/CD)",
        r"(?:ERP|SAP|Salesforce|ServiceNow|Workday) (?:implementation|migration|deployment|integration|projects?|systems?)",
        r"(?:APIs?|microservices|backend services|front[- ]?end development|full[- ]?stack)",
        r"(?:IT support|technical support|service desk|help desk|systems administration)",
        r"(?:write|develop|test|debug|deploy|maintain|implement(?:ing)?) (?:\w+ ){0,3}(?:code|software|applications)",
        r"go[- ]live (?:with|of) (?:\w+ ){0,2}(?:software|applications|systems)",
        r"(?:Linux kernel|device drivers?|operating systems?|embedded (?:Linux|software))",
        r"(?:programming|coding|debugging|source code|codebase|C/C\+\+|C\+\+ code|software solutions)",
        r"(?:HRIS|systems? configurations?|configuration support|user acceptance testing|UAT|technical requirements)",
    ),
    "data": (
        r"(?:data (?:science|engineering|analytics|pipelines?|warehouses?|modeling|modelling))",
        r"(?:machine learning|predictive model(?:s|ing)?|statistical (?:analysis|model(?:s|ing)?))",
        r"(?:business intelligence|ETL|data visualization|data visualisation)",
        r"(?:train|deploy|develop|build) (?:\w+ ){0,3}(?:ML models?|AI models?|language models?)",
    ),
    "engineering": (
        r"(?:manufacturing|production) (?:processes|lines?|operations|engineering|equipment|facilities)",
        r"(?:mechanical|electrical|hardware|aerospace|chemical|industrial) (?:engineering|design|systems?)",
        r"(?:product validation|process engineering|quality engineering|design verification)",
        r"(?:semiconductors?|embedded systems?|circuit design|printed circuit|robotics|firmware)",
        r"(?:power generation|power systems?|utility infrastructure|renewable energy)",
    ),
    "healthcare": (
        r"(?:clinical (?:trials?|research|operations|programs?|studies)|patient care|patient safety)",
        r"(?:healthcare delivery|medical devices?|drug development|drug discovery)",
        r"(?:regulatory submissions?|FDA submissions?|GCP|clinical protocols?)",
        r"(?:nursing|medical services|health services|pharmaceutical development)",
    ),
    "finance": (
        r"(?:financial (?:reporting|statements?|analysis|planning)|investment (?:analysis|portfolios?))",
        r"(?:accounting|accounts payable|accounts receivable|general ledger|tax compliance)",
        r"(?:credit risk|underwriting|banking operations|loan (?:operations|processing)|actuarial)",
        r"(?:audit procedures|GAAP|financial controls|FP&A)",
    ),
    "operations": (
        r"(?:supply chain|logistics|procurement|strategic sourcing)",
        r"(?:warehouse (?:operations|management)|inventory (?:planning|management|control))",
        r"(?:distribution centers?|freight|transportation operations|fulfillment operations)",
        r"(?:demand planning|supply planning|purchase orders|supplier performance)",
    ),
    "marketing": (
        r"(?:marketing campaigns?|digital marketing|brand strategy|brand management)",
        r"(?:sales (?:pipeline|targets?|operations|enablement)|lead generation|customer acquisition)",
        r"(?:advertising|media buying|search engine optimization|content strategy)",
    ),
    "education": (
        r"(?:curriculum (?:development|design)|student (?:services|affairs|advising|success))",
        r"(?:academic programs?|instructional design|classroom instruction|teaching)",
        r"(?:research grants?|research administration|faculty support)",
    ),
    "business": (
        r"(?:business operations|business transformation|organizational change|management consulting)",
        r"(?:corporate strategy|business strategy|process improvement|operational excellence)",
        r"(?:human resources|talent acquisition|employee relations)",
    ),
}
_DUTY_RX = {k: tuple(_rx(v) for v in values) for k, values in _DUTIES.items()}
# A short posting may contain just one unambiguous description of the work.
# Require an action attached to a specific domain, not incidental tool vocabulary.
_DIRECT_DOMAIN = {
    "construction": r"(?:construction projects?|(?:commercial|building|residential|civil) construction)",
    "it": r"(?:software (?:implementation|development|deployment|installations?)|cloud migration|IT infrastructure|network administration)",
    "data": r"(?:data (?:pipelines?|warehouses?|analytics)|machine learning models?|statistical models?)",
    "engineering": r"(?:mechanical (?:systems|design)|manufacturing processes|electrical systems|circuit design)",
    "healthcare": r"(?:clinical (?:trials?|research)|patient care|drug development)",
    "finance": r"(?:financial statements|general ledger|tax returns|accounting operations)",
    "operations": r"(?:supply chain|warehouse operations|inventory management|logistics operations)",
    "marketing": r"(?:marketing campaigns?|digital marketing|brand strategy|advertising campaigns?)",
    "education": r"(?:curriculum development|academic programs?|student advising|classroom instruction)",
    "business": r"(?:corporate strategy|human resources|organizational change|business transformation)",
}
_DIRECT_RX = {key: _rx(r"(?:lead|manage|oversee|develop|design|build|implement|coordinate|deliver|own|perform|provide|prepare)(?:s|ing)? (?:[\w-]+ ){0,4}" + pattern)
              for key, pattern in _DIRECT_DOMAIN.items()}
_TITLE_RX = {
    "construction": _rx(r"construction|preconstruction|civil|structural|architectural|estimator|superintendent|MEP|BIM|capital projects?|facilities (?:project|program)|real estate development"),
    "it": _rx(r"IT|information technology|software|(?:technical|tech) (?:project|program|product|proj|prg)|technology|cloud|cyber ?security|DevOps|scrum master|ERP|SAP|Salesforce|ServiceNow|network(?:ing)?|systems? administrator|web developer|full[- ]?stack|front[- ]?end|back[- ]?end"),
    "data": _rx(r"data (?!cent(?:er|re))|data$|analytics|business intelligence|machine learning|artificial intelligence|AI|ML"),
    "engineering": _rx(r"manufacturing|mechanical|electrical|hardware|aerospace|industrial|firmware|embedded|semiconductor|engineering|engineer|energy|utilities"),
    "healthcare": _rx(r"clinical|healthcare|medical|patient|pharmaceutical|biotech|nursing"),
    "finance": _rx(r"financial|finance|accounting|accountant|tax|credit|underwriting|investment|banking|actuarial|audit"),
    "operations": _rx(r"supply chain|logistics|procurement|warehouse|inventory|sourcing|transportation|fulfillment"),
    "marketing": _rx(r"marketing|sales|advertising|brand|content strategist"),
    "education": _rx(r"academic|curriculum|student|education|instructional|teacher|faculty|research administration"),
}
# The named occupation is stronger than a client sector or product in the title:
# "Software Engineer - Medical" and "Data Analyst, Finance" describe software and
# analytics work. This is only a tie-breaker when actual duties are inconclusive.
_ROLE_TITLE_RX = {
    "construction": _rx(r"(?:construction|facilities|capital projects?) (?:project |program )?(?:manager|coordinator|management)|(?:civil|structural) engineer|construction estimator|superintendent"),
    "it": _rx(r"software (?:engineer(?:ing)?|developer|architect)|(?:IT|information technology|technical|technology|cloud|network(?:ing)?) (?:project |program |product )?(?:manager|coordinator|engineer|architect)|scrum master|systems? administrator"),
    "data": _rx(r"(?:data|analytics|business intelligence|machine learning|AI|ML) (?:scientist|engineer(?:ing)?|analyst|architect|manager)"),
    "engineering": _rx(r"(?:mechanical|electrical|hardware|manufacturing|aerospace|industrial) engineer(?:ing)?"),
    "healthcare": _rx(r"clinical (?:project |program )?(?:manager|coordinator)|(?:nurse|nursing|physician|therapist)"),
    "finance": _rx(r"(?:finance|financial|accounting|investment|credit) (?:analyst|manager|director)|accountant|actuary"),
    "operations": _rx(r"(?:supply chain|logistics|procurement|warehouse|inventory) (?:analyst|manager|coordinator|director)"),
    "marketing": _rx(r"(?:marketing|sales|brand) (?:analyst|manager|coordinator|director)"),
    "education": _rx(r"(?:academic|education|curriculum|instructional) (?:program )?(?:manager|coordinator|designer)|teacher"),
}
_DUTY_HEADING = re.compile(r"^(?:key |primary |essential |core )?(?:responsibilities|duties|essential functions|what you(?:'ll| will) (?:do|work on)|your (?:role|impact|responsibilities)|the (?:role|opportunity)|position summary|job (?:summary|description)|about (?:the|this) (?:role|job))\b", re.I)
_SKIP_HEADING = re.compile(r"^(?:about (?!the (?:role|job)|this (?:role|job))|who we are|company (?:overview|profile)|our (?:company|mission)|benefits|perks|compensation|equal (?:opportunity|employment)|what we offer)\b", re.I)
_OTHER_HEADING = re.compile(r"^(?:(?:(?:required|preferred|minimum|basic|desired) )?(?:qualifications|requirements|education|experience)|what you(?:'ll| will) (?:bring|need)|(?:required|essential|additional) (?:technical )?skills|skills (?:and|&) qualifications|the qualifications)\b", re.I)
_VERB = _rx(r"manag(?:e[sd]?|ing)|lead(?:s|ing)?|oversee(?:s|ing)?|coordinat(?:e[sd]?|ing)|deliver(?:s|ing)?|develop(?:s|ing)?|build(?:s|ing)?|implement(?:s|ing)?|design(?:s|ing)?|maintain(?:s|ing)?|execut(?:e[sd]?|ing)|support(?:s|ing)?|ensur(?:e[sd]?|ing)|driv(?:es?|ing)|gather(?:s|ing)?|defin(?:e[sd]?|ing)|perform(?:s|ing)?|deploy(?:s|ing)?|configur(?:e[sd]?|ing)|administer(?:s|ing)?|responsible")
_COMPANY_SENTENCE = re.compile(r"\b(?:we are|our company|is a (?:global |leading |world)|equal opportunity|regardless of|all qualified applicants|we offer|our benefits)\b", re.I)
_INLINE_HEADING = re.compile(r"(?<!\w)(?=(?:(?:key |primary |essential |core )?responsibilities:?|(?:required|preferred|minimum|basic) qualifications:?|(?:essential|additional|required) skills|job description:?|what you(?:'ll| will) (?:do|work on):|benefits:))", re.I)


def _plain(text):
    value = html.unescape(str(text or ""))
    if re.search(r"</?(?:p|div|li|br|h[1-6]|ul|section)\b", value, re.I):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(value, "html.parser")
        for tag in soup(["script", "style", "nav", "footer"]):
            tag.decompose()
        value = soup.get_text("\n", strip=True)
    return unicodedata.normalize("NFKC", value).replace("\r", "\n")


def _duty_text(text):
    """Keep role statements; do not categorize by benefits or employer boilerplate."""
    sections, loose, mode = [], [], "loose"
    text = _INLINE_HEADING.sub("\n", _plain(text))
    for line in re.split(r"\n+|(?<=[.!?])\s+(?=[A-Z])", text):
        line = line.strip(" \t#*:•-–")
        if not line:
            continue
        if _SKIP_HEADING.match(line):
            mode = "skip"
            continue
        if _DUTY_HEADING.match(line):
            mode = "duties"
        elif _OTHER_HEADING.match(line):
            mode = "qualifications"
        if _COMPANY_SENTENCE.search(line):
            continue
        if mode == "duties":
            sections.append(line)
        elif mode == "loose" and _VERB.search(line):
            loose.append(line)
    return "\n".join(sections or loose)


def _company_key(name):
    key = re.sub(r"[^a-z0-9]+", " ", str(name or "").lower()).strip()
    return re.sub(r"\s+(?:inc|llc|ltd|corp|corporation|incorporated)$", "", key)


@lru_cache(maxsize=4)
def _company_index(stamp):
    try:
        with open(os.path.join(os.path.dirname(__file__), "companies.json"), encoding="utf-8") as handle:
            blob = json.load(handle)
        sectors = blob.get("sectors", [])
        return {_company_key(r[0]): sectors[r[1]] for r in blob.get("rows", [])
                if isinstance(r, list) and isinstance(r[1], int) and 0 <= r[1] < len(sectors)}
    except (OSError, ValueError, TypeError, IndexError):
        return {}


def company_type_for(company):
    path = os.path.join(os.path.dirname(__file__), "companies.json")
    try:
        stat = os.stat(path)
        return _company_index((stat.st_mtime_ns, stat.st_size)).get(_company_key(company), "")
    except OSError:
        return ""


def _company_category(company_type, company):
    background = str(company_type or "")
    # Explicit names are usable background; brand names and staffing firms are not.
    if not background or background.lower() in ("unsorted", "other", "unknown"):
        background = str(company or "")
    for key, pattern in (
        ("construction", r"construction|real estate|architecture|civil engineering|built environment"),
        ("it", r"software|technology|IT services|internet|cloud|cybersecurity"),
        ("healthcare", r"health|medical|pharma|biotech|life sciences"),
        ("finance", r"bank|finance|financial|insurance|investment|accounting"),
        ("education", r"education|universit|college|school|academic"),
        ("engineering", r"manufactur|industrial|energy|utilities|aerospace|semiconductor"),
        ("operations", r"logistics|transportation|supply chain"),
        ("marketing", r"advertising|marketing"),
    ):
        if re.search(pattern, background, re.I):
            return key, background
    return "", ""


def _result(category, source, confidence, evidence):
    return dict(category=category, category_label=CATEGORY_LABELS[category],
                category_source=source, category_confidence=confidence,
                category_evidence=str(evidence)[:400], category_version=CATEGORY_VERSION)


def classify_job(title, jd="", company="", company_type=""):
    title = _plain(title)
    # Defer the import: core uses the category helpers while this decision must
    # share its posting-validity gate. A careers shell is not evidence of duties.
    from core import clean_jd
    cleaned, verdict = clean_jd(_plain(jd))
    duties = "" if verdict == "not-a-posting" else _duty_text(cleaned)
    hits = {key: [m.group(0) for rx in patterns if (m := rx.search(duties))]
            for key, patterns in _DUTY_RX.items()}
    ranked = sorted(hits, key=lambda k: len(hits[k]), reverse=True)
    first, second = ranked[:2]
    title_keys = [k for k, rx in _TITLE_RX.items() if rx.search(title)]
    # Data/software/civil engineers are specific disciplines, not generic engineering.
    if len(title_keys) > 1 and "engineering" in title_keys:
        title_keys.remove("engineering")
    if len(title_keys) > 1:
        occupations = [(m.start(), key) for key in title_keys
                       if (m := _ROLE_TITLE_RX.get(key).search(title))]
        if occupations:
            title_keys = [min(occupations)[1]]
    n, runner = len(hits[first]), len(hits[second])
    if n >= 2 and n > runner:
        return _result(first, "jd", "high" if n >= 3 and n - runner >= 2 else "medium",
                       "; ".join(hits[first]))
    if n == 1 and runner == 0 and _DIRECT_RX[first].search(duties):
        return _result(first, "jd", "medium", "; ".join(hits[first]))
    # A single explicit duty corroborated by the title is useful evidence. Competing
    # domains need a margin; mentioning one tool never overturns a specific title.
    if n and len(title_keys) == 1 and len(hits[title_keys[0]]) == n:
        key = title_keys[0]
        return _result(key, "jd", "medium", "; ".join(hits[key]))
    if len(title_keys) == 1:
        return _result(title_keys[0], "title", "medium", title)
    if n >= 2:
        return _result("other", "unknown", "low", "Mixed duties: " + "; ".join(
            CATEGORY_LABELS[k] for k in ranked if len(hits[k]) == n))
    ckey, background = _company_category(company_type or company_type_for(company), company)
    if ckey and (not title_keys or ckey in title_keys):
        return _result(ckey, "company", "low", background)
    return _result("other", "unknown", "low", "Insufficient role-specific evidence")


def category_for_job(job):
    """Use stored JD-based classification on light feed rows without fetching text."""
    if (job.get("category_version") == CATEGORY_VERSION
            and job.get("category") in CATEGORY_LABELS and not job.get("jd")):
        result = {key: job.get(key, "") for key in CATEGORY_FIELDS}
        result["category_label"] = CATEGORY_LABELS[result["category"]]
        return result
    return classify_job(job.get("title", ""), job.get("jd", ""),
                        job.get("company", ""), job.get("company_type", ""))


def category_tip(category):
    sources = {"jd": "From job duties", "title": "From title; duties not confirmed",
               "company": "Inferred from company background", "unknown": "Needs review"}
    return sources.get(category.get("category_source"), "Needs review") + (
        ": " + category["category_evidence"] if category.get("category_evidence") else "")
