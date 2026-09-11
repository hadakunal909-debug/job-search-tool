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

import db as real_db
import scraper.score_jobs as sj


class _FakeDB(object):
    """Just enough of db.py for the JD phase: a url -> row table with a jd column."""

    COLS_SCORE = "url,found_date,location,first_seen"
    JOBS_DERIVED_SQL = ""
    JD_MAX_CHARS = real_db.JD_MAX_CHARS

    # DELEGATED, NOT REIMPLEMENTED. jd_fingerprint is a pure function of the text -- no
    # database, no network -- and the whole point of it is that the fingerprint the scoring
    # pass stamps is byte-identical to the one db.update_jds writes. A fake with its own
    # copy could agree with itself while disagreeing with production, which is the exact
    # failure this column exists to make impossible.
    jd_fingerprint = staticmethod(real_db.jd_fingerprint)

    # _send_derived asks this to tell an un-migrated column apart from a real write failure.
    # Delegated for the same reason as jd_fingerprint above: a fake with its own idea of what
    # 'that column does not exist' looks like could pass while production dropped a field.
    _column_missing = staticmethod(real_db._column_missing)

    def __init__(self, rows):
        self.rows = {r["url"]: dict(r) for r in rows}
        self.jd_writes = []                       # every url passed to update_jds, in order
        self.kv = {}                              # put_kv/get_kv blobs, incl. the thin ledger
        self.score_calls = []                     # one entry per update_scores call: its urls
        self.field_calls = []                     # one entry per update_job_fields call

    # --- reads -------------------------------------------------------------------
    def load_jobs(self, include_jd=True, cols=None):
        return [dict(r) for r in self.rows.values()]

    def load_jobs_by_urls(self, urls, include_jd=True):
        return [dict(self.rows[u]) for u in urls if u in self.rows]

    def urls_missing_jd(self):
        return {u for u, r in self.rows.items() if not (r.get("jd") or "").strip()}

    def urls_missing_jd_terms(self):
        # The analysis backlog, which is a DIFFERENT question from the one above: score_jobs
        # subtracts these two to find rows holding a description nothing ever analysed. A fake
        # that answered only the first would make that subtraction return every row here.
        return {u for u, r in self.rows.items() if not (r.get("jd_terms") or "").strip()}

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

    def update_job_fields(self, rows, keys=None):
        # Applied the way db._upsert would, and the two details it gets right are the whole
        # reason these tests can see anything:
        #   1. it WRITES the columns rather than merging truthy values, so a stale value failing
        #      to clear is visible (the CSV fallback merges; the remote path does not);
        #   2. with no `keys`, the union is taken over the rows' NON-NULL values, because
        #      _upsert drops Nones on its way to computing it. A fake that unioned the raw keys
        #      would send exp_max_years on every batch for free and the group-naming test would
        #      pass against the old behaviour.
        cols = (sorted(keys) if keys else
                sorted({k for r in rows for k, v in r.items() if v is not None}))
        self.field_calls.append({"urls": [r.get("url") for r in rows], "cols": cols})
        for r in rows:
            row = self.rows.get(r.get("url"))
            if row is None:
                continue
            for k in cols:
                if k != "url":
                    row[k] = r.get(k)

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

    def has_remote_db(self):
        return True


def _write_resume(tmp):
    """main() aborts outright without a resume.txt in the working directory ("scores would all
    be 0"), so every run here needs one. Its content is irrelevant to what these tests assert."""
    with open(os.path.join(tmp, "resume.txt"), "w", encoding="utf-8") as fh:
        fh.write("Program manager. Roadmap, stakeholder management, risk, budget, delivery.\n")


def _run_main(rows, cache, argv, idf=None):
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
        # `idf` DEFAULTS TO EMPTY, WHICH IS NOT A NEUTRAL CHOICE. An empty idf sends main()
        # down its cold-start branch, and that branch reads the WHOLE corpus into row_jd --
        # after which every row already holds its text and the by-url lookup below it is
        # never reached. A test about that lookup has to hand over a warm idf or it silently
        # exercises nothing, which is exactly how the first version of
        # test_the_analysis_asks_the_database_before_the_whole_jd_cache passed against the
        # unfixed code and proved nothing.
        sj.core.load_idf = lambda *a, **kw: dict(idf or {})
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


