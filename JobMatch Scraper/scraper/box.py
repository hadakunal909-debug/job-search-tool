"""Box's public careers listing, paginated without filtering away any jobs."""
import re
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from scraper.infosys import PartialScrapeError

BOARD = "https://careers.box.com/en/jobs/"


def parse_page(html, url):
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for card in soup.select(".card-job"):
        link = card.select_one("a.js-view-job[href]")
        if link is None:
            continue
        marker = card.select_one(".map-marker")
        location = marker.parent.get_text(" ", strip=True) if marker else ""
        rows.append({"company": "Box", "title": link.get_text(" ", strip=True),
                     "url": urljoin(url, link["href"]), "location": location})
    nxt = soup.select_one('a[rel~="next"][href]')
    count = re.search(r"Displaying\s+\d+\s+to\s+\d+\s+of\s+([\d,]+)\s+matching jobs",
                      soup.get_text(" ", strip=True), re.I)
    return rows, urljoin(url, nxt["href"]).split("#")[0] if nxt else "", int(count[1].replace(",", "")) if count else None


def scrape_box(board_url=BOARD):
    import scraper
    rows, seen, visited = [], set(), set()
    url, total = BOARD, None
    while url:
        if url in visited or len(visited) >= 100 or urlparse(url).hostname != "careers.box.com":
            raise PartialScrapeError("Box pagination repeated, exceeded its cap or changed host", rows)
        visited.add(url)
        try:
            response = scraper._safe_get(url, timeout=20)
            response.raise_for_status()
            page, nxt, count = parse_page(response.text, response.url)
        except Exception as exc:
            raise PartialScrapeError("Box listing fetch failed: %s" % type(exc).__name__, rows) from exc
        if total is None:
            total = count
        fresh = [row for row in page if row["url"] not in seen]
        if not fresh and count != 0:
            raise PartialScrapeError("Box listing missing or pagination repeated", rows)
        for row in fresh:
            rows.append(row)
            seen.add(row["url"])
        url = nxt
    if total is None or len(rows) < total:
        raise PartialScrapeError("Box returned %d of %s advertised jobs" % (len(rows), total), rows)
    return rows
