"""
match.py — the SELF-TRAINING relevance engine. No AI.

It ranks your résumés and stories against a job using:
  1. IDF-weighted keyword coverage (core.py) — the base relevance.
  2. LEARNED association weights — model.assoc[jd_term][story_id], reinforced every time you
     keep/approve a story for a job with those terms. The more you use the tool, the better
     it predicts which story fits which kind of job.
  3. LESSONS — explicit rules you (or your feedback) added: if a lesson's trigger terms match
     the job, it boosts the linked stories and surfaces its guidance.
"""
import core

_TOP_TERMS = 18          # how many salient JD terms drive association learning
_ASSOC_CAP = 30.0        # cap learned bonus so it nudges, never dominates, base relevance
_LESSON_BONUS = 12.0


def applicable_lessons(jd_terms, lessons):
    """Lessons whose triggers intersect the JD's salient terms (no triggers = always-on)."""
    jt = set(jd_terms or [])
    out = []
    for l in lessons or []:
        trig = set(l.get("triggers") or [])
        if not trig or (trig & jt):
            out.append(l)
    return out


def _coverage(text, analyzed):
    return core.score_against((text or "").lower(), analyzed)


def rank_resumes(resumes, analyzed):
    """Each résumé scored by IDF-weighted JD-keyword coverage. Best first."""
    ranked = []
    for r in resumes or []:
        score, have, missing = _coverage(r.get("content", ""), analyzed)
        ranked.append({"resume": r, "score": score, "have": have, "missing": missing})
    ranked.sort(key=lambda x: -x["score"])
    return ranked


def rank_stories(stories, analyzed, jd_terms, model, lessons, top_k=6):
    """Stories scored by coverage + learned association + lesson boosts. Returns top_k with
    a human-readable reason for each."""
    assoc = model.get("assoc") or {}
    salient = set(jd_terms[:_TOP_TERMS])
    # WEIGHT NOW COUNTS. Every lesson carried a `weight` that nothing read, so a lesson the
    # user had reinforced ranked exactly like one they had written once and forgotten. MAX and
    # not SUM across lessons, so two lessons naming the same story cannot stack past the
    # "nudges, never dominates" bargain _ASSOC_CAP makes just above.
    boost = {}
    for l in applicable_lessons(jd_terms, lessons):
        try:
            w = float(l.get("weight", 1.0) or 1.0)
        except (TypeError, ValueError):
            w = 1.0
        for sid in l.get("boost_story_ids") or []:
            boost[sid] = max(boost.get(sid, 0.0), w)
    ranked = []
    for s in stories or []:
        sid = s.get("id")
        cov, have, _ = _coverage(s.get("title", "") + " " + s.get("text", "")
                                 + " " + " ".join(s.get("skills", []) or []), analyzed)
        learned = 0.0
        for t in salient:
            learned += (assoc.get(t) or {}).get(sid, 0.0)
        learned = min(_ASSOC_CAP, learned)
        lbonus = _LESSON_BONUS * boost.get(sid, 0.0)
        total = cov + learned + lbonus
        reason = []
        if have:
            reason.append("matches " + ", ".join(have[:4]))
        if learned > 0:
            reason.append("you've favored this for similar jobs")
        if lbonus:
            reason.append("a lesson says to feature this")
        ranked.append({"story": s, "score": round(total, 1), "coverage": cov,
                       # "general fit" was worse than an empty reason: it filled the slot where a
                       # reason goes and told the reader we did not have one. Empty lets the
                       # template omit the line rather than print a shrug.
                       "learned": round(learned, 1), "reason": "; ".join(reason)})
    ranked.sort(key=lambda x: -x["score"])
    return ranked[:top_k]


# ---------------- self-training updates ----------------
#
# record_tailor USED TO LIVE HERE and has been deleted. It folded each tailored job's terms into
# a PER-USER document-frequency table, model["df"] / model["n"] -- which nothing ever read, in
# any version. So it grew a jsonb column on the users row on every tailor and bought nothing;
# measured on the local KB it held 36 terms over n=3, including "take", "through", "emphasis",
# "responsible", "procedures" and a company name.
#
# It cannot be rescued by wiring up a reader either. The question it was trying to answer -- how
# common is this term, so how much should matching it count -- is a corpus question, and a corpus
# answer now exists: norms.py over 41,427 postings, and core.load_idf() over the same. Three
# documents can never beat either. What DOES belong here is the association below, because that
# is about this user's own choices and nothing corpus-wide can know it.


def learn_associations(jd_terms, story_ids, model, amount=1.0):
    """Reinforce: the user kept/approved these stories for a job with these terms."""
    assoc = model.setdefault("assoc", {})
    for t in set(jd_terms[:_TOP_TERMS]):
        row = assoc.setdefault(t, {})
        for sid in story_ids or []:
            row[sid] = round(float(row.get(sid, 0.0)) + amount, 3)
    return model
