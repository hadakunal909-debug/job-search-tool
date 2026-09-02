#!/usr/bin/env python3
"""norms.py — what the app has learned from its own corpus about ROLES and EMPLOYERS.

Two questions no single posting can answer, and the corpus can:

  * "What does an Operations Manager posting usually ask for?"   role_norm("ops")
  * "Which tools does this employer actually lean on?"           company_tools(key)

plus the two that follow from having those: what is UNUSUAL about the posting in front of you
(distinctive), and how much of the role's usual ask your résumé already covers (coverage).

WHY THIS IS NOT core.py. core is imported by the scraper, the digest and score_jobs, none of
which needs a 640 KB table of shares; and the ranking here is a presentation judgement, not part
of the match. Same argument jdrender.py makes for itself. It imports core and the standard
library, nothing else -- so a test of a share needs no Flask app and no database.

THE ARTIFACT IS OPTIONAL BY CONTRACT. load_norms() returns {} when norms.json is absent and
every function degrades to "no answer", which is the contract core.load_sponsor_counts already
has: a missing data file costs the feature, never the page.

The counting lives in scripts/build_norms.py; the RANKING lives here, so the builder's own
--show output and what a reader sees can never drift apart.
"""
import json
import os

import core

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "norms.json")
# Memoised the way core.load_idf is: read once per process, and let a long-lived worker pick up
# a rebuilt file through _reset_cache() (web.py's /reload calls it).
_cache = {"blob": None, "loaded": False}
_skills_cache = {"key": None, "vocab": None}

# A family smaller than this gets no norm at all -- at 150 rows a "10% of postings" claim rests
# on fifteen documents. Kept here rather than in the builder because a READER has to know which
# claims the artifact is entitled to make.
MIN_FAMILY = 300
# No employer may be more than this share of a family's rows; one employer held 328 of the 1,822
# `systems` postings, and without the cap its template became "what the role asks for".
EMPLOYER_SHARE = 0.05
# "What the role asks for" means at least this many of its postings say so...
FAM_FLOOR = 0.10
# ...and the term is known corpus-wide, or the share is noise on a handful of rows.
CORPUS_DF_MIN = 20
# An employer needs this many openings before a share of them means anything.
MIN_EMPLOYER = 12
# "Unusual for this role" is a BAND. Below the floor the term is unknowable for the family;
# above the ceiling it is the norm rather than a departure from it.
DISTINCT_MIN = 0.02
DISTINCT_MAX = 0.35


def _reset_cache():
    _cache["blob"] = None
    _cache["loaded"] = False
    _skills_cache["key"] = None


def load_norms(path=_PATH):
    """The norms blob, or {} if it was never built. Never raises."""
    if path == _PATH and _cache["loaded"]:
        return _cache["blob"] or {}
    blob = {}
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                blob = json.load(f) or {}
    except Exception:
        blob = {}
    if path == _PATH:
        _cache["blob"] = blob
        _cache["loaded"] = True
    return blob


def built(blob=None):
    """The _meta dict, so a page can state the vintage instead of implying freshness."""
    return ((blob if blob is not None else load_norms()).get("_meta") or {})


def _own_words(key):
    """The words a family is NAMED by. `ops` postings say "operations" 95% of the time, which is
    the title echoed back rather than a thing to know."""
    own = set((core.ROLE_LABELS.get(key) or key).lower().split())
    for k, _lab, _grp, phrases in core.ROLE_FAMILIES:
        if k == key:
            for ph in phrases:
                own.update(ph.split())
    return own


def _dedupe_by_stem(scored):
    """`budget` at 55% and `budgeting` at 57% are one fact. Keep the stronger spelling."""
    best = {}
    for row in scored:
        k = " ".join(core._stem(w) for w in row[1].split())
        if k not in best or row[0] > best[k][0]:
            best[k] = row
    return sorted(best.values(), reverse=True)


def _skill_vocab(blob):
    """The vocabulary that can be called a SKILL: the curated tools and domain terms, plus
    anything a family norm has validated as role-relevant.

    Derived at READ time rather than stored, so a term newly added to core.KEYWORD_STOP drops
    out of it without rebuilding the artifact -- role_norm below filters through
    core.display_terms, so the two stay in step by construction.

    Memoised per blob identity: it is 21 role_norm passes and `distinctive` is called per page.
    """
    key = id(blob)
    if _skills_cache.get("key") != key:
        vocab = set(core.ATS_KEYWORDS)
        for fam_key in (blob.get("fam") or {}):
            vocab |= {t for t, _pf, _pc in role_norm(fam_key, 60, blob=blob)}
        _skills_cache["key"] = key
        _skills_cache["vocab"] = vocab
    return _skills_cache["vocab"]


def role_norm(key, cap=12, blob=None):
    """[(term, share_in_family, share_in_corpus)] — what this role usually asks for.

    Ranked by PREVALENCE DIFFERENCE, share_in_family - share_in_corpus, not by a lift ratio.
    Lift saturates: any term exclusive to a family hits the ceiling N/n_fam, so the top of a
    lift ranking for `ops` was "salaried", "stairs", "mile", "shifts" and "delivered". The
    difference lets a common term with a real gap beat a rare exclusive one, and both halves
    are shares a reader can check.
    """
    blob = load_norms() if blob is None else blob
    fam = (blob.get("fam") or {}).get(key)
    if not fam:
        return []
    corpus = blob.get("corpus") or {}
    n, total = float(fam["n"]), float(corpus.get("n") or 0)
    cdf = corpus.get("df") or {}
    if not n or not total:
        return []
    own = _own_words(key)
    scored = []
    for term, count in (fam.get("df") or {}).items():
        share = count / n
        if share < FAM_FLOOR:
            continue
        cd = cdf.get(term, 0)
        if cd < CORPUS_DF_MIN:
            continue
        if any(w in own for w in term.split()):
            continue
        if not core.display_terms([term], "", 1):
            continue
        scored.append((share - cd / total, term, share, cd / total))
    return [(t, pf, pc) for _d, t, pf, pc in _dedupe_by_stem(scored)[:cap]]


