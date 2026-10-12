"""Offline contracts for public SilkRoad tenant, pagination and JD admission."""
import os
os.environ["EV_OFF"] = "1"

import unittest
import re
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import scraper
from scraper import silkroad
from scraper.infosys import PartialScrapeError

BOARD = "https://jobs.silkroad.com/NYULangone/NYULHCareers"


def listing(page=1, pages=1, count=2, first=1, owner="NYU Langone Health", next_url=None):
    panels = "".join(
        '<a class="sr-panel" id="Jobs_PagedJobList_Job-{0}" href="/NYULangone/NYULHCareers/jobs/{0}">'
        '<div id="Jobs_PagedJobList_JobTitle-{0}">Research &amp; Data Analyst {0}</div>'
        '<div id="Jobs_PagedJobList_JobLocation-{0}">New York, NY</div></a>'.format(identifier)
        for identifier in range(first, first + count))
    next_url = next_url or "/NYULangone/NYULHCareers?page=" + str(min(page + 1, pages))
    disabled = ' disabled=""' if page == pages else ""
    return ('<h1 id="Jobs_JobsList_CompanyName">' + owner + '</h1>' + panels
            + '<div id="Jobs_PagedJobList_CurrentPageText">Page %s of %s</div>' % (page, pages)
            + '<button id="Jobs_PagedJobList_NextLink" data-href="' + next_url + '"' + disabled + '></button>')


def detail(identifier=7, title="Research &amp; Data Analyst", owner="NYU Langone Health", description=None):
    description = description if description is not None else ('<p>Build reliable reports &amp; data pipelines.</p><ul><li>Validate patient-research datasets.</li></ul>'
        '<p>Work with researchers and operational teams to define reporting requirements, improve data quality, investigate discrepancies, document repeatable methods, and communicate findings. Candidates should demonstrate experience with SQL, analytical tools, and careful handling of research records.</p>')
    return ('<h1 id="Jobs_JobsList_CompanyName">' + owner + '</h1>'
            + '<h1 id="Jobs_JobDetail_TitleText">' + title + '</h1>'
            + '<div id="Jobs_JobDetail_JobDescriptionText">' + description + '</div>'
            + '<a id="Jobs_JobDetail_Multiform_ApplyLink" href="/NYULangone/NYULHCareers/Apply/MultiForm/%s">Apply</a>' % identifier)


def response(html, url=BOARD, status=200, location=None):
    return Mock(text=html, url=url, status_code=status, headers={"Location": location} if location else {})


def embedded_html(html, owner="NYU Langone Health"):
    metadata = ('<meta property="og:description" content="Find a career with ' + owner + '">'
                '<meta property="og:image:alt" content="Logo for ' + owner + '">')
    html = html.replace('<h1 id="Jobs_JobsList_CompanyName">' + owner + '</h1>', metadata)
    return html


def first_command_xml(identifier=7, title="Research &amp; Data Analyst", description=None):
    description = description if description is not None else 'Build reliable reports and data pipelines. ' * 8
    return ('<jobs><job><jobId>{0}</jobId><applyUrl>https://firstcommand.silkroad.com/epostings/?fuseaction=app.jobInfo&amp;version=3&amp;jobid={0}</applyUrl>'
        '<title>{1}</title><jobDescription><![CDATA[<p>{2}</p>]]></jobDescription>'
        '<requiredSkills><![CDATA[<p>Required Skills: SQL and statistics.</p>]]></requiredSkills><requiredExperience></requiredExperience>'
        '<postingDate>2026-10-12T09:12:29Z</postingDate><jobLocation><municipality>Fort Worth</municipality><region>Texas</region><countryCode>US</countryCode></jobLocation>'
        '</job></jobs>').format(identifier, title, description)


def grouped_listing():
    cards = re.findall(r'<a class="sr-panel".*?</a>', listing(count=3))
    return ('<h1 id="Jobs_JobsList_CompanyName">NYU Langone Health</h1>'
        '<form id="Jobs_JobSearch_SearchForm"><input name="SearchString" value="">'
        '<select id="Jobs_JobSearch_CategoriesSelect"><option></option><option value="10">Research</option><option value="20">Operations</option></select></form>'
        '<section class="sr-wrapper sr-wrapper--large"><article class="sr-search__section"><h2 id="Jobs_PagedJobList_Category__10">Research</h2>' + ''.join(cards[:2]) + '</article>'
        '<article class="sr-search__section"><h2 id="Jobs_PagedJobList_Category__20">Operations</h2>' + cards[2] + '</article>'
        '<div id="Jobs_PagedJobList_OpenSubmission">Submit your resume/CV</div></section>')


