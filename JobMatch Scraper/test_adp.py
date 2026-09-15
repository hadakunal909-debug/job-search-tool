"""Offline tests for public ADP listings, pagination and description identity."""
import os
os.environ["EV_OFF"] = "1"
import unittest
from unittest.mock import patch
import scraper
from scraper import adp
from scraper.infosys import PartialScrapeError

URL = "https://workforcenow.adp.com" + adp.PAGE + "?cid=tenant-1234&ccId=external&jobId=999&source=IN"


def job(jid):
    return {"clientRequisitionID": "internal-id", "requisitionTitle": "Data Engineer",
            "postDate": "2026-09-15T12:00:00Z", "requisitionDescription": "<p>Build reliable data systems.</p>",
            "customFieldGroup": {"stringFields": [{"nameCode": {"codeValue": "ExternalJobID"}, "stringValue": str(jid)}]},
            "requisitionLocations": [{"nameCode": {"shortName": "Austin, TX, US"}}]}


class ADPTests(unittest.TestCase):
    def test_pipeline_probe_detail_and_tenant_ownership(self):
        from scraper import score_jobs
        with patch.object(adp, "listing_page", return_value=([job(999)], 27)):
            self.assertEqual(scraper.probe_board(URL, "adp"), 27)
        with patch.object(adp, "detail_jd", return_value=("Actual description", "2026-09-15")):
            self.assertEqual(score_jobs.detail_jd(URL), (URL, "Actual description", "2026-09-15"))
        self.assertTrue(adp.owns_url(URL, URL.replace("jobId=999", "jobId=1000")))
        self.assertFalse(adp.owns_url(URL, URL.replace("tenant-1234", "other-12345")))
        self.assertFalse(adp.owns_url(URL, URL.replace("ccId=external", "ccId=internal")))

    def test_requests_do_not_reuse_another_tenants_cookie(self):
        with patch.object(scraper.SESSION, "get") as get:
            get.return_value.json.return_value = {}
            adp.request_json(URL, "job-requisitions")
        self.assertEqual(get.call_args.kwargs["headers"]["Cookie"], "")
        self.assertEqual(get.call_args.kwargs["params"]["cid"], "tenant-1234")

    def test_tenant_and_external_center_survive_normalization(self):
        board, ats, _ = scraper.detect_board(URL)
        self.assertEqual(ats, "adp")
        self.assertEqual(board, URL.split("&jobId")[0])
        self.assertIsNone(scraper.detect_board(URL.replace(".adp.com", ".adp.com.evil.test")))
        self.assertIsNone(scraper.detect_board(URL.replace("&ccId=external", "")))
        self.assertEqual(adp.board_url(URL.replace("&", "&amp;")), board)

    def test_careers_page_link_is_discovered(self):
        with patch.object(scraper, "_safe_get") as get:
            get.return_value.status_code = 200
            get.return_value.url = "https://example.test/careers"
            get.return_value.text = '<a href="' + URL.replace('&', '&amp;') + '">Jobs</a>'
            hit = scraper.detect_linked_ats("https://example.test/careers")
        self.assertEqual(hit[:2], (adp.board_url(URL), "adp"))

    def test_pagination_uses_actual_page_length_and_public_job_id(self):
        with patch.object(adp, "listing_page", side_effect=[([job(1), job(2)], 3), ([job(3)], 3)]) as get:
            rows = adp.scrape_adp(URL)
        self.assertEqual(get.call_args_list[1].args[1], 2)
        self.assertEqual(len(rows), 3)
        self.assertTrue(rows[0]["url"].endswith("&jobId=1"))
        self.assertEqual(rows[0]["found_date"], "2026-09-15")
        self.assertEqual(rows[0]["location"], "Austin, TX, US")

    def test_failed_or_repeated_later_page_preserves_partial_rows(self):
        for later in [RuntimeError("HTTP 503"), ([job(1)], 2), ([], 2)]:
            with patch.object(adp, "listing_page", side_effect=[([job(1)], 2), later]):
                with self.assertRaises(PartialScrapeError) as caught:
                    adp.scrape_adp(URL)
            self.assertEqual(len(caught.exception.rows), 1)

    def test_empty_requires_explicit_zero_total(self):
        with patch.object(adp, "request_json", return_value={"jobRequisitions": [], "meta": {"totalNumber": 0}}):
            self.assertEqual(adp.scrape_adp(URL), [])
        with patch.object(adp, "request_json", return_value={}):
            with self.assertRaises(PartialScrapeError):
                adp.scrape_adp(URL)

    def test_detail_rejects_another_posting(self):
        with patch.object(adp, "request_json", return_value=job(999)):
            text, date = adp.detail_jd(URL)
            self.assertIn("Build reliable data systems.", text)
            self.assertEqual(date, "2026-09-15")
        with patch.object(adp, "request_json", return_value=job(123)):
            self.assertEqual(adp.detail_jd(URL), ("", ""))


if __name__ == "__main__":
    unittest.main()
