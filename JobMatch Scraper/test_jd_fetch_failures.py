"""A failed posting must not discard healthy JDs or stop analysis/persistence."""
import contextlib
import io
import unittest
from unittest.mock import patch

import requests
import scraper
import scraper.score_jobs as sj
from test_jd_persist import _JD, _run_with_fetch


class FetchFailureTests(unittest.TestCase):
    bad = "https://digitalcareers.infosys.com/global-careers/company-job/description/reqid/1"

    def test_infosys_block_is_retryable_and_does_not_parse_error_page(self):
        response = requests.Response()
        response.status_code = 403
        response.url = "https://www.infosys.com/404/?token=secret"
        response._content = b"<div class='description-page-right'>Access denied</div>"
        output = io.StringIO()
        with patch.object(scraper, "_safe_get", return_value=response), \
                patch.object(sj.core, "fetch_jd") as fallback, contextlib.redirect_stdout(output):
            self.assertEqual(sj.detail_jd(self.bad), (self.bad, "", ""))
        fallback.assert_not_called()
        self.assertIn("HTTP 403", output.getvalue())
        self.assertNotIn("secret", output.getvalue())

    def test_all_adapter_failures_are_isolated_but_process_interrupts_propagate(self):
        for failure in (requests.Timeout(), requests.ConnectionError(), ValueError("bad JSON")):
            with self.subTest(failure=type(failure).__name__), \
                    patch.object(sj, "_detail_jd", side_effect=failure):
                self.assertEqual(sj.detail_jd(self.bad), (self.bad, "", ""))
        with patch.object(sj, "_detail_jd", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                sj.detail_jd(self.bad)

    def test_failed_fetch_does_not_stop_batch_writes_and_scoring(self):
        urls = ["https://example.test/good-before", self.bad, "https://example.test/good-after"]
        rows = [{"url": u, "jd": "", "location": "Boston, MA", "found_date": "2026-09-24",
                 "first_seen": "2026-09-24", "title": "Program Manager", "company": "Example"}
                for u in urls]
        safe_detail = sj.detail_jd

        def adapter(url):
            if url == self.bad:
                raise requests.HTTPError("blocked")
            return url, _JD, "2026-09-24"

        with patch.object(sj, "_detail_jd", side_effect=adapter):
            fake, tried = _run_with_fetch(rows, {}, ["--new-only"],
                                         lambda u: safe_detail(u)[1])
        self.assertEqual(set(tried), set(urls))
        for url in (urls[0], urls[2]):
            self.assertEqual(fake.rows[url]["jd"], _JD)
            self.assertTrue(fake.rows[url]["jd_terms"])
            self.assertIsNotNone(fake.rows[url]["match_score"])
        self.assertEqual(fake.rows[self.bad]["jd"], "")
        self.assertNotIn(self.bad, fake.jd_writes)
        self.assertIn(self.bad, fake.urls_missing_jd())


if __name__ == "__main__":
    unittest.main()
