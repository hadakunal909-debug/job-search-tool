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


# --- what the feed's "Confirmed posting date" filter actually means --------------------
def test_trusted_means_somebody_stated_it():
    import core
    assert core.is_trusted_date("2026-08-07")                      # publisher field
    assert core.is_trusted_date("", "2026-08-07")                  # service confirmed it
    assert core.is_trusted_date("2026-07-09 00:00", "2026-08-07")  # verified beats a guess
    assert not core.is_trusted_date("2026-07-09 00:00")            # the scrape stamp
    assert not core.is_trusted_date(scraper._workday_date("Posted 30+ Days Ago"))
    assert not core.is_trusted_date("")                            # aged by first_seen only
    assert not core.is_trusted_date(None)


def test_trusted_agrees_with_what_verify_dates_queues():
    # The two read the same string-shape contract from opposite ends: a row is queued for
    # verification precisely when nobody has stated its date. If these ever disagree, the feed
    # would call a date confirmed while the verifier still considered it a guess.
    import core
    for s in ("2026-08-07", "2026-08-07 00:00", "2026-08-07 14:31", "", "  ", "nonsense"):
        assert core.is_trusted_date(s) == clean(s), s


def test_verifiedonly_is_a_real_pref_and_defaults_off():
    import core
    assert core.DEFAULT_PREFS["verifiedonly"] is False
    assert core.normalize_prefs({"verifiedonly": "1"})["verifiedonly"] is True
    assert core.normalize_prefs({"verifiedonly": "junk"})["verifiedonly"] is False
    # FEED ONLY. prefs_match drives the email, and every digest candidate is a job we just
    # found — applying this there would empty the digest rather than filter it.
    import inspect
    assert "verifiedonly" not in inspect.getsource(core.prefs_match)


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


# --- MUST NOT WRITE: a date from a site the service admits it cannot read --------------
def test_an_unsupported_response_yields_no_date_and_no_confidence():
    """The service returns supported=false alongside a confident-looking date, and the caller
    accepts on confidence alone. Two different iCIMS postings at one employer both came back
    2023-06-29 / conf=high / supported=false — one date per EMPLOYER is page furniture, and
    writing it would record a three-year-old lie as a verified posting date."""
    class _Resp:
        status_code = 200

        def __init__(self, payload):
            self._p = payload

        def json(self):
            return self._p

    class _Sess:
        def __init__(self, payload):
            self._p = payload

        def get(self, *a, **k):
            return _Resp(self._p)

    real = verify_dates._session
    try:
        verify_dates._session = lambda: _Sess(
            {"most_probable_date": "2023-06-29", "confidence": "high", "supported": False})
        date, conf, note = verify_dates.check("https://careers-x.icims.com/jobs/1")
        assert date == "", "an unsupported site's date must never be written"
        assert conf == "", "and it must not be recorded as a confident answer"
        assert note == "unsupported"
        # ...and the acceptance rule the caller applies would now reject it.
        assert not (date and conf in verify_dates.MIN_CONFIDENCE)

        # A supported answer still comes through untouched.
        verify_dates._session = lambda: _Sess(
            {"most_probable_date": "2026-07-16", "confidence": "medium", "supported": True})
        date, conf, note = verify_dates.check("https://x.example/jobs/1")
        assert (date, conf) == ("2026-07-16", "medium")
        assert date and conf in verify_dates.MIN_CONFIDENCE
    finally:
        verify_dates._session = real


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  -", fn.__name__)
    print("\nAll %d date-source checks passed." % len(fns))
