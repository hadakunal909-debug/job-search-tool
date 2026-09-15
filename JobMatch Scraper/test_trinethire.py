import os
os.environ['EV_OFF'] = '1'
import unittest
from unittest.mock import patch
from bs4 import BeautifulSoup
import scraper
from scraper import trinethire as th

URL = 'https://app.trinethire.com/companies/123-example/jobs'


class TriNetTests(unittest.TestCase):
    def test_employer_and_table_contract(self):
        self.assertEqual(scraper.detect_board(URL + '/456-analyst')[1], 'trinethire')
        self.assertIsNone(scraper.detect_board(URL.replace('.com/', '.com.evil.test/')))
        html = '<table class="job-list"><tr class="job"><td><a href="/companies/123-example/jobs/456-analyst">Data Analyst</a></td><td class="location">Austin, TX</td></tr></table>'
        row = th.parse_jobs(html, URL)[0]
        self.assertEqual(row['location'], 'Austin, TX')
        self.assertEqual(row['url'], URL + '/456-analyst')
        self.assertEqual(th.parse_jobs('<table class="job-list"></table>', URL), [])
        with self.assertRaises(ValueError):
            th.parse_jobs('<html>Unavailable</html>', URL)
        with self.assertRaises(ValueError):
            th.parse_jobs(html + '<a rel="next">Next</a>', URL)

    def test_description_omits_application_form(self):
        html = '<div class="job-descr content">Build systems.</div><form>Upload your resume</form>'
        with patch.object(th, 'page', return_value=BeautifulSoup(html, 'html.parser')):
            self.assertEqual(th.detail_jd(URL + '/456-analyst'), ('Build systems.', ''))
        from scraper import score_jobs
        with patch.object(th, 'detail_jd', return_value=('Full description', '')):
            self.assertEqual(score_jobs.detail_jd(URL + '/456-analyst'), (URL + '/456-analyst', 'Full description', ''))


if __name__ == '__main__':
    unittest.main()
