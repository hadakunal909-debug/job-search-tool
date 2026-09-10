#!/usr/bin/env python3
"""
test_title_filter.py — freezes the title-filter decisions that were made from measurements.

The INCLUDE/EXCLUDE lists look like the kind of thing you tidy up by intuition, and every entry
below is one that intuition gets WRONG. Each assertion records a number measured against the live
corpus on 2026-08-09, so a future edit has to argue with the data rather than with taste.

No database and no network — just the compiled matchers.
"""
import sys

import core
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


def test_the_examples_that_were_already_working_still_work():
    """Kunal asked for "associate project manager, assistant project manager, assistant product
    manager". All of them ALREADY matched -- the matcher is whole-PHRASE, so a seniority prefix
    rides along on "project manager". Pinned so nobody adds 20 redundant prefix variants."""
    for t in ("Associate Project Manager", "Assistant Project Manager",
              "Assistant Product Manager", "Senior Project Manager", "Project Manager II"):
        assert verdict(t) == "keep", t


def test_the_2026_08_20_widening_keeps_what_it_measured():
    """Each of these was the SOLE reason for the row count in the comment beside it in
    scraper.INCLUDE. Measured over 185,753 US postings from a full sweep."""
    for t in ("Chief of Staff", "Sr Tech Project Mgr", "Infrastructure TPM",
              "Prog Mgr, AI Automation", "Manager, Strategic Initiatives", "Programme Manager",
              "ePMO Lead", "Change Manager", "Program Analyst, Legal Ops",
              "Network Deployment Manager I", "Process Improvement Manager",
              "ERP Transformation Manager", "Strategic Delivery Lead",
              "System Development Engineer I", "Program Manger, AUTA Experience"):
        assert verdict(t) == "keep", t


def test_the_2026_08_20_widening_still_refuses_what_it_rejected():
    """THE VALUABLE HALF. Every one of these looked like an obvious addition, was measured, and
    was turned down -- the rows are real, they are just somebody else's job. Re-adding any of
    them needs new numbers, not an opinion.

      project engineer       +349, a construction flood (Actalent 60, M.C. Dean 59)
      continuous improvement  +70, manufacturing-plant lean roles
      portfolio management    +41, INVESTMENT management (Morgan Stanley 11, BlackRock)
      vendor manager          +31, Amazon retail category buying
      apm                      +5, Application Performance Monitoring, not Associate PM
      bsa                      +6, Bank Secrecy Act, not Business Systems Analyst
      specialist           +6,473, the control case for why bare words stay out
    """
    for t in ("Project Engineer", "Paving Project Engineer", "Continuous Improvement Analyst",
              "Sr. Vendor Manager, Canada Fashion", "Quantitative Portfolio Analyst",
              "Senior Manager, Global Portfolio Management", "APM Serverless",
              "Compliance Manager, BSA/AML Program", "Deal Operations Administrator",
              "Biochemistry Resource Manager", "Olympic Power Delivery Specialist"):
        assert verdict(t) == "drop-no-include", t
    # And the bare-word control, plus the "manger" typo that was rejected for being a real word.
    for t in ("Sales Specialist", "PR Specialist", "Sales Manger", "Store Manger"):
        assert verdict(t) in ("drop-no-include", "drop-exclude"), t


def test_release_train_engineer_is_not_eaten_by_the_rail_guard():
    """The Agile role, not a locomotive.

    "train engineer" was a bare EXCLUDE term added 2026-08-01 to catch rail titles arriving via
    the early-career markers. Because EXCLUDE vetoes first and unconditionally, it also killed
    every "Release Train Engineer" -- a SAFe role core.ROLE_FAMILIES already lists under `scrum`,
    so the scraper and the feed disagreed about whether it was wanted. Both halves of the fix are
    pinned here: without the INCLUDE entry, dropping the veto alone still leaves the title
    matching nothing.
    """
    assert verdict("Release Train Engineer") == "keep"
    assert verdict("Senior Release Train Engineer") == "keep"


