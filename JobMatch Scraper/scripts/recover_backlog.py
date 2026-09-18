"""Read-only, resumable validation of a cannot-scrape register.

Every input gets a result, including exclusions and missing URLs. A live snapshot
is required; this program never registers boards or imports jobs. Candidate URLs
retain their provenance, and a working endpoint alone never establishes identity.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urljoin, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('EV_OFF', '1')


def key(value):
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def canon(url):
    return re.sub(r'^https?://(?:www\.)?', '', url.strip().rstrip('/').lower())


def check(task):
    row, out_dir, budget = task
    import scraper
    from bs4 import BeautifulSoup
    from scripts.recover_failed_boards import PacedSession
    from scraper.probe_migratemate import grade

    class AuditSession(PacedSession):
        def __init__(self):
            super().__init__()
            self.calls = []
            self.session.headers.update(scraper.HEADERS)

        def request(self, method, url, **kwargs):
            kwargs['timeout'] = min(float(kwargs.get('timeout', 8)), 8)
            r = super().request(method, url, **kwargs)
            self.calls.append({'url': url, 'status': r.status_code, 'final_url': r.url})
            return r

    session = AuditSession()
    session.deadline = time.monotonic() + budget
    scraper.SESSION = session
    result = dict(row, status='unresolved', checked_at=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
    result['attempts'] = []
    url = row['candidate_url']
    candidates = []
    try:
        hit = scraper.detect_board(url)
        if hit:
            candidates.append((hit, url))
        r = scraper._safe_get(url, timeout=8)
        result.update(http_status=r.status_code, landing_url=r.url)
        soup = BeautifulSoup(r.text, 'html.parser')
        title = soup.title.get_text(' ', strip=True) if soup.title else ''
        result['page_title'] = title
        result['page_headings'] = [h.get_text(' ', strip=True) for h in soup.select('h1,h2')[:8]]
        if r.status_code in (401, 403, 429, 451):
            result['status'] = 'access_restricted'
            return result
        if r.status_code >= 400:
            result['status'] = 'http_error'
            return result
        hit = scraper.detect_board(r.url)
        if hit:
            candidates.append((hit, r.url))
        linked = []
        for a in soup.select('a[href],iframe[src]'):
            target = urljoin(r.url, a.get('href') or a.get('src'))
            hit = scraper.detect_board(target)
            if hit:
                candidates.append((hit, r.url))
            elif re.search(r'\b(jobs|careers|positions|opportunities|vacancies)\b', a.get_text(' ', strip=True), re.I):
                linked.append(target)
        # Embedded ATS URLs (including script configuration) are already handled
        # by the project's detector; preserve the page that linked the account.
        for m in scraper._ATS_LINK_RE.finditer(r.text):
            hit = scraper.detect_board(m.group(0))
            if hit:
                candidates.append((hit, r.url))
        if 'avature.portal.urlPath' in r.text:
            hit = scraper.avature_from_html(r.text, r.url)
            if hit:
                candidates.append((hit, r.url))
        if '/postings/all_jobs.atom' in r.text:
            candidates.append(((urljoin(r.url, '/postings/search'), 'peopleadmin', ''), r.url))
        origin = '{0.scheme}://{0.netloc}'.format(urlparse(r.url))
        fingerprints = [('phenom', scraper.detect_phenom), ('successfactors', scraper.detect_successfactors),
                        ('jibe', scraper.detect_jibe), ('eightfold', scraper.detect_eightfold),
                        ('data-search-results-module-name', scraper.detect_radancy)]
        if not candidates:
            for marker, detector in fingerprints:
                if marker in r.text.lower() or (marker == 'successfactors' and 'jobTitle-link' in r.text):
                    hit = detector(r.url if marker.startswith('data-') else origin)
                    if hit:
                        candidates.append((hit, r.url))
        if not candidates:
            for target in list(dict.fromkeys(linked))[:3]:
                hit = scraper.detect_linked_ats(target)
                if hit:
                    candidates.append((hit, target))
                    break
        result['career_links'] = list(dict.fromkeys(linked))[:15]
        seen = set()
        for hit, evidence in candidates:
            board, ats, suggested = hit
            if (board, ats) in seen:
                continue
            seen.add((board, ats))
            item = dict(board_url=board, ats_type=ats, evidence_url=evidence)
            result['attempts'].append(item)
            try:
                reported = scraper.board_display_name(board, ats, timeout=8)
                verdict, score = grade(row['company'], board, ats, reported, '')
                # Never turn a name-shaped account into identity evidence.
                item.update(reported_name=reported, identity_status=('name_match' if reported and verdict == 'confirmed' else 'needs_review'), identity_score=score)
                jobs = scraper.SCRAPERS[ats](board)
                item['complete'] = bool(getattr(jobs, 'complete', True))
                if not item['complete']:
                    item['error'] = getattr(jobs, 'incomplete_reason', '') or getattr(jobs, 'reason', '') or 'Incomplete pagination'
            except Exception as exc:
                jobs = getattr(exc, 'rows', [])
                item.update(error=type(exc).__name__ + ': ' + str(exc)[:200], complete=False)
            item['job_count'] = len(jobs)
            item['title_pass'] = sum(scraper.title_verdict(j.get('title', ''))[0] for j in jobs)
            item['us_title_pass'] = sum(scraper.title_verdict(j.get('title', ''))[0] and scraper.is_us_location(j.get('location', '')) for j in jobs)
            if jobs:
                raw = Path(out_dir) / 'raw_jobs' / (key(row['company'] + board) + '.json')
                raw.parent.mkdir(exist_ok=True)
                raw.write_text(json.dumps(dict(company=row['company'], board_url=board, jobs=jobs)), encoding='utf-8')
                item['raw_file'] = str(raw)
                result['status'] = 'readable' if item['complete'] else 'partial'
                break
        if candidates and result['status'] == 'unresolved':
            result['status'] = 'empty_or_failed_adapter'
        elif not candidates:
            result['status'] = 'no_adapter_detected'
    except Exception as exc:
        result.update(status='fetch_error', error=type(exc).__name__ + ': ' + str(exc)[:200])
    finally:
        result['requests'] = session.calls
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input', required=True)
    p.add_argument('--snapshot', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--workers', type=int, default=4, choices=range(1, 7))
    p.add_argument('--budget', type=int, default=55)
    p.add_argument('--limit', type=int)
    p.add_argument('--include-education', action='store_true')
    args = p.parse_args()
    import db, core
    snapshot = json.loads(Path(args.snapshot).read_text(encoding='utf-8'))
    assert snapshot['backend'] and 'blocks' in snapshot and snapshot['boards']
    blocks = {b['name_key'] for b in snapshot['blocks']}
    known = [(b['url'], b['company']) for b in snapshot['boards']]
    known += [(u, n) for u, _, n in snapshot['sources']]
    known_names = {db._block_core(db.block_key(n)) for _, n in known}
    known_urls = {canon(u) for u, _ in known}
    domains = json.loads((ROOT / 'company_domains.json').read_text(encoding='utf-8'))['domains']
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    checkpoint = out / 'audit.jsonl'
    done = {json.loads(l)['company'] for l in checkpoint.read_text(encoding='utf-8').splitlines()} if checkpoint.exists() else set()
    rows = list(csv.DictReader(open(args.input, encoding='utf-8-sig')))
    pending = []
    def save(row):
        with checkpoint.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(row) + '\n')
    for r in rows:
        if r['company'] in done:
            continue
        status = ''
        if db.is_blocked(r['company'], blocks):
            status = 'blocked'
        elif db._block_core(db.block_key(r['company'])) in known_names:
            status = 'already_configured_name'
        elif r['best_url'] and canon(r['best_url']) in known_urls:
            status = 'already_configured_url'
        elif re.search(r'\b(university|college)\b', r['company'], re.I) and not args.include_education:
            # A separate specialist handles these; do not mark them completed.
            continue
        url = r['best_url']
        provenance = r['url_provenance']
        if not url and domains.get(core.norm_company(r['company'])):
            url = 'https://' + domains[core.norm_company(r['company'])] + '/careers'
            provenance = 'cached domain; careers path unverified'
        if not status and not url:
            status = 'needs_portal_discovery'
        if status:
            save(dict(r, status=status))
        else:
            pending.append(dict(r, candidate_url=url, candidate_provenance=provenance))
    if args.limit:
        pending = pending[:args.limit]
    print('Pending portal checks:', len(pending), flush=True)
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(check, (r, str(out), args.budget)): r for r in pending}
        for f in as_completed(futures):
            r = futures[f]
            try:
                result = f.result()
            except Exception as exc:
                result = dict(r, status='worker_error', error=str(exc)[:200])
            save(result)
            print(result['company'], result['status'], [(x['ats_type'], x.get('job_count')) for x in result.get('attempts', [])], flush=True)


if __name__ == '__main__':
    main()
