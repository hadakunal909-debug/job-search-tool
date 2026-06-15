"""
research.py — the self-directed company researcher. No AI.

Given a company (name or URL) it resolves the domain, then crawls a bounded set of the
company's OWN pages (home + discovered about/values/culture/careers/mission/team/news),
SSRF-safe and robots-aware, and extracts — by code, not AI — what matters for tailoring:
its stated values, culture/"what they're looking for" lines, an about-summary, and its
salient keywords. The result is stored and ACCUMULATES per company over time.

All outbound fetches of user-controlled URLs go through safefetch._safe_get.
"""
import re
from urllib.parse import urlparse, urljoin
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup

import core

from . import safefetch

_DOMAIN_MAP = {
    "amazon": "amazon.com", "google": "google.com", "microsoft": "microsoft.com",
    "stripe": "stripe.com", "salesforce": "salesforce.com", "nvidia": "nvidia.com",
    "datadog": "datadoghq.com", "mongodb": "mongodb.com", "snowflake": "snowflake.com",
    "servicenow": "servicenow.com", "workday": "workday.com", "samsara": "samsara.com",
    "palantir": "palantir.com", "ramp": "ramp.com", "brex": "brex.com", "uber": "uber.com",
    "lyft": "lyft.com", "airbnb": "airbnb.com", "pinterest": "pinterest.com",
    "robinhood": "robinhood.com", "instacart": "instacart.com", "visa": "visa.com",
    "northeastern university": "northeastern.edu", "harvard university": "harvard.edu",
    "jpmorgan": "jpmorganchase.com", "capital one": "capitalone.com", "boeing": "boeing.com",
    "johnson & johnson": "jnj.com", "pfizer": "pfizer.com", "intel": "intel.com",
}

_PAGE_HINTS = ("about-us", "about", "company", "mission", "values", "who-we-are", "our-story",
               "culture", "careers", "life-at", "life", "team", "people", "news", "newsroom",
               "press", "blog", "products", "solutions", "what-we-do", "our-work", "platform")
# Tried directly even when the homepage is a JS/consent shell with no usable links — many
# corporate sites gate bots behind a cookie interstitial, so link discovery finds nothing.
# Ordered high-value first (about/values/what-we-do) since the page budget is capped.
_COMMON_PATHS = ("/about", "/about-us", "/company", "/who-we-are", "/our-story", "/mission",
                 "/values", "/culture", "/what-we-do", "/products", "/solutions", "/our-work",
                 "/news", "/newsroom", "/blog", "/press", "/careers", "/en-us/company.html")
_MAX_PAGES = 10
_PAGE_CHARS = 6000
_HINT_RE = re.compile("|".join(re.escape(h) for h in _PAGE_HINTS), re.I)
# Cookie/consent/JS-shell boilerplate — a page that's mostly this carries no real signal.
_LOWVALUE_RE = re.compile(
    r"requires? cookies|cookie preferences|cookie settings|go to cookie|update your preferences|"
    r"accept all cookies|cookie policy|manage cookies|enable javascript|please enable|"
    r"your privacy choices|privacy policy|all rights reserved|terms of (?:use|service)|"
    r"skip to (?:main|content)|select your (?:language|region|country)|please update|\{\d\}", re.I)


def _is_lowvalue(text):
    t = text or ""
    if len(t) < 600 and _LOWVALUE_RE.search(t):
        return True
    hits = len(_LOWVALUE_RE.findall(t))
    return hits >= 2 and len(t) < 1200          # consent text dominates a thin page

# Stated-value vocabulary — phrases companies use to describe what they stand for.
_VALUE_LEXICON = (
    "integrity", "ownership", "accountability", "transparency", "innovation", "excellence",
    "collaboration", "teamwork", "inclusion", "diversity", "belonging", "customer obsession",
    "customer focus", "customer first", "impact", "mission", "growth mindset", "curiosity",
    "trust", "respect", "empathy", "humility", "bias for action", "move fast", "data-driven",
    "quality", "craftsmanship", "sustainability", "boldness", "courage", "passion",
    "results", "grit", "long-term", "candor", "frugality", "deliver", "one team",
)
_SEEK_CUE = re.compile(r"\b(we(?:'re| are) looking for|seeking|you (?:are|have|will|'ll)|"
                       r"thrive|ideal|join us|be part of|our team|who we are|what we value|"
                       r"values|culture|believe)", re.I)
_SENT = re.compile(r"(?<=[.!?])\s+|\n+")

