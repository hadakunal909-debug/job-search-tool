"""
test_jd_persist.py — the JD a run fetched must end up in the DATABASE, not just on disk.

Run it directly
    python test_jd_persist.py
or via pytest (functions are named test_*).

THE BUG THIS GUARDS. There are two records of "we have this job's description": the on-disk
jd_cache.json.gz and the `jd` column the website renders from. score_jobs used to decide what
still needed fetching from the CACHE alone (heavy pass) or from the DATABASE alone (new-only
pass), and nothing reconciled the two. A row whose description was fetched, banked to disk, and
then not written to the database was excluded from the fetch queue by the cache and from the
site by the empty column — with no path back, because _persist_jds only ever writes text
fetched in the current run. It read "description pending" on the site forever.

Measured on the live corpus 2026-08-16, before the fix: 1,827 of the 3,816 rows showing as
pending already had a full description sitting in the local cache — all 79 lululemon postings
and 1,186 of Amazon's among them.

These tests drive scraper.score_jobs.main() with the network and the database stubbed, so they
assert on what was WRITTEN rather than on what was printed.
"""
import gzip
import json
import os
import sys
import tempfile

import scraper.score_jobs as sj


class _FakeDB(object):
    """Just enough of db.py for the JD phase: a url -> row table with a jd column."""

    COLS_SCORE = "url,found_date,location,first_seen"
    JOBS_DERIVED_SQL = ""

    def __init__(self, rows):
        self.rows = {r["url"]: dict(r) for r in rows}
        self.jd_writes = []                       # every url passed to update_jds, in order
        self.kv = {}                              # put_kv/get_kv blobs, incl. the thin ledger
        self.score_calls = []                     # one entry per update_scores call: its urls

    # --- reads -------------------------------------------------------------------
    def load_jobs(self, include_jd=True, cols=None):
        return [dict(r) for r in self.rows.values()]

    def load_jobs_by_urls(self, urls, include_jd=True):
        return [dict(self.rows[u]) for u in urls if u in self.rows]

    def urls_missing_jd(self):
        return {u for u, r in self.rows.items() if not (r.get("jd") or "").strip()}

    # --- writes ------------------------------------------------------------------
    def update_jds(self, jds):
        for u, jd in jds.items():
            self.jd_writes.append(u)
            if u in self.rows:
                self.rows[u]["jd"] = jd

    def update_scores(self, scores):
        self.score_calls.append(sorted(scores))
        for u, s in scores.items():
            if u in self.rows:
                self.rows[u]["match_score"] = s

    def update_job_fields(self, rows):
        pass

    # --- run bookkeeping main() touches but these tests don't assert on ------------
    # The thin-JD retry ledger lives in this KV row (db.get_kv/put_kv over scrape_status).
    def get_kv(self, key, default=None):
        return dict(self.kv.get(key) or ({} if default is None else default))

    def put_kv(self, key, obj):
        self.kv[key] = dict(obj or {})

    def get_scrape_status(self):
        return {}

    def set_scrape_status(self, *a, **kw):
        pass

    def backend_name(self):
        return "fake"

    def using_supabase(self):
        return True


def _write_resume(tmp):
    """main() aborts outright without a resume.txt in the working directory ("scores would all
    be 0"), so every run here needs one. Its content is irrelevant to what these tests assert."""
    with open(os.path.join(tmp, "resume.txt"), "w", encoding="utf-8") as fh:
        fh.write("Program manager. Roadmap, stakeholder management, risk, budget, delivery.\n")


