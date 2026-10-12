"""Offline guards for published native employer catalogs and explicit paging totals."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ["EV_OFF"] = "1"
import scraper
from scraper import wordpressjobs as wp
from scraper.infosys import PartialScrapeError

ALLIANCE = "https://www.aggteam.com/careers/"
MAYLOR = "https://maylorusa.com/company/careers/"
ORIGIN, CONFIG = wp.board_parts(ALLIANCE)


def job(jid=1):
    return {"id": jid, "status": "publish", "type": "awsm_job_openings", "date": "2026-09-01T12:00:00",
            "link": ORIGIN + "/jobs/role-%s/" % jid, "title": {"rendered": "Program &amp; Operations Analyst"},
            "content": {"rendered": "<p>Full employer description and requirements.</p>"},
            "class_list": ["status-publish", "job-location-arlington-va"]}


def page(jobs, total, pages):
    return SimpleNamespace(headers={"X-WP-Total": str(total), "X-WP-TotalPages": str(pages)}, json=lambda: jobs)


class WordPressJobsTests(unittest.TestCase):
    def test_exact_board_detection_and_registered_dispatch(self):
        self.assertEqual(scraper.detect_board(ALLIANCE), (ALLIANCE, "wordpressjobs", "Alliance Global Group LLC"))
        self.assertEqual(scraper.detect_board(MAYLOR.rstrip("/"))[0], MAYLOR)
        self.assertIs(scraper.SCRAPERS["wordpressjobs"], wp.scrape_wordpressjobs)
        with patch.object(scraper, "scrape_wordpressjobs", return_value=[]):
            self.assertEqual(scraper.probe_board(ALLIANCE, "wordpressjobs"), 0)
        for url in [ALLIANCE + "?search=analyst", ALLIANCE.replace("www.aggteam.com", "www.aggteam.com.evil.test"),
                    ORIGIN + "/jobs/program-manager/", MAYLOR.replace("https", "http")]:
            with self.assertRaises(ValueError):
                wp.board_parts(url)
        self.assertIsNone(scraper.detect_board(ORIGIN + "/jobs/program-manager/"))

    def test_job_content_location_and_original_publisher_date(self):
        row = wp.parse_job(job(), ORIGIN, CONFIG, {"arlington-va": "Arlington VA"})
        self.assertEqual(row["title"], "Program & Operations Analyst")
        self.assertEqual(row["location"], "Arlington VA")
        self.assertEqual(row["found_date"], "2026-09-01")
        self.assertIn("requirements", row["jd"])

    def test_maylor_keeps_2021_date_and_full_public_role_fields(self):
        origin, config = wp.board_parts(MAYLOR)
        data = dict(job(), type="jobs", date="2021-06-03T17:47:52", link=origin + "/job/secretary-2-3/",
                    position_job_location_addressLocality="Tampa", position_job_location_addressRegion="FL",
                    position_description="<p>Manage projects.</p>", position_qualifications="<ul><li>Engineering degree.</li></ul>")
        row = wp.parse_job(data, origin, config, {})
        self.assertEqual(row["location"], "Tampa, FL")
        self.assertEqual(row["found_date"], "2021-06-03")
        self.assertEqual(row["jd"], "Manage projects.\nEngineering degree.")

    def test_full_pagination_uses_advertised_count_and_stable_id_sort(self):
        with patch.object(wp, "_configuration", return_value=(ORIGIN, CONFIG, {"arlington-va": "Arlington VA"})), \
                patch.object(wp, "PAGE_SIZE", 2), patch.object(wp, "_get", side_effect=[page([job(1), job(2)], 3, 2), page([job(3)], 3, 2)]) as get:
            self.assertEqual(len(wp.scrape_wordpressjobs(ALLIANCE)), 3)
        self.assertEqual(get.call_args.kwargs["params"]["page"], 2)
        self.assertEqual(get.call_args.kwargs["params"]["orderby"], "id")

    def test_explicit_empty_is_complete_but_missing_total_is_not(self):
        with patch.object(wp, "_configuration", return_value=(ORIGIN, CONFIG, {})), \
                patch.object(wp, "_get", return_value=page([], 0, 0)):
            self.assertEqual(wp.scrape_wordpressjobs(ALLIANCE), [])
        bad = page([], 0, 0)
        bad.headers = {}
        with patch.object(wp, "_configuration", return_value=(ORIGIN, CONFIG, {})), \
                patch.object(wp, "_get", return_value=bad):
            with self.assertRaises(PartialScrapeError):
                wp.scrape_wordpressjobs(ALLIANCE)

    def test_error_repeated_or_changed_later_page_preserves_rows(self):
        for later in [RuntimeError("HTTP503"), page([job(1)], 2, 2), page([], 2, 2), page([job(2)], 3, 3)]:
            with patch.object(wp, "PAGE_SIZE", 1), \
                    patch.object(wp, "_configuration", return_value=(ORIGIN, CONFIG, {"arlington-va": "Arlington VA"})), \
                    patch.object(wp, "_get", side_effect=[page([job(1)], 2, 2), later]):
                with self.assertRaises(PartialScrapeError) as caught:
                    wp.scrape_wordpressjobs(ALLIANCE)
            self.assertEqual(len(caught.exception.rows), 1)

    def test_malformed_job_keeps_valid_rows_and_cannot_claim_full_catalog(self):
        for bad in [dict(job(2), status="draft"), dict(job(2), link="https://other.test/jobs/role-2/"),
                    dict(job(2), class_list=["job-location-unknown"]), dict(job(2), title={"rendered": ""})]:
            with patch.object(wp, "_configuration", return_value=(ORIGIN, CONFIG, {"arlington-va": "Arlington VA"})), \
                    patch.object(wp, "_get", return_value=page([job(1), bad, job(3)], 3, 1)):
                with self.assertRaises(PartialScrapeError) as caught:
                    wp.scrape_wordpressjobs(ALLIANCE)
            self.assertEqual(len(caught.exception.rows), 2)

    def test_configuration_requires_first_party_published_rest_job_route(self):
        html = '<link rel="https://api.w.org/" href="https://www.aggteam.com/wp-json/">'
        response = SimpleNamespace(text=html)
        index = SimpleNamespace(json=lambda: {"routes": {"/wp/v2/awsm_job_openings": {"methods": ["GET"]}}})
        with patch.object(wp, "_get", side_effect=[response, index]):
            self.assertEqual(wp._configuration(ALLIANCE)[:2], (ORIGIN, CONFIG))
        for bad in ["<html>Temporarily unavailable</html>", html.replace("www.aggteam.com", "other.test")]:
            with patch.object(wp, "_get", return_value=SimpleNamespace(text=bad)):
                with self.assertRaises(ValueError):
                    wp._configuration(ALLIANCE)


if __name__ == "__main__":
    unittest.main()
