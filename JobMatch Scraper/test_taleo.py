"""Offline regressions for the public Taleo protocols and incomplete page safety."""
import json
import unittest
from unittest.mock import Mock, patch
from urllib.parse import quote

import scraper
from scraper import taleo
from scraper.infosys import PartialScrapeError

BOARD = "https://example.taleo.net/careersection/staff/jobsearch.ftl"
FIELDS = ["reqlistitem.contestnumber", "reqlistitem.basiclocations",
          "reqlistitem.title", "reqlistitem.postingdate"]


def response(text="", data=None, url=BOARD):
    item = Mock(text=text, url=url)
    item.json.return_value = data
    return item


def classic(total=2):
    return """<form id="ftlform" action="jobsearch.ftl">
      <input name="listRequisition.nbElements" value="%s">
      <input name="rlPager.currentPage" value="1">
      <input name="csrfToken" value="opaque-public-session">
      <input id="search.sortBy" name="dropSortBy" value="wrong">
    </form><script>
    var definitions={listRequisition:{_hlid:%r},search:{_ctls:['sortBy']}};
    api.fillList('requisitionListInterface','listRequisition',%r);
    api.fillForm('search',['TITLE']);
    </script>""" % (total, FIELDS, ["123", "US-Maryland-Baltimore", "Data Analyst", "Sep 18, 2026"])


def ajax(page=2, total=2, identifier="124", error=""):
    fields = ["ftlerrors", error, "rlPager.currentPage", str(page),
              "listRequisition.nbElements", str(total), "csrfToken", "next-token"]
    return "ftlPager_processResponse!$!meta!$!%s!$!%s" % (
        "!|!".join([identifier, "US-Georgia-Atlanta", "Research Analyst", "09/18/26"]),
        "!|!".join(fields))


def modern(page=1, total=2, identifiers=("123",), page_size=1):
    return {"pagingData": {"totalCount": total, "currentPageNo": page, "pageSize": page_size},
            "requisitionList": [{"contestNo": item, "jobId": "internal-" + item,
               "column": ["Data &amp; Research Analyst", '["US-Pennsylvania-Pittsburgh"]'],
               "linkedColumn": 0, "locationsColumns": [1]} for item in identifiers]}


def detail():
    interface = "requisitionDescriptionInterface"
    fields = ["description", "visaValue"]
    values = ["!*!" + quote("<p>Analyze university research data and build reporting systems. "
                            "Work with scientists and administrators to improve data quality.</p>"), "No"]
    return """<nav>Unrelated employer navigation</nav><div id="%s.descRequisitionContainer">
       <h2 id="%s.description"></h2><span id="%s.visaLabel"></span>
       <span id="%s.visaValue"></span><input value="secret"><script>unwanted()</script></div>
       <script>var d={%s:{_hles:['visaLabel'],descRequisition:{_hles:%r}}};
       api.fillInterface('%s',['Visa Sponsorship Provided']);
       api.fillList('%s','descRequisition',%r);</script>""" % (
           interface, interface, interface, interface, interface, fields, interface, interface, values)


