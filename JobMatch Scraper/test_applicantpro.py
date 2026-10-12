import copy
import json
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import requests

import core
import db
from scraper import applicantpro as ap

BOARD = 'https://example.applicantpro.com/jobs/'
PAGE = '''domainId : 12, getParams : {"isInternal":0}, domainName : "applicantpro.com";
courierCurrentRouteData = {"domain_id":"12","career_site_name":"Example"};'''

# IDs, context, nested bootstrap shape, envelope, and date format from the
# public Metas Solutions posting 1879158 captured on 2026-10-12. JD text is
# condensed here; the complete nine captured HTML/API pairs are replayed offline.
DETAIL_URL = 'https://metassolutions.isolvedhire.com/jobs/1879158'
DETAIL_API = 'https://metassolutions.isolvedhire.com/core/jobs/2988/1879158/job-details'
DETAIL_PAGE = '''<title>Senior Data Scientist - Remote, OR - Metas Solutions Jobs</title>
<script>
mountingData.courierCurrentRouteData = {"domain_id":"2988","career_site_name":"Metas Solutions","user_id":0};
window.bootstrapVue("#inline_auth", ['InlineAuth'], { componentData: { domainId : 9999, jobListingId : 9999, }});
window.bootstrapVue("#job_detail", ['JobDetail'], { componentData: {
token : "", organizationId : 2361, domainId : 2988, domainTitle : "Metas Solutions", jobListingId : 1879158,
jobInfo : {"mdiMapMarker":"Remote, OR, USA","mdiInbox":"Full Time"}, siteCss : {"accent":"#414956"}, }});
</script>'''
DETAIL_DATA = {'success': True, 'data': {
    'id': 1879158, 'siteId': 2988, 'title': 'Senior Data Scientist',
    'description': '<p><strong>Metas Solutions</strong> is seeking an experienced Senior Data Scientist.</p>'
                   '<h2>Requirements</h2><ul><li>Advanced analytics and statistical experience.</li>'
                   '<li>Final full-description requirement.</li></ul>',
    'advertisingDescription': 'A shortened advertising snippet.', 'startDateRef': '06-Oct-2026'}}


def detail_responses(page=DETAIL_PAGE, data=None, api_url=DETAIL_API, status=200):
    data = copy.deepcopy(DETAIL_DATA) if data is None else data
    def check_status():
        if status >= 400:
            raise requests.HTTPError('public detail unavailable')
    return [SimpleNamespace(url=DETAIL_URL, text=page, raise_for_status=lambda: None),
            SimpleNamespace(url=api_url, json=lambda: data, raise_for_status=check_status)]


def payload():
    return {'success': True, 'data': {'jobCount': 1, 'jobs': [
        {'siteId': 12, 'title': 'Data Engineer', 'jobUrl': BOARD+'123', 'jobLocation': 'Austin, TX, USA',
         'startDateRef': 'Sep 18, 2026', 'endDateRef': 'Oct 18, 2026'}]}}


