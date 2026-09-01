"""The 2026-08-21 speed changes must not have changed any ANSWER. Two of them replaced work
with a cache or a shortcut, and both are the kind that is right on the data you looked at:

  1. web.py::_row_pending reads jobs.jd_terms as a STRING instead of unpacking it.
  2. db.get_job_jd memoizes per url, including the empty result.
  3. core._stem and core._alias_forms are lru_cached. _stem was 3.5M calls and 16.7s of a single
     35s ranked_rows rebuild, so this is the largest of the three -- and the one where a wrong
     answer would silently move every match percentage in the product.
  4. web.user_scores persists to a file, so a worker that has never scored a user reads it
     instead of spending 5-15 seconds. A wrong answer here is a wrong match percentage on
     every card, so the stored scores are compared against freshly computed ones.
  5. web._cache_max() bounds the per-user caches by BYTES, derived from the live row count.
     The old count cap allowed ~2.9 GB at today's corpus and got worse with every scrape, so
     what matters is that the budget holds at any corpus size -- including one where a single
     entry is bigger than the whole budget.

Run it with the local snapshot present and (1) is checked against every real row.

_row_pending reads jobs.jd_terms as a STRING instead of unpacking it, which is the whole saving
(a ranked_rows rebuild used to unpack the corpus twice — once in user_scores, once in _build_row).
A string test standing in for a parse is exactly the kind of shortcut that is right on the data
you looked at and wrong on the row you didn't, so this checks every row in the local snapshot
rather than a handful of literals.

    python scripts/test_speed_caches.py

Falls back to synthetic cases when jobs_snapshot.json.gz is absent (CI has no database and no
snapshot — see docs/INDEX.md on tests that read the live corpus).
"""
import os
import sys
import gzip
import json
import shutil
import hashlib
import tempfile

os.environ.setdefault("EV_OFF", "1")             # analytics reads this at import, once
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core
import db
import web


def old_pending(j):
    """The expression _row_pending replaced, verbatim from _build_row."""
    _an = web.job_analysis(j)
    return bool(_an.get("thin")) or not _an


def check(rows, label):
    bad = []
    for j in rows:
        want, got = old_pending(j), web._row_pending(j)
        if want != got:
            bad.append((j.get("url"), repr(j.get("jd_terms"))[:120], want, got))
    print("  %-22s %6d rows, %d disagreement(s)" % (label, len(rows), len(bad)))
    for u, t, want, got in bad[:10]:
        print("    %s\n      jd_terms=%s\n      old=%s new=%s" % (u, t, want, got))
    return bad


def synthetic():
    """The shapes pack_analyzed can emit, plus the ones it cannot but the column might hold."""
    packed_ok = core.pack_analyzed({"weight": {"python": 1.0, "sql": 0.5}, "thin": False})
    packed_thin = core.pack_analyzed({"weight": {"python": 1.0}, "thin": True})
    assert packed_ok.endswith('"n":0}'), packed_ok
    assert packed_thin.endswith('"n":1}'), packed_thin
    return [
        {"url": "u1", "jd_terms": packed_ok},          # readable
        {"url": "u2", "jd_terms": packed_thin},        # thin
        {"url": "u3", "jd_terms": ""},                 # never scored
        {"url": "u4", "jd_terms": None},               # NULL column
        {"url": "u5"},                                 # column absent
        {"url": "u6", "jd_terms": "{not json"},        # malformed
        {"url": "u7", "jd_terms": '{"w":{},"n":0}'},   # empty w: pack_analyzed never writes this
        {"url": "u8", "jd_terms": '{"n":0}'},          # no w at all
        {"url": "u9", "jd_terms": '{"w":{"a":1},"n":0} '},   # trailing space
        {"url": "u10", "jd_terms": {"w": {"a": 1}, "n": 0}},  # dict, not str
        {"url": "u11", "jd_terms": '{"w":{"a":1}}'},   # no n key
    ]


