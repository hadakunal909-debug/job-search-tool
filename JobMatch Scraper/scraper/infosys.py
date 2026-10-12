"""Infosys' official careers listing and its public browser search API.

The October 2026 listing renders through Algolia. Discover its public search-only
configuration from the scripts linked by the official page; retain the legacy
HTML pagination path for older deployments and every row on a partial failure.
"""
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

BOARD = "https://digitalcareers.infosys.com/infosys/global-careers?location=USA&page=1&per_page=100"
SEARCH_PAGE_SIZE = 100
SEARCH_MAX_PAGES = 100


class PartialScrapeError(RuntimeError):
    """A later page failed; callers can still preserve the rows already obtained."""
    def __init__(self, message, rows):
        super().__init__(message)
        self.rows = rows


def parse_page(html, url):
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for link in soup.select("a.job[href]"):
        if "/company-job/description/reqid/" not in link["href"]:
            continue
        title = link.select_one(".job-title")
        loc = link.select_one(".js-job-city")
        if title is None or not title.get_text(strip=True):
            continue
        rows.append({"title": title.get_text(" ", strip=True),
                     "url": urljoin(url, link["href"]),
                     "company": "Infosys",
                     "location": loc.get_text(" ", strip=True) if loc else "",
                     "source": "infosys"})
    nxt = next((a.get("href") for a in soup.select("a.pagination-button[href]")
                if a.get_text(" ", strip=True).lower() == "next"), "")
    total = re.search(r"Showing\s+[\d,]+\s+to\s+[\d,]+\s+of\s+([\d,]+)\s+matching jobs",
                      soup.get_text(" ", strip=True), re.I)
    return rows, urljoin(url, nxt) if nxt else "", int(total[1].replace(",", "")) if total else None


def _search_config(html, url):
    """Read the public search configuration the official page gives every visitor."""
    import scraper
    soup = BeautifulSoup(html, "html.parser")
    scripts = [tag.get("src", "") for tag in soup.find_all("script", src=True)]
    for src in scripts:
        script_url = urljoin(url, src)
        parsed = urlparse(script_url)
        # Only the Infosys assets actually linked by this page qualify. Do not
        # turn arbitrary third-party scripts or an unrelated tenant into a board.
        if (parsed.scheme != "https" or
                not (parsed.hostname == "digitalcareers.infosys.com" or
                     (parsed.hostname or "").endswith(".cloudfront.net")) or
                "/assets/infosys/merged/js/" not in parsed.path):
            continue
        response = scraper._safe_get(script_url, timeout=25)
        response.raise_for_status()
        config = re.search(r"algoliasearch\(\s*['\"]([A-Z0-9]{10})['\"]\s*,\s*"
                           r"['\"]([a-zA-Z0-9]{20,128})['\"]\s*\)", response.text)
        index = re.search(r"indexName\s*:\s*['\"]([A-Za-z0-9_]*Infosys[A-Za-z0-9_]*)['\"]",
                          response.text)
        if config and index:
            return config[1], config[2], index[1]
    raise ValueError("Infosys page has no readable listings or public search configuration")


def _search_page(config, page=0):
    import scraper
    app, key, index = config
    response = scraper._safe_post(
        "https://%s-dsn.algolia.net/1/indexes/%s/query" % (app.lower(), index),
        {"query": "", "facetFilters": ["country:USA"],
         "hitsPerPage": SEARCH_PAGE_SIZE, "page": page},
        headers={"X-Algolia-Application-Id": app, "X-Algolia-API-Key": key}, timeout=25)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("Infosys public search did not return an object")
    hits, total, returned_page = data.get("hits"), data.get("nbHits"), data.get("page")
    if (not isinstance(hits, list) or isinstance(total, bool) or
            not isinstance(total, int) or total < 0 or len(hits) > total or
            isinstance(returned_page, bool) or not isinstance(returned_page, int) or
            returned_page != page or data.get("exhaustiveNbHits") is False):
        raise ValueError("Infosys search schema, count or page is missing")
    return hits, total


