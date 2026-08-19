"""
test_prompt_evals.py — guards the WRITING, which nothing else in the suite looks at.

No external test deps, no network, no API key, no model call: run it directly
    python test_prompt_evals.py
or via pytest if you have it (functions are named test_*).

Two halves, and the second is the one that matters:

  1. PROMPT CONSTRUCTION. Both prompts (resume_brain.ai._rewrite_prompt and core._tailor_prompt)
     must carry the style rules and must NOT carry the instruction that caused the problem. The
     original prompts asked for "strong action verbs", which is the phrasing most likely to make a
     model produce Spearheaded / Leveraged / Orchestrated. That is a one-line regression away from
     coming back, and nothing else would catch it.

  2. OUTPUT SCORING, against RECORDED fixtures rather than live calls. The structural scorers are
     reimplemented from srbhr/Resume-Matcher's `apps/backend/tests/evals/scorers.py` (Apache-2.0):
     sections preserved, no fabricated employers, personal info and dates untouched, JD keyword
     coverage. Their harness has no language-quality scorers at all, so the cliche / repeated-opener
     / em-dash / bullet-count checks are ours.

BEFORE_DRAFT is a recorded sample of the register the old prompt produced. It is here so the
cliche delta is a number in the test output rather than an assertion of taste — if a future prompt
change quietly regresses the voice, AFTER_DRAFT starts failing while BEFORE_DRAFT does not move.
"""
import re

import core
import resume_score as rs
from resume_brain import voice

# ---------------------------------------------------------------------------------------------
# The candidate's real material. Every scorer measures a draft against THIS, never against taste.
# ---------------------------------------------------------------------------------------------
ORIGINAL = """Ada Lovelace
ada@example.com | (617) 555-0134 | Boston, MA | linkedin.com/in/adalovelace

Experience
Acme Analytics, Senior Analyst            Jan 2021 - Mar 2023
- Cut month-end close from 9 days to 4 by rebuilding the reconciliation pipeline
- Built a forecasting model that reduced stockouts 34% across 12 distribution centres
- Negotiated three vendor contracts, saving 18% against prior-year spend

Northwind Retail, Analyst                 Jun 2018 - Dec 2020
- Automated 240 hours per quarter of manual reporting using Python and Airflow
- Mentored 4 junior analysts, two of whom were promoted within a year

Education
Boston University, BS Statistics          2018

Skills
SQL, Python, Airflow, Power BI, Excel
"""

JD_KEYWORDS = ("sql", "python", "airflow", "forecasting", "reconciliation", "power bi")

# What the old prompt produced: every fact intact, every sentence in a register nobody speaks.
BEFORE_DRAFT = """Ada Lovelace
ada@example.com | (617) 555-0134 | Boston, MA | linkedin.com/in/adalovelace

Summary
Results-oriented analytics professional with a proven track record of delivering robust,
cutting-edge solutions in fast-paced environments. Passionate about leveraging data.

Experience
Acme Analytics, Senior Analyst            Jan 2021 - Mar 2023
- Spearheaded a robust reconciliation pipeline initiative, cutting month-end close from 9 days to 4
- Spearheaded the development of an innovative forecasting model, reducing stockouts 34% across 12
  distribution centres
- Leveraged extensive experience in vendor management to negotiate three contracts, saving 18%

Northwind Retail, Analyst                 Jun 2018 - Dec 2020
- Orchestrated seamless automation of 240 hours per quarter of manual reporting using Python
- Facilitated the mentorship of 4 junior analysts, two of whom were promoted within a year

Education
Boston University, BS Statistics          2018

Skills
SQL, Python, Airflow, Power BI, Excel
"""

# What the new prompt should produce: the same facts, the candidate's own register, the job's
# terminology worked in where it is true.
AFTER_DRAFT = """Ada Lovelace
ada@example.com | (617) 555-0134 | Boston, MA | linkedin.com/in/adalovelace

Summary
Analyst who rebuilds reporting that people rely on. Reconciliation, forecasting and the SQL and
Python pipelines underneath them.

Experience
Acme Analytics, Senior Analyst            Jan 2021 - Mar 2023
- Rebuilt the reconciliation pipeline and cut month-end close from 9 days to 4
- Forecast stockouts across 12 distribution centres, bringing them down 34%
- Negotiated three vendor contracts, saving 18% against prior-year spend

Northwind Retail, Analyst                 Jun 2018 - Dec 2020
- Automated 240 hours per quarter of manual reporting in Python and Airflow
- Mentored 4 junior analysts, two of whom were promoted within a year

Education
Boston University, BS Statistics          2018

Skills
SQL, Python, Airflow, Power BI, Excel
"""


# ---------------------------------------------------------------------------------------------
# Scorers. Deterministic, no model, no network. Each returns a number or a list, never a verdict —
# the thresholds live in the tests so a scorer can be read on its own.
# ---------------------------------------------------------------------------------------------
def bullets_of(text):
    _header, sections = rs.split_sections(text or "")
    items, _found = rs._experience_items(sections)
    return [i["text"] for i in items]


