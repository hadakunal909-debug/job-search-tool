"""SilkRoad's public jobs.silkroad.com career portals.

The server renders job IDs, employer identity and numbered pagination. Collect
every advertised page, reject scope/pagination drift, and keep partial rows on
failure. Public posting pages carry full descriptions and an exact apply ID.
Candidate profile, application and authentication endpoints are never fetched.
"""
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup
from scraper.infosys import PartialScrapeError

MAX_PAGE_SIZE = 500
MAX_PAGES = 500
_SEGMENT = r"[A-Za-z0-9_-]+"
_BASE_PATH = r"/(" + _SEGMENT + r")/(" + _SEGMENT + r")"
FIRST_COMMAND_BOARD = "https://jobs.silkroad.com/FirstCommand/Careers"
FIRST_COMMAND_CAREERS = "https://www.firstcommand.com/get-started/careers/"
FIRST_COMMAND_FEED = "https://firstcommand-careers.silkroad.com/eprise/main/SiteGen/DynamicContent/Snippets/FirstCommand_Custom_Feed/FirstCommand_Custom_Feed.xml"
FIRST_COMMAND_OWNER = "First Command"


def board_url(url):
    """Canonical unfiltered portal, also from a public posting/apply/profile link."""
    try:
        parsed = urlsplit(url or "")
        if _first_command_posting_id(url):
            return FIRST_COMMAND_BOARD
        if url == FIRST_COMMAND_FEED:
            return FIRST_COMMAND_BOARD
        if (parsed.scheme != "https" or parsed.netloc.lower() != "jobs.silkroad.com"
                or parsed.username or parsed.password or parsed.fragment):
            return None
        matched = re.fullmatch(_BASE_PATH + r"(?:/(?i:jobs)/[1-9][0-9]*|/Profile|/Apply/MultiForm/[1-9][0-9]*)?/?", parsed.path)
        if not matched:
            return None
        return "https://jobs.silkroad.com/" + matched[1] + "/" + matched[2]
    except (TypeError, ValueError):
        return None


def _text(element):
    return element.get_text(" ", strip=True) if element is not None else ""


def _same_text(left, right):
    return " ".join((left or "").split()).casefold() == " ".join((right or "").split()).casefold()


def _full_jd(text):
    normalized = " ".join((text or "").split()).casefold()
    return len(normalized) >= 250 and not normalized.startswith((
        "description unavailable", "job description unavailable", "this job is no longer available",
        "this position is no longer available", "job not found", "position not found"))


def _first_command_posting_id(url):
    try:
        parsed = urlsplit(url or "")
        query = parse_qs(parsed.query, keep_blank_values=True)
        identifier = query.get("jobid", [""])[0]
        if (parsed.scheme == "https" and parsed.netloc == "firstcommand.silkroad.com"
                and parsed.path in ("/epostings", "/epostings/") and not parsed.fragment
                and re.fullmatch(r"[1-9][0-9]*", identifier)
                and query == {"fuseaction": ["app.jobInfo"], "version": ["3"], "jobid": [identifier]}):
            return identifier
    except (TypeError, ValueError):
        pass
    return None


def _parse_first_command_xml(xml):
    if len(xml.encode("utf-8")) > 20_000_000 or re.search(r"<!DOCTYPE|<!ENTITY", xml, re.I):
        raise ValueError("First Command feed exceeds its bounded public XML schema")
    root = ET.fromstring(xml)
    if (root.tag != "jobs" or root.attrib or (root.text or "").strip()
            or any(child.tag != "job" or (child.tail or "").strip() for child in root) or len(root) > 10000):
        raise ValueError("First Command feed has an unexpected catalog schema")
    rows, seen = [], set()
    for job in root:
        def field(name, required=True):
            elements = job.findall(name)
            if len(elements) != 1 and (required or len(elements) > 1):
                raise ValueError("First Command posting has missing or repeated public fields")
            return (elements[0].text or "").strip() if elements else ""
        identifier, url, title = field("jobId"), field("applyUrl"), field("title")
        if _first_command_posting_id(url) != identifier or not title or identifier in seen:
            raise ValueError("First Command posting has an invalid or repeated ID, URL or title")
        location = job.findall("jobLocation")
        if len(location) != 1:
            raise ValueError("First Command posting has no exact public location")
        components = []
        for name in ("municipality", "region", "countryCode"):
            elements = location[0].findall(name)
            if len(elements) != 1:
                raise ValueError("First Command posting has an incomplete location schema")
            components.append((elements[0].text or "").strip())
        if not components[0] or not re.fullmatch(r"[A-Z]{2}", components[2]):
            raise ValueError("First Command posting has an invalid public location")
        main = _text(BeautifulSoup(field("jobDescription"), "html.parser"))
        if not _full_jd(main):
            raise ValueError("First Command posting has no full public description")
        sections = [main]
        for name in ("requiredSkills", "requiredExperience"):
            section = _text(BeautifulSoup(field(name, False), "html.parser"))
            if section:
                sections.append(section)
        date = datetime.fromisoformat(field("postingDate").replace("Z", "+00:00"))
        if date.tzinfo is None:
            raise ValueError("First Command posting date has no published timezone")
        rows.append({"title": title, "location": ", ".join(filter(None, components)), "url": url,
                     "jd": "\n\n".join(sections), "found_date": date.date().isoformat(),
                     "source": "silkroad", "identity_text": FIRST_COMMAND_OWNER})
        seen.add(identifier)
    return rows


