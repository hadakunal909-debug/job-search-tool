"""Public ClearCompany HRMDirect employer catalogs and native posting descriptions.

The advertised count is location-specific: one requisition can have many public
req_loc URLs. Count every published location, never collapse them by requisition.
Unknown counts, malformed rows and interrupted pages remain partial failures.
"""
import re
from urllib.parse import parse_qs, urljoin, urlparse, urlencode

from bs4 import BeautifulSoup

from scraper.infosys import PartialScrapeError

MAX_PAGES = 100
MAX_ROWS = 20000
_HOST = re.compile(r"[a-z0-9][a-z0-9-]*\.hrmdirect\.com\Z", re.I)
_PATHS = {"/employment/index.php", "/employment/job-openings.php",
          "/employment/job-opening.php", "/employment/view.php"}
_COUNT = re.compile(r"\bThere are currently\s+([\d,]+)\s+openings?\b", re.I)
_ONE_COUNT = re.compile(r"\bThere is currently one opening\b", re.I)
_PAGE_KEYS = {"page", "offset", "start"}
_LIST_KEYS = _PAGE_KEYS | {"search", "sort", "jbsrc", "nohd"}
_SCOPE_KEY = re.compile(r"cust_sort[1-9]\d*\Z")
_TITLE_REMOTE_COUNTRY = re.compile(
    r"(?:^|[,\-]\s*)Remote\s*,\s*(Spain|United States|United Kingdom|Canada|India|"
    r"Germany|France|Ireland|Portugal|Netherlands|Australia|Singapore|Japan|Brazil|"
    r"Mexico|Poland|Philippines|South Africa)\s*$", re.I)


def _text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _title_location(title):
    # Native employers sometimes publish an explicit remote country only in the
    # title, leaving catalog/detail location cells empty. Preserve that statement;
    # an ordinary unknown location remains unknown and is never assigned to the US.
    match = _TITLE_REMOTE_COUNTRY.search(title)
    return "Remote, " + match[1] if match else ""


def _full_jd(text):
    normalized = " ".join((text or "").split()).casefold()
    return len(normalized) >= 250 and not normalized.startswith((
        "description unavailable", "job description unavailable", "this job is no longer available",
        "this position is no longer available", "job not found", "position not found"))


def board_parts(url):
    p = urlparse(str(url or ""))
    if (p.scheme not in ("https", "http") or p.username or p.password
            or not _HOST.fullmatch(p.netloc)
            or p.hostname.lower().split(".")[0] in ("apply", "www", "app")
            or p.path not in _PATHS):
        raise ValueError("Not a native HRMDirect employer career URL")
    return "https://" + p.hostname.lower()


def board_url(url):
    scopes = _scope_params(parse_qs(urlparse(url).query, keep_blank_values=True))
    return board_parts(url) + "/employment/index.php" + ("?" + urlencode(scopes) if scopes else "")


def _scope_params(query):
    scopes = {}
    for key, values in query.items():
        if not key.startswith("cust_sort"):
            continue
        if (not _SCOPE_KEY.fullmatch(key) or len(values) != 1
                or not re.fullmatch(r"[1-9]\d*", values[0])):
            raise ValueError("ClearCompany employer division selector is malformed")
        scopes[key] = values[0]
    return scopes


def _catalog_params(url):
    query = parse_qs(urlparse(url).query, keep_blank_values=True)
    scopes = _scope_params(query)
    if (not set(query).issubset(_LIST_KEYS | set(scopes))
            or any(len(values) != 1 for values in query.values())
            or any(not re.fullmatch(r"\d+", query[k][0]) for k in _PAGE_KEYS & set(query))):
        raise ValueError("ClearCompany catalog URL contains an unknown filter")
    return {key: values[0] for key, values in query.items()}


