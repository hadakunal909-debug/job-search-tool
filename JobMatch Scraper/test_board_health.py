"""
test_board_health.py -- what the board health report is allowed to call a failure.

No network and no database: db.get_kv/put_kv are replaced with a dict, and the one scrape_all
case that runs here is the case where the budget is already spent, which never opens a socket.
    python test_board_health.py

WHY THIS EXISTS. scrape_all reports a board it never STARTED the same way it reports a board that
raised -- rows is None for both, and it has to be, because reconcile_closed must refuse to retire
postings from a board it could not read. save_board_health then filed that shared `ok=False` as an
outcome, so a starved board arrived in the triage list wearing a failure's clothes. Measured on the
live blob for the 2026-08-21 17:58 run: 569 boards presented as failing, of which 566 had simply
never been fetched and 3 had actually raised. The signal was there the whole time; it was buried
566 deep. These checks pin the distinction at both ends -- where it is produced and where it is
reported.
"""
import contextlib
import io

import scraper
import db


def _quiet(fn, *a, **k):
    """Run fn with stdout captured; returns (result, what it printed).

    Not only tidiness. scripts/run_tests.py marks a suite failed when its OUTPUT contains a FAIL
    line, and both functions under test print exactly that by design -- so a suite that lets
    their reports through fails while every assertion in it passes.
    """
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = fn(*a, **k)
    return r, buf.getvalue()


def _store():
    """Point db's kv at a fresh dict and hand it back."""
    kv = {}
    db.get_kv = lambda k, default=None: kv.get(k, {} if default is None else default)
    db.put_kv = lambda k, v: kv.__setitem__(k, v)
    return kv


def _result(company, ok=True, skipped=False, err=None, secs=1.0, n=3, ats="greenhouse"):
    return {"entry": ("u/" + company, ats, company), "company": company, "ok": ok,
            "skipped": skipped, "err": err, "secs": secs,
            "urls": {"%s-%d" % (company, i) for i in range(n)}}


def _boards(kv):
    return kv["board_health"]["boards"]


# --- the producer: scrape_all has to be able to tell the two apart ---------------------------

def test_scrape_all_marks_an_unreached_board_skipped_not_failed():
    """A budget that is already spent must produce skipped=True with no error attached."""
    got = []
    sources = [("u/a", "greenhouse", "A"), ("u/b", "ashby", "B")]
    # A positive-but-vanishing budget puts the deadline in the past, which is the branch that
    # returns without touching the network. budget=0 would DISABLE the deadline instead.
    _quiet(scraper.scrape_all, sources, workers=2, board_results=got, budget_min=1e-9)
    assert len(got) == 2, got
    for br in got:
        assert br["skipped"] is True, br
        assert br["err"] is None, br
        assert br["ok"] is False, br          # unchanged: reconcile_closed depends on this
        assert br["secs"] is None, br


def test_scrape_all_marks_a_raising_board_failed_not_skipped():
    def boom(url):
        raise RuntimeError("404 Client Error: Not Found")
    scraper.SCRAPERS["_test_boom"] = boom
    try:
        got = []
        _quiet(scraper.scrape_all, [("u/x", "_test_boom", "X")], workers=1, board_results=got,
                           budget_min=5)
        assert len(got) == 1, got
        assert got[0]["skipped"] is False, got
        assert "404" in (got[0]["err"] or ""), got
        assert got[0]["ok"] is False, got
    finally:
        scraper.SCRAPERS.pop("_test_boom", None)


def test_an_unknown_ats_type_is_a_failure_not_a_skip():
    """A source naming an adapter that does not exist is a config error, and the report has to
    say so -- it is the one case where nothing was fetched and something is still wrong."""
    got = []
    _quiet(scraper.scrape_all, [("u/y", "_test_no_such_ats", "Y")], workers=1, board_results=got,
                       budget_min=5)
    assert got[0]["skipped"] is False, got
    assert "unknown ats_type" in (got[0]["err"] or ""), got


