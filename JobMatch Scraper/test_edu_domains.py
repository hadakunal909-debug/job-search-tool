#!/usr/bin/env python3
"""
test_edu_domains.py — freezes the .edu domain lookup that makes universities discoverable.

Universities are the one employer class worth chasing on principle: an H-1B filed by one is
cap-exempt, so it skips the lottery. They were also the class the board discovery could never
reach. The old rule — strip "University of", glue the rest together, add ".edu" — got 4 of the
32 universities in careers_us.md right and sent the other 28 to a domain that does not exist.

Every DOMAIN below was resolved and confirmed against the live site on 2026-08-21, and every
TITLE below is the real homepage title fetched the same day. Both halves are pinned because
both halves failed on the first two attempts:

  * generating the right candidate is not enough if the picker chooses the wrong one, and
  * the picker cannot be judged on invented titles — the near-miss schools are near-misses
    precisely because their real titles look correct.

No database and no network — the two functions under test are pure string logic, which is why
the fix was split that way in the first place.
"""
import sys

from scraper import find_everify_boards as feb

# name -> the .edu domain that actually serves it (verified live 2026-08-21).
# The comment on each line is the naming convention it needed, i.e. why one rule cannot work.
VERIFIED = {
    "University of Michigan": "umich.edu",                      # "u" + truncated state
    "University of Oregon": "uoregon.edu",                      # "u" + whole state
    "University of Idaho": "uidaho.edu",
    "University of Tulsa": "utulsa.edu",
    "University of Akron": "uakron.edu",
    "University of Delaware": "udel.edu",                       # "u" + truncation
    "University of South Carolina": "sc.edu",                   # initials, no "u"
    "University of New Hampshire": "unh.edu",                   # "u" + initials
    "University of Southern Mississippi": "usm.edu",
    "University of Missouri": "missouri.edu",                   # the bare name
    "Towson University": "towson.edu",
    "Kean University": "kean.edu",
    "Drexel University": "drexel.edu",
    "Old Dominion University": "odu.edu",                       # initials + "u"
    "James Madison University": "jmu.edu",
    "Utah State University": "usu.edu",
    "Western Kentucky University": "wku.edu",
    "North Dakota State University": "ndsu.edu",
    "Sam Houston State University": "shsu.edu",
    "Southern Illinois University Edwardsville": "siue.edu",     # initials incl. "University"
    "Wichita State University": "wichita.edu",                  # name with "State" dropped
    "Weber State University": "weber.edu",
    "Montclair State University": "montclair.edu",
    "Arkansas State University": "astate.edu",                  # first initial + "state"
    "Kansas State University": "k-state.edu",                   # first initial + "-state"
    "California State University Fullerton": "fullerton.edu",   # the campus alone
    "California State University Chico": "csuchico.edu",         # "csu" + campus
    # Generated as ucdenver.edu, which 301s to cudenver.edu — so the resolver REPORTS
    # cudenver.edu while the generator only ever has to propose ucdenver.edu. Same for
    # College of Charleston, where cofc.edu redirects to charleston.edu.
    "University of Colorado Denver": "ucdenver.edu",
    "University of Detroit Mercy": "udmercy.edu",
    "University of Louisiana at Lafayette": "louisiana.edu",    # drop the campus
    "College of Charleston": "charleston.edu",
    "Boston College": "bc.edu",
}


def test_every_verified_domain_is_generated():
    """The generator has to PROPOSE the right domain before anything can confirm it.

    This is the assertion the old rule failed 28 times out of 32. It checks membership, not
    position: which convention wins is decided by the network, not by this list's order.
    """
    missing = []
    for name, domain in VERIFIED.items():
        label = domain[:-4]                       # strip ".edu"
        if label not in feb._edu_domain_candidates(name):
            missing.append("%s -> %s" % (name, domain))
    assert not missing, "not generated: " + "; ".join(missing)


def test_generator_stays_small():
    """Every extra candidate is a live HTTP fetch during discovery, so the conventions have to
    pay for themselves. 14 guesses covers all 32 institutions; a change that needs many more
    is adding a special case, not a convention."""
    for name in VERIFIED:
        n = len(feb._edu_domain_candidates(name))
        assert n <= 14, "%s generates %d candidates" % (name, n)


def test_no_candidate_is_a_stub():
    """A one-character label would match half the .edu namespace. The generator drops them
    rather than letting the network sort it out."""
    for name in VERIFIED:
        for d in feb._edu_domain_candidates(name):
            assert len(d) > 1 and not d.startswith("-") and not d.endswith("-"), (name, d)


