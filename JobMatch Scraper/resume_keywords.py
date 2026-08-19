"""
resume_keywords.py — "the keywords your target roles expect", offline and per role track.

Why this exists: Resume Worded scores five categories and one of them is Skills. `resume_score.py`
had no equivalent, because the other four can be judged from the document alone and this one needs
an outside opinion about what the job market asks for.

Their source is a curated list built from job posts. Ours is better positioned and worse curated:

  * `idf.json` holds 622,354 weighted terms mined from real postings — but it is a STATISTICAL
    vocabulary with no notion of skill-ness. "block logo" scores 8.21 and `pmp` scores 4.11. Rarity
    is not skill-ness, which core.py's own comments say at length. Ranking by IDF and calling the
    top terms "skills" would produce confident nonsense.
  * `core.ATS_KEYWORDS` + `core.SKILLS` are genuinely curated — and tiny, about 270 concepts, and
    visibly biased to PM/analyst/ops work (no `react`, no `pytorch`).

So the design is: **curation decides what counts as a skill, the corpus decides which skills matter
for which track.** scripts/build_resume_vocab.py intersects the two — it counts how often each
curated skill appears in dev-track postings versus mgmt-track postings — and writes
resume_keywords.json. Without that file this module still works from the curated set alone, which is
track-agnostic but never wrong; it degrades in coverage, not in correctness.

Track comes from the RÉSUMÉ, not from a job title, because there is no job here. It is inferred by
whichever track's expectations the résumé already satisfies better, which is stable and explainable
and beats asking the user to self-classify.

Presence is decided by `core._term_present`, the same function the feed's match % uses, so a term
counted as present here means exactly what it means there — whole words, aliases and stems, with
"data" never answering for "database".
"""
import json
import os

import core

KEYWORDS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resume_keywords.json")
TRACKS = ("dev", "mgmt")
# How many expected terms a track is judged on. Enough to be a real bar, small enough that the
# missing list stays a to-do rather than a wall.
TOP_N = 40
_cache = {"data": None, "loaded": False}


# Soft skills are EXCLUDED from expectations, deliberately.
#
# Without this the check rewarded the exact thing the rest of the rubric penalises. A résumé whose
# skills line reads "Leadership, teamwork, communication" scored HIGHER on keywords than a strong
# résumé that demonstrated all three and listed none — while `skills_demonstrated` was docking it
# for the same line. Resume Worded names this failure mode as the flaw in keyword scanners: they
# treat soft skills like hard skills and tell people to paste "teamwork" onto a résumé, which a
# hiring manager reads as a red flag. An ATS matches hard skills; soft skills are earned by
# evidence, which is what the leadership and skills-demonstrated checks measure.
_SOFT = frozenset((
    "communication", "leadership", "teamwork", "collaboration", "problem solving",
    "interpersonal", "organization", "organizational", "time management", "adaptability",
    "attention to detail", "detail oriented", "work ethic", "self motivated", "motivated",
    "team player", "critical thinking", "creativity", "flexibility", "initiative",
    "multitasking", "reliability", "professionalism", "enthusiasm", "positive attitude",
    "written communication", "verbal communication", "presentation skills", "customer service",
))


def _curated():
    """The track-agnostic fallback: every curated skill concept we have, ordered by how COMMONLY
    postings ask for it.

    core.SKILLS maps canonical -> aliases; only the canonical form goes in, because the aliases are
    spellings of the same skill and _term_present already resolves them.

    The ordering matters more than it looks. This list gets truncated to TOP_N, and sorting it
    alphabetically — which is what a bare sorted() does — means asking every résumé about the first
    forty skills in the alphabet. That produced a Skills score of 20/100 for a strong data résumé
    and took six points off the total for no reason. IDF is the fix and it is already on disk:
    a LOW idf weight means the term appears in many postings, which is exactly "commonly expected".
    Ascending, therefore, not descending.
    """
    terms = set(core.ATS_KEYWORDS)
    try:
        terms |= set(core.SKILLS.keys())
    except Exception:
        pass
    terms = [t for t in terms if len(t) >= 3 and t.lower() not in _SOFT]
    try:
        idf = core.load_idf() or {}
    except Exception:
        idf = {}
    if not idf:
        return sorted(terms)
    # Unseen terms sort last: if the corpus has never mentioned it, it is not a common expectation.
    return sorted(terms, key=lambda t: (idf.get(t, 99.0), t))


