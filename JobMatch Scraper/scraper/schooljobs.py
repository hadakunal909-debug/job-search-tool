"""Public NEOGOV employer boards hosted by SchoolJobs and GovernmentJobs.

Uses the HTML listing endpoint published by AgencyPages/search.js. Validates
the total and requested page; incomplete sweeps retain their successful rows.
"""
import re
import time
from datetime import datetime
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup
from scraper.infosys import PartialScrapeError

MAX_PAGES = 1000
HOSTS = {"schooljobs.com", "www.schooljobs.com", "governmentjobs.com", "www.governmentjobs.com"}


def board_url(url):
    """Only whole public employer boards; never expand restricted subfolders."""
    try:
        parsed = urlparse(url or "")
        if parsed.scheme not in ("http", "https") or parsed.netloc.lower() not in HOSTS:
            return None
        match = re.fullmatch(r"/careers/([a-zA-Z0-9_-]+)(?:/jobs/\d+(?:/[^/]+)?)?/?", parsed.path)
        if not match or match[1].lower() in {"home", "jobinfo", "jobs", "applications"}:
            return None
        return "https://www.%s/careers/%s" % (parsed.netloc.lower().removeprefix("www."), match[1].lower())
    except (TypeError, ValueError):
        return None


def _page_url(board, page):
    return urljoin(board, "/careers/home/index") + "?" + urlencode({
        "agency": urlparse(board).path.rsplit("/", 1)[-1], "page": page,
        "sort": "PositionTitle", "isDescendingSort": "false"})


def _date(value):
    try:
        return datetime.strptime(value.strip(), "%m/%d/%y").date().isoformat()
    except ValueError:
        return ""


def _parse(source, board, page):
    soup = BeautifulSoup(source, "html.parser")
    count = soup.select_one("#job-postings-number")
    if not count or not re.fullmatch(r"[\d,]+", count.get_text(strip=True)):
        raise ValueError("NEOGOV listing is missing its public job count")
    total = int(count.get_text(strip=True).replace(",", ""))
    current = soup.select(".pagination li.active")
    if current and any(item.get_text(strip=True) != str(page) for item in current):
        raise ValueError("NEOGOV did not return the requested page")
    if page > 1 and not current:
        raise ValueError("NEOGOV later page has no pagination metadata")
    table = {}
    for cell in soup.select("th.job-table-title[data-job-id]"):
        row = cell.find_parent("tr")
        posted = row.select_one(".job-table-posted")
        location = row.select_one(".job-table-location")
        table[cell["data-job-id"]] = (
            _date(posted.get_text(strip=True)) if posted else "",
            location.get_text(" ", strip=True) if location else "")
    result = []
    for item in soup.select("li.list-item[data-job-id]"):
        link = item.select_one("h3 a.item-details-link[href]")
        if not link:
            raise ValueError("NEOGOV job row has no title or public link")
        target = urljoin(board, link["href"])
        parsed = urlparse(target)
        expected_path = urlparse(board).path + "/jobs/" + item["data-job-id"]
        if (board_url(target) != board or not item["data-job-id"].isdigit()
                or not (parsed.path == expected_path or parsed.path.startswith(expected_path + "/"))):
            raise ValueError("NEOGOV job URL belongs to a different employer or posting")
        title = link.get_text(" ", strip=True)
        if not title:
            raise ValueError("NEOGOV job title is empty")
        posted, location = table.get(item["data-job-id"], ("", ""))
        # The published share title is location + title on boards hiding location
        # columns. Only remove an exact title suffix, never guess the place.
        if not location:
            share = item.select_one("[data-title]")
            if share and share["data-title"].endswith(title):
                location = share["data-title"][:-len(title)].strip()
        row = {"title": title, "url": target.split("?", 1)[0],
               "location": location, "source": "schooljobs"}
        if posted:
            row["date_posted"] = posted
        result.append(row)
    if total == 0 and result:
        raise ValueError("NEOGOV count conflicts with its listed jobs")
    if total:
        ranges = soup.select(".items-div")
        wanted = [str((page - 1) * 10 + 1), str(min(page * 10, total))]
        if not ranges or any([span.get_text(strip=True) for span in item.select("span")] != wanted
                             for item in ranges):
            raise ValueError("NEOGOV listing range does not match the requested page")
        if len(result) != min(10, max(0, total - (page - 1) * 10)):
            raise ValueError("NEOGOV page is missing advertised rows")
    return result, total


def _fetch(board, page):
    import scraper
    response = scraper._safe_get(_page_url(board, page), headers=dict(
        scraper.HEADERS, **{"X-Requested-With": "XMLHttpRequest", "Referer": board}), timeout=30)
    response.raise_for_status()
    landing = urlparse(response.url)
    agency = urlparse(board).path.rsplit("/", 1)[-1]
    if (landing.netloc.lower() not in HOSTS or landing.path.lower() != "/careers/home/index"
            or parse_qs(landing.query).get("agency") != [agency]):
        raise ValueError("NEOGOV listing left its public employer search")
    # Some education customers redirect GovernmentJobs to SchoolJobs. The
    # published agency slug must survive unchanged before accepting the alias.
    actual_board = "https://" + landing.netloc.lower() + "/careers/" + agency
    return _parse(response.text, actual_board, page)


def scrape_schooljobs(url):
    board = board_url(url)
    if not board:
        raise ValueError("Unsupported public NEOGOV employer board")
    rows, seen, expected, raw_count, pages = [], set(), None, 0, set()
    for page in range(1, MAX_PAGES + 1):
        try:
            batch, total = _fetch(board, page)
            if expected is not None and total != expected:
                raise ValueError("NEOGOV job count changed during pagination")
            expected = total
            ids = [row["url"] for row in batch]
            signature = tuple(ids)
            if signature in pages:
                raise ValueError("NEOGOV returned a repeated page")
            pages.add(signature)
            # The vendor can list one job more than once, even on different
            # pages. Its total counts entries; verify every range and dedupe
            # public job IDs only after accounting for all advertised entries.
            for row in batch:
                if row["url"] not in seen:
                    rows.append(row)
                    seen.add(row["url"])
            raw_count += len(batch)
            seen.update(ids)
            if raw_count == total:
                return rows
            if not batch or raw_count > total:
                raise ValueError("NEOGOV listing does not match its advertised count")
        except Exception as exc:
            if rows:
                raise PartialScrapeError("NEOGOV pagination failed: %s" % str(exc), rows) from exc
            raise
        time.sleep(0.15)
    raise PartialScrapeError("NEOGOV page limit reached before advertised total", rows)


def probe_schooljobs(url):
    board = board_url(url)
    if not board:
        raise ValueError("Unsupported public NEOGOV employer board")
    rows, total = _fetch(board, 1)
    if not rows and total:
        raise ValueError("NEOGOV listing contains no public rows")
    return total


def description_html(source):
    """Read the actual description, excluding site-wide privacy and login text."""
    soup = BeautifulSoup(source, "html.parser")
    description = soup.select_one("#details-info")
    if not description or len(description.get_text(" ", strip=True)) < 100:
        return ""
    for unwanted in description.select("script,style,input,button"):
        unwanted.decompose()
    return str(description)