class SilkRoadTests(unittest.TestCase):
    def test_canonical_current_public_link_variants(self):
        for value in [BOARD, BOARD + "/", BOARD + "/jobs/7?source=Indeed.com",
                      BOARD + "/Profile", BOARD + "/Apply/MultiForm/7", BOARD + "/Jobs/7"]:
            self.assertEqual(silkroad.board_url(value), BOARD)

    def test_rejects_other_hosts_ports_credentials_fragments_and_private_paths(self):
        for value in [BOARD.replace("https://", "http://"), BOARD.replace("jobs.silkroad.com", "jobs.silkroad.com.evil.test"),
                      BOARD.replace("jobs.silkroad.com", "jobs.silkroad.com:443"),
                      BOARD.replace("jobs.silkroad.com", "user@jobs.silkroad.com"), BOARD + "#jobs",
                      BOARD + "/jobs/0", BOARD + "/jobs/7/extra", BOARD + "/Profile/Manage", BOARD + "/Login",
                      "https://jobs.silkroad.com/one", "https://jobs.silkroad.com/one/%2E%2E"]:
            self.assertIsNone(silkroad.board_url(value), value)

    def test_full_multi_page_unique_catalog(self):
        with patch.object(silkroad, "_get", side_effect=[response(listing(1, 2, 25)), response(listing(2, 2, 3, 26))]) as get, patch.object(silkroad.time, "sleep"):
            rows = silkroad.scrape_silkroad(BOARD)
        self.assertEqual(len(rows), 28)
        self.assertEqual(len({row["url"] for row in rows}), 28)
        self.assertEqual([call.args[0] for call in get.call_args_list], [BOARD, BOARD + "?page=2"])
        self.assertEqual(rows[0]["title"], "Research & Data Analyst 1")
        self.assertEqual(rows[0]["identity_text"], "NYU Langone Health")
        self.assertNotIn("found_date", rows[0])

    def test_other_tenant_uses_current_structured_owner(self):
        html = listing(owner="Precise Systems").replace("NYULangone/NYULHCareers", "Precise/ProfessionalCareers")
        board = "https://jobs.silkroad.com/Precise/ProfessionalCareers"
        with patch.object(silkroad, "_get", return_value=response(html, board)):
            self.assertEqual(silkroad.identity_text(board), "Precise Systems")
            self.assertEqual(silkroad.scrape_silkroad(board)[0]["identity_text"], "Precise Systems")

    def test_explicit_empty_is_complete(self):
        html = '<h1 id="Jobs_JobsList_CompanyName">NYU Langone Health</h1><h2 id="Jobs_JobSearchResults_NoResults_PageHeading">Can\'t find results that match</h2><p id="Jobs_JobSearchResults_NoResults_PageText">You can still submit your resume/CV.</p>'
        with patch.object(silkroad, "_get", return_value=response(html)):
            self.assertEqual(silkroad.scrape_silkroad(BOARD), [])

    def test_incomplete_empty_markup_is_not_a_complete_catalog(self):
        html = '<h1 id="Jobs_JobsList_CompanyName">NYU Langone Health</h1><h2 id="Jobs_JobSearchResults_NoResults_PageHeading">Can\'t find results that match</h2>'
        with patch.object(silkroad, "_get", return_value=response(html)):
            with self.assertRaises(ValueError):
                silkroad.scrape_silkroad(BOARD)

    def test_loading_shell_is_not_an_empty_catalog(self):
        with patch.object(silkroad, "_get", return_value=response('<h1 id="Jobs_JobsList_CompanyName">NYU Langone Health</h1>Loading...')):
            with self.assertRaises(ValueError):
                silkroad.scrape_silkroad(BOARD)

    def test_duplicate_batch_preserves_prior_public_rows(self):
        with patch.object(silkroad, "_get", side_effect=[response(listing(1, 2, 25)), response(listing(2, 2, 2, 25))]), patch.object(silkroad.time, "sleep"):
            with self.assertRaises(PartialScrapeError) as caught:
                silkroad.scrape_silkroad(BOARD)
        self.assertEqual(len(caught.exception.rows), 25)

    def test_duplicate_ids_within_page_are_rejected(self):
        html = listing(count=2).replace("Job-2", "Job-1").replace("jobs/2", "jobs/1").replace("JobTitle-2", "JobTitle-1").replace("JobLocation-2", "JobLocation-1")
        with patch.object(silkroad, "_get", return_value=response(html)):
            with self.assertRaises(ValueError):
                silkroad.scrape_silkroad(BOARD)

    def test_total_pages_changing_preserves_partial(self):
        with patch.object(silkroad, "_get", side_effect=[response(listing(1, 2, 25)), response(listing(2, 3, 25, 26))]), patch.object(silkroad.time, "sleep"):
            with self.assertRaises(PartialScrapeError) as caught:
                silkroad.scrape_silkroad(BOARD)
        self.assertEqual(len(caught.exception.rows), 25)

    def test_employer_changing_preserves_partial(self):
        with patch.object(silkroad, "_get", side_effect=[response(listing(1, 2, 25)), response(listing(2, 2, 1, 26, owner="Other Company"))]), patch.object(silkroad.time, "sleep"):
            with self.assertRaises(PartialScrapeError) as caught:
                silkroad.scrape_silkroad(BOARD)
        self.assertEqual(len(caught.exception.rows), 25)

    def test_repeated_page_number_is_not_complete(self):
        with patch.object(silkroad, "_get", side_effect=[response(listing(1, 2, 25)), response(listing(1, 2, 25))]), patch.object(silkroad.time, "sleep"):
            with self.assertRaises(PartialScrapeError):
                silkroad.scrape_silkroad(BOARD)

    def test_short_middle_page_rejected(self):
        with patch.object(silkroad, "_get", side_effect=[response(listing(1, 3, 25)), response(listing(2, 3, 24, 26))]), patch.object(silkroad.time, "sleep"):
            with self.assertRaises(PartialScrapeError) as caught:
                silkroad.scrape_silkroad(BOARD)
        self.assertEqual(len(caught.exception.rows), 25)

    def test_tenant_ten_per_page_catalog(self):
        with patch.object(silkroad, "_get", side_effect=[response(listing(1, 3, 10)), response(listing(2, 3, 10, 11)), response(listing(3, 3, 4, 21))]), patch.object(silkroad.time, "sleep"):
            self.assertEqual(len(silkroad.scrape_silkroad(BOARD)), 24)

    def test_last_page_cannot_be_larger_than_first(self):
        with patch.object(silkroad, "_get", side_effect=[response(listing(1, 2, 10)), response(listing(2, 2, 11, 11))]), patch.object(silkroad.time, "sleep"):
            with self.assertRaises(PartialScrapeError) as caught:
                silkroad.scrape_silkroad(BOARD)
        self.assertEqual(len(caught.exception.rows), 10)

    def test_rendered_card_missing_id_is_not_silently_omitted(self):
        html = listing().replace('id="Jobs_PagedJobList_Job-1"', 'id="unrelated-card"')
        with patch.object(silkroad, "_get", return_value=response(html)):
            with self.assertRaises(ValueError):
                silkroad.scrape_silkroad(BOARD)

    def test_page_size_cap_is_bounded(self):
        with patch.object(silkroad, "MAX_PAGE_SIZE", 2), patch.object(silkroad, "_get", return_value=response(listing(count=3))):
            with self.assertRaises(ValueError):
                silkroad.scrape_silkroad(BOARD)

    def test_foreign_and_skipped_next_page_links_rejected(self):
        for next_url in ["https://example.test/NYULangone/NYULHCareers?page=2", "/NYULangone/OtherPortal?page=2", "/NYULangone/NYULHCareers?page=3"]:
            with patch.object(silkroad, "_get", return_value=response(listing(1, 2, 25, next_url=next_url))):
                with self.assertRaises(ValueError):
                    silkroad.scrape_silkroad(BOARD)

    def test_final_page_must_disable_next(self):
        with patch.object(silkroad, "_get", return_value=response(listing().replace(' disabled=""', ""))):
            with self.assertRaises(ValueError):
                silkroad.scrape_silkroad(BOARD)

    def test_foreign_or_mismatched_posting_href_rejected(self):
        for wrong in ["https://example.test/jobs/1", "/NYULangone/OtherPortal/jobs/1", "/NYULangone/NYULHCareers/jobs/99"]:
            html = listing().replace("/NYULangone/NYULHCareers/jobs/1", wrong)
            with patch.object(silkroad, "_get", return_value=response(html)):
                with self.assertRaises(ValueError):
                    silkroad.scrape_silkroad(BOARD)

    def test_missing_location_rejected(self):
        html = listing().replace("Jobs_PagedJobList_JobLocation-1", "unrelated-location")
        with patch.object(silkroad, "_get", return_value=response(html)):
            with self.assertRaises(ValueError):
                silkroad.scrape_silkroad(BOARD)

    def test_bounded_cap_preserves_rows(self):
        with patch.object(silkroad, "MAX_PAGES", 1), patch.object(silkroad, "_get", return_value=response(listing(1, 2, 25))):
            with self.assertRaises(PartialScrapeError) as caught:
                silkroad.scrape_silkroad(BOARD)
        self.assertEqual(len(caught.exception.rows), 25)

    def test_later_http_failure_preserves_rows(self):
        with patch.object(silkroad, "_get", side_effect=[response(listing(1, 2, 25)), ValueError("HTTP 403")]), patch.object(silkroad.time, "sleep"):
            with self.assertRaises(PartialScrapeError) as caught:
                silkroad.scrape_silkroad(BOARD)
        self.assertEqual(len(caught.exception.rows), 25)

    def test_get_disallows_redirect_and_checks_http_scope(self):
        with patch.object(scraper.SESSION, "get", return_value=response("", BOARD, 403)) as get:
            with self.assertRaises(ValueError):
                silkroad._get(BOARD)
            self.assertFalse(get.call_args.kwargs["allow_redirects"])
        for wrong in [BOARD.replace("NYULHCareers", "OtherPortal"), BOARD + "?page=3", "https://example.test/"]:
            with patch.object(scraper.SESSION, "get", return_value=response("", wrong)):
                with self.assertRaises(ValueError):
                    silkroad._get(BOARD)

    def test_published_embedded_route_fetches_only_exact_public_tenant(self):
        location = "https://corp.example/careers/?cxembeddedroot=%2FNYULangone%2FNYULHCareers%3Fembedded%3Dtrue#jobs"
        with patch.object(scraper.SESSION, "get", side_effect=[response("", BOARD, 302, location), response(listing(), BOARD + "?embedded=true")]) as get:
            self.assertEqual(silkroad._get(BOARD).url, BOARD + "?embedded=true")
        self.assertEqual([call.args[0] for call in get.call_args_list], [BOARD, BOARD + "?embedded=true"])
        self.assertTrue(all(call.kwargs["allow_redirects"] is False for call in get.call_args_list))

    def test_unpublished_foreign_private_or_changed_embedded_redirect_rejected(self):
        roots = [None, "/NYULangone/OtherPortal?embedded=true", "/NYULangone/NYULHCareers/Apply/MultiForm/7?embedded=true",
                 "https://evil.test/NYULangone/NYULHCareers?embedded=true", "/NYULangone/NYULHCareers?embedded=false",
                 "/NYULangone/NYULHCareers?embedded=true&page=3", "/NYULangone/NYULHCareers?embedded=true&embedded=true"]
        from urllib.parse import quote
        for root in roots:
            location = "https://corp.example/careers/" + ("?cxembeddedroot=" + quote(root, safe="") if root else "")
            with patch.object(scraper.SESSION, "get", return_value=response("", BOARD, 302, location)) as get:
                with self.assertRaises(ValueError):
                    silkroad._get(BOARD)
            self.assertEqual(get.call_count, 1)

    def test_embedded_http_failure_or_second_redirect_stops(self):
        location = "https://corp.example/?cxembeddedroot=%2FNYULangone%2FNYULHCareers%3Fembedded%3Dtrue"
        for status in [302, 403]:
            with patch.object(scraper.SESSION, "get", side_effect=[response("", BOARD, 302, location), response("", BOARD + "?embedded=true", status, location)]) as get:
                with self.assertRaises(ValueError):
                    silkroad._get(BOARD)
            self.assertEqual(get.call_count, 2)

    def test_get_private_path_never_fetches(self):
        with patch.object(scraper.SESSION, "get") as get:
            for value in [BOARD + "/Profile", BOARD + "/Apply/MultiForm/7", BOARD + "?SelectedCategory=7"]:
                with self.assertRaises(ValueError):
                    silkroad._get(value)
            get.assert_not_called()

    def test_embedded_paging_preserves_flag_and_explicit_owner(self):
        first = embedded_html(listing(1, 2, 10)).replace('data-href="/NYULangone/NYULHCareers?page=2"', 'data-href="/NYULangone/NYULHCareers?embedded=true&amp;page=2"')
        second = embedded_html(listing(2, 2, 2, 11))
        for identifier in range(1, 13):
            first = first.replace('jobs/%s"' % identifier, 'jobs/%s?embedded=true"' % identifier)
            second = second.replace('jobs/%s"' % identifier, 'jobs/%s?embedded=true"' % identifier)
        with patch.object(silkroad, "_get", side_effect=[response(first, BOARD + "?embedded=true"), response(second, BOARD + "?embedded=true&page=2")]) as get, patch.object(silkroad.time, "sleep"):
            rows = silkroad.scrape_silkroad(BOARD)
        self.assertEqual(len(rows), 12)
        self.assertEqual(rows[0]["identity_text"], "NYU Langone Health")
        self.assertEqual(rows[0]["url"], BOARD + "/jobs/1?embedded=true")
        self.assertEqual(get.call_args_list[1].args[0], BOARD + "?embedded=true&page=2")

    def test_embedded_owner_requires_both_matching_structured_fields(self):
        html = embedded_html(listing())
        for wrong in [html.replace("Logo for NYU Langone Health", "Logo for New York University"),
                      html.replace('property="og:image:alt"', 'property="unrelated"'),
                      html.replace("Find a career with NYU Langone Health", "Search job openings")]:
            with patch.object(silkroad, "_get", return_value=response(wrong)):
                with self.assertRaises(ValueError):
                    silkroad.scrape_silkroad(BOARD)

    def test_full_description_decoded_and_chrome_excluded(self):
        html = "<nav>Account/login chrome</nav>" + detail()
        with patch.object(silkroad, "_get", return_value=response(html, BOARD + "/jobs/7")):
            jd, date = silkroad.detail_jd(BOARD + "/jobs/7?source=Indeed.com", "Research & Data Analyst", "NYU Langone Health")
        self.assertTrue(jd.startswith("Build reliable reports & data pipelines. Validate patient-research datasets."))
        self.assertGreater(len(jd), 250)
        self.assertNotIn("Account/login chrome", jd)
        self.assertEqual(date, "")

    def test_wrong_id_title_owner_or_missing_description_remains_retryable(self):
        cases = [(detail(identifier=8), None, None), (detail(), "Other Role", None),
                 (detail(), None, "New York University"), (detail(description=""), None, None)]
        for html, title, owner in cases:
            with patch.object(silkroad, "_get", return_value=response(html, BOARD + "/jobs/7")):
                self.assertEqual(silkroad.detail_jd(BOARD + "/jobs/7", title, owner), ("", ""))

    def test_nonposting_detail_does_not_fetch_accounts_or_apply(self):
        with patch.object(silkroad, "_get") as get:
            for url in [BOARD, BOARD + "/Profile", BOARD + "/Apply/MultiForm/7", "https://example.test/jobs/7"]:
                self.assertEqual(silkroad.detail_jd(url), ("", ""))
            get.assert_not_called()

    def test_embedded_configurable_description_and_discrete_required_skills(self):
        html = embedded_html(detail()).replace("Jobs_JobDetail_JobDescriptionText", "ConfigurablePageDetail__JobDescription")
        html = html.replace('MultiForm/7"', 'MultiForm/7?embedded=true"') + '<div id="Jobs_JobDetail_JobRequiredSkillsText">Required Skills: SQL and statistics.</div>'
        with patch.object(silkroad, "_get", return_value=response(html, BOARD + "/jobs/7?embedded=true")) as get:
            jd, date = silkroad.detail_jd(BOARD + "/jobs/7?embedded=true", "Research & Data Analyst", "NYU Langone Health")
        self.assertIn("Validate patient-research datasets.", jd)
        self.assertIn("Required Skills: SQL and statistics.", jd)
        self.assertEqual(date, "")
        self.assertEqual(get.call_args.args[0], BOARD + "/jobs/7?embedded=true")

    def test_corporate_uppercase_posting_path_is_preserved(self):
        with patch.object(silkroad, "_get", return_value=response(detail(), BOARD + "/Jobs/7")) as get:
            self.assertTrue(silkroad.detail_jd(BOARD + "/Jobs/7")[0])
        self.assertEqual(get.call_args.args[0], BOARD + "/Jobs/7")

    def test_short_or_long_placeholder_description_remains_retryable(self):
        for stub in ["Description unavailable", "Build data reports.", "Description unavailable " + "Please check back later. " * 20]:
            with patch.object(silkroad, "_get", return_value=response(detail(description=stub), BOARD + "/jobs/7")):
                self.assertEqual(silkroad.detail_jd(BOARD + "/jobs/7"), ("", ""))

    def test_first_command_full_xml_catalog_and_jd_date(self):
        first = first_command_xml()
        xml = first.replace('</jobs>', '') + first_command_xml(8).removeprefix('<jobs>')
        rows = silkroad._parse_first_command_xml(xml)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['location'], 'Fort Worth, Texas, US')
        self.assertEqual(rows[0]['identity_text'], 'First Command')
        self.assertEqual(rows[0]['found_date'], '2026-10-12')
        self.assertIn('Required Skills: SQL and statistics.', rows[0]['jd'])
        with patch.object(silkroad, '_first_command_catalog', return_value=rows):
            self.assertEqual(silkroad.scrape_silkroad(silkroad.FIRST_COMMAND_BOARD), rows)
            self.assertEqual(silkroad.identity_text(silkroad.FIRST_COMMAND_BOARD), 'First Command')
            self.assertEqual(silkroad.detail_jd(rows[0]['url'], 'Research & Data Analyst', 'First Command'), (rows[0]['jd'], '2026-10-12'))
            self.assertEqual(silkroad.detail_jd(rows[0]['url'], 'Other role'), ('', ''))
            self.assertEqual(silkroad.detail_jd(rows[0]['url'], expected_company='Other employer'), ('', ''))

    def test_first_command_exact_current_redirect_and_published_endpoint_only(self):
        board, feed = silkroad.FIRST_COMMAND_BOARD, silkroad.FIRST_COMMAND_FEED
        with patch.object(scraper.SESSION, 'get', side_effect=[response('', board, 302, silkroad.FIRST_COMMAND_CAREERS), response(first_command_xml(), feed)]) as get:
            self.assertEqual(len(silkroad._first_command_catalog()), 1)
        self.assertEqual([call.args[0] for call in get.call_args_list], [board, feed])
        self.assertTrue(all(call.kwargs['allow_redirects'] is False for call in get.call_args_list))
        for location in ['https://evil.test/careers/', silkroad.FIRST_COMMAND_CAREERS + '?new=1']:
            with patch.object(scraper.SESSION, 'get', return_value=response('', board, 302, location)) as get:
                with self.assertRaises(ValueError):
                    silkroad._first_command_catalog()
            self.assertEqual(get.call_count, 1)

    def test_first_command_feed_http_or_redirect_scope_failure(self):
        board, feed = silkroad.FIRST_COMMAND_BOARD, silkroad.FIRST_COMMAND_FEED
        for bad in [response('', feed, 403), response('', feed, 302), response(first_command_xml(), 'https://evil.test/feed.xml')]:
            with patch.object(scraper.SESSION, 'get', side_effect=[response('', board, 302, silkroad.FIRST_COMMAND_CAREERS), bad]):
                with self.assertRaises(ValueError):
                    silkroad._first_command_catalog()

    def test_first_command_xml_rejects_partial_schema_duplicates_scope_and_stubs(self):
        xml = first_command_xml()
        cases = [xml[:-5], '<html>Loading</html>', '<jobs>Loading</jobs>', xml.replace('</jobs>', '<next>page2</next></jobs>'),
                 xml.replace('firstcommand.silkroad.com', 'other.silkroad.com'), xml.replace('jobid=7', 'jobid=8'),
                 xml.replace('app.jobInfo', 'app.apply'), xml.replace('<title>', '<otherTitle>'),
                 xml.replace('<jobId>7</jobId>', '<jobId>7</jobId><jobId>7</jobId>'),
                 xml.replace('<countryCode>US</countryCode>', ''), xml.replace('2026-10-12T09:12:29Z', 'bad-date'),
                 first_command_xml(description='Description unavailable'),
                 xml.replace('</jobs>', '') + xml.removeprefix('<jobs>')]
        for bad in cases:
            with self.assertRaises((ValueError, silkroad.ET.ParseError)):
                silkroad._parse_first_command_xml(bad)

    def test_first_command_public_info_links_canonicalize_and_apply_does_not(self):
        url = 'https://firstcommand.silkroad.com/epostings/?fuseaction=app.jobInfo&version=3&jobid=7'
        self.assertEqual(silkroad.board_url(url), silkroad.FIRST_COMMAND_BOARD)
        self.assertEqual(silkroad.board_url(silkroad.FIRST_COMMAND_FEED), silkroad.FIRST_COMMAND_BOARD)
        self.assertIsNone(silkroad.board_url(url.replace('app.jobInfo', 'app.apply')))
        with patch.object(scraper.SESSION, 'get') as get:
            self.assertEqual(silkroad.detail_jd(url.replace('app.jobInfo', 'app.apply')), ('', ''))
            get.assert_not_called()

    def test_first_command_modern_portal_200_uses_normal_template(self):
        board = silkroad.FIRST_COMMAND_BOARD
        html = listing(owner='First Command').replace('NYULangone/NYULHCareers', 'FirstCommand/Careers')
        with patch.object(scraper.SESSION, 'get', return_value=response(html, board)) as get:
            self.assertEqual(len(silkroad.scrape_silkroad(board)), 2)
        self.assertEqual(get.call_count, 1)

    def test_first_command_rejects_blank_duplicate_or_extra_query_fields(self):
        url = 'https://firstcommand.silkroad.com/epostings/?fuseaction=app.jobInfo&version=3&jobid=7'
        for query in ['&jobid=', '&fuseaction=', '&version=', '&filter=']:
            self.assertIsNone(silkroad.board_url(url + query))
            with patch.object(scraper.SESSION, 'get') as get:
                self.assertEqual(silkroad.detail_jd(url + query), ('', ''))
                get.assert_not_called()
            xml = first_command_xml().replace('jobid=7</applyUrl>', 'jobid=7' + query.replace('&', '&amp;') + '</applyUrl>')
            with self.assertRaises(ValueError):
                silkroad._parse_first_command_xml(xml)

    def test_first_command_canonical_url_and_query_order_retain_exact_posting(self):
        rows = silkroad._parse_first_command_xml(first_command_xml())
        canonical = scraper.canonical_url(rows[0]['url'])
        self.assertEqual(urlsplit(canonical).path, '/epostings')
        ordered = 'https://firstcommand.silkroad.com/epostings?jobid=7&version=3&fuseaction=app.jobInfo'
        with patch.object(silkroad, '_first_command_catalog', return_value=rows):
            for url in [rows[0]['url'], canonical, ordered, ordered.replace('/epostings?', '/epostings/?')]:
                self.assertEqual(silkroad.board_url(url), silkroad.FIRST_COMMAND_BOARD)
                self.assertEqual(silkroad.detail_jd(url, 'Research & Data Analyst', 'First Command'), (rows[0]['jd'], rows[0]['found_date']))
                self.assertEqual(silkroad.detail_jd(url, 'Other role'), ('', ''))
                self.assertEqual(silkroad.detail_jd(url, expected_company='Other employer'), ('', ''))
            self.assertEqual(silkroad.detail_jd(ordered.replace('jobid=7', 'jobid=999999')), ('', ''))

    def test_first_command_canonical_path_still_rejects_duplicate_or_private_queries(self):
        url = 'https://firstcommand.silkroad.com/epostings?jobid=7&version=3&fuseaction=app.jobInfo'
        invalid = [url + '&jobid=8', url + '&jobid=', url + '&version=', url + '&fuseaction=',
                   url.replace('/epostings?', '/epostings//?'), url.replace('app.jobInfo', 'app.apply'),
                   url.replace('version=3', 'version=4'), url.replace('firstcommand.silkroad.com', 'other.silkroad.com')]
        with patch.object(silkroad, '_first_command_catalog') as catalog:
            for value in invalid:
                self.assertIsNone(silkroad.board_url(value))
                self.assertEqual(silkroad.detail_jd(value), ('', ''))
            catalog.assert_not_called()

    def test_complete_unfiltered_grouped_catalog_covers_all_categories(self):
        with patch.object(silkroad, '_get', return_value=response(grouped_listing())) as get:
            rows = silkroad.scrape_silkroad(BOARD)
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({row['url'] for row in rows}), 3)
        self.assertEqual(get.call_count, 1)

    def test_grouped_catalog_missing_unknown_category_filter_or_end_is_incomplete(self):
        html = grouped_listing()
        cases = [html.replace('<option value="20">Operations</option>', ''),
                 html.replace('<h2 id="Jobs_PagedJobList_Category__20">Operations</h2>', ''),
                 html.replace('Category__20', 'Category__99'),
                 html.replace('Category__20">Operations', 'Category__20">Other'),
                 html.replace('Jobs_PagedJobList_OpenSubmission', 'missing-end'),
                 html.replace('name="SearchString" value=""', 'name="SearchString" value="Finance"'),
                 html.replace('value="20">Operations</option>', 'value="20" selected>Operations</option>'),
                 html + '<button id="Jobs_PagedJobList_NextLink">Next</button>']
        for bad in cases:
            with patch.object(silkroad, '_get', return_value=response(bad)):
                with self.assertRaises(ValueError):
                    silkroad.scrape_silkroad(BOARD)

    def test_grouped_catalog_outside_card_or_wrong_page_is_not_complete(self):
        html = grouped_listing()
        card = re.findall(r'<a class="sr-panel".*?</a>', html)[0]
        outside = html.replace(card, '') + card
        for bad, page in [(outside, 1), (html, 2)]:
            with self.assertRaises(ValueError):
                silkroad._parse_page(bad, BOARD, page)

    def test_grouped_catalog_empty_or_missing_cards_is_not_verified_empty(self):
        html = grouped_listing()
        for card in re.findall(r'<a class="sr-panel".*?</a>', html):
            html = html.replace(card, '')
        with patch.object(silkroad, '_get', return_value=response(html)):
            with self.assertRaises(ValueError):
                silkroad.scrape_silkroad(BOARD)

    def test_ogp_image_owner_requires_exact_matching_public_description(self):
        html = embedded_html(grouped_listing()).replace('Logo for ', 'OGP Image for ')
        with patch.object(silkroad, '_get', return_value=response(html)):
            self.assertEqual(silkroad.identity_text(BOARD), 'NYU Langone Health')
        wrong = html.replace('OGP Image for NYU Langone Health', 'OGP Image for New York University')
        with patch.object(silkroad, '_get', return_value=response(wrong)):
            with self.assertRaises(ValueError):
                silkroad.identity_text(BOARD)

    def test_blank_list_location_enriched_only_by_exact_full_posting(self):
        html = listing(count=1).replace('New York, NY', '')
        posting = detail(identifier=1, title='Research &amp; Data Analyst 1').replace('Jobs_JobDetail_JobDescriptionText', 'ConfigurablePageDetail__JobDescription')
        posting += ('<div id="ConfigurablePageDetail__DisplayLocation"><h2>Job Location</h2><div>United States</div></div>'
                    '<div id="ConfigurablePageDetail__RequiredSkills">Required Skills: SQL and statistics.</div>'
                    '<div id="ConfigurablePageDetail__RequiredExperience">Required Experience: research operations.</div>')
        with patch.object(silkroad, '_get', side_effect=[response(html), response(posting, BOARD + '/jobs/1')]) as get:
            rows = silkroad.scrape_silkroad(BOARD)
        self.assertEqual(rows[0]['location'], 'United States')
        self.assertIn('Required Skills: SQL and statistics.', rows[0]['jd'])
        self.assertIn('Required Experience: research operations.', rows[0]['jd'])
        self.assertEqual([call.args[0] for call in get.call_args_list], [BOARD, BOARD + '/jobs/1'])

    def test_blank_location_wrong_id_title_owner_stub_or_label_remains_failed(self):
        html = listing(count=1).replace('New York, NY', '')
        posting = detail(identifier=1, title='Research &amp; Data Analyst 1') + '<div id="ConfigurablePageDetail__DisplayLocation"><h2>Job Location</h2><div>United States</div></div>'
        bad_cases = [posting.replace('MultiForm/1"', 'MultiForm/99"'),
                     posting.replace('Research &amp; Data Analyst 1', 'Other role'),
                     posting.replace('NYU Langone Health', 'Other employer'),
                     posting.replace('<h2>Job Location</h2>', '<h2>Head Office</h2>'),
                     detail(identifier=1, title='Research &amp; Data Analyst 1', description='Description unavailable')]
        for bad in bad_cases:
            with patch.object(silkroad, '_get', side_effect=[response(html), response(bad, BOARD + '/jobs/1')]):
                with self.assertRaises(ValueError):
                    silkroad.scrape_silkroad(BOARD)

    def test_grouped_end_is_unique_after_sections_in_same_catalog_container(self):
        html = grouped_listing()
        sentinel = '<div id="Jobs_PagedJobList_OpenSubmission">Submit your resume/CV</div>'
        without_end = html.replace(sentinel, '')
        cases = [html + sentinel,
                 without_end.replace('<article class="sr-search__section">', sentinel + '<article class="sr-search__section">', 1),
                 without_end.replace('<h2 id="Jobs_PagedJobList_Category__10">', sentinel + '<h2 id="Jobs_PagedJobList_Category__10">', 1),
                 without_end + sentinel,
                 html.replace(sentinel, sentinel + '<div>Unknown incomplete content</div>')]
        for bad in cases:
            with patch.object(silkroad, '_get', return_value=response(bad)):
                with self.assertRaises(ValueError):
                    silkroad.scrape_silkroad(BOARD)


if __name__ == "__main__":
    unittest.main(verbosity=2)
