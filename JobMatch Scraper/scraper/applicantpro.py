"""Public ApplicantPro/isolved career-site listings, using their own Vue feed.

The employer page supplies a domain ID and the public listing parameters. The
feed returns the entire board and its advertised count; a mismatch is partial.
"""
import datetime
import json
import re
from html import unescape
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
    return domain[1], settings, unescape(name).strip()


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
        rows.append({'url': url, 'title': unescape(title).strip(), 'company': company,
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


def _detail_listing_id(url):
    path = urlparse(url).path
    match = re.fullmatch(r'/(?:openings/[a-z0-9-]+/)?jobs/(\d+)(?:-\d+)?(?:\.html)?/?', path, re.I)
    return int(match[1]) if match else None


def _detail_name(value):
    return ' '.join(unescape(value).split()).casefold()


def _vue_detail_config(soup, url):
    # Only the public JobDetail component supplies these IDs. InlineAuth and
    # SocialMediaWidget carry other IDs and abbreviated advertising descriptions.
    pattern = re.compile(r'''\bbootstrapVue\s*\(\s*(["'])#job_detail\1\s*,\s*\[\s*(["'])JobDetail\2\s*\]\s*,\s*\{\s*componentData\s*:\s*\{(.*?)\}\s*\}\s*\)''', re.S)
    scripts = [tag.get_text() for tag in soup.find_all('script')]
    blocks = [match[3] for script in scripts for match in pattern.finditer(script)]
    if len(blocks) != 1:
        return None
    domains = re.findall(r'\bdomainId\s*:\s*(\d+)\s*(?=,|}|$)', blocks[0])
    listings = re.findall(r'\bjobListingId\s*:\s*(\d+)\s*(?=,|}|$)', blocks[0])
    listing_id = _detail_listing_id(url)
    if len(domains) != 1 or len(listings) != 1 or not listing_id:
        return None
    domain, listing = int(domains[0]), int(listings[0])
    if not domain or listing != listing_id:
        return None
    employer = re.search(r'\bdomainTitle\s*:\s*("(?:\\.|[^"\\])*")\s*(?=,|}|$)', blocks[0])
    try:
        company = json.loads(employer[1]) if employer else ''
    except (ValueError, TypeError):
        return None
    if not isinstance(company, str) or not company.strip():
        return None
    # The route context corroborates the tenant and keeps employee-only pages out.
    context = re.search(r'courierCurrentRouteData\s*=\s*(\{[^\n]*?\})\s*;', '\n'.join(scripts))
    if context:
        try:
            identity = json.loads(context[1])
            if not isinstance(identity, dict) or str(identity.get('domain_id')) != str(domain) or str(identity.get('user_id', 0)) != '0':
                return None
            if not isinstance(identity.get('career_site_name'), str) or _detail_name(identity['career_site_name']) != _detail_name(company):
                return None
        except (ValueError, TypeError):
            return None
    title = soup.title.get_text(' ', strip=True) if soup.title else ''
    if not _detail_name(title).endswith(' - ' + _detail_name(company + ' Jobs')):
        return None
    return (domain, listing, title) if title else None


def _vue_detail(data, domain, listing, page_title):
    if not isinstance(data, dict) or data.get('success') is not True or not isinstance(data.get('data'), dict):
        raise ValueError('ApplicantPro detail did not report success')
    job = data['data']
    # Same-title postings can differ by location; require both returned IDs.
    for key, expected in (('id', listing), ('siteId', domain)):
        value = job.get(key)
        if isinstance(value, bool) or not re.fullmatch(r'\d+', str(value)) or int(value) != expected:
            raise ValueError('ApplicantPro detail belongs to a different job or employer')
    title, description = job.get('title'), job.get('description')
    if not isinstance(title, str) or not title.strip() or not isinstance(description, str) or not description.strip():
        raise ValueError('ApplicantPro detail title/description is absent')
    normalized_title, normalized_page = _detail_name(title), _detail_name(page_title)
    if normalized_page != normalized_title and not normalized_page.startswith(normalized_title + ' - '):
        raise ValueError('ApplicantPro detail title disagrees with the posting page')
    jd = BeautifulSoup(description, 'html.parser').get_text('\n', strip=True)
    jd = re.sub(r'\n{3,}', '\n\n', jd).strip()
    if not jd:
        raise ValueError('ApplicantPro detail description contains no text')
    posted = ''
    for fmt in ('%d-%b-%Y', '%b %d, %Y', '%Y-%m-%d'):
        try:
            posted = datetime.datetime.strptime(job.get('startDateRef', ''), fmt).date().isoformat()
            break
        except (ValueError, TypeError):
            pass
    return jd, posted


def detail_jd(url):
    import requests
    import scraper
    board = board_url(url)
    response = scraper._safe_get(url, timeout=20)
    response.raise_for_status()
    if board_url(response.url) != board or _detail_listing_id(response.url) != _detail_listing_id(url):
        raise ValueError('ApplicantPro redirected to a different employer or job')
    soup = BeautifulSoup(response.text, 'html.parser')
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(tag.get_text())
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict) and data.get('@type') == 'JobPosting':
            description = data.get('description')
            if isinstance(description, str) and description.strip():
                return BeautifulSoup(description, 'html.parser').get_text('\n', strip=True), str(data.get('datePosted') or '')[:10]
    config = _vue_detail_config(soup, response.url)
    if not config:
        return '', ''
    domain, listing, page_title = config
    origin = 'https://' + urlparse(board).netloc
    api_url = '%s/core/jobs/%s/%s/job-details' % (origin, domain, listing)
    try:
        response = scraper._safe_get(api_url, headers=dict(scraper.HEADERS, Accept='application/json'), timeout=20)
        response.raise_for_status()
        if response.url != api_url:
            return '', ''
        return _vue_detail(response.json(), domain, listing, page_title)
    except (requests.RequestException, ValueError, TypeError):
        # A failed public detail read stays pending; never substitute the short
        # advertising/social snippet for the complete description.
        return '', ''
