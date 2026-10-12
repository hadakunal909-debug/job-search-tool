"""Offline guards for Meta's separate advertised count and complete public catalog."""
import json
import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

os.environ["EV_OFF"] = "1"
import scraper
from scraper.infosys import PartialScrapeError


def job(jid="1", title="Program Manager", locations=None):
    return {"id": jid, "title": title, "locations": locations if locations is not None else ["Paris, France", "Austin, TX"]}


def payload(count=None, jobs=None, featured=None, legacy=False, **extra):
    block = {}
    if count is not None:
        block["job_count"] = count
    if jobs is not None:
        block["all_jobs"] = jobs
    if featured is not None:
        block["featured_jobs"] = featured
    return json.dumps(dict(data={"job_search_with_featured_jobs" + ("" if legacy else "_v2"): block}, **extra))


class MetaCareersTests(unittest.TestCase):
    def test_separate_count_complete_catalog_and_additional_featured(self):
        rows = scraper._metacareers_payload_rows([
            payload(count=2), payload(jobs=[job(), job("2")], featured=[job(), job("3")])])
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["location"], "Austin, TX")
        self.assertTrue(rows[2]["url"].endswith("/3/"))

    def test_legacy_schema_streamed_json_and_duplicate_response_remain_supported(self):
        body = payload(count=1, legacy=True) + "\n" + payload(jobs=[job()], featured=[], legacy=True)
        self.assertEqual(len(scraper._metacareers_payload_rows([body, body])), 1)

    def test_unicode_line_separators_inside_json_titles_are_not_record_boundaries(self):
        title = "Program\u0085and\u2028Operations\u2029Manager"
        body = payload(count=1, jobs=[job(title=title)])
        body = json.dumps(json.loads(body), ensure_ascii=False)
        self.assertEqual(scraper._metacareers_payload_rows([body])[0]["title"], title)

    def test_only_explicit_zero_and_empty_catalog_is_complete(self):
        self.assertEqual(scraper._metacareers_payload_rows([payload(count=0, jobs=[])]), [])
        for bodies in [[], [payload(count=0)], [payload(jobs=[])], [payload(count=True, jobs=[])]]:
            with self.assertRaises(PartialScrapeError):
                scraper._metacareers_payload_rows(bodies)

    def test_truncation_changing_count_and_overcount_preserve_rows(self):
        for bodies in [[payload(count=3, jobs=[job()])],
                       [payload(count=1), payload(count=2, jobs=[job()])],
                       [payload(count=0, jobs=[job()])]]:
            with self.assertRaises(PartialScrapeError) as caught:
                scraper._metacareers_payload_rows(bodies)
            self.assertEqual(len(caught.exception.rows), 1)

    def test_invalid_count_in_same_response_preserves_valid_jobs(self):
        with self.assertRaises(PartialScrapeError) as caught:
            scraper._metacareers_payload_rows([payload(count="unknown", jobs=[job()])])
        self.assertEqual(len(caught.exception.rows), 1)

    def test_graphql_errors_or_malformed_response_do_not_succeed(self):
        for body in [payload(count=1, jobs=[job()], errors=[{"message": "truncated"}]),
                     payload(count=1, jobs=[job()]) + "\n<html>Error</html>"]:
            with self.assertRaises(PartialScrapeError) as caught:
                scraper._metacareers_payload_rows([body])
            self.assertEqual(len(caught.exception.rows), 1)

    def test_malformed_job_does_not_drop_other_valid_jobs(self):
        for bad in [None, job(jid="bad"), job(title=""), dict(job(), locations="Austin, TX")]:
            with self.assertRaises(PartialScrapeError) as caught:
                scraper._metacareers_payload_rows([payload(count=2, jobs=[job(), bad, job("2")])])
            self.assertEqual(len(caught.exception.rows), 2)

    def test_duplicate_or_conflicting_catalog_id_is_incomplete(self):
        for jobs in [[job(), job()], [job(), job(title="Different")]]:
            with self.assertRaises(PartialScrapeError) as caught:
                scraper._metacareers_payload_rows([payload(count=1, jobs=jobs)])
            self.assertEqual(len(caught.exception.rows), 1)

    def fake_browser(self, count_body, catalog_body):
        page, browser, pw = MagicMock(), MagicMock(), MagicMock()
        pw.chromium.launch.return_value = browser
        browser.new_page.return_value = page
        callback = []
        page.on.side_effect = lambda event, fn: callback.append(fn)

        def response(body):
            result = MagicMock(url="https://www.metacareers.com/graphql", status=200)
            result.request.method = "POST"
            result.text.return_value = body
            callback[0](result)

        page.goto.side_effect = lambda *a, **kw: (response(count_body), types.SimpleNamespace(status=200))[1]
        page.wait_for_timeout.side_effect = lambda *a: response(catalog_body)
        manager = MagicMock()
        manager.__enter__.return_value = pw
        module = types.ModuleType("playwright.sync_api")
        module.sync_playwright = MagicMock(return_value=manager)
        return page, browser, module

    def test_browser_waits_for_catalog_after_count_and_closes(self):
        page, browser, module = self.fake_browser(payload(count=1), payload(jobs=[job()]))
        with patch.dict(sys.modules, {"playwright.sync_api": module}), \
                patch.object(scraper, "_metacareers_http_payloads", side_effect=scraper._MetaBootstrapError("bootstrap missing")):
            rows = scraper.scrape_metacareers(scraper.METACAREERS_JOBS_URL)
        self.assertEqual(len(rows), 1)
        page.wait_for_timeout.assert_called_once()
        browser.close.assert_called_once()
        self.assertEqual(browser.new_page.call_args.kwargs, {})

    def test_browser_catalog_timeout_preserves_partial(self):
        page, browser, module = self.fake_browser(payload(count=2), payload(jobs=[job()]))
        with patch.dict(sys.modules, {"playwright.sync_api": module}), \
                patch.object(scraper, "_metacareers_http_payloads", side_effect=scraper._MetaBootstrapError("bootstrap missing")):
            with self.assertRaises(PartialScrapeError) as caught:
                scraper.scrape_metacareers(scraper.METACAREERS_JOBS_URL)
        self.assertEqual(len(caught.exception.rows), 1)
        self.assertEqual(page.wait_for_timeout.call_count, 60)
        browser.close.assert_called_once()

    def test_board_scope_cannot_be_filtered_or_foreign(self):
        for url in ["https://metacareers.com.evil.test/jobs/", "https://www.metacareers.com/jobs/?q=pm",
                    "https://www.metacareers.com/jobs/123/", "http://www.metacareers.com/jobs/"]:
            with self.assertRaises(ValueError):
                scraper.scrape_metacareers(url)

    def test_public_http_path_needs_no_browser_and_no_partial_fallback(self):
        with patch.object(scraper, "_metacareers_http_payloads", return_value=[payload(count=1, jobs=[job()])]):
            self.assertEqual(len(scraper.scrape_metacareers(scraper.METACAREERS_JOBS_URL)), 1)
        with patch.object(scraper, "_metacareers_http_payloads", return_value=[payload(count=2, jobs=[job()])]):
            with self.assertRaises(PartialScrapeError) as caught:
                scraper.scrape_metacareers(scraper.METACAREERS_JOBS_URL)
        self.assertEqual(len(caught.exception.rows), 1)

    def test_http_discovers_rotating_ids_from_official_script_only(self):
        session = MagicMock()
        session.__enter__.return_value = session
        page = MagicMock(text='["LSD",[],{"token":"public-page-token"},323]'
                             '<script src="https://static.xx.fbcdn.net/rsrc.php/v4/x/job-search.js"></script>'
                             '<script src="https://evil.test/config.js"></script>')
        script = MagicMock(text='CareersJobSearchHideFiltersBarV2Query_candidate_portalRelayOperation",[],function(){a.exports="123"}'
                               'CareersJobSearchResultsV2DataQuery_candidate_portalRelayOperation",[],function(){a.exports="456"}')
        session.get.side_effect = [page, script]
        session.post.side_effect = [MagicMock(text=payload(count=1)), MagicMock(text=payload(jobs=[job()]))]
        with patch.object(scraper.requests, "Session", return_value=session):
            self.assertEqual(len(scraper.scrape_metacareers(scraper.METACAREERS_JOBS_URL)), 1)
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(session.post.call_args_list[0].kwargs["data"]["doc_id"], "123")
        self.assertEqual(session.post.call_args_list[1].kwargs["data"]["doc_id"], "456")
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])
        self.assertFalse(json.loads(session.post.call_args.kwargs["data"]["variables"])["isLoggedIn"])


if __name__ == "__main__":
    unittest.main()