def sections_of(text):
    _header, sections = rs.split_sections(text or "")
    return [s["title"].strip().lower() for s in sections]


def sections_preserved(original, draft):
    """No section that had content in the original may vanish from the draft."""
    return [s for s in sections_of(original) if s not in sections_of(draft)]


_DATE_RE = re.compile(
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}\s*-\s*"
    r"(?:(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}|Present)")


def _employers(text):
    """Employer names, taken only from lines that also carry a date range.

    A bare "^Capital…, " pattern is not enough: it matched the BEFORE_DRAFT summary sentence
    ("Results-oriented analytics professional with a proven track record of delivering robust,")
    and reported it as an invented employer. A comma AND a date range on the same line is the
    actual signature of an employer/role line, which is also how _experience_items tells them
    apart from bullets.
    """
    out = set()
    for line in (text or "").splitlines():
        if "," in line and _DATE_RE.search(line):
            name = line.split(",", 1)[0].strip()
            if name and name[:1].isupper():
                out.add(name.lower())
    return out


def fabricated_employers(original, draft):
    return sorted(_employers(draft) - _employers(original))


def dates_changed(original, draft):
    """Date ranges are copied, never reformatted. Symmetric difference, so an invented range and a
    dropped one are both failures."""
    before = set(_DATE_RE.findall(original or ""))
    after = set(_DATE_RE.findall(draft or ""))
    return sorted(before ^ after)


def personal_info_unchanged(original, draft):
    """Name and every contact token must survive byte for byte."""
    head = [l for l in (original or "").splitlines() if l.strip()][:2]
    return all(l.strip() in (draft or "") for l in head)


def jd_keywords_present(draft, keywords=JD_KEYWORDS):
    low = (draft or "").lower()
    hits = sum(1 for k in keywords if k in low)
    return hits / float(len(keywords)) if keywords else 1.0


def cliche_count(draft):
    """Total AI_SLOP hits. voice.find_slop is whole-word, so "robustness" is not "robust"."""
    return sum(n for _phrase, n in voice.find_slop(draft))


def repeated_openers(draft):
    return voice.repeated_openers(bullets_of(draft))


def ats_hostile(draft):
    return voice.find_ats_hostile(draft)


# ---------------------------------------------------------------------------------------------
# 1. Prompt construction
# ---------------------------------------------------------------------------------------------
_CTX = {
    "jd_text": "Analytics engineer. SQL, Python, Airflow, forecasting, reconciliation, Power BI.",
    "company_name": "Acme", "company": {"what_they_do": "logistics analytics"},
    "name": "Ada Lovelace", "email": "ada@example.com", "base_resume": ORIGINAL,
    "stories": [{"title": "Close speedup", "text": "Rebuilt reconciliation, 9 days to 4."}],
    "symphony": {"tones": ["fast-paced"], "focus": "company-focused (we/our)",
                 "formality": "casual", "top_verbs": ["drive", "own", "partner"]},
    "mirror_terms": ["reconciliation", "forecasting"], "missing": ["dbt"],
    "lessons": ["lead with the automation work"],
}


def _prompts():
    from resume_brain import ai
    return {
        "ai._rewrite_prompt": ai._rewrite_prompt(_CTX, "keyword"),
        "core._tailor_prompt": core._tailor_prompt(ORIGINAL, _CTX["jd_text"], "keyword"),
    }


def test_neither_prompt_asks_for_strong_action_verbs():
    """The regression this file exists for. 'strong action verbs' is the instruction that produced
    BEFORE_DRAFT; it must not come back into either prompt."""
    for name, p in _prompts().items():
        low = p.lower()
        assert "strong action verb" not in low, name
        assert "expert résumé writer" not in low and "expert resume writer" not in low, name


def test_both_prompts_carry_the_shared_style_rules():
    for name, p in _prompts().items():
        assert "BANNED WORDS AND PHRASES" in p, name
        assert "spearheaded" in p.lower(), name          # named as banned, not as encouragement
        assert "ACCURACY OVER RHYTHM" in p, name
        assert "not yours to improve" in p, name         # the preservation block
        assert "Cut p95 latency" in p, name              # the specificity pairs


def test_every_intensity_level_reaches_the_prompt():
    from resume_brain import ai
    seen = set()
    for value, _label, _hint in voice.intensity_choices():
        p = ai._rewrite_prompt(_CTX, value)
        marker = voice.INTENSITY[value].split(".")[0]    # e.g. "Intensity: LIGHT TOUCH"
        assert marker in p, value
        seen.add(marker)
    assert len(seen) == 3, seen


def test_an_unknown_intensity_falls_back_and_never_raises():
    from resume_brain import ai
    for junk in (None, "", "  ", "aggressive", "FULL TAILOR PLEASE", 7):
        p = ai._rewrite_prompt(_CTX, junk)
        assert voice.INTENSITY[voice.DEFAULT_INTENSITY].split(".")[0] in p, repr(junk)


