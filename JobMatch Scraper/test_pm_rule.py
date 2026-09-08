#!/usr/bin/env python3
"""
test_pm_rule.py — freezes core.reads_like_pm, the rule that lets a posting into the corpus on
the strength of its DESCRIPTION when its title matched nothing.

The thresholds were set by measurement (scripts/calibrate_pm_rule.py, 300 postings a bucket on
2026-08-20), and the properties below are the ones that make the measurement mean anything. A
future edit that raises recall by weakening the anchor gate will pass the calibration sweep and
fail here, which is the point.

No database and no network — the supply checks at the bottom read scraper/__init__.py as TEXT
rather than calling a board, for exactly that reason.
"""
import os

import core
import scraper

# A real posting reads like this: it names the work, not just the vocabulary.
PM_JD = """
Responsibilities: own the project plan end to end, track milestones and deliverables, manage
stakeholder expectations across the business, chair the weekly steering committee, maintain the
risk register and escalate blockers. You will coordinate cross-functional teams, report status
to leadership, and keep delivery on time and within budget. Jira and Confluence.
"""

# The trap this rule exists to survive. Every agile word is here and NONE of the work is
# delivery ownership.
SWE_JD = """
You will design and build backend services in Go, write unit tests, review pull requests,
participate in sprint planning and daily scrum, groom the backlog, and work with
cross-functional partners against the roadmap. Kubernetes, Docker, CI/CD, observability.
Requirements: 5 years of software engineering, strong knowledge of distributed systems.
"""

CLINICAL_JD = """
Provide direct patient care, administer medications, document in the EMR, and collaborate with
the care team on the unit. BSN required. Reporting to the charge nurse. Rotating shifts.
"""


def test_the_shipped_thresholds_are_the_measured_ones():
    """If these change, scripts/calibrate_pm_rule.py must have been re-run and its table in
    core.py updated. They are asserted so the numbers in that comment cannot go stale silently.

    Raised from 2/6 to 3/8 on 2026-08-20 when the JD supply widened past the four boards the
    original number was measured on. See the second table in core.py: at 2/6 the rule claimed
    8.5% of every Greenhouse posting.
    """
    assert core.PM_MIN_ANCHORS == 3
    assert core.PM_MIN_POINTS == 8
    assert core.PM_ANCHOR_WEIGHT == 2


def test_a_delivery_posting_fires():
    assert core.reads_like_pm(PM_JD)


def test_a_software_posting_does_not():
    """Measured tech-fire rate at the shipped gate is 12.8%, so this is not a claim that no
    engineering JD ever fires -- it is a claim that the *agile vocabulary alone* is not enough,
    which is the specific failure the two-tier split was built for."""
    assert not core.reads_like_pm(SWE_JD)


def test_clinical_work_is_nowhere_near():
    assert not core.reads_like_pm(CLINICAL_JD)


def test_support_words_alone_can_never_carry_a_posting():
    """The anchor gate is the load-bearing half. Twenty support words and no anchor is still a
    no, however high the points total climbs."""
    text = " ".join(core.PM_SUPPORT)
    anchors, support, _veto = core.pm_signal(text)
    assert anchors == 0, "a support-only text must score zero anchors"
    assert support >= 20, "sanity: the support list should light up on itself"
    assert core.pm_points(anchors, support) >= core.PM_MIN_POINTS, (
        "this test is only meaningful while the points gate is passed")
    assert not core.reads_like_pm(text)


def test_repetition_cannot_inflate_a_score():
    """DISTINCT phrases, not occurrences. One word said fifty times is one signal — otherwise a
    JD that happens to repeat "stakeholder" outranks one that does the job."""
    once = "stakeholder management and the project plan"
    many = once + (" stakeholder" * 50)
    assert core.pm_signal(once) == core.pm_signal(many)


def test_the_two_lists_do_not_overlap():
    """A phrase in both tiers would be counted twice and weighted 3x, silently. Anchors are
    matched whole-phrase, so an anchor containing a support word ("project plan" vs "scope") is
    fine; identical entries are not."""
    dupes = set(core.PM_ANCHORS) & set(core.PM_SUPPORT)
    assert not dupes, "in both PM_ANCHORS and PM_SUPPORT: %s" % sorted(dupes)


def test_thresholds_are_reachable():
    """A points gate below the anchor floor is dead configuration: PM_MIN_ANCHORS anchors
    already score PM_ANCHOR_WEIGHT * PM_MIN_ANCHORS, so a lower gate never rejects anything."""
    assert core.PM_MIN_POINTS >= core.PM_ANCHOR_WEIGHT * core.PM_MIN_ANCHORS


