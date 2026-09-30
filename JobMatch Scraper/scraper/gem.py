"""Gem's public employer job boards and the GraphQL API used by their own UI.

The public JobBoardList query returns the whole jobPostings array, with no
cursor, limit or offset. Read it once, retain every row, and fail visibly on a
partial GraphQL response rather than treating missing postings as closed.
"""
import re
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

from bs4 import BeautifulSoup
from scraper.infosys import PartialScrapeError

HOST = "jobs.gem.com"
API = "https://jobs.gem.com/api/public/graphql"
_SLUG = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_POST_ID = re.compile(r"^[A-Za-z0-9_=-]{1,200}$")

LIST_QUERY = """query JobBoardList($boardId: String!) {
  oatsExternalJobPostings(boardId: $boardId) {
    jobPostings {
      extId title descriptionHtml firstPublishedTsSec compensationHtml
      jobPostSectionHtml { introHtml outroHtml }
      locations { name city isoCountry isRemote }
      job { locationType employmentType }
    }
  }
  jobBoardExternal(vanityUrlPath: $boardId) { teamDisplayName pageTitle }
}"""

DETAIL_QUERY = """query ExternalJobPosting($boardId: String!, $extId: String!) {
  oatsExternalJobPosting(boardId: $boardId, extId: $extId) {
    extId title descriptionHtml firstPublishedTsSec compensationHtml
    jobPostSectionHtml { introHtml outroHtml }
  }
}"""


def board_parts(url):
    p = urlparse(url)
    parts = p.path.strip("/").split("/")
    if (p.scheme not in ("http", "https") or p.hostname != HOST or p.username
            or p.password or p.port not in (None, 80, 443)
            or not parts or not _SLUG.fullmatch(parts[0])):
        raise ValueError("Not a Gem employer job board")
    if len(parts) > 3 or (len(parts) == 3 and parts[2] != "application"):
        raise ValueError("Not a Gem board or posting URL")
    ext_id = parts[1] if len(parts) > 1 else ""
    if ext_id and not _POST_ID.fullmatch(ext_id):
        raise ValueError("Gem posting ID is invalid")
    return parts[0], ext_id


def board_url(url):
    return "https://" + HOST + "/" + board_parts(url)[0]


def owns_url(board, posting):
    try:
        return board_parts(board)[0] == board_parts(posting)[0]
    except ValueError:
        return False


def request_json(query, variables, operation):
    import scraper
    response = scraper._safe_post(API, {"query": query, "variables": variables,
                                       "operationName": operation}, timeout=25)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Gem returned a non-object GraphQL response")
    return payload


def _date(value):
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Gem publication timestamp is invalid")
    try:
        return datetime.fromtimestamp(value, timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("Gem publication timestamp is invalid") from exc


def _description(post):
    sections = post.get("jobPostSectionHtml") or {}
    if not isinstance(sections, dict):
        raise ValueError("Gem description sections are invalid")
    blocks = [sections.get("introHtml"), post.get("descriptionHtml"),
              post.get("compensationHtml"), sections.get("outroHtml")]
    if any(value is not None and not isinstance(value, str) for value in blocks):
        raise ValueError("Gem description HTML is invalid")
    soup = BeautifulSoup("\n".join(value for value in blocks if value), "html.parser")
    for tag in soup.select("script, style, form"):
        tag.decompose()
    return soup.get_text("\n", strip=True)


def parse_post(post, url, company=""):
    if not isinstance(post, dict):
        raise ValueError("Gem posting is not an object")
    ext_id, title = post.get("extId"), post.get("title")
    if (not isinstance(ext_id, str) or not _POST_ID.fullmatch(ext_id)
            or not isinstance(title, str) or not title.strip()):
        raise ValueError("Gem posting lacks its public ID or title")
    locations = post.get("locations") or []
    if not isinstance(locations, list):
        raise ValueError("Gem locations are invalid")
    labels = []
    for loc in locations:
        if not isinstance(loc, dict):
            raise ValueError("Gem location is not an object")
        name, country = loc.get("name") or loc.get("city") or "", loc.get("isoCountry") or ""
        if not isinstance(name, str) or not isinstance(country, str):
            raise ValueError("Gem location label is invalid")
        # Keep explicit country evidence, even when the UI's label is just a city.
        name, country = name.strip(), country.strip()
        suffix = country if country and not re.search(r"\b" + re.escape(country) + r"\b", name, re.I) else ""
        label = ", ".join(value for value in (name, suffix) if value)
        if label and label not in labels:
            labels.append(label)
    row = {"title": title.strip(), "url": board_url(url) + "/" + quote(ext_id, safe="_-="),
           "location": " | ".join(labels), "source": "gem", "jd": _description(post)}
    if company:
        row["company"] = company
    posted = _date(post.get("firstPublishedTsSec"))
    if posted:
        row["found_date"] = posted
    return row


def parse_listing(payload, url):
    board_url(url)
    data = payload.get("data") if isinstance(payload, dict) else None
    listing = data.get("oatsExternalJobPostings") if isinstance(data, dict) else None
    board = data.get("jobBoardExternal") if isinstance(data, dict) else None
    posts = listing.get("jobPostings") if isinstance(listing, dict) else None
    company = board.get("teamDisplayName") if isinstance(board, dict) else None
    if not isinstance(posts, list) or not isinstance(company, str) or not company.strip():
        raise ValueError("Gem listing or employer identity is missing")
    rows, seen = [], {}
    try:
        for post in posts:
            row = parse_post(post, url, company.strip())
            prev = seen.get(row["url"])
            if prev is not None:
                if prev != row:
                    raise ValueError("Gem repeated a posting ID with conflicting content")
                continue
            seen[row["url"]] = row
            rows.append(row)
        if payload.get("errors"):
            raise ValueError("Gem returned GraphQL errors with partial data")
        # There is no pagination in the current public UI contract. A future
        # cursor or advertised total must not silently become a partial scrape.
        page_info = listing.get("pageInfo") or {}
        if (not isinstance(page_info, dict) or page_info.get("hasNextPage")
                or listing.get("nextCursor") or listing.get("nextPage")):
            raise ValueError("Gem added pagination; the listing needs review")
        total = listing.get("totalCount")
        if total is not None and (isinstance(total, bool) or not isinstance(total, int)
                                  or total != len(rows)):
            raise ValueError("Gem listing differs from its advertised total")
    except Exception as exc:
        raise PartialScrapeError("Gem listing incomplete: " + str(exc)[:160], rows) from exc
    return rows


def scrape_gem(url):
    slug = board_parts(url)[0]
    return parse_listing(request_json(LIST_QUERY, {"boardId": slug}, "JobBoardList"), url)


def identity_text(url):
    payload = request_json(LIST_QUERY, {"boardId": board_parts(url)[0]}, "JobBoardList")
    parse_listing(payload, url)
    return payload["data"]["jobBoardExternal"]["teamDisplayName"].strip()


def detail_jd(url):
    slug, ext_id = board_parts(url)
    if not ext_id:
        return "", ""
    payload = request_json(DETAIL_QUERY, {"boardId": slug, "extId": ext_id}, "ExternalJobPosting")
    if payload.get("errors"):
        raise ValueError("Gem detail returned GraphQL errors")
    data = payload.get("data")
    if not isinstance(data, dict) or "oatsExternalJobPosting" not in data:
        raise ValueError("Gem detail schema is missing")
    post = data["oatsExternalJobPosting"]
    if post is None:
        return "", ""
    if not isinstance(post, dict) or post.get("extId") != ext_id:
        raise ValueError("Gem returned a different posting")
    return _description(post), _date(post.get("firstPublishedTsSec"))
