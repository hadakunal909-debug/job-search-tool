"""Scoring checkpoints require durable derived fields and bounded comparison reads."""
import copy
import unittest
from unittest.mock import patch

import core
import db
from scraper import score_jobs as sj


URL = "https://example.com/jobs/one"
JD = ("Project manager responsible for roadmap planning, stakeholder communication, "
      "risk management, program delivery, budgeting and schedules. " * 5)


class FakeDB:
    JOBS_DERIVED_SQL = ""
    jd_fingerprint = staticmethod(db.jd_fingerprint)
    _column_missing = staticmethod(db._column_missing)

    def __init__(self, row):
        self.row = copy.deepcopy(row)
        self.reads = []
        self.writes = []
        self.invalidations = []
        self.events = []
        self.fail = None

    def has_remote_db(self):
        return True

    def _terms_rows(self, urls):
        self.reads.append(("terms", list(urls)))
        if self.fail == "read":
            raise RuntimeError("comparison read failed")
        return {u: self.row.get("jd_terms") for u in urls}

    def _facts_rows(self, urls):
        self.reads.append(("facts", list(urls)))
        return {u: {k: self.row.get(k) for k in db.JOB_FACTS_COLS} for u in urls}

    def refresh_job_categories(self, rows, jds=None, strict=False):
        if self.fail == "category":
            if strict:
                raise RuntimeError("category write failed")
            return 0
        return 0

    def update_job_fields(self, rows, keys=None):
        self.events.append("write")
        if self.fail == "fields":
            raise RuntimeError("derived write failed")
        if self.fail == "facts_fp":
            raise RuntimeError("column facts_fp does not exist")
        self.writes.append(copy.deepcopy(rows))
        for row in rows:
            self.row.update(row)

    def clear_scores_for_urls(self, urls):
        self.events.append("invalidate")
        self.invalidations.append(list(urls))
        if self.fail == "invalidate":
            raise RuntimeError("invalidation failed")
        if self.fail == "partial_invalidate":
            return 0
        return len(urls)


