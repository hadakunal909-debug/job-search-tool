"""Regression coverage for the public Infosys listing and partial-result retention."""
import unittest
import json
from unittest.mock import patch
import requests
import scraper
from scraper.infosys import BOARD, PartialScrapeError, parse_page, scrape_infosys, detail_jd, probe_infosys
from scraper import infosys


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


SHELL = '<script src="https://example.cloudfront.net/assets/infosys/merged/js/listing.js"></script>'
SCRIPT = "const s=algoliasearch('ABCDEFGHIJ','12345678901234567890123456789012'); const o={indexName:'production_Infosys_jobs'};"


def hit(reqid="123BR", country="USA", location="Austin, TX"):
    return {"title": "Data Engineer", "reqid": [reqid], "country": [country],
            "work_location": [location]}


def search_response(hits, total, page_number=0):
    return response(json.dumps({"hits": hits, "nbHits": total, "page": page_number}))


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

    def test_modern_public_search_pages_all_us_results(self):
        with patch.object(scraper, "_safe_get", side_effect=[response(SHELL), response(SCRIPT)]), \
                patch.object(scraper, "_safe_post", side_effect=[search_response([hit()], 2),
                    search_response([hit("124BR")], 2, 1)]) as post:
            rows = scrape_infosys()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["location"], "Austin, TX, USA")
        self.assertTrue(rows[0]["url"].endswith("/reqid/123BR"))
        self.assertEqual([c.args[1]["page"] for c in post.call_args_list], [0, 1])
        self.assertEqual(post.call_args.args[1]["facetFilters"], ["country:USA"])

    def test_modern_search_failure_preserves_obtained_rows(self):
        with patch.object(scraper, "_safe_get", side_effect=[response(SHELL), response(SCRIPT)]), \
                patch.object(scraper, "_safe_post", side_effect=[search_response([hit()], 2),
                                                                  requests.Timeout()]):
            with self.assertRaises(PartialScrapeError) as raised:
                scrape_infosys()
        self.assertEqual(len(raised.exception.rows), 1)

    def test_modern_search_refuses_foreign_or_malformed_rows(self):
        for bad in (hit(country="India"), hit(reqid="../../login"), hit(location=""),
                    hit(location={"city": "Austin"})):
            with self.subTest(bad=bad), patch.object(infosys, "_search_page",
                    return_value=([hit(), bad], 2)):
                with self.assertRaises(PartialScrapeError) as raised:
                    infosys._scrape_search(("ABCDEFGHIJ", "public", "production_Infosys_jobs"))
            self.assertEqual(len(raised.exception.rows), 1)

    def test_modern_search_repeats_missing_count_and_early_end_are_incomplete(self):
        for second in ([hit()], []):
            with self.subTest(second=second), patch.object(infosys, "_search_page",
                    side_effect=[([hit()], 2), (second, 2)]):
                with self.assertRaises(PartialScrapeError) as raised:
                    infosys._scrape_search(("ABCDEFGHIJ", "public", "production_Infosys_jobs"))
            self.assertEqual(len(raised.exception.rows), 1)
        with patch.object(scraper, "_safe_post", return_value=response('{"hits":[],"page":0}')):
            with self.assertRaises(ValueError):
                infosys._search_page(("ABCDEFGHIJ", "public", "production_Infosys_jobs"))

    def test_modern_search_requires_an_exact_count_and_the_requested_page(self):
        for data in ({"hits": [hit()], "nbHits": 0, "page": 0},
                     {"hits": [hit()], "nbHits": 1, "page": False},
                     {"hits": [hit()], "nbHits": 1, "page": 1},
                     {"hits": [hit()], "nbHits": 1, "page": 0, "exhaustiveNbHits": False}):
            with self.subTest(data=data), patch.object(scraper, "_safe_post",
                                                      return_value=response(json.dumps(data))):
                with self.assertRaises(ValueError):
                    infosys._search_page(("ABCDEFGHIJ", "public", "production_Infosys_jobs"))

    def test_modern_count_falling_or_unique_rows_exceeding_total_stays_partial(self):
        for pages, retained in (([([hit()], 2), ([hit("124BR")], 1)], 2),
                                ([([hit()], 2), ([hit("124BR"), hit("125BR")], 2)], 3)):
            with self.subTest(pages=pages), patch.object(infosys, "_search_page", side_effect=pages):
                with self.assertRaises(PartialScrapeError) as raised:
                    infosys._scrape_search(("ABCDEFGHIJ", "public", "production_Infosys_jobs"))
            self.assertEqual(len(raised.exception.rows), retained)

    def test_modern_search_zero_and_probe_use_advertised_count(self):
        with patch.object(infosys, "_search_page", return_value=([], 0)):
            self.assertEqual(infosys._scrape_search(("ABCDEFGHIJ", "public", "production_Infosys_jobs")), [])
        with patch.object(scraper, "_safe_get", side_effect=[response(SHELL), response(SCRIPT)]), \
                patch.object(scraper, "_safe_post", return_value=search_response([hit()], 677)):
            self.assertEqual(probe_infosys(), 677)

    def test_board_probe_dispatches_to_the_modern_infosys_probe(self):
        with patch.object(infosys, "probe_infosys", return_value=677) as probe:
            self.assertEqual(scraper.probe_board(BOARD, "infosys"), 677)
        probe.assert_called_once_with(BOARD)

    def test_unrelated_script_cannot_be_used_as_the_search_tenant(self):
        shell = SHELL.replace("example.cloudfront.net", "unrelated.example")
        with patch.object(scraper, "_safe_get") as get:
            with self.assertRaises(ValueError):
                infosys._search_config(shell, BOARD)
            get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
