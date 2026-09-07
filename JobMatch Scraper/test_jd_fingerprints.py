"""
test_jd_fingerprints.py — a derived column must not be able to lie about which text it read.

Run it directly
    python test_jd_fingerprints.py
or via pytest (functions are named test_*).

WHAT THIS GUARDS. `jobs.jd_fp` is the fingerprint of the description a row STORES; `jobs.facts_fp`
is the fingerprint of the text its derived columns (exp_max_years, jd_terms, sponsor_jd,
sponsor_reason) were READ FROM. Equal means the reading is about the text we hold. Different means
it is about a text we replaced and never re-read -- which is what happened to ~2,300 rows on
2026-09-04, put nine postings asking 3 to 10 years into a "0 to 2 Years" search, and hid 117
genuinely entry-level ones from that same search.

Everything here is offline. The live-corpus half is scripts/check_derived.py, which is a script
rather than a suite for the reason db.list_users() taught: a test that finds nothing because it is
not connected is a green tick that proves nothing.
"""
import os
import sys

import db as real_db
from scripts.check_derived import classify, STALE, UNKNOWN, NOJD

APP = os.path.dirname(os.path.abspath(__file__))
FAILED = []


def _check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("  -- " + detail) if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


# ---- the fingerprint itself ----------------------------------------------------------------

def test_absence_is_not_a_fingerprint():
    # THE ONE THAT MATTERS MOST. If empty text hashed to a value, every row we have never fetched
    # a description for would carry a real jd_fp against a NULL facts_fp -- a permanent mismatch
    # -- and the query that is supposed to mean "these rows are lying" would return the entire
    # fetch backlog instead. It would be technically correct and completely useless.
    for empty in ("", None, "   ", "\n\t "):
        _check("no fingerprint for %r" % (empty,), real_db.jd_fingerprint(empty) is None)
    _check("...but real text does get one", bool(real_db.jd_fingerprint("a real description")))


def test_it_hashes_what_the_column_will_actually_hold():
    # update_jds truncates to JD_MAX_CHARS before storing. The scoring pass fingerprints the text
    # it FETCHED, which can be longer -- 8,879 chars is the longest measured on the live corpus.
    # Hash the uncapped copy at one end and the capped one at the other and every freshly fetched
    # long description reads as stale for ever, which is a tripwire that cries permanently.
    cap = real_db.JD_MAX_CHARS
    long_text = "x" * (cap + 900)
    _check("text over the cap fingerprints as its stored prefix",
           real_db.jd_fingerprint(long_text) == real_db.jd_fingerprint("x" * cap))
    _check("...and update_jds really does truncate to the same constant",
           "[:JD_MAX_CHARS]" in open(os.path.join(APP, "db.py"), encoding="utf-8").read())


def test_different_text_different_fingerprint():
    a = real_db.jd_fingerprint("Five years of experience required.")
    b = real_db.jd_fingerprint("One year of experience required.")
    _check("two descriptions do not collide", a != b and a and b)
    _check("the same description is stable", a == real_db.jd_fingerprint(
        "Five years of experience required."))


# ---- the three states check_derived reports -------------------------------------------------

def test_classify_separates_unknown_from_stale():
    # A NULL facts_fp is NOT a defect: it means no scoring pass has read the row since the column
    # existed, which is true of the WHOLE corpus on migration day. Reporting that as stale would
    # make the first run return 47,000 rows and teach everyone to ignore the tool.
    _check("agreeing row is not reported",
           classify({"jd_fp": "aaa", "facts_fp": "aaa"}) is None)
    _check("disagreeing row is STALE",
           classify({"jd_fp": "aaa", "facts_fp": "bbb"}) == STALE)
    _check("unread row is UNKNOWN, not stale",
           classify({"jd_fp": "aaa", "facts_fp": None}) == UNKNOWN)
    _check("row with no description at all is NOJD",
           classify({"jd_fp": None, "facts_fp": None}) == NOJD)
    # close_dead_jds blanks a junk description in both stores. The reading it left behind is
    # about a document the table no longer holds, so this one IS stale rather than "no jd".
    _check("a reading left behind after the text was CLEARED is STALE",
           classify({"jd_fp": None, "facts_fp": "bbb"}) == STALE)
    # Postgres NULL and the empty string both arrive as falsy over the two transports.
    _check("empty string is treated as NULL, not as a fingerprint",
           classify({"jd_fp": "", "facts_fp": ""}) == NOJD)


# ---- the writers -----------------------------------------------------------------------------

class _Resp(object):
    status_code = 200
    text = ""