def _first_command_catalog():
    """Exact public redirect/feed contract observed on the official career UI.

    The XML has no employer field; identity comes from this fixed, independently
    verified corporate frontend contract. Do not infer other XML feed tenants.
    """
    import scraper
    response = scraper.SESSION.get(FIRST_COMMAND_BOARD, headers=scraper.HEADERS, timeout=30, allow_redirects=False)
    _check_response_scope(response, FIRST_COMMAND_BOARD)
    if response.status_code == 200:
        return response
    if response.status_code not in (301, 302) or response.headers.get("Location") != FIRST_COMMAND_CAREERS:
        raise ValueError("First Command no longer publishes its verified public career redirect")
    feed = scraper.SESSION.get(FIRST_COMMAND_FEED, headers=scraper.HEADERS, timeout=30, allow_redirects=False)
    if feed.status_code != 200 or feed.url != FIRST_COMMAND_FEED:
        raise ValueError("First Command public XML feed returned an unexpected status or scope")
    return _parse_first_command_xml(feed.text)


def _embedded(url):
    return parse_qs(urlsplit(url).query).get("embedded") == ["true"]


def _public_query(embedded=False, page=None):
    parts = ["embedded=true"] if embedded else []
    if page is not None:
        parts.append("page=" + str(page))
    return "?" + "&".join(parts) if parts else ""


def _check_response_scope(response, url):
    requested, actual = urlsplit(url), urlsplit(response.url)
    if (actual.scheme != "https" or actual.netloc.lower() != "jobs.silkroad.com"
            or actual.username or actual.password or actual.fragment
            or actual.path.rstrip("/") != requested.path.rstrip("/")
            or parse_qs(actual.query) != parse_qs(requested.query)):
        raise ValueError("SilkRoad public page changed its requested scope")


def _get(url):
    import scraper
    board = board_url(url)
    requested = urlsplit(url)
    if (not board or not re.fullmatch(re.escape(urlsplit(board).path) + r"(?:/(?i:jobs)/[1-9][0-9]*)?/?", requested.path)
            or set(parse_qs(requested.query)) - {"embedded", "page"}):
        raise ValueError("Unsupported SilkRoad public page scope")
    response = scraper.SESSION.get(url, headers=scraper.HEADERS, timeout=30, allow_redirects=False)
    _check_response_scope(response, url)
    if response.status_code in (301, 302) and not _embedded(url):
        # The ATS itself publishes the iframe route inside the corporate-site
        # redirect. Fetch only that exact same-tenant public path, never the
        # external site or an account/application URL.
        location = urlsplit(response.headers.get("Location", ""))
        roots = parse_qs(location.query).get("cxembeddedroot", [])
        root = urlsplit(roots[0]) if len(roots) == 1 else None
        expected_query = parse_qs(requested.query)
        expected_query["embedded"] = ["true"]
        if (location.scheme == "https" and root is not None
                and not root.scheme and not root.netloc and not root.fragment
                and root.path.rstrip("/") == requested.path.rstrip("/")
                and parse_qs(root.query) == expected_query):
            url = "https://jobs.silkroad.com" + root.path + "?" + root.query
            requested = urlsplit(url)
            response = scraper.SESSION.get(url, headers=scraper.HEADERS, timeout=30, allow_redirects=False)
            _check_response_scope(response, url)
    if response.status_code != 200:
        raise ValueError("SilkRoad public page returned HTTP %s" % response.status_code)
    return response


