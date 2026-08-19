"""
test_resume_keywords.py — guards the Skills category, which is the one Resume Worded scores and
resume_score.py had no equivalent for.

No external test deps: run it directly
    python test_resume_keywords.py
or via pytest if you have it (functions are named test_*).

Covers the three things that were wrong on the way here, each of which made the check worse than
having no check at all:

  * expectations were truncated ALPHABETICALLY, so every résumé was judged against the first forty
    skills in the alphabet — a strong data résumé scored 20/100 and lost six points off the total
  * soft skills counted, so a résumé whose skills line read "Leadership, teamwork, communication"
    OUTSCORED one that demonstrated all three, while skills_demonstrated docked it for the same
    line. That is the exact criticism Resume Worded makes of keyword scanners
  * presence was measured over the whole document, so a keyword pasted into a skills list counted
    the same as one earned in an accomplishment
"""
import core
import resume_keywords as rk

DEV = """Ada Dev
ada@example.com | (617) 555-0134 | Boston, MA

Experience
Acme, Engineer                            Jan 2021 - Mar 2023
- Built and shipped a python service on kubernetes, cutting latency 40%
- Automated the sql reporting pipeline with terraform and git
- Led testing and code review for a team of 6

Skills
Python, SQL, Kubernetes, Terraform, Git
"""

# The trap: claims, not evidence. Its skills line is nothing but soft skills.
CLAIMS = """Jane Claims
jane@example.com | (617) 555-0199 | Boston, MA

Experience
Acme, Analyst                             Jan 2021 - Mar 2023
- Responsible for various tasks
- Assisted the team

Skills
Leadership, teamwork, communication, problem solving, collaboration, time management
"""


def test_expectations_are_ordered_by_demand_not_alphabetically():
    """The bug: sorted() on a term list is alphabetical, and the list gets truncated to TOP_N. That
    asked every résumé about the first forty skills in the alphabet."""
    terms, _src = rk.expected_terms("dev")
    assert terms, "no expectations at all"
    assert terms != sorted(terms), "expectations are still in alphabetical order"


def test_soft_skills_are_never_expectations():
    """An ATS matches hard skills. Soft skills are earned through evidence, which is what the
    leadership and skills-demonstrated checks measure — expecting them here rewarded the bare
    claim the rest of the rubric penalises."""
    for track in rk.TRACKS + (None,):
        terms, _ = rk.expected_terms(track)
        bad = [t for t in terms if t.lower() in rk._SOFT]
        assert not bad, "%r expects soft skills: %s" % (track, bad)


def test_evidence_scope_ignores_a_skills_list():
    """A keyword in a skills line is a claim; the same keyword in a bullet is evidence."""
    listed_only = "Experience\nAcme, Analyst   Jan 2021 - Mar 2023\n- Did things\n\nSkills\nPython\n"
    whole = rk.evaluate(listed_only)
    evidence = rk.evaluate(listed_only, evidence_text="Did things")
    assert whole["scope"] == "document"
    assert evidence["scope"] == "experience"
    assert "python" not in [t.lower() for t in evidence["have"]], evidence["have"]


def test_claims_do_not_outscore_evidence():
    """The regression that mattered: a résumé of pure soft-skill claims must not beat one that
    demonstrates hard skills."""
    def ev(txt):
        import resume_score as rs
        items = [i for s in rs.split_sections(txt)[1] if s["group"] == "experience"
                 for i in s["items"] if i["bullet"]]
        return rk.evaluate(txt, evidence_text=" ".join(i["text"] for i in items))

    dev, claims = ev(DEV), ev(CLAIMS)
    assert dev["score"] > claims["score"], (dev["score"], claims["score"])


def test_track_is_inferred_from_the_resume():
    """There is no job here, so the track cannot come from a title. It comes from which track's
    expectations the résumé already satisfies."""
    if rk.load_expectations() is None:
        print("      (skipped: resume_keywords.json not built)")
        return
    assert rk.infer_track(DEV) == "dev", rk.infer_track(DEV)
    assert rk.infer_track("") in rk.TRACKS


def test_presence_uses_whole_word_matching():
    """Reuses core._term_present, so 'data' must not be answered by 'database' — the same rule
    test_scoring.py pins for the feed's matcher."""
    low = "i maintain a large database"
    words = core._resume_wordset(low)
    assert rk._present("database", low, words) is True
    assert rk._present("data", low, words) is False


def test_evaluate_never_raises_and_is_deterministic():
    for txt in ("", None, "   ", "x" * 5000, DEV):
        out = rk.evaluate(txt)
        assert 0 <= out["score"] <= 100
        assert set(out) >= {"track", "source", "have", "missing", "score", "total", "scope"}
    assert len(set(rk.evaluate(DEV)["score"] for _ in range(4))) == 1


def test_a_missing_data_file_degrades_to_curation_not_to_nothing():
    """The built file is optional. Without it the check must still work from the curated skill set —
    narrower coverage, but never a wrong answer, and never a free 10/10."""
    saved = rk._cache.copy()
    try:
        rk._reset_cache()
        rk._cache.update({"data": None, "loaded": True})     # pretend the file is absent
        terms, src = rk.expected_terms("dev")
        assert src == "curated" and terms
        assert rk.evaluate(DEV)["total"] > 0
    finally:
        rk._cache.clear()
        rk._cache.update(saved)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  -", fn.__name__)
    print("\nAll %d résumé-keyword checks passed." % len(fns))