def test_a_finished_cursor_slice_still_derives_only_what_it_walked():
    """A pass can be PARTIAL without being budget-truncated, and that is the expensive case.

    Run two below resumes at the cursor, so `todo_order` is the slice d..f rather than the
    corpus -- and three rows fit inside the budget, so the loop ends on its own and
    `unscored_left` is 0. `truncated` is therefore False while half the corpus has not been
    looked at, and the derived-fields phase used to read that as "this pass covered
    everything" and fall through to the whole of `row_loc`.

    That phase has no budget of its own and writes at ~2,000 rows per 80 s through the proxy.
    Measured in production: the 2026-09-11 run finished a 9,677-row slice, handed the derived
    write all 53,247 rows, banked 26,000 of them and was SIGKILLed by the step cap at 26
    minutes -- while the runs either side of it, which the budget DID truncate, narrowed to
    ~19,700 rows and finished in ~21. So the run that analysed the least wrote the most, and
    the cap had already been raised 14 -> 15 -> 26 chasing it.
    """
    first, _ = _run_budget()
    second, _ = _run_budget(rows=_budget_rows(), kv=first.kv)
    walked = {_url(ch) for ch in "def"}
    touched = {u for call in second.field_calls for u in call["urls"]}
    assert touched and touched <= walked, \
        "the derived write covered %r; it must cover only the rows this run walked (%r), or a " \
        "pass that finished its cursor slice pays for the whole corpus with no budget on it" \
        % (sorted(touched), sorted(walked))


def test_a_finished_cursor_slice_does_not_blank_the_jdmeta_cache():
    """The same wrong predicate, second consumer. jdmeta is the web app's map for the WHOLE
    corpus and save_jdmeta REPLACES the file, so a pass holding only its slice must merge the
    previous entries back in first. Gated on `truncated`, that merge was skipped for exactly
    the run above: 9,677 entries were written over a 53,247-row map, blanking the rest for the
    web app and putting them back through core.job_meta at request time."""
    prior = {_url(ch): {"analyzed": {"keywords": {}}, "exp_years": None} for ch in _BUDGET_ROWS}
    first, _ = _run_budget(prior_meta=prior)
    _, metas = _run_budget(rows=_budget_rows(), kv=first.kv, prior_meta=prior)
    assert metas, "save_jdmeta was never called"
    saved = metas[-1]
    for ch in "abc":
        assert _url(ch) in saved, \
            "row %s vanished from jdmeta; a pass that resumed at a cursor never looked at it, " \
            "so its stored analysis must be carried over rather than replaced" % ch


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


def test_new_only_reaches_the_oldest_unscored_row_before_the_newest():
    """The cheap pass has no cursor, so its ORDER is the only thing bounding how long a row can
    wait. Newest-first plus a budget cuts the tail in the same place on every run: the 944 rows
    found on 2026-08-30 holding a full description and no analysis had stragglers three weeks
    old, buried under each day's ~2,000 new rows. Rows a past run left unscored are therefore
    drained oldest-first, behind whatever this run fetched."""
    rows = _budget_rows(score=None)          # every row NULL -> the whole set is the backlog
    fake, _ = _run_budget(rows=rows, argv=["--new-only"])
    scored = [ch for ch in _BUDGET_ROWS if fake.rows[_url(ch)]["match_score"] is not None]
    assert scored == ["d", "e", "f"],         "scored %r; the budget must reach the OLDEST unscored rows, or the ones under a busy "         "day's ingest are never analysed at all" % (scored,)
    for ch in "abc":
        assert fake.rows[_url(ch)]["match_score"] is None,             "row %s was written despite the budget stopping before it" % ch