class _FakeHTTP(object):
    """Replaces db._http wholesale -- _LazyHTTP.__getattr__ raises with no backend configured."""

    def __init__(self, post):
        self.post = post


def test_update_jds_stamps_the_text_it_writes():
    import json
    sent = []

    def post(url, headers=None, params=None, data=None, timeout=None):
        sent.extend(json.loads(data))
        return _Resp()

    real_http, real_remote = real_db._http, real_db.has_remote_db
    real_db._http, real_db.has_remote_db = _FakeHTTP(post), lambda: True
    real_db._jd_cache.clear()
    try:
        real_db.update_jds({"u/1": "Seven years of experience.", "u/2": ""})
    finally:
        real_db._http, real_db.has_remote_db = real_http, real_remote

    # BOTH TABLES NOW POST. update_jds writes jobs (url, jd, jd_fp) and then mirrors into
    # job_descriptions (url, jd, jd_chars, updated_at). Keying a single dict on url let the
    # mirror row overwrite the jobs row, and the assertion below then looked for jd_fp on a
    # payload that is not supposed to carry it. Take the rows that name the fingerprint column.
    by_url = {r["url"]: r for r in sent if "jd_fp" in r}
    mirrored = {r["url"]: r for r in sent if "jd_chars" in r}
    _check("both rows were written", set(by_url) == {"u/1", "u/2"}, repr(sorted(by_url)))
    # THE STAMP RIDES WITH THE TEXT. Two statements would leave a window in which the column and
    # its fingerprint disagree -- and a crash inside that window is indistinguishable from the
    # bug this whole mechanism exists to detect.
    _check("the description carries its fingerprint in the same row",
           by_url.get("u/1", {}).get("jd_fp") == real_db.jd_fingerprint(
               "Seven years of experience."),
           repr(by_url.get("u/1")))
    _check("an empty description stamps NULL, not a hash",
           by_url.get("u/2", {}).get("jd_fp") is None, repr(by_url.get("u/2")))
    # The mirror is a shadow copy until the backfill stamps completion, so it must carry the
    # same text -- a mirror that lags is the bug this whole revamp is about, one table down.
    _check("job_descriptions is mirrored with the same text",
           mirrored.get("u/1", {}).get("jd") == "Seven years of experience.", repr(mirrored))
    _check("...and with its length, which is what urls_with_jd filters on",
           mirrored.get("u/1", {}).get("jd_chars") == len("Seven years of experience."),
           repr(mirrored.get("u/1")))


def test_the_scoring_pass_stamps_and_distrusts_a_mismatched_cache():
    """_persist_derived must (a) write facts_fp and (b) refuse a cached reading of another text.

    Source-level, because driving _persist_derived needs a corpus, an idf and a database. The
    behaviour it pins is the one the 2026-09-03 re-derive notes had to handle by hand: "a stale
    file writes the OLD parser's answers straight back", whose only remedy was remembering to
    move jdmeta.json aside.
    """
    src = open(os.path.join(APP, "scraper", "score_jobs.py"), encoding="utf-8").read()
    _check("facts_fp is in the written column group", '"facts_fp")' in src)
    _check("...and in the payload", '"facts_fp": m.get("jd_fp")' in src)
    _check("a cache entry whose text does not match is not used",
           'if m is not None and m.get("jd_fp") != fp:' in src)
    _check("a freshly computed reading is stamped", 'm["jd_fp"] = fp' in src)
    _check("the analysis loop stamps what it writes to jdmeta",
           'm["jd_fp"] = db.jd_fingerprint(jd)' in src)


