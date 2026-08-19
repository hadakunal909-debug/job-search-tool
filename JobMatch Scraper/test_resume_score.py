"""
test_resume_score.py — guards the résumé rubric against the four traps that made it wrong once.

No external test deps: run it directly
    python test_resume_score.py
or via pytest if you have it (functions are named test_*).

Covers, in order of how badly each one broke the score:
  * a role/employer/date line is NOT a bullet (counting them diluted every impact check and
    penalised résumés for their own job titles)
  * a bare-YYYY pattern also matches the year inside "Jan 2024", so the date check has to consume
    specific formats before loose ones or it never once reports "consistent"
  * a date year is not a metric ("Jan 2021 - Mar 2023" must not read as a quantified bullet)
  * the action-verb check fires only on clear non-verbs, because a whitelist cannot contain every
    English present-tense verb and a false offender discredits the whole report
  * a good résumé and a bad one must land far apart — a grader that rates everything 90+ is the
    failure mode this module exists to avoid
"""
import resume_score as rs

GOOD = """Ada Lovelace
ada@example.com | (617) 555-0134 | Boston, MA | linkedin.com/in/adalovelace

Experience
Acme Analytics, Senior Analyst            Jan 2021 - Mar 2023
- Cut month-end close from 9 days to 4 by rebuilding the reconciliation pipeline
- Led a team of 6 analysts through a migration that retired $1.2M of annual licence cost
- Built a forecasting model that reduced stockouts 34% across 12 distribution centres
- Negotiated three vendor contracts, saving 18% against prior-year spend
- Mentored 4 junior analysts, two of whom were promoted within a year

Globex, Analyst                           Jun 2019 - Dec 2020
- Automated 240 hours per quarter of manual reporting using Python and Airflow
- Rebuilt the KPI dashboard used daily by 80 stakeholders across 5 regions

Education
Northeastern University, MS Analytics, 2019

Skills
Python, SQL, Airflow, Power BI, dbt, Snowflake
"""

BAD = """Jane Doe

Objective
To obtain a challenging position where I can grow my career.

Professional Experience
Acme Corp, Analyst          01/2020 - 03/2022
- Responsible for various reporting tasks and dashboards
- Assisted the team with several projects that were successfully delivered
- Helped to manage stakeholder communication and worked closely with vendors
- Various ad hoc analysis

Globex, Intern              Mar 2019
- Supported senior staff
- Duties included filing

Education
State University, BS Business, 2019

Skills
Leadership, teamwork, communication, Excel, hard worker, self-starter

References available upon request
"""


def _check(report, key):
    return next(c for c in report["checks"] if c["key"] == key)


def test_good_and_bad_land_far_apart():
    """The bar here is 75, not 85, and that is the point of the recalibration.

    The first cut put every real résumé between 86 and 92, so "Exceptional" described the average
    document and the number carried almost no information. Thresholds were tightened, the ten checks
    that every résumé passes had their weight cut, and the bands moved up. A well-written résumé now
    lands in the high seventies with real headroom above it; 88+ has to be earned.
    """
    good, bad = rs.score_resume(GOOD)["score"], rs.score_resume(BAD)["score"]
    assert good >= 75, "well-written résumé scored only %d" % good
    assert bad <= 50, "weak résumé scored %d — the rubric is not discriminating" % bad
    assert good - bad >= 30, "only %d points separate a good résumé from a bad one" % (good - bad)


def test_top_band_is_not_where_everyone_lands():
    """A grader whose highest band holds the average document is a compliment, not a measurement."""
    assert rs.band_of(rs.score_resume(GOOD)["score"]) != "Exceptional"
    assert rs.band_of(88) == "Exceptional" and rs.band_of(87) == "Strong"


