"""Cornerstone career-site search using its public anonymous page context.

The page supplies a short-lived visitor token. It is used only at the employer's
CSOD origin or the CSOD cloud endpoint, never persisted or sent to another host.
"""
import json
import re
from datetime import datetime
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from scraper.infosys import PartialScrapeError


def board_url(url):
    p = urlparse(url)
    match = re.match(r"/ux/ats/careersite/(\d+)/home(?:/|$)", p.path)
    if not re.fullmatch(r"[a-z0-9-]+\.csod\.com", p.hostname or "") or not match:
        raise ValueError("Not a Cornerstone career-site URL")
    return "https://" + p.hostname + "/ux/ats/careersite/" + match[1] + "/home"


def context(url):
    import scraper
    board = board_url(url)
    r = scraper._safe_get(board, timeout=20)
    r.raise_for_status()
    match = re.search(r"csod\.context\s*=\s*(\{.*?\});", r.text, re.S)
    if not match:
        raise ValueError("Cornerstone anonymous page context is absent")
    data = json.loads(match[1])
    cloud = urlparse(data.get("endpoints", {}).get("cloud", ""))
    if (cloud.scheme != "https" or cloud.username or cloud.password or cloud.port
            or not re.fullmatch(r"[a-z0-9-]+\.api\.csod\.com", cloud.hostname or "")
            or cloud.path not in ("", "/") or cloud.query or cloud.fragment):
        raise ValueError("Cornerstone cloud endpoint is not trusted")
    if not data.get("token") or data.get("corp") != urlparse(board).hostname.split('.')[0]:
        raise ValueError("Cornerstone employer context does not match the URL")
    return board, data, "https://" + cloud.hostname


def headers(data):
    return {"Authorization": "Bearer " + data["token"],
            "CSOD-Accept-Language": data.get("cultureName") or "en-US"}


def payload(site_id, page, data):
    return dict(careerSiteId=site_id, careerSitePageId=site_id, pageNumber=page,
                pageSize=100, cultureId=int(data.get("cultureID") or 1),
                searchText='', cultureName=data.get("cultureName") or 'en-US',
                states=[], countryCodes=[], cities=[], placeID='', radius=None,
                # Zero means only today's postings; null is the site's Anytime filter.
                postingsWithinDays=None, customFieldCheckboxKeys=[],
                customFieldDropdowns=[], customFieldRadios=[])


def parse_date(value):
    value = (value or '').strip()
    for fmt in ('%m/%d/%Y', '%Y-%m-%d'):
        try:
            return datetime.strptime(value[:10], fmt).date().isoformat()
        except ValueError:
            pass
    return ''


def scrape_cornerstone(url):
    import scraper
    board, data, cloud = context(url)
    site_id = int(re.search(r'/careersite/(\d+)/', board)[1])
    rows, seen = [], set()
    for page in range(1, 201):
        try:
            r = scraper._safe_post(cloud + '/rec-job-search/external/jobs',
                                   payload(site_id, page, data), headers=headers(data), timeout=25)
            r.raise_for_status()
            envelope = r.json()
            result = envelope.get('data')
            if (envelope.get('status') != 'Success' or not isinstance(result, dict)
                    or not isinstance(result.get('requisitions'), list)
                    or not isinstance(result.get('totalCount'), int)
                    or result['totalCount'] < 0):
                raise ValueError('Cornerstone listing schema is missing')
            jobs, total = result['requisitions'], result['totalCount']
            added = 0
            for job in jobs:
                rid, title = str(job.get('requisitionId') or ''), job.get('displayJobTitle')
                if not rid.isdigit() or not isinstance(title, str) or not title.strip():
                    raise ValueError('Cornerstone posting identity is missing')
                if rid in seen:
                    continue
                locations = []
                for loc in job.get('locations') or []:
                    text = ', '.join(str(loc[k]) for k in ('city', 'state', 'country') if loc.get(k))
                    if text and text not in locations:
                        locations.append(text)
                rows.append({'title': title.strip(), 'url': board + '/requisition/' + rid,
                             'location': '; '.join(locations),
                             'found_date': parse_date(job.get('postingEffectiveDate'))})
                seen.add(rid)
                added += 1
            if len(rows) >= total:
                return rows
            if not added:
                raise ValueError('Cornerstone pagination stopped before the reported total')
        except Exception as exc:
            raise PartialScrapeError(str(exc), rows) from exc
    raise PartialScrapeError('Cornerstone exceeded 200 pages', rows)


def detail_jd(url):
    import scraper
    board = board_url(url)
    match = re.fullmatch(re.escape(urlparse(board).path) + r'/requisition/(\d+)/?', urlparse(url).path)
    if not match:
        return '', ''
    _, data, _ = context(board)
    origin = 'https://' + urlparse(board).hostname
    target = origin + '/services/x/job-requisition/v2/requisitions/' + match[1] + '/jobDetails?cultureId=' + str(int(data.get('cultureID') or 1))
    # Never forward even this anonymous bearer token through a redirect.
    r = scraper.SESSION.get(target, headers=headers(data), timeout=20, allow_redirects=False)
    r.raise_for_status()
    if r.status_code != 200:
        raise ValueError('Cornerstone detail endpoint redirected')
    result = r.json()
    if result.get('status') != 'Success' or not isinstance(result.get('data'), dict):
        raise ValueError('Cornerstone description schema is missing')
    return BeautifulSoup(result['data'].get('externalDescription') or '', 'html.parser').get_text('\n', strip=True), ''