# The company's mission/purpose statement.
_MISSION_RE = re.compile(r"\b(our mission|mission is|our purpose|purpose is|we believe|"
                         r"we exist to|our vision|vision is|we strive to)\b", re.I)
# What the company actually does (its product/business in a sentence).
_WHATWEDO_RE = re.compile(r"\b(we (?:are|build|help|provide|offer|make|deliver|design|create|power|enable)|"
                          r"is (?:a|an|the)\b|provides|builds|helps|delivers|leading|world'?s|"
                          r"platform (?:for|that)|solutions? (?:for|that)|software (?:for|that))\b", re.I)
# "Current projects / ideas" — recent initiatives, launches, partnerships, investments.
_INITIATIVE_RE = re.compile(r"\b(launch(?:ed|ing|es)?|introduc(?:ed|ing|es)?|announc(?:ed|ing|es)?|"
                            r"unveil(?:ed|ing|s)?|partner(?:ship|ed|ing)?|acqui(?:re|red|sition)|"
                            r"expand(?:ed|ing|s)?|invest(?:ed|ing|ment)?|releas(?:ed|ing|es)?|"
                            r"next-gen|roadmap|202[4-9])\b", re.I)
# Navigation / chrome lines that masquerade as headings — never real signal.
_NAV_JUNK_RE = re.compile(r"^(home|about|menu|search|contact|careers?|login|log in|sign in|"
                          r"subscribe|newsletter|cookie|privacy|legal|terms|follow us|share|"
                          r"read more|learn more|view all|see all|next|previous|back to top)\b", re.I)
# Call-to-action prefixes — a "sentence" that opens with one is flattened nav/link text.
_CTA_RE = re.compile(r"^(read|explore|learn|listen|watch|view|see|discover|follow|browse|"
                     r"download|subscribe|sign up|get started|contact|find out|click)\b", re.I)


def _headings(html_text):
    """Pull h1-h3 text from a page (before it's flattened) — used to spot current initiatives."""
    try:
        soup = BeautifulSoup(html_text or "", "lxml")
        out = []
        for h in soup.find_all(["h1", "h2", "h3"]):
            t = re.sub(r"\s{2,}", " ", h.get_text(separator=" ", strip=True))
            if 18 < len(t) < 140 and not _NAV_JUNK_RE.match(t):
                out.append(t)
        return out
    except Exception:
        return []


def _list_items(html_text):
    """Pull short <li>/heading phrases — companies list their values/principles/perks this way."""
    try:
        soup = BeautifulSoup(html_text or "", "lxml")
        out = []
        for tag in soup.find_all(["li", "h3", "h4"]):
            t = re.sub(r"\s{2,}", " ", tag.get_text(separator=" ", strip=True))
            if 6 < len(t) < 90 and 1 <= len(t.split()) <= 12 \
                    and not _NAV_JUNK_RE.match(t) and not _CTA_RE.match(t) and any(c.isalpha() for c in t):
                out.append(t)
        return out
    except Exception:
        return []


# Pages that tend to hold the company's NAMED values / operating principles.
_VALUE_PAGE_RE = re.compile(r"value|culture|principle|belief|mission|who-we-are|/about", re.I)
# Pages where employee perks/benefits actually live (so product copy saying "flexible" is ignored).
_CAREERS_PAGE_RE = re.compile(r"career|jobs|life-at|/life|benefit|perks|working|join-us|join", re.I)
# Employee perks / benefits — strong HR terms only (avoid generic words that appear in product copy).
_BENEFIT_RE = re.compile(
    r"\b(health insurance|medical|dental|vision|401\(?k\)?|retirement|paid time off|\bpto\b|"
    r"parental leave|maternity|paternity|stock options?|equity|wellness|"
    r"learning (?:budget|stipend)|tuition|sabbatical|flexible (?:work|hours|schedule)|"
    r"work from home|remote work)\b", re.I)
# Technologies — for matching + 'mirror these tools'. Unambiguous tokens only (dropped go/excel/
# spark/swift/ruby/vue/git etc. that collide with ordinary English words).
_TECH_LEXICON = (
    "python", "java", "javascript", "typescript", "react", "node.js", "angular",
    "aws", "azure", "google cloud", "gcp", "kubernetes", "docker", "terraform",
    "postgresql", "mysql", "mongodb", "snowflake", "databricks", "kafka", "hadoop",
    "salesforce", "sap", "oracle", "workday", "servicenow", "jira", "confluence", "tableau",
    "power bi", "looker", "figma", "github", "graphql", "kotlin", "machine learning",
    "tensorflow", "pytorch", "agile", "scrum", "ms project", "primavera", "visio")


