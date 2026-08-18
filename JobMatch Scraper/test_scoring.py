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
    # The SCORE here is capped at 16 by the one-term confidence rule (covered separately);
    # what this test is about is which side of have/missing the term lands on.
    assert have2 == ["data"] and score2 > 0                       # real whole-word hit


def test_score_is_floored_never_rounds_up_to_100():
    # Enough terms to clear the confidence cap, so this tests the FLOORING rule and nothing else.
    # Résumé covers the heavy terms but misses a tiny one: 99.9% must read 99, never 100.
    heavy = {t: 1000.0 for t in ("alpha", "beta", "gamma", "delta", "epsilon", "zeta")}
    heavy["tiny"] = 1.0
    a = {"terms": list(heavy), "weight": heavy, "total": sum(heavy.values()), "thin": False}
    assert core.score_against("alpha beta gamma delta epsilon zeta", a)[0] == 99
    # A genuine full sweep of every term may legitimately read 100.
    even = {t: 1.0 for t in ("alpha", "beta", "gamma", "delta", "epsilon", "zeta")}
    b = {"terms": list(even), "weight": even, "total": 6.0, "thin": False}
    assert core.score_against("alpha beta gamma delta epsilon zeta", b)[0] == 100


def test_matching_is_morphological_not_literal():
    """A spelling gap is not a qualification gap. Every pair below was a MISS before: `kpi` and
    `kpis` were two different skills across 890 live postings, and a resume saying "budgets"
    failed a posting asking for "budgeting"."""
    r = "project manager who managed budgets, built dashboards and tracked kpis"
    w = core._resume_wordset(r)
    for term in ("budgeting", "budget", "kpi", "kpis", "dashboard", "dashboards",
                 "project management", "managing"):
        assert core._term_present(term, r, w), term


def test_a_phrase_needs_all_of_itself():
    """"risk management" must not be answered by the word "management" on its own — the whole
    point of scoring phrases is that they are more specific than their parts."""
    r = "experienced in management of large teams"
    w = core._resume_wordset(r)
    assert core._term_present("management", r, w)
    assert not core._term_present("risk management", r, w)
    assert not core._term_present("stakeholder management", r, w)


def test_aliases_resolve_in_both_directions():
    """A posting asking for "microsoft project" is answered by a resume that wrote "MS Project",
    and the reverse. A forward-only map gets exactly half of these."""
    a = core._resume_wordset("delivered with ms project and power bi")
    assert core._term_present("microsoft project", "delivered with ms project and power bi", a)
    b = core._resume_wordset("delivered with microsoft project")
    assert core._term_present("ms project", "delivered with microsoft project", b)


def test_a_rare_word_does_not_outrank_a_real_skill():
    """idf rewards rarity, and rarity in a job corpus usually means a company name or a one-off
    turn of phrase. "caterpillar inc" outranking "pmp" is how 63% of the terms being screened on
    came to be words appearing in exactly one posting."""
    idf = {"pmp": 4.0, "caterpillar inc": 10.2, "budget": 3.0}
    a = core.analyze_jd("PMP certification required. Manage budget for caterpillar inc.", idf)
    assert "pmp" in a["weight"] and "caterpillar inc" in a["weight"]
    assert a["weight"]["pmp"] > a["weight"]["caterpillar inc"], a["weight"]


def test_a_posting_we_barely_read_cannot_claim_a_strong_match():
    """The confidence cap. A "Senior Delivery Manager" whose analysis yielded ONE term scored
    100% because the résumé held that term, and 9% of the live corpus was being judged on three
    terms or fewer. The ceiling now rises with how much of the role we could actually read."""
    one = {"terms": ["python"], "weight": {"python": 1.0}, "total": 1.0, "thin": False}
    assert core.score_against("python", one)[0] == 16          # 1 of 6
    three = {"terms": ["python", "sql", "aws"],
             "weight": {"python": 1.0, "sql": 1.0, "aws": 1.0}, "total": 3.0, "thin": False}
    assert core.score_against("python sql aws", three)[0] == 50   # 3 of 6
    six = {t: 1.0 for t in ("python", "sql", "aws", "docker", "kafka", "spark")}
    full = {"terms": list(six), "weight": six, "total": 6.0, "thin": False}
    assert core.score_against("python sql aws docker kafka spark", full)[0] == 100


def test_thin_scores_zero_but_still_reports_keywords():
    """A JD we could not analyse scores 0 — but the keyword lists still come back, because the
    job page's panel and the résumé tailorer both read them and "cannot score" is not "found
    nothing"."""
    a = core.analyze_jd("Python SQL")            # too short: thin
    score, have, missing = core.score_against("python sql", a)
    assert score == 0
    assert have or missing


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
