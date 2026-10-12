"""Offline discovery, identity and intake contracts for the new native families."""
import unittest
from unittest.mock import patch
import scraper
from scraper import clearcompany, silkroad, score_jobs, cornerstone
from scraper.infosys import PartialScrapeError

CC='https://thermo.hrmdirect.com/employment/index.php'
SR='https://jobs.silkroad.com/NYULangone/NYULHCareers'
JD='Coordinate engineering projects, research, data quality and budgets. ' * 12

class BacklogIntegrations(unittest.TestCase):
    def test_pasted_postings_detect_exact_native_catalog(self):
        self.assertEqual(scraper.detect_board(CC.replace('index.php','view.php')+'?req=123&location=abcdef00-1234'),(CC,'clearcompany',''))
        self.assertEqual(scraper.detect_board(SR+'/jobs/123081'),(SR,'silkroad',''))
        for url in ['https://thermo.hrmdirect.com.evil.test/employment/view.php?req=123',
                    'https://jobs.silkroad.com.evil.test/NYULangone/NYULHCareers',
                    'https://evil.test@thermo.hrmdirect.com/employment/index.php',
                    'https://jobs.silkroad.com/NYULangone/NYULHCareers/Profile/evil']:
            self.assertIsNone(scraper.detect_board(url),url)

    def test_location_posting_identity_survives_intake_canonicalization(self):
        first=CC.replace('index.php','job-opening.php')+'?req=123&req_loc=456'
        other=CC.replace('index.php','job-opening.php')+'?req=123&req_loc=789'
        self.assertNotEqual(scraper.canonical_url(first),scraper.canonical_url(other))
        self.assertEqual(scraper.canonical_url(first),first)

    def test_official_first_command_posting_uses_gated_native_reader(self):
        url='https://firstcommand.silkroad.com/epostings/?fuseaction=app.jobInfo&version=3&jobid=3328'
        self.assertEqual(scraper.detect_board(url),(silkroad.FIRST_COMMAND_BOARD,'silkroad',''))
        self.assertEqual(scraper.detect_board(silkroad.FIRST_COMMAND_FEED),(silkroad.FIRST_COMMAND_BOARD,'silkroad',''))
        with patch.object(silkroad,'detail_jd',return_value=(JD,'2026-10-01')) as native:
            self.assertEqual(score_jobs.detail_jd(url),(url,JD,'2026-10-01'))
            native.assert_called_once_with(url)
        for invalid in [url.replace('firstcommand.silkroad.com','other.silkroad.com'),
                        url.replace('app.jobInfo','app.apply'),
                        silkroad.FIRST_COMMAND_FEED+'?redirect=other']:
            self.assertIsNone(scraper.detect_board(invalid))

    def test_native_employer_metadata_reaches_identity_gate(self):
        with patch.object(clearcompany,'board_name',return_value='Thermo Systems LLC'):
            self.assertEqual(scraper.board_display_name(CC,'clearcompany'),'Thermo Systems LLC')
        with patch.object(silkroad,'identity_text',return_value='NYU Langone Health'):
            self.assertEqual(scraper.board_display_name(SR,'silkroad'),'NYU Langone Health')

    def test_failed_or_partial_catalog_is_not_an_empty_probe(self):
        for ats,board in [('clearcompany',CC),('silkroad',SR)]:
            def broken(_url):
                raise PartialScrapeError('page interrupted',[{'url':board+'/jobs/1','title':'Analyst'}])
            with patch.dict(scraper.SCRAPERS,{ats:broken}):
                self.assertIsNone(scraper.probe_board(board,ats))
            with patch.dict(scraper.SCRAPERS,{ats:lambda _url:[]}):
                self.assertEqual(scraper.probe_board(board,ats),0)

    def test_native_full_descriptions_reach_regular_intake_queue(self):
        for module,url in [(clearcompany,CC.replace('index.php','job-opening.php')+'?req=123&req_loc=456'),
                           (silkroad,SR+'/jobs/123081')]:
            row={'url':url,'title':'Project Manager','location':'New York, NY'}
            with patch.object(module,'detail_jd',return_value=(JD,'')), patch.object(clearcompany,'detail_job',return_value={'jd':JD,'location':'New York, NY'}):
                tried,got,_=scraper._fetch_jd_queue([row],10,'offline',score_jobs)
            self.assertEqual((tried,got),(1,1))
            self.assertEqual(row['jd'],JD)

    def test_native_location_enrichment_precedes_country_gate(self):
        row={'url':CC.replace('index.php','job-opening.php')+'?req=123&req_loc=456','title':'Project Manager','location':''}
        with patch.object(clearcompany,'detail_job',return_value={'jd':JD,'location':'Madrid, Spain'}):
            scraper._fetch_jd_queue([row],10,'offline',score_jobs)
        self.assertEqual(row['location'],'Madrid, Spain')
        self.assertFalse(scraper.is_us_location(row['location']))

    def test_failed_native_description_stays_retryable(self):
        url=SR+'/jobs/123081'
        with patch.object(silkroad,'detail_jd',side_effect=ValueError('posting mismatch')):
            self.assertEqual(score_jobs.detail_jd(url),(url,'',''))

    def test_oracle_government_cloud_uses_existing_full_detail_reader(self):
        url='https://iaaxmg.fa.ocs.oraclegovcloud.com/hcmUI/CandidateExperience/en/sites/CX_2001/job/11483'
        with patch.object(score_jobs,'oracle_detail_jd',return_value=JD) as native, patch.object(score_jobs.core,'fetch_jd',return_value=''):
            self.assertEqual(score_jobs.detail_jd(url)[1],JD)
            native.assert_called_once_with(url)

    def test_cornerstone_numeric_success_requires_exact_posting_reference(self):
        from unittest.mock import Mock
        board='https://barge.csod.com/ux/ats/careersite/4/home'
        data={'cultureID':1,'token':'offline-anonymous-context'}
        result={'status':0,'data':{'displayTitle':'Project Manager','ref':'req1731','externalDescription':'<p>'+JD+'</p>'}}
        response=Mock(status_code=200)
        response.json.return_value=result
        with patch.object(cornerstone,'context',return_value=(board,data,'https://offline.api.csod.com')), patch.object(scraper.SESSION,'get',return_value=response):
            self.assertEqual(cornerstone.detail_jd(board+'/requisition/1731'),(JD.strip(),''))
            for changed in [dict(result,status=False),dict(result,data=dict(result['data'],ref='req9999'))]:
                response.json.return_value=changed
                with self.assertRaises(ValueError):
                    cornerstone.detail_jd(board+'/requisition/1731')

if __name__=='__main__':
    unittest.main()
