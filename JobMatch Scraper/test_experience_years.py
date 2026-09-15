"""
test_experience_years.py — the fixture set for core.experience_years and its helpers.

No external test deps, no network: run it directly
    python test_experience_years.py
or via pytest if you have it (functions are named test_*).

This parser had no suite until 2026-09-03, and it decides the feed's experience filter, the
scraper's MAX_YEARS hard drop, and the "N+ yrs" chip on every card. Measured over the 41,434
cached descriptions, the version it replaced read a requirement out of 65.8% of them and missed
one that was plainly written in 6.0%.

THE ASYMMETRY, and it runs the opposite way to test_liveness'. Reading a floor too HIGH hides a
posting the reader qualifies for and nobody ever learns it existed. Reading one too LOW shows a
senior job in an entry-level feed, which costs a glance. So where the two readings are equally
defensible this parser takes the lower one — but only where they are, which is what the ladder
cases below pin down.
"""
import core


def _eq(text, want):
    got = core.experience_years(text)
    assert got == want, "%r -> %r (wanted %r)" % (text[:70], got, want)


def test_digits_the_original_behaviour():
    """Unchanged, and first, because everything else is measured against it."""
    _eq("5 years of experience", 5)
    _eq("5+ years of relevant experience", 5)
    _eq("3-5 years of professional experience", 3)      # a range states its floor
    _eq("3 to 5 years of industry experience", 3)
    _eq("7 yrs experience", 7)
    _eq("2-3 yearsof experience in accounting", 2)
    _eq("two yearsof experience in accounting", 2)
    _eq("2 yearsoftware development", None)


def test_a_bare_year_count_is_not_a_requirement():
    """The context gate. Without it a benefits page becomes a seniority bar."""
    _eq("401k vesting after 3 years", None)
    _eq("No experience necessary", None)
    _eq("", None)
    _eq(None, None)
    # >20 is a company statistic, not a person's career.
    _eq("Tapestry Solutions brings over 30 years of industry experience", None)
    _eq("Our company brings over 100 years of industry experience", None)


def test_spelled_out_numbers():
    """1,623 descriptions state their floor ONLY in words. A digits-only rule read every one of
    them as 'no requirement stated', and None means KEEP."""
    _eq("Minimum of five years of construction project management experience", 5)
    _eq("Four (4) years of experience in continuous improvement", 4)
    _eq("Minimum of eight (8) years of experience in medical device development", 8)
    _eq("two to three years of professional experience", 2)
    _eq("ten years of relevant experience", 10)


def test_months():
    """Floor-divided: under a year is a floor of zero, and that is a real answer."""
    _eq("6 months of healthcare scheduling experience is required", 0)
    _eq("18 months of relevant experience", 1)


def test_requirements_heading_counts_as_context():
    """A requirements LIST does not repeat the word 'experience' on every bullet. 800
    descriptions were missed for exactly this."""
    _eq("Basic Qualifications 7+ years of security engineering or security operations", 7)
    _eq("Minimum Qualifications 5+ years software engineering and/or production experience", 5)


def test_required_beats_preferred():
    """'5 required, 10+ preferred' is a five-year job. Reading the maximum over both made it a
    ten-year job and hid a role the reader qualifies for."""
    _eq("5 years of experience required; 10+ years preferred", 5)
    _eq("Required: 3 years of experience. Preferred: 7 years of experience.", 3)
    # ...but a floor stated ONLY as a preference still stands. An employer asking for ten years,
    # however politely, is not describing an entry-level job.
    _eq("10+ years of experience preferred", 10)


def test_required_and_preferred_sections_keep_their_meaning():
    for sep in ("\n", "\n\n", " "):
        text = sep.join(["Required Qualifications", "2 years of experience",
                         "Preferred Qualifications", "5 years of experience"])
        assert core.experience_floors(text) == (2, 5)
        _eq(text, 2)
        evidence = core.experience_evidence(text)
        assert [(e["years"], e["kind"]) for e in evidence] == [(2, "required"), (5, "preferred")]
        assert all(e["quote"] in text for e in evidence)
    _eq("Preferred Qualifications\n5 years of experience\nRequired Qualifications\n2 years of experience", 2)
    _eq("Basic Qualifications\n2 years in analytics\nPreferred Qualifications\n5 years in analytics", 2)
    _eq("Required Qualifications\n2 years of experience\nBenefits\nPaid sabbatical after 5 years", 2)
    _eq("Preferred Qualifications\nMinimum 3 years of experience required", 3)
    assert core.experience_floors("Preferred Qualifications\n3-5 years of experience, "
                                 "with at least 2 years supporting products") == (None, 3)
    assert core.experience_floors("Preferred Qualifications\nMinimum 5 years of experience") == (None, 5)
    _eq("Basic Qualifications 5+ years building React applications", 5)
    assert core.experience_floors("Apply even if you do not meet all preferred qualifications. "
                                 "2 years of experience required.") == (2, None)
    assert core.experience_floors("We welcome applicants who lack some preferred qualifications. "
                                 "2 years of experience.") == (2, None)