def test_the_rail_titles_that_guard_was_for_are_still_blocked():
    """Narrowing an exclude is only safe if the thing it was protecting against stays out."""
    for t in ("Passenger Train Engineer", "Freight Train Engineer", "Train Engineer Trainee",
              "PASSENGER ENGINEER TRAINEE", "Locomotive Engineer"):
        assert verdict(t) == "drop-exclude", t
    # Bare "Train Engineer" needs no exclude of its own: it matches no INCLUDE phrase, so it
    # falls out on the keep rule instead. Pinned so nobody "restores" the broad term for it.
    assert verdict("Train Engineer") == "drop-no-include"


# ---------------------------------------------------------------------------------------------
# THE INVARIANT, not an example list. Added 2026-09-08.
#
# A title the scraper KEEPS and no role family CLAIMS is invisible to anyone who ticks a role
# chip: roles_for_title returns () and core.roles_match only ignores that when nothing at all is
# selected. ROLE_FAMILIES' own header has warned about this since the Release-Train-Engineer
# incident, and it was still true of 124 of the 237 INCLUDE phrases -- 465 stored product-titled
# rows among them, every one deleted by ticking the Product Manager chip.
#
# Asserted as a property over the whole list so the next keyword added cannot reintroduce it.
LEVEL_ONLY = frozenset((
    # These describe a RUNG, not a job, and legitimately belong to no family. They are in
    # INCLUDE so that "2027 New Grad Rotational Program" is admitted at all.
    "new grad", "early career", "rotation program", "rotational program",
    "intern", "interns", "internship", "internships",
    "co-op", "co-ops", "coop", "coops", "co op", "summer analyst", "summer associate",
))


def test_every_include_phrase_is_claimed_by_a_family():
    """Every INCLUDE phrase resolves to at least one role family, LEVEL_ONLY excepted."""
    orphans = sorted(p for p in scraper.INCLUDE
                     if p not in LEVEL_ONLY and not core.roles_for_title(p))
    assert not orphans, ("in scraper.INCLUDE but claimed by no core.ROLE_FAMILIES entry, so "
                         "invisible to every role chip:\n  " + "\n  ".join(orphans))


def test_level_only_really_is_level_only():
    """The escape hatch must stay small and must stay honest -- a role phrase parked in
    LEVEL_ONLY would defeat the test above silently."""
    for p in LEVEL_ONLY:
        assert p in scraper.INCLUDE, "%s is not even an INCLUDE keyword" % p
        assert not core.roles_for_title(p), (
            "%s now resolves to %s -- take it out of LEVEL_ONLY" % (p, core.roles_for_title(p)))


def test_the_lululemon_retail_flood_is_refused_at_the_GATE():
    """Measured 2026-09-08: of 145 active rows titled "product operations", 81 were lululemon
    SHOP-FLOOR jobs -- 55 "Product Operations Lead | <mall>" and 26 "Product Operations Educator
    | <mall>" -- and they were 22% of everything the Product Manager chip showed an entry-level
    reader. Same class as test_retail_operations_associate_is_gone above (306 rows, 73% Sephora).

    Refused at the SCRAPE GATE and not in ROLE_FAMILIES, which is where this was first fixed and
    was the wrong layer: narrowing the family hid the rows from one chip while leaving them in
    the corpus, AND left bare "product operations" unclaimed, which is precisely the invisible-row
    defect test_every_include_phrase_is_claimed_by_a_family exists to catch."""
    for t in ("Product Operations Educator | Sawgrass Mills Outlet",
              "Contract Product Operations Lead | Staten Island Mall Pop-Up",
              "Overnight Product Operations Educator | Sawgrass Mills Outlet",
              "Product Operations Educator '26 | Mall at Millenia",
              "Product Operations Lead", "Full-Time Product Operations Educator"):
        assert verdict(t) == "drop-exclude", t


def test_the_real_product_operations_roles_survive_that_exclude():
    """The other 64 rows -- 39 Manager/Director, 20 Analyst/Specialist, 4 bare. An exclude that
    took these too would be a worse bug than the flood it fixed.

    NOT here, and deliberately: Equifax's single genuine "Product Operations Lead". It is the one
    measured cost of the phrase above, 55:1 against the retail rows, and EXCLUDE cannot express
    "except at Equifax"."""
    for t in ("Product Operations Manager, Model Quality", "Product Operations",
              "Product Operations Analyst, World Wide Revenue Operations",
              "Product Operations Specialist | Generalist", "Director, Product Operations",
              "Product Operations Associate", "Senior Product Operations Manager"):
        assert verdict(t) == "keep", t


