#!/usr/bin/env python3
"""
test_workday_date.py — guards scraper._workday_date() against the '30+' trap.

WHY THIS FILE EXISTS. Workday's CXS API reports age as human text, and one of its values is a
LOWER BOUND: 'Posted 30+ Days Ago'. Reading the 30 out of that as an exact age made every such
posting land on found_date == exactly MAX_AGE_DAYS ago, and main()'s cutoff drops a row only when
`posted < age_cutoff` — so 'exactly 30 days old' is not 'over 30 days old' and they all passed.

Measured on a full run, 2026-08-09: 1,943 of 3,858 newly added rows carried found_date 2026-07-10,
precisely MAX_AGE_DAYS ago. Half of everything a scrape added was Workday postings of unknown
(possibly enormous) age wearing a date that just cleared the freshness gate. That is what "the
jobs I'm getting are thirty, forty days old" actually was.

No database and no network — pure date arithmetic.
"""
import datetime
import sys

import scraper

MAX = scraper.MAX_AGE_DAYS
CUTOFF = (datetime.date.today() - datetime.timedelta(days=MAX)).isoformat()


def _age(posted_on):
    d = scraper._workday_date(posted_on)[:10]
    return (datetime.date.today() - datetime.date.fromisoformat(d)).days


def _dropped(posted_on):
    """What main()'s age gate would decide: `posted and posted < age_cutoff`."""
    d = scraper._workday_date(posted_on)[:10]
    return bool(d) and d < CUTOFF


def test_plus_suffix_is_treated_as_older_than_the_bound():
    # The whole point: '30+' must not be recorded as exactly 30.
    assert _age("Posted 30+ Days Ago") == MAX + 1, _age("Posted 30+ Days Ago")
    assert _dropped("Posted 30+ Days Ago"), "'30+ Days Ago' must fail the freshness gate"


def test_plus_suffix_works_for_any_number():
    assert _age("Posted 45+ Days Ago") == 46
    assert _dropped("Posted 45+ Days Ago")


def test_exact_bound_without_plus_is_still_kept():
    # 'Posted 30 Days Ago' is a real, exact age. It is not OVER the limit, so it stays --
    # the fix must not quietly tighten MAX_AGE_DAYS by one day for everyone.
    assert _age("Posted 30 Days Ago") == MAX
    assert not _dropped("Posted 30 Days Ago")


def test_fresh_forms_are_unchanged():
    assert _age("Posted Today") == 0
    assert _age("Posted Yesterday") == 1
    assert _age("Posted 2 Days Ago") == 2
    assert _age("Posted 5 Days Ago") == 5
    for s in ("Posted Today", "Posted Yesterday", "Posted 2 Days Ago", "Posted 29 Days Ago"):
        assert not _dropped(s), s


def test_missing_or_junk_falls_back_to_today():
    # Pre-existing behaviour, kept deliberately: an absent date must not read as ancient.
    for s in ("", None, "Posted", "no idea"):
        assert _age(s) == 0, s


def test_output_shape_is_stable():
    # verify_dates._is_clean_api_date() keys off the length: 'YYYY-MM-DD HH:MM' is 16 chars and
    # therefore NOT a trusted clean date, which is what keeps these rows in the verification
    # queue so a real posting date can replace the guess. Don't shorten this to 10.
    out = scraper._workday_date("Posted 3 Days Ago")
    assert len(out) == 16 and out[10] == " ", out


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  -", fn.__name__)
    print("\nAll %d Workday-date checks passed (MAX_AGE_DAYS=%d, cutoff=%s)."
          % (len(fns), MAX, CUTOFF))