def _posting_url(url, origin=None):
    actual_origin = board_parts(url)
    if origin and actual_origin != origin:
        raise ValueError("ClearCompany posting belongs to another employer")
    p = urlparse(url)
    if p.path not in ("/employment/job-opening.php", "/employment/view.php"):
        raise ValueError("Not a ClearCompany posting URL")
    query = parse_qs(p.query, keep_blank_values=True)
    req = query.get("req", [])
    if len(req) != 1 or not re.fullmatch(r"[1-9]\d*", req[0]):
        raise ValueError("ClearCompany requisition ID is missing or malformed")
    params = {"req": req[0]}
    if "req_loc" in query:
        values = query["req_loc"]
        if len(values) != 1 or not re.fullmatch(r"[1-9]\d*", values[0]):
            raise ValueError("ClearCompany location ID is malformed")
        params["req_loc"] = values[0]
    if "location" in query:
        values = query["location"]
        if (len(values) != 1 or not re.fullmatch(r"[a-f0-9-]{8,64}", values[0], re.I)
                or "req_loc" in query):
            raise ValueError("ClearCompany legacy location ID is malformed")
        params["location"] = values[0]
    return actual_origin + p.path + "?" + urlencode(params)


def _get(url):
    from scraper import SESSION
    original, current, visited = urlparse(url), url, set()
    original_scope = _scope_params(parse_qs(original.query, keep_blank_values=True))
    for _ in range(4):
        if current in visited:
            raise ValueError("ClearCompany native redirect loop")
        visited.add(current)
        response = SESSION.get(current, timeout=25, allow_redirects=False,
                               headers={"User-Agent": "Mozilla/5.0 (compatible; JobMatchScraper/1.0)",
                                        "Accept": "text/html"})
        response.raise_for_status()
        if response.status_code == 200:
            return response
        if response.status_code not in (301, 302, 303, 307, 308):
            raise ValueError("ClearCompany returned HTTP %s" % response.status_code)
        candidate = urljoin(current, response.headers.get("Location", ""))
        redirected = urlparse(candidate)
        if (original.path not in ("/employment/index.php", "/employment/job-openings.php")
                or redirected.scheme != "https" or redirected.netloc.lower() != original.netloc.lower()
                or redirected.username or redirected.password or redirected.path != original.path
                or redirected.fragment):
            raise ValueError("ClearCompany redirect leaves the native employer catalog")
        params = _catalog_params(candidate)
        scope = _scope_params({key: [value] for key, value in params.items()})
        if original_scope and scope != original_scope:
            raise ValueError("ClearCompany redirect changes the employer division selector")
        if scope:
            original_scope = scope
        current = redirected.scheme + "://" + redirected.netloc.lower() + redirected.path
        if params:
            current += "?" + urlencode(params)
    raise ValueError("ClearCompany exceeded its native redirect safety cap")


def _body(response):
    # Legacy native pages omit charset headers. requests defaults to Latin-1,
    # which turns published smart quotes/dashes into C1 controls. HTML parsing
    # from original bytes detects the document encoding without losing characters.
    content = getattr(response, "content", None)
    return content if isinstance(content, bytes) else response.text


def _company(soup):
    title = _text(soup.title.get_text(" ", strip=True) if soup.title else "")
    match = re.search(r"(?:^| - )(?:Headquarters )?Careers At (.+)$", title, re.I)
    if not match or not _text(match[1]):
        raise ValueError("ClearCompany native employer name is missing")
    return _text(match[1])


def _index(html):
    soup = BeautifulSoup(html, "html.parser")
    company = _company(soup)
    body = soup.get_text(" ", strip=True)
    match = _COUNT.search(body)
    if not match and not _ONE_COUNT.search(body):
        raise ValueError("ClearCompany public advertised count is missing")
    total = int(match[1].replace(",", "")) if match else 1
    if total > MAX_ROWS:
        raise ValueError("ClearCompany exceeds the listing safety cap")
    return company, total


def board_name(url):
    return _index(_body(_get(board_url(url))))[0]


