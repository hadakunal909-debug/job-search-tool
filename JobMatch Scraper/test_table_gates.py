"""
test_table_gates.py — a row must come back COMPLETE in every combination of migrated tables.

Run it directly
    python test_table_gates.py
or via pytest (functions are named test_*).

WHY THIS EXISTS. The schema separation put three tables behind three independent stamps --
job_descriptions, job_facts, job_terms -- and the migrations are explicitly documented as
runnable in any order. So db.load_jobs and db.load_jobs_by_urls have to be correct in all eight
states, times include_jd either way: sixteen combinations, per function.

Nobody had run those combinations. An audit of the finished work did, and found two real bugs
that every other test missed:

  1. load_jobs(include_jd=True) returned rows with NO DESCRIPTION whenever job_facts or
     job_terms was stamped and job_descriptions was not. The merge branch fires on any of the
     three gates and selects _FEED_COLS, which does not name `jd` -- so the text was simply
     dropped. The scorer's full pass would have built its IDF over empty strings and analysed
     nothing, looking exactly like a corpus that had lost its descriptions.

  2. load_jobs_by_urls handled only the description split, so it read the copies still sitting
     on `jobs` while load_jobs read job_facts. Identical values today because the mirrors keep
     both in step -- and a 400 on every batch the moment a column is dropped.

Both were reachable, both were silent, and both were found by enumerating states rather than by
testing the happy path. That is the whole argument for this file.

Offline: the database is stubbed per combination.
"""
import itertools
import os
import sys

import db as real_db

FAILED = []

FEED = real_db._FEED_COLS.split(",")
JOBS_ONLY = {"url": "u/1", "title": "T", "company": "C", "found_date": "2026-09-01",
             "location": "Boston, MA", "sponsors_h1b": "yes", "match_score": 42,
             "status": "", "posted_verified": "", "posted_confidence": "",
             "is_active": True, "last_seen": "2026-09-06", "miss_count": 0,
             "first_seen": "2026-09-01"}
FACTS = {"url": "u/1", "loc_state": "MA", "loc_metro": "Boston", "remote": False,
         "salary_min": 90000, "salary_max": 120000, "salary_period": "year",
         "exp_max_years": 5, "sponsor_jd": "", "sponsor_reason": "", "facts_fp": "fp1"}


def _check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("  -- " + detail) if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def _run(jd_on, facts_on, terms_on, include_jd, by_urls):
    """One combination -> the first row db returns."""
    stamps = [{"name": n, "version": "v"} for n, on in
              (("job_descriptions", jd_on), ("job_facts", facts_on), ("job_terms", terms_on))
              if on]

    def fetch(table, params=None):
        params = params or {}
        if table == real_db.VERSIONS_TABLE:
            return stamps
        if table == real_db.JOB_FACTS_TABLE:
            return [dict(FACTS)]
        if table == real_db.JOB_TERMS_TABLE:
            return [{"url": "u/1", "jd_terms": "PACKED"}]
        if table == real_db.JD_TABLE:
            return [{"url": "u/1", "jd": "THE DESCRIPTION"}]
        # `jobs`, honouring the select -- so a column the caller dropped is genuinely absent,
        # which is the only way this can catch a merge that forgot to put one back.
        row = dict(JOBS_ONLY)
        if not facts_on:
            row.update({k: v for k, v in FACTS.items() if k != "url"})
        if not terms_on:
            row["jd_terms"] = "PACKED-FROM-JOBS"
        if include_jd and not jd_on:
            row["jd"] = "DESCRIPTION-FROM-JOBS"
        sel = (params.get("select") or "*").split(",")
        return [row if sel == ["*"] else {k: v for k, v in row.items() if k in sel}]

    saved = (real_db._fetch_all, real_db.has_remote_db, real_db._warn_full_jd_read)
    real_db._fetch_all, real_db.has_remote_db = fetch, lambda: True
    real_db._warn_full_jd_read = lambda: None          # the tripwire is not what is under test
    for m in (real_db._jd_src, real_db._facts_src, real_db._terms_src):
        m.update(ready=None, at=0.0)
    real_db._versions.update(map=None, at=0.0)
    try:
        rows = (real_db.load_jobs_by_urls(["u/1"], include_jd=include_jd) if by_urls
                else real_db.load_jobs(include_jd=include_jd))
    finally:
        (real_db._fetch_all, real_db.has_remote_db, real_db._warn_full_jd_read) = saved
        for m in (real_db._jd_src, real_db._facts_src, real_db._terms_src):
            m.update(ready=None, at=0.0)
        real_db._versions.update(map=None, at=0.0)
    return rows[0] if rows else {}