def _run_main(rows, cache, argv):
    """Run score_jobs.main() over `rows` with `cache` on disk. Returns the fake db.

    Every network path is poisoned rather than mocked-out-to-empty: a fetch attempted during
    these tests is itself the failure, because the whole point is that cached text is reused.
    """
    fake = _FakeDB(rows)
    tmp = tempfile.mkdtemp()
    cache_path = os.path.join(tmp, "jd_cache.json.gz")
    with gzip.open(cache_path, "wt", encoding="utf-8") as fh:
        json.dump(cache, fh)
    _write_resume(tmp)

    saved = {k: getattr(sj, k) for k in
             ("db", "JD_CACHE_FILE", "detail_jd", "jd_map_for", "NEW_JOBS_FILE")}
    saved_core = {k: getattr(sj.core, k) for k in ("load_idf", "save_jdmeta", "load_jdmeta")}
    saved_argv = sys.argv[:]
    saved_cwd = os.getcwd()

    def _no_network(*a, **kw):
        raise AssertionError("network fetch attempted for %r — the cache should have covered it"
                             % (a[:1],))

    try:
        sj.db = fake
        sj.JD_CACHE_FILE = cache_path
        sj.NEW_JOBS_FILE = os.path.join(tmp, "no_such_new_jobs.json")
        sj.detail_jd = _no_network
        sj.jd_map_for = _no_network
        sj.core.load_idf = lambda *a, **kw: {}
        sj.core.load_jdmeta = lambda *a, **kw: {}
        sj.core.save_jdmeta = lambda *a, **kw: None
        sys.argv = ["score_jobs"] + argv
        os.chdir(tmp)                     # keep idf.json / jdmeta.json out of the repo
        sj.main()
    finally:
        for k, v in saved.items():
            setattr(sj, k, v)
        for k, v in saved_core.items():
            setattr(sj.core, k, v)
        sys.argv = saved_argv
        os.chdir(saved_cwd)
    return fake


_JD = ("We are seeking a Program Manager to own delivery across teams. Responsibilities "
       "include roadmap, stakeholder management, risk and budget tracking. " * 6)


def _rows():
    return [
        # Has a JD in BOTH stores — must not be touched.
        {"url": "https://ex.com/a", "jd": _JD, "location": "Boston, MA", "found_date": "2026-08-01",
         "first_seen": "2026-08-01", "title": "Program Manager", "company": "Ex"},
        # The bug: cached on disk, EMPTY in the database.
        {"url": "https://ex.com/b", "jd": "", "location": "Boston, MA", "found_date": "2026-08-16",
         "first_seen": "2026-08-16", "title": "Program Manager", "company": "Ex"},
    ]


def _assert_repaired(fake):
    assert fake.rows["https://ex.com/b"]["jd"] == _JD, \
        "the cached description never reached the database — this is the pending-JD bug"
    assert "https://ex.com/a" not in fake.jd_writes, \
        "rewrote a description the database already had"


def test_heavy_pass_writes_cached_jd_the_database_is_missing():
    _assert_repaired(_run_main(_rows(), {"https://ex.com/a": _JD, "https://ex.com/b": _JD}, []))


def test_new_only_pass_reuses_the_cache_instead_of_refetching():
    # --new-only takes its backlog from the DATABASE, so before the fix it re-DOWNLOADED every
    # one of these rows on every run — spending the whole fetch budget on text already on disk.
    # _run_main raises if any fetch is attempted, so reaching the assert IS the test.
    _assert_repaired(_run_main(_rows(), {"https://ex.com/a": _JD, "https://ex.com/b": _JD},
                               ["--new-only"]))


def test_uncached_row_is_left_for_the_fetch_phase():
    """A row missing from BOTH stores must still be queued, not quietly considered handled."""
    cwd = os.getcwd()
    fetched = []
    try:
        fake = _FakeDB(_rows())
        tmp = tempfile.mkdtemp()
        cache_path = os.path.join(tmp, "jd_cache.json.gz")
        with gzip.open(cache_path, "wt", encoding="utf-8") as fh:
            json.dump({"https://ex.com/a": _JD}, fh)          # 'b' is in NEITHER store
        _write_resume(tmp)

        saved = {k: getattr(sj, k) for k in
                 ("db", "JD_CACHE_FILE", "detail_jd", "jd_map_for", "NEW_JOBS_FILE")}
        saved_core = {k: getattr(sj.core, k) for k in ("load_idf", "save_jdmeta", "load_jdmeta")}
        saved_argv = sys.argv[:]
        try:
            sj.db = fake
            sj.JD_CACHE_FILE = cache_path
            sj.NEW_JOBS_FILE = os.path.join(tmp, "none.json")
            sj.jd_map_for = lambda *a, **kw: {}

            def _detail(u):
                fetched.append(u)
                return u, _JD, ""
            sj.detail_jd = _detail
            sj.core.load_idf = lambda *a, **kw: {}
            sj.core.load_jdmeta = lambda *a, **kw: {}
            sj.core.save_jdmeta = lambda *a, **kw: None
            sys.argv = ["score_jobs"]
            os.chdir(tmp)
            sj.main()
        finally:
            for k, v in saved.items():
                setattr(sj, k, v)
            for k, v in saved_core.items():
                setattr(sj.core, k, v)
            sys.argv = saved_argv

        assert fetched == ["https://ex.com/b"], fetched
        assert fake.rows["https://ex.com/b"]["jd"] == _JD
    finally:
        os.chdir(cwd)


