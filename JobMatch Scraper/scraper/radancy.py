"""Read employer-hosted Radancy/TalentBrew listings and their public pagination API.

The AJAX endpoint returns rendered HTML, not a private ATS API. All rows are retained;
the shared intake pipeline decides which titles and locations belong in the feed.
"""
import json
import os
import re
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup


MAX_PAGES = 500
PAGE_SIZE = 100
TIME_BUDGET = 180


class RadancyRows(list):
    """A partial result keeps its postings but must not be used to close missing jobs."""
    def __init__(self):
        super().__init__()
        self.complete = True
        self.reason = ""
        self.reported_total = None


def _get(url, params=None):
    import scraper
    response = scraper._safe_get(url, params=params, headers=scraper.HEADERS, timeout=25)
    response.raise_for_status()
    return response


def _page_url(url, page):
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() != "p"]
    if page != 1:
        query.append(("p", str(page)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _block(soup):
    block = soup.select_one('#search-results[data-total-pages][data-current-page]')
    if block is None or not block.get("data-search-results-module-name"):
        return None
    endpoint = block.get("data-ajax-url", "")
    return block if "/search-jobs/results" in endpoint else None


def _listing(url):
    """Resolve a careers home page or detail link to a verified listing, at most 3 GETs."""
    candidates = [_page_url(url, 1)]
    last_error = None
    for index in range(3):
        if index >= len(candidates):
            break
        candidate = candidates[index]
        try:
            response = _get(candidate)
            soup = BeautifulSoup(response.text, "html.parser")
            if _block(soup) is not None:
                return _page_url(response.url, 1), soup
            for anchor in soup.select('a[href]'):
                href = urljoin(response.url, anchor.get("href", ""))
                parts = urlsplit(href)
                if (parts.netloc == urlsplit(response.url).netloc
                        and re.search(r"/search-jobs/?$", parts.path)):
                    plain = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
                    if plain not in candidates:
                        candidates.append(plain)
        except Exception as exc:
            last_error = exc
        fallback = urljoin(candidate, "/search-jobs")
        if fallback not in candidates:
            candidates.append(fallback)
    raise ValueError("No readable Radancy listing at %s%s" %
                     (url, ": " + str(last_error) if last_error else ""))


def detect_radancy(url):
    """Return (verified listing URL, 'radancy'), or None; never guess from a hostname."""
    try:
        listing_url, _soup = _listing(url)
        return listing_url, "radancy"
    except Exception:
        return None


def _number(block, key, default=0):
    try:
        return int(block.get(key, default))
    except (TypeError, ValueError):
        return default


def _rows(soup, base_url):
    rows = []
    for anchor in soup.select('#search-results-list a[href]'):
        url = urljoin(base_url, anchor.get("href", ""))
        if not re.search(r"/job/", urlsplit(url).path):
            continue  # Pagination and saved-search links also occur inside this container.
        title_node = anchor.select_one('h2, h3, .job-title, .title')
        title = (title_node.get_text(" ", strip=True) if title_node is not None
                 else (anchor.get("data-title") or "").strip())
        if not title:
            continue
        card = anchor.find_parent("li") or anchor
        location = card.select_one('.job-location, .location, [itemprop="jobLocation"]')
        row = {"title": title, "url": url.split("#", 1)[0],
               "location": location.get_text(" ", strip=True) if location else ""}
        company = card.select_one('.job-company, .company, [itemprop="hiringOrganization"]')
        if company and company.get_text(" ", strip=True):
            row["company"] = company.get_text(" ", strip=True)
        stamp = card.select_one('time[datetime], [itemprop="datePosted"]')
        posted = (stamp.get("datetime") or stamp.get("content") or stamp.get_text(" ", strip=True)
                  if stamp else "")
        if re.match(r"^\d{4}-\d{2}-\d{2}(?:$|T)", posted):
            row["found_date"] = posted[:10]
        rows.append(row)
    return rows


def _criteria(block):
    fields = {
        "ActiveFacetID": "active-facet-id", "Distance": "distance", "Keywords": "keywords",
        "Location": "location", "Latitude": "latitude", "Longitude": "longitude",
        "ShowRadius": "show-radius", "CustomFacetName": "custom-facet-name",
        "FacetTerm": "facet-term", "FacetType": "facet-type",
        "SearchResultsModuleName": "search-results-module-name", "SortCriteria": "sort-criteria",
        "SortDirection": "sort-direction", "KeywordType": "keyword-type",
        "LocationType": "location-type", "LocationPath": "location-path",
        "OrganizationIds": "organization-ids", "PostalCode": "postal-code",
    }
    params = {key: block.get("data-" + attr, "") for key, attr in fields.items()}
    params.update(CurrentPage=1, RecordsPerPage=PAGE_SIZE, SearchType=1,
                  SearchResultType=1, IsPagination="True")
    # An empty array must be omitted. Sending the STRING '[]' searches for a literal tag
    # named [] and returns a convincing, but entirely false, empty board.
    try:
        refined = json.loads(block.get("data-refined-keywords", "[]"))
    except (TypeError, ValueError):
        refined = []
    if isinstance(refined, list) and refined:
        params["RefinedKeywords"] = refined
    return params


def scrape_radancy(board_url):
    """Return deduplicated listing rows; flag every incomplete page walk explicitly."""
    import scraper
    started = time.monotonic()
    maximum = max(1, int(os.environ.get("RADANCY_MAX_PAGES", MAX_PAGES)))
    budget = max(1, int(os.environ.get("RADANCY_TIME_BUDGET", TIME_BUDGET)))
    listing_url, soup = _listing(board_url)
    initial = _block(soup)
    result = RadancyRows()
    seen = set()

    def keep(page_soup):
        added = 0
        for row in _rows(page_soup, listing_url):
            if row["url"] not in seen:
                seen.add(row["url"])
                result.append(row)
                added += 1
        return added

    def partial(reason):
        result.complete, result.reason = False, reason
        scraper.note_truncation(board_url, len(result), maximum * PAGE_SIZE,
                                result.reported_total, "Radancy: " + reason)

    keep(soup)  # Bank the readable first page before trying the more efficient endpoint.
    endpoint = urljoin(listing_url, initial.get("data-ajax-url"))
    params = _criteria(initial)
    use_ajax = urlsplit(endpoint).netloc == urlsplit(listing_url).netloc
    if use_ajax:
        try:
            payload = _get(endpoint, params).json()
            upgraded = BeautifulSoup(payload["results"], "html.parser")
            block = _block(upgraded)
            if block is None or _number(block, "data-current-page") != 1:
                raise ValueError("AJAX response did not contain page 1")
            if _number(initial, "data-total-job-results") > 0 and not _rows(upgraded, listing_url):
                raise ValueError("AJAX search unexpectedly returned no jobs")
            soup = upgraded
            keep(soup)
        except Exception:
            use_ajax = False  # The ordinary public ?p=N pages work without AJAX.

    block = _block(soup)
    total = _number(block, "data-total-job-results", _number(block, "data-total-results"))
    pages = max(1, _number(block, "data-total-pages", 1))
    result.reported_total = total
    for page in range(2, min(pages, maximum) + 1):
        if time.monotonic() - started >= budget:
            partial("time budget exceeded before page %d of %d" % (page, pages))
            break
        try:
            if use_ajax:
                payload = _get(endpoint, dict(params, CurrentPage=page)).json()
                page_soup = BeautifulSoup(payload["results"], "html.parser")
            else:
                page_soup = BeautifulSoup(_get(_page_url(listing_url, page)).text, "html.parser")
            page_block = _block(page_soup)
            if page_block is None or _number(page_block, "data-current-page") != page:
                raise ValueError("server did not return requested page %d" % page)
            if not keep(page_soup):
                raise ValueError("page %d contained no new job URLs" % page)
        except Exception as exc:
            partial("page %d of %d failed: %s" % (page, pages, str(exc)[:140]))
            break
    if result.complete and pages > maximum:
        partial("page cap reached (%d of %d pages)" % (maximum, pages))
    if result.complete and len(result) < total:
        partial("received %d distinct jobs; source advertised %d" % (len(result), total))
    return result
