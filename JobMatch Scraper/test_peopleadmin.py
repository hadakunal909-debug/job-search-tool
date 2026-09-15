import unittest
from types import SimpleNamespace
from unittest.mock import patch
from scraper import peopleadmin

URL = 'https://jobs.example.edu/postings/search'
PAGE = '<h2>Search Postings <span>(1)</span></h2><a href="/postings/all_jobs.atom">Feed</a>'
FEED = '''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:pa="jobs.example.edu">
<entry><title>Data Analyst</title><link rel="alternate" href="https://jobs.example.edu/postings/42"/>
<published>2026-09-15T12:00:00Z</published><content>&lt;p&gt;Analyze data&lt;/p&gt;</content>
<pa:city>New Brunswick</pa:city><pa:state>NJ</pa:state></entry></feed>'''


class PeopleAdminTests(unittest.TestCase):
    def response(self, text):
        return SimpleNamespace(text=text, content=text.encode(), url=URL, raise_for_status=lambda: None)

    def test_feed_includes_location_date_and_description(self):
        with patch('scraper._safe_get', side_effect=[self.response(PAGE), self.response(FEED)]):
            rows = peopleadmin.scrape_peopleadmin(URL)
        self.assertEqual(rows[0]['location'], 'New Brunswick, NJ')
        self.assertEqual(rows[0]['description'], 'Analyze data')
        self.assertEqual(rows[0]['date_posted'], '2026-09-15')

    def test_count_mismatch_retains_rows(self):
        with patch('scraper._safe_get', side_effect=[self.response(PAGE.replace('(1)', '(2)')), self.response(FEED)]):
            with self.assertRaises(peopleadmin.PartialScrapeError) as error:
                peopleadmin.scrape_peopleadmin(URL)
        self.assertEqual(len(error.exception.rows), 1)

    def test_external_feed_and_non_feed_response_are_rejected(self):
        self.assertEqual(peopleadmin.listing_info(PAGE.replace('/postings/all_jobs.atom', 'https://other.edu/postings/all_jobs.atom'), URL)[0], '')
        with self.assertRaises(ValueError):
            peopleadmin.parse_feed('<html/>', URL)

    def test_lagging_feed_is_filled_from_visible_listing(self):
        page = PAGE.replace('(1)', '(2)') + '<div class="job-item-posting"><div class="job-title"><a href="/postings/43">New Job</a></div></div>'
        with patch('scraper._safe_get', side_effect=[self.response(page), self.response(FEED)]):
            rows = peopleadmin.scrape_peopleadmin(URL)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]['title'], 'New Job')


if __name__ == '__main__':
    unittest.main()