def test_lululemons_own_corporate_product_roles_are_untouched():
    """The employer is not blocklisted -- only two title shapes are. Its three real product
    postings must still come through, or the fix cost more than the flood."""
    for t in ("Senior Product Manager - Search & Elevation",
              "Senior Product Manager - Omni Channel Fulfillment"):
        assert verdict(t) == "keep", t

def test_the_product_family_claims_what_include_admits():
    """The 2026-09-08 additions, named individually because each was a measured loss."""
    for t in ("Product Analyst", "Senior Product Analyst", "Product Coordinator",
              "Product Operations", "Product Operations Analyst", "Product Strategy Analyst",
              "Product Strategist", "Technology Product Analyst", "Product Lead",
              "Associate Product Owner", "Product Mgr", "Assoc Product Mgr"):
        assert "product" in core.roles_for_title(t), (t, core.roles_for_title(t))


def test_a_family_addition_can_only_widen():
    """Every phrase added on 2026-09-08 goes INTO a family, so a title can only gain families.
    The titles the feed already showed must keep every family they had."""
    frozen = {
        "Project Manager": "pm", "Program Manager": "program", "Product Manager": "product",
        "Scrum Master": "scrum", "Business Analyst": "ba", "Operations Manager": "ops",
        "Software Engineer": "swe", "Data Analyst": "dataanalyst", "Data Engineer": "dataeng",
        "DevOps Engineer": "devops", "QA Engineer": "qa", "Financial Analyst": "finance",
    }
    for title, key in frozen.items():
        assert key in core.roles_for_title(title), (title, key, core.roles_for_title(title))


# ---------------------------------------------------------------------------------------------
# ABBREVIATED TITLES (core.normalize_title, added 2026-09-10)
#
# Reported by Kunal from a live Applied Materials posting the app had never held. Numbers below
# are from a dump_titles sweep of 47,348 real postings (34,895 US) taken the same day.


def test_the_applied_materials_family_that_started_this():
    # Four "Tech Proj/Prg Mgmt" were live on their Workday board, plus the Non-Tech and the
    # "Prog Manager IV" spellings. All three failed INCLUDE *and* pm_title_gate, and a title
    # that fails both is never fetched and so can never be rescued on its description.
    for t in ("Tech Proj/Prg Mgmt", "Non-Tech Proj/Prg Mgmt", "Tech Proj/Prog Manager IV"):
        assert verdict(t) == "keep", t
        assert core.pm_title_gate(t), t


def test_the_shorthand_class_generally():
    for t in ("Program Mgmt", "Project Mgmt", "Prog Mgmt Analyst", "Tech Prgm Mgr",
              "Sr Bus Sys Analyst", "Prod Mgmt Spec", "Proj Coord II", "Project Admin",
              "Business Process Anlst 4", "Supply Chain Mgr III"):
        assert verdict(t) == "keep", t


def test_expansion_does_not_open_the_gate_on_everything():
    # The measured cost of the change, and the reason tech->technical is NOT in _TITLE_ABBR:
    # it admitted nothing across 34,895 US postings while opening pm_title_gate for 294 rows
    # that were overwhelmingly "Tech" as a NOUN. Each of those is a description fetch.
    for t in ("Mechatronics & Robotics Tech", "QC Tech", "DCO Tech", "PCT (Patient Care Tech)",
              "Data Center Controls Tech"):
        assert verdict(t) != "keep", t
        assert not core.pm_title_gate(t), t


def test_normalisation_never_re_admits_what_EXCLUDE_turned_away():
    # EXCLUDE reads the shadow title too, so expansion cannot smuggle a title past a veto.
    for t in ("Mechanical Engineer", "Registered Nurse", "Sr Mechanical Eng",
              "Proj Mgr - Chemical Plant", "Project Coordinator - Part Time"):
        assert verdict(t) == "drop-exclude", (t, scraper.title_verdict(t))


