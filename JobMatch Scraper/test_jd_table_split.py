"""
test_jd_table_split.py — jobs.jd stays the source until the copy is provably complete.

Run it directly
    python test_jd_table_split.py
or via pytest (functions are named test_*).

WHAT THIS GUARDS. Phase 2 moves the description into public.job_descriptions. During the move
BOTH stores hold it: update_jds writes jobs.jd and then mirrors, and every reader keys off
db.jd_table_ready() to decide which one to believe.

THE DANGEROUS STATE IS A HALF-BACKFILLED TABLE, not a missing one. A url absent from
job_descriptions is indistinguishable from a job that has no description -- so reading a partial
copy would put thousands of already-fetched postings back into the scrape's fetch queue, and
score_jobs would re-fetch text it already held against hosts that rate-limit. Everything here
exists to make that state unreachable: readiness is an explicit stamp the backfill sets only
after a row-for-row verify, an unreadable stamp counts as NOT ready, and the mirror can fail
without taking the authoritative write with it.

Offline. The live half is scripts/backfill_job_descriptions.py --verify.
"""
import json
import os
import sys

import db as real_db

APP = os.path.dirname(os.path.abspath(__file__))
FAILED = []


def _check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("  -- " + detail) if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


class _Resp(object):
    status_code = 200
    text = ""


class _FakeHTTP(object):
    def __init__(self, post):
        self.post = post


def _reset():
    real_db._jd_src.update(ready=None, at=0.0)
    real_db._versions.update(map=None, at=0.0)
    real_db._jd_tbl["ok"] = True
    real_db._fp_col["ok"] = True
    real_db._jd_cache.clear()


# ---- the readiness gate ---------------------------------------------------------------------

def test_the_gates_are_constants_since_the_contract():
    """All three readiness gates must answer True NO MATTER WHAT data_versions says.

    This replaces three tests that pinned the opposite, and the reversal is the whole contract
    step. While `jobs` still carried its own copy, "unstamped or unreadable -> NOT ready" was
    the safe answer: falling back read an older source that was still correct.
    MIGRATION_contract.sql drops those columns, so the same fallback would now select a column
    that does not exist and PostgREST would 400 the read -- for the FEED, on a transient
    failure to read one small table.

    The unreadable case is the one that matters, so it is tested explicitly: a gate that
    consulted the network could take the site down every time that read blipped, and this
    project has already measured the proxy answering 200 with HTML.
    """
    def blow_up(table, params=None):
        raise RuntimeError("connection reset by peer")

    def empty(table, params=None):
        return []                                    # table exists, nothing stamped

    for label, fetch in (("unreadable", blow_up), ("unstamped", empty)):
        saved = (real_db._fetch_all, real_db.has_remote_db)
        real_db._fetch_all, real_db.has_remote_db = fetch, lambda: True
        _reset()
        try:
            got = (real_db.jd_table_ready(), real_db.job_facts_ready(),
                   real_db.job_terms_ready())
        finally:
            real_db._fetch_all, real_db.has_remote_db = saved
        _check("%s stamps still read READY (no dropped column is ever selected)" % label,
               got == (True, True, True), repr(got))


def test_one_version_read_serves_every_consumer():
    calls = []

    def fetch(table, params):
        calls.append(table)
        return [{"name": "job_descriptions", "version": "v1"},
                {"name": "companies", "version": "v2"}]

    real_fetch, real_remote = real_db._fetch_all, real_db.has_remote_db
    real_db._fetch_all, real_db.has_remote_db = fetch, lambda: True
    _reset()
    try:
        real_db.jd_table_ready()
        real_db.get_data_version("companies")
        real_db.get_data_version("job_descriptions")
    finally:
        real_db._fetch_all, real_db.has_remote_db = real_fetch, real_remote
    # get_job_jd is on /job's critical path against a box measured at 271 ms round trip. A probe
    # per consumer would be a whole extra page load per worker.
    _check("three consumers, one round trip",
           calls.count(real_db.VERSIONS_TABLE) == 1, "%d read(s)" % len(calls))


# ---- the readers follow the gate --------------------------------------------------------------

def _capture_reads(ready):
    seen = []

    def fetch(table, params):
        seen.append(table)
        if table == real_db.VERSIONS_TABLE:
            return [{"name": "job_descriptions", "version": "v1"}] if ready else []
        return [{"jd": "text"}]

    real_fetch, real_remote = real_db._fetch_all, real_db.has_remote_db
    real_db._fetch_all, real_db.has_remote_db = fetch, lambda: True
    _reset()
    try:
        real_db.get_job_jd("u/1")
    finally:
        real_db._fetch_all, real_db.has_remote_db = real_fetch, real_remote
    return [t for t in seen if t != real_db.VERSIONS_TABLE]


