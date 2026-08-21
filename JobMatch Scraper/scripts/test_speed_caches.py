"""The 2026-08-21 speed changes must not have changed any ANSWER. Two of them replaced work
with a cache or a shortcut, and both are the kind that is right on the data you looked at:

  1. web.py::_row_pending reads jobs.jd_terms as a STRING instead of unpacking it.
  2. db.get_job_jd memoizes per url, including the empty result.
  3. core._stem and core._alias_forms are lru_cached. _stem was 3.5M calls and 16.7s of a single
     35s ranked_rows rebuild, so this is the largest of the three -- and the one where a wrong
     answer would silently move every match percentage in the product.

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

    real_fetch, real_supa = db._fetch_all, db.using_supabase
    db._fetch_all, db.using_supabase = fake_fetch, lambda: True
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
        db._fetch_all, db.using_supabase = real_fetch, real_supa
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
    print("FAIL: %d problem(s)" % len(fails) if fails else "PASS: all speed caches are faithful")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