def jd_cache():
    """db.get_job_jd's memo: hits, cached misses, eviction on write, no caching of errors.

    The cached MISS is the one worth pinning. A row with no stored description is the row the
    job page renders most often, so not caching it would leave the most common case paying the
    round trip every time -- and caching it wrongly would pin "no description" forever.
    """
    print("=" * 74)
    print("db.get_job_jd memo")
    print("=" * 74)
    bad = []
    def want(name, cond, extra=""):
        print("  %s %-46s %s" % ("ok " if cond else "FAIL", name, extra))
        if not cond:
            bad.append(name)

    calls = [0]
    store = {"u/full": "a real description", "u/empty": ""}

    def fake_fetch(table, params):
        calls[0] += 1
        u = params["url"].split("eq.", 1)[1]
        return [{"jd": store[u]}] if u in store else []

    # _upsert MUST be stubbed, not just _fetch_all. update_jds() below reaches the real write
    # path otherwise: on a laptop with a .env that means this test silently INSERTS its own
    # fixture rows into the real jobs table (it did -- "u/full" and one other had to be deleted
    # afterwards), and in CI, where there are no credentials, _upsert retries three times with
    # 3+6+9s of sleeps and then raises, so the suite fails after stalling for eighteen seconds.
    wrote = []
    real_fetch, real_supa, real_upsert = db._fetch_all, db.using_supabase, db._upsert
    db._fetch_all, db.using_supabase = fake_fetch, lambda: True
    db._upsert = lambda rows, chunk=200: wrote.extend(rows)
    db._jd_cache.clear()
    try:
        want("a JD is read once and remembered",
             [db.get_job_jd("u/full") for _ in range(2)] == ["a real description"] * 2
             and calls[0] == 1, "%d DB call(s) for 2 reads" % calls[0])

        calls[0] = 0
        want("an EMPTY JD is remembered too",
             [db.get_job_jd("u/empty") for _ in range(2)] == ["", ""] and calls[0] == 1,
             "%d DB call(s) for 2 reads" % calls[0])

        store["u/full"] = "REWRITTEN"
        db.update_jds({"u/full": "REWRITTEN"})
        want("update_jds evicts what it wrote", db.get_job_jd("u/full") == "REWRITTEN")
        want("...and it really did go through the write path",
             [r.get("url") for r in wrote] == ["u/full"], repr(wrote[:1]))

        def boom(table, params):
            raise RuntimeError("transient")
        db._fetch_all = boom
        want("a failed read is NOT cached",
             db.get_job_jd("u/err") == "" and "u/err" not in db._jd_cache)

        db._fetch_all = fake_fetch
        for i in range(db._JD_CACHE_MAX * 3):
            db.get_job_jd("u/pad%d" % i)
        want("the cache stays bounded", len(db._jd_cache) <= db._JD_CACHE_MAX,
             "%d entries, max %d" % (len(db._jd_cache), db._JD_CACHE_MAX))
    finally:
        db._fetch_all, db.using_supabase, db._upsert = real_fetch, real_supa, real_upsert
        db._jd_cache.clear()
    print()
    return bad