class TaleoTests(unittest.TestCase):
    def test_canonical_enterprise_section_and_portal(self):
        url = "https://umb.taleo.net/careersection/umb_faculty+and+post+docs/jobdetail.ftl?job=12&lang=en&portal=8100108441"
        self.assertEqual(taleo.board_url(url), "https://umb.taleo.net/careersection/umb_faculty+and+post+docs/jobsearch.ftl?portal=8100108441")
        self.assertEqual(scraper.detect_board(url)[:2], (taleo.board_url(url), "taleo"))

    def test_only_exact_enterprise_hosts_and_public_pages(self):
        for url in ["https://phe.tbe.taleo.net/careersection/2/jobsearch.ftl",
                    "https://example.taleo.net.evil.com/careersection/2/jobsearch.ftl",
                    "https://name:password@example.taleo.net/careersection/2/jobsearch.ftl",
                    "https://example.taleo.net/careersection/staff/mysubmissions.ftl"]:
            self.assertIsNone(taleo.board_url(url), url)

    def test_registry(self):
        self.assertIs(scraper.SCRAPERS["taleo"], taleo.scrape_taleo)

    @patch("scraper.taleo.time.sleep")
    @patch("scraper._safe_post")
    @patch("scraper._safe_get")
    def test_modern_pages_and_published_title_sort(self, get, post, sleep):
        get.return_value = response("portalNo:'999'<span id='JOB_TITLE-sortfield' sortid='5'></span>")
        observed = []
        def fetch(url, body, **kwargs):
            observed.append((url, dict(body)))
            return response(data=modern(page=body["pageNo"], identifiers=(str(body["pageNo"]),)))
        post.side_effect = fetch
        rows = taleo.scrape_taleo(BOARD)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["location"], "US, Pennsylvania, Pittsburgh")
        self.assertIn("job=1", rows[0]["url"])
        self.assertEqual([x[1]["pageNo"] for x in observed], [1, 2])
        self.assertEqual(observed[0][1]["sortingSelection"]["sortBySelectionParam"], "5")
        self.assertIn("portal=999", observed[0][0])

    @patch("scraper.taleo.time.sleep")
    @patch("scraper._safe_post")
    def test_advertised_but_nonpublic_jobs_stay_partial(self, post, sleep):
        post.side_effect = [response(data=modern(1, 3, ("1",), 2)),
                            response(data=modern(2, 3, ("2",), 2))]
        with self.assertRaises(PartialScrapeError) as caught:
            taleo._modern_scrape(BOARD, response(), "999")
        self.assertEqual(len(caught.exception.rows), 2)
        self.assertIn("2 public rows of 3", str(caught.exception))
        self.assertEqual(post.call_count, 2)

    @patch("scraper.taleo.time.sleep")
    @patch("scraper._safe_post")
    def test_later_http_failure_keeps_first_page(self, post, sleep):
        failed = response()
        failed.raise_for_status.side_effect = RuntimeError("HTTP unavailable")
        post.side_effect = [response(data=modern()), failed]
        with self.assertRaises(PartialScrapeError) as caught:
            taleo._modern_scrape(BOARD, response(), "999")
        self.assertEqual(len(caught.exception.rows), 1)

    @patch("scraper._safe_post")
    def test_first_http_failure_is_not_empty_success(self, post):
        post.side_effect = RuntimeError("network")
        with self.assertRaises(RuntimeError):
            taleo._modern_scrape(BOARD, response(), "999")

    def test_bad_metadata_and_missing_title_rejected(self):
        cases = [modern(page=2), {"pagingData": {}, "requisitionList": []}]
        bad = modern()
        bad["requisitionList"][0]["linkedColumn"] = 10
        cases.append(bad)
        for data in cases:
            with self.assertRaises(ValueError):
                taleo._modern_page(data, BOARD, 1)

    def test_duplicate_rows_never_reach_total(self):
        row = {"url": "example"}
        for batch, previous in [([row, row], set()), ([row], {"example"})]:
            with self.assertRaises(PartialScrapeError):
                taleo._collect([], batch, previous, 2)

    def test_classic_field_map_not_fixed_offsets(self):
        fields = list(reversed(FIELDS))
        values = list(reversed(["42", "US-Georgia-Atlanta", "Research Analyst", "09/18/26"]))
        rows = taleo._classic_rows(values, fields, BOARD)
        self.assertEqual(rows[0]["title"], "Research Analyst")
        self.assertEqual(rows[0]["date_posted"], "2026-09-18")
        self.assertIn("job=42", rows[0]["url"])

    def test_classic_control_ids_resolve_to_posted_names(self):
        rows, fields, data, count, endpoint = taleo._classic_setup(classic(), BOARD)
        self.assertEqual(data["dropSortBy"], "TITLE")
        self.assertEqual(data["csrfToken"], "opaque-public-session")
        self.assertEqual(endpoint, BOARD.replace(".ftl", ".ajax"))
        self.assertEqual(count, 2)

    @patch("scraper._safe_form_post")
    @patch("scraper._safe_get")
    def test_classic_follows_actual_form_protocol(self, get, post):
        get.return_value = response(classic())
        post.return_value = response(ajax())
        rows = taleo.scrape_taleo(BOARD)
        self.assertEqual(len(rows), 2)
        data = post.call_args.args[1]
        self.assertEqual(data["rlPager.currentPage"], "2")
        self.assertEqual(data["ftlcompclass"], "PagerComponent")
        self.assertEqual(data["csrfToken"], "next-token")

    @patch("scraper._safe_form_post")
    def test_classic_bad_pagination_preserves_rows(self, post):
        for result in [ajax(page=1), ajax(total=3), ajax(error="invalid"), "unexpected"]:
            post.return_value = response(result)
            with self.assertRaises(PartialScrapeError) as caught:
                taleo._classic_scrape(BOARD, response(classic()))
            self.assertEqual(len(caught.exception.rows), 1)

    def test_classic_external_form_refused(self):
        with self.assertRaises(ValueError):
            taleo._classic_setup(classic().replace('action="jobsearch.ftl"',
                                                   'action="https://evil.invalid/jobsearch.ftl"'), BOARD)

    @patch("scraper._safe_get")
    def test_redirect_to_account_page_refused(self, get):
        get.return_value = response(classic(), url=BOARD.replace("jobsearch", "mysubmissions"))
        with self.assertRaises(ValueError):
            taleo.scrape_taleo(BOARD)

    @patch("scraper._safe_get")
    def test_probe_validates_classic_page_count(self, get):
        get.return_value = response(classic(total=70))
        self.assertEqual(taleo.probe_taleo(BOARD), 70)

    @patch("scraper._safe_post")
    @patch("scraper._safe_get")
    def test_probe_modern_dynamic_portal(self, get, post):
        get.return_value = response("portalNo:'999'")
        post.return_value = response(data=modern(total=50))
        self.assertEqual(taleo.probe_taleo(BOARD), 50)
        self.assertIn("portal=999", post.call_args.args[0])

    def test_description_preserves_negative_visa_fact(self):
        result = taleo.description_html(detail())
        text = taleo.BeautifulSoup(result, "html.parser").get_text(" ", strip=True)
        self.assertIn("Visa Sponsorship Provided No", text)
        self.assertIn("Analyze university research data", text)
        self.assertNotIn("Unrelated employer navigation", text)
        self.assertNotIn("unwanted", result)
        self.assertNotIn("secret", result)

    def test_missing_description_does_not_return_navigation(self):
        self.assertEqual(taleo.description_html("<body>Job search navigation</body>"), "")

    @patch("core.requests.get")
    def test_core_description_hook(self, get):
        import core
        get.return_value = response(detail())
        result = core.fetch_jd(BOARD.replace("jobsearch", "jobdetail") + "?job=1")
        self.assertIn("Visa Sponsorship Provided No", result)
        self.assertNotIn("Unrelated employer navigation", result)


if __name__ == "__main__":
    unittest.main()
