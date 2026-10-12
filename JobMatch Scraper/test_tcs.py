"""Public TCS US search completeness, session isolation and description contracts."""
import unittest
from unittest.mock import Mock, patch

import requests
import scraper
from scraper import tcs


def job(jid="123J", title="Data Engineer", location="Chicago, IL"):
    return {"id": jid, "title": title, "location": location,
            "applyBy": "2026-11-17T23:59:59+05:30"}


def response(text="", data=None, status=200, headers=None):
    result = Mock()
    result.text = text
    result.json.return_value = data
    result.headers = headers or {}
    if status >= 400:
        result.raise_for_status.side_effect = requests.HTTPError(str(status))
    return result


def envelope(data):
    return {"result": True, "status": 200, "payload": data}


class TCSTests(unittest.TestCase):
    def tearDown(self):
        if hasattr(tcs._local, "context"):
            del tcs._local.context

    def test_pages_to_advertised_total_preserving_us_posting_links(self):
        with patch.object(tcs, "listing_page", side_effect=[([job()], 3),
                    ([job("124J"), job("125W")], 3)]) as fetch:
            rows = tcs.scrape_tcs()
        self.assertEqual([call.args[0] for call in fetch.call_args_list], [1, 2])
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["company"], "Tata Consultancy Services")
        self.assertEqual(rows[0]["url"], tcs.BASE + "jobs/123J?geography=US&language=EN")
        self.assertNotIn("found_date", rows[0])  # Deadline must never become posting date.

    def test_later_error_preserves_rows(self):
        with patch.object(tcs, "listing_page", side_effect=[([job()], 2), requests.Timeout()]):
            with self.assertRaises(tcs.PartialScrapeError) as raised:
                tcs.scrape_tcs()
        self.assertEqual(len(raised.exception.rows), 1)

    def test_repeated_page_and_premature_end_are_partial(self):
        for second in ([job()], []):
            with self.subTest(second=second), patch.object(tcs, "listing_page", side_effect=[([job()], 2), (second, 2)]):
                with self.assertRaises(tcs.PartialScrapeError) as raised:
                    tcs.scrape_tcs()
            self.assertEqual(len(raised.exception.rows), 1)

    def test_safety_cap_is_partial(self):
        with patch.object(tcs, "MAX_PAGES", 1), patch.object(tcs, "listing_page", return_value=([job()], 2)):
            with self.assertRaises(tcs.PartialScrapeError) as raised:
                tcs.scrape_tcs()
        self.assertEqual(len(raised.exception.rows), 1)

    def test_invalid_row_is_partial_not_a_false_complete_board(self):
        with patch.object(tcs, "listing_page", return_value=([job(), job("../../login")], 2)):
            with self.assertRaises(tcs.PartialScrapeError) as raised:
                tcs.scrape_tcs()
        self.assertEqual(len(raised.exception.rows), 1)

    def test_legitimate_zero(self):
        with patch.object(tcs, "listing_page", return_value=([], 0)):
            self.assertEqual(tcs.scrape_tcs(), [])

    def test_listing_request_uses_the_public_ui_shape(self):
        with patch.object(tcs, "_request", return_value={"content": [job()], "totalElements": 1,
                                                       "number": 4}) as fetch:
            self.assertEqual(tcs.listing_page(5), ([job()], 1))
        self.assertEqual(fetch.call_args.args[0], "jobs/searchJ")
        body = fetch.call_args.args[1]
        self.assertEqual(body["page"], 5)
        self.assertEqual(body["resultsPerPage"], 50)
        self.assertEqual(body["searchTerms"], [])
        self.assertIs(body["includeRegularJob"], True)
        self.assertIs(body["includeWalkIn"], True)
        self.assertEqual(body["jobLocationFilters"], [])
        self.assertIsNone(body["relevancySortOrder"])
        self.assertEqual(body["recentSortOrder"], "desc")

    def test_new_job_published_mid_sweep_is_recovered_without_accepting_a_gap(self):
        pages = [([job("124J"), job("123J")], 3),
                 ([job("123J"), job("122J")], 4), ([], 4),
                 ([job("125J"), job("124J")], 4)]
        with patch.object(tcs, "listing_page", side_effect=pages) as fetch:
            rows = tcs.scrape_tcs()
        self.assertEqual(len(rows), 4)
        self.assertEqual([c.args[0] for c in fetch.call_args_list], [1, 2, 3, 1])

    def test_head_refresh_still_refuses_an_unresolved_gap(self):
        pages = [([job()], 2), ([], 3), ([job("124J")], 3)]
        with patch.object(tcs, "listing_page", side_effect=pages):
            with self.assertRaises(tcs.PartialScrapeError) as raised:
                tcs.scrape_tcs()
        self.assertEqual(len(raised.exception.rows), 2)

    def test_count_falling_or_unique_rows_exceeding_total_stays_partial(self):
        for pages, retained in (([([job()], 2), ([job("124J")], 1)], 2),
                                ([([job()], 2), ([job("124J"), job("125J")], 2)], 3)):
            with self.subTest(pages=pages), patch.object(tcs, "listing_page", side_effect=pages):
                with self.assertRaises(tcs.PartialScrapeError) as raised:
                    tcs.scrape_tcs()
            self.assertEqual(len(raised.exception.rows), retained)

    def test_head_refresh_count_drift_preserves_every_collected_row(self):
        for head, total, retained in (([job("124J")], 2, 2),
                                     ([job("124J"), job("125J"), job("126J")], 3, 4)):
            pages = [([job()], 2), ([], 3), (head, total)]
            with self.subTest(head=head), patch.object(tcs, "listing_page", side_effect=pages):
                with self.assertRaises(tcs.PartialScrapeError) as raised:
                    tcs.scrape_tcs()
            self.assertEqual(len(raised.exception.rows), retained)

    def test_missing_count_is_not_zero(self):
        for data in ({"content": []}, {"content": [], "totalElements": True}, {"totalElements": 2}):
            with self.subTest(data=data), patch.object(tcs, "_request", return_value=data):
                with self.assertRaises(ValueError):
                    tcs.listing_page()

    def test_request_page_and_count_metadata_must_agree(self):
        for data in ({"content": [job()], "totalElements": 0, "number": 0},
                     {"content": [job()], "totalElements": 1, "number": 1},
                     {"content": [job()], "totalElements": 1, "number": False},
                     {"content": [job()], "totalElements": 1}):
            with self.subTest(data=data), patch.object(tcs, "_request", return_value=data):
                with self.assertRaises(ValueError):
                    tcs.listing_page()
        for page in (0, -1, True, "1"):
            with self.subTest(page=page), patch.object(tcs, "_request") as fetch:
                with self.assertRaises(ValueError):
                    tcs.listing_page(page)
                fetch.assert_not_called()

    def test_anonymous_context_uses_its_own_session_and_checks_country(self):
        session = Mock()
        session.headers = {}
        session.get.side_effect = [response('<ib-root></ib-root>'),
            response(data={"result": True}, headers={"X-NEXT-XSRF-TOKEN": "public-anonymous-token"}),
            response(data=envelope({"currentCountry": {"geoCode": "US", "id": 230}}))]
        with patch.object(scraper, "_make_session", return_value=session) as factory:
            self.assertIs(tcs._session(), session)
            self.assertIs(tcs._session(), session)
        factory.assert_called_once()
        self.assertEqual(session.get.call_args_list[0].args[0], tcs.SEARCH)
        self.assertEqual(session.get.call_args_list[1].args[0], tcs.API + "candidate")
        self.assertEqual(session.get.call_args.args[0], tcs.API + "en-US/landing/init")
        self.assertIs(session.get.call_args.kwargs["allow_redirects"], False)
        self.assertEqual(session.headers["NEXT-XSRF-TOKEN"], "public-anonymous-token")

    def test_wrong_country_and_shell_do_not_create_context(self):
        for country in ({"geoCode": "IN", "id": 101}, {}):
            session = Mock()
            session.headers = {}
            session.get.side_effect = [response(),
                response(headers={"X-NEXT-XSRF-TOKEN": "public-anonymous-token"}),
                response(data=envelope({"currentCountry": country}))]
            with self.subTest(country=country), patch.object(scraper, "_make_session", return_value=session):
                with self.assertRaises(ValueError):
                    tcs._session()
            session.close.assert_called_once()
            self.assertFalse(hasattr(tcs._local, "context"))

    def test_error_envelope_is_not_jobs(self):
        session = Mock()
        session.post.return_value = response(data={"result": "N", "data": {}})
        with patch.object(tcs, "_session", return_value=session):
            with self.assertRaises(ValueError):
                tcs.listing_page()

    def test_new_public_search_and_description_transport_use_normal_csrf(self):
        session = Mock()
        session.headers = {"NEXT-XSRF-TOKEN": "old-anonymous-token"}
        session.post.return_value = response(data=envelope({"content": [job()], "totalElements": 1,
                                                           "number": 0}),
                                           headers={"X-NEXT-XSRF-TOKEN": "rotated-anonymous-token"})
        session.get.return_value = response(data=envelope({"jobId": 123, "country": "United States", "description": "SQL role"}))
        with patch.object(tcs, "_session", return_value=session):
            self.assertEqual(tcs.listing_page(), ([job()], 1))
            self.assertEqual(tcs.detail_jd(tcs.NEXT + "en-US/jobs/123J"), ("SQL role", ""))
        self.assertEqual(session.post.call_args.args[0], tcs.API + "en-US/search/jobs")
        self.assertEqual(session.get.call_args.args[0], tcs.API + "en-US/job/desc/123")
        self.assertEqual(session.headers["NEXT-XSRF-TOKEN"], "rotated-anonymous-token")

    def test_missing_public_csrf_cannot_establish_session(self):
        session = Mock()
        session.headers = {}
        session.get.side_effect = [response(), response(),
            response(data=envelope({"currentCountry": {"geoCode": "US", "id": 230}}))]
        with patch.object(scraper, "_make_session", return_value=session):
            with self.assertRaises(ValueError):
                tcs._session()
        session.close.assert_called_once()

    def test_detail_has_full_description_experience_salary_and_no_false_date(self):
        data = {"jobId": 123, "country": "United States", "description": "<p>Build Python systems.</p>",
                "qualifications": "Computer Science", "skilldetail": "Python | SQL",
                "experience": "3 - 5 Years", "minSalary": "$100,000", "maxSalary": "$120,000",
                "applyby": "2026-11-17 00:00:00"}
        with patch.object(tcs, "_request", return_value=data) as fetch:
            jd, date = tcs.detail_jd(tcs.BASE + "jobs/123J?geography=US&language=EN")
        self.assertIn("Build Python systems.", jd)
        self.assertIn("Experience: 3 - 5 Years", jd)
        self.assertIn("$100,000 - $120,000 per year", jd)
        self.assertEqual(date, "")
        fetch.assert_called_once_with("job/desc", {"jobId": "123"})

    def test_walkin_uses_its_public_endpoint(self):
        with patch.object(tcs, "_request", return_value={"jobId": 123, "country": "United States", "description": "SQL role"}) as fetch:
            self.assertEqual(tcs.detail_jd(tcs.BASE + "jobs/123W"), ("SQL role", ""))
        fetch.assert_called_once_with("job/desc/walkin", {"jobId": "123"})

    def test_registered_detected_and_probe_uses_the_live_count(self):
        url = tcs.BASE + "jobs/123J?geography=US&language=EN"
        self.assertEqual(scraper.detect_board(url), (tcs.BOARD, "tcs", tcs.COMPANY))
        self.assertIs(scraper.SCRAPERS["tcs"], tcs.scrape_tcs)
        with patch.object(tcs, "listing_page", return_value=([job()], 2374)):
            self.assertEqual(scraper.probe_board(tcs.BOARD, "tcs"), 2374)
            self.assertIsNone(scraper.probe_board("https://example.com/", "tcs"))

    def test_scorer_dispatches_to_tcs_description(self):
        from scraper import score_jobs
        url = tcs.BASE + "jobs/123J?geography=US&language=EN"
        with patch.object(tcs, "detail_jd", return_value=("Full TCS description", "")):
            self.assertEqual(score_jobs.detail_jd(url), (url, "Full TCS description", ""))

    def test_wrong_id_country_or_host_never_yields_another_posting(self):
        for data in ({"jobId": 124, "country": "United States", "description": "Other job"},
                     {"jobId": 123, "country": "India", "description": "Other job"}):
            with patch.object(tcs, "_request", return_value=data):
                self.assertEqual(tcs.detail_jd(tcs.BASE + "jobs/123J"), ("", ""))
        with patch.object(tcs, "_request") as fetch:
            self.assertEqual(tcs.detail_jd("https://example.com/candidate/jobs/123J"), ("", ""))
            fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
