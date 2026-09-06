"""
test_requeue_analysis.py — replacing a description must invalidate the reading derived from it.

Run it directly
    python test_requeue_analysis.py
or via pytest (functions are named test_*).

THE BUG THIS GUARDS. A description is immutable in the ordinary path: score_jobs fetches one
only when the `jd` column is empty, so jd_terms / exp_max_years / sponsor_jd are normally a true
reading of the text the row holds. Three paths break that on purpose — refetch_thin_jds replaces
a loading shell with the real posting, close_dead_jds writes back a recovered one or blanks a
junk one, and the extension's detail-import fills in a description a browser import arrived
without. After any of them the row still carried the OLD text's analysis, and nothing noticed,
because the queue asks "is jd empty" and this row's is not.

MEASURED, 2026-09-06: nine postings in the owner's "0 to 2 Years" feed asked for 3 to 10 years
in their own descriptions — one of them ten. Their exp_max_years was NULL, derived from a
shorter earlier copy that stated no number, and web._filter_rows KEEPS a row it holds no number
for (many genuine entry-level posts state none). The job page, which parses the current text
live, printed the requirement under a card the filter had just admitted.

The fix is db.requeue_analysis: clear match_score, which score_jobs already reads as "no run has
ever scored this row" (_new_only_targets' `unscored` set), so the next pass re-derives every
JD-derived column from the text now stored.

WHY EACH ASSERTION IS HERE rather than one happy-path check:

  * `keys=` on the write. _upsert drops None values when it merges duplicate urls and then
    infers the key union from what survives — so a column that is None on every row of a batch
    is not sent at all, and the stale value lives on. This is the failure mode that makes the
    whole function a silent no-op, and it looks completely fine in a diff.
  * jd_terms is NOT cleared. Blanking it would make the feed render "JD pending" for a row
    holding a perfectly good description; a NULL match_score is invisible to the feed, which
    ranks on the per-user numbers in user_scores.
  * the call sites. A helper nothing calls fixes nothing, and every one of these was found by
    grepping for update_jds rather than by reading, so "is it wired" is the question that
    actually failed. This is the file's ONE source-text assertion — everything above it drives
    the real function and reads the real payload. It checks that each writer names
    requeue_analysis with the same collection it just wrote, which is a fact about the code
    rather than about its formatting, so a rename or a reflow does not make it lie.
"""
import json
import os
import sys

import db as real_db

APP = os.path.dirname(os.path.abspath(__file__))
FAILED = []


def _check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  -- " + detail) if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


class _Resp(object):
    status_code = 200
    text = ""


class _FakeHTTP(object):
    """Replaces db._http WHOLESALE, because db._LazyHTTP.__getattr__ raises when no backend is
    configured -- so `real_db._http.post` is itself an error in the offline suite this has to
    run in. Swapping the object sidesteps the descriptor entirely."""

    def __init__(self, post):
        self.post = post


class _Capture(object):
    """Stand in for the HTTP POST and decode the body _upsert actually built.

    STUBBED AT THE WIRE, NOT AT _upsert, and that is the whole point of this file. The rows a
    caller hands _upsert do not carry the cleared column at all -- _upsert PROJECTS them onto
    the `keys` group at payload-build time (`{k: r.get(k) for k in keys}`), and that projection
    is the step that turns "url only" into "url plus an explicit null". Asserting on the rows
    going IN passes whether or not keys= was ever passed, which is exactly the bug it is meant
    to catch. The first draft of this test did that and went green against a broken write.
    """

    def __init__(self):
        self.payloads = []
        self.calls = 0

    def __call__(self, url, headers=None, params=None, data=None, timeout=None):
        self.calls += 1
        self.payloads.extend(json.loads(data))
        return _Resp()

    @property
    def rows(self):
        return self.payloads


def test_requeue_clears_match_score():
    cap = _Capture()
    real_http, real_remote = real_db._http, real_db.has_remote_db
    real_db._http, real_db.has_remote_db = _FakeHTTP(cap), lambda: True
    try:
        n = real_db.requeue_analysis(["u/one", "u/two"])
    finally:
        real_db._http, real_db.has_remote_db = real_http, real_remote

    _check("it reports the number of rows it re-queued", n == 2, "got %r" % (n,))
    _check("it went through the write path", cap.calls == 1, "%d call(s)" % cap.calls)
    _check("every url is in the payload",
           [r.get("url") for r in cap.rows] == ["u/one", "u/two"], repr(cap.rows))
    # THE ONE THAT MATTERS. Without keys= the None is dropped by _upsert's merge and the column
    # is never sent, so the stale score survives and the row is never re-queued.
    _check("the body carries match_score EXPLICITLY, as a null",
           all("match_score" in r and r["match_score"] is None for r in cap.rows), repr(cap.rows))
    _check("...and nothing else rides along",
           all(set(r) == {"url", "match_score"} for r in cap.rows), repr(cap.rows))
    # Clearing these would make the feed say "JD pending" for a row with a real description.
    _check("jd_terms / exp_max_years are NOT cleared",
           not any(k in r for r in cap.rows for k in ("jd_terms", "exp_max_years")),
           repr(cap.rows))