class ApplicantProTests(unittest.TestCase):
    def test_actual_posting_start_date_and_tenant(self):
        rows = ap.parse_jobs(payload(), BOARD, '12', 'Example')
        self.assertEqual(rows[0]['date_posted'], '2026-09-18')
        self.assertEqual(rows[0]['location'], 'Austin, TX, USA')
        self.assertEqual(rows[0]['company'], 'Example')

    def test_listing_decodes_employer_and_title_without_changing_url_or_count(self):
        page = PAGE.replace('"career_site_name":"Example"',
                            '"career_site_name":"R&amp;B Wholesale Distributors, LLC"')
        data = payload()
        data['data']['jobs'][0]['title'] = 'Contracts &amp; Program Coordinator - NASA SEWP'
        original = copy.deepcopy(data)
        responses = [SimpleNamespace(url=BOARD, text=page, raise_for_status=lambda: None),
                     SimpleNamespace(json=lambda: data, raise_for_status=lambda: None)]
        with patch('scraper._safe_get', side_effect=responses):
            rows = ap.scrape_applicantpro(BOARD)
        self.assertEqual(rows[0]['company'], 'R&B Wholesale Distributors, LLC')
        self.assertEqual(rows[0]['title'], 'Contracts & Program Coordinator - NASA SEWP')
        self.assertEqual(rows[0]['url'], original['data']['jobs'][0]['jobUrl'])
        self.assertEqual(len(rows), original['data']['jobCount'])
        self.assertEqual(data, original)

    def test_count_mismatch_retains_rows(self):
        data = payload()
        data['data']['jobCount'] = 2
        with self.assertRaises(ap.PartialScrapeError) as exc:
            ap.parse_jobs(data, BOARD, '12', 'Example')
        self.assertEqual(len(exc.exception.rows), 1)

    def test_wrong_employer_or_internal_feed_is_rejected(self):
        for change in ('site', 'host'):
            data = payload()
            if change == 'site':
                data['data']['jobs'][0]['siteId'] = 13
            else:
                data['data']['jobs'][0]['jobUrl'] = 'https://other.applicantpro.com/jobs/123'
            with self.assertRaises(ap.PartialScrapeError):
                ap.parse_jobs(data, BOARD, '12', 'Example')
        with self.assertRaises(ValueError):
            ap.listing_config(PAGE.replace('"isInternal":0', '"isInternal":1'))

    def test_host_boundaries_and_explicit_tenant_path(self):
        self.assertEqual(ap.board_url('https://www.applicantpro.com/openings/example/jobs/123'), BOARD)
        self.assertEqual(ap.board_url('https://example.isolvedhire.com/jobs/123-4.html'), 'https://example.isolvedhire.com/jobs/')
        for url in ('https://applicantpro.com/jobs/', 'https://example.applicantpro.com.evil.test/jobs/', 'https://example.com/jobs/'):
            with self.assertRaises(ValueError):
                ap.board_url(url)

    def test_listing_requests_public_config_and_count(self):
        responses = [SimpleNamespace(url=BOARD, text=PAGE, raise_for_status=lambda: None),
                     SimpleNamespace(json=payload, raise_for_status=lambda: None)]
        with patch('scraper._safe_get', side_effect=responses) as get:
            self.assertEqual(len(ap.scrape_applicantpro(BOARD)), 1)
        self.assertEqual(get.call_args[0][0], 'https://example.applicantpro.com/core/jobs/12')

    def test_runtime_detection_probe_and_scorer_contract(self):
        import scraper
        from scraper import score_jobs
        self.assertEqual(scraper.detect_board(BOARD+'123'), (BOARD, 'applicantpro', ''))
        self.assertIs(scraper.SCRAPERS['applicantpro'], ap.scrape_applicantpro)
        with patch.object(scraper, 'scrape_applicantpro', return_value=[{}]):
            self.assertEqual(scraper.probe_board(BOARD, 'applicantpro'), 1)
        with patch.object(ap, 'detail_jd', return_value=('A full description', '2026-09-18')):
            self.assertEqual(score_jobs.detail_jd(BOARD+'123'), (BOARD+'123', 'A full description', '2026-09-18'))

    def test_vue_full_description_uses_exact_public_job_component(self):
        from scraper import score_jobs
        expected = ('Metas Solutions\nis seeking an experienced Senior Data Scientist.\n'
                    'Requirements\nAdvanced analytics and statistical experience.\nFinal full-description requirement.')
        with patch('scraper._safe_get', side_effect=detail_responses()) as get:
            self.assertEqual(score_jobs.detail_jd(DETAIL_URL), (DETAIL_URL, expected, '2026-10-06'))
        self.assertEqual([call.args[0] for call in get.call_args_list], [DETAIL_URL, DETAIL_API])
        self.assertEqual(get.call_args.kwargs['headers']['Accept'], 'application/json')
        self.assertNotIn('Authorization', get.call_args.kwargs['headers'])

    def test_vue_listing_and_employer_identity_must_match(self):
        mutations = [('id', 1879159), ('siteId', 2989), ('id', True),
                     ('title', 'Different Data Scientist'), ('title', None),
                     ('description', None), ('description', ['not HTML']), ('description', '<p></p>')]
        for key, value in mutations:
            with self.subTest(key=key, value=value):
                data = copy.deepcopy(DETAIL_DATA)
                data['data'][key] = value
                with patch('scraper._safe_get', side_effect=detail_responses(data=data)):
                    self.assertEqual(ap.detail_jd(DETAIL_URL), ('', ''))
        # A second posting may have the identical title; its ID still cannot pass.
        with patch('scraper._safe_get', side_effect=detail_responses(data={
                'success': True, 'data': dict(DETAIL_DATA['data'], id=1879159)})):
            self.assertEqual(ap.detail_jd(DETAIL_URL), ('', ''))

    def test_vue_malformed_failed_and_inaccessible_api_stays_pending(self):
        for data in ([], {}, {'success': 'true', 'data': DETAIL_DATA['data']},
                     {'success': False}, {'success': True, 'data': []}):
            with self.subTest(data=data), patch('scraper._safe_get', side_effect=detail_responses(data=data)):
                self.assertEqual(ap.detail_jd(DETAIL_URL), ('', ''))
        for status in (403, 429, 500):
            with self.subTest(status=status), patch('scraper._safe_get', side_effect=detail_responses(status=status)):
                self.assertEqual(ap.detail_jd(DETAIL_URL), ('', ''))
        responses = detail_responses()
        responses[1].json = lambda: (_ for _ in ()).throw(ValueError('not JSON'))
        with patch('scraper._safe_get', side_effect=responses):
            self.assertEqual(ap.detail_jd(DETAIL_URL), ('', ''))
        with patch('scraper._safe_get', side_effect=[detail_responses()[0], requests.Timeout('offline timeout')]):
            self.assertEqual(ap.detail_jd(DETAIL_URL), ('', ''))

    def test_vue_invalid_bootstrap_never_requests_another_job(self):
        pages = [DETAIL_PAGE.replace('jobListingId : 1879158', 'jobListingId : 1879159'),
                 DETAIL_PAGE.replace('"domain_id":"2988"', '"domain_id":"2989"'),
                 DETAIL_PAGE.replace('"user_id":0', '"user_id":1'),
                 DETAIL_PAGE.replace('"career_site_name":"Metas Solutions"', '"career_site_name":"Other Employer"'),
                 DETAIL_PAGE.replace(' - Metas Solutions Jobs</title>', ' - Other Employer Jobs</title>'),
                 DETAIL_PAGE.replace('"#job_detail"', '"#other_widget"')]
        for key, value in (('domainId', '2988.2'), ('domainId', '+2988'), ('domainId', '-2988'),
                           ('domainId', '2988e2'), ('jobListingId', '1879158.0'),
                           ('jobListingId', '1879158 + 1')):
            old = key + ' : ' + ('2988' if key == 'domainId' else '1879158')
            pages.append(DETAIL_PAGE.replace(old, key + ' : ' + value))
        for page in pages:
            with self.subTest(page=page), patch('scraper._safe_get', side_effect=detail_responses(page=page)) as get:
                self.assertEqual(ap.detail_jd(DETAIL_URL), ('', ''))
                self.assertEqual(get.call_count, 1)

    def test_vue_redirect_cannot_change_job_or_employer(self):
        for url in ('https://other.isolvedhire.com/jobs/1879158', DETAIL_URL.replace('1879158', '1879159')):
            responses = detail_responses()
            responses[0].url = url
            with self.subTest(url=url), patch('scraper._safe_get', side_effect=responses) as get:
                with self.assertRaises(ValueError):
                    ap.detail_jd(DETAIL_URL)
                self.assertEqual(get.call_count, 1)
        with patch('scraper._safe_get', side_effect=detail_responses(api_url='https://other.isolvedhire.com/login')):
            self.assertEqual(ap.detail_jd(DETAIL_URL), ('', ''))

    def test_legacy_jsonld_keeps_description_and_date_without_api(self):
        legacy = '<script type="application/ld+json">' + json.dumps({
            '@type': 'JobPosting', 'description': '<h2>Legacy responsibilities</h2><p>Full legacy description.</p>',
            'datePosted': '2026-09-18T10:00:00Z'}) + '</script>'
        for url in ('https://example.applicantpro.com/jobs/123-4.html',
                    'https://www.applicantpro.com/openings/example/jobs/123'):
            response = SimpleNamespace(url=url, text=legacy, raise_for_status=lambda: None)
            with self.subTest(url=url), patch('scraper._safe_get', return_value=response) as get:
                self.assertEqual(ap.detail_jd(url), ('Legacy responsibilities\nFull legacy description.', '2026-09-18'))
                get.assert_called_once()

    def test_vue_posting_date_does_not_use_closing_date(self):
        for start, expected in (('Sep 18, 2026', '2026-09-18'), ('2026-09-18', '2026-09-18'), ('unknown', '')):
            data = copy.deepcopy(DETAIL_DATA)
            data['data'].update(startDateRef=start, endDateRef='2026-12-31')
            with self.subTest(start=start), patch('scraper._safe_get', side_effect=detail_responses(data=data)):
                self.assertEqual(ap.detail_jd(DETAIL_URL)[1], expected)

    def test_tcs_exception_keeps_other_agencies_and_blocks(self):
        for name in ('TCS', 'Tata Consultancy Services', 'Tata Consultancy Services Limited', 'Tata Consultancy Services Ltd.'):
            self.assertFalse(core.is_agency(name))
        for name in ('Example Consultancy Services', 'Kforce Inc', 'Tata Consultancy Services Staffing LLC'):
            self.assertTrue(core.is_agency(name))
        self.assertTrue(db.is_blocked('Tata Consultancy Services', {'tata consultancy services'}))


if __name__ == '__main__':
    unittest.main()