def test_every_read_goes_to_the_table_that_owns_the_column():
    _check("the description is read from job_descriptions, always",
           _capture_reads(True) == [real_db.JD_TABLE], repr(_capture_reads(True)))
    _check("...including when nothing is stamped (this used to read `jobs`)",
           _capture_reads(False) == [real_db.JD_TABLE], repr(_capture_reads(False)))


def test_jobs_no_longer_owns_the_moved_columns():
    """The constants that name columns, and the one that hands out DDL.

    FIELDS is not merely a write list -- scraper/dedupe_urls.py SELECTS it verbatim on a URL
    move -- so a name left here after the drop is a 400 on a read, not a skipped column.
    JOBS_DERIVED_SQL is worse: it is rendered on the admin page and printed by score_jobs as a
    paste-this block, so leaving the ALTERs in would hand an operator a script that silently
    re-creates the very duplicate columns the contract step exists to remove.
    """
    moved = real_db.MOVED_OFF_JOBS
    _check("11 columns moved off jobs", len(moved) == 11, repr(sorted(moved)))
    _check("facts_fp did NOT move (the staleness query needs it beside jd_fp in one table)",
           "facts_fp" not in moved)
    for name in ("FIELDS", "_FEED_COLS", "_FEED_COLS_OPT"):
        v = getattr(real_db, name)
        cols = v if isinstance(v, (list, tuple)) else str(v).split(",")
        bad = sorted({c.strip() for c in cols} & moved)
        _check("%s names no moved column" % name, not bad, repr(bad))
    readd = [c for c in moved
             if ("not exists " + c + " ") in real_db.JOBS_DERIVED_SQL]
    _check("JOBS_DERIVED_SQL does not re-create them", not readd, repr(readd))


def test_a_write_to_jobs_cannot_carry_a_moved_column():
    """The choke point, tested at _upsert rather than at each of the four writers.

    A moved column reaching `jobs` is not a dropped value, it is PostgREST 400ing the whole
    batch -- the scrape's description write, or the score pass's derived write, failing
    entirely. And it must NOT strip when the target is a side table, or the mirrors would
    write nothing at all.
    """
    sent = {}

    def post(url, headers=None, params=None, data=None, timeout=None):
        sent.setdefault(url, []).append(json.loads(data))

        class R(object):
            status_code = 200
            text = ""
        return R()

    row = {"url": "u/1", "title": "T", "exp_max_years": 5, "jd_terms": "x", "jd": "text",
           "loc_state": "MA", "facts_fp": "fp"}
    saved = (real_db._http, real_db.has_remote_db)
    real_db._http = type("H", (object,), {"post": staticmethod(post)})()
    real_db.has_remote_db = lambda: True
    try:
        real_db._upsert([dict(row)])
        real_db._upsert([dict(row)], table=real_db.JOB_FACTS_TABLE, pk="url")
    finally:
        real_db._http, real_db.has_remote_db = saved

    jobs_body = [b for u, bs in sent.items() if u.endswith(real_db.TABLE) for b in bs]
    side_body = [b for u, bs in sent.items()
                 if u.endswith(real_db.JOB_FACTS_TABLE) for b in bs]
    got = sorted(jobs_body[0][0]) if jobs_body else []
    _check("the jobs write carries no moved column",
           bool(got) and not (set(got) & real_db.MOVED_OFF_JOBS), repr(got))
    _check("...but still carries facts_fp and the real jobs columns",
           "facts_fp" in got and "title" in got, repr(got))
    side = sorted(side_body[0][0]) if side_body else []
    _check("a side-table write is NOT stripped",
           "exp_max_years" in side and "loc_state" in side, repr(side))


