"""Source retention and recovery accounting are independent of network availability."""
import csv
import json
from pathlib import Path
import tempfile
import unittest
from scripts import retry_unscrapeable as retry


class RetryTests(unittest.TestCase):
    def test_original_order_and_bytes_survive_repeated_employers(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source.csv"
            content = b'company,why\r\nAcme,"first\r\nline"\r\nAcme,second\r\nAcme LLC,third\r\n'
            source.write_bytes(content)
            out = root / "run"
            manifest = retry.prepare(out, [source], sources=[])
            self.assertEqual(source.read_bytes(), content)
            self.assertEqual((out / manifest["sources"][0]["copy"]).read_bytes(), content)
            self.assertEqual(len(manifest["candidates"]), 2)
            report = next((out / "reports").glob("*.csv"))
            with report.open(encoding="utf-8-sig", newline="") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual([r["why"] for r in rows], ["first\r\nline", "second", "third"])
            self.assertEqual([r["source_line"] for r in rows], ["2", "4", "5"])

    def test_partial_jsonl_tail_does_not_swallow_next_record(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "checkpoint.jsonl"
            p.write_text('{"unfinished":', encoding="utf-8")
            retry.append_json(p, {"complete": True})
            self.assertEqual(retry.read_jsonl(p), [{"complete": True}])

    def test_resume_recovers_completed_workers_once(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out/'workers').mkdir()
            result = {'candidate_id': 'id', 'retry_status': 'scrapeable'}
            (out/'workers/id.result.json').write_text(json.dumps(result), encoding='utf-8')
            manifest = {'candidates': [{'candidate_id': 'id'}]}
            self.assertEqual(retry.recover_completed_workers(out, manifest), 1)
            self.assertEqual(retry.recover_completed_workers(out, manifest), 0)
            self.assertEqual(retry.read_jsonl(out/'checkpoints.jsonl'), [result])

    def test_all_direct_links_and_legacy_ats_fields_are_available(self):
        c = {"known_boards": [], "records": [{"original": {"board_url": "https://example.com", "ats": "workday", "example_url": "https://example.com/job/1"}}, {"original": {"url": "https://example.com/job/2"}}]}
        boards, pages, direct = retry.candidate_links(c)
        self.assertEqual(boards[0][1], "workday")
        self.assertEqual(len(direct), 2)

    def test_partial_or_failed_result_never_counts_confirmed(self):
        candidate = {"candidate_id": "id", "company": "Acme"}
        for fields in ({"complete": False}, {"error": "metrics failed"}):
            result = retry.summarize(candidate, [dict(kind="board", url="https://example.com", fetched=1, verdict="confirmed", **fields)])
            self.assertEqual(result["confirmed_boards"], 0)
            self.assertNotEqual(result["retry_status"], "scrapeable")

    def test_reconciled_report_preserves_rows_and_does_not_relabel_held_board(self):
        from scripts.report_retry_recovery import report
        from contextlib import redirect_stdout
        import io
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root/'unscrapeable_boards.csv'
            source.write_text('company,why\nAcme,first\nAcme,second\n', encoding='utf-8')
            out = root/'run'
            manifest = retry.prepare(out, [source], sources=[])
            candidate = manifest['candidates'][0]
            board = 'https://job-boards.greenhouse.io/acme'
            (root/'snapshot.json').write_text(json.dumps({'boards': [{'url': 'https://boards.greenhouse.io/acme/'}]}), encoding='utf-8')
            retry.append_json(out/'checkpoints.jsonl', retry.summarize(candidate, [dict(kind='board', url=board,
                ats_type='greenhouse', fetched=1, complete=True, verdict='confirmed', raw_jobs_file='raw_jobs/a.json')]))
            (out/'raw_jobs/a.json').write_text(json.dumps({'company':'Acme', 'jobs':[{'url':'https://example.com/job/1'}]}), encoding='utf-8')
            # A process may save rows just before it is interrupted while writing evidence.
            (out/'raw_jobs/orphan.json').write_text(json.dumps({'jobs':[{'url':'https://example.com/job/2'}]}), encoding='utf-8')
            with redirect_stdout(io.StringIO()):
                result = report(out, [], root/'report')
            self.assertTrue(result['all_sources_preserved'])
            self.assertEqual(result['distinct_returned_job_urls'], 2)
            self.assertEqual(result['new_complete_board_urls'], 0)
            with (root/'report/unscrapeable_boards_rechecked.csv').open(encoding='utf-8-sig', newline='') as f:
                self.assertEqual([r['why'] for r in csv.DictReader(f)], ['first', 'second'])


if __name__ == "__main__":
    unittest.main()
