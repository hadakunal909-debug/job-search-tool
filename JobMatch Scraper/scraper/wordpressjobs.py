"""Published employer WordPress jobs with an explicit REST total and full content.

These two employers' official careers pages publish jobs themselves; their older
ADP portals no longer represent those catalogs. Keep the verified post types and
canonical employer URLs distinct from ordinary WordPress blog posts.
"""
import math
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from scraper.infosys import PartialScrapeError

BOARDS = {
    "www.aggteam.com": ("/careers/", "awsm_job_openings", "/jobs/", "Alliance Global Group LLC"),
    "maylorusa.com": ("/company/careers/", "jobs", "/job/", "Maylor Foundation Contractors LLC"),
}
PAGE_SIZE = 100
MAX_PAGES = 50


def board_parts(url):
    p = urlparse(url)
    config = BOARDS.get(p.hostname)
    if (p.scheme != "https" or not config or p.path.rstrip("/") != config[0].rstrip("/")
            or p.query or p.fragment or p.username or p.password or p.port):
        raise ValueError("Not a verified employer WordPress careers board")
    return "https://" + p.hostname, config


def board_url(url):
    origin, config = board_parts(url)
    return origin + config[0]


def _text(html):
    return BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)


def _get(url, **kwargs):
    import scraper
    response = scraper._safe_get(url, timeout=20, **kwargs)
    response.raise_for_status()
    return response


def _configuration(url):
    origin, config = board_parts(url)
    soup = BeautifulSoup(_get(origin + config[0]).text, "html.parser")
    advertised = soup.find("link", rel="https://api.w.org/")
    if advertised is None or advertised.get("href") != origin + "/wp-json/":
        raise ValueError("Employer page no longer advertises its public WordPress API")
    index = _get(advertised["href"]).json()
    route = "/wp/v2/" + config[1]
    if not isinstance(index, dict) or not isinstance(index.get("routes"), dict) or route not in index["routes"]:
        raise ValueError("Public employer job collection is missing")
    if "GET" not in index["routes"][route].get("methods", []):
        raise ValueError("Job collection does not permit public listing reads")
    locations = {option["data-slug"]: option.get_text(" ", strip=True)
                 for option in soup.select(".awsm-job-location-filter-option option[data-slug]")}
    return origin, config, locations


def parse_job(job, origin, config, locations):
    if not isinstance(job, dict) or job.get("status") != "publish" or job.get("type") != config[1]:
        raise ValueError("WordPress row is not a published employer job")
    jid = job.get("id")
    if isinstance(jid, bool) or not isinstance(jid, int) or jid <= 0:
        raise ValueError("WordPress posting lacks its public ID")
    link = job.get("link")
    if not isinstance(link, str):
        raise ValueError("WordPress posting lacks its canonical link")
    parsed = urlparse(link)
    if (parsed.scheme != "https" or parsed.netloc != urlparse(origin).netloc
            or not re.fullmatch(re.escape(config[2]) + r"[^/]+/", parsed.path)
            or parsed.query or parsed.fragment):
        raise ValueError("WordPress posting URL belongs to another employer or collection")
    title = job.get("title")
    if not isinstance(title, dict) or not isinstance(title.get("rendered"), str) or not _text(title["rendered"]):
        raise ValueError("WordPress posting title is missing")
    date = job.get("date")
    if not isinstance(date, str) or not re.match(r"^\d{4}-\d{2}-\d{2}T", date):
        raise ValueError("WordPress publisher date is missing")
    if config[1] == "awsm_job_openings":
        classes = job.get("class_list")
        if not isinstance(classes, list) or any(not isinstance(c, str) for c in classes):
            raise ValueError("WordPress job-location terms are malformed")
        slugs = [c[len("job-location-"):] for c in classes if c.startswith("job-location-")]
        if any(s not in locations for s in slugs):
            raise ValueError("Job location is absent from the employer's published filters")
        location = "; ".join(locations[s] for s in slugs)
        content = job.get("content")
        if not isinstance(content, dict) or not isinstance(content.get("rendered"), str):
            raise ValueError("WordPress job content is malformed")
        jd = _text(content["rendered"])
    else:
        city = job.get("position_job_location_addressLocality") or job.get("position_job_location") or ""
        state = job.get("position_job_location_addressRegion") or ""
        country = job.get("position_job_location_addressCountry") or ""
        if any(not isinstance(v, str) for v in (city, state, country)):
            raise ValueError("WordPress job location is malformed")
        location = ", ".join(v.strip() for v in (city, state, country) if v.strip())
        fields = ("position_description", "position_responsibilities", "position_qualifications",
                  "position_educationRequirements", "position_experienceRequirements", "position_skills",
                  "position_job_benefits", "position_work_hours")
        parts = [job.get(k) or "" for k in fields]
        if any(not isinstance(part, str) for part in parts):
            raise ValueError("WordPress job content is malformed")
        jd = "\n".join(_text(part) for part in parts if _text(part))
    return {"title": _text(title["rendered"]), "url": link, "location": location,
            "found_date": date[:10], "source": "wordpressjobs", "jd": jd}


def scrape_wordpressjobs(url):
    rows, seen, ids, errors = [], set(), set(), []
    try:
        origin, config, locations = _configuration(url)
        total, pages = None, None
        for page in range(1, MAX_PAGES + 1):
            response = _get(origin + "/wp-json/wp/v2/" + config[1],
                            params={"per_page": PAGE_SIZE, "page": page, "status": "publish", "orderby": "id", "order": "asc"})
            count, page_count = response.headers.get("X-WP-Total"), response.headers.get("X-WP-TotalPages")
            if (not isinstance(count, str) or not re.fullmatch(r"\d+", count)
                    or not isinstance(page_count, str) or not re.fullmatch(r"\d+", page_count)):
                raise ValueError("WordPress public total/pages are missing")
            count, page_count = int(count), int(page_count)
            if page_count != math.ceil(count / PAGE_SIZE):
                raise ValueError("WordPress total and page count conflict")
            if total is None:
                total, pages = count, page_count
            elif total != count or pages != page_count:
                raise ValueError("WordPress catalog count changed while paging")
            batch = response.json()
            if not isinstance(batch, list):
                raise ValueError("WordPress listing is not an array")
            for job in batch:
                try:
                    row = parse_job(job, origin, config, locations)
                    if job["id"] in ids or row["url"] in seen:
                        raise ValueError("WordPress repeated a posting")
                    ids.add(job["id"])
                    seen.add(row["url"])
                    rows.append(row)
                except (TypeError, ValueError) as exc:
                    errors.append(str(exc))
            if page >= pages:
                if len(rows) != total:
                    errors.append("read %d of %d advertised jobs" % (len(rows), total))
                if errors:
                    raise ValueError("; ".join(dict.fromkeys(errors)))
                return rows
            if not batch:
                raise ValueError("WordPress ended before its advertised final page")
        raise ValueError("WordPress exceeded its page safety cap")
    except Exception as exc:
        raise PartialScrapeError("WordPress employer listing incomplete: " + str(exc)[:240], rows) from exc