# ---------------------------------------------------------------------------------------------
# THE TITLE GATE, added 2026-08-20 with the wider JD supply. It does more work than the
# thresholds do: on the Greenhouse sample it took the admitted set from 159 rows to 30.
def test_the_titles_this_feature_exists_for_pass_the_gate():
    """A delivery role wearing a useless title is the entire point. If these stop passing, the
    description path has nothing left to rescue."""
    for t in ("Coordinator II", "Business Operations Specialist", "Operations Analyst",
              "Technical Program Analyst", "Change Enablement Lead", "Product Operations",
              "Strategic Initiatives Associate", "Release Coordinator"):
        assert core.pm_title_gate(t), t


def test_the_greenhouse_flood_titles_are_refused_at_the_gate():
    """Every one of these was admitted by the un-gated 2/6 rule on the 30-board Greenhouse
    sample. Their JDs really do talk about milestones and stakeholders -- that is why the gate
    has to be on the TITLE, where the JD cannot argue back."""
    for t in ("EHS Manager", "Travelling EHS Manager", "Surveyor", "Senior Estimator",
              "HRIS Manager", "DEI Partner", "Creative Marketing Manager",
              "Director, Sales Enablement", "Senior Quality Control Manager"):
        assert not core.pm_title_gate(t), t


def test_project_engineer_stays_refused():
    """Measured and rejected TWICE as an INCLUDE keyword (+349 rows, 43% from four construction
    contractors). The description path must not re-admit what the title path threw out, or the
    two halves of the filter disagree -- the "Release Train Engineer" bug in reverse."""
    for t in ("Project Engineer", "Project Engineer II", "Senior Project Engineer",
              "Project Engineering Manager"):
        assert not core.pm_title_gate(t), t


def test_the_gate_is_a_gate_and_not_an_admission():
    """Passing the title gate must not be enough on its own -- a clinical JD under a title that
    happens to say "operations" still has to fail on the text."""
    assert core.pm_title_gate("Operations Manager")
    assert not core.admits_on_description("Operations Manager", CLINICAL_JD)


def test_admits_on_description_enforces_the_length_floor():
    """A truncated teaser must never be read as a complete description. Phenom serves a 372-char
    one, which is exactly the trap this floor was written for -- and note that PM_JD itself is
    366 chars, i.e. shorter than the floor, so the padding below is the point of the test rather
    than an accident of the fixture."""
    long_jd = PM_JD + " " + PM_JD
    assert len(long_jd) >= core._MIN_JD_CHARS > len(PM_JD)
    assert core.admits_on_description("Program Operations Lead", long_jd)
    assert not core.admits_on_description("Program Operations Lead", PM_JD)


# ---------------------------------------------------------------------------------------------
# THE JD SUPPLY. The rule above is worthless on a board that never hands a description over, so
# these freeze the wiring rather than the vocabulary. Source-text checks, not live calls: CI has
# no business depending on whether Greenhouse is up.
def test_every_board_that_can_inline_a_description_does():
    """Five ATS list feeds return the description in the response the sweep already reads. Each
    one that silently stopped doing so would cost the description rule a slice of the corpus
    with no error anywhere.

    Greenhouse used to be in this list and is now checked by BEHAVIOUR below instead. It moved
    its row building into a helper (_gh_rows) when the two-phase fetch landed, and a check that
    reads one function's source text cannot follow a call — it reported the description lost
    when it was one line away and working. That is the failure mode CLAUDE.md warns about, so
    the biggest source got the assertion that cannot lie about it.
    """
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "scraper", "__init__.py"), encoding="utf-8").read()
    for fn in ("scrape_lever", "scrape_ashby", "scrape_jibe",
               "scrape_recruitee", "scrape_pinpoint"):
        body = src.split("def %s(" % fn, 1)[1].split("\ndef ", 1)[0]
        assert '"jd"' in body, "%s no longer keeps the description it is handed" % fn


