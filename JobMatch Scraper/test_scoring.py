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

# ---- the 2026-09-02 JD-reading repair ---------------------------------------------------------
#
# analyze_jd matched ATS_KEYWORDS with a bare `kw in jd_low`, which invented a hard skill in 89%
# of stored postings. Each phantom then took x2.5 for being a "hard skill" and x1.6 again for the
# requirements section, so it outweighed the terms the job actually named and landed inside
# core_terms -- it moved the match percentage, not just the chip list. Both columns below are
# measured document frequencies from the real corpus; see scripts/measure_jd_reading.py.

_PHANTOMS = (
    ("We work across every division and supervision tier.", "visio"),      # 59.5% of postings
    ("A track record of excellence in delivery.", "excel"),                # 37.1%
    ("You will translate requirements for partners.", "sla"),              # 24.7%
    ("Own our digital roadmap end to end.", "git"),                        # 24.0%
    ("Maintain a strong safety culture and work safely.", "safe"),         # 20.6%
    ("Keep the cleaning schedule current.", "lean"),                       # 9.0%
)

_INFLECTIONS = (
    ("We align stakeholders across every team.", "stakeholder"),           # 7,165 postings
    ("You will own budgets and budgeting.", "budget"),                     # 2,238
    ("Publish roadmaps each quarter.", "roadmap"),                         # 1,126
    ("Report on KPIs every week.", "kpi"),                                 # 968
    ("Drive implementations to completion.", "implementation"),            # 720
    ("Run retrospectives after each sprint.", "retrospective"),            # 217
)


def test_ats_keywords_are_not_minted_from_longer_words():
    for text, phantom in _PHANTOMS:
        terms = core.analyze_jd(_RICH_JD + " " + text, None)["terms"]
        assert phantom not in terms, "%r invented from %r" % (phantom, text)


def test_ats_keyword_inflections_are_still_earned():
    """A word boundary on BOTH sides would have been the easy fix and a worse bug: these are
    the matches the substring rule was legitimately making."""
    for text, kw in _INFLECTIONS:
        terms = core.analyze_jd(text * 8, None)["terms"]
        assert kw in terms, "%r lost from %r" % (kw, text)


def test_punctuated_skill_names_are_reachable_at_all():
    """extract_keywords strips "-.+#/" and drops anything under three characters, so c++ became
    "c" and vanished -- measured, ci/cd is named in 12.0% of descriptions and could never once
    become a keyword. They are ATS_KEYWORDS members now, matched as phrases."""
    terms = core.analyze_jd(
        _RICH_JD + " You will write C++ and C# and own the CI/CD pipeline.", None)["terms"]
    for kw in ("c++", "c#", "ci/cd"):
        assert kw in terms, "%r still unreachable" % kw


def test_a_phrase_must_be_NAMED_not_merely_scattered():
    """The resume side asks "did you do this" and a scattered-stem match is right for it. The JD
    side asks "does this posting NAME this", and there the same rule fired `business
    requirements` on 63.8% of postings, because almost every description contains "business"
    somewhere and "requirements" somewhere."""
    scattered = ("Requirements: five years running a business unit. Responsibilities include "
                 "scheduling, vendor negotiation, budget ownership and reporting. " * 4)
    assert "business requirements" not in core.analyze_jd(scattered, None)["terms"]
    named = scattered + " You will gather business requirements from partners."
    assert "business requirements" in core.analyze_jd(named, None)["terms"]


def test_the_resume_side_still_matches_a_scattered_phrase():
    """The other half of that contract: phrase_exact must not have leaked onto the resume."""
    a = {"terms": ["project management"], "weight": {"project management": 1.0},
         "total": 1.0, "thin": False}
    # PRESENCE, not the number: a one-term analysis is capped at 16 by the confidence cap, and
    # phrase_exact is about whether the term is found, not about what it then scores.
    have = core.score_against("managed multiple projects end to end", a)[1]
    assert have == ["project management"], have


def test_an_unseen_term_is_not_the_heaviest_thing_in_the_table():
    """`sorted(idf.values())[len // 2]` was meant to be a median and was the MAXIMUM: 54.4% of
    idf.json's entries sit at max(idf), so the median of the distinct value list lands inside
    that block. This is the assertion that would have caught it."""
    assert core._UNSEEN_W < core._RARE_W_CAP
    assert core._UNSEEN_W < 10.8216            # max(idf) on the live corpus


def test_analyze_jd_term_order_is_deterministic():
    """jd_terms is TEXT and score_jobs DIFFS the stored string against the one it just built to
    decide whether to write. terms came from `list(<set of str>)`, whose order PYTHONHASHSEED
    randomises per process, so the diff never matched and the full pass re-upserted the whole
    corpus every day."""
    a = core.analyze_jd(_RICH_JD, None)["terms"]
    b = core.analyze_jd(_RICH_JD, None)["terms"]
    assert a == b == sorted(a), "term order is not repeatable"


def test_display_terms_refuses_eligibility_gates_and_keeps_short_skills():
    """A clearance is not a skill you can choose to add, so offering it under "worth adding" is
    advice nobody can act on -- the owner's own example. And a flat three-character floor hid
    c#, go, bi, qa and ux, every one of them an ATS keyword."""
    got = core.display_terms(
        ["clearance", "security clearance", "collaboration", "power bi", "c#",
         "Acme Corp", "tuition", "sql"], "Acme Corp", 10)
    assert got == ["power bi", "c#", "sql"], got


def test_display_terms_drops_a_term_that_lives_only_in_the_notice():
    got = core.display_terms(["regarding criminal", "power bi"], "Acme", 10,
                             body="own the power bi roadmap",
                             boiler="will receive consideration regarding criminal history")
    assert got == ["power bi"], got


def test_an_acronym_that_is_also_an_english_word_needs_its_own_case():
    """A word boundary is necessary and not sufficient. `safe` is SAFe, the Scaled Agile
    Framework; once the boundary fix stopped it matching "safety" it still matched the
    adjective. Measured over 6,000 stored descriptions: the word "safe" appears in 13.8% and
    only 15.5% of those are the framework, so 11.7% of the corpus was credited with a hard
    skill it never named. Case is the discriminator a lowercased pipeline throws away."""
    pad = (" Responsibilities include reporting, budget ownership and vendor negotiation."
           " Requirements: five years of program management and stakeholder management.") * 3
    for text, kw, want in (
            ("Maintains a safe and clean work area at all times.", "safe", False),
            ("Experience with SAFe and scaled agile delivery.", "safe", True),
            ("Certified SAFE Agilist leading a release train.", "safe", True),
            ("We lean into feedback and keep a lean team.", "lean", False),
            ("Lean and Six Sigma driven continuous improvement.", "lean", True)):
        got = kw in core.analyze_jd(text + pad, None)["terms"]
        assert got is want, "%r: expected %s for %r" % (kw, want, text)



if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  -", fn.__name__)
    print("\nAll %d scoring checks passed." % len(fns))