def test_year_contracts_are_not_experience_requirements():
    _eq("Required Qualifications\nThis is a 2 year contract in software development.", None)
    _eq("Required Qualifications\n2 years of experience on contract projects", 2)
    _eq("Requirements: Must be at least 18 years of age", None)
    _eq("Minimum 18 years old. Required Qualifications\n2 years of experience", 2)
    _eq("Required Qualifications 2 years of non-internship design or architecture work", 2)


def test_parser_rule_changes_restart_the_scoring_cursor():
    import re
    from scraper.score_jobs import _score_rev
    before = _score_rev("test resume")
    assert before != "norev"
    original = core._EXP_SECTION_RE
    try:
        core._EXP_SECTION_RE = re.compile(original.pattern + "|new heading", original.flags)
        assert _score_rev("test resume") != before
    finally:
        core._EXP_SECTION_RE = original
    assert _score_rev("test resume") == before


def test_audit_requires_the_same_description_and_checks_actual_values():
    from scripts.audit_experience import inspect_row
    import db
    text = "Required Qualifications\n2 years of experience\nPreferred Qualifications\n5 years of experience"
    row = {"url": "https://example.test/job", "jd_fp": db.jd_fingerprint(text), "exp_max_years": 5}
    finding = inspect_row(row, text)
    assert finding["status"] == "mismatch"
    assert finding["parsed_years"] == 2 and finding["preferred_years"] == 5
    assert inspect_row(dict(row, exp_max_years=2), text)["status"] == "consistent"
    assert inspect_row(row, text + " updated")["status"] == "different_description"
    assert inspect_row(dict(row, jd_fp=None), text)["status"] == "unverified_text"
    assert inspect_row(row, "")["status"] == "missing_description"


def test_an_and_list_keeps_its_maximum():
    """The case the strict reading was built for, and the ladder rule must not touch it: two
    requirements that both apply. No degree words, no alternation."""
    _eq("8+ years of engineering experience and 2 years of SQL experience", 8)


def test_the_degree_ladder_collapses_to_its_lowest_rung():
    """Alternatives, not a stack. Whoever holds the higher degree qualifies with fewer years,
    so the floor for the posting is the smallest rung."""
    _eq("Doctorate degree OR Master's degree and 2 years of experience OR Bachelor's degree "
        "and 4 years of experience OR Associate's degree and 8 years of experience OR High "
        "school diploma and 10 years of experience", 2)
    _eq("Required Qualifications: Bachelor's Degree with 8+ years of experience in Engineering "
        "or related field or master's degree with 7+ years of experience or Doctorate Degree "
        "with 2+ years of experience", 2)
    _eq("Preferred Qualifications: Bachelor of Science and 2+ years of related work experience "
        "OR Bachelor's Degree and 6+ years of directly related work experience OR 10+ years "
        "of related, relevant experience. 2+ years of experience with Java.", 2)


def test_the_ladder_rule_does_not_over_fire():
    """Three guards, and each one is a measured regression rather than a hypothetical.

    ADJACENCY: 89.9% of cached descriptions are a single blob with no newline, so grouping the
    rungs BY LINE put every year mention in a document into one group and collapsed the lot the
    moment the text said 'or' and 'degree' anywhere. That moved 9,958 descriptions and is the
    'senior req hides behind its junior line item' bug the maximum exists to prevent.

    ONE SENTENCE: a ladder is punctuated with commas and slashes, never a full stop.

    BOTH SIGNALS: alternation AND the degrees being traded against.
    """
    _eq("We need 9 years of leadership experience. Separately, our CEO has a degree or two "
        "and 2 years of experience here.", 9)
    # "or" with no degree words is not a ladder.
    _eq("9 years of experience in Python or Java, and 2 years of experience in SQL", 9)
    # A degree word with no alternation is not a ladder either.
    _eq("Bachelor's degree required. 9 years of engineering experience. 2 years of SQL "
        "experience.", 9)