class ScorePersistenceTests(unittest.TestCase):
    def setUp(self):
        meta = core.job_meta(JD, {})
        meta["jd_fp"] = db.jd_fingerprint(JD)
        self.meta = {URL: meta}
        p, salary = core.parse_location("Boston, MA", JD), core.parse_salary(JD)
        sv, reason = meta.get("sponsor_jd") or ("", "")
        self.row = {
            "url": URL, "title": "Project Manager", "company": "Example",
            "location": "Boston, MA", "jd_fp": meta["jd_fp"],
            "loc_state": p["state"], "loc_metro": p["metro"], "remote": bool(p["remote"]),
            "salary_min": salary["min"], "salary_max": salary["max"],
            "salary_period": salary["period"], "exp_max_years": meta.get("exp_years"),
            "sponsor_jd": sv, "sponsor_reason": reason,
            "facts_fp": meta["jd_fp"], "jd_terms": core.pack_analyzed(meta["analyzed"]),
        }

    def persist(self, fake, strict=True):
        # Model the production COLS_SCORE projection: terms and provenance are omitted.
        selected = {k: v for k, v in self.row.items() if k not in ("jd_terms", "facts_fp")}
        with patch.object(sj, "db", fake):
            sj._persist_derived({URL: "Boston, MA"}, {URL: JD},
                                current_rows=[selected], jdmeta=self.meta, idf={}, strict=strict)

    def changed(self):
        fake = FakeDB(self.row)
        fake.row["jd_terms"] = "old analysis"
        return fake

    def test_unchanged_analysis_reads_only_selected_urls_and_preserves_user_scores(self):
        fake = FakeDB(self.row)
        self.persist(fake)
        self.assertEqual(fake.reads, [("terms", [URL]), ("facts", [URL])])
        self.assertEqual(fake.writes, [])
        self.assertEqual(fake.invalidations, [])

    def test_strict_changed_analysis_invalidates_before_writing(self):
        fake = self.changed()
        self.persist(fake)
        self.assertEqual(fake.events, ["invalidate", "write"])
        self.assertEqual(fake.row["jd_terms"], self.row["jd_terms"])

    def test_strict_read_failure_is_not_a_successful_checkpoint(self):
        fake = self.changed()
        fake.fail = "read"
        with self.assertRaisesRegex(RuntimeError, "comparison read"):
            self.persist(fake)
        self.assertEqual(fake.events, [])

    def test_strict_category_failure_is_not_a_successful_checkpoint(self):
        fake = self.changed()
        fake.fail = "category"
        with self.assertRaisesRegex(RuntimeError, "category write"):
            self.persist(fake)
        self.assertEqual(fake.events, [])

    def test_strict_write_failure_keeps_batch_retryable(self):
        fake = self.changed()
        fake.fail = "fields"
        with self.assertRaisesRegex(RuntimeError, "derived write"):
            self.persist(fake)
        self.assertEqual(fake.row["jd_terms"], "old analysis")
        fake.fail = None
        self.persist(fake)
        self.assertEqual(fake.row["jd_terms"], self.row["jd_terms"])
        self.assertEqual(fake.invalidations, [[URL], [URL]])

    def test_strict_missing_provenance_is_not_silently_downgraded(self):
        fake = self.changed()
        fake.fail = "facts_fp"
        with self.assertRaisesRegex(RuntimeError, "facts_fp"):
            self.persist(fake)
        self.assertEqual(fake.events.count("write"), 1)

    def test_strict_invalidation_failure_prevents_analysis_replacement(self):
        for failure in ("invalidate", "partial_invalidate"):
            with self.subTest(failure=failure):
                fake = self.changed()
                fake.fail = failure
                with self.assertRaises(RuntimeError):
                    self.persist(fake)
                self.assertEqual(fake.writes, [])
                self.assertEqual(fake.row["jd_terms"], "old analysis")

    def test_legacy_write_failure_remains_best_effort(self):
        fake = self.changed()
        fake.fail = "fields"
        self.persist(fake, strict=False)
        self.assertEqual(fake.invalidations, [])


class CorpusReadTests(unittest.TestCase):
    class FakeCorpusDB:
        def __init__(self, jds, missing=()):
            self.jds = dict(jds)
            self.missing = set(missing)
            self.calls = []

        def has_remote_db(self):
            return True

        def urls_missing_jd(self):
            return set(self.missing)

        def _jd_rows(self, urls=None):
            self.calls.append(urls)
            return dict(self.jds) if urls is None else {u: self.jds[u] for u in urls if u in self.jds}

        def load_jobs(self, *args, **kwargs):
            raise AssertionError("corpus hydration must not read complete job rows")

    def test_large_partial_cache_downloads_only_missing_descriptions(self):
        missing_urls = {"https://example.com/jobs/%d" % i for i in range(6000)}
        cached_urls = {"https://example.com/jobs/cached%d" % i for i in range(7000)}
        no_jd = "https://example.com/jobs/empty"
        fake = self.FakeCorpusDB({u: JD for u in missing_urls}, missing=[no_jd])
        all_urls = missing_urls | cached_urls | {no_jd}
        with patch.object(sj, "db", fake), patch.object(sj, "_load_jd_cache", return_value={u: JD for u in cached_urls}):
            corpus, missing, db_missing = sj._jd_corpus(all_urls, False)
        self.assertEqual(fake.calls, [sorted(missing_urls)])
        self.assertEqual(set(corpus), missing_urls | cached_urls)
        self.assertEqual(missing, {no_jd})
        self.assertEqual(db_missing, {no_jd})

    def test_empty_cache_uses_description_table_only_and_drops_unrelated_rows(self):
        fake = self.FakeCorpusDB({URL: JD, "https://example.com/jobs/pruned": JD})
        with patch.object(sj, "db", fake), patch.object(sj, "_load_jd_cache", return_value={}):
            corpus, missing, _ = sj._jd_corpus({URL}, False)
        self.assertEqual(fake.calls, [None])
        self.assertEqual(corpus, {URL: JD})
        self.assertEqual(missing, set())

    def test_mostly_cold_cache_uses_one_description_table_read(self):
        cached_url = "https://example.com/jobs/cached"
        other = "https://example.com/jobs/two"
        fake = self.FakeCorpusDB({URL: JD, other: JD})
        with patch.object(sj, "db", fake), patch.object(sj, "_load_jd_cache", return_value={cached_url: JD}):
            corpus, missing, _ = sj._jd_corpus({URL, other, cached_url}, False)
        self.assertEqual(fake.calls, [None])
        self.assertEqual(set(corpus), {URL, other, cached_url})
        self.assertEqual(missing, set())

    def test_full_mode_preserves_description_refetch_semantics(self):
        fake = self.FakeCorpusDB({URL: JD})
        with patch.object(sj, "db", fake), patch.object(sj, "_load_jd_cache", return_value={URL: JD}):
            corpus, missing, _ = sj._jd_corpus({URL}, True)
        self.assertEqual(fake.calls, [])
        self.assertEqual(corpus, {URL: JD})
        self.assertEqual(missing, {URL})

    def test_failed_hydration_does_not_continue_with_incomplete_corpus(self):
        fake = self.FakeCorpusDB({URL: JD})
        with patch.object(sj, "db", fake), patch.object(sj, "_load_jd_cache", return_value={}), \
                patch.object(fake, "_jd_rows", side_effect=RuntimeError("description read failed")):
            with self.assertRaisesRegex(RuntimeError, "description read failed"):
                sj._jd_corpus({URL}, False)


