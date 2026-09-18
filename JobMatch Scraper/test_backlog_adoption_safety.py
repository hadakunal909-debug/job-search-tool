"""Offline adoption regressions. All database and network operations are mocked."""
import csv
import io
import sys
import tempfile
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest import TestCase, main
from unittest.mock import Mock, patch

import db
import scraper
from scraper import adopt_everify_boards as adoption
from scraper.linked_accounts import is_account_link, resolve_linked_account


class AdoptionSafetyTests(TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.source = Path(self.directory) / "input.csv"
        self.output = Path(self.directory) / "output.csv"
        self.remote = self.stack.enter_context(patch.object(db, "has_remote_db", return_value=True))
        self.stack.enter_context(patch.object(db, "_check_backend_intent"))
        self.stack.enter_context(patch.object(db, "backend_name", return_value="test remote"))
        self.read = self.stack.enter_context(patch.object(adoption, "_remote_adoption_rows", return_value=[]))
        self.write = self.stack.enter_context(patch.object(db, "add_board", return_value=(True, "")))
        self.stack.enter_context(patch.object(scraper, "SOURCES", []))
        self.stack.enter_context(patch.object(adoption, "verify", side_effect=self.verified))
        self.stack.enter_context(patch.object(scraper, "SESSION"))
        self.stack.enter_context(patch.object(adoption.feb, "_fast_session", return_value=Mock()))

    @staticmethod
    def verified(row):
        return dict(row, verdict="confirmed", score=100, reported_name=row["employer"])

    def run_adoption(self, names=("Acme",), flags=(), bodyshop="no"):
        with self.source.open("w", newline="", encoding="utf-8") as source:
            writer = csv.DictWriter(source, fieldnames=["employer", "board_url", "ats_type", "job_count", "bodyshop"])
            writer.writeheader()
            for i, name in enumerate(names):
                writer.writerow(dict(employer=name, board_url="https://jobs.lever.co/test%d" % i,
                                     ats_type="lever", job_count=10, bodyshop=bodyshop))
        argv = ["adopt", "--csv", str(self.source), "--out", str(self.output),
                "--workers", "1", "--no-yield-check", "--no-location-check"] + list(flags)
        with patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()) as stdout:
            result = adoption.main()
        records = []
        if self.output.exists():
            with self.output.open(encoding="utf-8", newline="") as source:
                records = list(csv.DictReader(source))
        return result, records, stdout.getvalue()

    def test_failed_return_is_not_reported_as_success(self):
        self.write.return_value = False, "permission denied"
        code, rows, output = self.run_adoption()
        self.assertEqual(code, 1)
        self.assertEqual(rows[0]["added"], "ERROR: permission denied")
        self.assertIn("ADDED: 0 board(s)", output)
        self.assertIn("FAILED database writes: 1", output)

    def test_raised_write_error_is_reported_and_nonzero(self):
        self.write.side_effect = TimeoutError("database timed out")
        code, rows, _ = self.run_adoption()
        self.assertEqual(code, 1)
        self.assertIn("database timed out", rows[0]["added"])

    def test_success_count_requires_acknowledged_write(self):
        code, rows, output = self.run_adoption(flags=("--require-remote",))
        self.assertEqual(code, 0)
        self.assertEqual(rows[0]["added"], "yes")
        self.assertIn("ADDED: 1 board(s)", output)
        self.write.assert_called_once()

    def test_remote_read_failure_aborts_before_any_write(self):
        self.read.side_effect = RuntimeError("blocklist unavailable")
        code, _, output = self.run_adoption()
        self.assertEqual(code, 1)
        self.write.assert_not_called()
        self.assertIn("blocklist unavailable", output)

    def test_malformed_remote_blocks_fail_closed(self):
        for invalid in ({"error": "forbidden"}, [None], [{}], [{"name_key": ""}]):
            with self.subTest(invalid=invalid):
                self.read.return_value = invalid
                code, _, _ = self.run_adoption()
                self.assertEqual(code, 1)
                self.write.assert_not_called()

    def test_blocked_company_is_never_written_including_tcs_override(self):
        self.read.side_effect = lambda table, params: (
            [{"name_key": "tata consultancy services"}] if table == db.BLOCKED_TABLE else [])
        code, rows, _ = self.run_adoption(("Tata Consultancy Services Limited",),
                                         ("--allow-tcs", "--include-bodyshops"), bodyshop="yes")
        self.assertEqual(code, 0)
        self.assertIn("admin blocklist", rows[0]["added"])
        self.write.assert_not_called()

    def test_tcs_exception_does_not_include_other_staffing_firms_or_namesakes(self):
        names = ("Tata Consultancy Services Limited", "TCS", "TCS Solutions", "Other Staffing LLC")
        code, rows, _ = self.run_adoption(names, ("--allow-tcs",), bodyshop="yes")
        self.assertEqual(code, 0)
        self.assertEqual([r["employer"] for r in rows if r["added"] == "yes"], list(names[:2]))
        self.assertEqual(self.write.call_count, 2)

    def test_tcs_requires_explicit_exception(self):
        code, rows, _ = self.run_adoption(("Tata Consultancy Services",), bodyshop="yes")
        self.assertEqual(code, 0)
        self.assertEqual(rows[0]["added"], "no")
        self.write.assert_not_called()

    def test_explicit_legacy_bodyshop_option_remains_supported(self):
        code, rows, _ = self.run_adoption(("Other Staffing LLC",), ("--include-bodyshops",), "yes")
        self.assertEqual(code, 0)
        self.assertEqual(rows[0]["added"], "yes")

    def test_require_remote_rejects_local_even_during_dry_run(self):
        self.remote.return_value = False
        code, _, output = self.run_adoption(flags=("--require-remote", "--dry-run"))
        self.assertEqual(code, 1)
        self.write.assert_not_called()
        self.assertIn("--require-remote needs", output)

    def test_dry_run_reads_real_blocks_but_does_not_write(self):
        code, rows, _ = self.run_adoption(flags=("--dry-run",))
        self.assertEqual(code, 0)
        self.assertEqual(rows[0]["added"], "would-add")
        self.assertEqual(self.read.call_args_list[0].args[0], db.BLOCKED_TABLE)
        self.write.assert_not_called()

    def test_existing_database_board_is_skipped(self):
        self.read.side_effect = lambda table, params: (
            [{"url": "https://jobs.lever.co/test0"}] if table == db.BOARDS_TABLE else [])
        code, rows, _ = self.run_adoption()
        self.assertEqual(code, 0)
        self.assertEqual(rows[0]["added"], "no")
        self.write.assert_not_called()

    def test_corrupt_local_block_file_is_not_an_empty_blocklist(self):
        self.remote.return_value = False
        block_path = Path(self.directory) / "blocked.json"
        block_path.write_text("{corrupt", encoding="utf-8")
        with patch.object(db, "BLOCKED_FILE", str(block_path)):
            code, _, _ = self.run_adoption()
        self.assertEqual(code, 1)
        self.write.assert_not_called()


