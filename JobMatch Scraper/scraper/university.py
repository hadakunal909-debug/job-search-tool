"""Public PageUp/Clinch university listings, using the site's own pagination."""
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from requests.exceptions import ChunkedEncodingError, ConnectionError, Timeout

from scraper.infosys import PartialScrapeError


def _text(node):
    return node.get_text(' ', strip=True) if node else ''


def _get_page(url):
    """Retry a dropped public response once; HTTP errors remain visible."""
    import scraper
    for attempt in range(2):
        try:
            return scraper._safe_get(url, timeout=30)
        except (ChunkedEncodingError, ConnectionError, Timeout):
            if attempt:
                raise
            time.sleep(0.5)


def pageup_page(html, base_url):
    """Return rows, next URL, advertised total for either public PageUp layout.

    Only explicit posting selectors qualify. Menus, category pages and the
    duplicated desktop/mobile tables are not jobs.
    """
    soup = BeautifulSoup(html, 'html.parser')
    rows, seen = [], set()
    for link in soup.select('a.job-link[href], .job-search-results-title a[href], '
                            '.job-search-results-card-title a[href]'):
        url = urljoin(base_url, link['href'])
        path = urlsplit(url).path
        if urlsplit(url).netloc != urlsplit(base_url).netloc:
            continue
        if not (re.search(r'/job/\d+/', path) or re.search(r'^/jobs/[^/]+$', path)):
            continue
        title = _text(link)
        if not title or url in seen:
            continue
        seen.add(url)
        card = link.find_parent('tr') or link.find_parent(class_='job-search-results-card-body')
        location = _text(card.select_one('.location, .job-search-results-location, '
                                         '.job-component-list-location')) if card else ''
        opened = card.select_one('.open-date time[datetime]') if card else None
        row = {'url': url, 'title': title, 'location': location, 'source': 'pageup'}
        if opened:
            row['date_posted'] = opened['datetime'][:10]
        rows.append(row)
    nxt = soup.select_one('a[rel~="next"][href]')
    if nxt is None:
        nxt = next((a for a in soup.select('a[href]')
                    if re.match(r'^More Jobs\s+\d+', _text(a))), None)
    next_url = urljoin(base_url, nxt['href']) if nxt else ''
    if next_url and (urlsplit(next_url).netloc != urlsplit(base_url).netloc
                     or urlsplit(next_url).path != urlsplit(base_url).path):
        raise ValueError('PageUp pagination left the validated board')
    text = soup.get_text(' ', strip=True)
    match = re.search(r'Displaying\s+[\d,]+\s*[^\d]+\s*[\d,]+\s+of\s+([\d,]+)\s+in total', text, re.I)
    total = int(match[1].replace(',', '')) if match else None
    if total is None:
        # Legacy pages say "More Jobs 711" after the first 20 rows. The number
        # is remaining jobs, not the page count or the full advertised total.
        more = re.search(r'^More Jobs\s+([\d,]+)', _text(nxt)) if nxt else None
        if more:
            total = len(rows) + int(more[1].replace(',', ''))
    return rows, next_url, total


def scrape_pageup(board_url):
    """Read all public pages, and fail with retained rows on incomplete traversal."""
    import scraper
    rows, seen, visited = [], set(), set()
    next_url, total = board_url, None
    try:
        for _ in range(200):
            if next_url in visited:
                raise ValueError('PageUp repeated pagination URL')
            visited.add(next_url)
            response = _get_page(next_url)
            response.raise_for_status()
            page, following, advertised = pageup_page(response.text, response.url)
            if total is None:
                total = advertised
            fresh = [row for row in page if row['url'] not in seen]
            if not fresh:
                raise ValueError('PageUp listing returned no new postings')
            rows.extend(fresh)
            seen.update(row['url'] for row in fresh)
            if not following:
                if total is not None and len(rows) != total:
                    raise ValueError('PageUp returned %s of %s advertised postings' % (len(rows), total))
                return rows
            next_url = following
        raise ValueError('PageUp pagination limit reached')
    except Exception as exc:
        if rows:
            raise PartialScrapeError('Incomplete PageUp listing: %s' % exc, rows) from exc
        raise


def umich_page(html, base_url):
    """Public U-M Drupal result table. The bare /search-jobs form has no rows."""
    soup = BeautifulSoup(html, 'html.parser')
    rows = []
    for tr in soup.select('tr'):
        link = tr.select_one('.views-field-title a[href]')
        if link is None:
            continue
        url = urljoin(base_url, link['href'])
        if urlsplit(url).netloc != 'careers.umich.edu' or not re.match(r'/job_detail/\d+/', urlsplit(url).path):
            continue
        row = {'url': url, 'title': _text(link),
               'location': _text(tr.select_one('.views-field-field-job-work-location')),
               'source': 'umich'}
        date = tr.select_one('.views-field-created time[datetime]')
        if date:
            row['date_posted'] = date['datetime'][:10]
        rows.append(row)
    nxt = soup.select_one('a[rel~="next"][href]')
    target = urljoin(base_url, nxt['href']) if nxt else ''
    if target and (urlsplit(target).netloc != 'careers.umich.edu' or urlsplit(target).path != '/search-jobs'):
        raise ValueError('U-M pagination left the career board')
    return rows, target


def scrape_umich(board_url):
    import scraper
    if urlsplit(board_url).netloc != 'careers.umich.edu':
        raise ValueError('U-M adapter requires careers.umich.edu')
    next_url = 'https://careers.umich.edu/search-jobs?position=All'
    rows, seen, visited = [], set(), set()
    try:
        for _ in range(250):
            if next_url in visited:
                raise ValueError('U-M repeated pagination URL')
            visited.add(next_url)
            response = _get_page(next_url)
            response.raise_for_status()
            page, following = umich_page(response.text, response.url)
            fresh = [row for row in page if row['url'] not in seen]
            if not fresh:
                raise ValueError('U-M listing returned no new postings')
            rows.extend(fresh)
            seen.update(row['url'] for row in fresh)
            if not following:
                return rows
            next_url = following
        raise ValueError('U-M pagination limit reached')
    except Exception as exc:
        if rows:
            raise PartialScrapeError('Incomplete U-M listing: %s' % exc, rows) from exc
        raise


def ku_page(html, base_url):
    """KU publishes its complete external BrassRing catalog as three HTML tables."""
    soup = BeautifulSoup(html, 'html.parser')
    rows, seen = [], set()
    for tr in soup.select('tr[id]'):
        link = tr.select_one('.job-name-row a[href]')
        if link is None:
            continue
        target = urljoin(base_url, link['href'])
        if urlsplit(target).netloc != 'employment.ku.edu' or not re.fullmatch(
                r'/jobs/(?:faculty|staff|students)/[^/]+/[0-9]+br', urlsplit(target).path, re.I):
            continue
        if target in seen:
            continue
        seen.add(target)
        rows.append({'title': _text(link), 'url': target, 'source': 'ku',
                     'location': _text(tr.select_one('.job-campus-row'))})
    if soup.select_one('a[rel~="next"], .pager__item--next a'):
        raise PartialScrapeError('KU catalog acquired pagination; traversal needs review', rows)
    return rows


def scrape_ku(board_url):
    if urlsplit(board_url).netloc != 'employment.ku.edu':
        raise ValueError('KU adapter requires employment.ku.edu')
    response = _get_page('https://employment.ku.edu/jobs')
    response.raise_for_status()
    rows = ku_page(response.text, response.url)
    if not rows:
        raise ValueError('KU public catalog has no readable job tables')
    return rows