def test_min_years_still_reads_the_floor():
    """experience_min_years answers a different question and keeps answering it."""
    assert core.experience_min_years("3-5 years of professional experience") == 3
    assert core.experience_min_years("no numbers here") is None


def test_required_years_is_zero_not_none():
    """The scraper's hard-drop reads this one and does arithmetic on it."""
    assert core.required_years("5 years of experience") == 5
    assert core.required_years("nothing stated") == 0


def test_a_rung_does_not_have_to_repeat_the_context_word():
    """The commonest phrasing of the ladder, and the one the first fix could not read.

    Employers drop "of experience" on the trailing rung constantly. Filtering hits on their own
    context word BEFORE grouping threw that rung away, leaving the senior rung alone in its
    group to answer for the whole posting -- the exact ten-versus-five error the ladder rule
    exists to prevent. The corpus has "...Master's with 6 years, or 12 years in lieu of degree"
    reading as twelve.
    """
    _eq("Bachelor's degree and 10 years of relevant experience, or a Master's degree and "
        "5 years", 5)
    _eq("Master's with 6 years, or 12 years in lieu of degree in project management", 6)
    # ...and a context-less count still cannot START a group, or every stray number is a floor.
    _eq("Our office opened 7 years ago. We ship software.", None)


def test_a_mixed_range_reads_its_floor_not_its_ceiling():
    """The dedupe test only asked whether a match STARTED inside a claimed span.

    The digit pass claims "3 years" out of "two to 3 years"; the word pass then matches the
    whole range starting BEFORE that span, so both survived and max() answered the ceiling.
    """
    _eq("two to 3 years of professional experience", 2)
    _eq("three to 5 years of relevant experience", 3)
    _eq("1 to three years of industry experience", 1)


def test_a_duration_is_not_experience():
    """Months read contract length, and a zero is worse than a None here.

    None means "states nothing" and the filter shows the posting. A 0 is a CONFIDENT answer: it
    survives "hide postings we couldn't read" and presents as a verified entry-level job.
    """
    _eq("This is a 12 month contract role in software development.", None)
    _eq("You will complete a 6 month rotation through our engineering organisation.", None)
    _eq("A 24 month fixed-term assignment in our development team.", None)
    _eq("18 months of relevant experience", 1)          # a real floor still reads


def test_a_field_LABELLED_years_is_context_enough():
    """Structured ATS blocks state the requirement as a field, not a sentence.

    Oracle's candidate page renders "Years: 3 to 5+ years" in its requisition field table, and
    with no experience word in reach that read as no requirement at all -- on 2,495 active rows,
    including JPMorgan Chase (582) and Oracle (513). The COLON is what makes this safe: it
    matches a label, not the word "years" inside a sentence.
    """
    _eq("Years: 3 to 5+ years", 3)
    _eq("Years: 5+ years", 5)
    _eq("Years : 2 to 4 years", 2)
    # ...and the sentences that must still read as nothing. "years" without a colon is prose.
    _eq("Our leadership team celebrates 10 years of business this month.", None)
    _eq("401k vesting after 3 years. Software development team.", None)


def test_the_compound_adjective_form():
    """"1-year experience" — a hyphen where the parser wanted a space, and singular.

    Found by scripts/audit_jd_reading.py, not by reading: the range branch already consumed a
    hyphen, but only when a SECOND number followed it, so "3-5 years" read and "3-year" did not.
    846 of the 42,419 cached descriptions use the form.
    """
    _eq("1-year experience in a professional office environment is required.", 1)
    _eq("2-year experience as a medical assistant is required.", 2)
    _eq("3-year experience preferred in a related field.", 3)
    _eq("3-5 years of experience", 3)              # the range form still reads its floor


def test_markdown_escapes_do_not_hide_the_number():
    """The renderer has always stripped these and the parser never did.

    The same posting stored twice — plain text from one host, markdown from another — answered
    5 and None. core.clean_jd unescapes now, so a caller that goes through the one door reads
    what the reader reads. 820 of 42,419 descriptions gain a floor from this.
    """
    md = "* 5\\+ years of Project or Program Management experience in enterprise IT."
    assert core.experience_years(md) is None, "the raw form really is unreadable"
    assert core.experience_years(core.clean_jd(md + " padding. " * 60)[0]) == 5