# ---------------------------------------------------------------------------------------
# THE SECOND CLASS: a row that HOLDS a description which is really a loading shell.
#
# It scores 0 and reads blank in the feed exactly like an empty one, but the fetch queue is
# built from rows whose `jd` is EMPTY — so a junk value used to be STICKY and nothing ever
# tried again. 1,533 rows (7.0% of the corpus) were in that state on 2026-08-17. The retry is
# deliberately BOUNDED: most of those rows sit on hosts that genuinely cannot be read, and
# retrying all of them every run would spend the entire fetch budget re-failing.
# ---------------------------------------------------------------------------------------

_SHELL = "Loading \u00d7 Sorry to interrupt CSS Error Refresh"          # 46 chars, verbatim


def _thin_rows(n=1, host="shell.com"):
    return [{"url": "https://%s/%d" % (host, i), "jd": _SHELL, "location": "Boston, MA",
             "found_date": "2026-08-16", "first_seen": "2026-08-16",
             "title": "Program Manager", "company": "Shell Co"} for i in range(n)]


def _run_with_fetch(rows, cache, argv, detail, ledger=None, jd_map=None):
    """Run main() with a CONTROLLED detail_jd, and return (fake db, urls it attempted).

    Separate from _run_main because that one poisons every network path. Here the point is
    which urls the queue offered, so the fetcher records them.
    """
    fake = _FakeDB(rows)
    if ledger is not None:
        fake.kv[sj.THIN_LEDGER_KEY] = dict(ledger)
    tmp = tempfile.mkdtemp()
    cache_path = os.path.join(tmp, "jd_cache.json.gz")
    with gzip.open(cache_path, "wt", encoding="utf-8") as fh:
        json.dump(cache, fh)
    _write_resume(tmp)
    tried = []

    saved = {k: getattr(sj, k) for k in
             ("db", "JD_CACHE_FILE", "detail_jd", "jd_map_for", "NEW_JOBS_FILE")}
    saved_core = {k: getattr(sj.core, k) for k in ("load_idf", "save_jdmeta", "load_jdmeta")}
    saved_argv, saved_cwd = sys.argv[:], os.getcwd()
    try:
        sj.db = fake
        sj.JD_CACHE_FILE = cache_path
        sj.NEW_JOBS_FILE = os.path.join(tmp, "none.json")
        sj.jd_map_for = jd_map if jd_map is not None else (lambda *a, **kw: {})

        def _detail(u):
            tried.append(u)
            return u, detail(u), ""
        sj.detail_jd = _detail
        sj.core.load_idf = lambda *a, **kw: {}
        sj.core.load_jdmeta = lambda *a, **kw: {}
        sj.core.save_jdmeta = lambda *a, **kw: None
        sys.argv = ["score_jobs"] + argv
        os.chdir(tmp)
        sj.main()
    finally:
        for k, v in saved.items():
            setattr(sj, k, v)
        for k, v in saved_core.items():
            setattr(sj.core, k, v)
        sys.argv = saved_argv
        os.chdir(saved_cwd)
    return fake, tried


def test_a_thin_row_is_probed_when_its_host_is_due():
    rows = _thin_rows(1)
    u = rows[0]["url"]
    fake, tried = _run_with_fetch(rows, {u: _SHELL}, [], lambda _u: _JD)
    assert tried == [u], tried
    assert fake.rows[u]["jd"] == _JD, "a repaired description never reached the database"
    led = fake.kv[sj.THIN_LEDGER_KEY]["hosts"]["shell.com"]
    assert led["f"] == 0 and led["ok"] == 1, led