def _identity(soup):
    name = _text(soup.select_one("#Jobs_JobsList_CompanyName"))
    if not name:
        descriptions = soup.select('meta[property="og:description"]')
        logos = soup.select('meta[property="og:image:alt"]')
        description = descriptions[0].get("content", "") if len(descriptions) == 1 else ""
        logo = logos[0].get("content", "") if len(logos) == 1 else ""
        logo_prefix = next((prefix for prefix in ("Logo for ", "OGP Image for ") if logo.startswith(prefix)), None)
        if description.startswith("Find a career with ") and logo_prefix:
            candidate = description.removeprefix("Find a career with ").strip()
            if _same_text(candidate, logo.removeprefix(logo_prefix)):
                name = candidate
    if not name or len(name) > 300:
        raise ValueError("SilkRoad page has no current employer identity")
    return name


def _complete_grouped_catalog(soup, panels, requested_page):
    """Unpaged category layout: every public category and card must be covered."""
    form = soup.select_one("#Jobs_JobSearch_SearchForm")
    categories = soup.select_one("#Jobs_JobSearch_CategoriesSelect")
    sentinels = soup.select("#Jobs_PagedJobList_OpenSubmission")
    if (requested_page != 1 or not panels or form is None or categories is None
            or len(sentinels) != 1
            or soup.select_one("#Jobs_PagedJobList_NextLink, #Jobs_PagedJobList_PrevPageLink") is not None):
        return False
    search = form.select_one('input[name="SearchString"]')
    if search is None or search.get("value", "").strip():
        return False
    for element in form.select('input[name="StartDate"], input[name="EndDate"]'):
        if element.get("value", "").strip():
            return False
    for select in form.select("select"):
        options = select.find_all("option")
        if (not options or options[0].get("value", _text(options[0])).strip()
                or any(option.has_attr("selected") and option.get("value", _text(option)).strip() for option in options)):
            return False
    options = [(option.get("value", ""), _text(option)) for option in categories.find_all("option") if option.get("value")]
    expected = dict(options)
    if (not expected or len(expected) != len(options)
            or any(not re.fullmatch(r"[1-9][0-9]*", identifier) or not name for identifier, name in options)):
        return False
    headings = soup.select('h2[id^="Jobs_PagedJobList_Category__"]')
    if len(headings) != len(expected):
        return False
    seen_categories, covered, sections = set(), set(), []
    for heading in headings:
        identifier = heading["id"].removeprefix("Jobs_PagedJobList_Category__")
        section = heading.parent
        if (identifier not in expected or identifier in seen_categories or not _same_text(_text(heading), expected[identifier])
                or section.name != "article" or "sr-search__section" not in section.get("class", [])):
            return False
        cards = section.select("a.sr-panel, a[id^='Jobs_PagedJobList_Job-']")
        if not cards or covered.intersection(map(id, cards)):
            return False
        covered.update(map(id, cards))
        seen_categories.add(identifier)
        sections.append(section)
    container = sentinels[0].parent
    if (container.name != "section" or "sr-wrapper" not in container.get("class", [])
            or any(section.parent is not container for section in sections)):
        return False
    children = container.find_all(recursive=False)
    return (len(children) == len(sections) + 1 and children[-1] is sentinels[0]
            and list(map(id, children[:-1])) == list(map(id, sections))
            and covered == set(map(id, panels)) and seen_categories == set(expected))


