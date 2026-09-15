"""Native iCIMS public HTML search and posting pages.

Availability is portal-specific: an unreadable response is a failure, never an
empty board. Follow the published pagination and keep incomplete runs partial.
"""
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup
from scraper.infosys import PartialScrapeError


def location_text(value):
    match = re.fullmatch(r"([A-Z]{2})-([A-Z]{2})-(.+)", value.strip())
    if match:
        return ', '.join((match[3], match[2], match[1]))
    return 'US' if value.strip() == 'US-' else value.strip()


def board_url(url):
    p = urlparse(url)
    if not re.fullmatch(r"[a-z0-9-]+\.icims\.com", p.hostname or ""):
        raise ValueError("Not a native iCIMS employer portal")
    return "https://" + p.hostname + "/jobs/search?ss=1&in_iframe=1"


def page(url):
    import scraper
    from requests.utils import default_user_agent
    board_url(url)
    # Canonical public posting URLs render an iframe wrapper by default.
    # Request the same inner page used by the employer's own embedded board.
    parts = urlparse(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query['in_iframe'] = '1'
    url = parts._replace(query=urlencode(query)).geturl()
    # The shared scraper impersonates an old Chrome version. iCIMS rejects
    # that header while accepting this same request's truthful Python client ID.
    r = scraper._safe_get(url, headers={"User-Agent": default_user_agent(), "Accept": "*/*"}, timeout=20)
    r.raise_for_status()
    try:
        text = r.content.decode("utf-8")
    except UnicodeDecodeError:
        text = r.content.decode("cp1252", errors="replace")
    return BeautifulSoup(text, "html.parser")


def parse_page(soup, url):
    rows = []
    for card in soup.select(".iCIMS_JobCardItem, .iCIMS_JobsTable .iCIMS_JobsTableRow"):
        a = card.select_one('a[href*="/jobs/"][href*="/job"]')
        if not a:
            raise PartialScrapeError("iCIMS posting link is missing", rows)
        target = urljoin(url, a["href"])
        p = urlparse(target)
        if p.hostname != urlparse(url).hostname or not re.fullmatch(r"/jobs/\d+/(?:[^/]+/)?job/?", p.path):
            raise PartialScrapeError("iCIMS posting identity is invalid", rows)
        title = a.select_one("h3, h2")
        if title is None:
            title = BeautifulSoup(str(a), "html.parser")
            for label in title.select(".sr-only, .field-label"):
                label.decompose()
        title = title.get_text(" ", strip=True)
        if not title:
            raise PartialScrapeError("iCIMS posting title is missing", rows)
        loc = ""
        for field in card.select(".header"):
            label = field.select_one('.field-label')
            if label and 'location' in label.get_text(' ', strip=True).lower():
                label.decompose()
                loc = field.get_text(' ', strip=True)
                break
        for field in card.select(".iCIMS_JobHeaderTag"):
            dt, dd = field.select_one("dt"), field.select_one("dd")
            if dt and dd and "location" in dt.get_text(" ", strip=True).lower():
                loc = dd.get_text(" ", strip=True)
                break
        date = ''
        posted = card.select_one('.header.right span[title]')
        if posted:
            try:
                date = datetime.strptime(posted['title'].split()[0], '%m/%d/%Y').date().isoformat()
            except ValueError:
                pass
        row = {"title": title, "url": "https://" + p.hostname + p.path,
               "location": location_text(loc)}
        if date:
            row['found_date'] = date
        rows.append(row)
    if not rows:
        text = soup.get_text(" ", strip=True).lower()
        if not any(x in text for x in ("no jobs found", "no matching jobs", "no jobs are currently available",
                                       "there are currently no job", "no positions match")):
            raise ValueError("iCIMS results or explicit empty state is absent")
    current_page = int(parse_qs(urlparse(url).query).get("pr", ["0"])[0] or 0)
    next_url = ""
    # The last page still contains a disabled 'next' link. Use page numbers,
    # not the mere presence of that anchor, to establish completion.
    for a in soup.select("a[href]"):
        label = a.get_text(" ", strip=True)
        if "next page of results" in label.lower():
            target = urljoin(url, a["href"])
            values = parse_qs(urlparse(target).query).get("pr", [])
            if values and values[0].isdigit() and int(values[0]) > current_page:
                next_url = target
    counts = re.search(r"Page\s+(\d+)\s+of\s+(\d+)", soup.get_text(" ", strip=True), re.I)
    if counts:
        actual, total = map(int, counts.groups())
        if actual != current_page + 1 or (actual < total and not next_url):
            raise PartialScrapeError("iCIMS pagination is incomplete", rows)
        if actual == total:
            next_url = ""
    return rows, next_url


def scrape_icims(url):
    board = board_url(url)
    current, visited, rows, seen = board, set(), [], set()
    while current:
        try:
            if current in visited or len(visited) >= 150:
                raise ValueError("iCIMS pagination did not finish within 150 pages")
            if board_url(current) != board:
                raise ValueError("iCIMS pagination changed employer")
            visited.add(current)
            batch, next_url = parse_page(page(current), current)
            new = [r for r in batch if r["url"] not in seen]
            if batch and not new:
                raise ValueError("iCIMS repeated a results page")
            rows.extend(new)
            seen.update(r["url"] for r in new)
            current = next_url
        except Exception as exc:
            raise PartialScrapeError(str(exc), rows + getattr(exc, "rows", [])) from exc
    # Some portals omit location from every card. Enrich only titles the intake
    # already accepts; retaining the other rows still preserves board completeness.
    import scraper
    remaining, until = 50, time.monotonic() + 45
    for row in rows:
        if not row['location'] and scraper.title_verdict(row['title'])[0]:
            if remaining <= 0 or time.monotonic() >= until:
                break
            remaining -= 1
            try:
                jd, date, loc = detail_fields(row['url'])
                row['location'] = loc
                if jd:
                    row['jd'] = jd
                if date:
                    row['found_date'] = date
            except Exception:
                pass  # Unknown location remains explicit; intake can retry the JD.
    return rows


def posting_date(job, now=None):
    """Reject iCIMS template dates generated from the request clock.

    Ascension's detail page emits now minus two years / now plus one year on
    every request. Those moving timestamps are not dates for this posting.
    A separately stated listing date remains usable.
    """
    raw = str(job.get('datePosted') or '')
    try:
        posted = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        expires = datetime.fromisoformat(str(job.get('validThrough') or '').replace('Z', '+00:00'))
        clock = now or datetime.now(timezone.utc)
        if (posted.tzinfo and expires.tzinfo
                and posted.year == clock.year - 2 and expires.year == clock.year + 1
                and posted.replace(year=clock.year) == expires.replace(year=clock.year)
                and abs((posted.replace(year=clock.year) - clock).total_seconds()) < 300):
            return ''
    except (ValueError, TypeError):
        pass
    return raw[:10]


def detail_fields(url):
    soup = page(url)
    from scraper.score_jobs import _jobposting_nodes
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(tag.string or tag.get_text(), strict=False)
        except ValueError:
            continue
        for job in _jobposting_nodes(data):
            description = BeautifulSoup(job.get("description") or "", "html.parser").get_text("\n", strip=True)
            if description:
                places = job.get('jobLocation') or []
                if isinstance(places, dict):
                    places = [places]
                locations = []
                for place in places:
                    address = place.get('address') or {}
                    country = address.get('addressCountry') or ''
                    if isinstance(country, dict):
                        country = country.get('name') or ''
                    value = ', '.join(str(x) for x in (address.get('addressLocality'), address.get('addressRegion'), country) if x)
                    if value and value not in locations:
                        locations.append(value)
                return description, posting_date(job), '; '.join(locations)
    # Keep the actual JD sections; omit apply/login controls and talent networks.
    sections = []
    for content in soup.select(".iCIMS_JobContent"):
        heading = content.select_one("h2")
        if heading and heading.get_text(" ", strip=True).lower() in ("connect with us!", "options"):
            continue
        for form in content.select("form, script, style"):
            form.decompose()
        sections.append(content.get_text("\n", strip=True))
    return "\n\n".join(sections), "", ""


def detail_jd(url):
    text, date, _ = detail_fields(url)
    return text, date
