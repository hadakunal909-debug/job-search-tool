"""Public Radancy pagination: complete, partial, duplicate and fallback responses."""
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import scraper
from scraper import radancy


URL = "https://jobs.example.com/search-jobs"


def html(page, ids, pages=2, total=2):
    return ('<section id="search-results" data-total-pages="%d" data-current-page="%d" '
            'data-total-job-results="%d" data-search-results-module-name="Search Results" '
            'data-ajax-url="/search-jobs/results"><ul id="search-results-list">%s</ul></section>' %
            (pages, page, total, ''.join('<li><a href="/job/city/engineer/1/%s"><h2>Engineer %s</h2>'
             '<span class="job-location">Austin, Texas</span></a></li>' % (i, i) for i in ids)))


class Response:
    url = URL
    def __init__(self, text="", payload=None):
        self.text, self.payload = text, payload
    def json(self):
        if self.payload is None:
            raise ValueError("not JSON")
        return self.payload


class RadancyTests(unittest.TestCase):
    def test_linked_listing_is_fingerprinted_once(self):
        page = SimpleNamespace(status_code=200, url='https://example.com/careers',
                               text='<a href="/search-jobs">Jobs</a>' * 5)
        with patch.object(scraper, '_safe_get', return_value=page), patch.object(scraper, 'detect_radancy', return_value=None) as detect:
            self.assertIsNone(scraper.detect_linked_ats(page.url))
        detect.assert_called_once_with('https://example.com/search-jobs')

    def test_complete_pagination_deduplicates_first_page(self):
        responses = [Response(html(1, [1])), Response(payload={"results": html(1, [1])}),
                     Response(payload={"results": html(2, [2])})]
        with patch.object(radancy, "_get", side_effect=responses):
            rows = radancy.scrape_radancy(URL)
        self.assertTrue(rows.complete)
        self.assertEqual([r["title"] for r in rows], ["Engineer 1", "Engineer 2"])

    def test_later_failure_retains_jobs_and_marks_incomplete(self):
        with patch.object(radancy, "_get", side_effect=[Response(html(1, [1])), Response(payload={"results": html(1, [1])}), RuntimeError("403")]), patch("scraper.note_truncation"):
            rows = radancy.scrape_radancy(URL)
        self.assertFalse(rows.complete)
        self.assertEqual(len(rows), 1)
        self.assertIn("page 2", rows.reason)

    def test_wrong_page_cannot_claim_completeness(self):
        with patch.object(radancy, "_get", side_effect=[Response(html(1, [1])), Response(payload={"results": html(1, [1])}), Response(payload={"results": html(1, [2])})]), patch("scraper.note_truncation"):
            rows = radancy.scrape_radancy(URL)
        self.assertFalse(rows.complete)
        self.assertEqual(len(rows), 1)

    def test_ajax_failure_falls_back_to_public_page(self):
        with patch.object(radancy, "_get", side_effect=[Response(html(1, [1])), RuntimeError("no AJAX"), Response(html(2, [2]))]):
            rows = radancy.scrape_radancy(URL)
        self.assertTrue(rows.complete)
        self.assertEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main()