def _parse_page(html, board, requested_page, embedded=False):
    soup = BeautifulSoup(html, "html.parser")
    owner = _identity(soup)
    # Count every rendered panel, including a malformed/missing posting ID. Do
    # not silently omit a card and infer a smaller first-page size from it.
    panels = soup.select("a.sr-panel, a[id^='Jobs_PagedJobList_Job-']")
    pager = soup.select_one("#Jobs_PagedJobList_CurrentPageText")
    if pager is None:
        if (requested_page == 1 and not panels
                and soup.select_one("#Jobs_JobSearchResults_NoResults_PageHeading") is not None
                and soup.select_one("#Jobs_JobSearchResults_NoResults_PageText") is not None):
            return [], 0, owner
        if not _complete_grouped_catalog(soup, panels, requested_page):
            raise ValueError("SilkRoad page has no public pagination metadata or complete category layout")
        current, total_pages = 1, 1
    else:
        match = re.fullmatch(r"Page\s+([0-9]+)\s+of\s+([0-9]+)", _text(pager))
        if not match:
            raise ValueError("SilkRoad page has invalid pagination metadata")
        current, total_pages = int(match[1]), int(match[2])
    if current != requested_page or not 1 <= current <= total_pages:
        raise ValueError("SilkRoad did not return the requested page")
    if not panels or len(panels) > MAX_PAGE_SIZE:
        raise ValueError("SilkRoad page is empty or exceeds its bounded page size")
    if pager is not None:
        next_button = soup.select_one("#Jobs_PagedJobList_NextLink")
        if next_button is None:
            raise ValueError("SilkRoad page is missing its public next-page state")
        if current == total_pages:
            if not next_button.has_attr("disabled"):
                raise ValueError("SilkRoad final page still advertises a next page")
        else:
            next_url = urljoin(board, next_button.get("data-href", ""))
            next_parsed = urlsplit(next_url)
            if (next_button.has_attr("disabled") or board_url(next_url) != board
                    or next_parsed.path.rstrip("/") != urlsplit(board).path
                    or parse_qs(next_parsed.query) != parse_qs(_public_query(embedded, current + 1)[1:])):
                raise ValueError("SilkRoad next-page link changes scope or skips a page")
    rows = []
    for panel in panels:
        panel_id = panel.get("id", "")
        if not panel_id.startswith("Jobs_PagedJobList_Job-"):
            raise ValueError("SilkRoad rendered a job card without its public posting ID")
        identifier = panel_id.removeprefix("Jobs_PagedJobList_Job-")
        if not re.fullmatch(r"[1-9][0-9]*", identifier):
            raise ValueError("SilkRoad listing has an invalid public posting ID")
        url = urljoin(board, panel.get("href", ""))
        expected = board + "/jobs/" + identifier + _public_query(embedded)
        parsed = urlsplit(url)
        if (board_url(url) != board or parsed.path.rstrip("/") != urlsplit(board).path + "/jobs/" + identifier
                or parse_qs(parsed.query) != parse_qs(_public_query(embedded)[1:])):
            raise ValueError("SilkRoad posting URL does not match its tenant and ID")
        title = _text(panel.select_one("#Jobs_PagedJobList_JobTitle-" + identifier))
        location_element = panel.select_one("#Jobs_PagedJobList_JobLocation-" + identifier)
        location = _text(location_element)
        if not title or location_element is None:
            raise ValueError("SilkRoad posting is missing its public title or location")
        rows.append({"title": title, "location": location, "url": expected,
                     "source": "silkroad", "identity_text": owner})
    return rows, total_pages, owner


def scrape_silkroad(url):
    board = board_url(url)
    if not board:
        raise ValueError("Unsupported SilkRoad public career URL")
    first_response = None
    if board == FIRST_COMMAND_BOARD and not _embedded(url):
        catalog = _first_command_catalog()
        if isinstance(catalog, list):
            return catalog
        first_response = catalog
    rows, seen, expected_pages, expected_owner, expected_size = [], set(), None, None, None
    page = 1
    embedded = _embedded(url)
    while True:
        try:
            response = first_response if page == 1 and first_response is not None else _get(board + _public_query(embedded, page if page > 1 else None))
            embedded = _embedded(response.url)
            batch, total_pages, owner = _parse_page(response.text, board, page, embedded)
            if expected_pages is not None and total_pages != expected_pages:
                raise ValueError("SilkRoad advertised page count changed while paging")
            if expected_owner is not None and not _same_text(owner, expected_owner):
                raise ValueError("SilkRoad employer identity changed while paging")
            if expected_size is not None and (len(batch) > expected_size
                                             or page < total_pages and len(batch) != expected_size):
                raise ValueError("SilkRoad rendered page size changed while paging")
            batch_urls = [row["url"] for row in batch]
            if len(set(batch_urls)) != len(batch_urls) or seen.intersection(batch_urls):
                raise ValueError("SilkRoad returned duplicate or repeated public postings")
            for row in batch:
                if not row["location"]:
                    detail = _get(row["url"])
                    identifier = urlsplit(row["url"]).path.rstrip("/").split("/")[-1]
                    info = _detail_data(BeautifulSoup(detail.text, "html.parser"), board, identifier,
                                        _embedded(detail.url), row["title"], owner)
                    if not info.get("location"):
                        raise ValueError("SilkRoad blank list location has no validated public detail location")
                    row["location"], row["jd"] = info["location"], info["jd"]
            expected_pages, expected_owner = total_pages, owner
            if expected_size is None:
                expected_size = len(batch)
            rows.extend(batch)
            seen.update(batch_urls)
            if total_pages == 0 or page == total_pages:
                return rows
            if page >= MAX_PAGES:
                raise ValueError("SilkRoad advertised pages exceed the bounded page limit")
            page += 1
            time.sleep(0.15)
        except Exception as error:
            if rows:
                raise PartialScrapeError("SilkRoad incomplete public listing: %s" % error, rows) from error
            raise


