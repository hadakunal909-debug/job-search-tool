import os
os.environ['EV_OFF'] = '1'
import json
import unittest
from unittest.mock import patch
from bs4 import BeautifulSoup
import scraper
from scraper import icims, jazzhr, score_jobs
from scraper import cornerstone as cs
from scraper.infosys import PartialScrapeError

IC = 'https://careers-example.icims.com/jobs/search?ss=1&in_iframe=1'
JAZZ = 'https://example.applytojob.com/apply'


def soup(text):
    return BeautifulSoup(text, 'html.parser')


def ic_page(number=1, total=1, job_id=1, next_link=True):
    return soup('''<div class="iCIMS_SearchResultsHeader">Page %d of %d</div>
      <li class="iCIMS_JobCardItem"><a href="/jobs/%d/data-analyst/job?in_iframe=1">
      <span class="sr-only">External Job Title</span><h3>Data Analyst</h3></a>
      <div class="iCIMS_JobHeaderTag"><dt>Job Location : Location</dt><dd>US-NY-New York</dd></div>
      <div class="description">Preview is not a complete JD</div></li>%s''' % (
        number, total, job_id,
        '<a href="?pr=%d&amp;in_iframe=1">Next page of results</a>' % number if next_link else ''))


class PublicCareerTests(unittest.TestCase):
    def test_icims_client_id_and_header_location(self):
        from requests import Response
        response = Response()
        response.status_code = 200
        response._content = b'<html>Results</html>'
        with patch.object(scraper, '_safe_get', return_value=response) as get:
            icims.page('https://careers-example.icims.com/jobs/1/analyst/job')
            self.assertTrue(get.call_args.kwargs['headers']['User-Agent'].startswith('python-requests/'))
            self.assertIn('in_iframe=1', get.call_args.args[0])
        page = ic_page()
        page.select_one('.iCIMS_JobHeaderTag').decompose()
        page.select_one('li').append(soup('<div class="header left"><span class="field-label">Job Locations</span><span>US-FL-Fort Lauderdale</span></div>'))
        row = icims.parse_page(page, IC)[0][0]
        self.assertEqual(row['location'], 'Fort Lauderdale, FL, US')
        import core
        self.assertEqual(core.parse_location(row['location'])['state'], 'FL')

    def test_missing_location_uses_kept_posting_metadata(self):
        page = ic_page()
        page.select_one('.iCIMS_JobHeaderTag').decompose()
        with patch.object(icims, 'page', return_value=page), patch.object(icims, 'detail_fields', return_value=('Full JD', '2026-09-01', 'Toronto, ON, CA')):
            row = icims.scrape_icims(IC)[0]
        self.assertEqual(row['location'], 'Toronto, ON, CA')
        self.assertFalse(scraper.is_us_location(row['location']))
        self.assertEqual(row['jd'], 'Full JD')

    def test_intake_receives_location_and_date_before_filtering(self):
        rows = [{'url': 'https://careers-example.icims.com/jobs/1/analyst/job', 'title': 'Analyst', 'location': ''},
                {'url': 'https://careers-example.icims.com/jobs/2/analyst/job', 'title': 'Analyst', 'location': 'Boston, MA, US', 'found_date': '2026-09-15'}]
        with patch.object(icims, 'detail_fields', return_value=('Full description. ' * 100, '2025-01-01', 'Toronto, ON, CA')):
            tried, got, _ = scraper._fetch_jd_queue(rows, 10, 'test', score_jobs)
        self.assertEqual((tried, got), (2, 2))
        self.assertEqual(rows[0]['found_date'], '2025-01-01')
        self.assertFalse(scraper.is_us_location(rows[0]['location']))
        self.assertEqual(rows[1]['location'], 'Boston, MA, US')
        self.assertEqual(rows[1]['found_date'], '2026-09-15')

    def test_detection_preserves_tenant_and_drops_tracking(self):
        self.assertEqual(scraper.detect_board(JAZZ + '/AbC123/Analyst?source=indeed')[:2], (JAZZ, 'jazzhr'))
        self.assertEqual(scraper.detect_board(IC.split('?')[0].replace('search', '44/analyst/job'))[:2], (IC, 'icims'))
        self.assertIsNone(scraper.detect_board('https://example.applytojob.com.evil.test/apply'))
        self.assertIsNone(scraper.detect_board('https://example.icims.com.evil.test/jobs/search'))

    def test_icims_all_pages_and_clean_title(self):
        with patch.object(icims, 'page', side_effect=[ic_page(1, 2), ic_page(2, 2, 2)]) as fetch:
            rows = icims.scrape_icims(IC)
        self.assertEqual(len(rows), 2)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(rows[0]['title'], 'Data Analyst')
        self.assertEqual(rows[0]['location'], 'New York, NY, US')
        self.assertNotIn('jd', rows[0])
        self.assertNotIn('?', rows[0]['url'])

    def test_icims_partial_failure_keeps_prior_rows(self):
        with patch.object(icims, 'page', side_effect=[ic_page(1, 2), RuntimeError('HTTP 503')]):
            with self.assertRaises(PartialScrapeError) as ctx:
                icims.scrape_icims(IC)
        self.assertEqual(len(ctx.exception.rows), 1)
        with self.assertRaises(PartialScrapeError):
            icims.parse_page(ic_page(1, 2, next_link=False), IC)

    def test_icims_repeated_page_is_not_complete(self):
        with patch.object(icims, 'page', side_effect=[ic_page(1, 2), ic_page(1, 2)]):
            with self.assertRaises(PartialScrapeError):
                icims.scrape_icims(IC)

    def test_empty_states_do_not_accept_error_shells(self):
        for mod, url, empty in [(icims, IC, 'No jobs found'), (jazzhr, JAZZ, 'No open positions')]:
            self.assertEqual(mod.parse_page(soup(empty), url)[0], [])
            with self.assertRaises(ValueError):
                mod.parse_page(soup('<h1>Human Verification</h1>'), url)

    def test_jazzhr_location_and_description_without_form(self):
        html = '''<li class="list-group-item"><h3><a href="/apply/AbC123/Data-Analyst">Data Analyst</a></h3>
                  <ul><li><i class="fa-map-marker"></i>Remote</li><li>Analytics</li></ul></li>'''
        rows, nxt = jazzhr.parse_page(soup(html), JAZZ)
        self.assertEqual(rows[0]['location'], 'Remote')
        self.assertEqual(nxt, '')
        with patch.object(jazzhr, 'page', return_value=soup('<div id="job-description">Full job description</div><form>Upload resume</form>')):
            self.assertEqual(jazzhr.detail_jd(rows[0]['url']), ('Full job description', ''))
            self.assertEqual(score_jobs.detail_jd(rows[0]['url']), (rows[0]['url'], 'Full job description', ''))

    def test_icims_jsonld_description_and_date(self):
        job = {'@type': 'JobPosting', 'description': '<h2>Responsibilities</h2><p>Build systems.</p>', 'datePosted': '2026-09-01T10:00:00Z'}
        html = '<script type="application/ld+json">' + json.dumps({'@graph': [job]}) + '</script><form>Log in</form>'
        with patch.object(icims, 'page', return_value=soup(html)):
            jd, date = icims.detail_jd(IC)
            self.assertIn('Build systems.', jd)
            self.assertNotIn('Log in', jd)
            self.assertEqual(date, '2026-09-01')

    def test_icims_all_description_sections_without_talent_network(self):
        html = '<div class="iCIMS_JobContent"><h2>Overview</h2>Build.</div><div class="iCIMS_JobContent"><h2>Qualifications</h2>Five years.</div><div class="iCIMS_JobContent"><h2>Connect With Us!</h2>Join our network.</div>'
        with patch.object(icims, 'page', return_value=soup(html)):
            text, _ = icims.detail_jd(IC)
        self.assertIn('Five years.', text)
        self.assertNotIn('network', text)

    def test_next_page_cannot_change_tenant(self):
        with patch.object(jazzhr, 'page', return_value=soup('''<li class="list-group-item"><h3><a href="/apply/A1/Analyst">Analyst</a></h3></li>
                    <a rel="next" href="https://other.applytojob.com/apply?page=2">Next</a>''')) as fetch:
            with self.assertRaises(PartialScrapeError) as ctx:
                jazzhr.scrape_jazzhr(JAZZ)
            self.assertEqual(len(ctx.exception.rows), 1)
            self.assertEqual(fetch.call_count, 1)


