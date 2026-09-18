"""Avature listings that publish complete next-page links, including Siemens.

Some tenants use JobDetail links but folderOffset pagination. Follow the portal's
links, retaining country filters, instead of guessing the offset parameter from
the posting URL. A '999+' total is a lower bound, not an end-of-board signal.
"""
import datetime
import re
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup
from scraper.infosys import PartialScrapeError

SIEMENS_BOARD = ("https://jobs.siemens.com/en_US/externaljobs/SearchJobs/"
                 "?42386=%5B812209%5D&42386_format=17546&listFilterMode=1")
SIEMENS_ENERGY_BOARD = "https://jobs.siemens-energy.com/en_US/jobs/SearchJobsUS"
MAX_PAGES = 1500


def _board_parts(url):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or not re.search(r"/SearchJobs(?:US)?/?$", parsed.path, re.I):
        raise ValueError("Not an Avature SearchJobs listing")
    return parsed


def parse_page(html, url):
    import scraper
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    scraper._avature_cards(html, url, set(), rows)
    for row in rows:
        if urlparse(row["url"]).hostname != urlparse(url).hostname:
            raise ValueError("Avature posting left the listing's host")
        row["source"] = "avature_links"
        if urlparse(url).hostname == "jobs.siemens-energy.com":
            row["company"] = "Siemens Energy"
            if urlparse(url).path.rstrip("/").lower().endswith("/searchjobsus") and not row.get("location"):
                row["location"] = "United States"
        if urlparse(url).hostname == "jobs.siemens.com":
            row["company"] = "Siemens"
            # Siemens' published country filter includes multi-location jobs with
            # at least one US location, even when the card hides its city list.
            if (parse_qs(urlparse(url).query).get("42386") == ["[812209]"]
                    and row.get("location", "").strip().lower() == "multiple locations"):
                row["location"] = "Multiple Locations, United States"
    legend = soup.select_one(".list-controls__text__legend")
    text = legend.get_text(" ", strip=True) if legend else ""
    match = re.search(r"(?:of\s+)?([\d,]+)(\+)?\s+results?", text, re.I)
    total = int(match[1].replace(",", "")) if match else None
    lower_bound = bool(match and match[2])
    if lower_bound:
        total += 1
    nxt = next((a.get("href") for a in soup.select("a[href]")
                if re.search(r"\bnext\s+page\b", a.get("aria-label", ""), re.I)
                or re.match(r"^next\b", a.get_text(" ", strip=True), re.I)), "")
    return rows, urljoin(url, nxt) if nxt else "", total, lower_bound


def scrape_avature_links(board_url):
    import scraper
    board = _board_parts(board_url)
    filters = {k: v for k, v in parse_qs(board.query).items()
               if k not in ("folderOffset", "jobOffset", "folderRecordsPerPage", "jobRecordsPerPage")}
    url, rows, seen, visited, expected = board_url, [], set(), set(), 0

    def read_page(target):
        parsed = _board_parts(target)
        if parsed.hostname != board.hostname or parsed.path.rstrip("/") != board.path.rstrip("/"):
            raise ValueError("Avature pagination left the original listing")
        query = parse_qs(parsed.query)
        if any(query.get(key) != value for key, value in filters.items()):
            raise ValueError("Avature pagination lost a requested filter")
        response = scraper._safe_get(target, timeout=25)
        response.raise_for_status()
        result = parse_page(response.text, target)
        if result[2] is None:
            raise ValueError("Avature response lacks its result count")
        return result

    def retain(page):
        fresh = [row for row in page if row["url"] not in seen]
        for row in fresh:
            seen.add(row["url"])
            rows.append(row)
        return len(fresh)

    try:
        terminal = ""
        while url:
            if url in visited or len(visited) >= MAX_PAGES:
                raise ValueError("Avature pagination repeated or exceeded its safety cap")
            visited.add(url)
            page, nxt, total, lower_bound = read_page(url)
            expected = max(expected, total)
            fresh = retain(page)
            if not fresh and (total or nxt):
                raise ValueError("Avature page repeated or ended before its advertised total")
            terminal, url = url, nxt
        if len(rows) < expected and len(visited) > 1:
            # New postings inserted at the head while a long, newest-first crawl
            # runs can shift one already-seen row into a later page. Recover only
            # concrete unseen postings from a bounded head re-read, then verify
            # the current tail/count. An unresolved gap remains a partial error.
            head = board_url
            for _ in range(5):
                page, nxt, total, _lower = read_page(head)
                expected = max(expected, total)
                retain(page)
                if len(rows) >= expected or not nxt:
                    break
                head = nxt
            page, nxt, total, _lower = read_page(terminal)
            expected = max(expected, total)
            retain(page)
            if nxt:
                raise ValueError("Avature listing grew beyond its verified final page")
        if len(rows) < expected:
            raise ValueError("Avature returned %d of at least %d advertised jobs" % (len(rows), expected))
        return rows
    except Exception as exc:
        raise PartialScrapeError("Avature linked listing incomplete: " + str(exc), rows) from exc


def detail_jd(url):
    """Description plus metadata from Avature's posting articles, excluding site chrome."""
    import scraper
    parsed = urlparse(url)
    if not re.search(r"/(?:JobDetail|FolderDetail)/[^/]+(?:/\d+)?/?$", parsed.path):
        return "", ""
    response = scraper._safe_get(url, timeout=25)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    parts = [a.get_text("\n", strip=True) for a in soup.select("article.article--details .article__content")]
    date = ""
    for field in soup.select(".article__content__view__field"):
        text = field.get_text(" ", strip=True)
        if re.match(r"Posted (?:since|on)\b", text, re.I):
            match = re.search(r"\d{1,2}-[A-Za-z]{3}-\d{4}", text)
            if match:
                try:
                    date = datetime.datetime.strptime(match[0], "%d-%b-%Y").strftime("%Y-%m-%d")
                except ValueError:
                    pass
    return "\n\n".join(p for p in parts if p), date
