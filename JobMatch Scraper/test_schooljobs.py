"""Offline tests for public NEOGOV listing ranges, duplicates, and job details."""
import unittest
from unittest.mock import Mock, patch
import scraper
from scraper import schooljobs as sj
from scraper.infosys import PartialScrapeError

BOARD = "https://www.schooljobs.com/careers/example"


def page(number=1, total=12, identifiers=None):
    start, end = (number - 1) * 10 + 1, min(number * 10, total)
    ids = identifiers if identifiers is not None else list(range(start, end + 1))
    rows = "".join('''<li class="list-item" data-job-id="%s"><h3>
        <a class="item-details-link" href="/careers/example/jobs/%s/data-analyst">Data Analyst</a>
        </h3><button data-title="Orem, UT Data Analyst"></button></li>''' % (i, i) for i in ids)
    table = "".join('''<tr><th class="job-table-title" data-job-id="%s"></th>
        <td class="job-table-posted">09/18/26</td></tr>''' % i for i in ids)
    return '''<span id="job-postings-number">%s</span><ul>%s</ul><table>%s</table>
        <ul class="pagination"><li class="active">%s</li></ul>
        <div class="items-div"><span>%s</span><span>%s</span></div>''' % (total, rows, table, number, start, end)


def response(source, url=None):
    return Mock(text=source, url=url or sj._page_url(BOARD, 1))


class SchoolJobsTests(unittest.TestCase):
    def test_canonical_board_and_job(self):
        self.assertEqual(sj.board_url(BOARD + "/jobs/123/data-analyst?x=1"), BOARD)
        self.assertEqual(sj.board_url("http://schooljobs.com/careers/example"), BOARD)
        self.assertEqual(scraper.detect_board(BOARD)[:2], (BOARD, "schooljobs"))
        self.assertIs(scraper.SCRAPERS["schooljobs"], sj.scrape_schooljobs)

    def test_unsafe_and_restricted_urls_refused(self):
        for url in [BOARD + "/WorkStudy/promotionaljobs", BOARD + "/Staff", 
                    "https://schooljobs.com.evil.org/careers/example",
                    "https://user:pass@www.schooljobs.com/careers/example",
                    "https://www.schooljobs.com/careers/home"]:
            self.assertIsNone(sj.board_url(url))

    def test_listing_fields_and_exact_dates(self):
        rows, count = sj._parse(page(), BOARD, 1)
        self.assertEqual(count, 12)
        self.assertEqual(len(rows), 10)
        self.assertEqual(rows[0]["location"], "Orem, UT")
        self.assertEqual(rows[0]["date_posted"], "2026-09-18")
        self.assertEqual(rows[0]["url"], BOARD + "/jobs/1/data-analyst")

    def test_page_number_and_range_must_match(self):
        with self.assertRaises(ValueError):
            sj._parse(page(), BOARD, 2)
        with self.assertRaises(ValueError):
            sj._parse(page().replace('<span>1</span>', '<span>2</span>'), BOARD, 1)

    def test_missing_rows_and_wrong_employer_fail(self):
        for source in [page(identifiers=[1]), page().replace('/careers/example/jobs/', '/careers/other/jobs/')]:
            with self.assertRaises(ValueError):
                sj._parse(source, BOARD, 1)

    def test_missing_count_and_nonlisting_fail(self):
        with self.assertRaises(ValueError):
            sj._parse("<h1>Welcome to SchoolJobs</h1>", BOARD, 1)

    @patch("scraper.schooljobs.time.sleep")
    @patch("scraper.schooljobs._fetch")
    def test_complete_pages(self, fetch, sleep):
        fetch.side_effect = [sj._parse(page(), BOARD, 1), sj._parse(page(2), BOARD, 2)]
        self.assertEqual(len(sj.scrape_schooljobs(BOARD)), 12)
        self.assertEqual(fetch.call_count, 2)

    @patch("scraper.schooljobs.time.sleep")
    @patch("scraper.schooljobs._fetch")
    def test_vendor_duplicate_posting_deduped_after_full_ranges(self, fetch, sleep):
        fetch.side_effect = [sj._parse(page(), BOARD, 1),
                             sj._parse(page(2, identifiers=[10, 12]), BOARD, 2)]
        self.assertEqual(len(sj.scrape_schooljobs(BOARD)), 11)
        self.assertEqual(fetch.call_count, 2)

    @patch("scraper.schooljobs.time.sleep")
    @patch("scraper.schooljobs._fetch")
    def test_repeated_whole_page_stays_partial(self, fetch, sleep):
        fetch.side_effect = [sj._parse(page(total=20), BOARD, 1)] * 2
        with self.assertRaises(PartialScrapeError) as caught:
            sj.scrape_schooljobs(BOARD)
        self.assertEqual(len(caught.exception.rows), 10)

    @patch("scraper.schooljobs.time.sleep")
    @patch("scraper.schooljobs._fetch")
    def test_total_changes_and_http_failures_keep_rows(self, fetch, sleep):
        for second in [RuntimeError("network"), ([], 13)]:
            fetch.side_effect = [sj._parse(page(), BOARD, 1), second]
            with self.assertRaises(PartialScrapeError) as caught:
                sj.scrape_schooljobs(BOARD)
            self.assertEqual(len(caught.exception.rows), 10)

    @patch("scraper.schooljobs._fetch")
    def test_initial_failure_is_not_empty_success(self, fetch):
        fetch.side_effect = RuntimeError("network")
        with self.assertRaises(RuntimeError):
            sj.scrape_schooljobs(BOARD)

    @patch("scraper._safe_get")
    def test_vendor_alias_keeps_same_agency(self, get):
        government = BOARD.replace("schooljobs.com", "governmentjobs.com")
        get.return_value = response(page())
        self.assertEqual(len(sj._fetch(government, 1)[0]), 10)
        self.assertEqual(get.call_args.kwargs["headers"]["X-Requested-With"], "XMLHttpRequest")
        get.return_value = response(page(), sj._page_url(BOARD.replace("example", "other"), 1))
        with self.assertRaises(ValueError):
            sj._fetch(government, 1)

    @patch("scraper.schooljobs._fetch")
    def test_probe_validates_rows(self, fetch):
        fetch.return_value = sj._parse(page(), BOARD, 1)
        self.assertEqual(sj.probe_schooljobs(BOARD), 12)
        fetch.return_value = ([], 12)
        with self.assertRaises(ValueError):
            sj.probe_schooljobs(BOARD)

    def test_detail_excludes_privacy_and_navigation(self):
        text = "Analyze research data and improve student services. " * 4
        result = sj.description_html('<nav>privacy policy</nav><div id="details-info">' + text + '</div>')
        self.assertIn(text, result)
        self.assertNotIn("privacy", result)
        self.assertEqual(sj.description_html('<nav>privacy policy</nav>'), "")

    @patch("core.requests.get")
    def test_core_detail_hook(self, get):
        from core import fetch_jd
        get.return_value = response('<nav>privacy policy</nav><div id="details-info">' + "Analyze research data. " * 10 + '</div>')
        result = fetch_jd(BOARD + '/jobs/1/data-analyst')
        self.assertIn("Analyze research data", result)
        self.assertNotIn("privacy", result)


if __name__ == "__main__":
    unittest.main()
