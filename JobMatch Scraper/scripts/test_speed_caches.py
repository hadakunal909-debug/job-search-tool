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
import io
import sys
import time
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
    real_fetch, real_supa, real_upsert = db._fetch_all, db.has_remote_db, db._upsert
    db._fetch_all, db.has_remote_db = fake_fetch, lambda: True
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
        db._fetch_all, db.has_remote_db, db._upsert = real_fetch, real_supa, real_upsert
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
        files = [n for n in os.listdir(tmp) if web._rows_stale(n)]
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
        n = len([x for x in os.listdir(tmp) if web._rows_stale(x)])
        want("the directory stays bounded", n <= web._SCORES_MAX_FILES,
             "%d files, max %d" % (n, web._SCORES_MAX_FILES))

        want("_scores_clear removes them all",
             (web._scores_clear() or True)
             and not [x for x in os.listdir(tmp) if web._rows_stale(x)])
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
                 # n is the THIN flag (0 = scoreable). A truthy n makes _row_pending true
                 # and _build_row forces score to 0, which would leave every row here tied.
                 "jd_terms": '{"w":{"python":2,"sql":1},"n":0}', "match_score": i % 100}
                for i in range(60)]
        print("  (no snapshot -- %d synthetic rows)" % len(rows))

    # ONE POSTING FROM TWO HOSTS, APPENDED WHATEVER THE CORPUS IS.
    #
    # Without this the whole point of the suite is vacuous on CI. _dedupe_rows only collapses a
    # group spanning more than one host, the 60 synthetic rows above are all distinct, and CI has
    # no snapshot -- so "dedupe runs after the overlay" was asserted against a dedupe that
    # collapsed nothing, and a regression moving it into _base_rows() would have passed.
    #
    # Greenhouse serving one posting as both boards.greenhouse.io and job-boards.greenhouse.io is
    # the real case _dedupe_rows documents. Neither host is in _AGGREGATOR_HOSTS and neither row
    # carries date_verified, so _dupe_rank falls through to the SCORE -- which is exactly the
    # tie-break that does not exist yet while the base rows are all sitting at 0.
    _DUPE = {"title": "Staff Platform Engineer", "company": "Dupeco",
             "location": "Boston, MA, United States", "found_date": "2026-08-15",
             "jd_terms": '{"w":{"python":3,"kubernetes":2},"n":0}', "match_score": 0}
    DUPE_LOSER = "https://boards.greenhouse.io/dupeco/jobs/1"
    DUPE_WINNER = "https://job-boards.greenhouse.io/dupeco/jobs/1"
    rows = list(rows) + [dict(_DUPE, url=DUPE_LOSER), dict(_DUPE, url=DUPE_WINNER)]

    saved = (dict(web._jobs_cache), dict(web._base_rows_cache),
             web._jd_blocked_hosts, web._repost_clusters)
    try:
        # OFFLINE BY CONSTRUCTION, not by luck. _build_row reaches for the database in exactly two
        # places, both db.get_kv against a KV row and both memoised for the life of the worker:
        # _repost_count (repost_clusters) and _host_jd_blocked (jd_host_verdicts). Each is wrapped
        # in its own try/except, so the suite passes either way -- but on a CI runner with no
        # credentials that is a call which can retry with backoff before it raises, measured
        # elsewhere in this project at 18 s. Seeding both memos makes the count ZERO, verified by
        # counting db attempts with every db function stubbed to raise.
        #
        # It was _repost_count, not _host_jd_blocked, that the first attempt here missed -- which
        # is this file's own lesson restated: stubbing the read path of ONE db function is not
        # what makes a test offline, and the way to know is to count, not to reason.
        web._jd_blocked_hosts = set()
        web._repost_clusters = {}
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
        # Force the tie-break to have a right answer: same posting, one copy scored and one not.
        scores[DUPE_LOSER], scores[DUPE_WINNER] = 0, 77
        # THE CORPUS IS IN THE KEY, so the seed has to be built the way the product builds it.
        # Spelling the key as a literal ("u", md5) is what this line used to do, and when the
        # fingerprint joined the key the seed stopped matching in silence -- user_scores missed,
        # scored the corpus for real, and 40,198 of 40,199 rows "differed" from the reference.
        # That reads as a total product regression and is a stale literal in a test, so it goes
        # through web._corpus_key like every caller.
        seed_key = ("u", hashlib.md5(b"cv").hexdigest(),
                    web._corpus_key(web._jobs_cache["fp"]))
        web._score_cache[seed_key] = scores
        got = web.ranked_rows("u", "cv")

        ref = [web._build_row(j, scores.get(j.get("url"), 0)) for j in rows if j.get("url")]
        ref = web._dedupe_rows(ref)
        ref.sort(key=lambda r: r["score"], reverse=True)

        want("row COUNT matches", len(got) == len(ref), "%d vs %d" % (len(got), len(ref)))
        want("row ORDER matches",
             [r["url"] for r in got] == [r["url"] for r in ref])
        diff = sum(1 for a, b in zip(got, ref) if a != b)
        want("no row differs in ANY field", diff == 0, "%d differing" % diff)

        # ...and the dedupe was not a no-op, or the three checks above proved nothing about it.
        # Counted over the PAIR, not over the corpus: the real snapshot already collapses ~20
        # other cross-host duplicates, so any assertion of the form "out == in - 1" holds only
        # on synthetic input and fails on the corpus it matters most on.
        urls = set(r["url"] for r in got)
        survivors = [u for u in (DUPE_WINNER, DUPE_LOSER) if u in urls]
        want("the dedupe actually collapsed the pair", len(survivors) == 1,
             "%d of 2 survived" % len(survivors))
        want("and kept the SCORED copy, not the first one", survivors == [DUPE_WINNER],
             survivors[0][8:40] if survivors else "neither")

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
        web._jd_blocked_hosts, web._repost_clusters = saved[2], saved[3]
        web._rows_cache.clear()
        web._score_cache.clear()
    return bad


