"""JazzHR's public hosted career pages, without applicant/session APIs."""
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from scraper.infosys import PartialScrapeError


def board_url(url):
    p = urlparse(url)
    if not re.fullmatch(r"[a-z0-9-]+\.applytojob\.com", p.hostname or ""):
        raise ValueError("Not a JazzHR employer portal")
    return "https://" + p.hostname + "/apply"


def page(url):
    import scraper
    board_url(url)
    r = scraper._safe_get(url, timeout=20)
    r.raise_for_status()
    # Some hosted pages declare UTF-8 while serving Windows-1252 punctuation.
    raw = r.content
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    return BeautifulSoup(text, "html.parser")


def parse_page(soup, url):
    board = board_url(url)
    rows = []
    cards = soup.select("li.list-group-item")
    for card in cards:
        a = card.select_one("h3 a[href]")
        if not a:
            continue
        target = urljoin(board, a["href"])
        if (urlparse(target).hostname != urlparse(board).hostname
                or not re.fullmatch(r"/apply/[A-Za-z0-9]+/[^/]+/?", urlparse(target).path)
                or not a.get_text(strip=True)):
            raise PartialScrapeError("JazzHR posting identity is missing", rows)
        marker = card.select_one(".fa-map-marker")
        rows.append({"title": a.get_text(" ", strip=True),
                     "url": target.split("?")[0],
                     "location": marker.parent.get_text(" ", strip=True) if marker else ""})
    if not rows:
        text = soup.get_text(" ", strip=True).lower()
        # A branded header alone cannot establish an empty board.
        if not any(x in text for x in ("no open positions", "no current openings",
                                       "no openings at this time", "no open jobs")):
            raise ValueError("JazzHR listing or explicit empty state is absent")
    next_link = soup.select_one('a[rel="next"], .pagination .next:not(.disabled) a[href]')
    if soup.select_one(".pagination") and not next_link:
        # An unfamiliar paginator must not silently truncate the board.
        active = soup.select_one(".pagination .active")
        if not active:
            raise PartialScrapeError("JazzHR pagination needs review", rows)
    return rows, urljoin(url, next_link["href"]) if next_link else ""


def scrape_jazzhr(url):
    board = board_url(url)
    current, visited, rows, seen = board, set(), [], set()
    while current:
        try:
            if current in visited or len(visited) >= 50:
                raise ValueError("JazzHR pagination did not finish")
            if board_url(current) != board:
                raise ValueError("JazzHR pagination changed employer")
            visited.add(current)
            batch, next_url = parse_page(page(current), current)
            new = [r for r in batch if r["url"] not in seen]
            if batch and not new:
                raise ValueError("JazzHR repeated a results page")
            rows.extend(new)
            seen.update(r["url"] for r in new)
            current = next_url
        except Exception as exc:
            partial = getattr(exc, "rows", [])
            raise PartialScrapeError(str(exc), rows + partial) from exc
    return rows


def detail_jd(url):
    soup = page(url)
    description = soup.select_one("#job-description")
    return (description.get_text("\n", strip=True), "") if description else ("", "")