def _next_pages(soup, current_url, origin):
    pages = []
    scopes = _scope_params(parse_qs(urlparse(current_url).query, keep_blank_values=True))
    for a in soup.find_all("a", href=True):
        candidate = urljoin(current_url, a["href"])
        p = urlparse(candidate)
        query = parse_qs(p.query, keep_blank_values=True)
        label = _text(a.get_text(" ", strip=True)).lower()
        explicit_next = ("next" in a.get("rel", []) or label in ("next", "next >", ">", "›", "»")
                         or str(a.get("aria-label") or "").lower().startswith("next"))
        if not explicit_next and not (_PAGE_KEYS & set(query)):
            continue
        if (p.scheme != "https" or "https://" + p.netloc.lower() != origin
                or p.path != "/employment/job-openings.php"
                or not set(query).issubset(_LIST_KEYS | set(_scope_params(query)))
                or any(len(values) != 1 for values in query.values())
                or any(not re.fullmatch(r"\d+", query[k][0]) for k in _PAGE_KEYS & set(query))
                or not (_PAGE_KEYS & set(query))):
            raise ValueError("ClearCompany pagination leaves the unfiltered employer catalog")
        candidate_scopes = _scope_params(query)
        if candidate_scopes and candidate_scopes != scopes:
            raise ValueError("ClearCompany pagination changes the employer division selector")
        query.update({key: [value] for key, value in scopes.items()})
        canonical = origin + p.path + "?" + urlencode(sorted((k, v[0]) for k, v in query.items()))
        if canonical not in pages:
            pages.append(canonical)
    return pages


def _parse_page(html, current_url, origin, company, total):
    soup = BeautifulSoup(html, "html.parser")
    if _company(soup).casefold() != company.casefold():
        raise ValueError("ClearCompany catalog employer changed while paging")
    tables, cards = soup.select("table.reqResultTable"), soup.select("div.jobListItem")
    if not ((len(tables) == 1 and not cards) or (not tables and cards)):
        raise ValueError("ClearCompany native listing table is absent or ambiguous")
    rows, errors = [], []
    items = tables[0].find_all("tr") if tables else cards
    for item in items:
        if tables and not (item.has_attr("data-req-id") or item.select_one("td.posTitle")):
            continue
        try:
            anchors = item.select("td.posTitle a[href]" if tables else "h4.jobListTitle a[href]")
            if len(anchors) != 1 or not _text(anchors[0].get_text(" ", strip=True)):
                raise ValueError("ClearCompany listing title/link is missing")
            a = anchors[0]
            url = _posting_url(urljoin(current_url, a["href"]), origin)
            query = parse_qs(urlparse(url).query)
            req = item.get("data-req-id")
            if (tables and not req) or (req and req != query["req"][0]):
                raise ValueError("ClearCompany listing requisition IDs conflict")
            location_parts = []
            for key in ("cities", "state", "offices", "country", "countries"):
                cell = item.select_one("td." + key)
                value = _text(cell.get_text(" ", strip=True)) if cell else ""
                if value and value not in location_parts:
                    location_parts.append(value)
            title = _text(a.get_text(" ", strip=True))
            row = {"title": title, "url": url, "location": ", ".join(location_parts),
                   "source": "clearcompany"}
            if not row["location"] and _title_location(title):
                row["location"] = _title_location(title)
                row["location_evidence"] = "native_title_remote_country"
            rows.append(row)
        except (ValueError, TypeError, AttributeError) as exc:
            errors.append(str(exc))
    try:
        pages = _next_pages(soup, current_url, origin)
    except ValueError as exc:
        pages = []
        errors.append(str(exc))
    if total and not rows and not errors:
        errors.append("ClearCompany returned an empty page before the advertised total")
    return rows, pages, errors


def scrape_clearcompany(url):
    """Read every public posting/location in the native default division scope."""
    rows, seen, errors = [], set(), []
    try:
        origin = board_parts(url)
        index_url = board_url(url)
        index_response = _get(index_url)
        index_url = getattr(index_response, "url", None) or index_url
        scopes = _scope_params(parse_qs(urlparse(index_url).query, keep_blank_values=True))
        company, total = _index(_body(index_response))
        pending = [origin + "/employment/job-openings.php?" + urlencode({"search": "true", **scopes})]
        visited = set()
        while pending:
            if len(visited) >= MAX_PAGES:
                raise ValueError("ClearCompany exceeded its page safety cap")
            page = pending.pop(0)
            if page in visited:
                continue
            visited.add(page)
            page_response = _get(page)
            resolved_page = getattr(page_response, "url", None) or page
            if _scope_params(parse_qs(urlparse(resolved_page).query, keep_blank_values=True)) != scopes:
                raise ValueError("ClearCompany listing changes the employer division selector")
            batch, next_pages, page_errors = _parse_page(_body(page_response), resolved_page, origin, company, total)
            errors.extend(page_errors)
            for row in batch:
                if row["url"] in seen:
                    errors.append("ClearCompany repeated a posting/location while paging")
                    continue
                seen.add(row["url"])
                rows.append(row)
            if len(rows) > total:
                errors.append("ClearCompany returned more rows than advertised")
            pending.extend(p for p in next_pages if p not in visited and p not in pending)
        final_index = _get(index_url)
        if _scope_params(parse_qs(urlparse(getattr(final_index, "url", None) or index_url).query,
                                 keep_blank_values=True)) != scopes:
            errors.append("ClearCompany advertised employer division selector changed while paging")
        current_company, current_total = _index(_body(final_index))
        if current_company.casefold() != company.casefold() or current_total != total:
            errors.append("ClearCompany advertised count or employer changed while paging")
        if len(rows) != total:
            errors.append("ClearCompany read %d of %d advertised posting locations" % (len(rows), total))
        if errors:
            raise ValueError("; ".join(dict.fromkeys(errors)))
        return rows
    except Exception as exc:
        raise PartialScrapeError("ClearCompany listing incomplete: " + str(exc)[:300], rows) from exc