def score_pct_equivalence():
    """core.score_pct must equal core.score_against(...)[0] on EVERY analysed row, not a sample.

    score_pct exists because user_scores reads only [0] while score_against also builds and
    fully sorts `have` and `missing` for consumers that are not on that path -- ~78,000 discarded
    sorts per pass at this corpus size. It took the scoring pass from 8.7 s to 0.77 s at 38,805
    rows. The entire justification is that the ANSWER did not move.

    Checked over every row rather than a handful because this is the number every match
    percentage in the product is made of, and a divergence would look like a plausible score.
    The two rules most likely to drift are the confidence cap (a thin JD cannot claim a strong
    match) and the clean-sweep rule (100 requires nothing in the whole JD to be absent), so the
    resumes below are chosen to drive rows to both ends of the range.
    """
    print("=" * 74)
    print("core.score_pct == core.score_against[0]")
    print("=" * 74)
    bad = []

    snap = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "jobs_snapshot.json.gz")
    if os.path.exists(snap):
        with gzip.open(snap, "rt", encoding="utf-8") as fh:
            rows = (json.load(fh) or {}).get("rows") or []
    else:
        rows = synthetic()
        print("  (no snapshot -- %d synthetic rows)" % len(rows))

    analyses = [web.job_analysis(j) for j in rows]
    analyses = [a for a in analyses if a.get("terms")]

    RESUMES = {
        "typical": ("python flask sql postgres docker aws kubernetes spark airflow pandas "
                    "roadmap analytics jira agile pytest product manager engineer data etl "),
        "empty": "",
        # Drives rows to a clean sweep, which is the only path where `missing` changes the score.
        "kitchen sink": " ".join(sorted({t for a in analyses[:400] for t in a["terms"]})),
    }
    for label, txt in RESUMES.items():
        low = txt.lower()
        n = worst = 0
        for a in analyses:
            want = core.score_against(low, a)[0]
            got = core.score_pct(low, a)
            if want != got:
                n += 1
                worst = max(worst, abs(want - got))
        print("  %s %-30s %d rows, %d disagreements%s"
              % ("ok " if n == 0 else "FAIL", "resume: " + label, len(analyses), n,
                 "  (max delta %d)" % worst if n else ""))
        if n:
            bad.append("score_pct != score_against for resume %r" % label)

    # And the memo must not change the answer either, cold or warm.
    core._term_present.cache_clear()
    low = RESUMES["typical"].lower()
    cold = [core.score_pct(low, a) for a in analyses[:800]]
    warm = [core.score_pct(low, a) for a in analyses[:800]]
    core._term_present.cache_clear()
    recold = [core.score_pct(low, a) for a in analyses[:800]]
    same = cold == warm == recold
    print("  %s %-30s hits=%d misses=%d"
          % ("ok " if same else "FAIL", "_term_present memo is faithful",
             core._term_present.cache_info().hits, core._term_present.cache_info().misses))
    if not same:
        bad.append("_term_present memo changed a score")
    return bad


