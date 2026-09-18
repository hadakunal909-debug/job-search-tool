"""PageUp's two live layouts, pagination coverage, and incomplete-run retention."""
import os
import sys
from pathlib import Path
from unittest.mock import patch

os.environ['EV_OFF'] = '1'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scraper
from scraper.infosys import PartialScrapeError
from scraper.university import pageup_page, scrape_pageup, umich_page, scrape_umich, ku_page, scrape_ku
from scraper.peopleadmin import listing_info


BASE = 'https://example.edu/cw/en-us/listing/'
LEGACY = '''<table><tr><td><a class="job-link" href="/cw/en-us/job/123/data-engineer">Data Engineer</a></td>
<td><span class="location">Denver, CO</span></td>
<td><span class="open-date"><time datetime="2026-09-18T14:00:00Z">Sep 18</time></span></td></tr></table>
<a class="job-link" href="/cw/en-us/job/123/data-engineer">Data Engineer</a>
<a href="?page=2">More Jobs 1</a>'''
LAST = '''<a class="job-link" href="/cw/en-us/job/456/analyst">Analyst</a>'''
CLINCH = '''<div class="job-search-results-card-body"><h3 class="job-search-results-card-title">
<a href="https://example.edu/jobs/data-engineer">Data Engineer</a></h3>
<div class="job-component-list-location">East Lansing, Michigan</div></div>
<tr><td class="job-search-results-title"><a href="/jobs/program-coordinator">Program Coordinator</a></td>
<td class="job-search-results-location">Tuscaloosa</td></tr>
Displaying <b>1 - 2</b> of <b>2</b> in total'''


class Response:
    def __init__(self, html, url):
        self.text, self.url = html, url
    def raise_for_status(self):
        pass


def main():
    rows, nxt, total = pageup_page(LEGACY, BASE)
    assert len(rows) == 1 and total == 2 and nxt == BASE+'?page=2'
    assert rows[0]['location'] == 'Denver, CO' and rows[0]['date_posted'] == '2026-09-18'
    rows, nxt, total = pageup_page(CLINCH, 'https://example.edu/jobs/search')
    assert len(rows) == 2 and total == 2 and not nxt
    assert [r['location'] for r in rows] == ['East Lansing, Michigan', 'Tuscaloosa']
    with patch.object(scraper, '_safe_get', side_effect=[Response(LEGACY, BASE), Response(LAST, BASE+'?page=2')]) as get:
        rows = scrape_pageup(BASE)
        assert len(rows) == 2 and get.call_count == 2
    for second in [Response(LEGACY, BASE+'?page=2'), Response('<html>Access denied</html>', BASE+'?page=2'), RuntimeError('timeout')]:
        with patch.object(scraper, '_safe_get', side_effect=[Response(LEGACY, BASE), second]):
            try:
                scrape_pageup(BASE)
                raise AssertionError('incomplete pagination reported as successful')
            except PartialScrapeError as exc:
                assert len(exc.rows) == 1
    try:
        pageup_page(LEGACY.replace('?page=2', 'https://other.edu/cw/en-us/listing/?page=2'), BASE)
        raise AssertionError('cross-board pagination accepted')
    except ValueError:
        pass
    with patch.object(scraper, '_safe_get', return_value=Response(CLINCH.replace('of <b>2', 'of <b>3'), 'https://example.edu/jobs/search')):
        try:
            scrape_pageup('https://example.edu/jobs/search')
            raise AssertionError('advertised total mismatch accepted')
        except PartialScrapeError as exc:
            assert len(exc.rows) == 2
    michigan = '''<tr><td class="views-field-created"><time datetime="2026-09-18T10:00:00-04:00">9/18/26</time></td>
    <td class="views-field-title"><a href="/job_detail/123/analyst">Analyst</a></td>
    <td class="views-field-field-job-work-location">Ann Arbor Campus</td></tr>'''
    u = 'https://careers.umich.edu/search-jobs?position=All'
    rows, nxt = umich_page(michigan, u)
    assert len(rows) == 1 and not nxt and rows[0]['date_posted'] == '2026-09-18'
    with patch.object(scraper, '_safe_get', return_value=Response(michigan, u)):
        assert scrape_umich(u) == rows
    ku = '<tr id="12BR"><td class="job-name-row"><a href="/jobs/staff/analyst/12br">Analyst</a></td><td class="job-campus-row">Lawrence</td><td class="job-review-row">2026-09-30</td></tr>'
    assert len(ku_page(ku+ku, 'https://employment.ku.edu/jobs')) == 1
    assert 'date_posted' not in ku_page(ku, 'https://employment.ku.edu/jobs')[0]
    assert not ku_page(ku.replace('/jobs/staff/', 'https://other.edu/jobs/staff/'), 'https://employment.ku.edu/jobs')
    try:
        ku_page(ku+'<a rel="next" href="?page=2">next</a>', 'https://employment.ku.edu/jobs')
        raise AssertionError('KU unexpected pagination accepted')
    except PartialScrapeError as exc:
        assert len(exc.rows) == 1
    for label in ['Search Position Postings (222)', 'Search Requisitions (211)', 'View Results ( 33 )']:
        count = int(''.join(c for c in label if c.isdigit()))
        assert listing_info('<h2>'+label+'</h2>', 'https://example.edu/')[1] == count
    print('PASS PageUp legacy, Clinch cards/table, pagination, totals, and partial failures')


if __name__ == '__main__':
    main()