class RemoteAdoptionReadTests(TestCase):
    def test_bad_json_shape_cannot_turn_into_an_empty_blocklist(self):
        for payload in ({}, {"error": "denied"}, None, [None], [{}]):
            with self.subTest(payload=payload), patch.object(db, "_http", new=Mock()) as http:
                http.get.return_value.json.return_value = payload
                with self.assertRaises(RuntimeError):
                    adoption._remote_adoption_rows(db.BLOCKED_TABLE, "name_key")

    def test_remote_paging_keeps_blocks_after_the_first_page(self):
        first = [{"name_key": "blocked %d" % i} for i in range(1000)]
        last = [{"name_key": "tata consultancy services"}]
        with patch.object(db, "_http", new=Mock()) as http:
            http.get.side_effect = [Mock(json=Mock(return_value=first)),
                                    Mock(json=Mock(return_value=last))]
            rows = adoption._remote_adoption_rows(db.BLOCKED_TABLE, "name_key")
        self.assertEqual(len(rows), 1001)
        self.assertEqual(rows[-1], last[0])
        self.assertEqual(http.get.call_args_list[1].kwargs["params"]["offset"], 1000)
        self.assertEqual(http.get.call_args_list[0].kwargs["params"]["order"], "name_key")


class LinkedAccountTests(TestCase):
    WORKABLE = "https://apply.workable.com/j/2B2D6E78CE"
    JOBVITE = "https://app.jobvite.com/CompanyJobs/Job.aspx?j=oJSLAfwG&s=Indeed"

    def setUp(self):
        self.get = patch.object(scraper, "_safe_get").start()
        self.addCleanup(patch.stopall)

    @staticmethod
    def response(url, text="", status=200):
        return Mock(url=url, text=text, status_code=status)

    def test_workable_redirect_uses_account_not_j(self):
        self.get.return_value = self.response("https://apply.workable.com/zifo/j/2B2D6E78CE")
        result = resolve_linked_account(self.WORKABLE)
        self.assertEqual(result[:2], ("https://apply.workable.com/zifo", "workable"))
        self.get.assert_called_once_with(self.WORKABLE, timeout=15)

    def test_jobvite_careers_prefix_is_not_account(self):
        self.get.return_value = self.response("https://jobs.jobvite.com/careers/tylertech/job/oJSLAfwG")
        self.assertEqual(resolve_linked_account(self.JOBVITE)[:2],
                         ("https://jobs.jobvite.com/tylertech", "jobvite"))

    def test_redirect_hop_cap_still_preserves_verified_account(self):
        response = self.response("https://jobs.jobvite.com/careers/imprivata/jobs", status=302)
        self.assertEqual(resolve_linked_account(self.JOBVITE, response)[:2],
                         ("https://jobs.jobvite.com/imprivata", "jobvite"))

    def test_passed_response_avoids_second_fetch(self):
        response = self.response(self.WORKABLE, '<link rel="canonical" href="/dsn/j/50FF280DC4">')
        self.assertEqual(resolve_linked_account(self.WORKABLE, response)[:2],
                         ("https://apply.workable.com/dsn", "workable"))
        self.get.assert_not_called()

    def test_jobvite_embed_on_employer_landing_resolves(self):
        response = self.response("https://www.imprivata.com/company/join-team", '<div class="jv-careersite" data-careersite="imprivata"></div><script src="https://jobs.jobvite.com/__assets__/scripts/careersite/public/iframe.js"></script>')
        self.assertEqual(resolve_linked_account(self.JOBVITE, response)[:2],
                         ("https://jobs.jobvite.com/imprivata", "jobvite"))

    def test_generic_jobvite_asset_is_never_a_tenant(self):
        response = self.response(self.JOBVITE, '<script src="https://jobs.jobvite.com/__assets__/scripts/careersite/public/iframe.js"></script>')
        self.assertIsNone(resolve_linked_account(self.JOBVITE, response))

    def test_conflicting_accounts_are_left_unresolved(self):
        response = self.response(self.WORKABLE, '<a href="https://apply.workable.com/zifo">Zifo</a><a href="https://apply.workable.com/dsn">DSN</a>')
        self.assertIsNone(resolve_linked_account(self.WORKABLE, response))

    def test_expired_or_refused_page_is_unresolved(self):
        for status in (403, 404, 500):
            self.get.return_value = self.response("https://apply.workable.com/zifo", status=status)
            self.assertIsNone(resolve_linked_account(self.WORKABLE))
        self.get.side_effect = ValueError("blocked redirect")
        self.assertIsNone(resolve_linked_account(self.WORKABLE))

    def test_other_urls_are_not_fetched(self):
        for url in ("https://apply.workable.com/zifo", "https://apply.workable.com/j/",
                    "https://evil.example/j/123", "https://app.jobvite.com/CompanyJobs/Job.aspx",
                    "https://apply.workable.com.evil.example/j/123",
                    "https://user@apply.workable.com/j/123"):
            self.assertFalse(is_account_link(url))
            self.assertIsNone(resolve_linked_account(url))
        self.get.assert_not_called()

    def test_unrelated_platform_link_cannot_mislabel_account(self):
        response = self.response(self.WORKABLE, '<a href="https://jobs.jobvite.com/acme">Jobs</a>')
        self.assertIsNone(resolve_linked_account(self.WORKABLE, response))


if __name__ == "__main__":
    main(verbosity=2)
