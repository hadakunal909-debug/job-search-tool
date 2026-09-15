"""Infosys' public, server-rendered careers board.

The older careers.infosys.com host is no longer the listing. Follow the real
pagination links on digitalcareers.infosys.com and retain every listing returned.
"""
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

BOARD = "https://digitalcareers.infosys.com/infosys/global-careers?location=USA&page=1&per_page=100"


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
