"""TikTok public US pagination, country validation and complete JD extraction."""
import unittest
from unittest.mock import Mock, patch

import requests
import scraper
from scraper import tiktok as tt


def city(code="CT_94", country="United States of America", country_code="CN_6"):
    return {"code": code, "en_name": "Los Angeles", "parent": {
        "code": "ST_31", "en_name": "California", "parent": {"code": country_code, "en_name": country, "parent": None}}}


def job(jid="123", location=None):
    return {"id": jid, "title": "Data Engineer", "city_info": city() if location is None else location,
            "description": "Summary without salary", "requirement": "Required Python"}


class TikTokTests(unittest.TestCase):
    def test_current_us_city_codes_are_derived_from_country_hierarchy(self):
        filters = {"city_list": [city(), city("NEW_US_CITY"), city("CT_OTHER", "Canada", "CN_12")]}
        with patch.object(tt, "_request", return_value=filters):
            self.assertEqual(tt.us_city_codes(), ["CT_94", "NEW_US_CITY"])

    def test_missing_us_filters_raise_instead_of_global_or_empty(self):
        for filters in ({}, {"city_list": []}, {"city_list": [city("X", "Canada", "CN_12")]}):
            with self.subTest(filters=filters), patch.object(tt, "_request", return_value=filters):
                with self.assertRaises(ValueError):
                    tt.us_city_codes()

    def test_every_page_reuses_verified_us_scope_and_actual_offset(self):
        with patch.object(tt, "us_city_codes", return_value=["CT_94"]), patch.object(tt, "listing_page", side_effect=[([job()], 3), ([job("124"), job("125")], 3)]) as fetch:
            rows = tt.scrape_tiktok()
        self.assertEqual([call.args for call in fetch.call_args_list], [(["CT_94"], 0), (["CT_94"], 1)])
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["location"], "Los Angeles, California, United States of America")
        self.assertEqual(rows[0]["url"], "https://lifeattiktok.com/search/123")
        self.assertNotIn("jd", rows[0])
        self.assertNotIn("found_date", rows[0])

    def test_request_is_unfiltered_except_for_us_locations(self):
        with patch.object(tt, "_request", return_value={"job_post_list": [], "count": 0}) as fetch:
            self.assertEqual(tt.listing_page(["CT_94"], 100), ([], 0))
        self.assertEqual(fetch.call_args.args[1], {"keyword": "", "limit": 100, "offset": 100,
                         "recruitment_id_list": [], "location_code_list": ["CT_94"]})

    def test_duplicate_empty_or_failed_later_page_retains_jobs(self):
        for later in [([job()], 2), ([], 2), requests.Timeout()]:
            with self.subTest(later=later), patch.object(tt, "us_city_codes", return_value=["CT_94"]), patch.object(tt, "listing_page", side_effect=[([job()], 2), later]):
                with self.assertRaises(tt.PartialScrapeError) as raised:
                    tt.scrape_tiktok()
            self.assertEqual(len(raised.exception.rows), 1)

    def test_filter_ignored_or_bad_posting_is_partial(self):
        for bad in (job("bad/id"), job("124", city("X", "Canada", "CN_12")), job("124", {})):
            with self.subTest(bad=bad), patch.object(tt, "us_city_codes", return_value=["CT_94"]), patch.object(tt, "listing_page", return_value=([job(), bad], 2)):
                with self.assertRaises(tt.PartialScrapeError) as raised:
                    tt.scrape_tiktok()
            self.assertEqual(len(raised.exception.rows), 1)

    def test_zero_and_safety_cap(self):
        with patch.object(tt, "us_city_codes", return_value=["CT_94"]), patch.object(tt, "listing_page", return_value=([], 0)):
            self.assertEqual(tt.scrape_tiktok(), [])
        with patch.object(tt, "us_city_codes", return_value=["CT_94"]), patch.object(tt, "MAX_PAGES", 1), patch.object(tt, "listing_page", return_value=([job()], 2)):
            with self.assertRaises(tt.PartialScrapeError) as raised:
                tt.scrape_tiktok()
        self.assertEqual(len(raised.exception.rows), 1)

    def test_missing_count_is_not_an_empty_board(self):
        with patch.object(tt, "_request", return_value={"job_post_list": []}):
            with self.assertRaises(ValueError):
                tt.listing_page(["CT_94"])

    def test_jd_includes_pay_section_excludes_navigation_and_invents_no_date(self):
        html = ('<nav>Sign in</nav><div><p>Responsibilities</p><p>Build Python systems.</p></div>'
                '<div><p>Qualifications</p><p>SQL experience required.</p></div>'
                '<div><p>Job Information</p><p>Salary range: $100,000 - $120,000 per year.</p></div>'
                '<footer>Privacy settings</footer>')
        result = Mock(text=html)
        with patch.object(scraper, "_safe_get", return_value=result):
            jd, date = tt.detail_jd(tt.BOARD + "/123")
        self.assertIn("$100,000 - $120,000", jd)
        self.assertIn("Build Python systems.", jd)
        self.assertNotIn("Sign in", jd)
        self.assertNotIn("Privacy settings", jd)
        self.assertEqual(date, "")

    def test_detail_shell_is_not_a_jd(self):
        with patch.object(scraper, "_safe_get", return_value=Mock(text="<p>Responsibilities</p>")):
            self.assertEqual(tt.detail_jd(tt.BOARD + "/123"), ("", ""))

    def test_registration_and_detection(self):
        self.assertIs(scraper.SCRAPERS["tiktok"], tt.scrape_tiktok)
        for url in ("https://careers.tiktok.com/", tt.BOARD, tt.BOARD + "/123"):
            self.assertEqual(scraper.detect_board(url), (tt.BOARD, "tiktok", "TikTok"))

    def test_scorer_dispatch(self):
        from scraper import score_jobs
        url = tt.BOARD + "/123"
        with patch.object(tt, "detail_jd", return_value=("Complete posting including pay", "")):
            self.assertEqual(score_jobs.detail_jd(url), (url, "Complete posting including pay", ""))


if __name__ == "__main__":
    unittest.main()