def test_a_backed_off_host_is_not_probed_at_all():
    """The waste this whole mechanism exists to prevent. The fetcher raises, so REACHING the
    end of this test is the assertion."""
    rows = _thin_rows(3)
    cache = {r["url"]: _SHELL for r in rows}
    led = {"rev": sj._extractor_rev(),
           "hosts": {"shell.com": {"f": 4, "next": "2099-01-01", "ok": 0}}}

    def _boom(u):
        raise AssertionError("probed %s while its host was backed off" % u)

    _run_with_fetch(rows, cache, [], _boom, ledger=led)


def test_shipping_an_extractor_re_opens_a_backed_off_host():
    """A stale fingerprint must make every host due again — that is what stops a working new
    extractor sitting unused behind a 64-day backoff. The failure count is NOT reset, so a
    comment-only edit costs one probe round rather than restarting the whole ladder."""
    rows = _thin_rows(2)
    cache = {r["url"]: _SHELL for r in rows}
    led = {"rev": "an-older-build", "hosts": {"shell.com": {"f": 4, "next": "2099-01-01"}}}
    fake, tried = _run_with_fetch(rows, cache, [], lambda _u: "", ledger=led)
    assert tried, "a changed extractor fingerprint did not re-open the host"
    assert fake.kv[sj.THIN_LEDGER_KEY]["hosts"]["shell.com"]["f"] == 5, "failure count restarted"


def test_the_probe_is_capped_per_host():
    rows = _thin_rows(40)
    cache = {r["url"]: _SHELL for r in rows}
    fake, tried = _run_with_fetch(rows, cache, [], lambda _u: "")
    assert len(tried) == sj.THIN_PROBE_PER_HOST, \
        "%d urls probed on one due host; the cap is %d" % (len(tried), sj.THIN_PROBE_PER_HOST)


def test_a_still_thin_refetch_never_overwrites_the_stored_description():
    """Re-reading the same 46-character shell must not count as a repair. This is the gain rule:
    without it the ledger would mark the host hot and drain 400 rows of nothing."""
    rows = _thin_rows(1)
    u = rows[0]["url"]
    fake, tried = _run_with_fetch(rows, {u: _SHELL}, [], lambda _u: _SHELL)
    assert tried == [u]
    assert fake.rows[u]["jd"] == _SHELL
    assert u not in fake.jd_writes, "wrote a shell back over a shell"
    assert fake.kv[sj.THIN_LEDGER_KEY]["hosts"]["shell.com"]["f"] == 1


def test_a_hot_host_drains_its_backlog_on_the_next_run():
    rows = _thin_rows(50)
    cache = {r["url"]: _SHELL for r in rows}
    led = {"rev": sj._extractor_rev(),
           "hosts": {"shell.com": {"f": 0, "ok": 1, "next": "2000-01-01"}}}
    fake, tried = _run_with_fetch(rows, cache, [], lambda _u: _JD, ledger=led)
    assert len(tried) == 50, "a host that worked last run should drain, got %d" % len(tried)


def test_a_lost_ledger_still_bounds_the_run():
    """CI is stateless and the KV table may be absent, so the cap has to hold with NO ledger."""
    rows = _thin_rows(30, host="h1.com") + _thin_rows(30, host="h2.com")
    cache = {r["url"]: _SHELL for r in rows}
    fake, tried = _run_with_fetch(rows, cache, [], lambda _u: "")
    assert len(tried) <= sj.THIN_PROBE_MAX, len(tried)
    assert len(tried) == 2 * sj.THIN_PROBE_PER_HOST, tried