def test_greenhouse_keeps_the_description_it_is_handed():
    """The same property as above for the largest source, asserted on the row it actually builds.

    No network: _gh_rows is the pure half of scrape_greenhouse, so one fixture response proves
    the description survives the parse — including the HTML unescaping, which is the part most
    likely to break silently (`content` is HTML-escaped HTML, so a bare parser mangles it)."""
    payload = {"jobs": [{
        "title": "Program Operations Lead",
        "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/1",
        "location": {"name": "Boston, MA"},
        "first_published": "2026-08-20T00:00:00Z",
        # Escaped, exactly as the API sends it.
        "content": "&lt;p&gt;Own the &lt;b&gt;roadmap&lt;/b&gt; and run discovery.&lt;/p&gt;",
    }]}
    row = scraper._gh_rows(payload, True)[0]
    assert row["jd"], "scrape_greenhouse no longer keeps the description it is handed"
    assert "roadmap" in row["jd"], "the description survived but its HTML was not unescaped"
    assert "&lt;" not in row["jd"] and "<p>" not in row["jd"], "escaped markup leaked into the jd"
    assert row["title"] == "Program Operations Lead" and row["location"] == "Boston, MA"
    # And the cheap phase must NOT invent one: an absent jd is what tells main() there is
    # nothing to bank, and an empty string would be banked as a real (useless) description.
    assert "jd" not in scraper._gh_rows(payload, False)[0]


def test_greenhouse_asks_for_the_content_it_needs():
    """Greenhouse only returns descriptions when asked, and it is 409 of the 1,173 boards in
    SOURCES -- by far the largest single source. Losing the parameter would be invisible."""
    assert scraper.GREENHOUSE_JD is True, "the default must be on"
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "scraper", "__init__.py"), encoding="utf-8").read()
    body = src.split("def scrape_greenhouse(", 1)[1].split("\ndef ", 1)[0]
    assert "content=true" in body


def test_the_jd_lookup_budget_is_bounded():
    """The pass that BUYS descriptions for boards that do not inline them must stay capped. An
    unbounded version is not a scrape, it is a crawl: a full sweep leaves ~213,000 postings on
    the floor and a request each would never finish inside the run budget."""
    assert 0 < scraper.JD_LOOKUP_PER_BOARD <= scraper.JD_LOOKUP_BUDGET
    assert scraper.JD_LOOKUP_BUDGET <= 5000, "a budget this large is not a budget"
    assert scraper.JD_LOOKUP_WORKERS >= 1
    # And a clock, because every assertion above counts REQUESTS. 1,200 of them measured 4.5 min
    # on 2026-08-21, which is not a number any of the ceilings above can see.
    assert scraper.JD_LOOKUP_BUDGET_MIN > 0, "an unbounded pass is what overran the scrape step"
    assert scraper.JD_LOOKUP_BUDGET_MIN <= 6, "longer than the headroom the CI step has to give"


def test_the_jd_lookup_pass_stops_when_its_clock_runs_out():
    """The ceilings above cap requests; this caps time, and the two came apart in production.

    2026-08-21: 1,200 fetches at 8 workers took 4.5 min and put the scrape step at 26.2 min
    against a timeout-minutes of 26. The runner killed it 0.14 s after its last line, and because
    GitHub skips every later step once one fails, that also cost the score pass and the digest.

    Driven with a slow stub and a tiny clock rather than by reading the source, because the thing
    worth freezing is that the pass ACTUALLY returns early -- the natural spelling of this loop
    (ex.map inside a `with`) submits every future up front and drains them all on the way out, so
    a version that looks bounded and is not would pass any substring check.
    """
    import time
    from scraper import score_jobs

    # The three fixtures above that the lookup would really pick: past core.pm_title_gate, and
    # rejected by title_verdict for want of a keyword rather than on an EXCLUDE hit.
    titles = ("Coordinator II", "Change Enablement Lead", "Release Coordinator")
    scraped = [{"url": "https://acme%d.wd1.myworkdayjobs.com/en-US/careers/job/%d" % (i % 7, i),
                "title": titles[i % len(titles)], "company": "Acme %d" % (i % 7),
                "location": "Boston, MA", "jd": ""} for i in range(64)]

    real_detail, real_budget = score_jobs.detail_jd, scraper.JD_LOOKUP_BUDGET_MIN

    def slow(url):
        time.sleep(0.25)
        return url, "x" * (core._MIN_JD_CHARS + 10), None

    score_jobs.detail_jd = slow
    scraper.JD_LOOKUP_BUDGET_MIN = 0.02                       # 1.2 s
    try:
        t0 = time.monotonic()
        tried, got = scraper.fill_missing_jds(scraped, set(), set())
        spent = time.monotonic() - t0
    finally:
        score_jobs.detail_jd = real_detail
        scraper.JD_LOOKUP_BUDGET_MIN = real_budget

    # 64 fetches of 0.25 s over JD_LOOKUP_WORKERS threads need ~2 s and the clock allows 1.2, so
    # it has to cut in. The overshoot is bounded by the fetches already in flight, never the pass.
    assert 0 < tried < len(scraped), "expected truncation, got tried=%d of %d" % (tried, len(scraped))
    assert spent < 1.2 + 3, "ran past the budget by more than the in-flight fetches: %.1fs" % spent
    assert got == tried, "every stubbed fetch returns a usable description"
    # What it never reached must be left alone, so the keep loop drops those on their titles --
    # exactly what would have happened if the board had published no description at all.
    assert sum(1 for j in scraped if j.get("jd")) == got


