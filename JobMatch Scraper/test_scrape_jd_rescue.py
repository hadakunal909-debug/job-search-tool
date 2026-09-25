"""A recovered description must reach storage without aborting the scrape's save phase."""
import contextlib
import io
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("EV_OFF", "1")

import scraper


STORED_URL = "https://careers.example.test/jobs/123"
NEW_URL = "https://careers.example.test/jobs/456"
AGGREGATOR_URL = "https://www.indeed.com/viewjob?jk=abc"
JD = ("We seek a Software Engineer to build applications, design APIs and collaborate "
      "with product teams. Requirements include Python and software development. " * 8)


def posting(url, jd=""):
    return {"url": url, "title": "Software Engineer", "company": "Example",
            "location": "Boston, MA", "found_date": "", "jd": jd}


def run_scrape(candidate_url, jd=JD, missing=True, slice_size=0):
    written, jd_writes, requeued, statuses = [], [], [], []
    incumbent = posting(STORED_URL)
    boards = [("https://board.example.test/1", "greenhouse", "Example"),
              ("https://board.example.test/2", "greenhouse", "Example")]
    rows = {boards[0][0]: [posting(candidate_url, jd), posting(NEW_URL)],
            boards[1][0]: [posting(candidate_url, jd)]}

    def fake_scrape(sources, **kwargs):
        return [dict(row) for source in sources for row in rows[source[0]]]

    scraper_stubs = {
        "SOURCES": boards, "SCRAPE_SLICE": slice_size,
        "SCRAPE_BUDGET_MIN": 0, "SCRAPE_ROTATE": 0,
        "JOBSPY_BOARDS": [("fixture", "fixture")], "JOBRIGHT_BOARDS": [],
        "JOBSPY_FINGERPRINT_ENFORCE": True, "JOBSPY_JDS": {},
        "JOBSPY_CALLS": [0], "JOBRIGHT_CALLS": [0],
        "DUMP_REJECTS": "", "VERBOSE": False,
        "scrape_all": fake_scrape, "custom_sources": lambda: [],
        "load_sponsors": lambda: set(), "apply_resume_terms": lambda: [],
        "fill_missing_jds": lambda *a, **k: (0, 0),
        "publish_known_urls": lambda *a, **k: None,
        "save_board_health": lambda *a, **k: None,
        "truncation_report": lambda: "",
    }
    db_stubs = {
        "load_jobs": lambda *a, **k: [dict(incumbent)],
        "existing_urls": lambda: {STORED_URL},
        "urls_missing_jd": lambda: {STORED_URL} if missing else set(),
        "blocked_company_keys": lambda: set(),
        "add_jobs": lambda batch: written.extend(dict(r) for r in batch),
        "update_jds": lambda batch: jd_writes.append(dict(batch)),
        "requeue_analysis": lambda urls: requeued.extend(urls),
        "set_scrape_status": lambda status: statuses.append(dict(status)),
        "backend_name": lambda: "fake", "has_remote_db": lambda: True,
    }
    output = io.StringIO()
    with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
        stack.enter_context(contextlib.chdir(tmp))
        stack.enter_context(contextlib.redirect_stdout(output))
        stack.enter_context(patch.dict(os.environ, {
            "DISCOVER_LIMIT": "0", "PRUNE_DAYS": "0", "RECONCILE_CLOSED": "0"}))
        stack.enter_context(patch.multiple(scraper, **scraper_stubs))
        stack.enter_context(patch.multiple(scraper.db, **db_stubs))
        stack.enter_context(patch.object(scraper.core, "load_visa_tags", return_value={}))
        stack.enter_context(patch.object(scraper.core, "load_sponsor_counts", return_value={}))
        scraper.main()
    return written, jd_writes, requeued, statuses, output.getvalue()


class ScrapeJDRescueTests(unittest.TestCase):
    def assert_recovery(self, candidate_url, slice_size):
        written, jd_writes, requeued, statuses, output = run_scrape(
            candidate_url, slice_size=slice_size)
        self.assertEqual([row["url"] for row in written], [NEW_URL])
        self.assertEqual(jd_writes, [{STORED_URL: JD.strip()}])
        self.assertEqual(requeued, [STORED_URL])
        self.assertEqual(statuses[-1]["phase"], "scoring")
        self.assertEqual(statuses[-1]["new"], 1)
        self.assertIn("Recovered descriptions for existing postings: 1 by", output)

    def test_exact_url_rescue_banks_description_and_new_jobs(self):
        self.assert_recovery(STORED_URL, slice_size=0)

    def test_fingerprint_rescue_banks_description_and_new_jobs(self):
        self.assert_recovery(AGGREGATOR_URL, slice_size=0)

    def test_rescue_is_written_once_across_slices(self):
        for url in (STORED_URL, AGGREGATOR_URL):
            with self.subTest(url=url):
                self.assert_recovery(url, slice_size=1)

    def test_existing_description_and_short_teaser_are_never_replaced(self):
        for candidate_url in (STORED_URL, AGGREGATOR_URL):
            for jd, missing in ((JD, False), ("Loading, please wait", True)):
                with self.subTest(candidate_url=candidate_url, missing=missing):
                    written, jd_writes, requeued, statuses, _ = run_scrape(
                        candidate_url, jd=jd, missing=missing)
                    self.assertEqual([r["url"] for r in written], [NEW_URL])
                    self.assertEqual(jd_writes, [])
                    self.assertEqual(requeued, [])
                    self.assertEqual(statuses[-1]["phase"], "scoring")


if __name__ == "__main__":
    unittest.main()
