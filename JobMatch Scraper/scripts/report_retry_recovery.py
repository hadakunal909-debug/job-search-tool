"""Reconcile retry runs without changing their source snapshots or evidence."""
import argparse
import collections
import csv
import hashlib
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scraper
from scripts.retry_unscrapeable import read_jsonl, utcnow, normal_name


def canonical_board(url):
    p = urlsplit(url)
    host = p.netloc.lower()
    if host in ('boards.greenhouse.io', 'job-boards.greenhouse.io'):
        host = 'job-boards.greenhouse.io'
    query = [(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith('utm_') and k.lower() not in ('cid', 'source', 'ref')]
    return urlunsplit(('https', host, p.path.rstrip('/'), urlencode(sorted(query)), ''))


def evaluate(result, run, verifications=None):
    record = dict(result)
    boards = []
    for attempt in result.get('attempts', []):
        if attempt.get('kind') != 'board' or not attempt.get('fetched'):
            continue
        a = dict(attempt)
        a['run_directory'] = str(run)
        a['complete_verified'] = bool(a.get('complete', True) and not a.get('error') and not a.get('truncation') and not a.get('network_issues'))
        a['identity_confirmed'] = a.get('verdict') == 'confirmed' and a.get('scope') != 'single_posting'
        verification = (verifications or {}).get((normal_name(result.get('company', '')), canonical_board(a['url'])))
        if verification and a.get('scope') != 'single_posting':
            a['identity_confirmed'] = True
            a['verification_source'] = verification['source']
        boards.append(a)
    complete = [b for b in boards if b['complete_verified'] and b['identity_confirmed']]
    if complete:
        status = 'scrapeable'
    elif any(not b['complete_verified'] for b in boards):
        status = 'partial_readable'
    elif boards and all(b.get('scope') == 'single_posting' for b in boards):
        status = 'saved_posting_readable'
    elif boards:
        status = 'identity_review'
    else:
        status = result.get('retry_status', 'pending')
    record.update(status=status, boards=boards, complete_boards=complete, run_directory=str(run))
    return record


def write_csv(path, fields, rows):
    tmp = path.with_suffix('.csv.tmp')
    with tmp.open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)


