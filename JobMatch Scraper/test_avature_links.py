"""Regression coverage for Avature's filter-preserving, linked pagination."""
import unittest
from unittest.mock import Mock, patch

import requests
import scraper
from scraper import avature_links as av


def page(jid=1, count="2", nxt="", location="Multiple Locations"):
    card = (('<article class="article--result"><a href="/en_US/externaljobs/JobDetail/%s">'
             'Engineer</a><span class="list-item-location">%s</span></article>') % (jid, location)) if jid else ""
    return ('<div class="list-controls__text__legend">1 - 1 of %s results</div>%s%s' %
            (count, card, '<a aria-label="Go to Next Page, Number 2" href="%s">Next &gt;&gt;</a>' % nxt if nxt else ""))


def response(html, status=200):
    result = Mock(text=html)
    if status >= 400:
        result.raise_for_status.side_effect = requests.HTTPError(str(status))
    return result


class AvatureLinksTests(unittest.TestCase):
    def test_follows_published_folderoffset_for_jobdetail_and_keeps_us_filter(self):
        nxt = av.SIEMENS_BOARD + "&folderOffset=6&folderRecordsPerPage=6"
        with patch.object(scraper, "_safe_get", side_effect=[response(page(nxt=nxt)), response(page(2))]) as get:
            rows = av.scrape_avature_links(av.SIEMENS_BOARD)
        self.assertEqual(len(rows), 2)
        self.assertEqual(get.call_args_list[1].args[0], nxt)
        self.assertEqual(rows[0]["location"], "Multiple Locations, United States")
        self.assertEqual(rows[0]["company"], "Siemens")

    def test_lower_bound_total_does_not_stop_before_next_link(self):
        nxt = av.SIEMENS_BOARD + "&folderOffset=6"
        with patch.object(scraper, "_safe_get", side_effect=[response(page(count="1+", nxt=nxt)), response(page(2, count="1+"))]):
            self.assertEqual(len(av.scrape_avature_links(av.SIEMENS_BOARD)), 2)
        self.assertEqual(av.parse_page(page(count="999+"), av.SIEMENS_BOARD)[2:], (1000, True))

    def test_later_fetch_failure_keeps_first_page(self):
        nxt = av.SIEMENS_BOARD + "&folderOffset=6"
        with patch.object(scraper, "_safe_get", side_effect=[response(page(nxt=nxt)), response("blocked", 403)]):
            with self.assertRaises(av.PartialScrapeError) as raised:
                av.scrape_avature_links(av.SIEMENS_BOARD)
        self.assertEqual(len(raised.exception.rows), 1)

    def test_next_link_cannot_drop_country_filter_or_change_host(self):
        for nxt in ("?folderOffset=6", "https://example.com/en_US/externaljobs/SearchJobs/"):
            with self.subTest(nxt=nxt), patch.object(scraper, "_safe_get", return_value=response(page(nxt=nxt))) as get:
                with self.assertRaises(av.PartialScrapeError) as raised:
                    av.scrape_avature_links(av.SIEMENS_BOARD)
            self.assertEqual(len(raised.exception.rows), 1)
            get.assert_called_once()

    def test_repeated_page_is_incomplete(self):
        nxt = av.SIEMENS_BOARD + "&folderOffset=6"
        with patch.object(scraper, "_safe_get", side_effect=[response(page(nxt=nxt)), response(page(nxt=nxt))]):
            with self.assertRaises(av.PartialScrapeError) as raised:
                av.scrape_avature_links(av.SIEMENS_BOARD)
        self.assertEqual(len(raised.exception.rows), 1)

    def test_missing_tail_and_shell_are_not_success(self):
        for html in (page(), "Please enable Javascript", page(count="999+")):
            with self.subTest(html=html), patch.object(scraper, "_safe_get", return_value=response(html)):
                with self.assertRaises(av.PartialScrapeError):
                    av.scrape_avature_links(av.SIEMENS_BOARD)

    def test_explicit_zero_is_success(self):
        with patch.object(scraper, "_safe_get", return_value=response(page(jid=None, count="0"))):
            self.assertEqual(av.scrape_avature_links(av.SIEMENS_BOARD), [])

    def test_unfiltered_board_does_not_invent_us_location(self):
        rows = av.parse_page(page(), "https://jobs.siemens.com/en_US/externaljobs/SearchJobs")[0]
        self.assertEqual(rows[0]["location"], "Multiple Locations")

    def test_new_head_posting_reconciles_live_offset_drift(self):
        nxt = av.SIEMENS_BOARD + "&folderOffset=6"
        replies = [response(page(1, count="3", nxt=nxt)), response(page(2, count="3")),
                   response(page(3, count="3", nxt=nxt)), response(page(2, count="3"))]
        with patch.object(scraper, "_safe_get", side_effect=replies) as get:
            rows = av.scrape_avature_links(av.SIEMENS_BOARD)
        self.assertEqual(len(rows), 3)
        self.assertEqual([call.args[0] for call in get.call_args_list],
                         [av.SIEMENS_BOARD, nxt, av.SIEMENS_BOARD, nxt])

    def test_reconciliation_cannot_claim_success_when_tail_grows(self):
        nxt = av.SIEMENS_BOARD + "&folderOffset=6"
        replies = [response(page(1, count="3", nxt=nxt)), response(page(2, count="3")),
                   response(page(3, count="3", nxt=nxt)), response(page(2, count="4", nxt=nxt + "&new=1"))]
        with patch.object(scraper, "_safe_get", side_effect=replies):
            with self.assertRaises(av.PartialScrapeError) as raised:
                av.scrape_avature_links(av.SIEMENS_BOARD)
        self.assertEqual(len(raised.exception.rows), 3)

    def test_cap_preserves_partial_rows(self):
        with patch.object(av, "MAX_PAGES", 1), patch.object(scraper, "_safe_get", return_value=response(page(nxt=av.SIEMENS_BOARD + "&folderOffset=6"))):
            with self.assertRaises(av.PartialScrapeError) as raised:
                av.scrape_avature_links(av.SIEMENS_BOARD)
        self.assertEqual(len(raised.exception.rows), 1)

    def test_detail_excludes_navigation_and_reads_actual_posted_date(self):
        html = ('<nav>Login</nav><article class="article--details"><div class="article__content">'
                '<div class="article__content__view__field">Posted since 18-Sep-2026</div>'
                '</div></article><article class="article--details"><div class="article__content">'
                'Build Python systems.</div></article><footer>Siemens corporate info</footer>')
        with patch.object(scraper, "_safe_get", return_value=response(html)):
            jd, date = av.detail_jd("https://jobs.siemens.com/en_US/externaljobs/JobDetail/123")
        self.assertIn("Build Python systems.", jd)
        self.assertNotIn("Login", jd)
        self.assertNotIn("corporate info", jd)
        self.assertEqual(date, "2026-09-18")

    def test_registration_and_detection(self):
        self.assertIs(scraper.SCRAPERS["avature_links"], av.scrape_avature_links)
        self.assertEqual(scraper.detect_board("https://jobs.siemens.com/en_US/externaljobs/JobDetail/123"),
                         (av.SIEMENS_BOARD, "avature_links", "Siemens"))

    def test_energy_us_listing_keeps_the_us_route_and_honest_country(self):
        html = page(count="1", location="").replace("/JobDetail/1", "/FolderDetail/Engineer/1")
        with patch.object(scraper, "_safe_get", return_value=response(html)):
            rows = av.scrape_avature_links(av.SIEMENS_ENERGY_BOARD)
        self.assertEqual(rows[0]["location"], "United States")
        self.assertEqual(rows[0]["company"], "Siemens Energy")
        self.assertEqual(scraper.detect_board(av.SIEMENS_ENERGY_BOARD),
                         (av.SIEMENS_ENERGY_BOARD, "avature_links", "Siemens Energy"))

    def test_scorer_uses_posting_articles(self):
        from scraper import score_jobs
        url = "https://jobs.siemens.com/en_US/externaljobs/JobDetail/123"
        with patch.object(av, "detail_jd", return_value=("Siemens full description", "2026-09-18")):
            self.assertEqual(score_jobs.detail_jd(url), (url, "Siemens full description", "2026-09-18"))


if __name__ == "__main__":
    unittest.main()
