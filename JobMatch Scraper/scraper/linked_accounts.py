"""Resolve vendor short posting links to the supported Workable/Jobvite account.

The account is absent from /j/<token> and legacy CompanyJobs/Job.aspx URLs. Guessing
``j`` or ``CompanyJobs`` creates a non-existent board. Their public HTTP redirects,
canonical URLs, or Jobvite's own embed markup supply the missing account instead.
No token is decoded, no account is guessed, and no database is read or written.
"""
import html
import re
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

_SLUG = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]*\Z")
_RESERVED = {"j", "job", "jobs", "careers", "companyjobs", "api", "static", "search",
             "account", "login", "www", "apply"}
_VENDOR_URL = re.compile(r"https?://(?:apply\.workable\.com|jobs\.jobvite\.com)/[^\s\"'<>\\]+", re.I)


def is_account_link(url):
    """Whether this URL needs a network lookup to discover its account."""
    try:
        parsed = urlparse(url or "")
        if parsed.scheme not in ("http", "https") or parsed.username or parsed.password:
            return False
        host = parsed.netloc.lower()
        if host == "apply.workable.com":
            return bool(re.fullmatch(r"/j/[a-zA-Z0-9]+/?", parsed.path))
        return (host == "app.jobvite.com"
                and parsed.path.lower() == "/companyjobs/job.aspx"
                and bool(parse_qs(parsed.query).get("j", [""])[0]))
    except (TypeError, ValueError):
        return False


def _account(url, platform):
    """Normalize only vendor-owned account paths, never an asset or generic route."""
    try:
        parsed = urlparse(html.unescape(url or ""))
        if parsed.scheme not in ("http", "https") or parsed.username or parsed.password:
            return None
        host = parsed.netloc.lower()
        parts = [p for p in parsed.path.split("/") if p]
        if platform == "workable":
            if host != "apply.workable.com":
                return None
            origin = "https://apply.workable.com"
        else:
            if host != "jobs.jobvite.com":
                return None
            origin = "https://jobs.jobvite.com"
            if parts and parts[0].lower() == "careers":
                parts = parts[1:]
        slug = parts[0] if parts else ""
        if not _SLUG.fullmatch(slug) or slug.lower() in _RESERVED:
            return None
        # An arbitrary path under a vendor does not establish a career account.
        if len(parts) > 1 and parts[1].lower() not in ("j", "job", "jobs"):
            return None
        from scraper import _name_from
        name = slug.upper() if len(slug) <= 4 and slug.isalpha() else _name_from(slug)
        return origin + "/" + slug, platform, name
    except (TypeError, ValueError):
        return None


def resolve_linked_account(url, response=None):
    """Return (board_url, ats_type, name) or None for an unresolved short posting link.

    Passing the response already fetched by detect_linked_ats avoids a second request.
    A direct caller gets one SSRF-checked fetch, including guarded redirects. A refused,
    expired, or ambiguous page remains unresolved; it never produces a guessed account.
    """
    if not is_account_link(url):
        return None
    platform = "workable" if urlparse(url).netloc.lower() == "apply.workable.com" else "jobvite"
    if response is None:
        from scraper import _safe_get
        try:
            response = _safe_get(url, timeout=15)
        except Exception:
            return None
    if response.status_code not in (200, 301, 302, 303, 307, 308):
        return None
    landing = str(getattr(response, "url", "") or url)
    direct = _account(landing, platform)
    if direct:
        return direct
    if response.status_code != 200:
        # _safe_get stops after three checked hops. An account URL already reached (or
        # named by this vendor's next redirect) is still an authoritative resolution;
        # probe_board will separately determine whether its listings are readable.
        target = urljoin(landing, response.headers.get("location", ""))
        return _account(target, platform)
    text = response.text or ""
    soup = BeautifulSoup(text, "html.parser")
    preferred = set()
    for el in soup.select('link[rel="canonical"][href], meta[property="og:url"][content]'):
        result = _account(urljoin(landing, el.get("href") or el.get("content")), platform)
        if result:
            preferred.add(result)
    if preferred:
        return next(iter(preferred)) if len(preferred) == 1 else None
    candidates = set()
    # Jobvite's documented iframe loader reads this attribute. Imprivata's legacy
    # posting redirects to an employer page with exactly this embed and no full ATS URL.
    if platform == "jobvite":
        for el in soup.select(".jv-careersite[data-careersite]"):
            slug = el.get("data-careersite", "").strip()
            if _SLUG.fullmatch(slug):
                result = _account("https://jobs.jobvite.com/" + slug, platform)
                if result:
                    candidates.add(result)
    for el in soup.select("a[href], iframe[src]"):
        result = _account(urljoin(landing, el.get("href") or el.get("src")), platform)
        if result:
            candidates.add(result)
    # Embedded configuration may escape slashes. Restrict the host and account path;
    # generic JS assets such as /__assets__/... cannot become bogus Jobvite accounts.
    for match in _VENDOR_URL.finditer(html.unescape(text).replace(r"\/", "/")):
        result = _account(match.group(0), platform)
        if result:
            candidates.add(result)
    return next(iter(candidates)) if len(candidates) == 1 else None