def test_a_narrow_read_still_returns_columns_that_moved():
    """load_jobs(cols=COLS_SCORE) must keep working, and this is the sharpest case.

    COLS_SCORE names nine columns that all moved to job_facts, and score_jobs diffs
    stored-vs-computed on them to decide whether to write. Let one come back missing and
    _persist_derived marks EVERY row changed on EVERY run and re-upserts the whole corpus for
    ever -- a failure that reads as "the scrape got slower", not as a bug.
    """
    asked = []

    def fetch(table, params=None, page=None):
        asked.append(table)
        if table == real_db.JOB_FACTS_TABLE:
            return [{"url": "u/1", "exp_max_years": 5, "loc_state": "MA", "remote": False,
                     "loc_metro": "Boston", "salary_min": 1, "salary_max": 2,
                     "salary_period": "year", "sponsor_jd": "", "sponsor_reason": ""}]
        # Honour the select, so a column the router forgot to ask `jobs` for is genuinely
        # absent from the result rather than supplied by a generous stub.
        sel = [c.strip() for c in (params or {}).get("select", "").split(",") if c.strip()]
        row = {"url": "u/1", "match_score": 7}
        return [{c: row.get(c, "v") for c in sel}]

    saved = (real_db._fetch_all, real_db.has_remote_db)
    real_db._fetch_all, real_db.has_remote_db = fetch, lambda: True
    _reset()
    try:
        rows = real_db.load_jobs(cols=real_db.COLS_SCORE)
    finally:
        real_db._fetch_all, real_db.has_remote_db = saved
    got = rows[0] if rows else {}
    missing = [c.strip() for c in real_db.COLS_SCORE.split(",")
               if c.strip() and c.strip() not in got]
    _check("every column COLS_SCORE asks for comes back", not missing, repr(missing))
    _check("...and exp_max_years came from job_facts, with its value",
           got.get("exp_max_years") == 5, repr(got))
    _check("job_facts was actually consulted", real_db.JOB_FACTS_TABLE in asked, repr(asked))


def test_a_failed_description_write_is_now_fatal():
    """The inversion the contract step forces, and it is the sharpest consequence of Phase 5.

    This test used to assert the opposite -- that a missing job_descriptions could not fail
    the write -- and the reasoning was sound at the time: jobs.jd was written first and was
    authoritative, so losing the mirror cost a copy and never the text.

    MIGRATION_contract.sql drops jobs.jd. There is now exactly ONE place a description is
    stored, so a swallowed failure is a description lost, on the scrape's critical path,
    while jobs.jd_fp has already been stamped to say we hold it. A scrape that stores no
    descriptions and reports success is precisely the shape of failure this project has
    already been bitten by.

    Raising leaves those urls in urls_missing_jd, which is what makes the next run re-fetch
    them. update_jds is chunked, so the blast radius is a chunk.
    """
    posted = []

    def post(url, headers=None, params=None, data=None, timeout=None):
        rows = json.loads(data)
        # job_descriptions is the one carrying jd_chars
        if any("jd_chars" in r for r in rows):
            raise RuntimeError('relation "public.job_descriptions" does not exist')
        posted.extend(rows)
        return _Resp()

    real_http, real_remote = real_db._http, real_db.has_remote_db
    real_db._http, real_db.has_remote_db = _FakeHTTP(post), lambda: True
    _reset()
    try:
        real_db.update_jds({"u/1": "a description"})       # must NOT raise
        ok = True
    except Exception as e:
        ok = False
        print("      raised: %r" % (e,))
    finally:
        real_db._http, real_db.has_remote_db = real_http, real_remote
        _reset()

    _check("a failed job_descriptions write RAISES rather than reporting success", not ok)
    # AND THE ORDER IS STILL A SAFETY PROPERTY, only reversed: the text is stored first, so a
    # failure costs both the text and its fingerprint rather than leaving jobs.jd_fp claiming
    # provenance for a description nobody can read.
    _check("...and no jd_fp was stamped for text that was never stored",
           not any(r.get("jd_fp") for r in posted), repr(posted))


# ---- the same gate, for the derived columns ---------------------------------------------

