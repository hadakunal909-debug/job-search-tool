#!/usr/bin/env python3
"""
test_pm_rule.py — freezes core.reads_like_pm, the rule that lets a posting into the corpus on
the strength of its DESCRIPTION when its title matched nothing.

The thresholds were set by measurement (scripts/calibrate_pm_rule.py, 300 postings a bucket on
2026-08-20), and the properties below are the ones that make the measurement mean anything. A
future edit that raises recall by weakening the anchor gate will pass the calibration sweep and
fail here, which is the point.

No database and no network.
"""
import core

# A real posting reads like this: it names the work, not just the vocabulary.
PM_JD = """
Responsibilities: own the project plan end to end, track milestones and deliverables, manage
stakeholder expectations across the business, chair the weekly steering committee, maintain the
risk register and escalate blockers. You will coordinate cross-functional teams, report status
to leadership, and keep delivery on time and within budget. Jira and Confluence.
"""

# The trap this rule exists to survive. Every agile word is here and NONE of the work is
# delivery ownership.
SWE_JD = """
You will design and build backend services in Go, write unit tests, review pull requests,
participate in sprint planning and daily scrum, groom the backlog, and work with
cross-functional partners against the roadmap. Kubernetes, Docker, CI/CD, observability.
Requirements: 5 years of software engineering, strong knowledge of distributed systems.
"""

CLINICAL_JD = """
Provide direct patient care, administer medications, document in the EMR, and collaborate with
the care team on the unit. BSN required. Reporting to the charge nurse. Rotating shifts.
"""


def test_the_shipped_thresholds_are_the_measured_ones():
    """If these change, scripts/calibrate_pm_rule.py must have been re-run and its table in
    core.py updated. They are asserted so the numbers in that comment cannot go stale silently.
    """
    assert core.PM_MIN_ANCHORS == 2
    assert core.PM_MIN_POINTS == 6
    assert core.PM_ANCHOR_WEIGHT == 2


def test_a_delivery_posting_fires():
    assert core.reads_like_pm(PM_JD)


def test_a_software_posting_does_not():
    """Measured tech-fire rate at the shipped gate is 12.8%, so this is not a claim that no
    engineering JD ever fires -- it is a claim that the *agile vocabulary alone* is not enough,
    which is the specific failure the two-tier split was built for."""
    assert not core.reads_like_pm(SWE_JD)


def test_clinical_work_is_nowhere_near():
    assert not core.reads_like_pm(CLINICAL_JD)


def test_support_words_alone_can_never_carry_a_posting():
    """The anchor gate is the load-bearing half. Twenty support words and no anchor is still a
    no, however high the points total climbs."""
    text = " ".join(core.PM_SUPPORT)
    anchors, support, _veto = core.pm_signal(text)
    assert anchors == 0, "a support-only text must score zero anchors"
    assert support >= 20, "sanity: the support list should light up on itself"
    assert core.pm_points(anchors, support) >= core.PM_MIN_POINTS, (
        "this test is only meaningful while the points gate is passed")
    assert not core.reads_like_pm(text)


def test_repetition_cannot_inflate_a_score():
    """DISTINCT phrases, not occurrences. One word said fifty times is one signal — otherwise a
    JD that happens to repeat "stakeholder" outranks one that does the job."""
    once = "stakeholder management and the project plan"
    many = once + (" stakeholder" * 50)
    assert core.pm_signal(once) == core.pm_signal(many)


def test_the_two_lists_do_not_overlap():
    """A phrase in both tiers would be counted twice and weighted 3x, silently. Anchors are
    matched whole-phrase, so an anchor containing a support word ("project plan" vs "scope") is
    fine; identical entries are not."""
    dupes = set(core.PM_ANCHORS) & set(core.PM_SUPPORT)
    assert not dupes, "in both PM_ANCHORS and PM_SUPPORT: %s" % sorted(dupes)


def test_thresholds_are_reachable():
    """A points gate below the anchor floor is dead configuration: PM_MIN_ANCHORS anchors
    already score PM_ANCHOR_WEIGHT * PM_MIN_ANCHORS, so a lower gate never rejects anything."""
    assert core.PM_MIN_POINTS >= core.PM_ANCHOR_WEIGHT * core.PM_MIN_ANCHORS


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  -", fn.__name__)
    print("\nAll %d PM-rule checks passed." % len(fns))