def test_the_off_target_titles_stay_off_target():
    # From the same sweep: everything normalisation was ASKED to leave alone, it left alone.
    for t in ("Senior Procurement Manager", "Supplier Account Technologist (E5)",
              "Facilities Manager (Semiconductor Lab)", "Senior NPI Supply Chain Expert - B5",
              "Global Category Manager"):
        assert verdict(t) != "keep", t


def test_dev_ops_survives_being_pulled_apart():
    # "dev ops" is ITSELF an INCLUDE phrase, so dev->developer and ops->operations between them
    # turned "Dev Ops Engineer" into "developer operations Engineer" and matched nothing. Those
    # four rows were the only regression across 25,973 distinct live titles; _TITLE_COMPOUND
    # joins the pair before anything is expanded.
    for t in ("Dev Ops Engineer", "HPC Dev Ops Engineer", "Senior Dev Ops Engineer",
              "Dev Sec Ops Engineer"):
        assert verdict(t) == "keep", t


def test_a_tab_inside_a_title_no_longer_hides_a_keyword():
    # scraper._dump_field's docstring has said "job titles really do contain tabs and newlines"
    # all along; nothing collapsed them before matching. Real Amazon posting.
    assert verdict("Manufacturing System\tDevelopment Engineer, Cloud AI/ML/storage") == "keep"
    assert verdict("Senior  Project\nManager") == "keep"


def test_the_comma_survives_normalisation():
    # _REVERSED_RE is anchored on the comma, so it must NOT become a separator -- and leaving
    # it alone is also what lets the reversed form pick up the expansion.
    assert verdict("Manager, Projects") == "keep"
    assert verdict("Dir, Programs") == "keep"


def test_the_hyphen_is_not_a_separator():
    # co-op / full-stack / part-time / roll-out are each a single vocabulary entry.
    assert verdict("Co-op Software Engineer") == "keep"
    assert verdict("Full-Stack Developer") == "keep"
    assert verdict("Project Coordinator - Part-Time") == "drop-exclude"


def test_the_shadow_title_is_never_the_stored_one():
    # The card still shows what the employer called the job.
    assert core.normalize_title("Tech Proj/Prg Mgmt") != "Tech Proj/Prg Mgmt"
    assert core.normalize_title("Senior Project Manager") == "Senior Project Manager"


def test_an_admitted_abbreviation_lands_in_a_ROLE_FAMILY():
    # Admitting a posting and then answering () from roles_for_title would bury it twice: ()
    # is deleted by any role selection and sorts last under ROLE_PRIORITY. It must behave
    # exactly like the spelled-out title it abbreviates.
    assert core.roles_for_title("Tech Proj/Prg Mgmt") == \
        core.roles_for_title("Technical Project/Program Manager")
    assert core.roles_for_title("Tech Proj/Prg Mgmt"), "abbreviated title has no role family"
    assert core.role_rank(core.roles_for_title("Sr Proj Mgr")) == \
        core.role_rank(("pm",))


# ---------------------------------------------------------------------------------------------
# ROLE PRIORITY (core.ROLE_PRIORITY, added 2026-09-10)


def test_role_priority_is_the_owners_order():
    # Given 2026-09-10: project, then product, then program, then the rest.
    order = [core.ROLE_PRIORITY.index(k) for k in ("pm", "product", "program")]
    assert order == sorted(order), core.ROLE_PRIORITY[:4]
    assert core.role_rank(("pm",)) < core.role_rank(("product",)) < core.role_rank(("program",))


def test_no_role_sorts_last_and_the_best_role_wins():
    assert core.role_rank(()) == core.ROLE_RANK_NONE
    assert core.role_rank(None) == core.ROLE_RANK_NONE
    assert core.role_rank(("swe", "pm")) == core.role_rank(("pm",))
    assert core.role_rank(("nonsense",)) == core.ROLE_RANK_NONE


def test_every_family_has_a_rank():
    # core.py asserts this at import; repeated here so the failure names the file to edit.
    missing = [k for k in core.ROLE_KEYS if k not in core.ROLE_PRIORITY]
    assert not missing, "add these to core.ROLE_PRIORITY (and app.js's twin): %s" % missing


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  -", fn.__name__)
    print("\nAll %d title-filter checks passed." % len(fns))