def test_hygiene_alone_cannot_buy_a_good_score():
    """The structural flaw a weighted average has and a recruiter does not: fifteen cheap checks can
    outvote one Impact failure. This résumé is immaculate — consistent dates, no filler, no pronouns,
    every section present, spelled correctly — and says nothing it achieved."""
    tidy = ("Jane Tidy\njane@example.com | (617) 555-0134 | Boston, MA\n\n"
            "Experience\nAcme Corp, Analyst                        Jan 2021 - Mar 2023\n"
            "- Managed the reporting process for the finance team\n"
            "- Owned the vendor relationship and the monthly close\n"
            "- Coordinated across departments on planning\n"
            "- Produced dashboards for leadership review\n\n"
            "Education\nNortheastern University, MS Analytics, 2019\n\n"
            "Skills\nPython, SQL, Excel\n")
    r = rs.score_resume(tidy)
    assert r["score"] < 70, "a résumé with no quantified impact scored %d" % r["score"]
    # And the report has to SAY the ceiling came from Impact, not leave the user diffing tiles.
    assert r["impact_gated"] or _check(r, "quantified_impact")["score"] < 5, r["score"]


def test_the_impact_gate_only_ever_lowers_a_score():
    """It caps; it must never manufacture points."""
    for txt in (GOOD, BAD):
        r = rs.score_resume(txt)
        assert r["score"] <= r["impact_ceiling"] or not r["impact_gated"]
        assert 0 <= r["score"] <= 100


def test_role_lines_are_not_bullets():
    """"Acme Analytics, Senior Analyst   Jan 2021 - Mar 2023" is not an accomplishment."""
    p = rs.score_resume(GOOD)["parse"]
    assert p["experience_bullets"] == 7, p["experience_bullets"]
    assert p["bullets"] == 7, p["bullets"]          # the two employer lines are excluded


def test_month_year_dates_read_as_one_format():
    """The trap: bare-YYYY also matches the year inside "Jan 2021", so specific shapes must be
    consumed first. Before the fix this check could never return 10."""
    assert rs._date_formats_in("Jan 2021 - Mar 2023") == ["Mon YYYY"]
    assert _check(rs.score_resume(GOOD), "date_consistency")["score"] == 10.0
    # A genuine mix is still caught.
    assert len(rs._date_formats_in("Jan 2021 - 03/2022")) == 2


def test_graduation_year_alone_is_not_an_inconsistency():
    """A degree line carrying "2019" beside month-year employment dates is normal. The check is
    scoped to the experience block so this does not cost points."""
    assert _check(rs.score_resume(GOOD), "date_consistency")["score"] == 10.0


def test_a_date_is_not_a_metric():
    assert rs._is_quantified("Cut close from 9 days to 4") is True
    assert rs._is_quantified("Reviewed 400+ transactions") is True
    assert rs._is_quantified("Managed the reporting process Jan 2021 - Mar 2023") is False
    assert rs._is_quantified("Owned vendor relationships in 01/2020") is False


def test_present_tense_verbs_are_not_flagged_as_nonverbs():
    """No whitelist holds every English verb, so the check fires only on clear non-verbs."""
    for line in ("Review 400+ monthly transactions in Workday",
                 "Led a team of 6 analysts", "Rebuilt the KPI dashboard",
                 "Negotiated three vendor contracts", "Ship weekly releases"):
        assert rs._opens_with_nonverb(line) is False, line
    for line in ("Responsibilities included reporting", "Key member of the analytics team",
                 "In charge of vendor management", "Various ad hoc analysis",
                 "The team I supported grew"):
        assert rs._opens_with_nonverb(line) is True, line


def test_weak_openers_and_filler_are_caught():
    bad = rs.score_resume(BAD)
    assert _check(bad, "weak_verb_openers")["score"] <= 2.0
    assert _check(bad, "filler_buzzwords")["score"] <= 2.0
    assert _check(bad, "quantified_impact")["score"] == 0.0
    assert "Objective section" in _check(bad, "unnecessary_content")["offenders"]
    assert "References line" in _check(bad, "unnecessary_content")["offenders"]


