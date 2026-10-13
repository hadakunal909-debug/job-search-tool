#!/usr/bin/env python3
"""Posting sponsorship readings and near-Apply evidence; no DB/network or web import."""
import os
import re
import socket
from pathlib import Path

os.environ["EV_OFF"] = "1"

def _no_network(*args, **kwargs):
    raise AssertionError("This sponsorship test must remain offline")

socket.create_connection = _no_network
socket.socket.connect = _no_network
socket.socket.connect_ex = _no_network

import core
from jinja2 import DictLoader, Environment

QUESTION = "Role can be filled by a candidate requiring sponsorship: "


def test_exact_posting_no_is_blocked_and_quoted():
    text = "Benefits include paid leave.\n" + QUESTION + "No"
    assert core.sponsorship_from_jd(text) == ("blocked", "JD says no visa sponsorship")
    evidence = core.sponsorship_evidence(text)
    assert evidence["verdict"] == "blocked"
    assert evidence["excerpt"] == QUESTION + "No"


def test_whitespace_and_case_do_not_change_the_answer():
    for text in (
        "ROLE\tcan  be filled\nby a candidate requiring sponsorship :\r\nNO",
        QUESTION.replace(" ", "\u00a0") + "No.",
        QUESTION + "\n No\nBenefits include paid leave.",
    ):
        assert core.sponsorship_from_jd(text)[0] == "blocked", repr(text)
        assert "No" in core.sponsorship_evidence(text)["excerpt"] or "NO" in core.sponsorship_evidence(text)["excerpt"]


def test_html_and_escaped_html_read_visible_answer():
    for text in (
        "<p>Role can be filled by a candidate requiring sponsorship: <strong>No</strong></p>",
        "<p>Role&nbsp;can be filled by a candidate requiring sponsorship:</p><p>No</p>",
        "&lt;p&gt;Role can be filled by a candidate requiring sponsorship: &lt;b&gt;No&lt;/b&gt;&lt;/p&gt;",
    ):
        evidence = core.sponsorship_evidence(text)
        assert evidence["verdict"] == "blocked", repr(text)
        assert evidence["excerpt"] == QUESTION + "No", evidence
        assert "<" not in evidence["excerpt"]


def test_answer_field_yes_is_an_explicit_offer():
    for text in (QUESTION + "Yes", QUESTION + "Yes.",
                 "<p>This position can be filled by a candidate who requires visa sponsorship: <b>Yes</b></p>"):
        assert core.sponsorship_from_jd(text) == ("open", "JD offers visa sponsorship"), repr(text)
        assert core.sponsorship_evidence(text)["verdict"] == "open"


def test_negative_answer_takes_precedence_over_general_offer():
    for text in (
        "Visa sponsorship is available.\n" + QUESTION + "No",
        QUESTION + "No.\nWe will sponsor qualified candidates.",
        QUESTION + "Yes.\nVisa sponsorship is not available for this position.",
    ):
        evidence = core.sponsorship_evidence(text)
        assert evidence["verdict"] == "blocked", text
        assert "No" in evidence["excerpt"] or "not available" in evidence["excerpt"]


def test_existing_restrictions_keep_precedence():
    for restriction in ("Must be a U.S. citizen.", "Active security clearance required.",
                        "Must be a permanent resident."):
        assert core.sponsorship_from_jd(QUESTION + "Yes.\n" + restriction)[0] == "blocked"


def test_existing_plain_offers_and_restrictions_still_read():
    for text in ("We do not offer visa sponsorship.", "Must be authorized to work without sponsorship."):
        assert core.sponsorship_from_jd(text)[0] == "blocked"
    for text in ("Visa sponsorship is available.", "We will sponsor qualified candidates."):
        assert core.sponsorship_from_jd(text)[0] == "open"


def test_silent_missing_and_unanswered_descriptions_stay_unknown():
    for text in (None, "", "Python and SQL skills required.", QUESTION,
                 QUESTION + "Unknown", QUESTION + "Not yet decided", QUESTION + "No decision yet",
                 "We assess sponsorship requirements individually. No relocation assistance is available."):
        assert core.sponsorship_from_jd(text) == ("", ""), repr(text)
        assert core.sponsorship_evidence(text) == {"verdict": "", "reason": "", "excerpt": ""}


