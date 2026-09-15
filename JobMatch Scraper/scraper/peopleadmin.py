"""Public PeopleAdmin job feeds linked from employer listing pages."""
import re
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from scraper.infosys import PartialScrapeError

ATOM = '{http://www.w3.org/2005/Atom}'


def listing_info(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    feed = next((urljoin(url, a['href']) for a in soup.select('a[href]')
                 if urlparse(urljoin(url, a['href'])).path == '/postings/all_jobs.atom'
                 and urlparse(urljoin(url, a['href'])).netloc == urlparse(url).netloc), '')
    text = ' '.join(h.get_text(' ', strip=True) for h in soup.select('h1,h2'))
    count = re.search(r'Search\s+Postings\s*\(([\d,]+)\)', text, re.I)
    return feed, int(count[1].replace(',', '')) if count else None


def parse_feed(content, feed_url):
    root = ET.fromstring(content)
    if root.tag != ATOM+'feed':
        raise ValueError('PeopleAdmin response is not an Atom feed')
    rows, seen = [], set()
    for entry in root.findall(ATOM+'entry'):
        fields = {e.tag.split('}')[-1]: (e.text or '').strip() for e in entry}
        link = next((e.get('href') for e in entry.findall(ATOM+'link') if e.get('rel') == 'alternate'), fields.get('id', ''))
        url = urljoin(feed_url, link)
        if not fields.get('title') or not re.search(r'/postings/\d+$', urlparse(url).path) or urlparse(url).netloc != urlparse(feed_url).netloc:
            continue
        if url in seen:
            continue
        seen.add(url)
        location = ', '.join(fields[k] for k in ('city', 'state', 'country') if fields.get(k))
        rows.append({'url': url, 'title': fields['title'], 'location': location,
                     'date_posted': fields.get('published', '')[:10],
                     'description': BeautifulSoup(fields.get('content', ''), 'html.parser').get_text(' ', strip=True),
                     'source': 'peopleadmin'})
    return rows


def scrape_peopleadmin(board_url):
    import scraper
    listing = urljoin(board_url, '/postings/search')
    response = scraper._safe_get(listing, timeout=25)
    response.raise_for_status()
    feed, total = listing_info(response.text, response.url)
    listing_html, listing_url = response.text, response.url
    if not feed:
        raise ValueError('No public PeopleAdmin feed linked by this board')
    response = scraper._safe_get(feed, timeout=30)
    response.raise_for_status()
    rows = parse_feed(response.content, feed)
    # Feeds can lag the visible board by a few requisitions. Fill missing postings
    # from the site's real pagination before declaring the result incomplete.
    if total is not None and len(rows) < total:
        by_url = {row['url']: row for row in rows}
        visited = set()
        try:
            for _page in range(100):
                if listing_url in visited:
                    break
                visited.add(listing_url)
                soup = BeautifulSoup(listing_html, 'html.parser')
                for card in soup.select('.job-item-posting'):
                    link = card.select_one('.job-title a[href]')
                    if link is None:
                        continue
                    url = urljoin(listing_url, link['href'])
                    if url not in by_url and re.search(r'/postings/\d+$', urlparse(url).path) and urlparse(url).netloc == urlparse(feed).netloc:
                        row = {'url': url, 'title': link.get_text(' ', strip=True), 'location': '', 'source': 'peopleadmin'}
                        by_url[url] = row
                        rows.append(row)
                if len(rows) >= total:
                    break
                nxt = soup.select_one('a[rel~="next"][href]')
                target = urljoin(listing_url, nxt['href']) if nxt else ''
                if not target or urlparse(target).netloc != urlparse(feed).netloc or urlparse(target).path != '/postings/search':
                    break
                response = scraper._safe_get(target, timeout=25)
                response.raise_for_status()
                listing_html, listing_url = response.text, response.url
        except Exception as exc:
            raise PartialScrapeError('PeopleAdmin pagination failed: %s' % type(exc).__name__, rows) from exc
    if total is None or len(rows) != total:
        raise PartialScrapeError('PeopleAdmin feed returned %d of %s advertised postings' % (len(rows), total), rows)
    return rows