def test_the_probed_window_moves_from_one_run_to_the_next():
    """THREE FIXED ROWS MUST NOT SPEAK FOR A WHOLE HOST.

    _thin_retry_plan took `sorted(by_host[h])[:3]` -- the same three urls, alphabetically, on
    every run forever. The daily seed rotated which HOSTS were due; nothing rotated which ROWS.
    So a host whose first three urls happened to be unfetchable recorded a failure every single
    run, doubled its backoff toward 64 days, and the rest of its backlog was never touched.

    Measured on the live corpus 2026-09-01: apply.actalentservices.com held 755 rows the feed
    was calling "JD pending" and sat at f=2, next=2026-09-05, while 31 of those rows were still
    listed on the board and would have returned a 5,393-character description on request.
    """
    urls = ["https://shell.com/job/%02d" % i for i in range(30)]
    day1 = sj._host_window(urls, 3, 40)
    day2 = sj._host_window(urls, 3, 41)
    assert day1 != day2, "the window did not move between runs: %r" % (day1,)
    assert not (set(day1) & set(day2)),         "consecutive runs re-probed %r -- the offset must step by the WINDOW, not by 1"         % (sorted(set(day1) & set(day2)),)

    # ...and it eventually reaches every row, which is the property the host-level backoff needs
    # in order to ever be re-earned.
    seen = set()
    for d in range(len(urls)):
        seen.update(sj._host_window(urls, 3, d))
    assert seen == set(urls), "%d of %d rows are unreachable by any seed" % (len(seen), len(urls))


def test_the_window_still_bounds_itself_at_the_edges():
    """The rotation must not change the size of the bite, or THIN_PROBE_MAX stops bounding the
    run -- and a wrapped window must not silently return fewer rows than it was asked for."""
    urls = ["https://shell.com/job/%02d" % i for i in range(10)]
    for seed in range(0, 97):
        w = sj._host_window(urls, 3, seed)
        assert len(w) == 3, "seed %d returned %d rows" % (seed, len(w))
        assert len(set(w)) == 3, "seed %d returned a duplicate: %r" % (seed, w)
        assert set(w) <= set(urls), "seed %d invented a url: %r" % (seed, w)
    assert sj._host_window(urls, 3, 0) == urls[:3], "seed 0 must keep the plain head"
    assert sj._host_window(urls[:2], 3, 7) == urls[:2], "a short list must not wrap onto itself"
    assert sj._host_window([], 3, 7) == []
    assert sj._host_window(urls, 0, 7) == []


def test_rotation_does_not_loosen_the_per_host_cap():
    """The cap is what makes an unreachable ledger safe. Rotating WHICH rows are taken must not
    change HOW MANY -- the bound is by construction, not by the ledger."""
    rows = _thin_rows(40)
    cache = {r["url"]: _SHELL for r in rows}
    for seed in (0, 1, 7, 13):
        plan, hosts = sj._thin_retry_plan(sorted(cache), {}, sj._extractor_rev(),
                                          "2026-09-02", seed=seed)
        assert len(plan) == sj.THIN_PROBE_PER_HOST,             "seed %d planned %d probes; the cap is %d" % (seed, len(plan), sj.THIN_PROBE_PER_HOST)
        assert hosts == ["shell.com"], hosts