# ---------------------------------------------------------------------------------------------
# THE SOFT VETO, added 2026-09-08. The rule could not admit a PRODUCT posting at all.
#
# Measured before the change: the APM_JD below scored 3 anchors and 11 points -- clearing both
# gates -- and reads_like_pm returned False on four vetoes (figma, go-to-market, user research,
# wireframes). The veto is tested first and short-circuits, so no amount of product vocabulary
# could argue back. A delivery role wearing a useless title got a second chance; a product role
# never did.

# A real Associate PM posting. Every phrase here is ordinary in that job.
APM_JD = (  # One string per sentence, not a triple-quoted block: a wrapped line can split a
  # phrase across a newline and the anchor regexes match a literal space. Real stored
  # descriptions have a median of ZERO newlines, so this is a fixture hazard, not a
  # corpus one -- measured on 300 of them, none splits "product manager".
    "Own the product roadmap end to end and report to a Senior Product Manager. "
    "Run user research and customer discovery, write PRDs and product requirements documents. "
    "Partner with design on wireframes in Figma, define OKRs, and groom the product backlog. "
    "Prioritize features, run A/B tests in Amplitude, and work with go-to-market partners on launch. "
    "You will coordinate cross-functional teams, track milestones and deliverables, and manage stakeholder expectations across the business. "
    "0-2 years of experience. Bachelor's degree required. This is an entry-level role and we welcome recent graduates.")

# The flood the design block was written for -- and note it is a DIFFERENT posting from
# "Product Designer", which never reaches this rule at all: scraper.EXCLUDE drops that on
# "designer" and admits_on_description's caller owns the no-EXCLUDE-hit precondition. What
# reaches here is the title dropped for want of a keyword, so that is what is fixtured.
DESIGN_MGR_JD = (  # One string per sentence, not a triple-quoted block: a wrapped line can split a
  # phrase across a newline and the anchor regexes match a literal space. Real stored
  # descriptions have a median of ZERO newlines, so this is a fixture hazard, not a
  # corpus one -- measured on 300 of them, none splits "product manager".
    "You will own the visual design and evolve our design system. "
    "Lead ux design and interaction design for core flows, run design reviews, and build wireframes in Figma. "
    "Partner with product managers on the product roadmap and product vision. "
    "Conduct user research and usability testing. "
    "You will coordinate cross-functional stakeholders, track milestones and deliverables against the roadmap, and manage the design backlog. "
    "8 years of design experience required.")

PRODUCT_MKTG_JD = (  # One string per sentence, not a triple-quoted block: a wrapped line can split a
  # phrase across a newline and the anchor regexes match a literal space. Real stored
  # descriptions have a median of ZERO newlines, so this is a fixture hazard, not a
  # corpus one -- measured on 300 of them, none splits "product manager".
    "Drive product marketing for our platform and own messaging and positioning. "
    "Build go-to-market plans with the product managers and support the sales pipeline. "
    "Run demand generation and content marketing campaigns. "
    "Partner on the product roadmap and product launch. "
    "You will coordinate cross-functional stakeholders, own the project plan, track milestones and deliverables, and report status to leadership on time and within budget.")


def test_a_product_posting_is_refused_on_the_design_words_and_that_is_the_trade():
    """THE OWNER'S CALL, 2026-09-08: "figma, wireframes, user research these were good, add
    them back." This fixture is kept precisely BECAUSE it now fails to be admitted -- it is
    the clearest statement of what the hard veto costs, and the next person to consider
    loosening it should meet the trade before the vocabulary.

    The posting is a genuine Associate PM role. It clears both thresholds on its own
    vocabulary -- three anchors, eleven points -- and is refused purely on the veto, which
    reads_like_pm tests FIRST and short-circuits. So no amount of product-ownership language
    can talk the rule into it.

    WHY THAT IS ACCEPTABLE, which is the half worth writing down: this path only ever sees
    titles that matched NO include keyword. Every ordinary product title now matches one --
    "product manager", "associate product manager", "product analyst", "product coordinator",
    "product operations manager", "product mgr" -- so the roles the reader is actually
    searching for are admitted on their titles and never reach this rule. What is given up
    is the product role wearing a useless title ("Coordinator II"), in exchange for keeping
    out the design flood, whose postings are made of these same three words.
    """
    # It is the veto doing this, not a shortage of product vocabulary.
    a, s, v = core.pm_signal(APM_JD)
    assert a >= core.PM_MIN_ANCHORS, a
    assert core.pm_points(a, s) >= core.PM_MIN_POINTS, (a, s)
    assert v >= core.PM_MAX_VETO, v
    assert not core.reads_like_pm(APM_JD)
    assert not core.admits_on_description("Coordinator II", APM_JD)


