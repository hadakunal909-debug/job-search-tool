"""Bulk JD pagination cannot hold the scoring executor past its fetch deadline."""
import contextlib
import io
import multiprocessing
import os
import time
import unittest
from unittest.mock import patch

import scraper
from scraper import bounded_call
import scraper.score_jobs as sj
from test_jd_persist import _JD, _run_with_fetch


URL = "https://careers.example.test/jobs/1"
BOARD = ("https://careers.example.test/jobs", "jibe", "Example")


def slow_paged_board():
    # Each page returns within its own timeout, but the complete sweep takes 30s.
    # This runs in a real disposable child process and never touches the network.
    for _ in range(120):
        time.sleep(0.25)
    return {}


def run_case(overall="8", fetch="0.03", jd_map=None):
    rows = [{"url": URL, "jd": "", "title": "Program Manager", "company": "Example",
             "location": "Boston, MA", "found_date": "2026-09-24",
             "first_seen": "2026-09-24"}]
    with patch.dict(os.environ, {"SCORE_RUN_BUDGET_MIN": overall,
                                "SCORE_BUDGET_MIN": fetch,
                                "SCORE_ANALYZE_BUDGET_MIN": "0"}), \
            patch.object(scraper, "SOURCES", [BOARD]), \
            patch.object(scraper, "custom_sources", return_value=[]), \
            patch.object(sj.random, "uniform", return_value=0), \
            contextlib.redirect_stdout(io.StringIO()):
        return _run_with_fetch(rows, {}, ["--new-only"], lambda u: "", jd_map=jd_map)


class ScoreFetchDeadlineTests(unittest.TestCase):
    def test_real_paged_worker_is_reaped_without_holding_the_executor(self):
        real_call = bounded_call.call
        def slow_worker(module, function, args=(), timeout=90, **kwargs):
            self.assertEqual((module, function), ("scraper.score_jobs", "jd_map_for"))
            self.assertEqual(args, (BOARD[0], BOARD[1], {URL}))
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 1.8)
            return real_call("test_score_fetch_deadline", "slow_paged_board", timeout=timeout)
        before = {p.pid for p in multiprocessing.active_children()}
        started = time.monotonic()
        with patch.object(bounded_call, "call", side_effect=slow_worker) as isolated:
            fake, tried = run_case()
        elapsed = time.monotonic() - started
        isolated.assert_called_once()
        self.assertLess(elapsed, 10, "executor waited for the board's 30-second page loop")
        self.assertEqual({p.pid for p in multiprocessing.active_children()}, before)
        self.assertEqual(fake.rows[URL]["jd"], "")
        self.assertNotIn(URL, fake.jd_writes)
        self.assertIn(URL, fake.urls_missing_jd())
        self.assertEqual(tried, [], "expired fetch budget must not start detail requests")

    def test_completed_bounded_board_is_banked_and_capped_at_90_seconds(self):
        with patch.object(bounded_call, "call", return_value={URL: _JD}) as isolated, \
                patch.object(sj, "jd_map_for") as direct:
            fake, _ = run_case(overall="20", fetch="9", jd_map=direct)
        isolated.assert_called_once()
        self.assertEqual(isolated.call_args.kwargs["timeout"], 90)
        direct.assert_not_called()
        self.assertEqual(fake.rows[URL]["jd"], _JD)
        self.assertIn(URL, fake.jd_writes)

    def test_exhausted_fetch_reserve_does_not_start_a_worker(self):
        with patch.object(bounded_call, "call") as isolated, \
                patch.object(sj, "jd_map_for") as direct:
            fake, tried = run_case(overall="1", jd_map=direct)
        isolated.assert_not_called()
        direct.assert_not_called()
        self.assertEqual(tried, [])
        self.assertIn(URL, fake.urls_missing_jd())

    def test_deadline_is_rechecked_after_board_jitter(self):
        now = [1_000_000.0]
        def spend_jitter(*args):
            now[0] += 2
        with patch.object(sj.time, "time", side_effect=lambda: now[0]), \
                patch.object(sj.time, "sleep", side_effect=spend_jitter), \
                patch.object(bounded_call, "call") as isolated:
            fake, tried = run_case(overall="8", fetch="0.01")
        isolated.assert_not_called()
        self.assertEqual(tried, [])
        self.assertIn(URL, fake.urls_missing_jd())

    def test_unlimited_or_negative_overall_budget_preserves_direct_calls(self):
        for overall in ("0", "-1"):
            with self.subTest(overall=overall), \
                    patch.object(bounded_call, "call") as isolated, \
                    patch.object(sj, "jd_map_for", return_value={URL: _JD}) as direct:
                fake, _ = run_case(overall=overall, fetch="0", jd_map=direct)
            isolated.assert_not_called()
            direct.assert_called_once()
            self.assertEqual(fake.rows[URL]["jd"], _JD)


if __name__ == "__main__":
    unittest.main()
