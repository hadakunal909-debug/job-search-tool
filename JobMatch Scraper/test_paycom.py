"""Offline regression tests for Paycom's public portal contract."""
import json
import os
os.environ["EV_OFF"] = "1"
import unittest
from unittest.mock import patch
import scraper
from scraper import paycom
from scraper.infosys import PartialScrapeError

URL = "https://www.paycomonline.net/v4/ats/web.php/portal/" + "AB" * 16 + "/jobs/123"


def job(jid):
    return {"jobId": jid, "jobTitle": "Data Engineer", "locations": "Austin, TX",
            "description": "Truncated preview..."}


class PaycomTests(unittest.TestCase):
    def test_pipeline_probe_and_description_tuple(self):
        from scraper import score_jobs
        with patch.object(paycom, "listing_page", return_value=([job(123)], 7)):
            self.assertEqual(scraper.probe_board(URL, "paycom"), 7)
        with patch.object(paycom, "detail_jd", return_value=("Full description", "")):
            self.assertEqual(score_jobs.detail_jd(URL), (URL, "Full description", ""))

    def test_board_identity_and_host(self):
        board, ats, _ = scraper.detect_board(URL)
        self.assertEqual(ats, "paycom")
        self.assertTrue(board.endswith("/career-page"))
        self.assertIsNone(scraper.detect_board(URL.replace(".net/", ".net.evil.test/")))
        with self.assertRaises(ValueError):
            paycom.board_url(URL.replace("AB" * 16, "unknown"))

    def test_guest_context_cannot_change_token_recipient(self):
        def html(service):
            return "var configsFromHost = " + json.dumps({"sessionJWT": "public-visitor",
                "libConfig": json.dumps({"atsPortalMantleServiceUrl": service})}) + ";"
        self.assertEqual(paycom.parse_context(html(paycom.SERVICE)), "public-visitor")
        with self.assertRaises(ValueError):
            paycom.parse_context(html("https://other.test/"))
        with patch.object(paycom, "context", return_value="public-visitor"), patch.object(scraper.SESSION, "post") as post:
            post.return_value.json.return_value = {}
            paycom.request_json(URL, "job-posting-previews/search", {})
        self.assertTrue(post.call_args.args[0].startswith(paycom.SERVICE + "api/ats/"))

    def test_pages_and_full_description_contract(self):
        with patch.object(paycom, "listing_page", side_effect=[([job(1), job(2)], 3), ([job(3)], 3)]) as get:
            rows = paycom.scrape_paycom(URL)
        self.assertEqual(len(rows), 3)
        self.assertEqual(get.call_args_list[1].args[1], 2)
        self.assertNotIn("jd", rows[0])
        self.assertNotIn("date_posted", rows[0])
        with patch.object(paycom, "request_json", return_value={"jobPosting": {
            "jobId": 123, "description": "<p>Full description</p>", "qualifications": "<p>Required skills</p>"}}):
            text, date = paycom.detail_jd(URL)
        self.assertIn("Full description", text)
        self.assertIn("Required skills", text)
        self.assertEqual(date, "")
        with patch.object(paycom, "request_json", return_value={"jobPosting": {"jobId": 999, "description": "Wrong job"}}):
            self.assertEqual(paycom.detail_jd(URL), ("", ""))

    def test_partial_page_failure_preserves_rows(self):
        for later in [RuntimeError("503"), ([job(1)], 2), ([], 2)]:
            with patch.object(paycom, "listing_page", side_effect=[([job(1)], 2), later]):
                with self.assertRaises(PartialScrapeError) as caught:
                    paycom.scrape_paycom(URL)
            self.assertEqual(len(caught.exception.rows), 1)

    def test_explicit_zero_and_duplicate_count(self):
        with patch.object(paycom, "request_json", return_value={"jobPostingPreviews": [], "jobPostingPreviewsCount": 0}):
            self.assertEqual(paycom.scrape_paycom(URL), [])
        with patch.object(paycom, "request_json", return_value={}):
            with self.assertRaises(PartialScrapeError):
                paycom.scrape_paycom(URL)
        with patch.object(paycom, "listing_page", side_effect=[([job(1), job(1)], 2), ([], 2)]):
            with self.assertRaises(PartialScrapeError) as caught:
                paycom.scrape_paycom(URL)
        self.assertEqual(len(caught.exception.rows), 1)


if __name__ == "__main__":
    unittest.main()