def _matrix(by_urls):
    label = "load_jobs_by_urls" if by_urls else "load_jobs"
    broken = []
    for jd_on, facts_on, terms_on, inc in itertools.product((0, 1), repeat=4):
        r = _run(jd_on, facts_on, terms_on, inc, by_urls)
        why = []
        missing = [c for c in FEED if c not in r]
        if missing:
            why.append("missing %s" % missing)
        # The values must be right whichever table they came from -- a merge that puts the key
        # back with a None would satisfy a presence check and still be wrong.
        if r.get("exp_max_years") != 5:
            why.append("exp_max_years=%r" % r.get("exp_max_years"))
        if not r.get("jd_terms"):
            why.append("jd_terms=%r" % r.get("jd_terms"))
        if inc and not r.get("jd"):
            why.append("jd=%r" % r.get("jd"))
        if why:
            broken.append("descr=%d facts=%d terms=%d jd=%d: %s"
                          % (jd_on, facts_on, terms_on, inc, "; ".join(why)))
    _check("%s: all 16 gate combinations return a complete row" % label,
           not broken, " | ".join(broken[:3]))


def test_load_jobs_gate_matrix():
    _matrix(by_urls=False)


def test_load_jobs_by_urls_gate_matrix():
    _matrix(by_urls=True)


def test_the_two_loaders_agree():
    """They are interchangeable to callers, so they must not disagree about where truth lives.

    notify.py's digest reads card fields through load_jobs_by_urls and the feed reads the same
    fields through load_jobs. One consulting job_facts while the other read the copies still on
    `jobs` is a difference nobody would see until the two stores diverged -- at which point the
    email and the website would be describing different jobs.
    """
    for jd_on, facts_on, terms_on, inc in itertools.product((0, 1), repeat=4):
        a = _run(jd_on, facts_on, terms_on, inc, by_urls=False)
        b = _run(jd_on, facts_on, terms_on, inc, by_urls=True)
        shared = set(FEED) & set(a) & set(b)
        differ = [c for c in sorted(shared) if a.get(c) != b.get(c)]
        if differ:
            _check("loaders agree at descr=%d facts=%d terms=%d jd=%d"
                   % (jd_on, facts_on, terms_on, inc), False, repr(differ))
            return
    _check("the two loaders return the same values in all 16 combinations", True)


def test_tables_not_keyed_on_url_are_ordered_on_their_own_key():
    """_fetch_all pages with `order=url`; two of these tables have no such column.

    FOUND ON THE LIVE DATABASE, minutes after the migration ran, and it would have made the
    entire revamp a no-op. `companies` is keyed on name_key and `data_versions` on name, so
    the default order asks Postgres for a column that is not there. Postgres answers
    `column "url" does not exist` -- and _table_missing() looks for the substring "does not
    exist", so load_companies' own except clause read a REAL error as "not migrated yet" and
    returned {}.

    The consequence was silent and total: every gate permanently not-ready, company_facts
    falling back for ever, and a symptom that reads as 'the migration ran and nothing
    changed'. No offline test could have caught it -- the tables had to exist to be queried
    wrongly.
    """
    asked = []

    def fetch(table, params=None):
        asked.append((table, (params or {}).get("order")))
        return []

    saved = (real_db._fetch_all, real_db.has_remote_db)
    real_db._fetch_all, real_db.has_remote_db = fetch, lambda: True
    real_db._versions.update(map=None, at=0.0)
    try:
        real_db.load_companies()
        real_db._versions_map()
    finally:
        real_db._fetch_all, real_db.has_remote_db = saved
        real_db._versions.update(map=None, at=0.0)

    by_table = dict(asked)
    _check("companies is ordered on name_key, not url",
           by_table.get(real_db.COMPANIES_TABLE) == "name_key", repr(asked))
    _check("data_versions is ordered on name, not url",
           by_table.get(real_db.VERSIONS_TABLE) == "name", repr(asked))
    # The three url-keyed tables must NOT be given a bespoke order -- the default is right
    # for them, and overriding it would be a second thing to keep in step.
    _check("...and nothing was asked for order=url on a table without one",
           "url" not in [o for t, o in asked
                        if t in (real_db.COMPANIES_TABLE, real_db.VERSIONS_TABLE)],
           repr(asked))


def test_has_a_description_and_missing_one_are_complements():
    """urls_with_jd and urls_missing_jd must partition the corpus. They did not.

    urls_missing_jd matches NULL *or* empty string, and its docstring explains why: a row can
    hold '' rather than NULL, and filtering on NULL alone would drop those from the fetch
    queue for ever. urls_with_jd matched `jd is not null` -- which counts an empty string as
    HAVING a description. So the same rows sat in both sets.

    Measured on the live corpus: 54 such rows. close_dead_jds writes '' to clear a junk
    description, which is how they get there. It surfaced because the backfill could not
    finish -- those 54 were permanently in its todo list and permanently unfetchable, so a
    pass copied nothing and the script stopped, correctly, rather than looping.

    `jd=neq.` is the one filter that means what the caller asks: in SQL a NULL fails <> too,
    so it excludes both, and it matches what the job_descriptions branch answers with
    jd_chars > 0.
    """
    seen = {}

    def fetch(table, params=None):
        p = params or {}
        seen[p.get("or") or p.get("jd") or "?"] = table
        return []

    saved = (real_db._fetch_all, real_db.has_remote_db)
    real_db._fetch_all, real_db.has_remote_db = fetch, lambda: True
    for m in (real_db._jd_src, real_db._facts_src, real_db._terms_src):
        m.update(ready=False, at=9e18)          # unstamped: the `jobs` branch
    try:
        real_db.urls_with_jd()
        real_db.urls_missing_jd()
    finally:
        real_db._fetch_all, real_db.has_remote_db = saved
        for m in (real_db._jd_src, real_db._facts_src, real_db._terms_src):
            m.update(ready=None, at=0.0)

    _check("urls_with_jd asks for jd <> '', not merely not-null",
           "neq." in seen, repr(sorted(seen)))
    _check("...and NOT the not.is.null form that counts an empty string as text",
           "not.is.null" not in seen, repr(sorted(seen)))
    _check("urls_missing_jd still matches NULL or empty",
           any(k.startswith("(jd.is.null") for k in seen), repr(sorted(seen)))