def test_the_bulk_phase_visits_every_board_exactly_once():
    """The windowed submission must not drop or repeat a board.

    The bulk loop was `ex.map(_one, bulk)`, which submits all of them at once and holds each
    result until the consumer reaches it IN ORDER -- so one slow board pins every map that
    finished behind it, and jd_map_for returns a WHOLE BOARD with every description. That is
    the phase 4 of the 6 score-step runs in the live cron log were SIGKILLed inside. It is now
    a bounded window drained with as_completed, which changes both the ORDER boards are visited
    in and the number in flight; this pins the part that must not change.
    """
    rows = _thin_rows(1)
    seen = []

    def _map(board_url, ats, needed=None):
        seen.append(board_url)
        return {}

    boards = [("https://boards.example.com/b%02d" % i, "greenhouse", "Co%02d" % i)
              for i in range(40)]
    saved_sources = sj.scraper.SOURCES
    saved_custom = sj.scraper.custom_sources
    saved_has = sj._board_has_missing
    try:
        sj.scraper.SOURCES = boards
        sj.scraper.custom_sources = lambda: []
        sj._board_has_missing = lambda *a, **kw: True
        _run_with_fetch(rows, {rows[0]["url"]: _SHELL}, [], lambda _u: _JD, jd_map=_map)
    finally:
        sj.scraper.SOURCES = saved_sources
        sj.scraper.custom_sources = saved_custom
        sj._board_has_missing = saved_has

    assert len(seen) == len(set(seen)), "a board was fetched twice: %r" % (
        [b for b in seen if seen.count(b) > 1][:3],)
    assert set(seen) == {b for b, _a, _c in boards}, (
        "%d of %d boards visited; missing %r"
        % (len(set(seen)), len(boards),
           sorted({b for b, _a, _c in boards} - set(seen))[:3]))


_ORACLE_URL = ("https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/"
               "sites/CX_45001/job/344471")
# Shaped from the real response for that requisition, trimmed to the keys under test.
_ORACLE_ITEM = {
    "ExternalDescriptionStr": "<p>Position is based in Nashville, TN. The LVV team is seeking "
                              "experienced vendor managers.</p>",
    "ExternalQualificationsStr": "",
    "requisitionFlexFields": [
        {"Prompt": "Role", "Value": "Individual Contributor"},
        {"Prompt": "Years", "Value": "3 to 5+ years"},
        {"Prompt": "Additional Info",
         "Value": "Visa / work permit sponsorship is not available for this position"},
        {"Prompt": "Empty one", "Value": ""},
        {"Prompt": "", "Value": "orphan value with no label"},
    ],
}


def test_oracle_reads_its_FIELD_TABLE_not_just_the_description():
    """The two facts that decide whether a posting is worth opening are not in its prose.

    Oracle's candidate page renders a labelled block above the description -- Role, Job Type,
    Years, Additional Info -- and every one is a requisitionFlexFields entry. The extractor read
    only the four description fields, so a posting showing "Years: 3 to 5+ years" on its own
    page reported "Not stated in this posting" here.

    THE SPONSORSHIP HALF IS THE SERIOUS ONE. "Visa / work permit sponsorship is not available for
    this position" sits in the same block, and core.sponsorship_from_jd reads it correctly the
    moment it can see it -- so without it the card fell back to the EMPLOYER's filing history
    and said "H-1B Likely" on a posting that rules sponsorship out in writing. 2,495 active rows
    are on this host, including JPMorgan Chase (582) and Oracle (513).
    """
    saved = sj.scraper._get_json
    try:
        sj.scraper._get_json = lambda *a, **kw: {"items": [_ORACLE_ITEM]}
        jd = sj.oracle_detail_jd(_ORACLE_URL)
    finally:
        sj.scraper._get_json = saved

    assert "vendor managers" in jd, "the description itself must survive"
    assert "Years: 3 to 5+ years" in jd, jd[-200:]
    assert "Visa / work permit sponsorship is not available" in jd, jd[-200:]
    # A label or a value alone is not a field, and rendering "": "" would be noise in jd_terms.
    assert "Empty one" not in jd and "orphan value" not in jd, jd[-200:]

    clean = sj.core.clean_jd(jd)[0]
    assert sj.core.experience_years(clean) == 3, sj.core.experience_years(clean)
    verdict, _why = sj.core.sponsorship_from_jd(clean)
    assert verdict == "blocked", verdict
    # ...and that verdict must actually strip the employer's routes off the card.
    assert sj.core.visa_tags_for_posting(("h1b", "green_card"), verdict, _why) == (), \
        "a posting that rules sponsorship out still showed the employer's H-1B chip"


