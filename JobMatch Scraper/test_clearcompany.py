"""Offline completeness/ownership guards for native HRMDirect public catalogs."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scraper import clearcompany as cc
from scraper.infosys import PartialScrapeError

ORIGIN = "https://thermo.hrmdirect.com"
BOARD = ORIGIN + "/employment/index.php"
TITLE = "Associate Project Manager"


def index(total=2, company="Thermo Systems LLC"):
    return '<title>Careers At %s</title><p>There are currently %s openings.</p>' % (company, total)


def row(req=100, loc=200, title=TITLE):
    return ('<tr class="reqitemAlt" data-req-id="%s"><td class="posTitle">'
            '<a href="job-opening.php?req=%s&amp;req_loc=%s&amp;#job">%s</a></td>'
            '<td class="cities">Plano</td><td class="state">TX</td>'
            '<td class="offices">Greenville, TX</td></tr>') % (req, req, loc, title)


def listing(rows, next_link="", company="Thermo Systems LLC"):
    return ('<title>Careers At %s</title><table class="reqResultTable">%s</table>%s'
            % (company, ''.join(rows), next_link))


def response(html):
    return SimpleNamespace(text=html)


def detail(req=100, title=TITLE, jd=None):
    jd = jd or '<p>Coordinate automation projects, schedules, engineering and budgets.</p>' * 8
    return ('<title>%s - Careers At Thermo Systems LLC</title><div class="reqResult"><h2>%s</h2>'
            '<table class="viewFields"><tr><td class="viewFieldName">Location:</td>'
            '<td class="viewFieldValue">Plano, TX, United States</td></tr></table>'
            '<a class="applyOnline" href="https://apply.hrmdirect.com/resumedirect/ApplyOnline/Apply.aspx?req_id=%s">Apply</a>'
            '<div class="jobDesc">%s</div></div>') % (title, title, req, jd)


class ClearCompanyTests(unittest.TestCase):
    def test_native_board_and_posting_paths_canonicalize_without_tracking(self):
        pasted = ORIGIN + '/employment/view.php?req=100&location=abcdef00-1234&jbsrc=1014'
        self.assertEqual(cc.board_url(pasted), BOARD)
        self.assertEqual(cc._posting_url(pasted), ORIGIN + '/employment/view.php?req=100&location=abcdef00-1234')
        for bad in ['https://thermo.hrmdirect.com.evil.test/employment/index.php',
                    'https://user@thermo.hrmdirect.com/employment/index.php',
                    'https://apply.hrmdirect.com/employment/index.php',
                    'https://thermo.hrmdirect.com/employment/../index.php',
                    'https://recruiting.adp.com/employment/index.php']:
            with self.subTest(url=bad), self.assertRaises(ValueError):
                cc.board_url(bad)

    def test_location_postings_are_not_collapsed_by_requisition(self):
        with patch.object(cc, '_get', side_effect=[response(index()), response(listing([row(100, 200), row(100, 201)])), response(index())]):
            jobs = cc.scrape_clearcompany(BOARD)
        self.assertEqual(len(jobs), 2)
        self.assertNotEqual(jobs[0]['url'], jobs[1]['url'])
        self.assertNotIn('found_date', jobs[0])

    def test_explicit_public_next_page_is_read_to_advertised_count(self):
        link = '<a rel="next" href="job-openings.php?search=true&amp;page=2">Next</a>'
        with patch.object(cc, '_get', side_effect=[response(index()), response(listing([row()], link)), response(listing([row(101, 201)])), response(index())]) as get:
            self.assertEqual(len(cc.scrape_clearcompany(BOARD)), 2)
        self.assertIn('page=2', get.call_args_list[2].args[0])

    def test_duplicate_page_never_counts_as_complete(self):
        link = '<a href="job-openings.php?search=true&amp;page=2">2</a>'
        with patch.object(cc, '_get', side_effect=[response(index()), response(listing([row()], link)), response(listing([row()])), response(index())]):
            with self.assertRaises(PartialScrapeError) as caught:
                cc.scrape_clearcompany(BOARD)
        self.assertEqual(len(caught.exception.rows), 1)

    def test_missing_rows_unknown_empty_and_malformed_row_remain_partial(self):
        malformed = row(101, 201).replace('data-req-id="101"', 'data-req-id="102"')
        for total, body in [(2, listing([row()])), (1, listing([])), (2, listing([row(), malformed]))]:
            with self.subTest(body=body), patch.object(cc, '_get', side_effect=[response(index(total)), response(body), response(index(total))]):
                with self.assertRaises(PartialScrapeError) as caught:
                    cc.scrape_clearcompany(BOARD)
                self.assertEqual(len(caught.exception.rows), 0 if not '<tr' in body else 1)
        with patch.object(cc, '_get', return_value=response('<title>Careers At Thermo Systems LLC</title>')):
            with self.assertRaises(PartialScrapeError):
                cc.scrape_clearcompany(BOARD)

    def test_verified_explicit_zero_with_native_table_is_valid_empty(self):
        with patch.object(cc, '_get', side_effect=[response(index(0)), response(listing([])), response(index(0))]):
            self.assertEqual(cc.scrape_clearcompany(BOARD), [])

    def test_advertised_count_drift_preserves_all_rows(self):
        with patch.object(cc, '_get', side_effect=[response(index()), response(listing([row(100, 200), row(101, 201)])), response(index(3))]):
            with self.assertRaises(PartialScrapeError) as caught:
                cc.scrape_clearcompany(BOARD)
        self.assertEqual(len(caught.exception.rows), 2)
        self.assertIn('changed', str(caught.exception))

    def test_other_employer_or_paging_host_is_rejected(self):
        for page in [listing([row()], company='Other Employer'),
                     listing([row()], '<a rel="next" href="https://evil.test/employment/job-openings.php?page=2">Next</a>')]:
            with patch.object(cc, '_get', side_effect=[response(index()), response(page), response(index())]) as get:
                with self.assertRaises(PartialScrapeError) as caught:
                    cc.scrape_clearcompany(BOARD)
            self.assertEqual(get.call_count, 2 if 'Other Employer' in page else 3)
            self.assertEqual(len(caught.exception.rows), 0 if 'Other Employer' in page else 1)

    def test_shared_session_deadline_and_redirect_contract(self):
        reply = SimpleNamespace(status_code=200, raise_for_status=lambda: None)
        with patch('scraper.SESSION.get', return_value=reply) as get:
            self.assertIs(cc._get(BOARD), reply)
        self.assertEqual(get.call_args.kwargs['timeout'], 25)
        self.assertIs(get.call_args.kwargs['allow_redirects'], False)
        self.assertIn('JobMatchScraper', get.call_args.kwargs['headers']['User-Agent'])

    def test_network_failure_preserves_first_completed_page(self):
        link = '<a rel="next" href="job-openings.php?search=true&amp;page=2">Next</a>'
        with patch.object(cc, '_get', side_effect=[response(index()), response(listing([row()], link)), TimeoutError('public page timed out')]):
            with self.assertRaises(PartialScrapeError) as caught:
                cc.scrape_clearcompany(BOARD)
        self.assertEqual(len(caught.exception.rows), 1)

    def test_full_jd_matches_exact_title_and_requisition(self):
        url = ORIGIN + '/employment/job-opening.php?req=100&req_loc=200'
        with patch.object(cc, '_get', return_value=response(detail())):
            job = cc.detail_job(url, TITLE)
        self.assertEqual(job['company'], 'Thermo Systems LLC')
        self.assertEqual(job['location'], 'Plano, TX, United States')
        self.assertNotIn('found_date', job)
        self.assertGreater(len(job['jd']), 400)
        with patch.object(cc, '_get', return_value=response(detail())):
            self.assertEqual(cc.detail_jd(url, TITLE), (job['jd'], ''))
        for html in [detail(req=101), detail(title='Other Job'), detail(jd='Not available')]:
            with patch.object(cc, '_get', return_value=response(html)):
                with self.assertRaises(ValueError):
                    cc.detail_jd(url, TITLE)

    def test_title_entities_and_unicode_are_kept(self):
        title = 'Program &amp; Operations\u0085Coordinator\u2028I'
        with patch.object(cc, '_get', side_effect=[response(index(1)), response(listing([row(title=title)])), response(index(1))]):
            jobs = cc.scrape_clearcompany(BOARD)
        self.assertEqual(jobs[0]['title'], 'Program & Operations Coordinator I')

    def test_legacy_html_encoding_preserves_published_smart_quotes_and_dashes(self):
        html = detail(jd='<p>Employee’s success – our projects.</p>' * 20)
        raw = html.encode('cp1252')
        reply = SimpleNamespace(content=raw, text=raw.decode('latin1'))
        with patch.object(cc, '_get', return_value=reply):
            jd, date = cc.detail_jd(ORIGIN + '/employment/job-opening.php?req=100', TITLE)
        self.assertIn('Employee’s success –', jd)
        self.assertNotIn('\x92', jd)
        self.assertNotIn('\x96', jd)
        self.assertEqual(date, '')

    def test_native_default_division_redirect_is_propagated_to_listing_and_refresh(self):
        scoped = BOARD + '?cust_sort1=277890'
        replies = [SimpleNamespace(status_code=302, headers={'Location': '//thermo.hrmdirect.com/employment/index.php?cust_sort1=277890'}, raise_for_status=lambda: None),
                   SimpleNamespace(status_code=200, text=index(1), url=scoped, raise_for_status=lambda: None),
                   SimpleNamespace(status_code=200, text=listing([row()]), url=ORIGIN + '/employment/job-openings.php?search=true&cust_sort1=277890', raise_for_status=lambda: None),
                   SimpleNamespace(status_code=200, text=index(1), url=scoped, raise_for_status=lambda: None)]
        with patch('scraper.SESSION.get', side_effect=replies) as get:
            self.assertEqual(len(cc.scrape_clearcompany(BOARD)), 1)
        urls = [call.args[0] for call in get.call_args_list]
        self.assertEqual(urls[1], scoped)
        self.assertEqual(urls[2], ORIGIN + '/employment/job-openings.php?search=true&cust_sort1=277890')
        self.assertEqual(urls[3], scoped)
        self.assertEqual(cc.board_url(scoped + '&jbsrc=123'), scoped)

    def test_catalog_redirects_reject_cross_host_unknown_filters_changed_scope_and_loops(self):
        for source, destination in [
                (BOARD, 'https://other.hrmdirect.com/employment/index.php?cust_sort1=277890'),
                (BOARD, '/employment/index.php?department=Engineering'),
                (BOARD, '/employment/index.php?cust_sort1=not-a-number'),
                (BOARD, '/employment/job-openings.php?cust_sort1=277890'),
                (BOARD + '?cust_sort1=277890', '/employment/index.php?cust_sort1=181490'),
                (BOARD, BOARD)]:
            reply = SimpleNamespace(status_code=302, headers={'Location': destination}, raise_for_status=lambda: None)
            with self.subTest(destination=destination), patch('scraper.SESSION.get', return_value=reply) as get:
                with self.assertRaises(ValueError):
                    cc._get(source)
                self.assertEqual(get.call_count, 1)

    def test_scoped_pagination_inherits_selector_and_rejects_another_division(self):
        page = ORIGIN + '/employment/job-openings.php?search=true&cust_sort1=277890'
        from bs4 import BeautifulSoup
        for suffix, expected_error in [('', False), ('&cust_sort1=181490', True)]:
            soup = BeautifulSoup('<a rel="next" href="job-openings.php?search=true&page=2%s">Next</a>' % suffix, 'html.parser')
            if expected_error:
                with self.assertRaises(ValueError):
                    cc._next_pages(soup, page, ORIGIN)
            else:
                self.assertIn('cust_sort1=277890', cc._next_pages(soup, page, ORIGIN)[0])

    def test_explicit_singular_native_card_template_counts_exact_location_once(self):
        head = '<title>Careers At Thermo Systems LLC</title>'
        card = ('<div class="jobListItem"><h4 class="jobListTitle">'
                '<a href="job-opening.php?req=100&req_loc=200#job">%s</a></h4>'
                '<a class="jobListLink" href="job-opening.php?req=100#job">Read More</a></div>') % TITLE
        one = head + '<p>There is currently one opening.</p>'
        with patch.object(cc, '_get', side_effect=[response(one), response(head + card), response(one)]):
            jobs = cc.scrape_clearcompany(BOARD)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['url'], ORIGIN + '/employment/job-opening.php?req=100&req_loc=200')
        self.assertEqual(jobs[0]['location'], '')
        self.assertNotIn('found_date', jobs[0])
        with patch.object(cc, '_get', side_effect=[response(one), response(head + card.replace('req=100&req_loc=200', 'req=unknown')), response(one)]):
            with self.assertRaises(PartialScrapeError):
                cc.scrape_clearcompany(BOARD)

    def test_headquarters_employer_template_is_anchored(self):
        self.assertEqual(cc._index('<title>Headquarters Careers At Servpro Industries, LLC</title><p>There are currently 39 openings.</p>'), ('Servpro Industries, LLC', 39))
        with self.assertRaises(ValueError):
            cc._index('<title>Unverified Careers At Servpro Industries, LLC</title><p>There are currently 39 openings.</p>')

    def test_published_country_is_retained_and_unknown_location_is_not_assigned_us(self):
        country_row = row().replace('<td class="cities">Plano</td>', '<td class="cities">Remote</td>').replace('<td class="state">TX</td>', '<td class="state"></td>').replace('<td class="offices">Greenville, TX</td>', '<td class="country">Spain</td>')
        with patch.object(cc, '_get', side_effect=[response(index(1)), response(listing([country_row])), response(index(1))]):
            self.assertEqual(cc.scrape_clearcompany(BOARD)[0]['location'], 'Remote, Spain')
        html = detail().replace('Plano, TX, United States', 'Remote').replace('</table>', '<tr><td class="viewFieldName">Country:</td><td class="viewFieldValue">Spain</td></tr></table>')
        with patch.object(cc, '_get', return_value=response(html)):
            self.assertEqual(cc.detail_job(ORIGIN + '/employment/job-opening.php?req=100', TITLE)['location'], 'Remote, Spain')

    def test_legacy_document_jd_requires_unique_native_fields_and_exact_application_ids(self):
        html = detail().replace('<div class="reqResult">', '').removesuffix('</div>')
        with patch.object(cc, '_get', return_value=response(html)):
            self.assertGreater(len(cc.detail_job(ORIGIN + '/employment/job-opening.php?req=100', TITLE)['jd']), 400)
        for malformed in [html + '<h2>Another Job</h2>', html + '<div class="jobDesc">Another description</div>',
                          html.replace('req_id=100', 'req_id=101'), html.replace('class="applyOnline"', 'class="unknown"')]:
            with self.subTest(html=malformed), patch.object(cc, '_get', return_value=response(malformed)):
                with self.assertRaises(ValueError):
                    cc.detail_job(ORIGIN + '/employment/job-opening.php?req=100', TITLE)

    def test_explicit_publisher_title_country_is_retained_when_native_cells_are_blank(self):
        title = 'Senior Business Analyst, CRM - Remote, Spain'
        blank_row = row(title=title).replace('Plano', '').replace('TX', '').replace('Greenville, ', '')
        with patch.object(cc, '_get', side_effect=[response(index(1)), response(listing([blank_row])), response(index(1))]):
            job = cc.scrape_clearcompany(BOARD)[0]
        self.assertEqual(job['location'], 'Remote, Spain')
        self.assertEqual(job['location_evidence'], 'native_title_remote_country')
        from scraper import is_us_location
        self.assertFalse(is_us_location(job['location']))
        with patch.object(cc, '_get', return_value=response(detail(title=title).replace('Plano, TX, United States', ''))):
            job = cc.detail_job(ORIGIN + '/employment/job-opening.php?req=100', title)
        self.assertEqual(job['location'], 'Remote, Spain')
        self.assertEqual(job['location_evidence'], 'native_title_remote_country')
        self.assertEqual(cc._title_location('Spain Market Analyst'), '')
        self.assertEqual(cc._title_location('Project Manager, Remote, Customer Success'), '')

    def test_every_application_link_and_blank_duplicate_posting_id_are_rejected(self):
        for query in ['req=100&req=', 'req=100&req_loc=', 'req=100&req_loc=200&req_loc=']:
            with self.subTest(query=query), self.assertRaises(ValueError):
                cc._posting_url(ORIGIN + '/employment/job-opening.php?' + query)
        for invalid_query in ['', '?req_id=100&req_id=', '?req_id=100&req_id=100']:
            extra = '<a class="applyOnline" href="https://apply.hrmdirect.com/resumedirect/ApplyOnline/Apply.aspx%s">Apply</a>' % invalid_query
            # Put the malformed link into the exact native posting block.
            html = detail().replace('<div class="jobDesc">', extra + '<div class="jobDesc">')
            with self.subTest(query=invalid_query), patch.object(cc, '_get', return_value=response(html)):
                with self.assertRaises(ValueError):
                    cc.detail_job(ORIGIN + '/employment/job-opening.php?req=100', TITLE)

    def test_unavailable_placeholders_and_whitespace_padding_are_not_full_descriptions(self):
        padded = ' Please check back later.' * 20
        cases = ['Description unavailable' + padded, 'JOB DESCRIPTION UNAVAILABLE' + padded,
                 'This job is no longer available' + padded,
                 'This position is no longer available' + padded,
                 'Job not found' + padded, 'Position not found' + padded,
                 'Build data reports.', 'Coordinate projects. ' + '\n  ' * 200]
        for text in cases:
            with self.subTest(text=text), patch.object(cc, '_get', return_value=response(detail(jd=text))):
                with self.assertRaises(ValueError):
                    cc.detail_job(ORIGIN + '/employment/job-opening.php?req=100', TITLE)

    def test_jd_section_headings_do_not_make_the_posting_title_ambiguous(self):
        body = '<h2>%s</h2><h2>Responsibilities</h2><p>Coordinate engineering projects and schedules.</p>' % TITLE
        html = detail(jd=body * 8)
        with patch.object(cc, '_get', return_value=response(html)):
            job = cc.detail_job(ORIGIN + '/employment/job-opening.php?req=100', TITLE)
        self.assertEqual(job['title'], TITLE)
        self.assertIn('Responsibilities', job['jd'])
        self.assertIn(TITLE, job['jd'])
        for invalid in [html.replace('<h2>%s</h2>' % TITLE, '', 1),
                        html.replace('<div class="jobDesc">', '<h2>Another Posting</h2><div class="jobDesc">'),
                        html.replace('req_id=100', 'req_id=101')]:
            with self.subTest(html=invalid), patch.object(cc, '_get', return_value=response(invalid)):
                with self.assertRaises(ValueError):
                    cc.detail_job(ORIGIN + '/employment/job-opening.php?req=100', TITLE)


if __name__ == '__main__':
    unittest.main()
