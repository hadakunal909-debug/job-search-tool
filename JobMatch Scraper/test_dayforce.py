"""Offline checks for Dayforce public session, pagination and posting scope."""
import os
os.environ["EV_OFF"] = "1"
import json
import threading
import unittest
from unittest.mock import Mock, patch

import scraper
from scraper import dayforce as df
from scraper.infosys import PartialScrapeError

URL = "https://jobs.dayforcehcm.com/en-US/facllc/AEI"
INFO = {"clientNamespace": "facllc", "jobBoardCode": "aei", "jobBoardId": 4, "isDisabled": False}


def data(job=None):
    result = {"query": {"clientNamespace": "facllc", "careerSiteXRefCode": "AEI"},
        "props": {"pageProps": {"dehydratedState": {"queries": [{"queryKey": ["site-info"],
            "state": {"status": "success", "data": dict(INFO)}}]}}}}
    if job:
        result["query"]["id"] = str(job["jobPostingId"])
        result["props"]["pageProps"]["jobData"] = job
    return result


def job(jid):
    return {"clientNamespace": "facllc", "jobBoardId": 4, "jobPostingId": jid,
        "jobReqId": 9000, "jobTitle": " Data Engineer ", "postingStartTimestampUTC": "2026-09-04T08:00:00Z",
        "postingLocations": [{"formattedAddress": "Madison, WI, USA"}], "jobDescription": "Preview"}