def test_a_thin_probe_never_displaces_a_row_with_no_description():
    """The empty-jd queue is the priority; probes are appended to its tail."""
    rows = _thin_rows(5) + [{"url": "https://ex.com/empty", "jd": "", "location": "Boston, MA",
                             "found_date": "2026-08-16", "first_seen": "2026-08-16",
                             "title": "Program Manager", "company": "Ex"}]
    cache = {r["url"]: _SHELL for r in rows if r["jd"]}
    fake, tried = _run_with_fetch(rows, cache, [], lambda _u: _JD)
    assert "https://ex.com/empty" in tried, "an empty-jd row was dropped from the queue"
    assert fake.rows["https://ex.com/empty"]["jd"] == _JD


def test_a_db_repaired_row_heals_the_stale_cache_without_a_fetch():
    """The divergence this retry would otherwise re-open: the column is good, the cache still
    holds the shell, and the scorer reads the CACHE. Must heal with no network at all."""
    rows = _thin_rows(1)
    u = rows[0]["url"]
    rows[0]["jd"] = _JD                                   # database already repaired

    def _boom(_u):
        raise AssertionError("fetched %s when the database already held its description" % _u)

    fake, tried = _run_with_fetch(rows, {u: _SHELL}, [], _boom)
    assert not tried, "fetched a row the database had already repaired"
    assert fake.rows[u]["jd"] == _JD


def test_new_only_does_no_thin_probing():
    """The cheap pass runs more often on a smaller budget; the thin backlog is the heavy pass's
    problem. Poisoned fetcher again, so reaching the end is the assertion."""
    rows = _thin_rows(3)
    cache = {r["url"]: _SHELL for r in rows}

    def _boom(u):
        raise AssertionError("--new-only probed a thin row: %s" % u)

    _run_with_fetch(rows, cache, ["--new-only"], _boom)


# ---------------------------------------------------------------------------------------
# THE THIRD CLASS: the description was fetched, and then dropped on the floor.
#
# Every branch of jd_map_for BUILDS the job URL it returns; the jobs table stores
# scraper.canonical_url() of that URL. Nothing forced the two to agree. JobDiva's branch emitted
# "/portal/?a=<token>#/jobs/<id>" while the row is stored as "/portal?a=<token>#/jobs/<id>" —
# canonical_url strips the trailing slash — so all 352 of its descriptions were downloaded on
# every single run and discarded on a key miss, leaving the rows on a 63-character shell.
#
# Failure mode worth naming: SILENT. Both stores look internally consistent, the run reports
# success, and the only symptom is a row that never improves.
# ---------------------------------------------------------------------------------------


def test_canonical_keys_covers_the_two_shapes_that_drift():
    """The rule that bit both branches is one rule — a path ending in "/" before the query."""
    import scraper
    for built in ("https://www1.jobdiva.com/portal/?a=TOK#/jobs/29051170",
                  "https://apply.actalentservices.com/v1/s/?opco=ENS&params=AAA"):
        stored = scraper.canonical_url(built)
        assert stored != built, "%s no longer drifts; this test has lost its subject" % built
        m = sj._canonical_keys({built: "DESCRIPTION"})
        assert m.get(stored) == "DESCRIPTION", \
            "a bulk map keyed %r would never match the stored row %r" % (built, stored)


def test_every_bulk_branch_url_shape_is_canonical_stable_or_handled():
    """A map keyed by ANY branch's URL shape must reach the stored row. Six of the eight are
    canonical-stable by construction; the two that are not go through the same boundary."""
    import scraper
    shapes = ["https://job-boards.greenhouse.io/acme/jobs/4567",
              "https://jobs.lever.co/acme/2f0c1e3d-1111-2222-3333-444455556666",
              "https://jobs.ashbyhq.com/acme/1a2b3c4d",
              "https://www.amazon.jobs/en/jobs/10387919/business-analyst-wwgs",
              "https://acme.pinpointhq.com/en/postings/1a2b",
              "https://careers-acme.icims.com/jobs/1234/x/job",
              "https://www1.jobdiva.com/portal/?a=TOK#/jobs/29051170",
              "https://apply.actalentservices.com/v1/s/?opco=ENS&params=AAA"]
    m = sj._canonical_keys({u: "JD-" + str(i) for i, u in enumerate(shapes)})
    for i, u in enumerate(shapes):
        assert m.get(scraper.canonical_url(u)) == "JD-" + str(i), \
            "branch shape %r does not reach its stored row" % u