def test_the_three_restored_words_are_hard_and_still_scored():
    """ADMISSION AND SCORING ARE DIFFERENT QUESTIONS, and this is the pair that proves it.

    PM_VETO decides whether a posting whose title matched nothing may enter the corpus.
    ATS_KEYWORDS decides what a description is SCORED on. Moving these three back to the hard
    veto must not have cost them their hard-skill weight in analyze_jd, or the reader's match
    percentage would have quietly dropped on every real product posting."""
    for w in ("figma", "wireframes", "user research"):
        assert w in core.PM_VETO, "%s must be a hard veto" % w
        assert w not in core.PM_VETO_SOFT, "%s must not be in both tiers" % w
        assert w in core.ATS_KEYWORDS, "%s must still be a scored hard skill" % w
    assert core.PM_VETO_SOFT == ("design reviews", "go-to-market"), core.PM_VETO_SOFT


def test_the_soft_veto_needs_product_anchors_to_be_overturned():
    """It is an override, not a deletion. Strip the product-ownership phrases and the same soft
    words bite again -- otherwise this is just a shorter PM_VETO."""
    assert core.pm_product_anchors(APM_JD) >= core.PM_PRODUCT_MIN
    # ONLY THE WORDS STILL IN THE SOFT TIER. This fixture used to name user research,
    # wireframes and Figma as well, and once those went hard it kept passing -- on three HARD
    # vetoes, not on the soft rule it claims to test. A test that passes for the wrong reason
    # is worse than one that fails.
    thin = ("We run design reviews with go-to-market partners. "
            "You will own the project plan, chair the steering committee, maintain the risk "
            "register, track milestones and deliverables, and manage stakeholder expectations "
            "across the business on time and within budget. Jira and Confluence.")
    assert core.pm_product_anchors(thin) < core.PM_PRODUCT_MIN
    _a, _s, v = core.pm_signal(thin)
    assert v >= core.PM_MAX_VETO, "with no product anchors the soft words must still count"


def test_the_words_a_designer_owns_stay_hard():
    """visual design / design system / ux design / interaction design are NOT in the soft tier,
    which is what keeps the design flood out even though the posting names two product anchors."""
    for p in ("visual design", "design system", "ux design", "interaction design",
              "product marketing"):
        assert p in core.PM_VETO, "%s must stay a hard veto" % p
        assert p not in core.PM_VETO_SOFT
    assert core.pm_product_anchors(DESIGN_MGR_JD) >= core.PM_PRODUCT_MIN, (
        "the fixture has to reach the override for this test to mean anything")
    assert not core.reads_like_pm(DESIGN_MGR_JD)
    assert not core.admits_on_description("Product Design Manager", DESIGN_MGR_JD)
    assert not core.admits_on_description("Director, Product Design", DESIGN_MGR_JD)


def test_product_marketing_stays_refused():
    """A PM posting mentions the function in passing; a product-marketing posting is made of it."""
    assert not core.reads_like_pm(PRODUCT_MKTG_JD)
    assert not core.admits_on_description("Product Marketing Manager", PRODUCT_MKTG_JD)
    assert not core.admits_on_description("Senior Product Marketing Manager", PRODUCT_MKTG_JD)


def test_the_soft_and_hard_veto_lists_do_not_overlap():
    """Same property as PM_ANCHORS/PM_SUPPORT: a phrase in both would be counted twice."""
    dupes = set(core.PM_VETO) & set(core.PM_VETO_SOFT)
    assert not dupes, "in both PM_VETO and PM_VETO_SOFT: %s" % sorted(dupes)


def test_every_product_anchor_is_a_real_anchor():
    """PM_PRODUCT_ANCHORS names a SUBSET of PM_ANCHORS. An entry that is not also an anchor
    would let a posting earn the override without ever scoring on it."""
    missing = sorted(set(core.PM_PRODUCT_ANCHORS) - set(core.PM_ANCHORS))
    assert not missing, "in PM_PRODUCT_ANCHORS but not PM_ANCHORS: %s" % missing


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  -", fn.__name__)
    print("\nAll %d PM-rule checks passed." % len(fns))
