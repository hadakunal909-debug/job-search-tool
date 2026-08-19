"""
test_resume_bullets.py — guards the per-bullet review, and specifically the RESULT detector, which
is the part that told well-written bullets they were duties.

No external test deps: run it directly
    python test_resume_bullets.py
or via pytest if you have it (functions are named test_*).

Background: the document rubric can say "6 of 15 bullets carry no number" and highlight them, but it
cannot say what to write instead. This module answers that, using the three-part model — action verb
+ scope + measurable result — so "this bullet is weak" becomes one of three concrete instructions.

Covers:
  * the hand-labelled result corpus. The first regex only accepted a percentage, a currency amount or
    a change verb beside a digit, so "Automated 240 hours per quarter" and "two of whom were
    promoted" both read as having NO result — time saved and countable outcomes are metrics, and
    telling a good bullet it is a duty is the most expensive kind of wrong
  * metric suggestions are keyed to the bullet's SUBJECT, not generic ("add a number" is what makes
    résumé tools useless)
  * weak openers map to replacements that fit what the weak verb was hiding
  * worst-first ordering, because the list is a work queue
"""
import resume_bullets as rb

# Hand-labelled. Left column is the bullet, right is whether a human would say it states a result.
RESULTS = (
    ("Cut month-end close from 9 days to 4 by rebuilding the pipeline", True),
    ("Automated 240 hours per quarter of manual reporting using Python", True),
    ("Mentored 4 junior analysts, two of whom were promoted within a year", True),
    ("Negotiated three vendor contracts, saving 18% against prior-year spend", True),
    ("Led a migration that retired $1.2M of annual licence cost", True),
    ("Reduced stockouts 34% across 12 distribution centres", True),
    ("Grew pipeline from 40 to 65 qualified accounts", True),
    ("Resolved 320 support tickets in the first quarter", True),
    # No result: reach, responsibility and process description are not outcomes.
    ("Rebuilt the KPI dashboard used daily by 80 stakeholders across 5 regions", False),
    ("Managed the reporting process for the finance team", False),
    ("Responsible for various reporting tasks and dashboards", False),
    ("Owned vendor relationships across the department", False),
    ("Coordinated across departments on quarterly planning", False),
)


def test_result_detection_matches_hand_labels():
    wrong = [(t, want, rb._has_result(t)) for t, want in RESULTS if rb._has_result(t) != want]
    assert not wrong, "\n".join("  %r want=%s got=%s" % w for w in wrong)


def test_time_saved_and_countable_outcomes_count_as_results():
    """The two shapes the first cut missed. Both are metrics in the standard taxonomy."""
    assert rb._has_result("Automated 240 hours per quarter of manual reporting")
    assert rb._has_result("Saved 12 hours a week on reconciliation")
    assert rb._has_result("Trained 9 analysts, six of whom were promoted")
    assert rb._has_result("Closed 40 audit findings")


def test_a_year_range_is_not_a_result():
    """The same trap the document rubric hit: dates are digits, and a role dated 2021-2023 must not
    read as a quantified achievement."""
    assert rb._has_result("Managed the reporting process Jan 2021 - Mar 2023") is False
    assert rb._has_result("Owned vendor relationships 2019 to 2022") is False


def test_metrics_are_keyed_to_the_subject():
    """Generic advice is what makes résumé tools useless."""
    vendor = rb.metrics_for("Managed the vendor contract renewals")
    assert any("dollar" in m for m in vendor), vendor
    dash = rb.metrics_for("Built a reporting dashboard for the team")
    assert any("dashboard" in m or "hours" in m for m in dash), dash
    people = rb.metrics_for("Mentored the junior analysts on the team")
    assert any("people" in m or "promoted" in m for m in people), people
    # Nothing recognisable still returns the taxonomy, not silence.
    assert rb.metrics_for("Did the thing") == list(rb._GENERIC_METRICS)


def test_weak_openers_map_to_fitting_replacements():
    """The replacement depends on what the weak verb was hiding: 'Assisted' wants verbs that say you
    did a thing, 'Participated in' wants verbs that say you drove one."""
    assert "Prepared" in rb.swaps_for("Assisted with the monthly close")
    assert "Led" in rb.swaps_for("Participated in the migration project")
    assert "Managed" in rb.swaps_for("Responsible for the reporting process")
    assert rb.swaps_for("Rebuilt the reconciliation pipeline") == []


def test_a_complete_bullet_scores_full_marks():
    b = rb.analyse_bullet({"text": "Cut month-end close from 9 days to 4 by rebuilding the pipeline",
                           "start": 0, "end": 62})
    assert b["score"] == 10.0, b
    assert b["missing"] == [] and b["problems"] == []
    assert b["metrics"] == []          # nothing to suggest when the result is already there


def test_a_duty_bullet_names_all_three_gaps():
    b = rb.analyse_bullet({"text": "Responsible for various reporting tasks", "start": 0, "end": 38})
    assert set(b["missing"]) == {"verb", "scope", "result"}
    assert b["score"] <= 1.0
    assert b["swaps"] and b["metrics"]
    assert len(b["problems"]) >= 3


def test_report_is_ordered_worst_first():
    """The list is a work queue: the user fixes three lines and stops, so the three worst must be at
    the top."""
    from test_resume_score import BAD, GOOD
    for txt in (GOOD, BAD):
        rows = rb.report(txt)["bullets"]
        assert rows == sorted(rows, key=lambda r: (r["score"], r["text"][:40]))


def test_report_counts_agree_with_the_rows():
    from test_resume_score import GOOD
    r = rb.report(GOOD)
    assert r["total"] == len(r["bullets"])
    assert r["complete"] == sum(1 for b in r["bullets"] if not b["missing"])
    assert r["missing_result"] == sum(1 for b in r["bullets"] if "result" in b["missing"])
    assert 0 <= r["complete_pct"] <= 100


def test_good_and_bad_separate_on_completeness():
    from test_resume_score import BAD, GOOD
    good, bad = rb.report(GOOD), rb.report(BAD)
    assert good["complete_pct"] >= 70, good["complete_pct"]
    assert bad["complete_pct"] == 0, bad["complete_pct"]


def test_spans_are_carried_through_for_highlighting():
    from test_resume_score import BAD
    for b in rb.report(BAD)["bullets"]:
        assert b["start"] is not None and b["end"] > b["start"]
        assert BAD[b["start"]:b["end"]] == b["text"]


def test_garbage_input_never_raises():
    for txt in ("", None, "   ", "\n\n", "x" * 4000, "- \n- \n"):
        r = rb.report(txt)
        assert r["total"] >= 0 and 0 <= r["complete_pct"] <= 100


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  -", fn.__name__)
    print("\nAll %d per-bullet checks passed." % len(fns))
