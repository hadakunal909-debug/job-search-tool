"""Score-user deadlines include reads and writes, with durable progress between batches."""
import contextlib
import io
import unittest
from unittest.mock import patch

from scraper import score_users as su


def rows(count):
    return [{"url": "https://jobs.test/%d" % i, "jd_terms": "ready", "is_active": True}
            for i in range(count)]


class ScoreUsersBudgetTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.now = 0.0
        self.stored, self.batches = {}, []
        self.write_seconds = 0.0
        self.stack.enter_context(patch.object(su.time, "monotonic", side_effect=lambda: self.now))
        self.stack.enter_context(patch.object(su.db, "has_remote_db", return_value=True))
        self.stack.enter_context(patch.object(su.db, "get_user_scores",
                                              side_effect=lambda *a: dict(self.stored)))
        self.saver = self.stack.enter_context(patch.object(
            su.db, "save_user_scores", side_effect=self.save))
        self.stack.enter_context(patch.object(su.core, "unpack_analyzed",
                                              return_value={"terms": {"python": 1}}))
        self.scorer = self.stack.enter_context(patch.object(su.core, "score_pct", return_value=72))

    def save(self, name, fp, scores, chunk=500, **kwargs):
        self.batches.append(dict(scores))
        self.stored.update(scores)
        self.now += self.write_seconds
        return len(scores)

    def test_slow_writes_stop_after_one_batch_and_next_run_resumes(self):
        source = rows(2 * su.SCORE_BATCH_SIZE + 3)
        self.write_seconds = 11
        self.assertEqual(su.score_user("user", "Python", "fp", source, deadline=10),
                         (su.SCORE_BATCH_SIZE, 0, 0))
        self.assertEqual(len(self.batches), 1)
        self.assertEqual(self.scorer.call_count, su.SCORE_BATCH_SIZE)
        self.write_seconds = 0
        self.assertEqual(su.score_user("user", "Python", "fp", source, deadline=30),
                         (su.SCORE_BATCH_SIZE + 3, su.SCORE_BATCH_SIZE, 0))
        self.assertEqual(len(self.stored), len(source))
        self.assertTrue(all(len(batch) <= su.SCORE_BATCH_SIZE for batch in self.batches))

    def test_expired_compute_does_not_start_a_write(self):
        def slow_score(*args):
            self.now += 11
            return 72
        self.scorer.side_effect = slow_score
        self.assertEqual(su.score_user("user", "Python", "fp", rows(3), deadline=10),
                         (0, 0, 0))
        self.saver.assert_not_called()

    def test_expired_read_does_not_start_scoring(self):
        def slow_read(*args):
            self.now = 11
            return {}
        with patch.object(su.db, "get_user_scores", side_effect=slow_read):
            self.assertEqual(su.score_user("user", "Python", "fp", rows(3), deadline=10),
                             (0, 0, 0))
        self.scorer.assert_not_called()
        self.saver.assert_not_called()

    def test_write_failure_surfaces_and_earlier_batch_stays_banked(self):
        def fail_second(name, fp, scores, **kwargs):
            if self.batches:
                raise RuntimeError("database unavailable")
            return self.save(name, fp, scores, **kwargs)
        self.saver.side_effect = fail_second
        with self.assertRaisesRegex(RuntimeError, "database unavailable"):
            su.score_user("user", "Python", "fp", rows(2 * su.SCORE_BATCH_SIZE), deadline=10)
        self.assertEqual(len(self.stored), su.SCORE_BATCH_SIZE)

    def test_foreign_key_skips_are_not_counted_as_written(self):
        self.saver.side_effect = lambda *a, **k: 0
        self.assertEqual(su.score_user("user", "Python", "fp", rows(3)), (0, 0, 0))

    def test_full_rewrites_current_scores_and_dry_run_writes_nothing(self):
        self.stored["https://jobs.test/0"] = 20
        self.assertEqual(su.score_user("user", "Python", "fp", rows(3), full=True), (3, 0, 0))
        self.assertEqual(self.stored["https://jobs.test/0"], 72)
        self.saver.reset_mock()
        self.assertEqual(su.score_user("user", "Python", "fp", rows(3), full=True, dry=True),
                         (3, 0, 0))
        self.saver.assert_not_called()

    def test_local_replacement_store_keeps_prior_and_earlier_batches(self):
        self.stored["https://jobs.test/prior"] = 81
        def replace(name, fp, scores, **kwargs):
            self.stored = dict(scores)
            return len(scores)
        self.saver.side_effect = replace
        with patch.object(su.db, "has_remote_db", return_value=False):
            self.assertEqual(su.score_user("user", "Python", "fp", rows(su.SCORE_BATCH_SIZE + 2)),
                             (su.SCORE_BATCH_SIZE + 2, 1, 0))
        self.assertEqual(len(self.stored), su.SCORE_BATCH_SIZE + 3)
        self.assertEqual(self.stored["https://jobs.test/prior"], 81)

    def test_main_budget_includes_corpus_read(self):
        def slow_corpus(*args, **kwargs):
            self.now = 61
            return rows(3)
        with patch.object(su.sys, "argv", ["score_users", "--budget-min", "1"]), \
                patch.object(su.db, "backend_name", return_value="fake"), \
                patch.object(su, "_profiles", return_value=[("user", "Python", "fp")]), \
                patch.object(su.db, "load_jobs", side_effect=slow_corpus):
            self.assertEqual(su.main(), 0)
        self.scorer.assert_not_called()
        self.saver.assert_not_called()

    def test_main_budget_includes_profile_read(self):
        def slow_profiles(*args, **kwargs):
            self.now = 61
            return [("user", "Python", "fp")]
        with patch.object(su.sys, "argv", ["score_users", "--budget-min", "1"]), \
                patch.object(su.db, "backend_name", return_value="fake"), \
                patch.object(su, "_profiles", side_effect=slow_profiles), \
                patch.object(su.db, "load_jobs") as load:
            self.assertEqual(su.main(), 0)
            load.assert_not_called()
        self.saver.assert_not_called()


if __name__ == "__main__":
    unittest.main()
