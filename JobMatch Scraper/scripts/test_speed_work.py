"""Exact-result guards for feed counting and repeated resume-analysis work. Offline."""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["EV_OFF"] = "1"
os.environ.setdefault("APP_SECRET", "test-only-not-a-real-key")

import web
from resume_brain import analyze


def test_count_matches_visible_results():
    rows = [dict(url="https://example.test/%d" % i,
                 title="Data Scientist" if i % 2 else "Program Manager",
                 company="Employer %d" % (i % 4), location="Boston, MA",
                 score=i, sponsor_jd="blocked" if i % 5 else "",
                 date="2026-09-01", roles=["data" if i % 2 else "program"],
                 visa=["h1b"], closed=i % 7 == 0, intern=i % 11 == 0,
                 remote=i % 3 == 0, exp_eff=i % 8, exp_src="jd", level="mid",
                 salary_min=80000, salary_period="year", date_trusted=True)
            for i in range(80)]
    statuses = {r["url"]: ("liked", "applied", "hidden")[i % 3]
                for i, r in enumerate(rows) if i % 5 == 0}
    controls = [{}, {"min": "50"}, {"remote": "1"}, {"exp": "2"},
                {"exp": "entry"}, {"exp": "senior"}, {"intern": "only"},
                {"intern": "no"}, {"showclosed": "1"}, {"loc": "missing"},
                {"roles": "program"}, {"visatags": "h1b"},
                {"hidenospon": "1", "minsal": "100000"}, {"date": "7"}]
    for tab in ("recommended", "liked", "applied", "hidden"):
        for q in ("", "data scientst", "boston", "zzzz"):
            for sort in ("score", "newest", "sponsor"):
                for control in controls:
                    args = dict(control, tab=tab, q=q, sort=sort)
                    expected = len(web._filter_rows(rows, statuses, args))
                    # A count must not accidentally spend time ranking or rearranging rows.
                    with patch.object(web, "searchRank", side_effect=AssertionError), \
                            patch.object(web, "_break_employer_runs", side_effect=AssertionError):
                        assert web._filter_rows(rows, statuses, args, count_only=True) == expected, args
    assert web._filter_rows([], {}, {}, count_only=True) == 0


def test_rejected_rows_need_no_fuzzy_search():
    rows = [dict(url="x", score=80, closed=True),
            dict(url="y", score=80, closed=False, remote=False)]
    with patch.object(web, "_row_haystack", side_effect=AssertionError):
        assert web._filter_rows(rows, {}, {"q": "data scientst", "remote": "1"}) == []


def test_analysis_preserves_every_field():
    descriptions = ["", "We build software. You have experience with Python and SQL. " * 8,
                    "About us\nOur team values collaboration and ownership.\n"
                    "Requirements\nYou have experience with Python and SQL.\n"
                    "You will lead delivery and manage stakeholders.\n"
                    "Benefits\nWe offer mentorship and career growth.",
                    "Responsibilities Lead delivery across teams Manage stakeholder expectations "
                    "Build reporting tools Improve processes Qualifications Experience with Python " * 5]
    for jd in descriptions:
        sentences = analyze._sentences(jd)
        req = analyze.core._requirements_text(jd) or jd
        expected = dict(analyzed=analyze.core.analyze_jd(jd, {}),
                        looking_for=[s for s in analyze._sentences(req) if analyze._REQ_CUE.search(s)][:12],
                        culture_lines=[s for s in sentences if analyze._CULTURE_CUE.search(s)][:8],
                        symphony=analyze.symphony(jd))
        expected["terms"] = sorted(expected["analyzed"]["terms"],
                                   key=lambda t: -expected["analyzed"]["weight"].get(t, 0))
        with patch.object(analyze, "_sentences", wraps=analyze._sentences) as split:
            actual = analyze.analyze(jd, {})
            assert actual == expected
            assert split.call_count == (1 if req == jd else 2)


if __name__ == "__main__":
    for test in (test_count_matches_visible_results, test_rejected_rows_need_no_fuzzy_search,
                 test_analysis_preserves_every_field):
        test()
        print("ok -", test.__name__)
    print("PASS: performance shortcuts preserve results")
