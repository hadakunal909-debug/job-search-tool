#!/usr/bin/env python3
"""
test_title_filter.py — freezes the title-filter decisions that were made from measurements.

The INCLUDE/EXCLUDE lists look like the kind of thing you tidy up by intuition, and every entry
below is one that intuition gets WRONG. Each assertion records a number measured against the live
corpus on 2026-08-09, so a future edit has to argue with the data rather than with taste.

No database and no network — just the compiled matchers.
"""
import sys

import scraper

INC = scraper._INCLUDE_RE
EXC = scraper._EXCLUDE_RE


def verdict(title):
    """'drop-exclude' | 'drop-no-include' | 'keep'.

    DELEGATES to scraper.title_verdict rather than rebuilding the rule from INC/EXC, which is
    what this used to do. That copy silently went stale the moment title_verdict grew a third
    branch (the reversed "Manager, Projects" form, 2026-08-20): every assertion here still passed
    while the function under test had changed behaviour. A test that reimplements its subject
    stops testing it.
    """
    keep, why = scraper.title_verdict(title)
    if keep:
        return "keep"
    return "drop-exclude" if why.startswith("off-target") else "drop-no-include"


def test_retail_operations_associate_is_gone():
    # "operations associate" was the sole reason 306 rows were kept, 73% of them Sephora (112)
    # and CubeSmart (111) shop-floor shift work, with ONE of the 306 clearing the 45% floor.
    for t in ("Operations Associate", "Operations Associate - Part Time",
              "Operations Associate - Flex", "Operations Associate - Full Time"):
        assert verdict(t) != "keep", t


def test_the_ops_titles_that_earned_their_place_survive():
    # Measured and KEPT: operations specialist (212 rows, median 27, Mayo Clinic / Kinder Morgan),
    # plus the coordinator/analyst/manager forms that are genuinely on-track.
    for t in ("Operations Specialist", "Operations Coordinator", "Operations Analyst",
              "Operations Manager", "Business Operations Manager"):
        assert verdict(t) == "keep", t


def test_part_time_is_excluded_whichever_way_it_is_spelled():
    # 128 rows carried it (81 Sephora), median score 22, NOT ONE at or above 45. Both spellings
    # are needed because _make_matcher escapes each term literally.
    assert verdict("Project Coordinator - Part Time") == "drop-exclude"
    assert verdict("Project Coordinator - Part-Time") == "drop-exclude"
    assert verdict("Seasonal Operations Manager") == "drop-exclude"


def test_flex_is_NOT_excluded():
    # 38 rows carry a standalone "Flex" but 3 clear the floor, because it is a real programme
    # name: Amazon's "US Flex Business Optimization" (46, 49) and Engie's "Renewables Flex" (46).
    # Excluding it would have thrown those away.
    for t in ("Program Manager, SSD, US Flex Business Optimization",
              "Manager, Project Controls (Renewables Flex Portfolio)"):
        assert verdict(t) == "keep", t


def test_full_time_is_NOT_excluded():
    # 63 rows carry it and Cisco's "Operations Analyst II (Full-Time)" scores 53. "Full time"
    # marks a job we WANT; only part-time is the retail tell.
    assert verdict("Operations Analyst II (Full-Time)") == "keep"


def test_on_target_titles_still_pass():
    for t in ("Director of Project Management", "Senior Engineering Project Manager",
              "Associate Product Manager", "Program Manager", "Business Analyst",
              "Scrum Master", "Implementation Specialist"):
        assert verdict(t) == "keep", t


def test_zero_value_early_career_markers_are_gone():
    # Measured 2026-08-09: five markers admitted 1,015 rows between them and NOT ONE cleared the
    # 45% floor. 'trainee' alone (425 rows) was the source of the retail-management noise -- the
    # "Store Manager Trainee" postings at Safeway / Sephora / Town Pump all entered through it.
    for t in ("Store Manager Trainee", "Management Trainee", "Entry Level Dental Assistant",
              "Entry-Level Technician", "Driver Apprentice", "Graduate Nurse"):
        assert verdict(t) != "keep", t


def test_internship_markers_are_KEPT():
    # These look weak on the same metric (661/188/59 rows, only 4/3/1 clearing) and must NOT be
    # removed for that reason: they feed the feed's "Internships & co-ops only" filter, internships
    # are OPT/STEM-OPT eligible, and an internship scoring low against a senior PM résumé is
    # expected rather than proof it is junk.
    for t in ("Software Engineer Intern", "Product Management Internship",
              "Co-op - Project Management", "Early Career Program Manager"):
        assert verdict(t) == "keep", t


def test_pharma_project_roles_are_kept():
    # Clinical/pharma titles are NOT filtered out: they match on the project/programme words and
    # are real project management at large H-1B sponsors (Abbott, Amgen). Whether the user wants
    # that industry is a feed-filter preference, not a title-filter defect.
    for t in ("Senior Project Manager Clinical Research", "Associate Clinical Project Manager",
              "Clinical Project Coordinator"):
        assert verdict(t) == "keep", t


def test_the_reversed_manager_comma_thing_form_is_kept():
    """Disney posts "Manager, Projects"; every INCLUDE phrase reads "thing role", so the filter
    dropped it and Kunal had to notice by hand. Reported 2026-08-20 from a disneycareers.com
    link -- the board was already scraped (114 Disney rows), so this was a filter blind spot,
    never a coverage gap."""
    for t in ("Manager, Projects", "Manager, Operations", "Coordinator, Projects",
              "Analyst, Operations", "Director, Programs", "Manager, Project Management"):
        assert verdict(t) == "keep", t


def test_the_reversed_form_does_not_smuggle_in_design_or_marketing():
    """The measured false positives. A looser version of this rule -- with bare "product" in the
    thing-list -- admitted 13 rows across 1,493 US titles and SIX were off-target: Product Design,
    Product Marketing, Product Architecture. "product" reads as design or marketing far more often
    than as product management, so only whole role phrases are allowed."""
    for t in ("Manager, Product Design", "Senior Director, Product Marketing, CxO Marketing",
              "Director, Product Architecture & Evangelism", "Manager, Talent Acquisition",
              "Director, Facilities", "Manager, Payroll", "Manager, Compensation"):
        assert verdict(t) != "keep", t


def test_the_reversed_thing_must_sit_next_to_the_comma():
    """Otherwise a stray "operations" three words later re-admits a pricing role: "Senior Analyst,
    Product & Pricing Operations" was in the loose version's output and should not be."""
    assert verdict("Senior Analyst, Product & Pricing Operations") != "keep"


def test_exclude_still_beats_the_reversed_form():
    """The reversed branch runs after EXCLUDE on purpose -- it must never re-admit something the
    exclude list already turned away."""
    for t in ("Manager, Projects - Retail Sales Associate", "Intern, Operations - Cashier"):
        assert verdict(t) == "drop-exclude", t


def test_known_retail_floor_titles_still_blocked():
    for t in ("Sales Associate", "Retail Associate", "Store Associate", "Cashier"):
        assert verdict(t) == "drop-exclude", t


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  -", fn.__name__)
    print("\nAll %d title-filter checks passed." % len(fns))
