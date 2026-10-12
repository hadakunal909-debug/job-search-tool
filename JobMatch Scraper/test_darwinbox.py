"""Offline public tenant, posting ownership and completeness checks."""
import os
os.environ["EV_OFF"] = "1"
import unittest
from unittest.mock import Mock, patch
import scraper
from scraper import darwinbox as dbx
from scraper.infosys import PartialScrapeError

URL = "https://buyersedgeplatform.darwinbox.com/ms/candidatev2/main/careers/allJobs"
INFO = {"id": "a6aa0742025543", "company_name": "Buyers Edge Platform", "subdomain": "buyersedgeplatform",
        "recruitment_enabled": True, "new_careers": True, "is_preview": False, "allJobsCount": 3}


def job(jid):
    return {"id": str(jid), "title": "R&amp;B Engineer", "jd": "&lt;p&gt;Build &amp;amp; verify systems.&lt;/p&gt;",
            "officelocation_show_arr": "Boston, Massachusetts, United States", "posted_on": 1790740800,
            "job_posting": {"careers": {"source": "careers", "from": 1790740800}}}


class DarwinboxTests(unittest.TestCase):
    def test_exact_public_main_tenant_urls(self):
        self.assertEqual(dbx.board_url(URL + "?marketing=true"), URL)
        self.assertEqual(dbx.board_url(URL.rsplit("/", 1)[0] + "/jobDetails/a6aa1"), URL)
        for bad in [URL.replace(".com/", ".com.evil.test/"), URL.replace("https://", "https://user@"),
                    URL.replace("/main/", "/other/"), URL.replace("allJobs", "applications"),
                    URL.replace("https://", "http://"), URL.replace(".com/", ".com:443/"), URL + "#login"]:
            with self.subTest(url=bad), self.assertRaises(ValueError):
                dbx.board_url(bad)

    def test_public_json_transport_has_no_auth_and_no_redirect_following(self):
        response = Mock(status_code=200, url=URL.split("/ms/")[0] + "/ms/candidateapi/job/alljobs",
                        json=Mock(return_value={"status": "success", "data": [], "job_counts": 0}))
        with patch.object(scraper.SESSION, "post", return_value=response) as post:
            self.assertEqual(dbx.listing_page(URL), ([], 0))
        args = post.call_args.kwargs
        self.assertEqual(args["json"], {"page": 1, "limit": 10, "companyId": "main"})
        self.assertFalse(args["allow_redirects"])
        self.assertNotIn("Authorization", args["headers"])
        for change in [{"status_code": 302}, {"url": response.url.replace("buyersedgeplatform", "other")},
                       {"json": Mock(return_value={"status": "error"})},
                       {"json": Mock(return_value={"status": "success", "data": [], "job_counts": True})},
                       {"json": Mock(side_effect=ValueError("HTML instead of JSON"))}]:
            fields = {"status_code":200, "url":response.url, "json":Mock(return_value={"status":"success"})}
            fields.update(change)
            with self.subTest(change=change), patch.object(scraper.SESSION, "post", return_value=Mock(**fields)):
                with self.assertRaises(ValueError):
                    dbx.listing_page(URL)

    def test_config_requires_current_exact_tenant_and_external_portal(self):
        for change in [{"subdomain": "other"}, {"is_preview": True}, {"recruitment_enabled": False},
                       {"new_careers": False}, {"allJobsCount": True}, {"company_name": ""}, {"id": "../other"}]:
            with self.subTest(change=change), patch.object(dbx, "_json", return_value={"message":{"company":dict(INFO, **change)}}):
                with self.assertRaises(ValueError):
                    dbx.company_info(URL)
        with patch.object(dbx, "_json", return_value={"message":{"company":INFO}}):
            self.assertEqual(dbx.identity_text(URL), "Buyers Edge Platform")

    def test_full_jd_decodes_once_without_changing_id_or_public_date(self):
        row = dbx.parse_job(job("a123"), URL)
        self.assertEqual(row["title"], "R&B Engineer")
        self.assertEqual(row["jd"], "Build & verify systems.")
        self.assertTrue(row["url"].endswith("/main/careers/jobDetails/a123"))
        self.assertEqual(row["found_date"], "2026-09-30")

    def test_unknown_date_is_omitted_for_pipeline_scrape_stamp(self):
        row = dbx.parse_job(dict(job("a1"), posted_on=None, created_on=None), URL)
        self.assertNotIn("found_date", row)
        row = dbx.parse_job(dict(job("a1"), posted_on=None, created_on="2026-09-18T00:00:00Z"), URL)
        self.assertEqual(row["found_date"], "2026-09-18")

    def test_detector_canonicalizes_only_supported_public_paths(self):
        posting = URL.rsplit("/", 1)[0] + "/jobDetails/a1?campaign=x"
        self.assertEqual(scraper.detect_board(posting), (URL, "darwinbox", ""))
        self.assertEqual(scraper.detect_board(URL), (URL, "darwinbox", ""))
        for bad in [URL.replace("/main/", "/other/"), URL.replace("allJobs", "applications"),
                    URL.replace(".com/", ".com.evil.test/"), URL.replace("https://", "https://user@"),
                    URL.replace(".com/", ".com:443/")]:
            with self.subTest(url=bad):
                self.assertIsNone(scraper.detect_board(bad))

    def test_board_identity_dispatch_uses_structured_config_and_fails_empty(self):
        with patch.object(dbx, "identity_text", return_value="Buyers Edge Platform") as identity:
            self.assertEqual(scraper.board_display_name(URL, "darwinbox"), "Buyers Edge Platform")
            identity.assert_called_once_with(URL)
        with patch.object(dbx, "identity_text", side_effect=ValueError("wrong tenant")):
            self.assertEqual(scraper.board_display_name(URL, "darwinbox"), "")

    def test_link_discovery_and_probe_keep_complete_empty_and_failed_distinct(self):
        match = scraper._ATS_LINK_RE.search('<a href="'+URL+'">Job Openings</a>')
        self.assertIsNotNone(match)
        self.assertEqual(match.group(0),URL)
        for result,expected in [([{"title":"Engineer"}],1),([],0)]:
            with patch.object(scraper,"scrape_darwinbox",return_value=result):
                self.assertEqual(scraper.probe_board(URL,"darwinbox"),expected)
        with patch.object(scraper,"scrape_darwinbox",side_effect=PartialScrapeError("partial",[job("a1")])):
            self.assertIsNone(scraper.probe_board(URL,"darwinbox"))

    def test_internal_only_malformed_id_missing_description_and_location_fail(self):
        for change in [{"job_posting":{"ijp":{"from":1790740800}}}, {"id":"../wrong"}, {"jd":""},
                       {"title":""}, {"officelocation_show_arr":[]}, {"job_posting":{"careers":{"from":True}}}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                dbx.parse_job(dict(job("a1"), **change), URL)

    def test_complete_pages_match_config_total_and_empty_is_explicit(self):
        with patch.object(dbx, "company_info", return_value=INFO), patch.object(dbx, "listing_page", side_effect=[([job("a1"), job("a2")],3),([job("a3")],3)]) as page:
            self.assertEqual(len(dbx.scrape_darwinbox(URL)),3)
            self.assertEqual([call.args[-1] for call in page.call_args_list],[1,2])
        with patch.object(dbx, "company_info", return_value=dict(INFO,allJobsCount=0)), patch.object(dbx, "listing_page", return_value=([],0)):
            self.assertEqual(dbx.scrape_darwinbox(URL),[])

    def test_partial_error_repeats_changed_total_and_early_end_preserve_rows(self):
        for later in [RuntimeError("HTTP503"), ([job("a1")],3), ([],3), ([job("a2")],4)]:
            with self.subTest(later=later), patch.object(dbx, "company_info", return_value=INFO), patch.object(dbx, "listing_page", side_effect=[([job("a1")],3),later]):
                with self.assertRaises(PartialScrapeError) as caught:
                    dbx.scrape_darwinbox(URL)
                self.assertEqual(len(caught.exception.rows),1)

    def test_bad_first_page_and_cap_cannot_report_complete(self):
        with patch.object(dbx, "company_info", return_value=INFO), patch.object(dbx, "listing_page", return_value=([],2)):
            with self.assertRaises(PartialScrapeError):dbx.scrape_darwinbox(URL)
        with patch.object(dbx, "company_info", return_value=INFO), patch.object(dbx, "listing_page", return_value=([job("a1")],3)), patch.object(dbx,"MAX_PAGES",1):
            with self.assertRaises(PartialScrapeError) as caught:dbx.scrape_darwinbox(URL)
            self.assertEqual(len(caught.exception.rows),1)

    def test_detail_must_match_exact_id_title_and_public_posting(self):
        url=URL.rsplit("/",1)[0]+"/jobDetails/a1"
        with patch.object(dbx,"company_info",return_value=INFO),patch.object(dbx,"_json",return_value={"message":{"job":[job("a1")]}}):
            self.assertEqual(dbx.detail_jd(url,"R&B Engineer"),("Build & verify systems.","2026-09-30"))
            self.assertEqual(dbx.detail_jd(url),("Build & verify systems.","2026-09-30"))
        for change in [{"id":"a2"},{"title":"Another Engineer"}]:
            with self.subTest(change=change),patch.object(dbx,"company_info",return_value=INFO),patch.object(dbx,"_json",return_value={"message":{"job":[dict(job("a1"),**change)]}}):
                self.assertEqual(dbx.detail_jd(url,"R&B Engineer"),("",""))

    def test_optional_title_still_refuses_another_id_or_internal_job(self):
        url=URL.rsplit("/",1)[0]+"/jobDetails/a1"
        with patch.object(dbx,"company_info",return_value=INFO),patch.object(dbx,"_json",return_value={"message":{"job":[job("a2")]}}):
            self.assertEqual(dbx.detail_jd(url),("",""))
        with patch.object(dbx,"company_info",return_value=INFO),patch.object(dbx,"_json",return_value={"message":{"job":[dict(job("a1"),job_posting={"ijp":{"from":1790740800}})]}}):
            with self.assertRaises(ValueError):dbx.detail_jd(url)

    def test_scorer_detail_dispatch_preserves_failure_for_normal_retry(self):
        from scraper import score_jobs
        url=URL.rsplit("/",1)[0]+"/jobDetails/a1"
        with patch.object(dbx,"detail_jd",return_value=("Full posting text","2026-09-30")) as detail:
            self.assertEqual(score_jobs._detail_jd(url),(url,"Full posting text","2026-09-30"))
            detail.assert_called_once_with(url)
        with patch.object(dbx,"detail_jd",side_effect=ValueError("wrong public scope")),patch.object(scraper,"_safe_get") as generic:
            self.assertEqual(score_jobs._detail_jd(url),(url,"",""))
            generic.assert_not_called()


if __name__ == "__main__":
    unittest.main()
