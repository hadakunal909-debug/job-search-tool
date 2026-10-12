"""Native full-listing JD retry paths preserve canonical keys and visible failures."""
import os
import unittest
from unittest.mock import Mock, patch

os.environ["EV_OFF"] = "1"
import scraper
from scraper import score_jobs
from scraper.infosys import PartialScrapeError

CASES = (
    ("wordpressjobs", "https://www.aggteam.com/careers/", "https://www.aggteam.com/jobs/project-manager/"),
    ("darwinbox", "https://acme.darwinbox.com/ms/candidatev2/main/careers/allJobs",
     "https://acme.darwinbox.com/ms/candidatev2/main/careers/jobDetails/role-123"),
)


class ListingJDBackfillTests(unittest.TestCase):
    def test_native_listing_descriptions_use_normal_canonical_keys(self):
        for ats, board, url in CASES:
            jd = "Full employer requirements and responsibilities, directly from its public listing."
            adapter = Mock(return_value=[{"url": url, "jd": jd}])
            with patch.dict(scraper.SCRAPERS, {ats: adapter}):
                result = score_jobs.jd_map_for(board, ats)
            self.assertEqual(result[scraper.canonical_url(url)], jd)
            self.assertEqual(result[url], jd)
            adapter.assert_called_once_with(board)
            self.assertIn(ats, score_jobs.BULK_JD_ATS)

    def test_needed_urls_filter_the_map_after_canonicalization(self):
        for ats, board, url in CASES:
            other = url.replace("project-manager", "other-role").replace("role-123", "other-role")
            with patch.dict(scraper.SCRAPERS, {ats: Mock(return_value=[{"url": url, "jd": "Needed description"},
                                                                     {"url": other, "jd": "Other description"}])}):
                result = score_jobs.jd_map_for(board, ats, {scraper.canonical_url(url)})
            self.assertEqual(result[scraper.canonical_url(url)], "Needed description")
            self.assertNotIn(scraper.canonical_url(other), result)

    def test_failed_or_partial_feed_is_never_a_successful_empty_map(self):
        for ats, board, url in CASES:
            failure = PartialScrapeError("advertised catalog is incomplete", [{"url": url, "jd": "Readable partial"}])
            with patch.dict(scraper.SCRAPERS, {ats: Mock(side_effect=failure)}):
                with self.assertRaises(PartialScrapeError) as caught:
                    score_jobs.jd_map_for(board, ats)
            self.assertIs(caught.exception, failure)

    def test_explicit_no_targets_skips_network_and_verified_empty_can_return_empty(self):
        for ats, board, _ in CASES:
            adapter = Mock(return_value=[])
            with patch.dict(scraper.SCRAPERS, {ats: adapter}):
                self.assertEqual(score_jobs.jd_map_for(board, ats, set()), {})
                adapter.assert_not_called()
                self.assertEqual(score_jobs.jd_map_for(board, ats), {})

    def test_board_scope_only_fetches_backlogs_for_the_exact_employer(self):
        for ats, board, url in CASES:
            self.assertTrue(score_jobs._board_has_missing(board, ats, {scraper.canonical_url(url)}))
            self.assertFalse(score_jobs._board_has_missing(board, ats, {url.replace("acme.darwinbox.com", "other.darwinbox.com")
                                                                      .replace("www.aggteam.com", "www.aggteam.com.evil.test")}))
            self.assertFalse(score_jobs._board_has_missing(board, ats, {"https://other.test/jobs/role?" + url}))


if __name__ == "__main__":
    unittest.main()