class CornerstoneTests(unittest.TestCase):
    URL = 'https://example.csod.com/ux/ats/careersite/4/home'
    CONTEXT = {'token': 'public-visitor', 'corp': 'example', 'cultureID': 1, 'cultureName': 'en-US',
               'endpoints': {'cloud': 'https://us.api.csod.com/'}}

    def response(self, value):
        from requests import Response
        r = Response()
        r.status_code = 200
        r._content = json.dumps(value).encode()
        return r

    def test_anonymous_context_is_bound_to_csod(self):
        self.assertEqual(scraper.detect_board(self.URL + '/requisition/7')[:2], (self.URL, 'cornerstone'))
        context = dict(self.CONTEXT, endpoints={'cloud': 'https://outside.example/'})
        response = self.response({})
        response._content = ('csod.context=' + json.dumps(context) + ';').encode()
        with patch.object(scraper, '_safe_get', return_value=response):
            with self.assertRaises(ValueError):
                cs.context(self.URL)

    def test_pagination_null_date_filter_and_partial_failures(self):
        one = self.response({'status': 'Success', 'data': {'totalCount': 2, 'requisitions': [
            {'requisitionId': 7, 'displayJobTitle': 'Analyst', 'postingEffectiveDate': '9/11/2026', 'locations': [{'city': 'Boston', 'state': 'MA', 'country': 'US'}]}]}})
        two = self.response({'status': 'Success', 'data': {'totalCount': 2, 'requisitions': [
            {'requisitionId': 8, 'displayJobTitle': 'Manager', 'locations': []}]}})
        with patch.object(cs, 'context', return_value=(self.URL, self.CONTEXT, 'https://us.api.csod.com')):
            with patch.object(scraper, '_safe_post', side_effect=[one, two]) as post:
                rows = cs.scrape_cornerstone(self.URL)
                self.assertIsNone(post.call_args.args[1]['postingsWithinDays'])
                self.assertEqual(rows[0]['found_date'], '2026-09-11')
                self.assertEqual(rows[0]['location'], 'Boston, MA, US')
                self.assertEqual(len(rows), 2)
            with patch.object(scraper, '_safe_post', side_effect=[one, RuntimeError('503')]):
                with self.assertRaises(PartialScrapeError) as ctx:
                    cs.scrape_cornerstone(self.URL)
                self.assertEqual(len(ctx.exception.rows), 1)

    def test_detail_token_is_not_forwarded_by_redirect(self):
        r = self.response({})
        r.status_code = 302
        with patch.object(cs, 'context', return_value=(self.URL, self.CONTEXT, 'https://us.api.csod.com')):
            with patch.object(scraper.SESSION, 'get', return_value=r) as get:
                with self.assertRaises(ValueError):
                    cs.detail_jd(self.URL + '/requisition/7')
                self.assertFalse(get.call_args.kwargs['allow_redirects'])


if __name__ == '__main__':
    unittest.main()
