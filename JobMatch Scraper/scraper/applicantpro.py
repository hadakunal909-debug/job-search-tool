"""Public ApplicantPro/isolved career-site listings, using their own Vue feed.

The employer page supplies a domain ID and the public listing parameters. The
feed returns the entire board and its advertised count; a mismatch is partial.
"""
import datetime
import json
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from scraper.infosys import PartialScrapeError


def board_url(url):
    p = urlparse(url)
    host = (p.hostname or '').lower()
    if re.fullmatch(r'[a-z0-9-]+\.(?:applicantpro|isolvedhire)\.com', host) and host.split('.')[0] != 'www':
        return 'https://' + host + '/jobs/'
    match = re.match(r'^/openings/([a-z0-9-]+)(?:/|$)', p.path, re.I)
    if host in ('www.applicantpro.com', 'applicantpro.com') and match:
        return 'https://' + match[1].lower() + '.applicantpro.com/jobs/'
    raise ValueError('Not an employer ApplicantPro/isolved board')


def listing_config(html):
    domain = re.search(r'\bdomainId\s*:\s*(\d+)', html)
    params = re.search(r'\bgetParams\s*:\s*(\{[^\n]*?\})\s*,\s*domainName', html)
    context = re.search(r'courierCurrentRouteData\s*=\s*(\{[^\n]*?\})\s*;', html)
    if not domain or not params or not context:
        raise ValueError('Public ApplicantPro listing configuration is absent')
    settings = json.loads(params[1])
    identity = json.loads(context[1])
    if int(settings.get('isInternal', 1)) != 0:
        raise ValueError('Internal employee board is not a public source')
    if str(identity.get('domain_id')) != domain[1]:
        raise ValueError('Listing and identity refer to different career sites')
    name = identity.get('career_site_name')
    if not isinstance(name, str) or not name.strip():
        raise ValueError('Career-site employer name is absent')
    return domain[1], settings, name.strip()


def parse_jobs(data, board, domain, company):
    if not isinstance(data, dict) or data.get('success') is not True:
        raise ValueError('ApplicantPro feed did not report success')
    value = data.get('data') or {}
    jobs, count = value.get('jobs'), value.get('jobCount')
    if not isinstance(jobs, list) or isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError('ApplicantPro listing schema/count is missing')
    rows, seen = [], set()
    for job in jobs:
        title, url = job.get('title'), job.get('jobUrl')
        if not title or not url or str(job.get('siteId')) != str(domain):
            raise PartialScrapeError('ApplicantPro job is missing identity fields', rows)
        if board_url(url) != board or not re.fullmatch(r'/jobs/\d+(?:-\d+)?(?:\.html)?/?', urlparse(url).path):
            raise PartialScrapeError('ApplicantPro job left the selected employer board', rows)
        if url in seen:
            continue
        seen.add(url)
        posted = ''
        try:
            posted = datetime.datetime.strptime(job.get('startDateRef', ''), '%b %d, %Y').date().isoformat()
        except (ValueError, TypeError):
            pass
        rows.append({'url': url, 'title': title.strip(), 'company': company,
                     'location': job.get('jobLocation') or ', '.join(str(job.get(k) or '') for k in ('city', 'abbreviation', 'iso3') if job.get(k)),
                     'date_posted': posted, 'source': 'applicantpro'})
    if len(rows) != count:
        raise PartialScrapeError('ApplicantPro returned %d of %d jobs' % (len(rows), count), rows)
    return rows


def scrape_applicantpro(url):
    import scraper
    board = board_url(url)
    response = scraper._safe_get(board, timeout=20)
    response.raise_for_status()
    if board_url(response.url) != board:
        raise ValueError('ApplicantPro redirected to a different employer board')
    domain, params, name = listing_config(response.text)
    origin = 'https://' + urlparse(board).netloc
    response = scraper._safe_get(origin + '/core/jobs/' + domain,
                                 params={'getParams': json.dumps(params)}, timeout=25)
    response.raise_for_status()
    return parse_jobs(response.json(), board, domain, name)


def detail_jd(url):
    import scraper
    board_url(url)
    response = scraper._safe_get(url, timeout=20)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(tag.get_text())
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict) and data.get('@type') == 'JobPosting':
            return BeautifulSoup(data.get('description', ''), 'html.parser').get_text('\n', strip=True), str(data.get('datePosted') or '')[:10]
    return '', ''