def test_the_mirror_carries_only_the_columns_that_moved():
    """mirror_job_facts filters to JOB_FACTS_COLS, and that is what makes the hook safe.

    It is wired into update_job_fields, through which EVERY write passes -- including
    requeue_analysis clearing match_score and close_dead_jds setting is_active. Those
    payloads must send nothing at all rather than a row of nulls, which would blank a real
    reading on its way past.
    """
    import json
    posted = []

    def post(url, headers=None, params=None, data=None, timeout=None):
        posted.append((url.rstrip('/').rsplit('/', 1)[-1], json.loads(data)))
        return _Resp()

    real_http, real_remote = real_db._http, real_db.has_remote_db
    real_db._http, real_db.has_remote_db = _FakeHTTP(post), lambda: True
    _reset()
    real_db._facts_tbl["ok"] = True
    try:
        # a payload with NO derived columns -- the requeue_analysis shape
        real_db.update_job_fields([{"url": "u/1"}], keys=("url", "match_score"))
        only_jobs = [t for t, _ in posted]
        posted[:] = []
        # ...and one that does carry them
        real_db.update_job_fields([{"url": "u/2", "exp_max_years": 3, "sponsor_jd": ""}],
                                  keys=("url", "exp_max_years", "sponsor_jd"))
        with_facts = [t for t, _ in posted]
    finally:
        real_db._http, real_db.has_remote_db = real_http, real_remote
        _reset()

    _check("a match_score clear does NOT touch job_facts",
           real_db.JOB_FACTS_TABLE not in only_jobs, repr(only_jobs))
    _check("a derived write DOES reach job_facts",
           real_db.JOB_FACTS_TABLE in with_facts, repr(with_facts))


# ---- the packed analysis --------------------------------------------------------------

def test_the_fingerprint_counts_the_table_that_holds_the_analysis():
    """Its third component counts rows carrying an analysis, and web keys its row cache on the
    result. Since the contract step `jobs` has no jd_terms to count, so this must read
    job_terms unconditionally -- asking `jobs` would now be a 400, and before the drop it
    would simply have frozen, leaving every worker serving cards whose score_pending flag is
    a scrape old.
    """
    asked = []

    def counter(table, params=None):
        asked.append((table, tuple(sorted((params or {}).items()))))
        return 1

    saved = (real_db.table_count, real_db._fetch_all, real_db.has_remote_db)
    real_db.table_count = counter
    real_db._fetch_all = lambda t, p=None, page=None: []      # nothing stamped anywhere
    real_db.has_remote_db = lambda: True
    _reset()
    real_db._terms_src.update(ready=None, at=0.0)
    try:
        real_db.jobs_fingerprint()
    finally:
        real_db.table_count, real_db._fetch_all, real_db.has_remote_db = saved
    counted = [t for t, p in asked if p]
    _check("the analysis count reads job_terms even with nothing stamped",
           bool(counted) and counted[0] == real_db.JOB_TERMS_TABLE, repr(asked))


def test_the_terms_mirror_writes_a_length_and_a_null_not_an_empty_string():
    """n_terms > 0 has to be the same predicate `jd_terms is not null` was on `jobs`.

    jobs_fingerprint and urls_missing_jd_terms both key off that predicate. If the mirror
    stored '' rather than NULL for a row with no analysis, `n_terms > 0` and
    `jd_terms is not null` would disagree, and the fingerprint would count rows the scorer
    still considers unanalysed.
    """
    import json
    posted = []

    def post(url, headers=None, params=None, data=None, timeout=None):
        if url.rstrip('/').rsplit('/', 1)[-1] == real_db.JOB_TERMS_TABLE:
            posted.extend(json.loads(data))
        return _Resp()

    real_http, real_remote = real_db._http, real_db.has_remote_db
    real_db._http, real_db.has_remote_db = _FakeHTTP(post), lambda: True
    _reset()
    real_db._terms_tbl["ok"] = True
    try:
        real_db.mirror_job_terms([{"url": "u/1", "jd_terms": "abc"},
                                  {"url": "u/2", "jd_terms": ""}])
    finally:
        real_db._http, real_db.has_remote_db = real_http, real_remote
        _reset()

    by_url = {r["url"]: r for r in posted}
    _check("a real analysis carries its length",
           by_url.get("u/1", {}).get("n_terms") == 3, repr(by_url.get("u/1")))
    _check("an empty analysis stores NULL, not an empty string",
           by_url.get("u/2", {}).get("jd_terms") is None, repr(by_url.get("u/2")))
    _check("...and length 0, so n_terms > 0 excludes it",
           by_url.get("u/2", {}).get("n_terms") == 0, repr(by_url.get("u/2")))