def _search_row(hit):
    def value(key):
        out = hit.get(key)
        return out[0] if isinstance(out, list) and len(out) == 1 else out

    if not isinstance(hit, dict):
        raise ValueError("Infosys search listing is not an object")
    reqid, title, country = value("reqid"), value("title"), value("country")
    locations = hit.get("work_location")
    if isinstance(locations, list):
        if not locations or any(not isinstance(x, str) or not x.strip() for x in locations):
            raise ValueError("Infosys search listing has malformed locations")
        locations = "; ".join(x.strip() for x in locations)
    if (not isinstance(reqid, str) or not re.fullmatch(r"\d+[A-Z]{1,2}", reqid, re.I) or
            not isinstance(title, str) or not title.strip() or country != "USA" or
            not isinstance(locations, str) or not locations.strip()):
        raise ValueError("Infosys search listing lacks a valid US ID, title or location")
    return {"title": title.strip(), "company": "Infosys", "source": "infosys",
            "url": "https://digitalcareers.infosys.com/global-careers/company-job/description/reqid/" + reqid,
            "location": locations.strip() + ", USA"}


def _scrape_search(config):
    rows, seen, expected = [], set(), 0
    try:
        for page in range(SEARCH_MAX_PAGES):
            hits, total = _search_page(config, page)
            expected = max(expected, total)
            fresh = 0
            for hit in hits:
                row = _search_row(hit)
                if row["url"] in seen:
                    continue
                seen.add(row["url"])
                rows.append(row)
                fresh += 1
            if total < expected or len(rows) > total:
                raise ValueError("Infosys search count changed or unique rows exceeded its total")
            if len(rows) == total:
                return rows
            if not fresh:
                raise ValueError("Infosys search repeated or ended before its advertised total")
        raise ValueError("Infosys search exceeded its page safety cap")
    except Exception as exc:
        raise PartialScrapeError("Infosys search incomplete: " + str(exc), rows) from exc


def probe_infosys(board_url=BOARD):
    """One listing request (plus the linked search script for the modern page)."""
    import scraper
    url = board_url if urlparse(board_url).hostname == "digitalcareers.infosys.com" else BOARD
    response = scraper._safe_get(url, timeout=25)
    response.raise_for_status()
    _rows, _next, total = parse_page(response.text, url)
    return total if total is not None else _search_page(_search_config(response.text, url))[1]


def scrape_infosys(board_url=BOARD):
    import scraper
    url = board_url if urlparse(board_url).hostname == "digitalcareers.infosys.com" else BOARD
    rows, seen, visited = [], set(), set()
    expected = None
    while url:
        if url in visited or len(visited) >= 100:
            raise PartialScrapeError("Infosys pagination repeated or exceeded 100 pages", rows)
        if urlparse(url).hostname != "digitalcareers.infosys.com":
            raise PartialScrapeError("Infosys pagination left its official host", rows)
        visited.add(url)
        try:
            response = scraper._safe_get(url, timeout=25)
            response.raise_for_status()
            page, nxt, count = parse_page(response.text, url)
            if not rows and not page and count is None:
                return _scrape_search(_search_config(response.text, url))
        except PartialScrapeError:
            raise
        except Exception as exc:
            raise PartialScrapeError("Infosys listing fetch failed: %s" % type(exc).__name__, rows) from exc
        if expected is None:
            expected = count
        fresh = [r for r in page if r["url"] not in seen]
        if not page and count != 0:
            raise PartialScrapeError("Infosys page has no readable job listings", rows)
        if page and not fresh:
            raise PartialScrapeError("Infosys pagination returned duplicate listings", rows)
        for row in fresh:
            seen.add(row["url"])
            rows.append(row)
        url = nxt
    if expected is not None and len(rows) < expected:
        raise PartialScrapeError("Infosys returned %d of %d advertised jobs" % (len(rows), expected), rows)
    return rows


def detail_jd(url):
    """Extract the posting's description, excluding the careers navigation/footer."""
    import scraper
    if urlparse(url).hostname != "digitalcareers.infosys.com":
        return ""
    response = scraper._safe_get(url, timeout=25)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    content = soup.select_one(".description-page-right")
    return content.get_text("\n", strip=True) if content else ""