def scorer_memos():
    """The memoized scorer helpers must be pure, and score_against must not care about cache state.

    A memo on a function that turns out to depend on anything but its argument does not crash --
    it quietly returns yesterday's answer, and here that would move every match percentage in the
    product. So: same inputs, cleared caches, identical output.
    """
    print("=" * 74)
    print("core._stem / core._alias_forms memos")
    print("=" * 74)
    bad = []
    def want(name, cond, extra=""):
        print("  %s %-46s %s" % ("ok " if cond else "FAIL", name, extra))
        if not cond:
            bad.append(name)

    words = ["budgeting", "management", "managing", "manager", "kpis", "analytics", "ops", "aws",
             "sas", "analysis", "series", "ration", "scheduling", "stakeholders", "", "a"]
    terms = ["ms project", "microsoft project", "sql", "power bi", "c++", "node.js", "agile"]

    core._stem.cache_clear(); core._alias_forms.cache_clear()
    cold_s = [core._stem(w) for w in words]
    cold_a = [core._alias_forms(t) for t in terms]
    warm_s = [core._stem(w) for w in words]
    core._stem.cache_clear(); core._alias_forms.cache_clear()
    recold_s = [core._stem(w) for w in words]
    recold_a = [core._alias_forms(t) for t in terms]

    want("_stem is deterministic across cache_clear", cold_s == warm_s == recold_s)
    want("_alias_forms is deterministic too", cold_a == recold_a)
    want("_stem still refuses to over-stem",
         core._stem("ops") == "ops" and core._stem("aws") == "aws"
         and core._stem("analysis") == "analysis",
         "ops/aws/analysis unchanged")
    want("_stem still folds the variants it must",
         core._stem("kpis") == "kpi" and core._stem("budgeting") == "budget"
         and core._stem("management") == core._stem("managing") == core._stem("manager"))
    # It returns a TUPLE now, and the memo means every caller shares one object.
    want("_alias_forms returns an immutable tuple",
         all(isinstance(x, tuple) for x in cold_a))
    want("...and the same object each time (so no caller may mutate it)",
         core._alias_forms("ms project") is core._alias_forms("ms project"))
    want("an alias and its canonical form agree",
         set(core._alias_forms("ms project")) == set(core._alias_forms("microsoft project")))

    # The thing that actually matters: a score must not depend on cache state.
    resume = "managed multiple projects, built budgets, wrote sql and python, used power bi"
    an = core.unpack_analyzed('{"w":{"project management":1.0,"sql":0.8,"power bi":0.6,'
                              '"kubernetes":0.4},"n":0}')
    core._stem.cache_clear(); core._alias_forms.cache_clear()
    a = core.score_against(resume, an)
    b = core.score_against(resume, an)
    core._stem.cache_clear(); core._alias_forms.cache_clear()
    c = core.score_against(resume, an)
    want("score_against is identical cold, warm and re-cold",
         a == b == c, "score=%s" % (a[0],))
    print()
    return bad