def load_expectations(path=KEYWORDS_PATH):
    """{"dev": [[term, weight], ...], "mgmt": [...]} from the built file, or None if absent.

    Cached like core.load_idf: this is read on every scoring call and the file does not change
    under a running worker.
    """
    if not _cache["loaded"]:
        _cache["loaded"] = True
        try:
            with open(path, encoding="utf-8") as fh:
                blob = json.load(fh)
            data = {t: [(str(k), float(w)) for k, w in (blob.get(t) or [])] for t in TRACKS}
            _cache["data"] = data if any(data.values()) else None
            # Optional provenance. The panel used to state "25,036 real jobs" as a literal in the
            # template, which stopped being true the first time the corpus grew. Read it if the
            # builder recorded it; say nothing rather than say a stale number.
            meta = blob.get("_meta") or {}
            try:
                _cache["jobs"] = int(meta.get("jobs") or 0) or None
            except (TypeError, ValueError):
                _cache["jobs"] = None
        except Exception:
            _cache["data"] = None
    return _cache["data"]


def _reset_cache():
    """For tests and long-lived workers, mirroring core._reset_idf_cache."""
    _cache.update({"data": None, "loaded": False, "jobs": None})


def corpus_jobs():
    """How many postings the expectations were measured across, or None if unrecorded."""
    load_expectations()
    return _cache.get("jobs")


def expected_terms(track=None):
    """(terms, source) for a track. `source` names where they came from so the UI can say so
    honestly rather than implying a curated per-role taxonomy we do not have."""
    data = load_expectations()
    if data and track in data and data[track]:
        return [t for t, _w in data[track][:TOP_N]], "corpus"
    if data:
        merged = {}
        for t in TRACKS:
            for term, w in data.get(t) or ():
                merged[term] = max(w, merged.get(term, 0.0))
        if merged:
            ordered = sorted(merged, key=lambda k: -merged[k])[:TOP_N]
            return ordered, "corpus"
    return _curated()[:TOP_N], "curated"


def _present(term, resume_low, words):
    try:
        return core._term_present(term, resume_low, words)
    except Exception:
        # Whole-word fallback, never a bare substring test: "data" must not be answered by
        # "database", which is the exact bug test_scoring.py pins for the feed's matcher.
        return (" %s " % term) in (" %s " % resume_low)


def infer_track(resume_text):
    """Which track this résumé reads as. Ties go to 'mgmt', matching core.role_track, which sends
    anything it cannot classify to mgmt so the two never disagree about the leftovers."""
    data = load_expectations()
    if not data:
        return "mgmt"
    low = (resume_text or "").lower()
    words = core._resume_wordset(low)
    best, best_n = "mgmt", -1
    for t in TRACKS:
        terms = [term for term, _w in (data.get(t) or [])[:TOP_N]]
        n = sum(1 for term in terms if _present(term, low, words))
        if n > best_n:
            best, best_n = t, n
    return best


def evaluate(resume_text, track=None, evidence_text=None):
    """Score a résumé against what its track's postings ask for.

    `evidence_text` — normally just the experience bullets — is where a term has to APPEAR to count.
    The full résumé is still used to infer the track, because a skills list is a fair signal of what
    someone does even when it is not proof they did it. Counting presence anywhere would mean a
    keyword pasted into a skills line scores the same as one earned in an accomplishment, which is
    the entire criticism of keyword scanners and would put this check at odds with
    resume_score's skills_demonstrated.

    Returns {track, source, have, missing, score, total, scope, corpus_jobs}. `missing` keeps
    expectation order,
    so the first entries are the most commonly demanded.
    """
    track = track if track in TRACKS else infer_track(resume_text)
    terms, source = expected_terms(track)
    if not terms:
        return {"track": track, "source": source, "have": [], "missing": [],
                "score": 0, "total": 0, "scope": "none", "corpus_jobs": corpus_jobs()}
    scope = "experience" if (evidence_text or "").strip() else "document"
    hay = (evidence_text if scope == "experience" else resume_text) or ""
    low = hay.lower()
    words = core._resume_wordset(low)
    have = [t for t in terms if _present(t, low, words)]
    missing = [t for t in terms if t not in have]
    return {"track": track, "source": source, "have": have, "missing": missing,
            "score": int(100.0 * len(have) / len(terms)), "total": len(terms), "scope": scope,
            # None unless the builder recorded it. The panel says "the real jobs in your own feed"
            # rather than printing a figure it cannot vouch for.
            "corpus_jobs": corpus_jobs()}