def company_tools(ckey, cap=8, min_share=0.15, min_gap=0.05, blob=None):
    """(top, unusual) for one employer, keyed by db.block_key(company).

    `top` is the tools they name most; `unusual` is the tools they name MORE than their own role
    mix predicts. The baseline is that mix and not the corpus, because a company hiring mostly
    engineers uses more git than average and reporting that as a fact about the company would
    just re-describe who they hire.

    TOOLS ONLY -- core.ATS_TOOLS. Over every term the honest answer is a boilerplate paragraph:
    measured, Northrop Grumman "employees 94%", JPMorgan "capabilities 69%", Amazon "onboarding
    97%". Restricted to the curated half it is Northrop "sap 31% against 4% expected".
    """
    blob = load_norms() if blob is None else blob
    co = (blob.get("co") or {}).get(ckey)
    if not co:
        return [], []
    n = float(co.get("n") or 0)
    if n < MIN_EMPLOYER:
        return [], []
    mix = co.get("fam") or {}
    mix_total = float(sum(mix.values())) or 1.0
    fams = blob.get("fam") or {}
    corpus = blob.get("corpus") or {}
    total = float(corpus.get("n") or 0)
    cdf = corpus.get("df") or {}
    # A TOOL THAT IS THE EMPLOYER'S OWN NAME IS THE NAME, not a thing to know: Oracle uses
    # Oracle, Workday uses Workday, Salesforce uses Salesforce. Same reason role_norm drops the
    # family's own words -- true, and it tells a reader nothing they did not have.
    own = set((co.get("name") or ckey).lower().replace(",", " ").replace(".", " ").split())
    rows = []
    for term, count in (co.get("tools") or {}).items():
        share = count / n
        if share < min_share:
            continue
        if set(term.split()) & own:
            continue
        expected = 0.0
        for k, kn in mix.items():
            fam = fams.get(k)
            if fam and fam.get("n"):
                expected += (kn / mix_total) * (fam["df"].get(term, 0) / float(fam["n"]))
        if not expected and total:
            expected = cdf.get(term, 0) / total
        rows.append((share - expected, term, share, expected))
    # Stem-deduped like role_norm: `kpi` and `kpis` are one tool, and Oracle was reporting both
    # at 19% as if they were two.
    rows = _dedupe_by_stem(rows)
    top = [(t, s) for _d, t, s, _e in sorted(rows, key=lambda r: -r[2])[:cap]]
    unusual = [(t, s, e) for d, t, s, e in rows if d >= min_gap][:cap]
    return top, unusual


def distinctive(key, terms, cap=6, blob=None):
    """[(term, share_in_family)] — what THIS posting asks for that the role usually does not.

    A band, not a ranking of rarity: below DISTINCT_MIN the term is unknowable for the family
    and above DISTINCT_MAX it IS the norm. Ranked ascending, so the least usual comes first.
    """
    blob = load_norms() if blob is None else blob
    fam = (blob.get("fam") or {}).get(key)
    if not fam:
        return []
    n = float(fam["n"])
    corpus = blob.get("corpus") or {}
    cdf = corpus.get("df") or {}
    # ONLY THINGS THAT COULD BE A SKILL. Without this the line read "unusual for this role:
    # salaried, stairs, https, jobs, problems, together" -- every one of them genuinely rare for
    # the family and none of them a thing to know. A ratio floor was tried first and measured
    # WORSE than useless: at 1.5 it let "employees" into the ops norm and at 2.0 it dropped
    # "reporting" and "stakeholder", which the role really does ask for.
    vocab = _skill_vocab(blob)
    out = []
    for term in (terms or []):
        low = (term or "").lower()
        if low not in vocab:
            continue
        if cdf.get(low, 0) < CORPUS_DF_MIN:
            continue
        share = (fam.get("df") or {}).get(low, 0) / n
        if share < DISTINCT_MIN or share >= DISTINCT_MAX:
            continue
        if not core.display_terms([low], "", 1):
            continue
        out.append((share, low))
    out.sort()
    seen, res = set(), []
    for share, term in out:
        k = " ".join(core._stem(w) for w in term.split())
        if k in seen:
            continue
        seen.add(k)
        res.append((term, share))
        if len(res) >= cap:
            break
    return res


def coverage(key, resume_low, cap=12, blob=None):
    """(held, total, missing) over what this role usually asks for, or None if there is no norm.

    THIS IS THE HONEST ANSWER TO "what are my chances", and it is deliberately a count rather
    than a probability. A probability would be a calibration claim, and there is nothing to
    calibrate against: the applications table holds 233 rows, every one of them still `applied`,
    with no interview, offer or rejection recorded and match_score NULL on all of them. Judged
    against that, "you hold 7 of the 12 things this role usually asks for" is the strongest
    claim the data supports -- and it is more actionable than a number, because it names the five.

    Matched with core._term_in, the same rule the match percentage uses, so the two agree.
    """
    norm = role_norm(key, cap=cap, blob=blob)
    if not norm:
        return None
    low = (resume_low or "").lower()
    words = core._wordset(low)
    held, missing = [], []
    for term, share, _pc in norm:
        (held if core._term_in(term, low, words) else missing).append((term, share))
    return len(held), len(norm), missing
