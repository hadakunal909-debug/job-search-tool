"""
test_date_sources.py — guards the two date fixes that emptied most of the verify backlog.

Both were free: the data was already in hand and being discarded.

  * Amazon publishes a real posting date and the scraper reformatted it to "YYYY-MM-DD HH:MM",
    which is this codebase's marker for a GUESS. 1,315 rows — 18% of the whole verification
    queue — were being sent to a rate-limited lookup that could only confirm what we had.
  * score_jobs already fetches Workday's CXS JSON for the description and threw away
    jobPostingInfo.startDate, while the stored date came from parsing "Posted 30+ Days Ago" off
    the list view. "30+" is a ceiling, not a measurement.

Run it directly
    python test_date_sources.py
or via pytest.

The shape assertions matter more than they look. verify_dates._is_clean_api_date() decides what
gets queued purely by string length, so a date's FORMAT is a load-bearing signal here, not
cosmetics. test_workday_date.py guards the other side of that contract.
"""
import datetime

import scraper
from scraper import score_jobs, verify_dates

beats = score_jobs._date_beats_stored
clean = verify_dates._is_clean_api_date


# --- the shape contract the two sides agree on ----------------------------------------
def test_bare_iso_is_trusted_and_a_timestamped_date_is_not():
    assert clean("2026-08-07")
    assert not clean("2026-08-07 00:00")
    assert not clean("2026-08-07 14:31")
    assert not clean("")


# --- Amazon: a real publisher date must not be disguised as a guess -------------------
def test_amazon_date_is_stored_as_a_trusted_bare_iso():
    # Exactly what scrape_amazon does with Amazon's posted_date field.
    got = datetime.datetime.strptime("June 13, 2026", "%B %d, %Y").strftime("%Y-%m-%d")
    assert got == "2026-06-13"
    assert clean(got), "Amazon's own posting date must not be queued for verification"
    assert len(got) == 10


def test_the_old_amazon_format_would_have_been_queued():
    # The regression this fixes: identical date, one strftime apart, and the whole difference
    # between "trusted" and "18% of the backlog".
    old = datetime.datetime.strptime("June 13, 2026", "%B %d, %Y").strftime("%Y-%m-%d %H:%M")
    assert old == "2026-06-13 00:00"
    assert not clean(old)


# --- MUST NOT REGRESS: Workday's derived shape stays untrusted ------------------------
def test_workday_list_date_still_reads_as_derived():
    # _workday_date reads the LIST view's relative text and is deliberately 16 chars so these
    # rows stay queued. score_jobs corrects them later from the detail page; the two stages are
    # supposed to disagree. test_workday_date.py locks the same contract from the other side.
    d = scraper._workday_date("Posted 3 Days Ago")
    assert len(d) == 16 and d[10] == " "
    assert not clean(d)


# --- the gate: what may replace what --------------------------------------------------
def test_a_stated_date_replaces_a_blank_or_a_guess():
    assert beats("2026-08-01", "")                      # nothing stored
    assert beats("2026-08-01", None)
    assert beats("2026-08-01", "   ")
    assert beats("2026-08-01", "2026-07-09 00:00")      # the scrape stamp
    assert beats("2026-08-01", scraper._workday_date("Posted 30+ Days Ago"))


# --- MUST NOT MERGE-STYLE GUARD: a publisher date is not ours to churn ----------------
def test_a_bare_iso_date_is_left_alone():
    # Greenhouse first_published, Adzuna created, Lever createdAt all land as bare ISO. Even a
    # differing detail-page date must not overwrite them here — scripts/audit_dates.py is where
    # those get questioned, deliberately and with evidence.
    assert not beats("2026-08-01", "2026-07-04")
    assert not beats("2026-08-01", "2026-08-01")


def test_an_empty_new_date_never_overwrites_anything():
    for stored in ("", "2026-07-04", "2026-07-09 00:00", None):
        assert not beats("", stored)
        assert not beats(None, stored)


# --- the Workday plumbing -------------------------------------------------------------
def test_wd_detail_jd_returns_a_pair_and_detail_jd_forwards_it():
    import inspect
    src = inspect.getsource(score_jobs.wd_detail_jd)
    assert 'info.get("startDate")' in src, "the free date must come off startDate"
    assert 'return "", ""' in src, "the failure path must keep the (jd, date) shape"
    # detail_jd's contract is (url, jd, date); the workday branch used to drop the date.
    assert "jd, date = wd_detail_jd(url)" in inspect.getsource(score_jobs.detail_jd)


def test_startdate_is_run_through_the_shared_parser():
    # So a tenant returning "2026-07-31T00:00:00" or "Jul 31, 2026" still yields a bare ISO
    # date rather than something _is_clean_api_date would reject.
    assert score_jobs._parse_date_any("2026-07-31") == "2026-07-31"
    assert score_jobs._parse_date_any("2026-07-31T00:00:00.000Z") == "2026-07-31"
    assert score_jobs._parse_date_any("") == ""
    assert clean(score_jobs._parse_date_any("2026-07-31"))


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  -", fn.__name__)
    print("\nAll %d date-source checks passed." % len(fns))