def test_page_size_is_chosen_from_row_width():
    """_fetch_all's page size is ours to pick, and picking it wrong costs either way.

    The docstring used to call 1000 "the server's per-request row cap (default 1000)". That was
    true of PostgREST and has not been true since: pgrest.py translates the request and psycopg
    runs it, and neither clamps `limit` -- it goes straight into the SQL. So every corpus-wide
    read was paying 47 round trips to a box measured 271 ms away for no reason at all.

    Measured against the live corpus, identical row sets both ways: job_terms 19.2s -> 4.7s,
    jobs 31.2s -> 8.3s, job_facts 24.1s -> 6.7s, and the feed's whole corpus load 69.2s -> 21.2s.

    The rule is ROW WIDTH, keeping a response near 3 MB, and it has a cliff on each side. Too
    small and a narrow read pays for round trips; too large and a description read materialises
    a 24 MB response as JSON on a box whose account cap is ~1.2 GB shared with the workers
    serving the site. Hence a test on both edges rather than on the numbers.
    """
    wide = ("*", "url,jd", "url,title,jd", "url,jd,jd_terms")
    for sel in wide:
        _check("a description read stays small: %r" % sel[:24],
               real_db._page_for(sel) <= real_db.PAGE_JD, "got %d" % real_db._page_for(sel))
    _check("url alone gets the widest page",
           real_db._page_for("url") == real_db.PAGE_NARROW)
    # Two columns, but one of them is the packed analysis: ~730 B a row, not ~100.
    _check("url,jd_terms is NOT treated as a narrow read",
           real_db._page_for("url,jd_terms") < real_db.PAGE_NARROW)
    _check("the feed column set gets the middle page",
           real_db._page_for(real_db._FEED_COLS) == real_db.PAGE_ROW)
    # FIELDS is the `jobs` write set and does NOT name jd -- the description has never
    # been one of the columns the scraper upserts. So it is a middle read, not a wide one.
    _check("FIELDS is a middle read, because it does not name jd",
           real_db._page_for(",".join(real_db.FIELDS)) == real_db.PAGE_ROW,
           "got %d" % real_db._page_for(",".join(real_db.FIELDS)))


def test_fetch_all_walks_every_page_whatever_the_size():
    """The page size must not change WHICH rows come back -- only how many trips it takes.

    This is the failure that would be silent and total: _fetch_all stops when a batch comes back
    shorter than the page it asked for, so a transport that quietly capped the limit would make
    the first page look like the last and the corpus would simply end at 1000 rows. There is no
    such cap in pgrest.limit_offset today -- `limit` goes straight into the SQL -- and this is
    what says so if one ever appears.
    """
    TOTAL = 4507                       # deliberately not a multiple of any page size

    class _Resp(object):
        def __init__(self, params):
            lo = int(params.get("offset") or 0)
            hi = min(lo + int(params.get("limit")), TOTAL)
            self._rows = [{"url": "u/%d" % i} for i in range(lo, hi)]

        def raise_for_status(self):
            pass

        def json(self):
            return self._rows

    class _Http(object):
        def __init__(self):
            self.calls = []

        def get(self, url, headers=None, params=None, timeout=None):
            self.calls.append(dict(params))
            return _Resp(params)

    for page in (7, 1000, 4000, 8000, TOTAL, TOTAL + 1):
        stub, saved = _Http(), real_db._http
        real_db._http = stub
        try:
            rows = real_db._fetch_all("t", {"select": "url"}, page=page)
        finally:
            real_db._http = saved
        _check("page=%-5d returns all %d rows (in %d request(s))"
               % (page, TOTAL, len(stub.calls)),
               len(rows) == TOTAL and rows[-1]["url"] == "u/%d" % (TOTAL - 1),
               "got %d rows" % len(rows))


def main():
    print("table gates")
    for fn in (test_page_size_is_chosen_from_row_width,
               test_fetch_all_walks_every_page_whatever_the_size,
               test_load_jobs_gate_matrix,
               test_load_jobs_by_urls_gate_matrix,
               test_the_two_loaders_agree,
               test_tables_not_keyed_on_url_are_ordered_on_their_own_key,
               test_has_a_description_and_missing_one_are_complements):
        fn()
    print("\n%s" % ("FAILED: " + ", ".join(FAILED) if FAILED else "all checks passed"))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