class DayforceTests(unittest.TestCase):
    def test_canonical_posting_and_shared_host_ownership(self):
        self.assertEqual(df.board_url(URL + "/jobs/1431?mode=apply"), URL)
        self.assertEqual(df.board_url("https://jobs.dayforcehcm.com/facllc/AEI/jobs/1431"), URL)
        self.assertTrue(df.owns_url(URL, URL + "/jobs/1431"))
        self.assertFalse(df.owns_url(URL, URL.replace("AEI", "Other") + "/jobs/1431"))
        for invalid in [URL.replace(".com/", ".com.evil.test/"), URL + "/signin", URL.replace("https://", "https://user@")]:
            with self.assertRaises(ValueError):
                df.board_url(invalid)

    def test_public_csrf_pair_and_explicit_listing_contract(self):
        csrf = Mock(json=Mock(return_value={"csrfToken": "a" * 64}))
        listing = Mock(json=Mock(return_value={"jobPostings": [job(1)], "maxCount": 1, "offset": 0, "count": 1}))
        with patch.object(scraper.SESSION, "get", return_value=csrf) as get, patch.object(scraper.SESSION, "post", return_value=listing) as post:
            token = df.anonymous_csrf()
            self.assertEqual(df.listing_page(URL, INFO, token), ([job(1)], 1))
        self.assertTrue(get.call_args.args[0].endswith("/api/auth/csrf"))
        self.assertEqual(post.call_args.kwargs["headers"]["X-CSRF-TOKEN"], "a" * 64)
        self.assertEqual(post.call_args.kwargs["json"]["jobBoardCode"], "AEI")
        self.assertNotIn("Authorization", post.call_args.kwargs["headers"])
        for payload in [{}, {"jobPostings": [], "maxCount": 0, "offset": 25, "count": 0},
                {"jobPostings": [], "maxCount": False, "offset": 0, "count": 0}]:
            with patch.object(scraper.SESSION, "post", return_value=Mock(json=Mock(return_value=payload))):
                with self.assertRaises(ValueError):
                    df.listing_page(URL, INFO, token)

    def run_scrape(self, pages):
        with patch.object(df, "page_data", return_value=data()), patch.object(df, "anonymous_csrf", return_value="token"), patch.object(df, "listing_page", side_effect=pages) as get:
            return df.scrape_dayforce(URL), get

    def test_complete_pagination_uses_returned_length_and_public_id(self):
        rows, get = self.run_scrape([([job(1), job(2)], 3), ([job(3)], 3)])
        self.assertEqual(len(rows), 3)
        self.assertEqual(get.call_args_list[1].args[-1], 2)
        self.assertEqual(rows[0]["url"], URL + "/jobs/1")
        self.assertEqual(rows[0]["found_date"], "2026-09-04")
        self.assertEqual(rows[0]["location"], "Madison, WI, USA")
        self.assertNotIn("jd", rows[0])
        self.assertEqual(self.run_scrape([([], 0)])[0], [])

    def test_later_error_repeated_page_empty_page_and_changed_total_preserve_rows(self):
        for later in [RuntimeError("HTTP 503"), ([job(1)], 2), ([], 2), ([job(2)], 3)]:
            with self.subTest(later=later), patch.object(df, "page_data", return_value=data()), patch.object(df, "anonymous_csrf", return_value="token"), patch.object(df, "listing_page", side_effect=[([job(1)], 2), later]):
                with self.assertRaises(PartialScrapeError) as caught:
                    df.scrape_dayforce(URL)
                self.assertEqual(len(caught.exception.rows), 1)

    def test_other_employer_or_internal_posting_is_rejected(self):
        for change in [{"clientNamespace": "other"}, {"jobBoardId": 3}, {"isInternal": True}, {"jobPostingId": True}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                df.parse_job(dict(job(1), **change), URL, INFO)
        bad = data()
        bad["props"]["pageProps"]["dehydratedState"]["queries"][0]["state"]["data"]["jobBoardCode"] = "OTHER"
        with self.assertRaises(ValueError):
            df.site_info(bad, URL)

    def test_page_requires_structured_matching_employer_scope(self):
        response = Mock(url=URL, text='<script id="__NEXT_DATA__" type="application/json">' + json.dumps(data()) + '</script>')
        with patch.object(scraper.SESSION, "get", return_value=response):
            self.assertEqual(df.page_data(URL), data())
            response.url = URL.replace("AEI", "Other")
            with self.assertRaises(ValueError):
                df.page_data(URL)
            response.url, response.text = URL, "<html>Unavailable</html>"
            with self.assertRaises(ValueError):
                df.page_data(URL)

    def test_public_employer_identity_prefers_configured_correspondence_name(self):
        context = data()
        context["props"]["pageProps"]["dehydratedState"]["queries"][0]["state"]["data"]["candidateCorrespondenceClientName"] = "Affiliated Engineers, Inc."
        with patch.object(df, "page_data", return_value=context):
            self.assertEqual(df.identity_text(URL), "Affiliated Engineers, Inc.")

    def test_detail_uses_full_content_date_and_exact_external_posting(self):
        posting = dict(job(1431), isInternal=False, postingStatus=1,
            jobPostingContent={"jobDescriptionHeader": "<p>Affiliated Engineers</p>",
                "jobDescription": "<p>Build reliable systems.</p>", "jobDescriptionFooter": "<p>Equal opportunity.</p>"})
        with patch.object(df, "page_data", return_value=data(posting)):
            text, date = df.detail_jd(URL + "/jobs/1431")
            self.assertEqual(date, "2026-09-04")
            self.assertIn("Build reliable systems.", text)
            self.assertIn("Equal opportunity.", text)
            self.assertNotIn("Preview", text)
        for change in [{"jobPostingId": 1432}, {"isInternal": True}, {"postingStatus": 2}]:
            with patch.object(df, "page_data", return_value=data(dict(posting, **change))):
                self.assertEqual(df.detail_jd(URL + "/jobs/1431"), ("", ""))

    def test_concurrent_boards_keep_their_cookie_and_header_pair_until_paging_finishes(self):
        first_in_search, second_attempted, release_first = threading.Event(), threading.Event(), threading.Event()
        active, observed, errors = {}, [], []
        def csrf():
            active["token"] = threading.current_thread().name
            return active["token"]
        def listing(url, info, token, offset):
            if threading.current_thread().name == "first":
                first_in_search.set()
                if not release_first.wait(2):
                    raise RuntimeError("test release timed out")
            observed.append((token, active["token"]))
            return [], 0
        def run():
            try:
                if threading.current_thread().name == "second":
                    second_attempted.set()
                df.scrape_dayforce(URL)
            except Exception as exc:
                errors.append(exc)
        with patch.object(df, "page_data", return_value=data()), patch.object(df, "anonymous_csrf", side_effect=csrf) as context, patch.object(df, "listing_page", side_effect=listing):
            first = threading.Thread(target=run, name="first")
            second = threading.Thread(target=run, name="second")
            first.start()
            try:
                self.assertTrue(first_in_search.wait(2))
                second.start()
                self.assertTrue(second_attempted.wait(2))
                self.assertEqual(context.call_count, 1)
            finally:
                release_first.set()
                first.join(2)
                if second.ident is not None:
                    second.join(2)
        self.assertFalse(errors)
        self.assertEqual(observed, [("first", "first"), ("second", "second")])


if __name__ == "__main__":
    unittest.main()