def test_the_backfill_agrees_with_the_python_fingerprint():
    """A column added by DDL and written by Python has two authors, and they can disagree.

    MIGRATION_jd_fingerprints.sql adds jd_fp and leaves it to db.update_jds, "on the next write
    of that row's description". A description is IMMUTABLE in normal operation -- score_jobs
    queues a fetch only when the column is empty -- so for the 46,849 rows already fetched there
    was no next write and the column stayed NULL for ever.

    The consequence was not an empty column, it was a LYING TOOL. check_derived.py reads a NULL
    jd_fp as "no description at all" and reported 100% of the corpus that way, on a database
    where 99.4% of rows have a description. This file's own docstring already warns that a tool
    whose failure mode is a reassuring number is worse than one that crashes; that warning was
    written about an earlier version of the same mistake.

    MIGRATION_jd_fp_backfill.sql computes it in SQL instead -- the input is already in the
    database, and the Python form would read 263 MB over the proxy to hand back 32 characters a
    row. Which means the hash now has two authors, and this pins the three ways they can drift.
    """
    mig = open(os.path.join(APP, "MIGRATION_jd_fp_backfill.sql"), encoding="utf-8").read()

    # 1. THE CAP. 158 stored descriptions are longer than JD_MAX_CHARS. Hash the untruncated
    #    text here and Python's capped text there, and those 158 read stale for ever.
    _check("the backfill truncates to JD_MAX_CHARS (%d)" % real_db.JD_MAX_CHARS,
           "left(d.jd, %d)" % real_db.JD_MAX_CHARS in mig,
           "constant moved and the SQL did not")

    # 2. IT MUST NEVER WRITE facts_fp. We know what text a row STORES; we do not know what text
    #    its exp_max_years was read from. Setting facts_fp = jd_fp would assert every derived
    #    column in the corpus is current -- the exact claim the pair exists to test, and the
    #    exact claim that was false on 2026-09-04.
    _check("it never stamps facts_fp",
           "facts_fp" not in mig.split("update public.jobs")[1].split(";")[0])

    # 3. IT ONLY FILLS NULLS, so re-running it cannot overwrite a stamp a real write made.
    _check("it only touches rows with no fingerprint yet", "j.jd_fp is null" in mig)

    # ...and the empty case still has to be absent, not a hash, on both sides.
    blank = chr(0xa0) + " " + chr(9) + chr(10)      # NBSP, space, tab, newline
    _check("Python still refuses to fingerprint whitespace",
           real_db.jd_fingerprint(blank) is None)
    _check("the SQL excludes whitespace-only text too", "btrim(d.jd" in mig)


def test_the_ddl_and_its_migration_agree():
    sql = real_db.JOBS_DERIVED_SQL
    mig = open(os.path.join(APP, "MIGRATION_jd_fingerprints.sql"), encoding="utf-8").read()
    for col in ("jd_fp", "facts_fp"):
        stmt = "alter table public.jobs add column if not exists %s text;" % col
        _check("JOBS_DERIVED_SQL adds %s" % col, stmt in sql)
        _check("MIGRATION_jd_fingerprints.sql adds %s" % col, stmt in mig)
    # db.py:262 marks this "LAST" -- without it PostgREST answers from its cached schema and
    # every column added above reads as missing until it happens to reload.
    for name, text in (("JOBS_DERIVED_SQL", sql), ("the migration file", mig)):
        _check("%s ends with the schema reload" % name,
               text.strip().endswith("notify pgrst, 'reload schema';"))
    # `<>` would answer NULL for a row with neither stamp and drop it from both sides.
    _check("the index uses `is distinct from`, not `<>`",
           "is distinct from facts_fp" in sql and "is distinct from facts_fp" in mig)


# ---- the deployment window: code shipped, migration not yet pasted --------------------------
#
# The migration is applied BY HAND in a SQL editor and the code ships in a zip, so there is no
# way to make the two atomic. Both writers therefore have to survive the column not being there,
# because the alternative is a scrape that stops storing descriptions (update_jds) or stops
# storing the readings the stamp exists to protect (_send_derived). This is the single most
# consequential pair of assertions in this file: everything else is wrong data, this is no data.

class _MissingColumn(Exception):
    """What PostgREST/psycopg actually answer for an un-migrated column."""

    def __repr__(self):
        # No escapes: this only has to satisfy db._column_missing, which looks for the
        # column name plus one of ('42703', 'undefined_column', 'does not exist').
        return "PgRestError: column " + self.args[0] + " does not exist (42703)"


def test_update_jds_survives_an_unmigrated_column():
    calls = []

    # table=/pk= are part of db._upsert's signature since the schema split, and _mirror_jds
    # passes them. A stub without them raises TypeError -- which, now that the description
    # write is authoritative and RAISES, would fail update_jds for a reason that has nothing
    # to do with what this test is about.
    def upsert(rows, chunk=200, keys=None, table=None, pk="url"):
        calls.append((table or real_db.TABLE, rows))
        if (table or real_db.TABLE) == real_db.TABLE and any("jd_fp" in r for r in rows):
            raise _MissingColumn("jd_fp")

    real_upsert, real_remote = real_db._upsert, real_db.has_remote_db
    real_db._upsert, real_db.has_remote_db = upsert, lambda: True
    real_db._fp_col["ok"] = True
    real_db._jd_cache.clear()
    try:
        real_db.update_jds({"u/1": "a description"})     # must NOT raise
        ok = True
    except Exception as e:
        ok = False
        print("      raised: %r" % (e,))
    finally:
        real_db._upsert, real_db.has_remote_db = real_upsert, real_remote
        real_db._fp_col["ok"] = True

    _check("a missing jd_fp column does not fail the description write", ok)
    # THE DESCRIPTION NOW LANDS IN job_descriptions, not on `jobs` -- the contract step moved
    # it, and update_jds writes it there FIRST so a failure cannot leave jobs.jd_fp claiming
    # provenance for text nobody stored. What `jobs` still gets is the fingerprint alone.
    _check("...and the description itself still went through",
           any(t == real_db.JD_TABLE and any("jd" in r for r in rs) for t, rs in calls),
           repr(calls))


