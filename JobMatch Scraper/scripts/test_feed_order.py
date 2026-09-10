#!/usr/bin/env python3
"""
test_feed_order.py -- what decides the ORDER of the feed, and what breaks up an employer wall.

The feed sorted on one integer. `score` is int(pct), so ~39,000 active rows fell into at most
101 buckets, Python's sort is stable, and the surviving order inside a bucket was whatever the
corpus read gave us -- db._fetch_all defaults to `order=url`, one employer is one ATS host, so
URL order IS employer order. That, and no grouping rule, is why the feed showed walls of one
company; "Newest" did the same thing because a board is scraped in one pass and dozens of rows
share a date.

Two things fixed it and both are frozen here: core.ROLE_PRIORITY as a tie-break inside the
chosen sort, and web._break_employer_runs as a last pass over the result.

The server/client halves are compared row for row by scripts/feed_parity.py. This suite is
about the RULES rather than the mirror: parity would stay green if both sides agreed on
something wrong.

No database and no network.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["EV_OFF"] = "1"
os.environ.setdefault("APP_SECRET", "test-only-not-a-real-key")

import core                                              # noqa: E402
import web                                               # noqa: E402


def row(score=0, date="2026-09-01", roles=(), company="Acme", url=None, **kw):
    r = {"score": score, "date": date, "first_seen": date, "roles": list(roles),
         "company": company, "url": url or "https://x/%s-%s-%s" % (company, score, date)}
    r.update(kw)
    return r


def order(rows, sort="score"):
    return [r["company"] for r in sorted(rows, key=lambda r: web._sort_key(r, sort))]


# ---------------------------------------------------------------------------------------------
# the sort key


def test_score_still_decides_first():
    """Role rank ranks WITHIN the chosen sort and never over it. A 90% match outranks an 80%
    one whatever their roles, or the tie-break would have quietly become the ranking."""
    rows = [row(score=80, roles=("pm",), company="lower-score-better-role"),
            row(score=90, roles=("security",), company="higher-score-worse-role")]
    assert order(rows)[0] == "higher-score-worse-role"


def test_newest_still_means_newest():
    rows = [row(date="2026-09-01", roles=("pm",), company="older-better-role"),
            row(date="2026-09-08", roles=("security",), company="newer-worse-role")]
    assert order(rows, "newest")[0] == "newer-worse-role"


def test_role_priority_breaks_a_score_tie():
    """The whole point: ~400 rows share a score bucket, and this is what orders them."""
    rows = [row(score=70, roles=("swe",), company="swe"),
            row(score=70, roles=("program",), company="program"),
            row(score=70, roles=("pm",), company="pm"),
            row(score=70, roles=("product",), company="product"),
            row(score=70, roles=(), company="none")]
    assert order(rows) == ["pm", "product", "program", "swe", "none"]


def test_role_priority_breaks_a_DATE_tie():
    """A board is scraped in one pass, so this is the tie that actually happens most."""
    rows = [row(date="2026-09-08", roles=("swe",), company="swe"),
            row(date="2026-09-08", roles=("pm",), company="pm"),
            row(date="2026-09-08", roles=("product",), company="product")]
    assert order(rows, "newest") == ["pm", "product", "swe"]


def test_project_management_outranks_product_management():
    """Asked for by name, 2026-09-10. Frozen because it is a preference, not a derivation --
    nothing in the code would notice if the two swapped."""
    rows = [row(score=50, roles=("product",), company="product"),
            row(score=50, roles=("pm",), company="project")]
    assert order(rows) == ["project", "product"]


def test_the_date_falls_back_to_first_seen_and_a_dateless_row_sorts_last():
    rows = [row(date="", company="dateless"), row(date="2026-09-02", company="dated")]
    rows[0]["first_seen"] = ""
    assert order(rows, "newest") == ["dated", "dateless"]


def test_an_unparseable_date_does_not_raise():
    for bad in ("not-a-date", "2026-9-1", "20260901", "2026-XX-01", None):
        r = row(company="c")
        r["date"], r["first_seen"] = bad, bad
        assert isinstance(web._row_date_num(r), int)


# ---------------------------------------------------------------------------------------------
# the employer run breaker


def pairs(*companies):
    return [(row(company=c, url="u%d" % i), "") for i, c in enumerate(companies)]


def longest_run(ps):
    best = cur = 0
    last = None
    for r, _st in ps:
        cur = cur + 1 if r["company"] == last else 1
        last = r["company"]
        best = max(best, cur)
    return best


def test_a_wall_of_one_employer_is_broken_up():
    out = web._break_employer_runs(pairs("A", "A", "A", "A", "B", "C", "D"))
    assert longest_run(out) <= web._EMPLOYER_RUN_MAX, [r["company"] for r, _ in out]


def test_nothing_is_ever_dropped():
    """DEFERRAL, not removal. Fifty real openings at one company are fifty real openings --
    docs/ARCHITECTURE.md, and the reason the +N-more tile was deleted rather than restored."""
    ps = pairs(*(["A"] * 30 + ["B"] * 5 + ["C"]))
    out = web._break_employer_runs(ps)
    assert len(out) == len(ps)
    assert sorted(r["url"] for r, _ in out) == sorted(r["url"] for r, _ in ps)


def test_a_single_employer_feed_is_not_reshuffled_into_nonsense():
    """Searching for one company, or filtering to it, must not reorder the result to satisfy
    a rule about variety. With no other employer within the lookahead the run is ACCEPTED."""
    ps = pairs(*(["A"] * 12))
    out = web._break_employer_runs(ps)
    assert [r["url"] for r, _ in out] == [r["url"] for r, _ in ps]


def test_the_sort_order_survives_wherever_it_can():
    """A card only moves when it would extend a run past the cap."""
    ps = pairs("A", "B", "C", "D", "E")
    assert web._break_employer_runs(ps) == ps


def test_a_deferred_row_comes_back_immediately():
    """It is a swap with the nearest different employer, not a demotion to the end."""
    out = [r["company"] for r, _ in web._break_employer_runs(pairs("A", "A", "A", "B", "A"))]
    assert out[:3] == ["A", "A", "B"], out
    assert out.count("A") == 4


def test_a_cap_of_zero_disables_it():
    ps = pairs("A", "A", "A", "A")
    assert web._break_employer_runs(ps, cap=0) == ps


def test_a_blank_employer_is_not_treated_as_one_company():
    """Every row with no company would otherwise read as one giant run and thrash the lookahead
    for nothing. They are equal to each other, which is the honest answer, and the cap applies."""
    ps = pairs("", "", "", "")
    out = web._break_employer_runs(ps)
    assert len(out) == len(ps)


# ---------------------------------------------------------------------------------------------
# seniority: the card and the filter must not disagree


def test_a_senior_card_cannot_sit_inside_a_years_ceiling():
    """core.title_level sees roman numerals and the Group/Advanced/Expert forms;
    core.title_experience_tier deliberately does not, because a LEVEL is not a year COUNT.
    The consequence, measured 2026-09-10 over 38,826 active rows: `level` says senior on 15,300
    and the years tier answers None on 2,172 of them, so the card printed Senior while the row
    sat inside the 0-to-2-years band."""
    for title in ("Technical Program Manager V", "Materials Project Manager III",
                  "Group Product Manager", "Product Manager (L5)"):
        assert core.title_level(title) == "senior", title
        r = {"title": title, "level": core.title_level(title), "exp_eff": "", "exp_years": ""}
        for band in ("2", "5", "senior"):
            assert not core.prefs_match(r, dict(core.DEFAULT_PREFS, exp=band, min=0)), \
                (title, band)


def test_the_second_rung_is_mid_and_not_senior():
    """"ii" sat with iii-vi until 2026-09-10 and made "Software Engineer II" senior. It is the
    weakest numeral in the set (71.5% against 81.1% for iii) and it is not what the word means.
    Measured: 1,183 active rows, 3.0% of the corpus, were senior for no reason but a bare ii --
    Project Manager II, Program Manager II, Technical Program Manager II, the on-target ones."""
    for title in ("Software Engineer II", "Project Manager II", "Coordinator II",
                  "Manager II, Operations Management"):
        assert core.title_level(title) == "mid", (title, core.title_level(title))


def test_a_mid_level_fails_a_two_year_ceiling_and_passes_a_five():
    """Which is exactly what exp_level_for would have said from a year count: mid is 3-5."""
    r = {"title": "Software Engineer II", "level": "mid", "exp_eff": "", "exp_years": ""}
    assert not core.prefs_match(r, dict(core.DEFAULT_PREFS, exp="2", min=0))
    assert core.prefs_match(r, dict(core.DEFAULT_PREFS, exp="5", min=0))


def test_a_senior_word_still_beats_the_numeral():
    assert core.title_level("Senior Engineer II") == "senior"
    assert core.title_level("Engineer I") == "entry"


def test_the_level_bands_are_the_inverse_of_exp_level_for():
    """LEVEL_MIN_YEARS is not a second opinion about seniority, it is exp_level_for read
    backwards. If one moves without the other the filter and the card stop agreeing again."""
    for level, floor in core.LEVEL_MIN_YEARS.items():
        assert core.exp_level_for(floor) == level, (level, floor)


def test_a_stated_year_count_still_wins_over_the_title():
    """The employer's own number beats anything read off the title -- unchanged."""
    r = {"title": "Technical Program Manager V", "level": "senior", "exp_eff": 1}
    assert core.prefs_match(r, dict(core.DEFAULT_PREFS, exp="2", min=0))


def test_a_row_we_could_not_read_at_all_is_still_kept():
    """A posting with NEITHER signal is always kept -- many genuine entry-level posts state no
    number. Only a row we have actually decided is SENIOR is dropped."""
    r = {"title": "Project Coordinator", "level": "", "exp_eff": ""}
    assert core.prefs_match(r, dict(core.DEFAULT_PREFS, exp="2", min=0))


def test_an_internship_is_exempt_however_it_is_titled():
    r = {"title": "Program Manager Intern", "level": "entry", "exp_eff": "", "intern": True}
    assert core.prefs_match(r, dict(core.DEFAULT_PREFS, exp="2", min=0))


def test_title_level_has_no_unreachable_second_half():
    """It ended with an empty return followed by four more lines repeating the entry/senior
    checks. Harmless, unreachable, and exactly the kind of thing that gets edited."""
    import inspect
    body = inspect.getsource(core.title_level)
    assert body.count('return "entry"') == 2, "title_level's dead tail is back"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  -", fn.__name__)
    print("\nAll %d feed-order checks passed." % len(fns))
