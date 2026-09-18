"""TikTok's public careers supplier API and server-rendered job descriptions.

careers.tiktok.com redirects to lifeattiktok.com. The site's own search client
publishes the API and website-path header used here. US city identifiers come
from the current public filter configuration, rather than a fixed city list.
"""
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from scraper.infosys import PartialScrapeError

BOARD = "https://lifeattiktok.com/search"
API = "https://api.lifeattiktok.com/api/v1/public/supplier"
API_HEADERS = {"Origin": "https://lifeattiktok.com", "website-path": "tiktok",
               "Accept-Language": "en-US", "Content-Type": "application/json"}
PAGE_SIZE = 100
MAX_PAGES = 1000


def _request(endpoint, payload):
    import scraper
    if endpoint not in ("/config/job/filters", "/search/job/posts"):
        raise ValueError("Unsupported TikTok public endpoint")
    response = scraper._safe_post(API + endpoint, payload, headers=API_HEADERS, timeout=30)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or data.get("code") != 0 or not isinstance(data.get("data"), dict):
        raise ValueError("TikTok API did not return a successful response")
    return data["data"]


def _places(city):
    chain, seen = [], set()
    while isinstance(city, dict):
        code = city.get("code")
        if not isinstance(code, str) or code in seen:
            raise ValueError("TikTok location hierarchy is invalid")
        seen.add(code)
        chain.append(city)
        city = city.get("parent")
    return chain


def us_city_codes():
    cities = _request("/config/job/filters", {}).get("city_list")
    if not isinstance(cities, list):
        raise ValueError("TikTok city filters are absent")
    codes = []
    for city in cities:
        places = _places(city)
        if any(p.get("code") == "CN_6" and p.get("en_name") == "United States of America" for p in places):
            codes.append(city["code"])
    if not codes:
        raise ValueError("TikTok public filters have no verified US cities")
    return sorted(set(codes))


def listing_page(city_codes, offset=0):
    data = _request("/search/job/posts", {"keyword": "", "limit": PAGE_SIZE,
                    "offset": offset, "recruitment_id_list": [], "location_code_list": city_codes})
    jobs, count = data.get("job_post_list"), data.get("count")
    if not isinstance(jobs, list) or isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("TikTok listing schema or total is missing")
    return jobs, count


def _row(job):
    if not isinstance(job, dict):
        raise ValueError("TikTok posting is not an object")
    jid, title = job.get("id"), job.get("title")
    if not isinstance(jid, str) or not re.fullmatch(r"\d+", jid) or not isinstance(title, str) or not title.strip():
        raise ValueError("TikTok posting lacks a title or public ID")
    places = _places(job.get("city_info"))
    if not any(p.get("code") == "CN_6" and p.get("en_name") == "United States of America" for p in places):
        raise ValueError("TikTok search returned a location outside its US filter")
    location = ", ".join(p["en_name"] for p in places if isinstance(p.get("en_name"), str) and p["en_name"])
    # The list includes responsibilities/requirements but omits pay transparency
    # sections. Leave jd for detail_jd so admitted jobs get the complete posting.
    return {"url": BOARD + "/" + jid, "title": title.strip(), "company": "TikTok",
            "location": location, "source": "tiktok"}


def scrape_tiktok(board_url=BOARD):
    if urlparse(board_url).hostname not in ("lifeattiktok.com", "careers.tiktok.com"):
        raise ValueError("Not TikTok's official careers portal")
    rows, seen, offset, expected = [], set(), 0, 0
    try:
        cities = us_city_codes()
        for _ in range(MAX_PAGES):
            jobs, count = listing_page(cities, offset)
            expected = max(expected, count)
            fresh = 0
            for job in jobs:
                row = _row(job)
                if row["url"] in seen:
                    continue
                rows.append(row)
                seen.add(row["url"])
                fresh += 1
            if len(rows) >= expected:
                return rows
            if not fresh:
                raise ValueError("TikTok search repeated or ended before its advertised total")
            offset += len(jobs)
        raise ValueError("TikTok exceeded its pagination safety cap")
    except Exception as exc:
        raise PartialScrapeError("TikTok listing incomplete: " + str(exc), rows) from exc


def detail_jd(url):
    import scraper
    parsed = urlparse(url)
    if parsed.hostname != "lifeattiktok.com" or not re.fullmatch(r"/search/\d+/?", parsed.path):
        return "", ""
    response = scraper._safe_get(url, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    parts = []
    for label in ("Responsibilities", "Qualifications", "Job Information"):
        heading = soup.find(lambda tag: tag.name in ("p", "h2", "h3") and tag.get_text(strip=True) == label)
        if heading is not None:
            section = heading.parent.get_text("\n", strip=True)
            if section != label:
                parts.append(section)
    # Two substantive sections distinguish a posting from navigation or an error page.
    return ("\n\n".join(parts), "") if len(parts) >= 2 else ("", "")
