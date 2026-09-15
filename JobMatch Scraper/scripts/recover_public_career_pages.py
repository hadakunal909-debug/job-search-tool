"""Bounded checks of saved JazzHR, native iCIMS and Cornerstone employer URLs.

Input: JSON list of {company, board_url, ats_type}, or the shipped evidence CSV.
Writes local evidence only;
registration and job intake require reviewing employer identity and completeness.
Each worker uses its own HTTP session; no full-universe scrape is involved.
"""
import argparse
import csv
from concurrent.futures import ProcessPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def check(task):
    target, directory, budget = task
    os.environ['EV_OFF'] = '1'
    import scraper
    from scripts.recover_failed_boards import PacedSession
    from bs4 import BeautifulSoup

    class Session(PacedSession):
        calls = 0
        title = ''

        def request(self, method, url, **kwargs):
            self.calls += 1
            r = super().request(method, url, **kwargs)
            if self.calls == 1:
                title = BeautifulSoup(r.content, 'html.parser').title
                self.title = title.get_text(' ', strip=True) if title else ''
            return r

    session = Session()
    session.deadline = time.monotonic() + budget
    scraper.SESSION = session
    start = time.monotonic()
    record = dict(target)
    rows = []
    try:
        if target['ats_type'] not in ('jazzhr', 'icims', 'cornerstone'):
            raise ValueError('Only JazzHR, native iCIMS and Cornerstone are accepted')
        detected = scraper.detect_board(target['board_url'])
        if not detected or detected[:2] != (target['board_url'], target['ats_type']):
            raise ValueError('Input must use the canonical employer board URL')
        rows = scraper.SCRAPERS[target['ats_type']](target['board_url'])
        record.update(status='readable' if rows else 'empty', complete=True)
    except Exception as exc:
        rows = getattr(exc, 'rows', [])
        record.update(status='partial' if rows else 'error', complete=False,
                      error=type(exc).__name__ + ': ' + str(exc)[:200])
    key = hashlib.sha256(target['board_url'].encode()).hexdigest()[:16]
    raw = Path(directory) / 'raw_jobs' / (key + '.json')
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(json.dumps(dict(target, jobs=rows)), encoding='utf-8')
    record.update(jobs=len(rows), raw_file=str(raw), page_title=session.title,
                  requests=session.calls, seconds=round(time.monotonic() - start, 1))
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--targets', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--workers', type=int, default=3, choices=(1, 2, 3))
    parser.add_argument('--budget', type=int, default=90)
    parser.add_argument('--retry-errors', action='store_true')
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    checkpoint = out / 'probes.jsonl'
    latest = {}
    if checkpoint.exists():
        for line in checkpoint.read_text(encoding='utf-8').splitlines():
            row = json.loads(line)
            latest[row['board_url']] = row
    path = Path(args.targets)
    if path.suffix.lower() == '.csv':
        with path.open(encoding='utf-8-sig', newline='') as stream:
            targets = [dict(company=r['display_name'] or r['company'], board_url=r['board_url'], ats_type=r['ats_type'])
                       for r in csv.DictReader(stream) if r['ats_type'] in ('jazzhr', 'icims', 'cornerstone')]
    else:
        targets = json.loads(path.read_text(encoding='utf-8'))
    pending = [t for t in targets if t['board_url'] not in latest or
               (args.retry_errors and not latest[t['board_url']]['complete'])]
    tasks = [(t, str(out), args.budget) for t in pending]
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for row in pool.map(check, tasks):
            with checkpoint.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(row) + '\n')
            print(json.dumps(row), flush=True)


if __name__ == '__main__':
    main()
