import unittest
from unittest.mock import patch
from types import SimpleNamespace

import core
import db
from scraper import applicantpro as ap

BOARD = 'https://example.applicantpro.com/jobs/'
PAGE = '''domainId : 12, getParams : {"isInternal":0}, domainName : "applicantpro.com";
courierCurrentRouteData = {"domain_id":"12","career_site_name":"Example"};'''


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

    def test_tcs_exception_keeps_other_agencies_and_blocks(self):
        for name in ('TCS', 'Tata Consultancy Services', 'Tata Consultancy Services Limited', 'Tata Consultancy Services Ltd.'):
            self.assertFalse(core.is_agency(name))
        for name in ('Example Consultancy Services', 'Kforce Inc', 'Tata Consultancy Services Staffing LLC'):
            self.assertTrue(core.is_agency(name))
        self.assertTrue(db.is_blocked('Tata Consultancy Services', {'tata consultancy services'}))


if __name__ == '__main__':
    unittest.main()
