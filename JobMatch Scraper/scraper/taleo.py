"""Public Oracle Taleo Enterprise career sections, modern and classic frontends.

Modern pages publish a portal ID and call /rest/jobboard/searchjobs. Classic
pages publish semantic field maps plus a paginated, form-encoded .ajax listing.
Both expose exact totals. Failed/repeated pages raise PartialScrapeError.
"""
import ast
import html
import json
import re
import time
from datetime import datetime
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup
from scraper.infosys import PartialScrapeError

MAX_PAGES = 500
_JS_ARRAY = r"\[(?:\s*'(?:\\.|[^'\\])*'\s*,?)*\s*\]"


def board_url(url):
    """Canonical public Enterprise section. Business Edition is a different API."""
    try:
        p = urlparse(url or "")
        if (p.scheme not in ("https", "http") or p.username or p.password
                or not re.fullmatch(r"[a-zA-Z0-9-]+\.taleo\.net", p.netloc)):
            return None
        m = re.fullmatch(r"/careersection/([^/]+)/(?:jobsearch|joblist|moresearch|jobdetail)\.ftl", p.path, re.I)
        if not m or not re.fullmatch(r"[a-zA-Z0-9_%+ .-]+", m[1]):
            return None
        portal = parse_qs(p.query).get("portal", [""])[0]
        base = "https://%s/careersection/%s/jobsearch.ftl" % (p.netloc.lower(), m[1])
        return base + ("?portal=" + portal if portal.isdigit() else "")
    except (TypeError, ValueError):
        return None


def _array(source, pattern):
    match = re.search(pattern + r"\s*(" + _JS_ARRAY + r")", source, re.S)
    if not match:
        raise ValueError("Taleo page is missing its public field data")
    result = ast.literal_eval(match[1])
    if not isinstance(result, list) or not all(isinstance(v, str) for v in result):
        raise ValueError("Invalid Taleo string array")
    return result


def _decode(value):
    return html.unescape(unquote(value or "")).removeprefix("!*!")


def _text(value):
    return BeautifulSoup(_decode(value), "html.parser").get_text(" ", strip=True)