def test_the_jobdiva_branch_returns_canonically_keyed_descriptions():
    """The real code path, offline. This is the test that would have caught the 352-row bug: the
    branch is driven with the URL the DATABASE holds and must return a description for it."""
    import scraper
    token = "TOK"
    board = "https://www1.jobdiva.com/portal/?a=%s" % token
    stored = scraper.canonical_url("https://www1.jobdiva.com/portal/?a=%s#/jobs/999" % token)
    saved = {k: getattr(scraper, k) for k in
             ("_jobdiva_token", "_jobdiva_session", "_jobdiva_pages", "jobdiva_job_detail")}
    try:
        scraper._jobdiva_token = lambda u: token
        scraper._jobdiva_session = lambda t: {"portalID": "1", "token": "x", "a": t}
        # The feed's inline jobDescription is TRUNCATED at 400 chars + "..." (measured: 196 of
        # 200 rows exactly 403). The detail endpoint is what carries the real text.
        scraper._jobdiva_pages = lambda t, jh: iter([[{"id": 999,
                                                      "jobDescription": "T" * 403}]])
        scraper.jobdiva_job_detail = lambda jid, jh: _JD
        got = sj.jd_map_for(board, "jobdiva", {stored})
    finally:
        for k, v in saved.items():
            setattr(scraper, k, v)
    assert stored in got, \
        "jd_map_for returned %r; the database holds %r" % (sorted(got)[:2], stored)
    # _text() normalises whitespace, so compare through it rather than to the raw fixture.
    assert got[stored] == sj._text(_JD),         "returned %d chars; the feed teaser is 403 and the real description is %d"         % (len(got[stored]), len(sj._text(_JD)))


# --------------------------------------------------------------------------------------------
# The ANALYSIS budget. core.job_meta is ~206 ms/row, so a full pass over 25k rows is ~79 min --
# and it used to sit unbudgeted above a single db.update_scores(), inside a 14-minute CI step.
# Killed mid-loop it banked every JD it had fetched and not one score, so the daily heavy
# re-score could not complete and stored scores drifted three scoring commits behind the code.
# These drive main() with a fake clock, which is the only way to assert on a wall-clock budget
# without making the suite slow or flaky.


class _Clock(object):
    """A time.time() that advances `step` seconds per CALL, so "how many rows fit in the budget"
    is arithmetic rather than a race.

    Safe to swap in wholesale because every other time.time() in main() is gated behind the JD
    fetch deadline, and these tests leave SCORE_BUDGET_MIN unset (0) so none of them run."""

    def __init__(self, step):
        self.now, self.step = 1000.0, step

    def time(self):
        self.now += self.step
        return self.now

    def sleep(self, *a, **kw):
        pass


_BUDGET_ROWS = "abcdef"
"""Six rows, one per letter, first_seen descending from 'a' so newest-first order IS a..f."""


def _budget_rows(score=11):
    return [{"url": "https://ex.com/%s" % ch, "jd": _JD, "location": "Boston, MA",
             "found_date": "2026-08-0%d" % (6 - i), "first_seen": "2026-08-0%d" % (6 - i),
             "title": "Program Manager", "company": "Ex", "match_score": score}
            for i, ch in enumerate(_BUDGET_ROWS)]