def test_listed_leadership_is_penalised_not_rewarded():
    """A skills section naming "Leadership, teamwork" must not earn leadership points — the rubric
    should never reward the bare claim it exists to discourage."""
    c = _check(rs.score_resume(BAD), "leadership_signals")
    assert "LISTED" in c["detail"]
    assert c["score"] < _check(rs.score_resume(GOOD), "leadership_signals")["score"]


def test_pronouns_match_whole_words_only():
    assert rs._word_hits("I led the team", rs._PRONOUNS) == ["i"]
    # "indices"/"is"/"our" inside other words must not register.
    assert rs._word_hits("Optimized indices; this is important", rs._PRONOUNS) == []


def test_titlecase_headings_are_detected():
    """resume_brain.latex only accepts ALL-CAPS or trailing-colon headings; most real résumés are
    Title Case, and every check depends on knowing which section a line sits in."""
    groups = [s["group"] for s in rs.score_resume(GOOD)["parse"]["sections"]]
    assert "experience" in groups and "education" in groups and "skills" in groups
    _, sections = rs.split_sections("Professional Experience\n- Built a thing\n")
    assert sections and sections[0]["group"] == "experience"


def test_contact_details_read_from_the_header():
    c = rs.score_resume(GOOD)["parse"]["contact"]
    assert c["email"] == "ada@example.com"
    assert c["phone"] == "(617) 555-0134"
    assert c["location"] == "Boston, MA"
    assert "adalovelace" in c["linkedin"]
    assert _check(rs.score_resume(BAD), "contact_details")["score"] == 0.0


def test_garbage_input_never_raises():
    for txt in ("", None, "   ", "\n\n\n", "x", "%s" % ("a" * 5000), "•\n•\n•"):
        r = rs.score_resume(txt)
        assert 0 <= r["score"] <= 100
        assert r["band"]
    assert rs.score_resume("")["score"] == 0
    assert rs.score_resume("short")["parse"]["readable"] is False


def test_score_is_deterministic():
    """A grader whose number drifts cannot answer "did my edit help?"."""
    assert len(set(rs.score_resume(GOOD)["score"] for _ in range(5))) == 1


def test_level_changes_the_standard():
    """A student and an executive are not held to the same bar."""
    thin = ("Experience\nIntern, Acme   Jun 2023 - Aug 2023\n"
            "- Supported the analytics team on reporting\n- Attended weekly planning meetings\n")
    assert rs.score_resume(thin, "entry")["score"] > rs.score_resume(thin, "senior")["score"]
    assert rs.score_resume(GOOD, "bogus")["level"] == "mid"     # unknown level falls back


def test_points_lost_orders_the_fixes():
    """top_fixes must be ranked by points recoverable, which is what makes the report actionable
    rather than a list of complaints."""
    fixes = rs.score_resume(BAD)["top_fixes"]
    assert fixes, "a weak résumé produced no fixes"
    assert fixes == sorted(fixes, key=lambda c: -c["points_lost"])
    assert fixes[0]["key"] == "quantified_impact"      # the heaviest check, and BAD fails it flat
    assert all(c["fix"] for c in fixes)


# ------------------------------------------------- wrapped bullets (the PDF-extraction bug)
# A PDF lays a long bullet across several visual lines and extraction returns one line per visual
# line, with nothing marking the continuations. Only the first carries a bullet glyph, so
# _experience_items kept that one and DROPPED the rest — including the half with the number in it.
# Every PDF-sourced résumé was scored on fragments and told its quantified bullets had no result.
WRAPPED = """KUNAL SINGH HADA
hada.k@example.com | (857) 555-0134 | Boston, MA

EXPERIENCE
Northeastern University, Boston MA            Jan 2025 - Present
-  Analyzed procurement and inventory workflows for Minoans.in, gathering requirements from 15+
   suppliers to design daily-stock SLAs and a custom dashboard, cutting stockouts 32%
-  Built Power Automate workflows to automate monthly asset file updates and reconciliation,
   eliminating hundreds of manual Excel entries and saving 12 hours per week
Minoans.in, Remote                           Jun 2024 - Dec 2024
-  Deployed 30+ RFID-based vehicle tracking units integrating them with a centralized
   dashboard to cut manual logging 45%

SKILLS
PMO Operations, Project planning, project coordination
"""


