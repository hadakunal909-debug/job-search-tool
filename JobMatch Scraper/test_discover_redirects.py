"""Offline discovery regressions: retain tenant paths without moving root API endpoints.

Run directly: python test_discover_redirects.py
"""
from contextlib import ExitStack
from unittest import TestCase, main
from unittest.mock import Mock, patch

import scraper
from scraper import find_everify_boards as feb


def response(url, text="", data=None, status=200):
    return Mock(url=url, text=text, status_code=status,
                headers={"content-type": "application/json"},
                json=Mock(return_value=data or {}))


class DiscoveryRedirectTests(TestCase):
    def test_greenhouse_script_embed_keeps_tenant(self):
        self.get.return_value = response("https://careers.aqr.com/", '<script src="https://boards.greenhouse.io/embed/job_board/js?for=aqr"></script>')
        hit = scraper.detect_linked_ats("https://careers.aqr.com/")
        self.assertEqual(hit[:2], ("https://job-boards.greenhouse.io/aqr", "greenhouse"))

    def test_ashby_encoded_spaces_keep_complete_tenant(self):
        url = "https://jobs.ashbyhq.com/Tools%20For%20Humanity"
        self.get.return_value = response("https://example.com/careers", '<iframe src="' + url + '"></iframe>')
        hit = scraper.detect_linked_ats("https://example.com/careers")
        self.assertEqual(hit[:2], (url, "ashby"))

    def test_avature_location_is_not_discarded_with_requisition_metadata(self):
        from bs4 import BeautifulSoup
        card = BeautifulSoup('<article><div class="article__header__text__subtitle"><span>India, Karnataka, BANGALORE</span><br><span>Req #: WD123</span><br><span>Posted 15-Sep-2026</span></div></article>', 'html.parser')
        location = scraper._avature_location(card, 'https://jobs.lenovo.com/en_US/careers/JobDetail/Example/1')
        self.assertEqual(location, 'India, Karnataka, BANGALORE')
        self.assertFalse(scraper.is_us_location(location))

    def test_pinpoint_feed_uses_verified_vanity_host(self):
        with patch.object(scraper, '_get_json', return_value={'data': []}) as get:
            self.assertEqual(scraper.scrape_pinpoint('https://careers.infor.com/'), [])
        get.assert_called_once_with('https://careers.infor.com/postings.json')

    def test_avature_vanity_domain_keeps_language_and_portal(self):
        html = '<meta name="avature.portal.urlPath" content="careers"><meta name="avature.portal.lang" content="en_US"><meta name="avature.portal.name" content="Jobs at Lenovo">'
        self.assertEqual(scraper.avature_from_html(html, 'https://jobs.lenovo.com/en_US/careers'),
                         ('https://jobs.lenovo.com/en_US/careers/SearchJobs', 'avature', 'Jobs at Lenovo'))
        self.assertIsNone(scraper.avature_from_html('<a href="/SearchJobs">Jobs</a>', 'https://example.com'))

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(feb, "_simple_ats", return_value=None))
        self.candidates = self.stack.enter_context(patch.object(
            feb, "_careers_candidates", return_value=["https://jobs.acme.com"]))
        # Every network entry point used by discovery is replaced, including Eightfold's
        # direct SESSION call. An unexpected request cannot reach a real host or database.
        self.session = self.stack.enter_context(patch.object(scraper, "SESSION"))
        self.get = self.stack.enter_context(patch.object(
            scraper, "_safe_get", return_value=response("", status=404)))
        self.post = self.stack.enter_context(patch.object(
            scraper, "_safe_post", return_value=response("", status=404)))
        self.probe = self.stack.enter_context(patch.object(
            scraper, "probe_board", return_value=17))

    def redirect(self, landing):
        self.session.get.return_value = response(landing)

    def test_workday_redirect_preserves_site_and_locale(self):
        landing = "https://acme.wd1.myworkdayjobs.com/en-US/External?source=careers"
        self.redirect(landing)
        self.get.return_value = response(landing)
        self.assertEqual(feb.discover("Acme"), (
            "Acme", "https://acme.wd1.myworkdayjobs.com/External", "workday", 17, "high"))
        self.get.assert_called_once_with(landing, timeout=15)
        self.probe.assert_called_once_with(
            "https://acme.wd1.myworkdayjobs.com/External", "workday")
        self.session.get.return_value.close.assert_called_once()

    def test_workdaysite_redirect_preserves_recruiting_tenant(self):
        landing = "https://wd5.myworkdaysite.com/recruiting/acme/External"
        self.redirect(landing)
        self.get.return_value = response(landing)
        self.assertEqual(feb.discover("Acme"), (
            "Acme", landing, "workday", 17, "high"))
        self.get.assert_called_once_with(landing, timeout=15)

    def test_careers_landing_path_is_scanned_for_links(self):
        landing = "https://www.acme.com/about/careers?region=us"
        board = "https://jobs.lever.co/acme"
        self.redirect(landing)
        self.get.return_value = response(landing, '<a href="%s">View jobs</a>' % board)
        self.assertEqual(feb.discover("Acme"), ("Acme", board, "lever", 17, "high"))
        self.get.assert_called_once_with(landing, timeout=15)

    def test_own_host_detectors_still_use_root_endpoints(self):
        origin = "https://talent.acme.com"
        landing = origin + "/us/en/search?region=us"
        # Exercise the real detector implementations. SuccessFactors, Jibe and Eightfold
        # must remain reachable when earlier detectors do not recognize this employer.
        for ats in ("phenom", "successfactors", "jibe", "eightfold"):
            with self.subTest(ats=ats):
                requested = []

                def get(url, **kwargs):
                    requested.append(url)
                    if ats == "successfactors" and url == origin + "/search/?q=&startrow=0":
                        return response(url, '<tr class="data-row"><td><a class="jobTitle-link">Engineer</a></td></tr>')
                    if ats == "jibe" and url == origin + "/api/jobs?limit=1":
                        return response(url, data={"jobs": [{"data": {"brand": "Acme"}}],
                                                   "totalCount": 17})
                    return response(url, status=200 if url == landing else 404)

                def post(url, *args, **kwargs):
                    requested.append(url)
                    if ats == "phenom" and url == origin + "/widgets":
                        return response(url, data={"refineSearch": {"totalHits": 17,
                                                                    "data": {"jobs": []}}})
                    return response(url, status=404)

                def session_get(url, **kwargs):
                    if kwargs.get("stream"):
                        return response(landing)
                    requested.append(url)
                    if ats == "eightfold" and url == origin + "/api/apply/v2/jobs":
                        return response(url, data={"count": 17, "positions": [{"id": 1}]})
                    return response(url, status=404)

                self.get.side_effect = get
                self.post.side_effect = post
                self.session.get.side_effect = session_get
                result = feb.discover("Acme")
                self.assertEqual(result[2:], (ats, 17, "high"))
                self.assertEqual(requested[0], landing)
                self.assertTrue(all(url.startswith(origin + "/") for url in requested))
                self.assertFalse(any("/us/en/search/" in url for url in requested))

    def test_same_origin_different_pages_are_both_scanned(self):
        origin = "https://www.acme.com"
        first, second = origin + "/careers", origin + "/careers/engineering"
        self.candidates.return_value = ["https://jobs.acme.com", "https://careers.acme.com"]
        self.session.get.side_effect = [response(first), response(second)]
        self.get.side_effect = lambda url, **kw: response(
            url, '<a href="https://jobs.lever.co/acme">Jobs</a>' if url == second else "")
        # Root detectors are attempted once for the first page, then reused for that host.
        own = [self.stack.enter_context(patch.object(scraper, name, return_value=None))
               for name in ("detect_phenom", "detect_successfactors", "detect_jibe", "detect_eightfold")]
        self.assertEqual(feb.discover("Acme")[1:3], ("https://jobs.lever.co/acme", "lever"))
        self.assertEqual([call.args[0] for call in self.get.call_args_list], [first, second])
        for detector in own:
            detector.assert_called_once_with(origin)

    def test_duplicate_landing_page_is_scanned_only_once(self):
        landing = "https://talent.acme.com/us/en/search"
        self.candidates.return_value = ["https://jobs.acme.com", "https://careers.acme.com"]
        self.redirect(landing)
        linked = self.stack.enter_context(patch.object(scraper, "detect_linked_ats", return_value=None))
        own = [self.stack.enter_context(patch.object(scraper, name, return_value=None))
               for name in ("detect_phenom", "detect_successfactors", "detect_jibe", "detect_eightfold")]
        self.assertEqual(feb.discover("Acme"), ("Acme", None, "needs-deeper-lookup", 0, ""))
        linked.assert_called_once_with(landing)
        for detector in own:
            detector.assert_called_once_with("https://talent.acme.com")


if __name__ == "__main__":
    main(verbosity=2)