def score_files():
    """web._scores_read/_scores_write must return EXACTLY what scoring produced, or nothing.

    The failure mode this guards is not a crash. A file keyed a little too loosely serves one
    resume's scores for another, or last week's corpus for this week's, and the feed shows
    confident percentages that are simply wrong -- which is worse than being slow, and is why
    the fingerprint is half the key.
    """
    print("=" * 74)
    print("web.user_scores persistent file")
    print("=" * 74)
    bad = []

    def want(name, cond, extra=""):
        print("  %s %-46s %s" % ("ok " if cond else "FAIL", name, extra))
        if not cond:
            bad.append(name)

    rows = [{"url": "https://b.example/%d" % i, "title": "Project Manager %d" % i,
             "company": "Acme", "location": "Boston, MA", "match_score": 40 + (i % 20),
             "jd_terms": '{"w":{"python":1.0,"sql":0.5,"roadmap":0.4},"n":0}'}
            for i in range(120)]
    FP = (len(rows), "2026-08-17")
    RESUME = "python sql roadmap stakeholder delivery"

    tmp = tempfile.mkdtemp(prefix="jm_scores_")
    real_get, real_dir = web.get_jobs, web._SCORES_DIR
    real_cache_rows, real_cache_fp = web._jobs_cache.get("rows"), web._jobs_cache.get("fp")
    web.get_jobs = lambda: rows
    web._SCORES_DIR = tmp
    web._jobs_cache["rows"], web._jobs_cache["fp"] = rows, FP
    web._score_cache.clear()
    try:
        computed = dict(web.user_scores("a@t", RESUME))
        want("scoring produced a score per row", len(computed) == len(rows),
             "%d of %d" % (len(computed), len(rows)))
        files = [n for n in os.listdir(tmp) if n.endswith(".json.gz")]
        want("...and wrote exactly one file", len(files) == 1, repr(files))

        web._score_cache.clear()                     # force the read path
        from_disk = dict(web.user_scores("a@t", RESUME))
        want("the stored scores are IDENTICAL", from_disk == computed,
             "%d differ" % len([u for u in computed if computed[u] != from_disk.get(u)]))

        rmd5 = hashlib.md5(RESUME.encode("utf-8")).hexdigest()
        want("a moved corpus is refused",
             web._scores_read("a@t", rmd5, (len(rows) + 1, "2026-08-18")) is None)
        want("no fingerprint means no read at all",
             web._scores_read("a@t", rmd5, None) is None)
        want("another user does not read this file",
             web._scores_read("b@t", rmd5, FP) is None)
        want("another resume does not read this file",
             web._scores_read("a@t", hashlib.md5(b"different").hexdigest(), FP) is None)

        # A corrupt file must recompute, not raise and not poison the feed.
        with open(web._scores_path("a@t", rmd5), "wb") as fh:
            fh.write(b"not gzip at all")
        want("a corrupt file is ignored", web._scores_read("a@t", rmd5, FP) is None)
        web._score_cache.clear()
        want("...and scoring still returns the right answers",
             dict(web.user_scores("a@t", RESUME)) == computed)

        # The directory must not grow without bound.
        for i in range(web._SCORES_MAX_FILES + 20):
            web._scores_write("pad%d@t" % i, rmd5, FP, {"u": 1})
        n = len([x for x in os.listdir(tmp) if x.endswith(".json.gz")])
        want("the directory stays bounded", n <= web._SCORES_MAX_FILES,
             "%d files, max %d" % (n, web._SCORES_MAX_FILES))

        want("_scores_clear removes them all",
             (web._scores_clear() or True)
             and not [x for x in os.listdir(tmp) if x.endswith(".json.gz")])
    finally:
        web.get_jobs, web._SCORES_DIR = real_get, real_dir
        web._jobs_cache["rows"], web._jobs_cache["fp"] = real_cache_rows, real_cache_fp
        web._score_cache.clear()
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    return bad


def cache_budget():
    """_cache_max() must never authorise more than CACHE_BUDGET_MB, at ANY corpus size.

    The bug being guarded is not "the number is wrong", it is "the number is in the wrong
    unit". A cap of 64 entries was harmless at 2,674 rows and was ~2.9 GB at 21,982, in a
    process shared hosting caps under 1 GB -- and nothing about the constant changed in
    between. So the assertion is on the BYTES the cap permits, across a range that brackets
     both the corpus this app started with and one several times larger than today's.
    """
    print("=" * 74)
    print("web._cache_max byte budget")
    print("=" * 74)
    bad = []

    def want(name, cond, extra=""):
        print("  %s %-46s %s" % ("ok " if cond else "FAIL", name, extra))
        if not cond:
            bad.append(name)

    real_rows = web._jobs_cache.get("rows")
    budget = web._CACHE_BUDGET_MB
    try:
        # The exact invariant, and the edge is real: once ONE entry is larger than the whole
        # budget there is no cap that honours it, and the right answer is 1 rather than 0 --
        # a cache of nothing recomputes on every single request. So: never more than the budget,
        # EXCEPT when a single entry already exceeds it, where the cap must be exactly 1.
        over = []
        for n in (2674, 13197, 21982, 40000, 100000, 250000):
            web._jobs_cache["rows"] = [None] * n
            cap = web._cache_max()
            per_mb = max(1.0, n * web._ROW_CACHE_BYTES_PER_ROW / 1048576.0)
            if per_mb > budget:
                if cap != 1:
                    over.append((n, cap, "one entry exceeds the budget; cap must be 1"))
            elif cap * per_mb > budget:
                over.append((n, cap, round(cap * per_mb)))
            elif cap < 1:
                over.append((n, cap, "cap below 1"))
        want("the budget holds wherever it can hold", not over, repr(over[:3]))
        web._jobs_cache["rows"] = [None] * 250000
        want("...and degrades to exactly 1 when it cannot", web._cache_max() == 1,
             "cap %d at 250k rows" % web._cache_max())

        # It must actually SHRINK as the corpus grows -- that is the whole point.
        caps = []
        for n in (2674, 21982, 100000):
            web._jobs_cache["rows"] = [None] * n
            caps.append(web._cache_max())
        want("the cap shrinks as the corpus grows", caps == sorted(caps, reverse=True),
             "2.7k/22k/100k rows -> %s" % (caps,))

        # Never zero: a cache of nothing recomputes on literally every request.
        web._jobs_cache["rows"] = [None] * 5000000
        want("never drops to zero", web._cache_max() >= 1, "cap %d" % web._cache_max())

        # An unread corpus must not be treated as an empty one.
        web._jobs_cache["rows"] = None
        want("an unread corpus assumes a large one, not none",
             web._cache_max() <= web._SCORE_CACHE_CEIL and web._cache_max() >= 1,
             "cap %d" % web._cache_max())

        want("the ceiling still applies", web._cache_max() <= web._SCORE_CACHE_CEIL)
    finally:
        web._jobs_cache["rows"] = real_rows
    print()
    return bad