def test_wrapped_bullets_are_rejoined_not_dropped():
    _h, secs = rs.split_sections(WRAPPED)
    items, _found = rs._experience_items(secs)
    assert len(items) == 3, "got %d bullets, want 3: %r" % (
        len(items), [i["text"][:40] for i in items])
    joined = " | ".join(i["text"] for i in items)
    for tail in ("cutting stockouts 32%", "saving 12 hours per week", "cut manual logging 45%"):
        assert tail in joined, "the continuation carrying %r was lost" % tail


def test_rejoining_recovers_the_metric_the_scorer_needs():
    """The whole point: the number lives on the continuation line."""
    c = _check(rs.score_resume(WRAPPED), "quantified_impact")
    assert c["score"] == 10.0, c["detail"]


def test_role_and_employer_lines_are_not_swallowed():
    """They are also non-bullet lines. Joining them into the bullet above would merge two jobs."""
    _h, secs = rs.split_sections(WRAPPED)
    exp = [s for s in secs if s["group"] == "experience"][0]
    plain = [i["text"] for i in exp["items"] if not i["bullet"]]
    assert len(plain) == 2, plain
    assert all("Jan 2025" in p or "Jun 2024" in p for p in plain), plain


def test_a_rejoined_bullet_keeps_one_span_per_visual_line():
    """One start..end across a wrapped bullet would cover the newline and the indentation between
    its lines, and the viewer would draw that as a block swallowing the gap."""
    _h, secs = rs.split_sections(WRAPPED)
    items, _ = rs._experience_items(secs)
    multi = [i for i in items if len(i["frags"]) > 1]
    assert multi, "no bullet was rejoined"
    for i in items:
        for (a, b) in i["frags"]:
            assert "\n" not in WRAPPED[a:b], WRAPPED[a:b]


def test_pdf_hyphen_artifact_is_repaired():
    """Extraction reads glyph positions, so kerning around a hyphen becomes a real space:
    "Excel-based" comes back as "Excel -based" and stops matching as a compound."""
    import core
    assert core._fix_pdf_artifacts("Excel -based workflows") == "Excel-based workflows"
    assert core._fix_pdf_artifacts("30+ RFID -based units") == "30+ RFID-based units"
    # A genuine spaced dash is left alone.
    assert core._fix_pdf_artifacts("cost - benefit analysis") == "cost - benefit analysis"


# --------------------------------------------------------------- spans (the highlighting contract)
def test_every_span_points_at_the_text_it_claims():
    """A highlight is only as good as its offset. This is the test that would have caught an
    off-by-one from splitlines() dropping the CRLF terminator length."""
    for txt in (GOOD, BAD, GOOD.replace("\n", "\r\n")):
        r = rs.score_resume(txt)
        for c in r["checks"]:
            for (a, b) in c["spans"]:
                assert 0 <= a < b <= len(txt), "%s: span %r out of range" % (c["key"], (a, b))
                frag = txt[a:b]
                # spacing_hygiene is the one check whose offence IS whitespace — a double space or
                # a space before a comma. Every other check pointing at blank characters would mean
                # a broken offset.
                if c["key"] != "spacing_hygiene":
                    assert frag.strip(), "%s: span %r selects only whitespace" % (c["key"], (a, b))
                assert "\n" not in frag, \
                    "%s: span %r crosses a line break: %r" % (c["key"], (a, b), frag)


def test_spans_select_the_offending_word_not_the_whole_line():
    """The weak-verb check objects to the opener. Marking three lines of good prose to point at
    'Assisted' reads as if the entire bullet were wrong."""
    r = rs.score_resume(BAD)
    spans = _check(r, "weak_verb_openers")["spans"]
    words = sorted(BAD[a:b].lower() for a, b in spans)
    assert words == ["assisted", "helped", "responsible", "supported"], words


