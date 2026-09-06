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

def test_readiness_is_a_stamp_not_the_table_existing():
    seen = []

    def fetch(table, params):
        seen.append(table)
        if table == real_db.VERSIONS_TABLE:
            return []                                   # table exists, nothing stamped
        return []

    real_fetch, real_remote = real_db._fetch_all, real_db.has_remote_db
    real_db._fetch_all, real_db.has_remote_db = fetch, lambda: True
    _reset()
    try:
        ready = real_db.jd_table_ready()
    finally:
        real_db._fetch_all, real_db.has_remote_db = real_fetch, real_remote
    # An EMPTY data_versions means the backfill has not declared completion. The table may well
    # exist and be half full; that is precisely the state that must not be read from.
    _check("an unstamped table is NOT ready", ready is False)


def test_a_stamp_makes_it_ready():
    def fetch(table, params):
        if table == real_db.VERSIONS_TABLE:
            return [{"name": "job_descriptions", "version": "backfilled-20260906"}]
        return []

    real_fetch, real_remote = real_db._fetch_all, real_db.has_remote_db
    real_db._fetch_all, real_db.has_remote_db = fetch, lambda: True
    _reset()
    try:
        _check("a stamped table is ready", real_db.jd_table_ready() is True)
    finally:
        real_db._fetch_all, real_db.has_remote_db = real_fetch, real_remote


def test_an_unreadable_stamp_counts_as_not_ready():
    def fetch(table, params):
        raise RuntimeError("connection reset by peer")

    real_fetch, real_remote = real_db._fetch_all, real_db.has_remote_db
    real_db._fetch_all, real_db.has_remote_db = fetch, lambda: True
    _reset()
    try:
        ready = real_db.jd_table_ready()
    finally:
        real_db._fetch_all, real_db.has_remote_db = real_fetch, real_remote
    # DELIBERATELY THE OPPOSITE OF _derived_signature, which RAISES when it cannot read a
    # version. There, guessing means two workers agree on a cache key while disagreeing about
    # the data. Here, guessing wrong in this direction just means reading jobs.jd -- which is
    # still the source of truth until the contract step drops the column. Falling back is free;
    # falling forward is not.
    _check("an unreadable stamp falls back rather than guessing", ready is False)


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


def test_get_job_jd_follows_the_gate():
    _check("unstamped -> reads jobs", _capture_reads(False) == [real_db.TABLE],
           repr(_capture_reads(False)))
    _check("stamped -> reads job_descriptions", _capture_reads(True) == [real_db.JD_TABLE],
           repr(_capture_reads(True)))


# ---- the mirror ------------------------------------------------------------------------------

def test_the_mirror_cannot_take_the_real_write_down_with_it():
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

    _check("a missing mirror table does not fail the write", ok)
    # THE ORDER IS THE SAFETY PROPERTY. jobs.jd is written first and is authoritative, so a
    # mirror that fails costs the copy and never the text.
    _check("...and the authoritative row still landed",
           any(r.get("url") == "u/1" and r.get("jd") == "a description" for r in posted),
           repr(posted))


# ---- the same gate, for the derived columns ---------------------------------------------

def test_job_facts_gate_matches_the_description_gate():
    """job_facts uses the identical stamp mechanism, and for a sharper reason.

    A missing row in job_descriptions reads as 'no description' -- wasteful and visible. A
    missing row in job_facts reads as 'no experience floor, no pay, no sponsorship verdict',
    and web._filter_rows KEEPS a row it has no number for, deliberately. So a half-copied
    facts table does not empty the feed, it silently WIDENS every filter -- which is exactly
    the defect this revamp started from.
    """
    def fetch(table, params, stamped):
        if table == real_db.VERSIONS_TABLE:
            return [{"name": "job_facts", "version": "v1"}] if stamped else []
        return []

    for stamped, expect in ((False, False), (True, True)):
        real_fetch, real_remote = real_db._fetch_all, real_db.has_remote_db
        real_db._fetch_all = lambda t, p, s=stamped: fetch(t, p, s)
        real_db.has_remote_db = lambda: True
        _reset()
        real_db._facts_src.update(ready=None, at=0.0)
        try:
            got = real_db.job_facts_ready()
        finally:
            real_db._fetch_all, real_db.has_remote_db = real_fetch, real_remote
        _check("job_facts ready=%s when stamped=%s" % (expect, stamped), got is expect)


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

def test_the_fingerprint_keeps_counting_the_same_thing():
    """jobs_fingerprint's third component must survive the analysis changing tables.

    It counts rows carrying an analysis, and web keys its row cache on the result. If the
    count froze -- because it went on asking `jobs` after the writer moved to job_terms --
    the fingerprint would stop moving when a scoring run writes, and every worker would go
    on serving cards whose score_pending flag is a scrape old. That is the exact failure the
    component was ADDED for, in the other direction.
    """
    asked = []

    def counter(table, params=None):
        asked.append((table, tuple(sorted((params or {}).items()))))
        return 1

    def fetch(table, params, stamped):
        if table == real_db.VERSIONS_TABLE:
            return [{"name": "job_terms", "version": "v1"}] if stamped else []
        return [{"first_seen": "2026-09-06"}]

    for stamped, want_table in ((False, real_db.TABLE), (True, real_db.JOB_TERMS_TABLE)):
        saved = (real_db.table_count, real_db._fetch_all, real_db.has_remote_db)
        real_db.table_count = counter
        real_db._fetch_all = lambda t, p, s=stamped: fetch(t, p, s)
        real_db.has_remote_db = lambda: True
        _reset()
        real_db._terms_src.update(ready=None, at=0.0)
        asked[:] = []
        try:
            real_db.jobs_fingerprint()
        finally:
            real_db.table_count, real_db._fetch_all, real_db.has_remote_db = saved
        counted = [t for t, p in asked if p]      # the filtered count, not the bare one
        _check("stamped=%s -> the analysis count reads %s" % (stamped, want_table),
               counted and counted[0] == want_table, repr(asked))


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
    for fn in (test_readiness_is_a_stamp_not_the_table_existing,
               test_a_stamp_makes_it_ready,
               test_an_unreadable_stamp_counts_as_not_ready,
               test_one_version_read_serves_every_consumer,
               test_get_job_jd_follows_the_gate,
               test_the_mirror_cannot_take_the_real_write_down_with_it,
               test_job_facts_gate_matches_the_description_gate,
               test_the_mirror_carries_only_the_columns_that_moved,
               test_the_fingerprint_keeps_counting_the_same_thing,
               test_the_terms_mirror_writes_a_length_and_a_null_not_an_empty_string,
               test_a_partial_derived_write_does_not_erase_the_other_half,
               test_the_migration_and_the_allowlist_agree):
        fn()
    _reset()
    print("\n%s" % ("FAILED: " + ", ".join(FAILED) if FAILED else "all checks passed"))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