def test_the_blind_boards_rotate_and_every_one_is_reached():
    """jibe/phenom boards are ROTATED, not skipped, and the rotation must reach all of them.

    _board_has_missing answers True unconditionally for these two because their rows store an
    apply url whose host varies per tenant -- correct, and the reason Actalent's 1,461 rows
    stopped sitting on a loading shell. The cost was never counted: 149 of the 151 boards a
    live run bulk-fetches are these, each returning its WHOLE board to satisfy almost none of
    the wanted rows. Taking a rotating slice bounds that, but only if the slice moves; a fixed
    head would fetch the same 20 forever and the other 129 would never be visited at all.
    """
    blind = sorted(("https://b%03d.example.com" % i, "jibe", "Co%03d" % i) for i in range(149))
    seen, n = set(), sj._BLIND_BOARDS_PER_RUN
    for day in range(9):
        window = sj._host_window(blind, n, 20260901 + day)
        assert len(window) == n, "run %d took %d boards, not %d" % (day, len(window), n)
        seen |= set(window)
    assert seen == set(blind), (
        "%d of %d blind boards reached in 9 runs; %d never visited"
        % (len(seen), len(blind), len(set(blind) - seen)))


def test_the_bulk_window_is_smaller_than_a_real_board_list():
    """The window is the memory bound, so it must actually bound something.

    151 boards were in flight on the run that died; the point of the constant is that the
    number of COMPLETED board maps waiting to be consumed is fixed regardless of how many
    boards there are. A window at or above the board count would be the old behaviour wearing
    a constant's name.
    """
    assert 0 < sj._BULK_WINDOW <= 32, sj._BULK_WINDOW
    # Not tied to the worker count, and deliberately: workers are a throttling decision about
    # outbound concurrency, the window is a memory one.
    assert sj._BULK_WINDOW >= 8, "a window below the worker count starves the pool"


# --- the derived-column write ----------------------------------------------------------------
#
# _persist_derived writes two column GROUPS, and the JD one (exp_max_years, sponsor_jd,
# sponsor_reason, jd_terms) is the expensive one to lose: it is the product of the analysis pass,
# and everything else a run produces has already been banked by the time it is sent. It is
# written in batches as the loop builds it; these two tests pin what batching must preserve.

_META = {"analyzed": {"weight": {"roadmap": 1.5, "stakeholder": 1.0}, "thin": False},
         "exp_years": None, "exp_level": "", "sponsor_jd": ["", ""]}


def _derived_rows(n):
    """n rows the derived phase wants to write: a readable description, and no JD columns stored
    at all, so every one of them diffs as changed."""
    return [{"url": "https://ex.com/d%02d" % i, "jd": _JD, "location": "Boston, MA",
             "found_date": "2026-09-01", "first_seen": "2026-09-01",
             "title": "Program Manager", "company": "Ex"} for i in range(n)]


def _run_derived(rows, fake=None, chunk=2):
    """Drive _persist_derived directly, with every row's analysis supplied.

    main() is not needed to test the WRITE and would bury the batch boundaries under a
    fetch/analyse pass; handing over jdmeta also keeps core.job_meta (~206 ms a row) out of it.
    """
    fake = fake if fake is not None else _FakeDB(rows)
    saved = (sj.db, sj.JD_WRITE_CHUNK)
    try:
        sj.db, sj.JD_WRITE_CHUNK = fake, chunk
        sj._persist_derived({r["url"]: r["location"] for r in rows},
                            {r["url"]: r["jd"] for r in rows},
                            current_rows=[dict(r) for r in rows],
                            jdmeta={r["url"]: dict(_META) for r in rows}, idf={})
    finally:
        sj.db, sj.JD_WRITE_CHUNK = saved
    return fake


