"""Phenom totals and continuation failures must not report healthy partial feeds."""
import os
os.environ['EV_OFF'] = '1'
from unittest.mock import patch
import scraper
from scraper import score_jobs
from scraper.phenom import scrape_phenom
from scraper.infosys import PartialScrapeError


class Response:
    def __init__(self, total, ids, status=200):
        self.total, self.ids, self.status = total, ids, status
    def raise_for_status(self):
        if self.status != 200:
            raise RuntimeError('HTTP %d' % self.status)
    def json(self):
        return {'refineSearch': {'status': 200, 'totalHits': self.total,
                'data': {'jobs': [{'title': 'Engineer', 'jobId': str(i)} for i in self.ids]}}}


def run(responses):
    calls = []
    def post(url, body, **kwargs):
        calls.append(body['from'])
        return responses.pop(0)
    with patch.object(scraper, '_safe_post', post), patch('scraper.phenom.time.sleep'):
        return scrape_phenom('https://careers.example.com'), calls


def main():
    rows, calls = run([Response(6200, range(0, 3100)), Response(6200, range(3100, 6200))])
    assert len(rows) == 6200 and calls == [0, 3100]
    assert rows[0]['_phenom_jid'] == '0' and rows[0]['_phenom_origin'] == 'https://careers.example.com'
    assert 'found_date' not in rows[0]
    assert run([Response(0, [])])[0] == []
    cases = [
        [Response(3, [1, 2]), Response(3, [], 500)],
        [Response(3, [1, 2]), Response(3, [])],
        [Response(3, [1, 2]), Response(3, [2])],
        [Response(3, [1, 2]), Response(4, [3, 4])],
        [Response(None, [])],
    ]
    for responses in cases:
        try:
            run(responses)
        except PartialScrapeError as exc:
            assert len(exc.rows) in (0, 2)
        else:
            raise AssertionError('Incomplete Phenom run was accepted')
    class PartitionResponse(Response):
        def json(self):
            data = super().json()
            data['refineSearch']['data']['aggregations'] = [{'field':'category', 'value':{'Tech':4,'Retail':4}}]
            return data
    selected = []
    def partitioned(url, body, **kwargs):
        selected.append(body['selected_fields'])
        category = body['selected_fields'].get('category', [''])[0]
        return {'':PartitionResponse(8, [1,2]), 'Tech':Response(4, [1,2,3,4]),
                'Retail':Response(4, [5,6,7,8])}[category]
    with patch.object(scraper, '_safe_post', partitioned), patch('scraper.phenom.RESULT_WINDOW', 5):
        assert len(scrape_phenom('https://careers.example.com')) == 8
        assert selected == [{}, {'category':['Tech']}, {'category':['Retail']}]
    def overlapping(url, body, **kwargs):
        category = body['selected_fields'].get('category', [''])[0]
        return {'':PartitionResponse(8, [1,2]), 'Tech':Response(4, [1,2,3,4]),
                'Retail':Response(4, [3,4,5,6])}[category]
    with patch.object(scraper, '_safe_post', overlapping), patch('scraper.phenom.RESULT_WINDOW', 5):
        try:
            scrape_phenom('https://careers.example.com')
        except PartialScrapeError as exc:
            assert len(exc.rows) == 6 and 'facet union' in str(exc)
        else:
            raise AssertionError('An incomplete facet union was accepted')
    with patch('scraper.phenom.RESULT_WINDOW', 5):
        try:
            run([Response(8, [1,2])])
        except PartialScrapeError as exc:
            assert len(exc.rows) == 2 and 'facet partition' in str(exc)
        else:
            raise AssertionError('An unpartitioned large listing was accepted')
    url = 'https://cvshealth.wd1.myworkdayjobs.com/CVS_Health_Careers/job/X/Engineer_R1/apply'
    normalized = scraper.canonical_url(url)
    with patch.object(scraper, 'scrape_phenom', return_value=[{'url':url,'_phenom_jid':'R1'}]) as listing, \
            patch.object(score_jobs, 'phenom_jd_by_id', return_value='Complete description') as detail:
        assert score_jobs._phenom_jd_map('https://jobs.cvshealth.com/us/en/search-results', {normalized}) == {normalized:'Complete description'}
        listing.assert_called_once_with('https://jobs.cvshealth.com/us/en/search-results')
        detail.assert_called_once_with('https://jobs.cvshealth.com', 'R1')
    with patch.object(scraper, 'scrape_phenom', side_effect=PartialScrapeError('blocked', [])):
        try:
            score_jobs._phenom_jd_map('https://jobs.cvshealth.com/us/en/search-results', {normalized})
        except PartialScrapeError:
            pass
        else:
            raise AssertionError('A failed Phenom listing became an empty JD lookup')
    print('Phenom completeness checks passed')


if __name__ == '__main__':
    main()
