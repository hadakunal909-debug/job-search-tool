#!/usr/bin/env python3
"""
test_workday_facets.py — guards the fix for Workday's 2000-posting clamp.

WHY THIS FILE EXISTS. Workday's CXS API answers `total: 2000` for ANY tenant with more than
2000 openings, and it clamps `offset` to match: offset=2000, 3000, 4000 and 8000 all return
the identical page. scrape_workday paged until `offset >= total`, believed the 2000 and
stopped — so a big board was read as an arbitrary 2000-posting slice of itself while
reporting ok, a full page count, and no error. Measured 2026-09-08 over all 246 Workday
boards: 15 pinned at exactly 2000, hiding 121,803 postings. Booz Allen was 2,000 of 2,310 —
which is how one linked posting turned out to be a systemic ceiling.

The fix slices the board by a facet, because a facet value's `count` is exact and unclamped.
Everything below is about the two ways that could silently make coverage WORSE:

  1. picking a decoy group — `distance` (five values of 1) or `locationMainGroup` (three
     values of 0) would replace a 2000-row walk with a 5-row one, and
  2. picking a TRUNCATED facet list — Walmart offers 55 `Job_Profiles` summing to 15,962
     against a true 21,279, so slicing on it silently drops the tail.

No database and no network: the planner is pure, and the end-to-end check stubs SESSION.
"""
import json
import sys

import scraper


# --------------------------------------------------------------------------- planner
def _facets(*groups):
    """groups: (param, [(id, count), ...]) -> a page-0 body carrying just those facets."""
    return {"facets": [{"facetParameter": p,
                        "values": [{"id": i, "count": c, "descriptor": i} for i, c in vs]}
                       for p, vs in groups]}


def test_no_clamp_means_no_plan():
    # THE MOST IMPORTANT CASE. A healthy board must keep its plain, cheap walk even when its
    # facets would partition perfectly — faceting a 300-posting board buys nothing and costs
    # a request per arm.
    body = _facets(("jobFamilyGroup", [("a", 100), ("b", 200)]))
    assert scraper._workday_facet_plan(body, 300) is None
    assert scraper._workday_facet_plan(body, scraper.WORKDAY_TOTAL_CLAMP - 1) is None


def test_a_group_summing_past_total_proves_the_clamp_and_names_the_true_size():
    body = _facets(("jobFamilyGroup", [("tech", 1179), ("cons", 731),
                                       ("eng", 397), ("lead", 3)]))
    param, arms, true_total = scraper._workday_facet_plan(body, 2000)
    assert param == "jobFamilyGroup", param
    assert true_total == 2310, true_total          # the real Booz Allen numbers
    assert len(arms) == 4


def test_arms_come_back_smallest_first():
    # The caller stops at WORKDAY_MAX_JOBS, so ordering is what decides whether truncation
    # eats many small role families or one big one. Smallest-first maximises COMPLETE arms.
    body = _facets(("jobFamilyGroup", [("big", 1800), ("tiny", 10), ("mid", 400)]))
    _p, arms, _t = scraper._workday_facet_plan(body, 2000)
    assert [a[0] for a in arms] == ["tiny", "mid", "big"], arms
    assert [a[1] for a in arms] == [10, 400, 1800]


def test_a_group_that_does_not_exceed_total_is_not_evidence():
    # `distance` is five values of 1 and `remoteType` a single small one: real facet groups
    # that describe a CONTROL, not a partition of the board. Without the `s > total` test
    # these look like a complete slicing axis.
    body = _facets(("distance", [("10", 1), ("25", 1), ("50", 1), ("any", 1)]),
                   ("remoteType", [("remote", 14), ("onsite", 3)]))
    assert scraper._workday_facet_plan(body, 2000) is None


