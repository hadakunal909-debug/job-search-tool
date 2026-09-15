"""TriNet Hire's public, server-rendered career tables."""
import re
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from scraper.infosys import PartialScrapeError


def board_url(url):
    p = urlparse(url)
    m = re.match(r"^/companies/(\d+-[a-z0-9-]+)(?:/|$)", p.path)
    if p.hostname != "app.trinethire.com" or not m:
        raise ValueError("Not a TriNet Hire employer portal")
    return "https://app.trinethire.com/companies/" + m[1] + "/jobs"


def page(url):
    import scraper
    r = scraper.SESSION.get(url, timeout=20)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def parse_jobs(html, url):
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.job-list")
    if not table:
        raise ValueError("TriNet job table is absent")
    # The current portal renders the complete table. Fail visibly if it adds paging.
    if soup.select_one(".pagination, a[rel=next]"):
        raise ValueError("TriNet pagination needs review")
    board = board_url(url)
    rows, seen = [], set()
    for tr in table.select("tr.job"):
        a = tr.select_one("a[href]")
        target = urljoin(board, a["href"]) if a else ""
        if not target.startswith(board + "/") or not a.get_text(strip=True):
            raise PartialScrapeError("TriNet posting identity is missing", rows)
        if target in seen:
            continue
        seen.add(target)
        loc = tr.select_one(".location")
        rows.append({"url": target, "title": a.get_text(" ", strip=True),
                     "location": loc.get_text(" ", strip=True) if loc else ""})
    return rows


def scrape_trinethire(url):
    return parse_jobs(str(page(board_url(url))), url)


def identity_text(url):
    soup = page(board_url(url))
    return soup.title.get_text(" ", strip=True) if soup.title else ""


def detail_jd(url):
    # JSON-LD is the employer's complete description on the public posting page.
    import json
    board_url(url)  # Validate the employer host before fetching a detail page.
    soup = page(url)
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(tag.string or tag.get_text())
        except ValueError:
            continue
        for obj in data if isinstance(data, list) else [data]:
            if isinstance(obj, dict) and obj.get("@type") == "JobPosting":
                return BeautifulSoup(obj.get("description") or "", "html.parser").get_text("\n", strip=True), (obj.get("datePosted") or "")[:10]
    description = soup.select_one(".job-descr.content")
    return (description.get_text("\n", strip=True), "") if description else ("", "")
