"""Network stalls must stop at a deadline without losing finished work."""
import multiprocessing
import sys
import time
import unittest
from unittest.mock import patch

from scraper.bounded_call import call
from scripts import close_dead_jds as cleanup
from scripts import jobspy_sweep as sweep


class BoundedCallTests(unittest.TestCase):
    def test_success_and_large_result_cross_process_boundary(self):
        self.assertEqual(call("builtins", "str", ("x" * 100000,), timeout=10), "x" * 100000)

    def test_hung_query_is_terminated_and_reaped(self):
        before = {p.pid for p in multiprocessing.active_children()}
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            call("time", "sleep", (60,), timeout=0.25)
        self.assertLess(time.monotonic() - started, 6)
        self.assertEqual({p.pid for p in multiprocessing.active_children()}, before)
        self.assertEqual(call("builtins", "len", ([1, 2],), timeout=10), 2)

    def test_worker_failure_is_visible(self):
        with self.assertRaisesRegex(RuntimeError, "ValueError"):
            call("builtins", "int", ("not-a-number",), timeout=10)

    def test_harvest_retains_success_after_timed_out_query(self):
        record = {"url": "https://example.test/job", "company": "Example"}
        with patch.object(sweep, "bounded_call", side_effect=[TimeoutError("blocked"), [record]]) as run:
            rows = sweep.harvest(["indeed", "linkedin"], ["coordinator"], "USA", 24, 50, False)
        self.assertEqual(rows, [record])
        self.assertEqual(run.call_count, 2)

    def test_expired_budget_starts_no_query(self):
        with patch.object(sweep, "bounded_call") as run:
            self.assertEqual(sweep.harvest(["indeed"], ["pm"], "USA", 24, 50, False,
                                          deadline=time.monotonic() - 1), [])
        run.assert_not_called()

    def test_findings_are_saved_before_optional_probes(self):
        events = []
        rows = [{"url": "https://example.test/job", "company": "Example", "title": "Coordinator"}]
        with patch.object(sys, "argv", ["jobspy_sweep", "--apply", "--out", "unused.csv"]), \
                patch.object(sweep.importlib.util, "find_spec", return_value=object()), \
                patch.object(sweep, "harvest", return_value=rows), \
                patch.object(sweep, "known_companies", return_value=(set(), True)), \
                patch.object(sweep.core, "load_visa_tags", return_value={}), \
                patch.object(sweep.core, "load_sponsor_counts", return_value={}), \
                patch.object(sweep, "enrich", return_value=("", "", 0)), \
                patch.object(sweep.scraper, "detect_board", return_value=None), \
                patch.object(sweep, "write_report", side_effect=lambda *_: events.append("report")), \
                patch.object(sweep.db, "add_findings", side_effect=lambda *_: (events.append("save") or (1, ""))), \
                patch.object(sweep, "bounded_call", side_effect=lambda *_a, **_kw: (events.append("probe") or ("", "", ""))):
            self.assertEqual(sweep.main(), 0)
        self.assertEqual(events[:3], ["report", "save", "probe"])

    def test_cleanup_never_closes_or_clears_unattempted_rows(self):
        urls = ["https://example.test/1", "https://example.test/2"]
        with patch.object(sys, "argv", ["cleanup", "--apply", "--workers", "1", "--budget-min", "1"]), \
                patch.object(cleanup.time, "monotonic", side_effect=[0, 1, 100]), \
                patch.object(cleanup.db, "load_jobs", return_value=[{"url": u} for u in urls]), \
                patch.object(cleanup.db, "urls_missing_jd", return_value=set(urls)), \
                patch.object(cleanup.sj, "_load_jd_cache", return_value={}), \
                patch.object(cleanup, "_probe", return_value=("gone", "404", 0, "")) as probe, \
                patch.object(cleanup.db, "update_job_fields") as update, \
                patch.object(cleanup.db, "update_jds") as update_jds, \
                patch.object(cleanup.db, "get_kv", return_value={}), \
                patch.object(cleanup.db, "put_kv"):
            cleanup.main()
        self.assertEqual(probe.call_count, 1)
        update.assert_called_once_with([{"url": urls[0], "is_active": False}])
        update_jds.assert_not_called()


if __name__ == "__main__":
    unittest.main()