def warm_user_scores():
    """/warm's per-user half must write the file the FEED will actually look for.

    The whole mechanism turns on one equivalence: web._warm_user_scores keys the stored file on
    md5(db.profile_text(u)), while a real request keys it on md5(current_profile()). If those two
    ever differ, every file /warm writes is ignored -- silently, with no error raised and no slow
    path fixed, which is the worst failure shape available. So it is asserted behaviourally here
    rather than trusted from reading current_profile once.
    """
    print("=" * 74)
    print("web._warm_user_scores")
    print("=" * 74)
    bad = []

    def want(name, cond, extra=""):
        print("  %s %-46s %s" % ("ok " if cond else "FAIL", name, extra))
        if not cond:
            bad.append(name)

    PROFILES = {"alice": "python sql aws flask docker", "bob": "roadmap discovery stakeholder",
                "carol": ""}
    saved = (db.list_users, db.profile_text, web._ensure_resume_migrated,
             dict(web._jobs_cache), dict(web._base_rows_cache))
    tmp = tempfile.mkdtemp(prefix="warmscores-")
    saved_dir, web._SCORES_DIR = web._SCORES_DIR, tmp
    try:
        rows = [{"url": "w%d" % i, "title": "Data Engineer %d" % i, "company": "Acme",
                 "location": "Boston, MA", "found_date": "2026-08-15",
                 "jd_terms": '{"w":{"python":3,"sql":2},"n":0}', "match_score": 0}
                for i in range(40)]
        web._jobs_cache.update(rows=rows, at=10 ** 12, fp=(len(rows), "warmtest"))
        web._base_rows_cache.update(fp=None, rows=None)
        web._score_cache.clear()
        web._rows_cache.clear()
        web._ensure_resume_migrated = lambda u: None
        db.profile_text = lambda u: PROFILES.get(u, "")
        db.list_users = lambda: [{"username": u, "disabled_at": None} for u in PROFILES]

        # THE EQUIVALENCE, behaviourally: what a request would score against, per user.
        for u in ("alice", "bob"):
            with web.app.test_request_context("/"):
                from flask import session
                session["user"] = u
                web._profile_cache.pop(u, None)
                want("current_profile() == db.profile_text(%r)" % u,
                     web.current_profile() == db.profile_text(u))

        r = web._warm_user_scores()
        want("reports the account count it saw", r.get("accounts") == 3, repr(r))
        want("computed the two with a resume", r.get("computed") == 2, repr(r.get("computed")))
        want("skipped the one without", r.get("no_resume") == 1)
        want("nothing failed", r.get("failed") == 0)

        # ...and the file it wrote is the one a request finds.
        for u in ("alice", "bob"):
            md5 = hashlib.md5(PROFILES[u].encode("utf-8")).hexdigest()
            stored = web._scores_read(u, md5, web._jobs_cache["fp"])
            want("the file for %r is what a request reads" % u,
                 stored is not None and len(stored) == len(rows),
                 "%d scores" % (len(stored) if stored else 0))

        # A second call must be nearly free -- it is on every keep-warm tick.
        web._score_cache.clear()
        r2 = web._warm_user_scores()
        want("a second pass recomputes nothing", r2.get("already_warm") == 2, repr(r2))

        # A moved corpus must invalidate, or a scrape would serve yesterday's scores.
        web._jobs_cache["fp"] = (len(rows), "moved")
        web._score_cache.clear()
        r3 = web._warm_user_scores()
        want("a moved fingerprint recomputes", r3.get("computed") == 2, repr(r3))

        # An empty account list is a REPORTED zero, not a silent success.
        db.list_users = lambda: []
        want("no accounts is reported, not assumed",
             web._warm_user_scores().get("accounts") == 0)
        db.list_users = lambda: (_ for _ in ()).throw(RuntimeError("db down"))
        want("a dead db is reported, not raised",
             "error" in web._warm_user_scores())
    finally:
        (db.list_users, db.profile_text, web._ensure_resume_migrated) = saved[0], saved[1], saved[2]
        web._jobs_cache.clear(); web._jobs_cache.update(saved[3])
        web._base_rows_cache.update(saved[4])
        web._SCORES_DIR = saved_dir
        web._score_cache.clear(); web._rows_cache.clear()
        shutil.rmtree(tmp, ignore_errors=True)
    return bad


