"""Regression coverage for the public Infosys listing and partial-result retention."""
import unittest
from unittest.mock import patch
import requests
import scraper
from scraper.infosys import BOARD, PartialScrapeError, parse_page, scrape_infosys, detail_jd


def page(job, total=2, next_url=""):
    return ('<p>Showing 1 to 1 of %s matching jobs</p><a class="job" '
            'href="/global-careers/company-job/description/reqid/%s">'
            '<div class="job-title">Data Engineer</div><div class="js-job-city">Austin, TX - USA</div>'
            '</a>%s' % (total, job, '<a class="pagination-button" href="%s">Next</a>' % next_url if next_url else ""))


def response(html, status=200):
    r = requests.Response()
    r.status_code = status
    r._content = html.encode()
    return r


class InfosysTests(unittest.TestCase):
    def test_follows_real_pagination_without_discarding_filters(self):
        nxt = BOARD.replace("page=1", "page=2")
        with patch.object(scraper, "_safe_get", side_effect=[response(page("1", next_url=nxt)), response(page("2"))]) as get:
            rows = scrape_infosys(BOARD)
        self.assertEqual(len(rows), 2)
        self.assertEqual(get.call_args_list[1].args[0], nxt)
        self.assertEqual(rows[0]["location"], "Austin, TX - USA")

    def test_partial_failure_keeps_first_page(self):
        with patch.object(scraper, "_safe_get", side_effect=[response(page("1", next_url=BOARD+"&next=1")), response("blocked", 403)]):
            with self.assertRaises(PartialScrapeError) as raised:
                scrape_infosys(BOARD)
        self.assertEqual(len(raised.exception.rows), 1)

    def test_missing_next_link_is_incomplete(self):
        with patch.object(scraper, "_safe_get", return_value=response(page("1"))):
            with self.assertRaises(PartialScrapeError):
                scrape_infosys(BOARD)

    def test_block_shell_is_not_empty_board(self):
        with patch.object(scraper, "_safe_get", return_value=response("enable javascript")):
            with self.assertRaises(PartialScrapeError):
                scrape_infosys(BOARD)

    def test_legitimate_zero(self):
        self.assertEqual(parse_page("Showing 0 to 0 of 0 matching jobs", BOARD), ([], "", 0))

    def test_description_excludes_site_navigation(self):
        html = '<nav>Sign in</nav><div class="description-page-right"><h2>Required skills</h2><p>Python and SQL</p></div><footer>Cookie settings</footer>'
        with patch.object(scraper, "_safe_get", return_value=response(html)):
            jd = detail_jd("https://digitalcareers.infosys.com/global-careers/company-job/description/reqid/1")
        self.assertEqual(jd, "Required skills\nPython and SQL")


if __name__ == "__main__":
    unittest.main()