def _norm_name(name):
    return re.sub(r"\b(inc|llc|ltd|corp|co|company|the)\b", " ", (name or "").lower()).strip()


def resolve_domain(company_name, company_url=""):
    if company_url:
        host = (urlparse(company_url if "//" in company_url else "//" + company_url).hostname or "")
        host = host.lower().lstrip(".")
        if host.startswith("www."):
            host = host[4:]
        if host:
            return host
    key = _norm_name(company_name)
    if not key:
        return ""
    if key in _DOMAIN_MAP:
        return _DOMAIN_MAP[key]
    base = re.sub(r"[^a-z0-9]", "", key)
    return (base + ".com") if base else ""


def _robots_allowed(base, path="/"):
    try:
        r = safefetch._safe_get(urljoin(base, "/robots.txt"), timeout=8)
        if r.status_code >= 400:
            return True
        rp = RobotFileParser()
        rp.parse(r.text.splitlines())
        return rp.can_fetch("*", urljoin(base, path))
    except Exception:
        return True


def clean_html(html_text, limit=_PAGE_CHARS):
    try:
        soup = BeautifulSoup(html_text or "", "lxml")
        for tag in soup(["script", "style", "nav", "header", "footer", "form", "noscript", "svg"]):
            tag.decompose()
        title = (soup.title.get_text(strip=True) if soup.title else "")
        text = soup.get_text(separator=" ", strip=True)
        return title, re.sub(r"\s{2,}", " ", text)[:limit]
    except Exception:
        return "", ""


def _pick_pages(home_html, base):
    picked, seen = [], set()
    try:
        soup = BeautifulSoup(home_html or "", "lxml")
    except Exception:
        return picked
    home_host = (urlparse(base).hostname or "").lower()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#") or href.lower().startswith(("mailto:", "tel:", "javascript:")):
            continue
        full = urljoin(base, href)
        if (urlparse(full).hostname or "").lower() != home_host:
            continue
        if not _HINT_RE.search(urlparse(full).path):
            continue
        key = full.split("#")[0].rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        picked.append(full)
        if len(picked) >= _MAX_PAGES - 1:
            break
    return picked


def crawl_company(domain):
    """Return [{'url','title','text'}] for a bounded set of the company's own pages, or [].
    Skips cookie-consent / JS-shell pages, and tries common about/careers paths directly so a
    consent-gated homepage (which yields no links) still produces real content."""
    if not domain:
        return []
    base = "https://" + domain
    if not _robots_allowed(base, "/"):
        return []
    pages = []
    home_html = ""
    try:
        home = safefetch._safe_get(base, timeout=12)
        if home.status_code < 400:
            home_html = home.text
            title, text = clean_html(home_html)
            if text and not _is_lowvalue(text):
                pages.append({"url": base, "title": title, "text": text,
                              "headings": _headings(home_html), "items": _list_items(home_html)})
    except Exception:
        pass

    # discovered hint links first, then common-path guesses; dedupe, keep same-host
    candidates, seen = [], {base.rstrip("/")}
    for u in _pick_pages(home_html, base) + [urljoin(base, p) for p in _COMMON_PATHS]:
        key = u.split("#")[0].rstrip("/")
        if key not in seen:
            seen.add(key)
            candidates.append(u)

    for u in candidates:
        if len(pages) >= _MAX_PAGES:
            break
        if not _robots_allowed(base, urlparse(u).path):
            continue
        try:
            r = safefetch._safe_get(u, timeout=12)
            if r.status_code >= 400:
                continue
            title, text = clean_html(r.text)
            if text and not _is_lowvalue(text):
                pages.append({"url": u, "title": title, "text": text,
                              "headings": _headings(r.text), "items": _list_items(r.text)})
        except Exception:
            continue
    return pages


def _sentences(text):
    return [s.strip() for s in _SENT.split(text or "")
            if 25 < len(s.strip()) < 280 and not _LOWVALUE_RE.search(s)]


def _dedupe(seq, n, keylen=80):
    seen, out = set(), []
    for s in seq:
        k = s.lower()[:keylen]
        if k not in seen:
            seen.add(k)
            out.append(s)
        if len(out) >= n:
            break
    return out


def _substantive(sentences):
    """Sentences that read like real prose, not stitched-together nav links."""
    return [s for s in sentences if len(s.split()) >= 8]


