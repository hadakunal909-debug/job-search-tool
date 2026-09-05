#!/usr/bin/env python3
"""The stored per-(user, job) match score: db.user_scores, and web.user_scores reading it.

    python scripts/test_user_scores.py

The property that matters most here is NOT that a stored score is served. It is that a stored
score computed against a resume the user has since replaced is NOT served -- because that is the
one way this table can be worse than the cache it replaces. A cache keyed on the resume's hash
self-invalidates; a table has to be asked the right question, and the whole design rests on
every read filtering on resume_fp.

Offline throughout: db falls back to a JSON file when there is no remote database, which is the
same code path with the same semantics and no network.
"""
import os
import sys
import json
import shutil
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("EV_OFF", "1")
os.environ.setdefault("APP_SECRET", "test-user-scores-not-a-real-key")

import core                                       # noqa: E402
import db                                         # noqa: E402
import web                                        # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print("  %s %-56s %s" % ("ok " if cond else "FAIL", name, extra))
    if not cond:
        FAILS.append(name)


RESUME_A = ("program management stakeholder roadmap analytics sql python delivery "
            "budgets operations healthcare portfolio governance") * 3
RESUME_B = "welding fabrication tig mig blueprint reading shop floor safety osha" * 3
JD = ("We are hiring a program manager to own the delivery roadmap, work with stakeholders "
      "across the organisation, run sql and python reporting, and manage budgets and staffing "
      "for the healthcare operations portfolio. Governance experience required. ") * 6

tmp = tempfile.mkdtemp(prefix="userscores-")
saved = (db.USER_SCORES_FILE, db.has_remote_db, dict(web._jobs_cache),
         web._SCORES_DIR, web._ROWS_DIR)
try:
    db.USER_SCORES_FILE = os.path.join(tmp, "user_scores_local.json")
    db.has_remote_db = lambda: False
    web._SCORES_DIR = os.path.join(tmp, "score_cache")
    web._ROWS_DIR = os.path.join(tmp, "row_cache")
    os.makedirs(web._SCORES_DIR, exist_ok=True)
    os.makedirs(web._ROWS_DIR, exist_ok=True)

    print("=" * 74)
    print("db.user_scores — the store")
    print("=" * 74)

    fp_a, fp_b = db.resume_fp(RESUME_A), db.resume_fp(RESUME_B)
    check("resume_fp is the md5 web.user_scores already keys its own caches on",
          fp_a == __import__("hashlib").md5(RESUME_A.encode("utf-8")).hexdigest(),
          "so the stored rows and the in-process ones cannot disagree")
    check("two different resumes get two different fingerprints", fp_a != fp_b)

    db.save_user_scores("u1", fp_a, {"https://j/1": 71, "https://j/2": 12})
    check("what was written is what comes back",
          db.get_user_scores("u1", fp_a) == {"https://j/1": 71, "https://j/2": 12})
    check("THE SAFETY PROPERTY: a score for another resume is not served",
          db.get_user_scores("u1", fp_b) == {},
          "a row about a document the user replaced must read as missing")
    check("another user's scores are not served", db.get_user_scores("u2", fp_a) == {})
    check("clearing by url drops it for everyone",
          db.clear_scores_for_urls(["https://j/1"]) >= 1
          and db.get_user_scores("u1", fp_a) == {"https://j/2": 12})

    print()
    print("=" * 74)
    print("web.user_scores — the read path")
    print("=" * 74)

    idf = core.load_idf()
    packed = core.pack_analyzed(core.job_meta(JD, idf)["analyzed"])
    JOBS = [{"url": "https://j/%d" % i, "title": "Program Manager", "company": "Acme",
             "location": "Boston, MA", "is_active": True, "match_score": 40,
             "jd_terms": packed} for i in range(1, 4)]
    web.get_jobs = lambda force=False: [dict(j) for j in JOBS]
    web._jobs_cache.update(rows=[dict(j) for j in JOBS], fp=(len(JOBS), "userscores"),
                           at=10 ** 12)
    web._score_cache.clear()
    web._rows_cache.clear()
    # WIPED, because the store section above left a row for https://j/2 and "with nothing
    # stored" has to actually be true or the baseline it establishes is the store's own answer.
    # The first draft of this test read 12 there, believed it was the scorer's number, and then
    # failed the NEXT check for disagreeing with itself.
    if os.path.exists(db.USER_SCORES_FILE):
        os.remove(db.USER_SCORES_FILE)

    live = web.user_scores("u1", RESUME_A)
    check("with nothing stored it still scores, exactly as before",
          all(live.get(j["url"], 0) > 0 for j in JOBS),
          " ".join("%s=%s" % (j["url"][-3:], live.get(j["url"])) for j in JOBS))

    # Store a DIFFERENT number than the scorer would produce, so "served from the store" is
    # distinguishable from "recomputed and happened to match".
    db.save_user_scores("u1", db.resume_fp(RESUME_A), {"https://j/1": 99})
    web._score_cache.clear()
    web._scores_clear() if hasattr(web, "_scores_clear") else None
    shutil.rmtree(web._SCORES_DIR, ignore_errors=True)
    os.makedirs(web._SCORES_DIR, exist_ok=True)
    served = web.user_scores("u1", RESUME_A)
    check("a stored score is SERVED, not recomputed", served.get("https://j/1") == 99,
          "got %s, and the scorer would have said %s"
          % (served.get("https://j/1"), live.get("https://j/1")))
    check("...while a job with no stored row is still computed",
          served.get("https://j/2") == live.get("https://j/2"),
          "%s" % served.get("https://j/2"))

    # The same stored row, asked for with a different resume, must not leak.
    web._score_cache.clear()
    shutil.rmtree(web._SCORES_DIR, ignore_errors=True)
    os.makedirs(web._SCORES_DIR, exist_ok=True)
    other = web.user_scores("u1", RESUME_B)
    check("THE SAFETY PROPERTY, through the feed: an edited resume re-scores",
          other.get("https://j/1") != 99,
          "got %s -- 99 was scored against the OTHER resume" % other.get("https://j/1"))

    print()
    print("=" * 74)
    print("degradation — the table does not exist yet")
    print("=" * 74)

    def boom(*_a, **_k):
        raise RuntimeError('relation "user_scores" does not exist')

    saved_get = db.get_user_scores
    try:
        db.get_user_scores = boom
        web._score_cache.clear()
        shutil.rmtree(web._SCORES_DIR, ignore_errors=True)
        os.makedirs(web._SCORES_DIR, exist_ok=True)
        fallback = web.user_scores("u1", RESUME_A)
        check("a store that raises does not break the feed",
              all(fallback.get(j["url"], 0) > 0 for j in JOBS),
              "every score derived, exactly as before the table existed")
    finally:
        db.get_user_scores = saved_get

    check("_table_missing recognises an unrun migration",
          db._table_missing(RuntimeError('relation "user_scores" does not exist')))
    check("...and a proxy that predates the table",
          db._table_missing(RuntimeError("table not allowed: user_scores")),
          "same 'not available yet' state, same quiet handling")
    check("...but not a real failure",
          not db._table_missing(RuntimeError("connection reset by peer")))
finally:
    (db.USER_SCORES_FILE, db.has_remote_db, _jc, web._SCORES_DIR, web._ROWS_DIR) = saved
    web._jobs_cache.clear()
    web._jobs_cache.update(_jc)
    web._score_cache.clear()
    web._rows_cache.clear()
    shutil.rmtree(tmp, ignore_errors=True)

print()
if FAILS:
    print("FAILURES (%d): %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1)
print("ALL USER SCORE CHECKS PASS")