def row_files():
    """The shared row file must be byte-faithful, and its key must cover every input.

    web._base_rows() is ~4,100 ms on production and lives in a per-process dict, so every worker
    paid it once. /warm cannot fix that -- one HTTP request reaches one worker -- so the rows go
    to a file the whole pool reads, exactly like score_cache/.

    THE KEY IS THE INTERESTING PART. A built row embeds logo_url, sponsor_counts and visa_tags
    output plus two KV maps, and jobs_fingerprint() covers none of them. A file keyed on the
    fingerprint alone would serve rows built against last week's logos indefinitely -- where an
    in-memory cache gets away with it only because it dies with the worker. So the derived
    signature is asserted here as carefully as the round trip.
    """
    print("=" * 74)
    print("web._rows_read / _rows_write (the shared base-rows file)")
    print("=" * 74)
    bad = []

    def want(name, cond, extra=""):
        print("  %s %-48s %s" % ("ok " if cond else "FAIL", name, extra))
        if not cond:
            bad.append(name)

    snap = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "jobs_snapshot.json.gz")
    if os.path.exists(snap):
        with gzip.open(snap, "rt", encoding="utf-8") as fh:
            rows = (json.load(fh) or {}).get("rows") or []
    else:
        rows = synthetic()
        print("  (no snapshot -- %d synthetic rows)" % len(rows))

    tmp = tempfile.mkdtemp(prefix="rowcache-")
    saved = (web._ROWS_DIR, dict(web._jobs_cache), dict(web._base_rows_cache))
    web._ROWS_DIR = tmp
    try:
        FP = (len(rows), "rowtest")
        web._jobs_cache.update(rows=rows, at=10 ** 12, fp=FP)
        web._base_rows_cache.update(fp=None, rows=None)
        web._jd_blocked_hosts = set()
        web._repost_clusters = {}

        # persist=True is what /warm passes. A plain request deliberately does NOT write: the
        # gzip is 2,153 ms of the 2,291 ms that a fingerprint move used to cost, and charging
        # that to whoever loads the feed next is the whole reason this argument exists.
        fresh = web._base_rows(persist=True)
        files = [f for f in os.listdir(tmp) if web._rows_stale(f)]
        want("a cold build with persist=True writes one file", len(files) == 1,
             "%d file(s)" % len(files))

        web._base_rows_cache.update(fp=None, sig=None, rows=None, by_url=None, persisted=None)
        web._base_rows()                              # a REQUEST must not write a second file
        want("a request does NOT write",
             len([f for f in os.listdir(tmp) if web._rows_stale(f)]) == 1)

        web._base_rows_cache.update(fp=None, sig=None, rows=None, by_url=None, persisted=None)
        loaded = web._base_rows()                     # must come from the file
        want("row COUNT matches", len(loaded) == len(fresh))
        want("row ORDER matches", [r["url"] for r in loaded] == [r["url"] for r in fresh])
        diff = sum(1 for a, b in zip(fresh, loaded) if a != b)
        want("no row differs in ANY field", diff == 0, "%d differing" % diff)

        # JSON had no tuples and `visa` came back a list, which needed a fix-up pass over every
        # row; pickle round-trips it natively and the pass is gone. Assert the PROPERTY rather
        # than the old fix-up list, because it is the property _filter_rows and the client
        # depend on and it must hold whatever the format is.
        tup = sorted({k for r in loaded for k, v in r.items()
                      if isinstance(v, tuple)} ^
                     {k for r in fresh for k, v in r.items() if isinstance(v, tuple)})
        want("every tuple field survives the round trip as a tuple", not tup,
             ",".join(tup) or "%d row(s) checked" % len(loaded))

        sig = web._derived_signature()
        want("a moved corpus fingerprint misses",
             web._rows_read((len(rows) + 1, "rowtest"), sig) is None)
        want("a moved derived signature misses",
             web._rows_read(FP, "0" * 32) is None)
        want("an unavailable fingerprint is never a key",
             web._rows_read((None, ""), sig) is None)

        # THE KV MAPS ARE DELIBERATELY *NOT* IN THE SIGNATURE, and this asserts that rather
        # than the reverse. They arrive over the network and their readers swallow a failure
        # into an empty map, so including them meant two workers computed different keys for
        # the same corpus and ping-ponged the shared file, each rebuilding 7 s and overwriting
        # the other. A key must be computable from the filesystem alone.
        web._repost_clusters = {"someclusterkey": 4}
        want("repost_clusters does NOT move the signature", web._derived_signature() == sig)
        web._repost_clusters = {}
        web._jd_blocked_hosts = {"blocked.example.com"}
        want("jd_host_verdicts does NOT move the signature", web._derived_signature() == sig)
        web._jd_blocked_hosts = set()
        want("the signature is stable for identical inputs", web._derived_signature() == sig)

        # CONTENT, NOT MTIME, and this is the pair that proves it. A deploy is a zip extract:
        # every shipped file is rewritten, so every mtime moves and no byte changes. Keyed on
        # mtime the stored rows died on every upload -- a 6.9 s rebuild for the first visitor
        # after every deploy, which is exactly when a cold worker is guaranteed. The second
        # assertion is the one that keeps the first honest: a signature that never moves is not
        # a cache key, it is a constant, so a real content change must still be caught.
        probe = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "sponsor_counts.json")
        if os.path.exists(probe):
            body = io.open(probe, "rb").read()
            try:
                io.open(probe, "wb").write(body)              # same bytes, new mtime
                want("rewriting a data file byte-for-byte does NOT move it",
                     web._derived_signature() == sig)
                io.open(probe, "wb").write(body + b" ")       # one byte more
                want("...but a single changed byte DOES",
                     web._derived_signature() != sig)
            finally:
                io.open(probe, "wb").write(body)
            want("and it comes back", web._derived_signature() == sig)
        else:
            print("  --  content-vs-mtime            SKIPPED (no sponsor_counts.json)")

        # Bounded, and _invalidate_jobs must take the file with it.
        for i in range(6):
            web._rows_write((len(rows) + 10 + i, "rowtest"), sig, fresh[:5], {"plan": [0]})
        n = len([f for f in os.listdir(tmp) if web._rows_stale(f)])
        want("the directory stays bounded", n <= web._ROWS_MAX_FILES,
             "%d files, cap %d" % (n, web._ROWS_MAX_FILES))
        web._rows_clear()
        want("_rows_clear empties it",
             not [f for f in os.listdir(tmp) if web._rows_stale(f)])

        # ---- the incremental path, which is what makes a moving corpus survivable ----
        web._base_rows_cache.update(fp=None, sig=None, rows=None, by_url=None, persisted=None)
        web._base_rows(persist=True)
        n_after_warm = len([f for f in os.listdir(tmp) if web._rows_stale(f)])
        base_n = len([r for r in rows if r.get("url")])
        # a scrape lands: some rows added, and some EXISTING rows updated
        added = [dict(r, url="https://added.example/%d" % i) for i, r in enumerate(rows[:5])]
        moved = [dict(r) for r in rows] + added
        for r in moved[:2]:
            r["is_active"] = "False"                  # a posting closed
        web._jobs_cache.update(rows=moved, at=10 ** 12, fp=(len(moved), "moved"))
        out = web._base_rows()
        want("only the delta is rebuilt", web._base_rows_cache["fresh"] == len(added) + 2,
             "rebuilt %s, expected %d" % (web._base_rows_cache["fresh"], len(added) + 2))
        want("every row is present", len(out) == base_n + len(added))
        # THE CORRECTNESS ARGUMENT: reuse is by VALUE, so an updated row must NOT be reused.
        # Reusing on url alone would serve a closed posting as open, and nothing would notice.
        want("an UPDATED row is rebuilt, not reused",
             all(r.get("closed") for r in out[:2]))
        want("a request still wrote nothing",
             len([f for f in os.listdir(tmp) if web._rows_stale(f)]) == n_after_warm,
             "%d files, unchanged from %d" %
             (len([f for f in os.listdir(tmp) if web._rows_stale(f)]), n_after_warm))
        # ---- the corpus-derived metadata that travels in the same file ----
        # Both halves used to be recomputed per user: _dupe_key over the whole corpus (225 ms of
        # the 280 ms dedupe, to arbitrate 0.41% of rows) and role_counts' own full pass. They
        # are facts about the postings, so they are built once and stored -- but the DEDUPE
        # CHOICE still has to happen per user, because _dupe_rank tie-breaks on the score.
        meta = web._base_rows_cache.get("meta") or {}
        # A COLD WORKER SIZES ITS LRU FROM THE BUILT ROWS, because it never loads the corpus.
        # Left to the 20k fallback it halved the per-entry estimate against a 40,294-row corpus
        # and doubled the cap -- a shared-host worker sized at ~340 MB where the budget said 170.
        n_rows = len(web._base_rows_cache["rows"] or ())
        if n_rows > 20000:
            saved_jc, saved_budget = dict(web._jobs_cache), web._CACHE_BUDGET_MB
            try:
                # LIFT THE BUDGET FIRST. At the default both answers land on the floor of 1 and
                # the comparison passes no matter what the code does -- a check that cannot fail
                # is worse than none. Sized so the correct answer is comfortably above it.
                web._CACHE_BUDGET_MB = n_rows * web._ROW_CACHE_BYTES_PER_ROW / 1048576.0 * 6
                web._jobs_cache.update(rows=None)
                cold = web._cache_max()
                web._jobs_cache.update(rows=saved_jc.get("rows"))
                loaded = web._cache_max()
                want("_cache_max is the same with the corpus unloaded", cold == loaded,
                     "%d vs %d, over %d built rows" % (cold, loaded, n_rows))
                want("...and that comparison is above the floor",
                     cold > web._SCORE_CACHE_MIN, "cap %d, floor %d"
                     % (cold, web._SCORE_CACHE_MIN))
                # ...and the regression it guards: the 20k fallback against a 40k corpus.
                web._jobs_cache.update(rows=None)
                base = web._base_rows_cache["rows"]
                web._base_rows_cache["rows"] = None
                want("a worker that knows NOTHING still guesses high, not low",
                     web._cache_max() > loaded,
                     "%d vs %d" % (web._cache_max(), loaded))
                web._base_rows_cache["rows"] = base
            finally:
                web._CACHE_BUDGET_MB = saved_budget
                web._jobs_cache.clear(); web._jobs_cache.update(saved_jc)
        else:
            print("  --  cold _cache_max             SKIPPED (corpus too small to differ)")

        want("the file carries a dedupe plan", isinstance(meta.get("plan"), list),
             "%d entries" % len(meta.get("plan") or []))
        want("the file carries role counts", bool(meta.get("roles")))
        base = web._base_rows()
        sc = {r["url"]: (i * 7) % 101 for i, r in enumerate(base)}

        def score_of(r):
            return 0 if r["score_pending"] else sc.get(r["url"], 0)

        # THE EQUIVALENCE, over the whole corpus and after the sort ranked_rows applies. Python's
        # sort is stable, so a plan that produced the same SET in a different order would rank
        # ties differently and nothing else here would notice.
        by_plan = web._apply_dedupe_plan(base, meta["plan"], score_of)
        by_ref = web._dedupe_rows([dict(r, score=score_of(r)) for r in base])
        want("the plan reproduces _dedupe_rows exactly", by_plan == by_ref,
             "%d vs %d rows" % (len(by_plan), len(by_ref)))
        by_plan.sort(key=lambda r: r["score"], reverse=True)
        by_ref.sort(key=lambda r: r["score"], reverse=True)
        want("...and still after the score sort", by_plan == by_ref)
        want("role counts match a fresh pass over the corpus",
             meta["roles"] == web._role_counts_for(web._jobs_cache["rows"]))

        # ...and /warm persists the new corpus rather than short-circuiting on the memory hit,
        # which it did until 2026-09-01: a stale early return meant the file froze for ever.
        web._base_rows(persist=True)
        want("/warm persists after a request built it",
             len([f for f in os.listdir(tmp) if web._rows_stale(f)]) == n_after_warm + 1,
             "%d files, was %d" %
             (len([f for f in os.listdir(tmp) if web._rows_stale(f)]), n_after_warm))
    finally:
        web._ROWS_DIR = saved[0]
        web._jobs_cache.clear(); web._jobs_cache.update(saved[1])
        web._base_rows_cache.update(saved[2])
        web._jd_blocked_hosts = None
        web._repost_clusters = None
        shutil.rmtree(tmp, ignore_errors=True)
    return bad