def test_requeue_is_a_noop_without_a_database():
    cap = _Capture()
    real_http, real_remote = real_db._http, real_db.has_remote_db
    real_db._http, real_db.has_remote_db = _FakeHTTP(cap), lambda: False
    try:
        n = real_db.requeue_analysis(["u/one"])
    finally:
        real_db._http, real_db.has_remote_db = real_http, real_remote
    # The CSV fallback copies truthy values only and so cannot express a clear at all
    # (update_job_fields' own docstring says so). Better to do nothing than to half-do it.
    _check("no write off a real database", cap.calls == 0 and n == 0, "%d call(s)" % cap.calls)


def test_requeue_ignores_empty_and_duplicate_urls():
    cap = _Capture()
    real_http, real_remote = real_db._http, real_db.has_remote_db
    real_db._http, real_db.has_remote_db = _FakeHTTP(cap), lambda: True
    try:
        n = real_db.requeue_analysis(["u/one", "", None, "u/one", "u/two"])
    finally:
        real_db._http, real_db.has_remote_db = real_http, real_remote
    # _upsert 500s the whole batch on a repeated url (Postgres 21000), so de-duplicating here is
    # not tidiness — a caller passing a dict's keys twice would take the write down.
    _check("blank urls dropped and duplicates collapsed",
           [r["url"] for r in cap.rows] == ["u/one", "u/two"] and n == 2, repr(cap.rows))


def test_every_out_of_band_jd_write_requeues():
    """A helper nothing calls fixes nothing. The one source-text check here -- see the module
    docstring for why this one is not driven behaviourally."""
    sites = (
        # (file, the write that replaces a description, the variable it must re-queue)
        ("scripts/refetch_thin_jds.py", "db.update_jds(fixed)", "db.requeue_analysis(fixed)"),
        ("scripts/close_dead_jds.py", "db.update_jds(recovered)", "db.requeue_analysis(recovered)"),
        ("scripts/close_dead_jds.py", 'db.update_jds({u: "" for u in clear})',
         "db.requeue_analysis(clear)"),
        # The extension's detail-import: a description arriving AFTER the row was scored.
        ("web.py", "db.update_jds(clean)", "db.requeue_analysis(clean)"),
        # THE ORIGIN OF THE 2026-09-04 BATCH. The sweep stores descriptions that arrived with
        # the listing and deliberately does NOT gate on `kept`, so for a posting already in the
        # table it REPLACES the text. 1,783 rows that day against 258 on a normal run.
        ("scraper/__init__.py", "db.update_jds(jds)", "db.requeue_analysis(held)"),
    )
    for path, write, requeue in sites:
        src = open(os.path.join(APP, path), encoding="utf-8").read()
        _check("%s: %s is present" % (path, write), write in src)
        _check("%s: ...and re-queues with %s" % (path, requeue), requeue in src)

    # The sweep's call MUST stay narrowed to rows that already existed. Unconditional, it would
    # re-analyse every listing description on every run (~206 ms/row) whether the text changed
    # or not -- and a row add_jobs() just inserted has a NULL match_score already, so it gains
    # nothing. This is the one call site where "wired up" is not sufficient.
    src = open(os.path.join(APP, "scraper/__init__.py"), encoding="utf-8").read()
    _check("the sweep re-queues only rows it ALREADY HELD, not what it just added",
           "held = [u for u in jds if u not in fresh]" in src
           and 'fresh = {r.get("url") for r in kept}' in src)


def main():
    print("requeue_analysis")
    for fn in (test_requeue_clears_match_score,
               test_requeue_is_a_noop_without_a_database,
               test_requeue_ignores_empty_and_duplicate_urls,
               test_every_out_of_band_jd_write_requeues):
        fn()
    print("\n%s" % ("FAILED: " + ", ".join(FAILED) if FAILED else "all checks passed"))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