def detail_job(url, expected_title=""):
    """Return the full native JD only when its exact requisition and title match."""
    canonical = _posting_url(url)
    req = parse_qs(urlparse(canonical).query)["req"][0]
    soup = BeautifulSoup(_body(_get(canonical)), "html.parser")
    company = _company(soup)
    blocks = soup.select("div.reqResult")
    if not blocks and len(soup.select("table.viewFields")) == 1:
        # Some native legacy templates place these unique fields directly in the
        # document. Exact title, full text and application requisition checks below
        # still establish posting ownership; ambiguous documents remain failures.
        block = soup
    elif len(blocks) == 1:
        block = blocks[0]
    else:
        raise ValueError("ClearCompany native posting is absent or ambiguous")
    descriptions = block.select("div.jobDesc")
    # Full native descriptions can contain section headings and even repeat the
    # posting title. Only headings outside the description identify the posting.
    headings = [heading for heading in block.select("h2")
                if not any(description in heading.parents for description in descriptions)]
    if len(headings) != 1 or len(descriptions) != 1:
        raise ValueError("ClearCompany posting title/full description is missing")
    title = _text(headings[0].get_text(" ", strip=True))
    if not title or (expected_title and title.casefold() != _text(expected_title).casefold()):
        raise ValueError("ClearCompany returned another posting title")
    application_ids = []
    for a in block.select("a.applyOnline[href]"):
        p = urlparse(a["href"])
        if (p.scheme != "https" or p.netloc.lower() != "apply.hrmdirect.com"
                or p.path != "/resumedirect/ApplyOnline/Apply.aspx"):
            raise ValueError("ClearCompany posting application ownership is invalid")
        ids = parse_qs(p.query, keep_blank_values=True).get("req_id", [])
        if len(ids) != 1 or ids[0] != req:
            raise ValueError("ClearCompany returned another requisition application link")
        application_ids.extend(ids)
    if not application_ids or any(value != req for value in application_ids):
        raise ValueError("ClearCompany returned another requisition description")
    jd = descriptions[0].get_text("\n", strip=True).strip()
    if not _full_jd(jd):
        raise ValueError("ClearCompany full description is missing, unavailable or too short")
    location, countries = "", []
    for tr in block.select("table.viewFields tr"):
        label, value = tr.select_one("td.viewFieldName"), tr.select_one("td.viewFieldValue")
        if label and value:
            key = _text(label.get_text(" ", strip=True)).rstrip(":").lower()
            native_value = _text(value.get_text(" ", strip=True))
            if key == "location":
                location = native_value
            elif key == "country" and native_value and native_value not in countries:
                countries.append(native_value)
    for country in countries:
        if country.casefold() not in location.casefold():
            location = (location + ", " if location else "") + country
    job = {"title": title, "url": canonical, "location": location,
           "source": "clearcompany", "company": company, "jd": jd}
    if not location and _title_location(title):
        job["location"] = _title_location(title)
        job["location_evidence"] = "native_title_remote_country"
    return job


def detail_jd(url, expected_title=""):
    # The native page exposes no publisher date; never replace it with today's date.
    return detail_job(url, expected_title)["jd"], ""
