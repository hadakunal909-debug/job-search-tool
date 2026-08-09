#!/usr/bin/env python3
"""
test_verify_queue.py — guards the ordering of scraper.verify_dates._candidates().

WHY. The date-lookup service is rate-limited to ~54 calls/minute and --budget-min drops the TAIL
of this queue, so queue POSITION decides whether a row ever gets a real posting date. Sorting on
found_date alone put the rows with NO date last, because an empty string is the smallest value
there is and the sort is descending.

Measured 2026-08-09 against a live queue of 8,259 rows: the 661 rows with no date at all sat at
positions 7,598-8,258, while the budget reaches roughly 432 rows per run. They were not being
processed slowly — they were never being processed. And they are the rows that need it most: with
no date the feed falls back to first_seen and the card reads "Added today", which is what made a
SAP posting from 3 August look like it had turned up that morning.

No database and no network — _candidates() is pure.
"""
import sys

from scraper import verify_dates as vd


def _q(*found_dates):
    rows = [{"url": "https://x/%d" % i, "found_date": fd} for i, fd in enumerate(found_dates)]
    return vd._candidates(rows, False)


def test_a_bare_iso_date_is_never_queued_at_all():
    # Not the ordering, but the precondition for every test below: _is_clean_api_date() trusts a
    # bare 'YYYY-MM-DD' (a real ATS date), so those rows never spend a rate-limited call. Only
    # blanks and the 'YYYY-MM-DD HH:MM' shape — Workday/Oracle and the scrape-timestamp fallback —
    # are candidates. Getting this wrong is why the fixtures below all use the datetime shape.
    assert _q("2026-08-09", "2026-07-01") == []


def test_dateless_rows_come_first():
    q = _q("2026-08-09 00:06", "", "2026-07-01 12:00", "", "2026-08-01 09:00")
    fds = [r["found_date"] for r in q]
    assert fds[:2] == ["", ""], fds
    assert all(f for f in fds[2:]), fds


def test_dated_rows_stay_newest_first():
    q = _q("2026-06-01 08:00", "2026-08-09 00:06", "2026-07-15 10:00")
    fds = [r["found_date"] for r in q]
    assert fds == ["2026-08-09 00:06", "2026-07-15 10:00", "2026-06-01 08:00"], fds


def test_ordering_holds_across_both_stored_shapes():
    # Both shapes are ISO-prefixed, so a plain string sort is still a date sort.
    q = _q("2026-08-01 09:00", "", "2026-08-09 00:06", "2026-07-01 00:00")
    fds = [r["found_date"] for r in q]
    assert fds[0] == "", fds
    assert fds[1:] == ["2026-08-09 00:06", "2026-08-01 09:00", "2026-07-01 00:00"], fds


def test_whitespace_only_counts_as_dateless():
    q = _q("2026-08-09", "   ")
    assert q[0]["found_date"].strip() == "", [r["found_date"] for r in q]


def test_a_row_with_no_url_is_not_queued():
    rows = [{"url": "", "found_date": ""}, {"url": "https://x/1", "found_date": ""}]
    assert len(vd._candidates(rows, False)) == 1


def test_already_dated_rows_are_skipped():
    # posted_verified set = we already have a real date, so it must not spend a call.
    rows = [{"url": "https://x/1", "found_date": "", "posted_verified": "2026-08-03"},
            {"url": "https://x/2", "found_date": ""}]
    q = vd._candidates(rows, False)
    assert [r["url"] for r in q] == ["https://x/2"], [r["url"] for r in q]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  -", fn.__name__)
    print("\nAll %d verify-queue checks passed." % len(fns))