def test_a_failed_jd_batch_does_not_throw_away_the_batches_around_it():
    """The JD write is the one phase whose work nothing else banks.

    On 2026-09-03 the pass printed `Derived fields: updated 1809 job(s)` and then died with no
    traceback at 1.24 GB RSS -- inside the single call that wrote the four JD columns for the
    whole corpus. match_score, loc_state and every fetched description had landed; only the
    analysis output was lost, and re-deriving it took an hour. So a write that fails must cost
    its own rows and nobody else's.
    """
    rows = _derived_rows(6)
    fake = _FakeDB(rows)
    real, calls = fake.update_job_fields, []

    def _flaky(payload, keys=None):
        calls.append([r["url"] for r in payload])
        if len(calls) == 2:                   # the second batch is the one the box kills
            raise RuntimeError("proxy said no")
        real(payload, keys=keys)

    fake.update_job_fields = _flaky
    _run_derived(rows, fake=fake, chunk=2)

    assert len(calls) >= 4, \
        "%d write(s) -- this test needs the JD group split into batches around a failing one" \
        % (len(calls),)
    stored = [bool(fake.rows[r["url"]].get("jd_terms")) for r in rows]
    assert stored == [True, True, False, False, True, True], \
        "stored %r -- one failed batch must not cost the batches before or after it" % (stored,)


def test_a_jd_batch_stating_no_years_still_clears_a_stale_floor():
    """Every batch writes the whole column GROUP, not the columns it happens to hold.

    db._upsert normalises each call to the union of its rows' keys and drops Nones on the way, so
    a batch in which no row states an experience floor would not send exp_max_years at all -- and
    a number an older, more credulous parser wrote would survive the very re-derive meant to
    clear it. Batching is what makes that reachable: with the whole corpus in one call, some row
    always carries a floor.
    """
    rows = _derived_rows(2)
    rows[0]["exp_max_years"] = 7              # what an earlier run's parser read
    fake = _run_derived(rows, chunk=1)        # one row a batch, so no batch holds a floor

    assert fake.rows[rows[0]["url"]].get("exp_max_years") is None, \
        "the stale floor survived -- the batch never sent the column"
    jd_calls = [c for c in fake.field_calls if "jd_terms" in c["cols"]]
    assert jd_calls and all("exp_max_years" in c["cols"] for c in jd_calls), \
        "a JD batch sent %r -- every batch must write the named group" \
        % ([c["cols"] for c in jd_calls],)


class _StubHTTP(object):
    """Records what db._upsert would have put ON THE WIRE. Only .post is needed -- that is the
    only verb an upsert uses -- and no credentials are touched, because db._rest and db._headers
    are pure since Supabase was removed. So this runs in CI, which has no database."""

    class _Resp(object):
        status_code = 204
        text = ""

    def __init__(self):
        self.posts = []
        self.tables = []

    def post(self, url, headers=None, params=None, data=None, timeout=None):
        # THE TABLE, NOT JUST THE ROWS. update_job_fields writes `jobs` and then
        # mirrors the derived columns into job_facts, so a recorder that could not
        # tell them apart reported 450 rows as [200, 200, 50, 200, 200, 50] and the
        # batching assertion below read that as a wire regression.
        self.posts.append(json.loads(data))
        self.tables.append(url.rstrip('/').rsplit('/', 1)[-1])
        return self._Resp()


def _upsert_posts(rows, keys=None):
    """The batches db.update_job_fields sends to `jobs` for `rows`, HTTP layer stubbed.

    JOBS ONLY. The job_facts mirror rides along on the same call and has its own
    coverage in test_the_derived_mirror_rides_with_the_write below; folding the two
    together here would make every assertion about the wire shape ambiguous.
    """
    saved = (real_db._http, real_db.has_remote_db)
    stub = _StubHTTP()
    try:
        real_db._http, real_db.has_remote_db = stub, lambda: True
        real_db.update_job_fields([dict(r) for r in rows], keys=keys)
    finally:
        real_db._http, real_db.has_remote_db = saved
    return [p for p, t in zip(stub.posts, stub.tables) if t == real_db.TABLE]


def _upsert_posts_by_table(rows, keys=None):
    """Same, but {table: [batches]} -- for asserting the mirror happened at all."""
    saved = (real_db._http, real_db.has_remote_db)
    stub = _StubHTTP()
    try:
        real_db._http, real_db.has_remote_db = stub, lambda: True
        real_db.update_job_fields([dict(r) for r in rows], keys=keys)
    finally:
        real_db._http, real_db.has_remote_db = saved
    out = {}
    for p, t in zip(stub.posts, stub.tables):
        out.setdefault(t, []).append(p)
    return out