def _run_budget(rows=None, budget="4", step=60, chunk=2, prior_meta=None, resume=None,
                kv=None, argv=None, fake=None):
    """main() over `rows` with a fake clock. Returns (fake db, list of jdmeta maps saved).

    budget=4 with step=60 scores exactly THREE rows: the deadline is set on the first clock call
    and each row costs one more, so row index 3 is the first to find the budget spent.

    `fake` takes a pre-built _FakeDB so a caller can wire a failing write before main() runs.
    """
    rows = _budget_rows() if rows is None else rows
    fake = _FakeDB(rows) if fake is None else fake
    fake.kv = dict(kv or {})
    tmp = tempfile.mkdtemp()
    cache_path = os.path.join(tmp, "jd_cache.json.gz")
    with gzip.open(cache_path, "wt", encoding="utf-8") as fh:
        json.dump({r["url"]: r["jd"] for r in rows}, fh)
    with open(os.path.join(tmp, "resume.txt"), "w", encoding="utf-8") as fh:
        fh.write(resume or "Program manager. Roadmap, stakeholder management, risk, budget.\n")

    saved = {k: getattr(sj, k) for k in
             ("db", "JD_CACHE_FILE", "detail_jd", "jd_map_for", "NEW_JOBS_FILE", "time",
              "ANALYZE_CHUNK")}
    saved_core = {k: getattr(sj.core, k) for k in ("load_idf", "save_jdmeta", "load_jdmeta")}
    saved_argv, saved_cwd = sys.argv[:], os.getcwd()
    # Hermetic: these are read at CALL time, so a value in the developer's shell would otherwise
    # decide what the test measures.
    env_keys = ("SCORE_NEW_ONLY", "SCORE_MAX_FETCH", "SCORE_BUDGET_MIN",
                "SCORE_ANALYZE_BUDGET_MIN", "SCORE_RESET_CURSOR")
    saved_env = {k: os.environ.get(k) for k in env_keys}
    metas = []

    def _no_network(*a, **kw):
        raise AssertionError("network fetch attempted — the cache covers every row here")

    try:
        for k in env_keys:
            os.environ.pop(k, None)
        if budget is not None:
            os.environ["SCORE_ANALYZE_BUDGET_MIN"] = str(budget)
        sj.db = fake
        sj.JD_CACHE_FILE = cache_path
        sj.NEW_JOBS_FILE = os.path.join(tmp, "no_such_new_jobs.json")
        sj.detail_jd = _no_network
        sj.jd_map_for = _no_network
        sj.time = _Clock(step)
        sj.ANALYZE_CHUNK = chunk
        sj.core.load_idf = lambda *a, **kw: {}
        sj.core.load_jdmeta = lambda *a, **kw: dict(prior_meta or {})
        sj.core.save_jdmeta = lambda m, *a, **kw: metas.append(dict(m))
        sys.argv = ["score_jobs"] + (argv or [])
        os.chdir(tmp)
        sj.main()
    finally:
        for k, v in saved.items():
            setattr(sj, k, v)
        for k, v in saved_core.items():
            setattr(sj.core, k, v)
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        sys.argv = saved_argv
        os.chdir(saved_cwd)
    return fake, metas


def _url(ch):
    return "https://ex.com/%s" % ch


def test_the_analysis_budget_stops_the_loop_and_banks_what_it_scored():
    fake, _ = _run_budget()
    scored = [ch for ch in _BUDGET_ROWS if fake.rows[_url(ch)]["match_score"] != 11]
    assert scored == ["a", "b", "c"], \
        "expected the 3 newest rows scored, got %r — a budget that cuts an arbitrary set is " \
        "the starvation bug the ordering exists to prevent" % (scored,)
    # The whole point: the work reached the database DURING the loop, not after it.
    assert len(fake.score_calls) >= 2, \
        "scores were written in %d call(s) — an unchunked write is lost entirely when the " \
        "process is killed, which is the defect being fixed" % len(fake.score_calls)


def test_rows_the_budget_never_reached_keep_their_previous_score():
    fake, _ = _run_budget()
    for ch in "def":
        assert fake.rows[_url(ch)]["match_score"] == 11, \
            "row %s was left at %r; an unreached row must keep the score it had, never be " \
            "zeroed or blanked" % (ch, fake.rows[_url(ch)]["match_score"])


def test_a_truncated_pass_records_where_to_resume():
    fake, _ = _run_budget()
    cur = fake.kv.get(sj.CURSOR_KEY) or {}
    assert cur.get("key"), "no cursor stored — the next run would re-score a..c and stall again"
    assert cur["key"][2] == _url("c"), \
        "cursor names %r; it must be the LAST row scored" % (cur["key"][2],)