def test_workdays_degree_picker_states_years_of_SCHOOLING():
    """"Bachelors Degree (+/- 16 years)" is sixteen years of EDUCATION, and employers paste it.

    Found on a real Abbott Project Coordinator whose actual requirement is the "Minimum 2 years"
    two lines below: it read as a SIXTEEN-year job and vanished from an entry-level feed. Only
    18 descriptions in the corpus use the notation, and it is the expensive direction on exactly
    the roles this app is searched for.
    """
    _eq("Qualifications: Bachelors Degree (± 16 years) or an equivalent combination of "
        "education and work experience Masters Degree (± 18 years) - Preferred "
        "Experience/Background Minimum 2 years Related work experience", 2)
    _eq("Bachelor's Degree (16 years) required", None)
    # A parenthesised count that is NOT a degree level is still a requirement.
    _eq("Experience (5 years) in project management is required", 5)


def test_a_company_describing_itself_is_not_a_requirement():
    """The four generic context words are also the vocabulary of an About Us paragraph.

    They have to be admitted -- a bulleted "7+ years in product management" names no other
    context word -- so they carry a guard instead. Measured, the deciding floor rests on one of
    them alone in 1.6% of postings.
    """
    _eq("Acme has been delivering engineering services for 15 years. Requirements: Python.",
        None)
    _eq("Founded 12 years ago, our development team builds tools.", None)
    _eq("Our leadership team celebrates 10 years of business this month.", None)
    # ...and a real requirement phrased with the same word still reads.
    _eq("What You Will Need 8+ years in product management, with time in data strategy", 8)
    _eq("Basic Qualifications: 7+ years of security engineering", 7)


def test_required_and_preferred_in_ONE_clause():
    """A comma is not a clause boundary, so both words are routinely in reach of one number.

    The nearest word wins, which is how the sentence reads aloud.
    """
    assert core.experience_floors(
        "10+ years of experience required, 12 years preferred") == (10, 12)
    assert core.experience_floors(
        "Minimum 5 years of experience, 10 years preferred") == (5, 10)
    # A preferred floor that does not EXCEED the required one is one fact written twice.
    assert core.experience_floors(
        "Minimum qualifications: 5 years of experience. Preferred: 5 years of experience.") \
        == (5, None)


def test_the_title_is_a_floor_of_last_resort():
    """Calibrated at 95.7% over 12,576 postings; see core.title_experience_tier."""
    for t in ("Senior Operations Manager", "Sr. Operations Manager", "Principal Engineer",
              "Director, Supply Chain", "Associate Director PMO", "VP of Engineering",
              "Head of Operations", "Lead Data Analyst", "Staff Accountant"):
        assert core.title_experience_tier(t) == 6, t
    # A FUNCTION IS NOT A LEVEL. "manager" is 87.1% and it is half of what this app is searched
    # for; the roman numerals are 71-81%; bare "associate" is 64.1%. Including any of them would
    # cut an entry-level project-management feed in half to fix a problem those rows do not have.
    for t in ("Operations Manager", "Supply Chain Program Manager 1", "Program Manager II",
              "Project Coordinator", "Business Analyst", "Associate Product Manager",
              "Data Specialist"):
        assert core.title_experience_tier(t) is None, t
    # A junior word VETOES a senior one: the junior rung of a senior ladder.
    for t in ("Junior Architect", "Software Engineering Intern", "New Grad Software Engineer",
              "Early Career Leadership Program", "Director Development Program Trainee"):
        assert core.title_experience_tier(t) is None, t


def test_education_is_read_the_same_way_as_the_years():
    """68.2% of descriptions name a degree, and it was being parsed and thrown away."""
    assert core.education_floors(
        "Bachelor's degree in engineering required. Master's degree preferred.") \
        == ("Bachelor's", "Master's")
    assert core.education_floors("MBA preferred.") == (None, "Master's")
    assert core.education_floors("High school diploma or GED required.") == ("High school", None)
    # An alternation of degrees is a list of ways to qualify, so it collapses like the ladder.
    assert core.education_floors(
        "Doctorate degree OR Master's degree and 2 years OR Bachelor's degree and 4 years")[0] \
        == "Bachelor's"
    assert core.education_floors("No degree mentioned at all.") == (None, None)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  -", fn.__name__)
    print("\nAll %d experience-parsing checks passed." % len(fns))