def base_rows():
    """web._base_rows() is shared and web.ranked_rows() must not have changed any ANSWER.

    The saving: _build_row emits 41 keys and exactly ONE of them, `score`, depends on who is
    asking. Deriving the other 40 per (user, resume) cost 1,941 ms per user per worker at
    21,960 rows. They are built once per corpus now and ranked_rows overlays the score.

    That is only safe if the output is IDENTICAL, so this rebuilds the old way -- _build_row
    with the real score, then dedupe, then sort -- and compares field by field and position by
    position. It is the same shape of check as score_files(): the fast path is only worth
    anything if it agrees with the slow one on every row.

    THE ORDERING TRAP THIS PINS. _dupe_rank tie-breaks on r["score"], so folding duplicates
    while every score is still 0 picks a different survivor. The dedupe therefore has to run
    AFTER the overlay, and a future refactor that moves it into _base_rows() for speed would
    pass every other test in this file.
    """
    print("=" * 74)
    print("web._base_rows sharing + ranked_rows equivalence")
    print("=" * 74)
    bad = []

    def want(name, cond, extra=""):
        print("  %s %-46s %s" % ("ok " if cond else "FAIL", name, extra))
        if not cond:
            bad.append(name)

    snap = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "jobs_snapshot.json.gz")
    if os.path.exists(snap):
        with gzip.open(snap, "rt", encoding="utf-8") as fh:
            rows = (json.load(fh) or {}).get("rows") or []
    else:
        rows = [{"url": "u%d" % i, "title": "Data Engineer %d" % i, "company": "Acme %d" % (i % 7),
                 "location": "Boston, MA", "found_date": "2026-08-0%d" % (1 + i % 9),
                 "jd_terms": '{"w":{"python":2,"sql":1},"n":2}', "match_score": i % 100}
                for i in range(60)]
        print("  (no snapshot -- %d synthetic rows)" % len(rows))

    saved = (dict(web._jobs_cache), dict(web._base_rows_cache))
    try:
        web._jobs_cache["rows"], web._jobs_cache["at"] = rows, 10 ** 12
        web._jobs_cache["fp"] = (len(rows), "test")
        web._base_rows_cache.update(fp=None, rows=None)
        web._rows_cache.clear()
        web._score_cache.clear()

        b1 = web._base_rows()
        b2 = web._base_rows()
        want("same corpus reuses the built rows", b1 is b2, "%d rows" % len(b1))
        want("every score in the base is 0", all(r["score"] == 0 for r in b1))

        # A moved corpus must replace it, or a scrape would serve yesterday's cards.
        web._jobs_cache["fp"] = (len(rows), "moved")
        want("a moved fingerprint rebuilds", web._base_rows() is not b1)

        # "Don't know" is db.jobs_fingerprint()'s (None, "") -- a TRUTHY tuple, which is the
        # whole reason the guard tests fp[0] rather than fp.
        web._jobs_cache["fp"] = (None, "")
        web._base_rows_cache.update(fp=None, rows=None)
        web._base_rows()
        want("an unavailable fingerprint is not a key", web._base_rows_cache["fp"] is None)

        # ...and equivalence, against the algorithm this replaced.
        web._jobs_cache["fp"] = (len(rows), "test")
        web._base_rows_cache.update(fp=None, rows=None)
        web._rows_cache.clear()
        scores = {r["url"]: (i * 7) % 101 for i, r in enumerate(rows) if r.get("url")}
        web._score_cache[("u", hashlib.md5(b"cv").hexdigest())] = scores
        got = web.ranked_rows("u", "cv")

        ref = [web._build_row(j, scores.get(j.get("url"), 0)) for j in rows if j.get("url")]
        ref = web._dedupe_rows(ref)
        ref.sort(key=lambda r: r["score"], reverse=True)

        want("row COUNT matches", len(got) == len(ref), "%d vs %d" % (len(got), len(ref)))
        want("row ORDER matches",
             [r["url"] for r in got] == [r["url"] for r in ref])
        diff = sum(1 for a, b in zip(got, ref) if a != b)
        want("no row differs in ANY field", diff == 0, "%d differing" % diff)

        # The point of the whole thing: two users share the underlying build.
        web._score_cache[("v", hashlib.md5(b"cv2").hexdigest())] = scores
        web.ranked_rows("v", "cv2")
        want("a second user did not rebuild the base", web._base_rows() is web._base_rows_cache["rows"])
        want("per-user rows are copies, not the shared dicts",
             all(g is not b for g, b in zip(got[:20], web._base_rows()[:20])))
    finally:
        web._jobs_cache.clear()
        web._jobs_cache.update(saved[0])
        web._base_rows_cache.update(saved[1])
        web._rows_cache.clear()
        web._score_cache.clear()
    return bad