def corpus_fp():
    """The fingerprint sidecar, and the three ways it must refuse to answer.

    _corpus_fp exists so a cold worker can look up its stored rows and scores WITHOUT parsing
    the corpus those keys describe -- 616 ms at 40,294 rows, on a request that then throws the
    corpus away. That makes the sidecar a cache key derived from a file, and a cache key that
    can be wrong is worse than no cache at all: believing a stale fingerprint serves another
    corpus's cards for the life of the worker.

    So the sidecar is stamped with the snapshot's own (st_mtime_ns, st_size) and anything that
    disagrees is refused rather than repaired. The tests below drive each way it can disagree
    and assert None comes back, then assert the fallbacks still produce the right answer.
    """
    print("=" * 74)
    print("web._corpus_fp / the fingerprint sidecar")
    print("=" * 74)
    bad = []

    def want(name, cond, extra=""):
        print("  %s %-48s %s" % ("ok " if cond else "FAIL", name, extra))
        if not cond:
            bad.append(name)

    tmp = tempfile.mkdtemp(prefix="snapfp-")
    saved = (web._JOBS_SNAPSHOT, web._JOBS_SNAPSHOT_FP, dict(web._jobs_cache),
             db.jobs_fingerprint)
    web._JOBS_SNAPSHOT = os.path.join(tmp, "jobs_snapshot.json.gz")
    web._JOBS_SNAPSHOT_FP = web._JOBS_SNAPSHOT + ".fp.json"
    try:
        rows = synthetic()
        FP = (len(rows), "2026-09-01")
        web._jobs_cache.update(rows=None, fp=None, at=0)
        db.jobs_fingerprint = lambda: (None, "")      # offline: the probe cannot answer

        want("no snapshot at all -> no answer", web._snapshot_fp(10 ** 9) is None)
        web._snapshot_write(rows, FP)
        want("_snapshot_write leaves a sidecar", os.path.exists(web._JOBS_SNAPSHOT_FP))
        want("and it reads back as the fingerprint", web._snapshot_fp(10 ** 9) == FP)
        want("outside the age window it declines", web._snapshot_fp(-1) is None)

        # 1. the snapshot was replaced (a deploy, a scrape) and the sidecar was not
        io.open(web._JOBS_SNAPSHOT, "ab").write(b"x")
        want("a REWRITTEN snapshot invalidates the stamp", web._snapshot_fp(10 ** 9) is None)
        web._snapshot_write(rows, FP)

        # 2. the mtime moved on its own -- _snapshot_touch's os.utime does exactly this, and
        #    until it restamped, every revalidation killed the fast path it exists to feed.
        os.utime(web._JOBS_SNAPSHOT, (time.time() - 5, time.time() - 5))
        want("a RE-TIMED snapshot invalidates the stamp", web._snapshot_fp(10 ** 9) is None)
        web._snapshot_touch(rows, FP)
        want("_snapshot_touch restamps it", web._snapshot_fp(10 ** 9) == FP)

        # 3. a corrupt or hand-written sidecar
        io.open(web._JOBS_SNAPSHOT_FP, "w", encoding="utf-8").write("{not json")
        want("a corrupt sidecar is refused, not raised", web._snapshot_fp(10 ** 9) is None)

        # ...and _snapshot_read heals it, which is what covers a deploy: the zip ships a
        # snapshot and no sidecar, so the first worker to pay the parse leaves one behind.
        got, fp2 = web._snapshot_read(10 ** 9)
        want("_snapshot_read still returns the corpus", bool(got) and fp2 == FP)
        want("...and heals the sidecar for the next worker", web._snapshot_fp(10 ** 9) == FP)

        # "don't know" is (None, "") -- TRUTHY, and keying on it would make two different
        # unknown corpora compare equal. The same trap _base_rows documents.
        web._snapshot_write(rows, (None, ""))
        want("an unavailable fingerprint is never stored as a key",
             web._snapshot_fp(10 ** 9) is None)
        web._snapshot_write(rows, FP)

        # ---- _corpus_fp itself: memory, then sidecar, then the probe, then get_jobs ----
        web._jobs_cache.update(rows=rows, fp=("mem", "hit"), at=time.time())
        want("memory wins when it is fresh", web._corpus_fp() == ("mem", "hit"))
        web._jobs_cache.update(rows=None, fp=None, at=0)
        want("then the sidecar, with no corpus loaded", web._corpus_fp() == FP)
        want("...and it did NOT load the corpus", web._jobs_cache["rows"] is None)

        os.utime(web._JOBS_SNAPSHOT, (time.time() - 5, time.time() - 5))   # stamp now stale
        db.jobs_fingerprint = lambda: (999, "2026-09-02")
        want("then the database probe", web._corpus_fp() == (999, "2026-09-02"))
        want("...and that did not load the corpus either", web._jobs_cache["rows"] is None)

        db.jobs_fingerprint = lambda: (None, "")
        want("a probe that cannot answer falls through to get_jobs",
             web._corpus_fp() == FP and web._jobs_cache["rows"] is not None)
    finally:
        web._JOBS_SNAPSHOT, web._JOBS_SNAPSHOT_FP = saved[0], saved[1]
        web._jobs_cache.clear(); web._jobs_cache.update(saved[2])
        db.jobs_fingerprint = saved[3]
        shutil.rmtree(tmp, ignore_errors=True)
    return bad


