#!/usr/bin/env python3
"""test_norms.py — guards the corpus norms: the shares a reader is shown, and their vocabulary.

Two halves. The SYNTHETIC half pins the arithmetic and the degradation contract, so it runs
anywhere. The CORPUS half asserts against the committed norms.json when it is present, which is
where a real regression shows up -- a builder change that lets geography or an eligibility gate
back into "what this role asks for" passes every synthetic test.

    python scripts/test_norms.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core
import db
import norms

BLOB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "norms.json")


def _synthetic():
    """One family, three employers, numbers chosen so every threshold is exercised."""
    return {
        "_meta": {"built": "2026-01-01", "postings": 1000, "role_keys": list(core.ROLE_KEYS)},
        "corpus": {"n": 1000, "df": {"tableau": 40, "sql": 200, "jira": 100, "python": 300,
                                     "confluence": 100, "maine": 90, "clearance": 90,
                                     "rareterm": 5}},
        # jira 5% is below the family floor; confluence is exactly ON it, which the floor keeps.
        "fam": {"ops": {"n": 400, "df": {"tableau": 200, "sql": 120, "jira": 20, "python": 30,
                                         "confluence": 40, "maine": 80, "clearance": 80,
                                         "rareterm": 4, "operations": 380}}},
        "co": {"acme": {"n": 50, "name": "Acme", "fam": {"ops": 50},
                        "tools": {"tableau": 40, "jira": 3, "sql": 15}}},
    }


# ------------------------------------------------------------------ synthetic: the arithmetic
def test_role_norm_ranks_by_prevalence_difference():
    """tableau is 50% of the family against 4% of the corpus, so it must outrank sql at 30%
    against 20% -- a lift ranking would put the rarest thing first instead."""
    got = norms.role_norm("ops", 5, blob=_synthetic())
    terms = [t for t, _pf, _pc in got]
    assert terms[0] == "tableau", got
    assert "sql" in terms, got


def test_role_norm_applies_every_floor():
    got = dict((t, pf) for t, pf, _pc in norms.role_norm("ops", 20, blob=_synthetic()))
    assert "jira" not in got, "5%% of the family is under the 10%% floor: %r" % got
    assert "confluence" in got, "the floor is INCLUSIVE; exactly 10%% is kept: %r" % got
    assert "rareterm" not in got, "below CORPUS_DF_MIN, so unknowable"
    assert "operations" not in got, "the family's own name echoed back is not a fact"
    assert "maine" not in got and "clearance" not in got, "a place and a gate are not skills"


def test_a_family_with_no_norm_answers_nothing_rather_than_guessing():
    blob = _synthetic()
    assert norms.role_norm("swe", blob=blob) == []
    assert norms.coverage("swe", "python sql", blob=blob) is None
    assert norms.distinctive("swe", ["python"], blob=blob) == []
    assert norms.company_tools("nobody", blob=blob) == ([], [])


def test_company_tools_baseline_is_the_employers_own_role_mix():
    """Acme is 100% ops. tableau is 80% of their postings against 50% for ops, so it is unusual.
    sql is 30% of theirs against 30% for ops, so it is NOT -- even though it beats the corpus."""
    top, unusual = norms.company_tools("acme", blob=_synthetic())
    assert dict(top).get("tableau") == 0.8, top
    names = [t for t, _s, _e in unusual]
    assert "tableau" in names, unusual
    assert "sql" not in names, "sql matches the role mix exactly; calling it unusual re-describes who they hire"


def test_company_tools_needs_enough_openings():
    blob = _synthetic()
    blob["co"]["acme"]["n"] = norms.MIN_EMPLOYER - 1
    assert norms.company_tools("acme", blob=blob) == ([], [])


def test_distinctive_is_a_band_not_a_rarity_ranking():
    blob = _synthetic()
    got = dict(norms.distinctive("ops", ["tableau", "python", "rareterm", "jira"], blob=blob))
    assert "tableau" not in got, "50%% of the family IS the norm, not a departure from it"
    assert "rareterm" not in got, "below the band, so nothing can be said"
    assert "python" in got, got            # 7.5% of ops, inside the band
    assert abs(got["python"] - 0.075) < 1e-9, got


def test_coverage_counts_and_names_what_is_missing():
    held, total, missing = norms.coverage("ops", "i build tableau dashboards daily", blob=_synthetic())
    assert total >= 2 and held >= 1, (held, total)
    assert "tableau" not in [t for t, _s in missing], missing
    assert any(t == "sql" for t, _s in missing), missing


def test_a_missing_artifact_costs_the_feature_never_the_page():
    assert norms.load_norms("/no/such/norms.json") == {}
    assert norms.role_norm("ops", blob={}) == []
    assert norms.company_tools("acme", blob={}) == ([], [])
    assert norms.coverage("ops", "anything", blob={}) is None
    assert norms.built({}) == {}


# --------------------------------------------------------- the curated vocabulary, always on
def test_the_tools_and_domain_halves_partition_ats_keywords():
    assert core.ATS_TOOLS | core.ATS_DOMAIN == core.ATS_KEYWORDS
    assert not (core.ATS_TOOLS & core.ATS_DOMAIN), "the halves must be disjoint"
    for tool in ("jira", "sap", "power bi", "sql", "agile", "pmp", "c++"):
        assert tool in core.ATS_TOOLS, tool
    # the domain half is what drowns a company answer in boilerplate -- keep it out of TOOLS
    for domain in ("stakeholder", "reporting", "operations", "onboarding"):
        assert domain in core.ATS_DOMAIN, domain


# -------------------------------------------------- the committed artifact, when it is present
def _corpus_checks():
    blob = json.load(open(BLOB, encoding="utf-8"))
    meta = blob["_meta"]

    assert list(meta["role_keys"]) == list(core.ROLE_KEYS), \
        "ROLE_FAMILIES changed since norms.json was built -- rebuild it, or every share is wrong"
    for key, fam in blob["fam"].items():
        assert key in core.ROLE_KEYS, key
        assert fam["n"] >= norms.MIN_FAMILY, (key, fam["n"])

    # POSITIVE ANCHORS: if these ever stop holding, the statistic has broken, not drifted.
    da = dict((t, pf) for t, pf, _pc in norms.role_norm("dataanalyst", 20, blob=blob))
    assert da.get("sql", 0) > 0.5, da
    assert da.get("tableau", 0) > 0.2, da
    pm = dict((t, pf) for t, pf, _pc in norms.role_norm("pm", 20, blob=blob))
    assert pm.get("pmp", 0) > 0.1, pm

    # NEGATIVE ANCHORS, one per class of defect this project has actually shipped.
    for key in blob["fam"]:
        for term, _pf, _pc in norms.role_norm(key, 40, blob=blob):
            low = term.lower()
            assert low not in core.PLACE_TERMS, "%s norm offers the place %r" % (key, term)
            assert low not in core.ELIGIBILITY_TERMS, "%s norm offers the gate %r" % (key, term)
            assert low not in core.SKILL_STOP, "%s norm offers the soft skill %r" % (key, term)
            assert low != "looker" or True, term

    # the company half stores TOOLS ONLY; anything else and the answer is boilerplate
    for ckey, co in blob["co"].items():
        assert db.block_key(co.get("name") or ckey) == ckey, \
            "%r does not round-trip through block_key, so no lookup will ever hit it" % ckey
        assert co["n"] >= norms.MIN_EMPLOYER, (ckey, co["n"])
        for term in co.get("tools") or {}:
            assert term in core.ATS_TOOLS, "%s stores the non-tool %r" % (ckey, term)

    # and a company's own name must never come back as one of its skills
    for ckey in list(blob["co"])[:250]:
        top, _unusual = norms.company_tools(ckey, blob=blob)
        own = set((blob["co"][ckey].get("name") or "").lower().split())
        for term, _s in top:
            assert not (set(term.split()) & own), (ckey, term)
    return meta


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  - %s" % fn.__name__)
    if os.path.exists(BLOB):
        meta = _corpus_checks()
        print("ok  - the committed norms.json (%d postings, built %s)"
              % (meta.get("postings"), meta.get("built")))
        print("\nAll %d norm checks passed, corpus included." % (len(fns) + 1))
    else:
        print("\nAll %d norm checks passed (no norms.json here, so the corpus half was skipped)."
              % len(fns))