class CursorPersistenceTests(unittest.TestCase):
    class FakeCursorDB:
        SCRAPE_STATUS_TABLE = "scrape_status"

        def __init__(self, remote=True, fail=False):
            self.remote = remote
            self.fail = fail
            self.remote_calls = []
            self.local_calls = []

        def has_remote_db(self):
            return self.remote

        def _upsert(self, rows, table=None, pk=None):
            self.remote_calls.append((rows, table, pk))
            if self.fail:
                raise RuntimeError("checkpoint write failed")

        def put_kv(self, key, value):
            self.local_calls.append((key, value))

    def test_remote_checkpoint_uses_strict_persistence_and_expected_schema(self):
        fake = self.FakeCursorDB()
        key = ("2026-09-24", "2026-09-23", URL)
        with patch.object(sj, "db", fake):
            sj._save_cursor("revision", key)
        rows, table, pk = fake.remote_calls[0]
        self.assertEqual((table, pk), ("scrape_status", "id"))
        self.assertEqual(rows[0]["id"], sj.CURSOR_KEY)
        self.assertEqual(rows[0]["data"]["key"], list(key))
        self.assertEqual(rows[0]["data"]["rev"], "revision")
        self.assertEqual(rows[0]["data"]["updated_at"], rows[0]["updated_at"])
        self.assertEqual(fake.local_calls, [])

    def test_failed_remote_checkpoint_does_not_report_local_fallback_as_durable(self):
        fake = self.FakeCursorDB(fail=True)
        with patch.object(sj, "db", fake), self.assertRaisesRegex(RuntimeError, "checkpoint write"):
            sj._save_cursor("revision", ("", "", URL))
        self.assertEqual(fake.local_calls, [])

    def test_finished_cycle_clears_remote_checkpoint(self):
        fake = self.FakeCursorDB()
        with patch.object(sj, "db", fake):
            sj._save_cursor("revision", None)
        data = fake.remote_calls[0][0][0]["data"]
        self.assertNotIn("key", data)
        self.assertNotIn("rev", data)

    def test_local_backend_keeps_existing_kv_persistence(self):
        fake = self.FakeCursorDB(remote=False)
        with patch.object(sj, "db", fake):
            sj._save_cursor("revision", None)
        self.assertEqual(fake.remote_calls, [])
        self.assertEqual(fake.local_calls, [(sj.CURSOR_KEY, {})])


if __name__ == "__main__":
    unittest.main()