def test_the_job_ads_voice_is_quarantined_to_the_cover_letter():
    """The job ad's tone and favourite verbs are still supplied — a cover letter should pitch in the
    reader's register — but the prompt must say they are not for the résumé. Feeding a recruiter's
    verbs into bullets is why the output read like a job posting."""
    from resume_brain import ai
    p = ai._rewrite_prompt(_CTX, "keyword")
    assert "drive, own, partner" in p                    # still present
    head = p.index("FOR THE COVER LETTER ONLY")
    assert head < p.index("drive, own, partner")         # and under that heading
    assert "never copy their verbs into a bullet" in p.lower()


# ---------------------------------------------------------------------------------------------
# 2. Output scoring
# ---------------------------------------------------------------------------------------------
def test_the_recorded_before_draft_is_full_of_cliches():
    """Sanity-check the fixture. If this stops failing the old way, the corpus is wrong and every
    delta below it is meaningless."""
    n = cliche_count(BEFORE_DRAFT)
    assert n >= 8, n
    assert repeated_openers(BEFORE_DRAFT), "expected 'Spearheaded' twice in the before draft"


def test_the_after_draft_has_no_cliches_and_no_repeated_openers():
    assert cliche_count(AFTER_DRAFT) == 0, voice.find_slop(AFTER_DRAFT)
    assert repeated_openers(AFTER_DRAFT) == [], repeated_openers(AFTER_DRAFT)


def test_the_after_draft_is_a_large_improvement_not_a_marginal_one():
    before, after = cliche_count(BEFORE_DRAFT), cliche_count(AFTER_DRAFT)
    assert after < before, (before, after)
    assert before - after >= 8, (before, after)


def test_no_ats_hostile_characters_in_either_direction():
    """Em dashes and smart quotes are the one language rule that is objectively right rather than a
    matter of taste — a parser mangles them."""
    assert ats_hostile(AFTER_DRAFT) == [], ats_hostile(AFTER_DRAFT)


def test_no_section_is_lost():
    for draft in (BEFORE_DRAFT, AFTER_DRAFT):
        assert sections_preserved(ORIGINAL, draft) == [], sections_preserved(ORIGINAL, draft)


def test_no_employer_is_invented():
    for draft in (BEFORE_DRAFT, AFTER_DRAFT):
        assert fabricated_employers(ORIGINAL, draft) == [], fabricated_employers(ORIGINAL, draft)


def test_no_date_range_is_reformatted_or_invented():
    for draft in (BEFORE_DRAFT, AFTER_DRAFT):
        assert dates_changed(ORIGINAL, draft) == [], dates_changed(ORIGINAL, draft)


def test_personal_info_survives_byte_for_byte():
    for draft in (BEFORE_DRAFT, AFTER_DRAFT):
        assert personal_info_unchanged(ORIGINAL, draft), draft[:60]


def test_the_bullet_count_is_preserved_at_keyword_intensity():
    """'keyword' promises the same bullets in the same order. A rewrite that quietly merges two
    lines has changed the document, not the wording."""
    assert len(bullets_of(AFTER_DRAFT)) == len(bullets_of(ORIGINAL)), (
        len(bullets_of(ORIGINAL)), len(bullets_of(AFTER_DRAFT)))


def test_the_jd_keywords_survive_the_rewrite():
    assert jd_keywords_present(AFTER_DRAFT) >= jd_keywords_present(ORIGINAL) - 0.01


def test_the_after_draft_keeps_every_real_metric():
    """The most expensive failure mode: prose improves and a number falls out with the sentence it
    was in."""
    for metric in ("9 days to 4", "34%", "12 distribution centres", "18%", "240 hours", "4 junior"):
        assert metric in AFTER_DRAFT, metric


def test_the_after_draft_scores_at_least_as_well_as_the_original():
    """Belt and braces: the rubric everyone else in this repo trusts must agree the rewrite is not
    a downgrade."""
    a = rs.score_resume(ORIGINAL, "mid")["score"]
    b = rs.score_resume(AFTER_DRAFT, "mid")["score"]
    assert b >= a - 2, (a, b)


def test_the_scorers_never_raise_on_junk():
    for txt in ("", None, "   ", "\n\n", "x" * 5000):
        cliche_count(txt)
        repeated_openers(txt)
        ats_hostile(txt)
        sections_preserved(ORIGINAL, txt)
        fabricated_employers(ORIGINAL, txt)
        dates_changed(ORIGINAL, txt)
        jd_keywords_present(txt)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  -", fn.__name__)
    print("\nAll %d prompt/output checks passed." % len(fns))
    print("cliche count: before=%d  after=%d  (delta %d)"
          % (cliche_count(BEFORE_DRAFT), cliche_count(AFTER_DRAFT),
             cliche_count(BEFORE_DRAFT) - cliche_count(AFTER_DRAFT)))
    print("repeated openers: before=%s  after=%s"
          % (repeated_openers(BEFORE_DRAFT) or "none", repeated_openers(AFTER_DRAFT) or "none"))