def test_the_real_upsert_sends_the_column_group_it_was_handed():
    """_FakeDB above MIMICS db._upsert's key handling, so it cannot also be the proof of it.

    Driven for real, with only the HTTP layer stubbed: handed rows in which no row states an
    experience floor, the inferred union does not contain exp_max_years at all, so the column is
    never written and whatever an older, more credulous parser left there survives. Naming the
    group is what puts it on the wire. Only the columns that can be None are exposed this way --
    an unfound sponsorship verdict is "" and rides along either way -- which is why this is
    stated as a measurement of the payload rather than as a rule about nulls.
    """
    rows = [{"url": "https://ex.com/u1", "exp_max_years": None, "sponsor_jd": "",
             "sponsor_reason": "", "jd_terms": '{"w":{"roadmap":1.5},"n":0}'},
            {"url": "https://ex.com/u2", "exp_max_years": None, "sponsor_jd": "",
             "sponsor_reason": "", "jd_terms": '{"w":{"budget":1.1},"n":0}'}]

    # SINCE THE CONTRACT STEP THIS IS A QUESTION ABOUT job_facts, not about `jobs`. Every
    # column here moved, so db._upsert strips them from the `jobs` write and sends nothing --
    # which is the correct new answer and is asserted below rather than assumed.
    by_table = _upsert_posts_by_table(rows)
    named_by_table = _upsert_posts_by_table(rows, keys=sj.JD_DERIVED_COLS)
    assert not by_table.get(real_db.TABLE), \
        "a moved column reached `jobs`: %r" % (by_table.get(real_db.TABLE),)

    inferred = by_table[real_db.JOB_FACTS_TABLE][0]
    named = named_by_table[real_db.JOB_FACTS_TABLE][0]

    # The hazard this test was written for is GONE at the new address, and that is worth
    # pinning rather than deleting. On `jobs` the union was inferred AFTER _upsert dropped
    # every None while merging duplicates, so a column that was None on every row of a batch
    # was never sent and a stale floor survived. mirror_job_facts infers from key PRESENCE,
    # before any of that -- so the null now goes out either way.
    assert "exp_max_years" in inferred[0], \
        "the inferred group dropped a column that was None on every row: %r" % (inferred[0],)
    assert all(r["exp_max_years"] is None for r in inferred), \
        "the column went out without the null that clears a stale floor"
    assert all(r["exp_max_years"] is None for r in named), \
        "a named group lost the null"
    assert "jd_terms" not in inferred[0], \
        "jd_terms belongs to job_terms, not job_facts: %r" % (inferred[0],)


def test_the_derived_mirror_rides_with_the_write():
    """A derived write must reach job_facts too, carrying only that table's own columns.

    The mirror is hooked into update_job_fields rather than into each caller, which is what
    makes a future writer get it for free -- so this is the test that proves the hook, not
    any one call site. It also pins the filtering: a payload carrying jd_terms must not push
    that into a table which has no such column.
    """
    rows = [{"url": "https://ex.com/m1", "exp_max_years": 5, "sponsor_jd": "",
             "sponsor_reason": "", "jd_terms": "x", "facts_fp": "abc"}]
    by_table = _upsert_posts_by_table(rows, keys=sj.JD_DERIVED_COLS)
    assert real_db.TABLE in by_table, "the authoritative write did not happen"
    assert real_db.JOB_FACTS_TABLE in by_table, (
        "the derived columns never reached job_facts: %r" % (sorted(by_table),))
    sent = by_table[real_db.JOB_FACTS_TABLE][0][0]
    assert "jd_terms" not in sent, (
        "jd_terms was pushed into a table with no such column: %r" % (sorted(sent),))
    assert sent.get("exp_max_years") == 5 and sent.get("facts_fp") == "abc", (
        "the mirror dropped a value it was supposed to carry: %r" % (sent,))