# --- the reporter: what lands in the blob ----------------------------------------------------

def test_a_starved_board_is_not_in_the_failing_list():
    kv = _store()
    _, printed = _quiet(scraper.save_board_health, [
        _result("Healthy"),
        _result("Starved", ok=False, skipped=True, secs=None, n=0, ats="workday"),
        _result("Broken", ok=False, err="404 Client Error: Not Found", secs=0.4, n=0,
                ats="ashby"),
    ])
    b = _boards(kv)
    # Through the shared predicate, because that is what both readers actually call.
    failing = sorted(r["company"] for r in b.values()
                     if r["runs"] and scraper.board_run_failed(r["runs"][-1]))
    assert failing == ["Broken"], failing
    # And the report itself: the starved board must not appear on a FAILED line, and must be
    # accounted for somewhere rather than dropped.
    failed_lines = [ln for ln in printed.splitlines() if ln.strip().startswith("FAILED")]
    assert len(failed_lines) == 1 and "Broken" in failed_lines[0], failed_lines
    assert "Starved" not in "".join(failed_lines), failed_lines
    assert "STARVED 1 board(s)" in printed, printed


def test_a_real_failure_keeps_the_reason():
    kv = _store()
    _quiet(scraper.save_board_health,
           [_result("Broken", ok=False, err="403 Forbidden", secs=0.4, n=0)])
    assert _boards(kv)["u/Broken"]["runs"][-1]["err"] == "403 Forbidden"


def test_a_skip_consumes_no_history_slot():
    """The window is eight runs deep. Spending those slots on non-events is what stopped the
    quiet-board check from seeing three real zero-fetches in a row."""
    kv = _store()
    starved = _result("Starved", ok=False, skipped=True, secs=None, n=0)
    for _ in range(scraper.BOARD_HEALTH_RUNS + 4):
        _quiet(scraper.save_board_health, [starved])
    rec = _boards(kv)["u/Starved"]
    assert rec["runs"] == [], rec["runs"]
    assert rec["skips"] == scraper.BOARD_HEALTH_RUNS + 4, rec["skips"]


def test_a_fetch_resets_the_starvation_streak():
    kv = _store()
    starved = _result("Board", ok=False, skipped=True, secs=None, n=0)
    _quiet(scraper.save_board_health, [starved])
    _quiet(scraper.save_board_health, [starved])
    assert _boards(kv)["u/Board"]["skips"] == 2
    _quiet(scraper.save_board_health, [_result("Board", n=2)])
    rec = _boards(kv)["u/Board"]
    assert rec["skips"] == 0, rec["skips"]
    assert len(rec["runs"]) == 1 and rec["runs"][-1]["n"] == 2, rec["runs"]


def test_skips_do_not_fake_a_quiet_board():
    """Three skips around one good fetch must not read as three clean zero-fetches -- that is
    the check that decides which boards are candidates to retire."""
    kv = _store()
    skip = _result("Board", ok=False, skipped=True, secs=None, n=0)
    _quiet(scraper.save_board_health, [_result("Board", n=5)])
    for _ in range(3):
        _quiet(scraper.save_board_health, [skip])
    rec = _boards(kv)["u/Board"]
    quiet = (len(rec["runs"]) >= 3
             and all(x["n"] == 0 and x["ok"] for x in rec["runs"][-3:]))
    assert not quiet, rec["runs"]
    assert [x["n"] for x in rec["runs"]] == [5], rec["runs"]


def test_the_history_window_still_holds_at_eight():
    kv = _store()
    for i in range(scraper.BOARD_HEALTH_RUNS + 5):
        _quiet(scraper.save_board_health, [_result("Board", n=i)])
    runs = _boards(kv)["u/Board"]["runs"]
    assert len(runs) == scraper.BOARD_HEALTH_RUNS, len(runs)
    assert [x["n"] for x in runs] == list(range(5, scraper.BOARD_HEALTH_RUNS + 5)), runs


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  -", fn.__name__)
    print("")
    print("All %d board-health checks passed." % len(fns))