def test_zero_count_hierarchical_group_is_ignored():
    # locationMainGroup reports its children as counts of 0 on every tenant measured.
    body = _facets(("locationMainGroup", [("country", 0), ("region", 0), ("primary", 0)]),
                   ("jobFamilyGroup", [("a", 1500), ("b", 900)]))
    param, _arms, true_total = scraper._workday_facet_plan(body, 2000)
    assert param == "jobFamilyGroup"
    assert true_total == 2400


def test_a_truncated_facet_list_loses_to_a_complete_one():
    # Walmart, as measured: Job_Profiles shows its top 55 values (15,962) while the complete
    # partition reports 21,279. Slicing on the truncated list would drop 5,317 postings and
    # look like a success.
    body = _facets(("Job_Profiles", [("p%d" % i, 290) for i in range(55)]),   # 15,950
                   ("jobFamilyGroup", [("a", 11000), ("b", 10279)]))          # 21,279
    param, _arms, true_total = scraper._workday_facet_plan(body, 2000)
    assert param == "jobFamilyGroup", param
    assert true_total == 21279, true_total


def test_job_family_wins_a_near_tie_against_geography():
    # Target: 12,343 by family, 12,356 by state — a 13-posting disagreement. Family is
    # preferred because when the budget truncates we lose one low-relevance family
    # ("Stores") rather than a slice of every role type in a set of states.
    body = _facets(("Location_Region_State_Province",
                    [("s%d" % i, 12356 // 52) for i in range(52)]),
                   ("jobFamilyGroup", [("stores", 11847), ("tech", 496)]))
    param, _arms, _t = scraper._workday_facet_plan(body, 2000)
    assert param == "jobFamilyGroup", param


def test_far_from_a_tie_the_bigger_sum_wins_regardless_of_name():
    # The preference is a tie-breaker, not an override: a job-family group that is itself
    # truncated must not beat a complete group that reports far more.
    body = _facets(("jobFamilyGroup", [("a", 1100), ("b", 1000)]),            # 2,100
                   ("Location_Country", [("us", 5000), ("in", 900)]))         # 5,900
    param, _arms, true_total = scraper._workday_facet_plan(body, 2000)
    assert param == "Location_Country", param
    assert true_total == 5900


def test_single_value_and_idless_groups_are_skipped():
    # A one-value group cannot partition anything, and a value with no id cannot be applied.
    body = {"facets": [
        {"facetParameter": "only", "values": [{"id": "x", "count": 9999}]},
        {"facetParameter": "noid", "values": [{"count": 5000}, {"count": 5000}]},
    ]}
    assert scraper._workday_facet_plan(body, 2000) is None


def test_no_facets_at_all():
    for body in ({}, {"facets": []}, {"facets": None}, None):
        assert scraper._workday_facet_plan(body, 2000) is None


# --------------------------------------------------------------- end to end, stubbed
def _arm_of(facets):
    """The facet value a request selected, or '' for the whole board. A hashable record of
    what was asked for — `appliedFacets` itself holds lists and cannot go in a set."""
    for _param, vals in sorted((facets or {}).items()):
        if vals:
            return vals[0]
    return ""


class _Resp(object):
    def __init__(self, payload):
        self.status_code = 200
        self._p = payload

    def json(self):
        return self._p


class _FakeBoard(object):
    """A tenant with 55 postings that reports a clamped total of 40.

    Arms partition 1..55; the unfaceted page 0 returns 1..20, which OVERLAPS the arms — so
    this also proves the run dedupes on externalPath rather than counting a posting twice.
    """
    ARMS = {"a": list(range(1, 11)),          # 10
            "c": list(range(11, 31)),         # 20
            "b": list(range(31, 56))}         # 25   -> 55 total

    def __init__(self):
        self.calls = []

    def post(self, url, headers=None, timeout=None, data=None):
        req = json.loads(data)
        facets, offset = req["appliedFacets"], req["offset"]
        limit = req["limit"]
        self.calls.append((_arm_of(facets), offset))
        if facets:
            ids = self.ARMS[facets["jobFamilyGroup"][0]]
            total = len(ids)
        else:
            ids = list(range(1, 21))
            total = 40                        # THE CLAMP: 40 while 55 exist
        page = ids[offset:offset + limit]
        return _Resp({"total": total, "jobPostings": [
            {"title": "Engineer %d" % i, "externalPath": "/job/Reston-VA/Engineer_%d" % i,
             "locationsText": "Reston, VA", "postedOn": "Posted Today"} for i in page],
            "facets": [{"facetParameter": "jobFamilyGroup", "values": [
                {"id": k, "count": len(v), "descriptor": k} for k, v in sorted(self.ARMS.items())]}]})


def _run_against(board, clamp=40, max_jobs=200):
    saved = (scraper.SESSION, scraper.WORKDAY_TOTAL_CLAMP, scraper.WORKDAY_MAX_JOBS)
    scraper.SESSION, scraper.WORKDAY_TOTAL_CLAMP, scraper.WORKDAY_MAX_JOBS = board, clamp, max_jobs
    try:
        return scraper.scrape_workday("https://acme.wd1.myworkdayjobs.com/External")
    finally:
        scraper.SESSION, scraper.WORKDAY_TOTAL_CLAMP, scraper.WORKDAY_MAX_JOBS = saved


def test_end_to_end_reads_past_the_clamp():
    board = _FakeBoard()
    rows = _run_against(board)
    # 55, not 40. This is the whole bug: the old code stopped at the reported total.
    assert len(rows) == 55, len(rows)
    assert len({r["url"] for r in rows}) == 55, "postings must be deduped across arms"
    # every arm was actually asked for, by facet
    asked = {c[0] for c in board.calls if c[0]}
    assert len(asked) == 3, asked
    # and the URLs are built the normal way, not mangled by the facet path
    assert rows[0]["url"].startswith("https://acme.wd1.myworkdayjobs.com/External/job/")


def test_end_to_end_truncation_is_reported_against_the_TRUE_total():
    # Budget below the board: the run must say so, and say 55 rather than Workday's 40 --
    # a truncation note quoting the clamp would understate what is missing.
    saved = list(scraper.TRUNCATED)
    del scraper.TRUNCATED[:]
    try:
        rows = _run_against(_FakeBoard(), max_jobs=32)
        assert len(rows) <= 32 + scraper.WORKDAY_PAGE_LIMIT, len(rows)
        assert scraper.TRUNCATED, "a truncated faceted read must be recorded"
        note = scraper.TRUNCATED[-1]
        assert note["total"] == 55, note
        assert "jobFamilyGroup" in note["detail"], note
    finally:
        del scraper.TRUNCATED[:]
        scraper.TRUNCATED.extend(saved)


def test_an_unclamped_board_still_takes_the_plain_walk():
    # Regression guard for the 245 boards that were never broken. With total below the clamp
    # the planner returns None and not one faceted request may be made.
    class _Small(_FakeBoard):
        def post(self, url, headers=None, timeout=None, data=None):
            req = json.loads(data)
            self.calls.append((_arm_of(req["appliedFacets"]), req["offset"]))
            ids = list(range(1, 31))[req["offset"]:req["offset"] + req["limit"]]
            return _Resp({"total": 30, "jobPostings": [
                {"title": "Engineer %d" % i, "externalPath": "/job/X/Engineer_%d" % i,
                 "locationsText": "Reston, VA", "postedOn": "Posted Today"} for i in ids],
                "facets": [{"facetParameter": "jobFamilyGroup",
                            "values": [{"id": "a", "count": 20}, {"id": "b", "count": 40}]}]})

    board = _Small()
    rows = _run_against(board)
    assert len(rows) == 30, len(rows)
    assert not any(c[0] for c in board.calls), "no board under the clamp may be faceted"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  -", fn.__name__)
    print("\nAll %d Workday-facet checks passed (clamp=%d, max_jobs=%d)."
          % (len(fns), scraper.WORKDAY_TOTAL_CLAMP, scraper.WORKDAY_MAX_JOBS))