def test_a_partial_derived_write_does_not_erase_the_other_half():
    """THE WORST BUG THIS REVAMP INTRODUCED, found by auditing rather than by a failure.

    _persist_derived writes its derived fields in TWO payloads -- location/pay first, then
    the JD signals. mirror_job_facts originally named ALL ten job_facts columns on either
    one, so PostgREST received the other five as explicit NULLs. Measured before the fix: a
    single location/pay write erased exp_max_years, sponsor_jd, sponsor_reason and facts_fp.
    The two groups would have taken turns wiping each other on every scrape -- an experience
    floor silently going NULL, which is precisely the defect the whole revamp answers.

    It was dormant only because job_facts did not exist yet. It would have corrupted the
    table on the first scrape after the migration.
    """
    import json
    seen = []

    def post(url, headers=None, params=None, data=None, timeout=None):
        seen.append((url.rstrip('/').rsplit('/', 1)[-1], json.loads(data)))
        return _Resp()

    def facts_payload():
        return [r for t, rows in seen if t == real_db.JOB_TERMS_TABLE or
                t == real_db.JOB_FACTS_TABLE for r in rows
                if t == real_db.JOB_FACTS_TABLE]

    real_http, real_remote = real_db._http, real_db.has_remote_db
    real_db._http, real_db.has_remote_db = _FakeHTTP(post), lambda: True
    _reset()
    real_db._facts_tbl["ok"] = True
    try:
        # the location/pay group, exactly as _persist_derived builds it
        real_db.update_job_fields([{"url": "u/1", "loc_state": "MA", "loc_metro": "Boston",
                                    "remote": False, "salary_min": 90000,
                                    "salary_max": 120000, "salary_period": "year"}])
        loc_rows = facts_payload()
        seen[:] = []
        # ...and the JD group
        real_db.update_job_fields([{"url": "u/1", "exp_max_years": 5, "sponsor_jd": "",
                                    "sponsor_reason": "", "jd_terms": "x",
                                    "facts_fp": "abc"}],
                                  keys=("url", "exp_max_years", "sponsor_jd",
                                        "sponsor_reason", "jd_terms", "facts_fp"))
        jd_rows = facts_payload()
    finally:
        real_db._http, real_db.has_remote_db = real_http, real_remote
        _reset()

    jd_cols = {"exp_max_years", "sponsor_jd", "sponsor_reason", "facts_fp"}
    loc_cols = {"loc_state", "loc_metro", "remote", "salary_min", "salary_max",
                "salary_period"}
    _check("the location/pay write names NO JD-derived column",
           loc_rows and not (set(loc_rows[0]) & jd_cols), repr(loc_rows))
    _check("...and does carry its own", loc_rows and loc_cols <= set(loc_rows[0]),
           repr(loc_rows))
    _check("the JD write names NO location column",
           jd_rows and not (set(jd_rows[0]) & loc_cols), repr(jd_rows))
    _check("...and does carry its own",
           jd_rows and jd_cols <= set(jd_rows[0]), repr(jd_rows))


def test_the_migration_and_the_allowlist_agree():
    import dbproxy
    _check("job_descriptions is allowlisted on the proxy",
           "job_descriptions" in dbproxy.ALLOWED_TABLES)
    mig = open(os.path.join(APP, "MIGRATION_job_descriptions.sql"), encoding="utf-8").read()
    _check("the migration creates the table",
           "create table if not exists public.job_descriptions" in mig)
    # Without CASCADE every pruned posting leaves 5.8 KB of description behind for ever, in the
    # one table where a row is expensive. user_scores learned this the same way.
    _check("...with ON DELETE CASCADE", "on delete cascade" in mig)
    _check("...and ends with the schema reload",
           mig.strip().endswith("notify pgrst, 'reload schema';"))
    # A backfill inside the migration would materialise 263 MB in one statement on a box with a
    # ~1.2 GB account-wide cap, while Passenger workers are serving the site.
    _check("the migration does NOT backfill", "insert into public.job_descriptions" not in mig)


def main():
    print("jd table split")
    for fn in (test_the_gates_are_constants_since_the_contract,
               test_jobs_no_longer_owns_the_moved_columns,
               test_a_write_to_jobs_cannot_carry_a_moved_column,
               test_one_version_read_serves_every_consumer,
               test_every_read_goes_to_the_table_that_owns_the_column,
               test_a_failed_description_write_is_now_fatal,
               test_a_narrow_read_still_returns_columns_that_moved,
               test_the_mirror_carries_only_the_columns_that_moved,
               test_the_fingerprint_counts_the_table_that_holds_the_analysis,
               test_the_terms_mirror_writes_a_length_and_a_null_not_an_empty_string,
               test_a_partial_derived_write_does_not_erase_the_other_half,
               test_the_migration_and_the_allowlist_agree):
        fn()
    _reset()
    print("\n%s" % ("FAILED: " + ", ".join(FAILED) if FAILED else "all checks passed"))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
