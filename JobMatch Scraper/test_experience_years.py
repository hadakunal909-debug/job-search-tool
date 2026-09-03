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


def test_a_bare_year_count_is_not_a_requirement():
    """The context gate. Without it a benefits page becomes a seniority bar."""
    _eq("401k vesting after 3 years", None)
    _eq("No experience necessary", None)
    _eq("", None)
    _eq(None, None)
    # >20 is a company statistic, not a person's career.
    _eq("Tapestry Solutions brings over 30 years of industry experience", None)


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


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  -", fn.__name__)
    print("\nAll %d experience-parsing checks passed." % len(fns))