def test_batching_the_group_did_not_change_the_wire():
    """The batches are a memory bound, not a request budget.

    db._upsert has chunked the wire at 200 rows since long before this; if flushing the JD group
    every JD_WRITE_CHUNK rows had turned into one request per row, the write would be slower on
    the box it was meant to survive.
    """
    big = [{"url": "https://ex.com/b%03d" % i, "exp_max_years": None, "sponsor_jd": "",
            "sponsor_reason": "", "jd_terms": "x"} for i in range(450)]
    sizes = [len(p) for p in _upsert_posts(big, keys=sj.JD_DERIVED_COLS)]
    assert sizes == [200, 200, 50], "450 rows went out as %r" % (sizes,)


def test_the_analysis_asks_the_database_before_the_whole_jd_cache():
    """The analysis setup must fetch the rows it needs, not materialise every description.

    _load_jd_cache() builds a dict of EVERY stored description. Measured on the cPanel box
    2026-09-04: 8 MB -> 492 MB peak RSS, 29,991 entries, 159 MB of text, taken in one
    json.load() that cannot be chunked. The analysis setup asked for it FIRST in order to
    supply text for a few hundred rows -- 267 on the 18:14 run -- and db.load_jobs_by_urls,
    which asks for exactly those urls, was sitting right underneath it as the fallback.

    That is why the fetch pass was SIGKILLed on EVERY cron run from 2026-09-02: rc=137,
    twice a weekday, straight to cron mail. The log always cut off in the same place, the
    line after "Picking up N row(s) a previous run left unscored."

    ASSERTED AS A CALL COUNT, not as peak memory. The cache written by this harness holds
    two rows and would not move RSS by a measurable byte, so a memory assertion here would
    pass against the old code as well. What is being pinned is that the expensive read is
    never REACHED when the database can answer, which is exactly what the count states.

    The scenario is built so this block is the ONLY thing that could load the cache: every
    row holds a description in the database, so urls_missing_jd() is empty and the repair
    path above does not read it, and nothing is fetched, so the merge-and-save path does
    not either. A non-zero count can therefore only have come from the statement under test.
    """
    rows = [
        # A description in the DATABASE and no jd_terms: this is the analysis backlog, the
        # set urls_missing_jd_terms() - urls_missing_jd() returns.
        {"url": "https://ex.com/a", "jd": _JD, "location": "Boston, MA",
         "found_date": "2026-08-01", "first_seen": "2026-08-01",
         "title": "Program Manager", "company": "Ex"},
        {"url": "https://ex.com/c", "jd": _JD, "location": "Austin, TX",
         "found_date": "2026-08-02", "first_seen": "2026-08-02",
         "title": "Program Manager", "company": "Ex"},
    ]
    reads = []
    saved = sj._load_jd_cache

    def _counted():
        reads.append(1)
        return saved()

    try:
        sj._load_jd_cache = _counted
        # A WARM idf, so main() takes its normal path -- see _run_main, where the default
        # empty one reads the whole corpus into row_jd and this statement is never reached.
        #
        # The cache holds an UNRELATED url. Seeding it with these two rows defeats the test
        # a second way: the fetch queue would be satisfied from the cache, row_jd would be
        # filled from there instead, and `need` would again be empty.
        fake = _run_main(rows, {"https://ex.com/z": _JD}, ["--new-only"],
                         idf={"program": 1.0, "manager": 1.0, "roadmap": 2.0})
    finally:
        sj._load_jd_cache = saved

    assert fake.score_calls, (
        "nothing was scored, so the run never reached the statement under test")
    assert not reads, (
        "the analysis loaded the entire JD cache %d time(s) even though the database held "
        "every description it asked for -- on the live box that read is 484 MB, and it is "
        "what gets the pass SIGKILLed" % len(reads))


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  -", fn.__name__)
    print("\nAll %d JD-persistence checks passed." % len(fns))
