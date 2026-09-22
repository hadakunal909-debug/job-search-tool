#!/usr/bin/env python3
"""Offline regressions for category provenance, full JDs and resumable backfills."""
import contextlib
import hashlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["EV_OFF"] = "1"
import db
from scripts import backfill_job_categories as backfill


ROW = {"url": "https://example.test/job/1", "title": "Project Manager", "company": "Acme"}
CONSTRUCTION = "Responsibilities: Manage construction projects and subcontractors. Review submittals and building permits."
IT = "Responsibilities: Lead software development and cloud migration. Own software releases and API deployment."


class CategoryStorageTests(unittest.TestCase):
    def test_full_fingerprint_not_prefix(self):
        left = "a" * 8000 + "construction projects"
        right = "a" * 8000 + "cloud migration"
        self.assertNotEqual(db.jd_fingerprint(left), db.jd_fingerprint(right))
        self.assertEqual(db.jd_fingerprint(left), hashlib.md5(left.encode()).hexdigest())
        self.assertIsNone(db.jd_fingerprint(" "))

    def test_remote_jd_preserved_and_category_refreshed(self):
        full = "Requirements and duties. " * 450 + IT
        with patch.object(db, "has_remote_db", return_value=True), \
             patch.object(db, "_mirror_jds") as mirror, \
             patch.object(db, "_upsert"), \
             patch.object(db, "load_job_category_inputs", return_value=[ROW]), \
             patch.object(db, "save_job_categories", return_value=1) as save:
            db.update_jds({ROW["url"]: full})
        self.assertEqual(mirror.call_args.args[0][0]["jd"], full)
        record = save.call_args.args[0][0]
        self.assertEqual(record["category"], "it")
        self.assertEqual(record["category_jd_fp"], db.jd_fingerprint(full))

    def test_full_descriptions_split_by_bytes_without_truncation(self):
        text = "x" * 1600000
        rows = [{"url": "https://example.test/%d" % i, "jd": text} for i in range(3)]
        with patch.object(db, "_upsert") as save:
            db._mirror_jds(rows)
        self.assertEqual(save.call_count, 3)
        self.assertTrue(all(call.args[0][0]["jd"] == text for call in save.call_args_list))

    def test_stale_classification_is_never_merged(self):
        current = dict(ROW, jd_fp=db.jd_fingerprint(CONSTRUCTION))
        stored = db.category_record(current, CONSTRUCTION)
        cases = [dict(current), dict(current, title="Software Engineer"),
                 dict(current, jd_fp=db.jd_fingerprint(IT)),
                 dict(current, company="Another company")]
        with patch.object(db, "job_category_rows", return_value={ROW["url"]: stored}):
            db._merge_job_categories(cases)
        self.assertEqual(cases[0]["category"], "construction")
        for changed in cases[1:]:
            self.assertNotIn("category", changed)

    def test_missing_jd_input_does_not_replace_existing_reading(self):
        with patch.object(db, "save_job_categories", return_value=0) as save:
            db.refresh_job_categories([ROW], jds={})
        self.assertEqual(save.call_args.args[0], [])

    def test_narrow_category_columns_are_not_sent_to_jobs(self):
        base, facts, terms, jd = db._route_cols("url,category,category_source")
        self.assertNotIn("category", base)
        self.assertIn("title", base)
        self.assertIn("jd_fp", base)
        self.assertFalse(facts or terms or jd)

    def test_missing_table_falls_back_but_network_error_surfaces(self):
        with patch.object(db, "has_remote_db", return_value=True), \
             patch.dict(db._category_table, {"missing_at": 0}), \
             patch.object(db, "_side_rows", side_effect=RuntimeError('relation "job_categories" does not exist')):
            self.assertEqual(db.job_category_rows(), {})
            with self.assertRaises(RuntimeError):
                db.job_category_rows(strict=True)
        with patch.object(db, "has_remote_db", return_value=True), \
             patch.dict(db._category_table, {"missing_at": 0}), \
             patch.object(db, "_side_rows", side_effect=RuntimeError("connection refused")):
            with self.assertRaises(RuntimeError):
                db.job_category_rows()

    def test_local_incremental_descriptions_and_categories_roundtrip(self):
        second = dict(ROW, url="https://example.test/job/2", title="IT Project Manager")
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(db, "has_remote_db", return_value=False), \
             patch.object(db, "JOBS_CSV", str(Path(directory) / "jobs.csv")), \
             patch.object(db, "JDS_FILE", str(Path(directory) / "jds.json")):
            db.add_jobs([ROW, second])
            db.update_jds({ROW["url"]: CONSTRUCTION})
            db.update_jds({second["url"]: IT})
            saved = {r["url"]: r for r in db._read_csv()}
            self.assertEqual(saved[ROW["url"]]["jd"], CONSTRUCTION)
            self.assertEqual(saved[ROW["url"]]["category"], "construction")
            self.assertEqual(saved[second["url"]]["category"], "it")
            self.assertEqual(len(db._load_json(db.JDS_FILE)), 2)
            with contextlib.redirect_stdout(io.StringIO()):
                report = backfill.run(verify=True)
            self.assertEqual(report["changed"], 0)

    def test_dry_run_writes_nothing_and_apply_verifies(self):
        expected = db.category_record(ROW, IT)
        with patch.object(db, "load_job_category_inputs", return_value=[ROW]), \
             patch.object(db, "has_remote_db", return_value=True), \
             patch.object(db, "_jd_rows", return_value={ROW["url"]: IT}), \
             patch.object(db, "job_category_rows", return_value={}), \
             patch.object(db, "save_job_categories") as save, \
             contextlib.redirect_stdout(io.StringIO()):
            report = backfill.run()
            self.assertEqual(report["changed"], 1)
            save.assert_not_called()
        with patch.object(db, "load_job_category_inputs", return_value=[ROW]), \
             patch.object(db, "has_remote_db", return_value=True), \
             patch.object(db, "_jd_rows", return_value={ROW["url"]: IT}), \
             patch.object(db, "job_category_rows", side_effect=[{}, {ROW["url"]: expected}]), \
             patch.object(db, "save_job_categories", return_value=1), \
             contextlib.redirect_stdout(io.StringIO()):
            report = backfill.run(apply=True)
            self.assertEqual(report["written"], 1)
            self.assertEqual(report["verified"], 1)


if __name__ == "__main__":
    unittest.main()
