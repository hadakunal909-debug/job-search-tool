#!/usr/bin/env python3
"""
test_sponsor_flag.py — freezes how sponsors_h1b resolves an employer name.

The flag used to read sponsors.txt and nothing else. That file holds ~620 names; visa_tags
holds ~77k and sponsor_counts ~118k, and both were already on disk, loaded by the app for
its badges. Measured against the live corpus on 2026-08-12: sponsors.txt alone resolved
277 of 1,682 employers, the federal indexes resolve 973 more. Every cap-exempt university
and hospital we scrape was storing sponsors_h1b='no' — the opposite of the truth for
exactly the employers an F-1 candidate should be looking at.

The tempting next step is to fuzzy-match the wide index too. Do not: at 77k entries the
nearest key to "Affinity" is "affinity aeronautical solutions" and the nearest to
"Adaptive Innovations" is "adaptive health". Different companies, both would flag. Fuzzy
stays scoped to the small curated list, which is what lets "Oak Ridge National Laboratory"
still reach "Ut Battelle LLC Oak Ridge National Laboratory".

No database and no network — the indexes are built inline.
"""
import sys

import scraper

# A stand-in for sponsors.txt: small, so the fuzzy tier is live (the <=5000 rule).
CURATED = ["Ut Battelle LLC Oak Ridge National Laboratory",
           "The Childrens Hospital of Philadelphia",
           "Meta Platforms"]

# Stand-ins for visa_tags / sponsor_counts keys. Those files are keyed by _norm_name
# output, so anything put here must already be normalised.
WIDE = {scraper._norm_name(n) for n in [
    "University of California, San Francisco",
    "Iowa State University",
    "Howard Hughes Medical Institute",
    "Ochsner Clinic Foundation",
    "Affinity Aeronautical Solutions",
    "Adaptive Health",
]}

NARROW = scraper.build_sponsor_index(CURATED)            # the old behaviour
WIDE_IDX = scraper.build_sponsor_index(CURATED, WIDE)    # the current behaviour


def flag(company, index):
    scraper._sponsor_cache.clear()        # the cache is keyed by company name only
    return scraper.sponsors_h1b(company, index)


def test_federal_index_resolves_what_sponsors_txt_never_had():
    # The three that shipped wrong in commit 95409ee's scoped scrape.
    for co in ("University of California, San Francisco", "Iowa State University",
               "Howard Hughes Medical Institute"):
        assert flag(co, NARROW) is False, "%s should be invisible to the curated list" % co
        assert flag(co, WIDE_IDX) is True, "%s should resolve via the federal indexes" % co


def test_fuzzy_over_the_curated_list_still_works():
    # These resolve ONLY because the curated list is small enough for the fuzzy tier:
    # the stored labels differ from the legal names the DOL files carry.
    assert flag("Oak Ridge National Laboratory", WIDE_IDX) is True
    assert flag("Children's Hospital of Philadelphia", WIDE_IDX) is True
    assert flag("Meta", WIDE_IDX) is True


def test_the_wide_index_is_never_fuzzy_matched():
    # Both of these have a close neighbour in WIDE. Flagging them would be a false
    # "this employer sponsors", which is worse than the miss it replaces.
    assert flag("Affinity", WIDE_IDX) is False
    assert flag("Adaptive Innovations", WIDE_IDX) is False


def test_a_name_variant_the_indexes_cannot_bridge_still_misses():
    # We store "Ochsner Health"; every federal file calls it "Ochsner Clinic Foundation".
    # Exact matching cannot close that and fuzzy is not allowed over the wide index, so
    # this is a KNOWN miss. Recorded so it is a decision, not a surprise: the fix is an
    # entry in sponsors.txt, where the fuzzy tier can reach it.
    assert flag("Ochsner Health", WIDE_IDX) is False


def test_unknown_employer_stays_unflagged():
    assert flag("Zzzz Nonexistent Holdings", WIDE_IDX) is False
    assert flag("", WIDE_IDX) is False


def test_build_sponsor_index_without_wide_is_the_old_shape():
    # main() passes `wide` positionally; a caller that predates it must still work.
    idx = scraper.build_sponsor_index(CURATED)
    assert idx["wide"] == set()
    assert scraper._norm_name("Meta Platforms") in idx["norm"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  -", fn.__name__)
    print("\nAll %d sponsor-flag checks passed." % len(fns))