# (institution, real homepage title, is this the right school?) — all titles fetched 2026-08-21.
# The wrong ones are the four that a body-text or word-fraction rule actually picked.
TITLES = [
    ("University of Michigan", "Marquette University // Be The Difference", False),
    ("Boston College", "Boston Baptist College", False),
    ("Boston College", "Home - Boston College", True),
    ("Arkansas State University", "University of Arkansas", False),
    ("Arkansas State University", "Arkansas State University | Home of the Red Wolves", True),
    ("Drexel University", "University of Denver", False),
    ("Drexel University", "Drexel Home", True),
    ("California State University Chico", "Home | Chadron State College", False),
    ("California State University Chico", "Chico State", True),
    ("Utah State University", "The University of Utah", False),
    ("Utah State University", "Utah State University", True),
    ("University of New Hampshire", "Hampshire College | Hampshire College", False),
    ("University of New Hampshire",
     "University of New Hampshire [UNH] | University of New Hampshire", True),
]


def admits(name, title):
    """The admission half of _resolve_edu_domains, applied to one title."""
    score, extra, named = feb._edu_title_match(title, name)
    return score >= 0.75 or (extra == 0 and named)


def test_wrong_school_is_not_admitted_outright():
    """The failure this fix exists to prevent is adopting a DIFFERENT university's board.

    "Chico State" scores 0.5 and is right; "University of Arkansas" scores 0.67 and is wrong.
    So the rule cannot be a threshold on the fraction — it needs the unexplained-word count.
    """
    for name, title, correct in TITLES:
        if correct:
            assert admits(name, title), "rejected the right school: %s / %r" % (name, title)


def test_near_miss_schools_lose_the_ranking():
    """Two wrong titles clear the admission bar on their own — "University of Arkansas" (0.67,
    nothing unexplained) and "Boston Baptist College" (1.00, one word unexplained). Only the
    best-tied rule drops them, so it is pinned here rather than left to the network."""
    for name in ("Arkansas State University", "Boston College", "Utah State University"):
        cands = [(feb._edu_title_match(t, n)[0], -feb._edu_title_match(t, n)[1], t, ok)
                 for n, t, ok in TITLES if n == name and admits(n, t)]
        assert cands, name
        cands.sort(reverse=True)
        best = cands[0][:2]
        winners = [(t, ok) for s, e, t, ok in cands if (s, e) == best]
        assert all(ok for _t, ok in winners), "%s picked %r" % (name, winners)
        assert len(winners) == 1, "%s left a tie: %r" % (name, winners)


def test_filler_does_not_count_against_a_match():
    """"Home - Boston College" and "Boston Baptist College" both contain every word of "Boston
    College". They are separable only because "home" is filler and "baptist" is not."""
    _s, extra_right, _n = feb._edu_title_match("Home - Boston College", "Boston College")
    _s, extra_wrong, _n = feb._edu_title_match("Boston Baptist College", "Boston College")
    assert extra_right == 0, extra_right
    assert extra_wrong > extra_right, (extra_wrong, extra_right)


def test_named_requires_a_word_unique_to_the_school():
    """`named` must not be satisfied by "university" or "state", which every school shares —
    that is the whole reason "University of Denver" fails for Drexel despite sharing a word."""
    assert not feb._edu_title_match("University of Denver", "Drexel University")[2]
    assert not feb._edu_title_match("Some State University", "Utah State University")[2]
    assert feb._edu_title_match("Chico State", "California State University Chico")[2]


def test_empty_and_junk_titles_are_rejected():
    """A missing title must score as evidence AGAINST, not as a neutral pass: a bot-walled root
    returns no title, and treating that as a match is how wichita.edu lost to the wrong host."""
    for t in ("", None, "Home", "Welcome"):
        score, extra, named = feb._edu_title_match(t, "Wichita State University")
        assert not (score >= 0.75 or (extra == 0 and named)), (t, score, extra, named)


def test_non_institutions_generate_nothing():
    """_careers_candidates only reaches this code behind _EDU_WORDS, but the generator is
    public enough to be called directly, and a company name must not produce .edu guesses."""
    assert not feb._EDU_WORDS.search("State of South Dakota")
    assert not feb._EDU_WORDS.search("Booz Allen Hamilton")
    assert feb._EDU_WORDS.search("Boston College")
    assert feb._EDU_WORDS.search("University of Michigan")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  -", fn.__name__)
    print("\nAll %d .edu-domain checks passed (%d institutions pinned)."
          % (len(fns), len(VERIFIED)))