def identity_text(url):
    """Current structured portal owner for board_display_name's identity check."""
    board = board_url(url)
    if not board:
        return ""
    if board == FIRST_COMMAND_BOARD and not _embedded(url):
        catalog = _first_command_catalog()
        if isinstance(catalog, list):
            return FIRST_COMMAND_OWNER
        return _parse_page(catalog.text, board, 1)[2]
    response = _get(board + _public_query(_embedded(url)))
    _, _, owner = _parse_page(response.text, board, 1, _embedded(response.url))
    return owner


def detail_jd(url, expected_title=None, expected_company=None):
    """Return (full public description, date) after exact tenant/posting checks.

    The observed template does not publish a posting date, so omit it. A mismatch
    returns a blank description and remains eligible for the ordinary JD retry.
    """
    identifier = _first_command_posting_id(url)
    if identifier:
        catalog = _first_command_catalog()
        if not isinstance(catalog, list):
            return "", ""
        for row in catalog:
            if (_first_command_posting_id(row["url"]) == identifier and (expected_title is None or _same_text(row["title"], expected_title))
                    and (expected_company is None or _same_text(row["identity_text"], expected_company))):
                return row["jd"], row["found_date"]
        return "", ""
    board = board_url(url)
    if not board:
        return "", ""
    requested = urlsplit(url)
    matched = re.fullmatch(re.escape(urlsplit(board).path) + r"/(?i:jobs)/([1-9][0-9]*)/?", requested.path)
    if not matched:
        return "", ""
    # Preserve the public posting path's case: current corporate links use both
    # /jobs/ and /Jobs/, and the response must match the path we requested.
    public_url = "https://jobs.silkroad.com" + requested.path.rstrip("/") + _public_query(_embedded(url))
    response = _get(public_url)
    soup = BeautifulSoup(response.text, "html.parser")
    info = _detail_data(soup, board, matched[1], _embedded(response.url), expected_title, expected_company)
    return info.get("jd", ""), ""


def _detail_data(soup, board, identifier, embedded=False, expected_title=None, expected_company=None):
    owner = _identity(soup)
    title = _text(soup.select_one("#Jobs_JobDetail_TitleText"))
    main = soup.select_one("#Jobs_JobDetail_JobDescriptionText, #ConfigurablePageDetail__JobDescription")
    description = _text(main)
    if not _full_jd(description):
        return {}
    if description:
        for selector in ("#Jobs_JobDetail_JobRequiredSkillsText, #ConfigurablePageDetail__RequiredSkills",
                         "#Jobs_JobDetail_JobRequiredExperienceText, #ConfigurablePageDetail__RequiredExperience"):
            element = soup.select_one(selector)
            if element is not None and main not in element.parents and element not in main.descendants:
                description += "\n\n" + _text(element)
    apply_link = soup.select_one("#Jobs_JobDetail_Multiform_ApplyLink")
    expected_apply = board + "/Apply/MultiForm/" + identifier + _public_query(embedded)
    if (not title or not description or apply_link is None
            or urljoin(board, apply_link.get("href", "")).rstrip("/") != expected_apply
            or expected_title is not None and not _same_text(title, expected_title)
            or expected_company is not None and not _same_text(owner, expected_company)):
        return {}
    location = _text(soup.select_one("#Jobs_JobDetail_LocationText"))
    if not location:
        block = soup.select_one("#ConfigurablePageDetail__DisplayLocation")
        if block is not None:
            labels, values = block.find_all("h2", recursive=False), block.find_all("div", recursive=False)
            if len(labels) == len(values) == 1 and _same_text(_text(labels[0]), "Job Location"):
                location = _text(values[0])
    return {"jd": description, "location": location, "identity_text": owner, "title": title}