def extract_company_knowledge(pages, company_name=""):
    """Deterministically distill crawled pages into a company knowledge record:
    what they do, mission, stated values, current initiatives, what they look for, keywords."""
    blob = " ".join(p.get("text", "") for p in pages)
    low = blob.lower()
    name = company_name or (pages[0].get("title", "").split("|")[0].strip() if pages else "")

    # per-page substantive sentences + which pages are news/blog (for initiatives)
    sents_by_page, all_sents, news_headings = [], [], []
    for p in pages:
        sents = _substantive(_sentences(p.get("text", "")))
        sents_by_page.append((p, sents))
        all_sents.extend(sents)
        tag = (p.get("url", "") + " " + p.get("title", "")).lower()
        if any(k in tag for k in ("news", "press", "blog", "newsroom", "stories", "media")):
            news_headings.extend(h for h in (p.get("headings", []) or [])
                                 if len(h.split()) >= 5)        # drop short nav-y headings

    # CORE VALUES — prefer the company's OWN named values (list items / short headings on a
    # values/culture/principles page); fall back to the generic value lexicon if none found.
    named_values = []
    for p in pages:
        if _VALUE_PAGE_RE.search((p.get("url", "") + " " + p.get("title", "")).lower()):
            named_values.extend(p.get("items", []) or [])
            named_values.extend(h for h in (p.get("headings", []) or []) if len(h.split()) <= 6)
    lexicon_hits = sorted({v.title() for v in _VALUE_LEXICON if v in low})
    named_values = _dedupe(named_values, 12, keylen=60)
    values = named_values if len(named_values) >= 3 else lexicon_hits

    looking_for = _dedupe((s for s in all_sents if _SEEK_CUE.search(s)), 10)
    mission = ""
    for s in all_sents:
        m = _MISSION_RE.search(s)
        if m:
            mission = s[m.start():].strip()        # drop nav text glued before the cue
            mission = mission[0].upper() + mission[1:] if mission else mission
            break

    # what they do: prefer about/home/product pages; first sentence that describes the business
    def _page_rank(p):
        t = (p.get("url", "") + p.get("title", "")).lower()
        for i, k in enumerate(("about", "what-we-do", "product", "solution", "company", "")):
            if k in t or k == "":
                return i
        return 9
    what_they_do = ""
    for p, sents in sorted(sents_by_page, key=lambda ps: _page_rank(ps[0])):
        hit = next((s for s in sents if _WHATWEDO_RE.search(s)), "")
        if hit:
            what_they_do = hit
            break

    # current initiatives/ideas: news headlines first, then announcement-flavored sentences
    # (skip flattened call-to-action/nav text that only looks like a sentence)
    initiatives = _dedupe(
        [h for h in news_headings if not _CTA_RE.match(h)]
        + [s for s in all_sents if _INITIATIVE_RE.search(s) and not _CTA_RE.match(s)], 8)

    about = " ".join(_dedupe(_substantive(
        _sentences(next((p.get("text", "") for p in pages
                         if "about" in (p.get("url", "") + p.get("title", "")).lower()),
                        (pages[0].get("text", "") if pages else "")))), 3))

    # PERKS / BENEFITS — only from careers/life/benefits pages (product copy elsewhere also says
    # "flexible"/"remote"), and only with strong HR signals.
    perks = []
    for p in pages:
        if not _CAREERS_PAGE_RE.search((p.get("url", "") + " " + p.get("title", "")).lower()):
            continue
        perks.extend(it for it in (p.get("items", []) or []) if _BENEFIT_RE.search(it))
        perks.extend(s for s in _substantive(_sentences(p.get("text", ""))) if _BENEFIT_RE.search(s))
    perks = _dedupe((p for p in perks if not _CTA_RE.match(p)), 8)

    # TECH STACK — unambiguous technologies named anywhere on the site.
    tech_stack = [t for t in _TECH_LEXICON if re.search(r"(?<![a-z])" + re.escape(t) + r"(?![a-z])", low)]

    keywords = core.extract_keywords(blob, top_n=25) if blob else []
    return {
        "name": name,
        "what_they_do": what_they_do,
        "mission": mission,
        "about": about,
        "values": values,
        "initiatives": initiatives,
        "perks": perks,
        "tech_stack": tech_stack,
        "looking_for": looking_for,
        "keywords": keywords,
        "pages": [p.get("url") for p in pages],
    }


def fetch_jd_url(url, limit=8000):
    try:
        r = safefetch._safe_get(url, timeout=15)
        if r.status_code >= 400:
            return ""
        _, text = clean_html(r.text, limit=limit)
        return text
    except Exception:
        return ""