def test_no_answer_is_not_inferred_from_distant_no():
    assert core.sponsorship_from_jd(QUESTION + "\nBenefits include paid leave. No remote work.") == ("", "")
    assert core.sponsorship_from_jd("Candidate requiring sponsorship: No") == ("", "")


def test_scripts_and_styles_do_not_supply_posting_answers():
    text = "<p>Build Python services.</p><script>" + QUESTION + "No</script><style>" + QUESTION + "Yes</style>"
    assert core.sponsorship_from_jd(text) == ("", "")


def test_excerpt_is_bounded_without_losing_restriction():
    text = "Benefits " + "include paid leave and insurance " * 30 + QUESTION + "No. " + "Next sentence " * 30
    evidence = core.sponsorship_evidence(text)
    assert evidence["verdict"] == "blocked"
    assert QUESTION + "No" in evidence["excerpt"]
    assert len(evidence["excerpt"]) <= 422, len(evidence["excerpt"])
    assert evidence["excerpt"].startswith("…")


def _render(verdict, include_reading=True, excerpt="", filing="H-1B"):
    template = (Path(__file__).parent / "templates" / "job.html").read_text(encoding="utf-8")
    env = Environment(loader=DictLoader({
        "job.html": template,
        "base.html": "{% block content %}{% endblock %}{% block scripts %}{% endblock %}",
        "_jobresearch.html": "",
    }), autoescape=True)
    env.filters["ago"] = lambda value: "recently"
    context = dict(
        row={"url": "https://example.test/job/1", "title": "Business Analyst", "company": "Example",
             "location": "Oakland, CA", "apply_url": "https://example.test/apply/1", "status": "",
             "salary_label": "", "remote": False, "repost": 0},
        asks={"exp_req": None, "exp_pref": None, "exp_inferred": False, "edu_req": "", "edu_pref": "",
              "exp_evidence": [], "level": "", "verdict": "posting"},
        about={"n_open": 1, "states": [], "remote": 0, "tech_stack": [], "bars": [], "strength_n": 0},
        filed=filing, narrowed="", gone=False, similar=[], similar_roles=[], have=[], missing=[],
        has_resume=False, has_jd=True, had_text=True, jd_html="<p>Full public posting.</p>", jd_jumps=[],
        jd_about="", research_pending=False,
        url_for=lambda name: "/" + name, csrf_token=lambda: "fictional-token",
        sponsor_window=lambda: "FY2020–FY2025", static_v=lambda name: "/static/" + name,
    )
    if include_reading:
        context["posting_sponsorship"] = {"verdict": verdict, "reason": "", "excerpt": excerpt}
    return env.get_template("job.html").render(**context)


def test_job_template_uses_posting_answer_not_filing_history():
    for verdict, answer in (("blocked", "No"), ("open", "Yes"), ("", "Unknown")):
        body = _render(verdict, excerpt=QUESTION + answer if verdict else "")
        before_apply = body.split('data-apply="1"')[0]
        assert re.search(r'data-posting-sponsorship="[^"]+">' + answer + r'</b>', before_apply)
        assert "H-1B Likely" not in before_apply
        history = body.split("Employer Sponsorship History")[1]
        assert "federal filing history for H-1B" in history
        assert "do not establish sponsorship for this posting" in history
        if verdict:
            assert "Posting evidence:" in before_apply
            assert QUESTION + answer in before_apply


def test_template_unknown_does_not_invent_quote_or_yes_from_no_record():
    body = _render("", filing="")
    assert 'data-posting-sponsorship="unknown">Unknown</b>' in body
    assert "data-sponsorship-evidence" not in body
    assert "There is no federal filing record on file" in body


def test_standalone_template_without_new_context_is_safe_unknown():
    assert 'data-posting-sponsorship="unknown">Unknown</b>' in _render("", include_reading=False)


def test_template_evidence_is_escaped_and_not_executable():
    body = _render("blocked", excerpt='<img src=x onerror="alert(1)">')
    assert "&lt;img" in body
    assert '<img src=x onerror="alert(1)">' not in body


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print("ok -", test.__name__)
    print("All %d posting sponsorship checks passed." % len(tests))
