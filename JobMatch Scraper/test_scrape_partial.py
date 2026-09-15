"""Incomplete boards retain retrieved jobs and never authorize closure reconciliation."""
import unittest
import contextlib
import io
from unittest.mock import patch
import scraper
from scraper.infosys import PartialScrapeError


class PartialRows(list):
    complete = False
    incomplete_reason = "page 2 failed"


class PartialScrapeTests(unittest.TestCase):
    def check_result(self, fn, expected):
        health = []
        with patch.dict(scraper.SCRAPERS, {"partial_fixture": fn}), patch.object(scraper.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
            rows = scraper.scrape_all([("https://example.com/jobs", "partial_fixture", "Example")],
                                      workers=1, board_results=health, budget_min=0)
        self.assertEqual(len(rows), expected)
        self.assertFalse(health[0]["ok"])
        self.assertFalse(health[0]["skipped"])
        self.assertTrue(health[0]["err"])
        if rows:
            self.assertEqual(rows[0]["company"], "Example")

    def test_exception_retains_completed_pages(self):
        def fetch(url):
            raise PartialScrapeError("page 2 failed", [{"url": "https://example.com/job/1", "title": "Engineer"}])
        self.check_result(fetch, 1)

    def test_partial_list_retains_jobs(self):
        self.check_result(lambda url: PartialRows([{"url": "https://example.com/job/1"}]), 1)

    def test_empty_partial_list_is_not_success(self):
        self.check_result(lambda url: PartialRows(), 0)

    def test_timed_adapter_preserves_empty_result_metadata(self):
        result = scraper._run_with_timeout(lambda url: PartialRows(), "https://example.com", 2)
        self.assertFalse(result.complete)


if __name__ == "__main__":
    unittest.main()