def test_the_next_run_resumes_below_the_cursor_instead_of_restarting():
    first, _ = _run_budget()
    # Same rows, same résumé (so the same rev), carrying the first run's cursor forward. Scores
    # reset to 11 so "who did run two touch?" is unambiguous.
    second, _ = _run_budget(rows=_budget_rows(), kv=first.kv)
    scored = [ch for ch in _BUDGET_ROWS if second.rows[_url(ch)]["match_score"] != 11]
    assert scored == ["d", "e", "f"], \
        "run two scored %r — it must pick up below the cursor, or repeated truncation never " \
        "covers the corpus" % (scored,)
    assert not (second.kv.get(sj.CURSOR_KEY) or {}).get("key"), \
        "the cursor survived a pass that reached the end of the corpus; the next cycle would " \
        "skip the newest rows"


def test_a_completed_pass_stores_no_cursor():
    # No budget at all -- the manual-backfill path every existing caller gets.
    fake, _ = _run_budget(budget=None)
    scored = [ch for ch in _BUDGET_ROWS if fake.rows[_url(ch)]["match_score"] != 11]
    assert scored == list(_BUDGET_ROWS), "an unbudgeted pass must score everything, got %r" % (scored,)
    assert not (fake.kv.get(sj.CURSOR_KEY) or {}).get("key"), \
        "an unbudgeted pass left a cursor behind, which would make the next budgeted run " \
        "resume mid-corpus over rows it had just refreshed"


def test_editing_the_resume_throws_the_cursor_away():
    first, _ = _run_budget()
    # A different résumé means every stored score is stale, so resuming mid-corpus would leave
    # d..f on the old scale indefinitely. The rev must invalidate the cursor and restart at 'a'.
    second, _ = _run_budget(rows=_budget_rows(), kv=first.kv,
                            resume="Data analyst. SQL, dashboards, forecasting, Python.\n")
    scored = [ch for ch in _BUDGET_ROWS if second.rows[_url(ch)]["match_score"] != 11]
    assert scored == ["a", "b", "c"], \
        "run two scored %r; a changed résumé must restart the pass at the newest row" % (scored,)


def test_a_failed_flush_does_not_advance_the_cursor_past_it():
    """The cursor names the last row WRITTEN, not the last analyzed.

    Those differ exactly when a flush fails. Advance over unwritten rows and the next run skips
    them, so they keep a stale score until the rev changes — a silent hole precisely in the rows
    the run thought it had handled."""
    rows = _budget_rows()
    fake = _FakeDB(rows)

    calls = []

    def _flaky(scores):
        calls.append(sorted(scores))
        # First chunk (a, b) lands; every later flush fails, so c is analyzed but never stored.
        if len(calls) > 1:
            raise RuntimeError("proxy said no")
        for u, s in scores.items():
            fake.rows[u]["match_score"] = s

    fake.update_scores = _flaky
    _run_budget(rows=rows, chunk=2, fake=fake)
    assert len(calls) > 1, "only one flush happened; this test needs a failing LATER chunk"
    cur = fake.kv.get(sj.CURSOR_KEY) or {}
    assert cur.get("key"), "no cursor stored at all"
    assert cur["key"][2] == _url("b"), \
        "cursor names %r, but only a..b were written — c would be skipped forever" \
        % (cur["key"][2],)


def test_a_truncated_pass_does_not_blank_the_jdmeta_cache():
    # jdmeta.json is the web app's precomputed analysis for the WHOLE corpus and save_jdmeta
    # REPLACES it. Writing only a truncated run's share would blank the rest -- and because
    # _persist_derived re-runs core.job_meta for anything missing from the map, at the same
    # ~206 ms/row, that would hand the derived phase the entire cost this budget just refused.
    prior = {_url(ch): {"analyzed": {"keywords": {}}, "exp_years": None} for ch in _BUDGET_ROWS}
    fake, metas = _run_budget(prior_meta=prior)
    assert metas, "save_jdmeta was never called"
    saved = metas[-1]
    for ch in "def":
        assert _url(ch) in saved, \
            "row %s vanished from jdmeta; every unreached row's analysis must be carried over" % ch
    for ch in "abc":
        assert saved[_url(ch)].get("analyzed", {}).get("keywords") != {}, \
            "row %s kept its stale prior analysis instead of the one just computed" % ch


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  -", fn.__name__)
    print("\nAll %d JD-persistence checks passed." % len(fns))