def report(primary, extra, out):
    primary, out = Path(primary).resolve(), Path(out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((primary/'manifest.json').read_text(encoding='utf-8'))
    best = {}
    rank = {'scrapeable': 5, 'partial_readable': 4, 'identity_review': 3, 'saved_posting_readable': 2}
    all_evidence = []
    verification_file = primary.parent/'identity_verifications.json'
    verifications = {(normal_name(v['company']), canonical_board(v['board_url'])): v
                     for v in json.loads(verification_file.read_text(encoding='utf-8'))} if verification_file.exists() else {}
    for run in [primary] + [Path(p).resolve() for p in extra]:
        for result in read_jsonl(run/'checkpoints.jsonl'):
            value = evaluate(result, run, verifications)
            cid = result['candidate_id']
            previous = best.get(cid)
            if previous is None or (rank.get(value['status'], 1), value.get('fetched', 0)) >= (rank.get(previous['status'], 1), previous.get('fetched', 0)):
                best[cid] = value
            all_evidence.extend(value['boards'])
    sources_ok = []
    for source in manifest['sources']:
        original = Path(source['path'])
        copied = primary/source['copy']
        sources_ok.append({'file': source['path'], 'backup_matches': hashlib.sha256(copied.read_bytes()).hexdigest() == source['sha256'],
                           'original_unchanged': original.exists() and hashlib.sha256(original.read_bytes()).hexdigest() == source['sha256']})
    held = {canonical_board(u) for u, a, c in scraper.SOURCES}
    snapshot = primary.parent/'snapshot.json'
    if snapshot.exists():
        held.update(canonical_board(b['url']) for b in json.loads(snapshot.read_text(encoding='utf-8')).get('boards', []) if b.get('url'))
    for candidate in manifest['candidates']:
        held.update(canonical_board(b['url']) for b in candidate.get('known_boards', []))
    output = []
    for candidate in manifest['candidates']:
        result = best.get(candidate['candidate_id'], {})
        complete = result.get('complete_boards', [])
        output.append({'company': candidate['company'], 'candidate_id': candidate['candidate_id'],
                       'status': result.get('status', 'pending'), 'registered_by_exact_name': bool(candidate.get('known_boards')),
                       'readable_endpoints': len(result.get('boards', [])), 'confirmed_complete_boards': len(complete),
                       'new_complete_board_urls': ' | '.join(b['url'] for b in complete if canonical_board(b['url']) not in held),
                       'fetched_rows': result.get('fetched', 0), 'title_pass': result.get('title_pass', 0),
                       'us_title_pass': result.get('us_title_pass', 0), 'board_urls': result.get('board_urls', ''),
                       'evidence': str(Path(result.get('run_directory', primary))/result.get('evidence_file', '')),
                       'source_rows': len(candidate['records'])})
    write_csv(out/'employer_results.csv', list(output[0]), output)
    by_id = {r['candidate_id']: r for r in output}
    source_reports = out/'sources'
    source_reports.mkdir(exist_ok=True)
    for source in manifest['sources']:
        records = [r for r in manifest['rows'] if r['source_id'] == source['source_id']]
        reconciled = []
        for record in records:
            row = dict(record['original'])
            row.update({'retry_'+k: v for k, v in by_id.get(record['candidate_id'], {}).items()})
            row.update(retry_source_file=record['source_file'], retry_source_sheet=record['sheet'],
                       retry_source_line=record['source_line'], retry_original_json=json.dumps(record['original'], ensure_ascii=False))
            reconciled.append(row)
        if reconciled:
            fields = list(dict.fromkeys(k for row in reconciled for k in row))
            write_csv(source_reports/(source['source_id']+'_'+Path(source['path']).stem+'.csv'), fields, reconciled)
    original_rows = [r for r in manifest['rows'] if Path(r['source_file']).name == 'unscrapeable_boards.csv']
    original_output = [dict(r['original'], **{('retry_'+k):v for k,v in by_id.get(r['candidate_id'], {}).items()}) for r in original_rows]
    write_csv(out/'unscrapeable_boards_rechecked.csv', list(original_output[0]), original_output)
    unique_boards, jobs = {}, set()
    unreadable_raw_files = []
    raw_evidence = {str((Path(b['run_directory'])/b['raw_jobs_file']).resolve()): b
                    for b in all_evidence if b.get('raw_jobs_file')}
    def recovered_jobs():
        # Later focused retries contain corrected fields (for example locations).
        # Prefer their representative row while retaining every raw file separately.
        for run in [Path(p).resolve() for p in reversed(extra)] + [primary]:
            for raw in sorted((run/'raw_jobs').glob('*.json')):
                try:
                    payload = json.loads(raw.read_text(encoding='utf-8'))
                except (OSError, ValueError) as exc:
                    unreadable_raw_files.append({'file': str(raw), 'error': str(exc)})
                    continue
                evidence = raw_evidence.get(str(raw.resolve()), {})
                recovery_status = ('unverified' if not evidence else
                                   'partial_readable' if not evidence['complete_verified'] else
                                   'saved_posting_readable' if evidence.get('scope') == 'single_posting' else
                                   'scrapeable' if evidence['identity_confirmed'] else 'identity_review')
                for job in payload.get('jobs', []):
                    if not job.get('url') or job['url'] in jobs:
                        continue
                    jobs.add(job['url'])
                    yield dict(url=job['url'], company=job.get('company') or payload.get('company', ''),
                               title=job.get('title', ''), location=job.get('location', ''),
                               date_posted=job.get('date_posted') or job.get('found_date') or '', ats_type=payload.get('ats_type', ''),
                               board_url=payload.get('board_url', ''), recovery_status=recovery_status, raw_jobs_file=str(raw))
    write_csv(out/'recovered_jobs.csv', ['url','company','title','location','date_posted','ats_type','board_url','recovery_status','raw_jobs_file'], recovered_jobs())
    for b in all_evidence:
        key = canonical_board(b['url'])
        if b['identity_confirmed'] and b['complete_verified'] and key not in held:
            unique_boards[key] = b
    board_rows = []
    for key, b in sorted(unique_boards.items()):
        raw = json.loads((Path(b['run_directory'])/b['raw_jobs_file']).read_text(encoding='utf-8'))
        board_rows.append({'employer':raw['company'], 'ats_type':b['ats_type'], 'board_url':b['url'],
                           'job_count':b['fetched'], 'title_pass':b.get('title_pass',0), 'us_title_pass':b.get('us_title_pass',0),
                           'confidence':b.get('confidence',''), 'reported_name':b.get('reported_name',''),
                           'verification_source':b.get('verification_source', ''),
                           'provenance':b.get('provenance',''), 'raw_jobs_file':str(Path(b['run_directory'])/b['raw_jobs_file']),
                           'deployment_needed':b['ats_type'] in ('radancy','infosys','box','peopleadmin')})
    board_fields = ['employer','ats_type','board_url','job_count','title_pass','us_title_pass','confidence','reported_name','verification_source','provenance','raw_jobs_file','deployment_needed']
    write_csv(out/'new_board_candidates.csv', board_fields, board_rows)
    statuses = dict(collections.Counter(r['status'] for r in output))
    original_statuses = dict(collections.Counter(by_id[r['candidate_id']]['status'] for r in original_rows))
    summary = {'updated_at':utcnow(), 'employers':len(output), 'source_rows':len(manifest['rows']), 'statuses':statuses,
               'original_473_statuses':original_statuses, 'new_complete_board_urls':len(unique_boards),
               'distinct_returned_job_urls':len(jobs), 'source_checks':sources_ok,
               'all_sources_preserved': all(s['backup_matches'] and s['original_unchanged'] for s in sources_ok),
               'unreadable_raw_files': unreadable_raw_files}
    (out/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    lines = ['# Saved job boards: retry results', '',
             f"Report generated: {summary['updated_at']}. Pending employers: {statuses.get('pending', 0):,}.", '',
             'The original files and their row order are preserved. This report combines the broad retry with deeper targeted retries.', '',
             f'- Employers in the saved sources: **{len(output):,}**.',
             f'- Original CSV rows: **{len(original_rows)}**.',
             f'- Distinct posting URLs retrieved: **{len(jobs):,}** (not all pass the existing US/title filters).',
             f'- Newly identified board URLs with confirmed identity and no observed fetch errors: **{len(unique_boards)}**.', '',
             '## Original failure CSV', '', '| Result | Employers |','|---|---:|']
    lines += [f'| {k} | {v} |' for k,v in original_statuses.items()]
    lines += ['', '## Full saved universe', '', '| Result | Employers |','|---|---:|']
    lines += [f'| {k} | {v} |' for k,v in statuses.items()]
    lines += ['', '## Preservation checks', '',
              f"All original files and backups match their snapshot hashes: **{summary['all_sources_preserved']}**.",
              f'Unreadable or still-being-written raw files: **{len(unreadable_raw_files)}**.']
    lines += ['', '## Files', '', '- [Original CSV with updated results in the same row order](unscrapeable_boards_rechecked.csv)',
              '- [All employer results](employer_results.csv)', '- [New board candidates and raw-job file paths](new_board_candidates.csv)', '',
              '- [Retrieved jobs, one row per distinct URL](recovered_jobs.csv)', '',
              '## How to read these results', '',
              '`scrapeable` means the adapter returned jobs, employer identity passed the existing verification rules, and this attempt recorded no request failures or truncation. Some existing adapters intentionally restrict countries, titles or page counts; this is observed adapter coverage, not a guarantee of every worldwide opening.', '',
              '`partial_readable` means jobs were saved but completeness could not be established. `identity_review` means data was returned but ownership needs review. `saved_posting_readable` means an individual saved posting was recovered, not the whole board. `timeout`, `fetch_error`, `empty`, and `unresolved` remain retryable; none means permanently impossible.', '',
              'The recovered-jobs CSV includes partial and unverified data, labeled in recovery_status. It is an archive of returned listings, not the filtered application feed. Repeated URLs retain one representative row there; all variants remain in the raw JSON files.', '',
              'Raw jobs and all attempt evidence remain in the run folders. Original files were checked against the SHA-256 hashes in the snapshot manifest. No production jobs, boards, ledgers or application records were changed by this retry. New adapter code is local and has not been deployed.', '',
              'The new-board CSV is a review list. Inspect employer identity and US/title yield before adopting boards; keep shared-parent-company boards under their correct employer name.']
    (out/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in summary.items() if k!='source_checks'},indent=2))
    return summary


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run',required=True)
    p.add_argument('--extra-run',action='append',default=[])
    p.add_argument('--out',required=True)
    a=p.parse_args()
    report(a.run,a.extra_run,a.out)