def test_update_jds_still_raises_on_a_real_failure():
    # The retry must be narrow. If any exception dropped the column and carried on, a genuine
    # write failure would look like success and descriptions would go missing silently.
    def upsert(rows, chunk=200, keys=None, table=None, pk="url"):
        raise RuntimeError("connection reset by peer")

    real_upsert, real_remote = real_db._upsert, real_db.has_remote_db
    real_db._upsert, real_db.has_remote_db = upsert, lambda: True
    real_db._fp_col["ok"] = True
    real_db._jd_cache.clear()
    try:
        real_db.update_jds({"u/1": "a description"})
        raised = False
    except RuntimeError:
        raised = True
    finally:
        real_db._upsert, real_db.has_remote_db = real_upsert, real_remote
        real_db._fp_col["ok"] = True
    _check("a real write failure still raises", raised)


def test_the_derived_write_keeps_its_readings_when_the_stamp_column_is_missing():
    import scraper.score_jobs as sj
    seen = []

    def update_job_fields(rows, keys=None):
        seen.append((rows, keys))
        if any("facts_fp" in r for r in rows):
            raise _MissingColumn("facts_fp")

    real = sj.db.update_job_fields
    sj.db.update_job_fields = update_job_fields
    try:
        ok = sj._send_derived([{"url": "u/1", "exp_max_years": 5, "facts_fp": "aaa"}],
                              "JD fields", keys=sj.JD_DERIVED_COLS)
    finally:
        sj.db.update_job_fields = real

    _check("the derived write reports success after dropping the stamp", ok is True)
    _check("...and exp_max_years still landed",
           any("exp_max_years" in r and "facts_fp" not in r for rows, _ in seen for r in rows),
           repr(seen))
    _check("...with facts_fp removed from the named column group too",
           any(k is not None and "facts_fp" not in k for _, k in seen[1:]), repr(seen))


def test_a_narrow_read_never_widens_into_the_description_text():
    """load_jobs(cols=...) falling back must stop at the feed set, never reach select=*.

    Found while building check_derived.py against a database where the fingerprint columns did
    not exist yet. The cols= select failed, fell through with include_jd still at its default
    True, and read all 25 columns including 263 MB of description -- twice, ~300 MB of a 5 GB
    monthly egress budget, from a caller that had asked for six columns. The docstring had said
    for months that this path "falls back to the normal include_jd=False path"; the code did not.
    """
    asked = []

    def fetch_all(table, params):
        asked.append(params.get("select"))
        if len(asked) == 1:
            raise _MissingColumn("jd_fp")      # the narrow select names an unmigrated column
        return []

    real_fetch, real_remote = real_db._fetch_all, real_db.has_remote_db
    real_db._fetch_all, real_db.has_remote_db = fetch_all, lambda: True
    try:
        real_db.load_jobs(cols="url,jd_fp,facts_fp")     # include_jd defaults to True
    finally:
        real_db._fetch_all, real_db.has_remote_db = real_fetch, real_remote

    _check("the narrow select was tried first", asked and asked[0] == "url,jd_fp,facts_fp",
           repr(asked))
    _check("the fallback is NOT select=*", "*" not in asked[1:], repr(asked))
    _check("...it is the no-description feed set",
           len(asked) > 1 and "jd" not in (asked[1] or "").split(","), repr(asked[1:]))


def main():
    print("jd fingerprints")
    for fn in (test_absence_is_not_a_fingerprint,
               test_the_backfill_agrees_with_the_python_fingerprint,
               test_it_hashes_what_the_column_will_actually_hold,
               test_different_text_different_fingerprint,
               test_classify_separates_unknown_from_stale,
               test_update_jds_stamps_the_text_it_writes,
               test_the_scoring_pass_stamps_and_distrusts_a_mismatched_cache,
               test_the_ddl_and_its_migration_agree,
               test_update_jds_survives_an_unmigrated_column,
               test_update_jds_still_raises_on_a_real_failure,
               test_the_derived_write_keeps_its_readings_when_the_stamp_column_is_missing,
               test_a_narrow_read_never_widens_into_the_description_text):
        fn()
    print("\n%s" % ("FAILED: " + ", ".join(FAILED) if FAILED else "all checks passed"))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