def main():
    fails = check(synthetic(), "synthetic")

    snap = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "jobs_snapshot.json.gz")
    if os.path.exists(snap):
        with gzip.open(snap, "rt", encoding="utf-8") as fh:
            rows = (json.load(fh) or {}).get("rows") or []
        fails += check(rows, "live snapshot")
        n_pending = sum(1 for j in rows if web._row_pending(j))
        print("  pending on the real corpus: %d of %d (%.1f%%)"
              % (n_pending, len(rows), 100.0 * n_pending / max(1, len(rows))))
    else:
        print("  live snapshot            SKIPPED (no jobs_snapshot.json.gz)")

    # _jdmeta wins over the column, and _row_pending must honour that too.
    web._jdmeta["jm1"] = {"analyzed": {"terms": ["a"], "thin": False}}
    web._jdmeta["jm2"] = {"analyzed": {"terms": ["a"], "thin": True}}
    jd_rows = [{"url": "jm1", "jd_terms": '{"w":{"a":1},"n":1}'},   # column thin, jdmeta not
               {"url": "jm2", "jd_terms": '{"w":{"a":1},"n":0}'}]   # column fine, jdmeta thin
    fails += check(jd_rows, "jdmeta override")
    web._jdmeta.clear()

    fails += jd_cache()
    fails += scorer_memos()
    fails += score_files()
    fails += cache_budget()
    fails += base_rows()
    print("FAIL: %d problem(s)" % len(fails) if fails else "PASS: all speed caches are faithful")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
