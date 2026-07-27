"""
test_scoring.py — guards the match-score fixes so the "fake 100%" bug can't silently return.

No external test deps: run it directly
    python test_scoring.py
or via pytest if you have it (functions are named test_*).

Covers:
  * thin/truncated JDs are flagged (-> the UI shows "JD pending", not a number)
  * whole-word matching ("data" must NOT match "database")
  * the score is floored, never rounded UP to a phantom 100%
  * is_agency flags staffing/consultancy firms but not real direct employers
"""
import core

_RICH_JD = (
    "We are seeking a Project Manager to lead cross-functional delivery. "
    "Responsibilities: manage project scope, budget, and schedule using Jira and MS Project. "
    "Requirements: PMP or CAPM, 3+ years project management, stakeholder management, risk "
    "management, agile and scrum experience, strong reporting and dashboards in Power BI, vendor "
    "management, process improvement, change management, and business requirements gathering. You "
    "will own status reporting, milestones, and deliverables across teams in a fast paced setting."
)


def test_thin_jd_is_flagged():
    assert core.analyze_jd("Project Coordinator needed. Apply now.", None)["thin"] is True
    assert core.analyze_jd(_RICH_JD, None)["thin"] is False


def test_empty_jd_is_thin_and_scores_zero():
    a = core.analyze_jd("", None)
    assert a["thin"] is True
    assert core.score_against("anything at all", a)[0] == 0


def test_whole_word_matching_not_substring():
    a = {"terms": ["data"], "weight": {"data": 1.0}, "total": 1.0, "thin": False}
    score, have, missing = core.score_against("i maintain a large database", a)
    assert have == [] and missing == ["data"] and score == 0      # "data" is not inside "database"
    score2, have2, _ = core.score_against("i analyze data every day", a)
    assert have2 == ["data"] and score2 == 100                    # real whole-word hit


def test_score_is_floored_never_rounds_up_to_100():
    # Résumé covers the heavy term but misses a tiny one: 1000/1001 = 99.9% must read 99, not 100.
    a = {"terms": ["alpha", "beta"], "weight": {"alpha": 1000.0, "beta": 1.0},
         "total": 1001.0, "thin": False}
    assert core.score_against("alpha only", a)[0] == 99
    # A genuine full sweep of every term may legitimately read 100.
    b = {"terms": ["alpha", "beta"], "weight": {"alpha": 1.0, "beta": 1.0},
         "total": 2.0, "thin": False}
    assert core.score_against("alpha and beta present", b)[0] == 100


def test_is_agency():
    for name in ("Actalent", "Apex Systems", "Insight Global", "Kforce", "TEKsystems",
                 "Financial Talent Group", "Phyton Talent Advisors", "Randstad Digital",
                 "Krest Global Solutions", "ABC Staffing", "Judge Direct Placement"):
        assert core.is_agency(name), "expected agency: %r" % name
    # Real direct employers must NOT be flagged — especially "<x> Technologies/Systems Inc",
    # which the broad body-shop regex would wrongly catch.
    for name in ("Tesla", "Mayo Clinic", "Bloomberg", "Google", "NVIDIA",
                 "Keysight Technologies Inc.", "Cadence Design Systems", ""):
        assert not core.is_agency(name), "should NOT be agency: %r" % name


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  -", fn.__name__)
    print("\nAll %d scoring checks passed." % len(fns))
