"""Offline contract tests for Gem's public board and description query."""
import copy
import os
import unittest
from unittest.mock import Mock, patch

os.environ["EV_OFF"] = "1"
from scraper import gem
from scraper.infosys import PartialScrapeError

URL = "https://jobs.gem.com/rex"
POST_ID = "am9icG9zdDo2qVK-phN34PIkhkl66hfF"


def posting(ext_id=POST_ID):
    return {"extId": ext_id, "title": "Associate, Strategic Projects",
            "locations": [{"name": "Austin, Texas", "isoCountry": "USA"},
                          {"name": "Toronto", "isoCountry": "CAN"}],
            "descriptionHtml": "<h2>Responsibilities</h2><p>Deliver complex projects.</p>",
            "compensationHtml": "<p>$50,000 - $80,000</p>",
            "jobPostSectionHtml": {"introHtml": "<p>About Rex.</p>",
                                   "outroHtml": "<p>Equal opportunity.</p>"},
            "firstPublishedTsSec": 1757254133}


def listing(posts=None):
    return {"data": {"oatsExternalJobPostings": {"jobPostings": [posting()] if posts is None else posts},
                     "jobBoardExternal": {"teamDisplayName": "Rex", "pageTitle": "Rex Careers"}}}


class GemTests(unittest.TestCase):
    def test_board_identity_is_scoped_and_tracking_does_not_change_it(self):
        self.assertEqual(gem.board_url(URL + "/" + POST_ID + "/application?source=link"), URL)
        self.assertTrue(gem.owns_url(URL, URL + "/" + POST_ID))
        self.assertFalse(gem.owns_url(URL, "https://jobs.gem.com/other/" + POST_ID))
        for bad in ("https://jobs.gem.com.evil.test/rex", "https://jobs.gem.com@evil.test/rex",
                    "javascript://jobs.gem.com/rex", "https://jobs.gem.com:8443/rex",
                    URL + "/../../secret", "https://jobs.gem.com/rex%2fother"):
            with self.subTest(url=bad), self.assertRaises(ValueError):
                gem.board_url(bad)

    def test_full_listing_preserves_every_posting_and_country(self):
        rows = gem.parse_listing(listing([posting("post_%d" % i) for i in range(240)]), URL)
        self.assertEqual(len(rows), 240)
        self.assertEqual(rows[0]["company"], "Rex")
        self.assertEqual(rows[0]["found_date"], "2025-09-07")
        self.assertEqual(rows[0]["location"], "Austin, Texas, USA | Toronto, CAN")
        self.assertIn("$50,000 - $80,000", rows[0]["jd"])
        self.assertEqual(gem.parse_listing(listing([]), URL), [])
        no_date = posting()
        no_date["firstPublishedTsSec"] = None
        self.assertNotIn("found_date", gem.parse_listing(listing([no_date]), URL)[0])

    def test_failure_preserves_valid_rows_and_never_reports_complete(self):
        cases = []
        bad_row = listing([posting(), {"title": "Missing public ID"}])
        cases.append(bad_row)
        errored = listing()
        errored["errors"] = [{"message": "A resolver failed"}]
        cases.append(errored)
        for fields in ({"pageInfo": {"hasNextPage": True}}, {"nextCursor": "abc"},
                       {"nextPage": 2}, {"totalCount": 2}):
            paged = listing()
            paged["data"]["oatsExternalJobPostings"].update(fields)
            cases.append(paged)
        conflicting = posting()
        conflicting["title"] = "Different title"
        cases.append(listing([posting(), conflicting]))
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(PartialScrapeError) as err:
                gem.parse_listing(payload, URL)
            self.assertEqual(len(err.exception.rows), 1)
            self.assertEqual(err.exception.rows[0]["url"], URL + "/" + POST_ID)
        self.assertEqual(len(gem.parse_listing(listing([posting(), copy.deepcopy(posting())]), URL)), 1)

    def test_missing_listing_is_failure_and_safe_public_transport_is_used(self):
        for payload in ({}, {"data": None}, {"data": {"oatsExternalJobPostings": {"jobPostings": []}}}):
            with self.assertRaises(ValueError):
                gem.parse_listing(payload, URL)
        response = Mock()
        response.json.return_value = listing()
        import scraper
        with patch.object(scraper, "_safe_post", return_value=response) as post:
            self.assertEqual(len(gem.scrape_gem(URL)), 1)
            self.assertEqual(post.call_args.args[0], gem.API)
            self.assertEqual(post.call_args.args[1]["variables"], {"boardId": "rex"})
            response.raise_for_status.assert_called_once()
        with patch.object(gem, "request_json") as request, self.assertRaises(ValueError):
            gem.scrape_gem("https://evil.test/rex")
        request.assert_not_called()

    def test_description_uses_authoritative_detail_and_excludes_application_form(self):
        post = posting()
        post["descriptionHtml"] += "<script>Track applicant</script><form>Upload resume</form>"
        payload = {"data": {"oatsExternalJobPosting": post}}
        with patch.object(gem, "request_json", return_value=payload):
            jd, date = gem.detail_jd(URL + "/" + POST_ID)
        self.assertEqual(date, "2025-09-07")
        self.assertIn("About Rex.", jd)
        self.assertIn("Equal opportunity.", jd)
        self.assertNotIn("Upload resume", jd)
        self.assertNotIn("Track applicant", jd)
        wrong_post = {"data": {"oatsExternalJobPosting": posting("another")}}
        with patch.object(gem, "request_json", return_value=wrong_post), self.assertRaises(ValueError):
            gem.detail_jd(URL + "/" + POST_ID)
        with patch.object(gem, "request_json", return_value={"data": {"oatsExternalJobPosting": None}}):
            self.assertEqual(gem.detail_jd(URL + "/" + POST_ID), ("", ""))

    def test_empty_board_has_authoritative_name(self):
        with patch.object(gem, "request_json", return_value=listing([])) as request:
            self.assertEqual(gem.identity_text(URL), "Rex")
            request.assert_called_once()

    def test_app_detects_probes_and_enriches_a_pasted_posting(self):
        import scraper
        from scraper import score_jobs
        posting_url = URL + "/" + POST_ID + "?source=GemJobBoardLink"
        self.assertEqual(scraper.detect_board(posting_url), (URL, "gem", "Rex"))
        self.assertIsNone(scraper.detect_board("https://jobs.gem.com.evil.test/rex"))
        with patch.object(gem, "request_json", return_value=listing()):
            self.assertEqual(scraper.probe_board(URL, "gem"), 1)
            self.assertEqual(scraper.board_display_name(URL, "gem"), "Rex")
        with patch.object(gem, "detail_jd", return_value=("The complete description", "2025-09-07")):
            self.assertEqual(score_jobs.detail_jd(posting_url),
                             (posting_url, "The complete description", "2025-09-07"))
        html = '<a href="' + URL + '">Our jobs</a>'
        self.assertEqual(scraper._ATS_LINK_RE.search(html).group(0), URL)


if __name__ == "__main__":
    unittest.main()