def score_write_visibility():
    """A score write must reach the CARD, and until 2026-09-04 it did not.

    THE BUG, as the owner hit it. jobs_fingerprint() was (row count, max first_seen) and both
    move on INSERTS only. A scrape inserts a bare row (url/title/company/location), which moves
    them and freezes a snapshot holding jd_terms NULL; the JD fetch and score pass then write
    jd, jd_terms and match_score onto that same row with an UPDATE, which moved neither. So
    _row_pending read a NULL column and the card said "JD pending" at score 0 -- while the job
    page, which reads the description live on the url key, scored the same posting at 18%.
    Worse than merely stale: past _JOBS_TTL the probe re-confirmed "unchanged" and restamped the
    sidecar, so the wrong answer renewed itself hourly and only an INSERT ever broke the loop.
    Measured on the live table that day: all 4,371 rows inserted passed through that window.

    TWO INDEPENDENT HALVES had to move and both are pinned here, because either alone leaves the
    card wrong:
      1. db.jobs_fingerprint() grew a third component -- the count of rows WITH jd_terms.
      2. _score_cache and _rows_cache took the corpus into their keys. They were keyed on
         (user, resume) alone and nothing clears them when the corpus moves on its own, so a
         warm worker went on serving its first render's rows however loudly (1) noticed.
    """
    print("=" * 74)
    print("a score write reaches the card")
    print("=" * 74)
    bad = []

    def want(name, cond, extra=""):
        print("  %s %-46s %s" % ("ok " if cond else "FAIL", name, extra))
        if not cond:
            bad.append(name)

    # ---- 1. the probe itself ----
    saved = (db.has_remote_db, db.table_count, db._http)
    try:
        state = {"n": 100, "scored": 60, "down": None}

        class _Resp(object):
            status_code = 200

            def json(self):
                return [{"first_seen": "2026-09-04"}]

        class _Http(object):
            def get(self, *a, **k):
                return _Resp()

        db.has_remote_db = lambda: True
        db.table_count = (lambda table, params=None:
                          None if state["down"] == ("scored" if params else "total")
                          else (state["scored"] if params else state["n"]))
        db._http = _Http()

        fp1 = db.jobs_fingerprint()
        want("the fingerprint carries three parts", len(fp1) == 3, str(fp1))
        want("...and the third is the scored count", fp1[2] == 60)

        state["scored"] = 61                        # one score write lands, nothing is inserted
        fp2 = db.jobs_fingerprint()
        want("a SCORE WRITE moves it", fp2 != fp1, "%s -> %s" % (fp1, fp2))
        want("...though the count and the date sat still", fp2[:2] == fp1[:2])

        # ALL OR NOTHING. A partial tuple would let two different unknown scoring states compare
        # equal, which is the trap _base_rows and _snapshot_fp both already document.
        state["down"] = "scored"
        want("an unreachable scored count -> don't know",
             db.jobs_fingerprint() == db.FP_UNKNOWN)
        state["down"] = "total"
        want("an unreachable total count -> don't know",
             db.jobs_fingerprint() == db.FP_UNKNOWN)
        want("...and 'don't know' is TRUTHY, so fp[0] is the test",
             bool(db.FP_UNKNOWN) and db.FP_UNKNOWN[0] is None)
        want("web._corpus_key refuses it", web._corpus_key(db.FP_UNKNOWN) is None)
    finally:
        db.has_remote_db, db.table_count, db._http = saved

    # ---- 2. the caches sitting in front of it ----
    tmp = tempfile.mkdtemp(prefix="scorewrite-")
    saved2 = (web._ROWS_DIR, web._SCORES_DIR, dict(web._jobs_cache),
              dict(web._base_rows_cache), web._jd_blocked_hosts, web._repost_clusters)
    try:
        web._ROWS_DIR = os.path.join(tmp, "rows")
        web._SCORES_DIR = os.path.join(tmp, "scores")
        os.makedirs(web._ROWS_DIR)
        os.makedirs(web._SCORES_DIR)
        # Offline by construction -- the same two memos base_rows() seeds, for the same reason.
        web._jd_blocked_hosts = set()
        web._repost_clusters = {}
        web._jdmeta.clear()

        SCORED = '{"w":{"python":1.0,"sql":0.9,"roadmap":0.8},"n":0}'
        FRESH = "https://b.example/urbandale"
        rows = [{"url": "https://b.example/%d" % i, "title": "Project Manager %d" % i,
                 "company": "Acme", "location": "Boston, MA", "match_score": 40,
                 "jd_terms": SCORED} for i in range(20)]
        # The row the scrape inserted a moment ago: its description is on the way, and the
        # scoring pass that will read it has not run yet.
        rows.append({"url": FRESH, "title": "Hospital Operations Manager", "company": "Acme",
                     "location": "Urbandale, Iowa, United States", "match_score": 0,
                     "jd_terms": None})
        RESUME = "python sql roadmap stakeholder delivery"

        web._jobs_cache.update(rows=rows, at=10 ** 12, fp=(len(rows), "2026-09-04", 20))
        web._base_rows_cache.update(fp=None, sig=None, rows=None, by_url=None, fresh=0,
                                    persisted=None, meta=None)
        web._rows_cache.clear()
        web._score_cache.clear()

        first = web.ranked_rows("u", RESUME)
        card = next(r for r in first if r["url"] == FRESH)
        want("the just-inserted row starts as JD pending",
             card["score_pending"] and card["score"] == 0)
        want("an unmoved corpus still reuses the cache", web.ranked_rows("u", RESUME) is first)

        # THE SCORE PASS LANDS. jd_terms arrives by UPDATE, so the row count and max(first_seen)
        # do not budge -- only the third component does. This is the exact event that used to be
        # invisible, and everything below is what the reader sees because of it.
        # REPLACED, NOT MUTATED IN PLACE, and the distinction is what makes this test mean
        # anything. _base_rows' incremental path reuses a built row when the SOURCE dict compares
        # equal to the one it built from -- and it holds a reference to that very dict, so
        # editing it in place makes `was[0] == j` compare an object against itself, always True,
        # and the stale card survives a moved fingerprint. Production cannot do that: a corpus
        # re-read parses fresh dicts out of JSON, so the comparison sees the new jd_terms. The
        # first draft of this test mutated, and both assertions below failed against a fix that
        # was working.
        rows = [dict(r) for r in rows]
        rows[-1] = dict(rows[-1], jd_terms=SCORED, match_score=18)
        web._jobs_cache["rows"] = rows
        web._jobs_cache["fp"] = (len(rows), "2026-09-04", 21)

        second = web.ranked_rows("u", RESUME)
        want("a moved corpus is NOT served from the warm cache", second is not first)
        card2 = next(r for r in second if r["url"] == FRESH)
        want("...the card stops saying JD pending", not card2["score_pending"])
        want("...and carries a real number instead of 0",
             card2["score"] > 0, "%d%%" % card2["score"])
        want("the dead generation is dropped, not left to the LRU",
             len(web._rows_cache) == 1, "%d entries" % len(web._rows_cache))
        want("every _score_cache key carries the corpus",
             bool(web._score_cache) and all(len(k) == 3 for k in web._score_cache),
             "%d entries" % len(web._score_cache))

        # "Don't know" must not be cached under, or two unknown corpora compare equal.
        web._jobs_cache["fp"] = db.FP_UNKNOWN
        before = len(web._rows_cache)
        web.ranked_rows("u", RESUME)
        want("an unknown corpus writes no cache entry",
             len(web._rows_cache) == before, "%d entries" % len(web._rows_cache))
    finally:
        web._ROWS_DIR, web._SCORES_DIR = saved2[0], saved2[1]
        web._jobs_cache.clear()
        web._jobs_cache.update(saved2[2])
        web._base_rows_cache.clear()
        web._base_rows_cache.update(saved2[3])
        web._jd_blocked_hosts, web._repost_clusters = saved2[4], saved2[5]
        web._rows_cache.clear()
        web._score_cache.clear()
        shutil.rmtree(tmp, ignore_errors=True)
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
    fails += score_pct_equivalence()
    fails += warm_user_scores()
    fails += row_files()
    fails += corpus_fp()
    fails += score_write_visibility()
    print("FAIL: %d problem(s)" % len(fails) if fails else "PASS: all speed caches are faithful")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