def test_pronoun_spans_do_not_match_inside_other_words():
    """The reason the lexicons use lookarounds rather than \\b: the pronoun 'i' must not match the
    'i' inside 'Objective'."""
    r = rs.score_resume(BAD)
    hits = [BAD[a:b] for a, b in _check(r, "personal_pronouns")["spans"]]
    assert hits == ["I", "my"], hits


def test_all_occurrences_are_highlighted_not_just_five():
    """MAX_OFFENDERS caps the written fix list so it stays readable. It must NOT cap spans, or the
    count in the rail disagrees with the marks in the document."""
    txt = ("Experience\nAcme, Analyst   Jan 2020 - Jan 2021\n"
           + "".join("- Responsible for thing number %d\n" % n for n in range(9)))
    c = _check(rs.score_resume(txt), "weak_verb_openers")
    assert len(c["offenders"]) <= rs.MAX_OFFENDERS
    assert len(c["spans"]) == 9, len(c["spans"])


# ------------------------------------------------------------------------------------- spelling
def test_spelling_does_not_flag_tools_names_or_employers():
    """The whole reason the vocabulary is mined from our own job postings rather than a generic
    dictionary — and the reason a mere absence is not enough to flag."""
    if not rs.load_vocab():
        print("      (skipped: resume_vocab.json not built)")
        return
    probe = ("Experience\nAcme, Engineer   Jan 2020 - Jan 2021\n"
             "- Rebuilt the Jaggaer and IntelliBuy pipeline on Kubernetes and Terraform\n"
             "- Shipped PyTorch models through a Workday integration\n")
    c = _check(rs.score_resume(probe), "spelling")
    assert c["score"] in (10.0, None), c["detail"]


def test_spelling_catches_real_typos_and_suggests_the_fix():
    if not rs.load_vocab():
        print("      (skipped: resume_vocab.json not built)")
        return
    v = rs.load_vocab()
    for wrong, right in (("recieved", "received"), ("managment", "management"),
                         ("acheived", "achieved"), ("seperate", "separate"),
                         ("anual", "annual")):
        assert rs._nearest(wrong, v) == right, "%s -> %r" % (wrong, rs._nearest(wrong, v))
    # A word that is absent but is NOT a typo of anything must stay quiet.
    assert rs._nearest("astrochakra", v) == ""


# ------------------------------------------------------------------- dormancy and the five groups
def test_a_dormant_check_is_excluded_not_given_free_marks():
    """Awarding 10/10 for a check that never ran hands out points and inflates exactly the résumés
    we know least about."""
    r = rs.score_resume(GOOD)
    for c in r["checks"]:
        if c["dormant"]:
            assert c["score"] is None and c["weight"] == 0.0 and c["points_lost"] == 0.0, c
    # And the arithmetic still closes: no dormant check leaks into the denominator.
    live = [c for c in r["checks"] if not c["dormant"]]
    tw = sum(c["weight"] for c in live)
    assert abs(sum(c["score"] * c["weight"] for c in live) / tw * 10 - r["score"]) < 1.0


def test_the_rubric_scores_five_categories():
    """Skills is the category we had no equivalent for at all; it is what Resume Worded's fifth
    group measures."""
    names = [g["name"] for g in rs.score_resume(GOOD)["groups"]]
    assert names == list(rs.GROUP_ORDER)
    assert "Skills" in names


def test_new_mechanical_checks_fire():
    messy = ("Professional Experience\n"
             "Acme, Analyst   Jan 2020 - Jan 2021\n"
             "- Built  a thing with a double space.\n"
             "- Built a second thing with no full stop\n"
             "SKILLS\n"
             "- Python, Kubernetes\n")
    r = rs.score_resume(messy)
    assert _check(r, "spacing_hygiene")["score"] < 10.0
    assert _check(r, "capitalization_consistency")["score"] < 10.0     # Title Case vs ALL CAPS
    assert _check(r, "punctuation_consistency")["score"] is not None


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  -", fn.__name__)
    print("\nAll %d résumé-score checks passed." % len(fns))