def _date(value):
    for fmt in ("%b %d, %Y", "%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), fmt).date().isoformat()
        except ValueError:
            pass
    return ""


def _detail_url(board, identifier):
    return urljoin(board, "jobdetail.ftl") + "?" + urlencode({"job": identifier, "lang": "en"})


def _modern_page(data, board, page):
    if not isinstance(data, dict) or data.get("careerSectionUnAvailable"):
        raise ValueError("Taleo career section is unavailable")
    paging, jobs = data.get("pagingData"), data.get("requisitionList")
    if not isinstance(paging, dict) or not isinstance(jobs, list):
        raise ValueError("Taleo search response has no listing or pagination metadata")
    total, current = paging.get("totalCount"), paging.get("currentPageNo")
    if type(total) is not int or total < 0 or current != page:
        raise ValueError("Taleo pagination did not return the requested page")
    rows = []
    for job in jobs:
        if not isinstance(job, dict):
            raise ValueError("Invalid Taleo job row")
        columns, title_index = job.get("column"), job.get("linkedColumn")
        ident = str(job.get("contestNo") or job.get("jobId") or "").strip()
        if (not isinstance(columns, list) or type(title_index) is not int
                or not 0 <= title_index < len(columns) or not ident):
            raise ValueError("Taleo job is missing its title or identifier")
        title = _text(columns[title_index])
        locations = []
        for index in job.get("locationsColumns") or []:
            if type(index) is not int or not 0 <= index < len(columns):
                raise ValueError("Invalid Taleo location column")
            value = columns[index]
            decoded = json.loads(value) if isinstance(value, str) and value.startswith("[") else [value]
            if not isinstance(decoded, list) or not all(isinstance(v, str) for v in decoded):
                raise ValueError("Invalid Taleo locations")
            locations.extend(_text(v).replace("-", ", ") for v in decoded if v)
        if not title:
            raise ValueError("Empty Taleo job title")
        rows.append({"title": title, "url": _detail_url(board, ident),
                     "location": "; ".join(dict.fromkeys(locations)), "source": "taleo"})
    return rows, total


def _collect(rows, batch, seen, total):
    new = [row for row in batch if row["url"] not in seen]
    if batch and (len(new) != len(batch) or len({row["url"] for row in batch}) != len(batch)):
        raise PartialScrapeError("Taleo returned duplicate/repeated jobs while paging", rows)
    rows.extend(new)
    seen.update(row["url"] for row in new)
    if len(rows) > total or (not batch and len(rows) != total):
        raise PartialScrapeError("Taleo listing stopped before its advertised total", rows)
    return len(rows) == total


def _modern_scrape(board, response, portal):
    import scraper
    api = urljoin(board, "/careersection/rest/jobboard/searchjobs") + "?" + urlencode({"lang": "en", "portal": portal})
    headers = dict(scraper.HEADERS, Referer=response.url, tz="GMT+00:00", tzname="UTC")
    # Use the published title sort for stable paging. Some tenants' search indexes
    # overcount public rows; that remains a partial result, even on the last page.
    option = BeautifulSoup(response.text, "html.parser").select_one("#JOB_TITLE-sortfield[sortid]")
    body = {"multilineEnabled": True}
    if option is not None:
        body["sortingSelection"] = {"sortBySelectionParam": option["sortid"],
                                    "ascendingSortingOrder": "true"}
    rows, seen, expected = [], set(), None
    for page in range(1, MAX_PAGES + 1):
        try:
            body["pageNo"] = page
            r = scraper._safe_post(api, body, headers=headers, timeout=30)
            r.raise_for_status()
            batch, total = _modern_page(r.json(), board, page)
            if expected is not None and total != expected:
                raise ValueError("Taleo total changed while paging")
            expected = total
            if _collect(rows, batch, seen, total):
                return rows
            page_size = r.json()["pagingData"].get("pageSize")
            if type(page_size) is not int or page_size <= 0:
                raise ValueError("Invalid Taleo page size")
            if page * page_size >= total:
                raise PartialScrapeError("Taleo returned %d public rows of %d advertised postings" %
                                         (len(rows), total), rows)
        except PartialScrapeError:
            raise
        except Exception as exc:
            if rows:
                raise PartialScrapeError("Taleo pagination failed: %s" % type(exc).__name__, rows) from exc
            raise
        time.sleep(0.15)
    raise PartialScrapeError("Taleo page limit reached before advertised total", rows)


def _classic_rows(values, fields, board):
    if not fields or len(values) % len(fields):
        raise ValueError("Taleo classic row width does not match its field map")
    rows = []
    for start in range(0, len(values), len(fields)):
        job = dict(zip(fields, values[start:start + len(fields)]))
        title = _text(job.get("reqlistitem.title", ""))
        ident = _decode(job.get("reqlistitem.contestnumber") or job.get("reqlistitem.no", ""))
        if not title or not ident:
            raise ValueError("Taleo classic row is missing title or identifier")
        location = _text(job.get("reqlistitem.basiclocations", ""))
        row = {"title": title, "url": _detail_url(board, ident),
               "location": location.replace("-", ", "), "source": "taleo"}
        posted = _date(_text(job.get("reqlistitem.postingdate", "")))
        if posted:
            row["date_posted"] = posted
        rows.append(row)
    return rows


def _classic_setup(source, board):
    soup = BeautifulSoup(source, "html.parser")
    form = soup.find("form", id="ftlform")
    if not form:
        raise ValueError("No public Taleo search form")
    fields = _array(source, r"\blistRequisition\s*:\s*\{.*?_hlid\s*:")
    values = _array(source, r"api\.fillList\('requisitionListInterface',\s*'listRequisition',")
    data = {el["name"]: el.get("value", "") for el in form.select("input[name]")
            if el.get("type") not in ("checkbox", "radio", "submit", "button") or el.has_attr("checked")}
    for el in form.select("select[name]"):
        option = el.find("option", selected=True) or el.find("option")
        data[el["name"]] = option.get("value", "") if option else ""
    # JS fills controls by DOM id; their posted names can differ (sortBy/dropSortBy).
    for match in re.finditer(r"api\.fillForm\('([^']+)',\s*(" + _JS_ARRAY + r")\);", source):
        names = _array(source, re.escape(match[1]) + r"\s*:\s*\{\s*_ctls\s*:")
        for key, value in zip(names, ast.literal_eval(match[2])):
            el = soup.find(id=match[1] + "." + key)
            if el is not None and el.get("name"):
                data[el["name"]] = value
    total = int(data["listRequisition.nbElements"])
    if total < 0 or data.get("rlPager.currentPage") != "1":
        raise ValueError("Invalid Taleo classic initial pagination")
    endpoint = urljoin(board, form.get("action") or "jobsearch.ftl")
    if urlparse(endpoint).netloc != urlparse(board).netloc or not endpoint.endswith(".ftl"):
        raise ValueError("Taleo form target is not the same public section")
    return _classic_rows(values, fields, board), fields, data, total, endpoint[:-4] + ".ajax"


def _classic_scrape(board, response):
    import scraper
    first, fields, data, expected, endpoint = _classic_setup(response.text, response.url)
    rows, seen = [], set()
    if _collect(rows, first, seen, expected):
        return rows
    for page in range(2, MAX_PAGES + 1):
        try:
            data.update({"ftlinterfaceid": "requisitionListInterface", "ftlcompid": "rlPager",
                         "jsfCmdId": "rlPager", "ftlcompclass": "PagerComponent",
                         "ftlcallback": "ftlPager_processResponse", "ftlajaxid": "ftlx%d" % page,
                         "rlPager.currentPage": str(page)})
            r = scraper._safe_form_post(endpoint, data, timeout=30)
            r.raise_for_status()
            parts = r.text.split("!$!")
            if len(parts) != 4 or "ftlPager_processResponse" not in parts[0]:
                raise ValueError("Invalid Taleo classic page response")
            returned = parts[3].split("!|!")
            if len(returned) % 2:
                raise ValueError("Invalid Taleo pagination metadata")
            changes = dict(zip(returned[::2], returned[1::2]))
            if changes.get("ftlerrors") or int(changes.get("rlPager.currentPage", "0")) != page:
                raise ValueError("Taleo classic returned a failed or incorrect page")
            if int(changes.get("listRequisition.nbElements", "-1")) != expected:
                raise ValueError("Taleo total changed while paging")
            data.update(changes)
            batch = _classic_rows(parts[2].split("!|!") if parts[2] else [], fields, board)
            if _collect(rows, batch, seen, expected):
                return rows
        except PartialScrapeError:
            raise
        except Exception as exc:
            raise PartialScrapeError("Taleo classic pagination failed: %s" % type(exc).__name__, rows) from exc
        time.sleep(0.15)
    raise PartialScrapeError("Taleo classic page limit reached before advertised total", rows)


def scrape_taleo(url):
    """Read every public job in an Enterprise career section, never account pages."""
    import scraper
    board = board_url(url)
    if not board:
        raise ValueError("Unsupported Taleo Enterprise board URL")
    r = scraper._safe_get(board, timeout=30)
    r.raise_for_status()
    landing = board_url(r.url)
    if not landing or urlparse(landing).netloc != urlparse(board).netloc:
        raise ValueError("Taleo board did not return a public career section")
    portal = re.search(r"\bportalNo\s*:\s*['\"](\d+)['\"]", r.text)
    if portal:
        return _modern_scrape(landing, r, portal[1])
    return _classic_scrape(landing, r)



def probe_taleo(url):
    """Validate one public page and return its advertised count, without a full sweep."""
    import scraper
    board = board_url(url)
    if not board:
        raise ValueError("Unsupported Taleo Enterprise board URL")
    r = scraper._safe_get(board, timeout=25)
    r.raise_for_status()
    if not board_url(r.url) or urlparse(r.url).netloc != urlparse(board).netloc:
        raise ValueError("Taleo board redirect left its public section")
    portal = re.search(r"\bportalNo\s*:\s*['\"](\d+)['\"]", r.text)
    if portal:
        api = urljoin(board, "/careersection/rest/jobboard/searchjobs") + "?" + urlencode({"lang": "en", "portal": portal[1]})
        response = scraper._safe_post(api, {"pageNo": 1, "multilineEnabled": True}, timeout=25)
        response.raise_for_status()
        return _modern_page(response.json(), board, 1)[1]
    return _classic_setup(r.text, r.url)[3]


def description_html(source):
    """Decode Taleo's own field arrays into its description DOM, without JS.

    Generic HTML extraction sees empty spans and returns only site navigation.
    Preserve the label map, including facts such as 'Visa Sponsorship Provided: No'.
    """
    interface = "requisitionDescriptionInterface"
    try:
        labels = _array(source, interface + r"\s*:\s*\{.*?_hles\s*:")
        label_values = _array(source, r"api\.fillInterface\('" + interface + r"',")
        fields = _array(source, r"\bdescRequisition\s*:\s*\{.*?_hles\s*:")
        values = _array(source, r"api\.fillList\('" + interface + r"',\s*'descRequisition',")
        if len(labels) != len(label_values) or len(fields) != len(values):
            return ""
    except (ValueError, SyntaxError):
        return ""
    soup = BeautifulSoup(source, "html.parser")
    container = soup.find(id=interface + ".descRequisitionContainer")
    if container is None:
        return ""
    for key, value in list(zip(labels, label_values)) + list(zip(fields, values)):
        element = soup.find(id=interface + "." + key)
        if element is None or element.name not in ("span", "div", "a", "h1", "h2", "h3", "p"):
            continue
        decoded = _decode(value)
        element.clear()
        if value.startswith("!*!"):
            parsed = BeautifulSoup(decoded, "html.parser")
            for child in list(parsed.contents):
                element.append(child)
        else:
            element.append(decoded)
    for element in container.select('input,button,script,style,.hidden-audible'):
        element.decompose()
    if len(container.get_text(" ", strip=True)) < 100:
        return ""
    return str(container)
