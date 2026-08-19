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
    boost_ids = set()
    for l in applicable_lessons(jd_terms, lessons):
        boost_ids |= set(l.get("boost_story_ids") or [])
    ranked = []
    for s in stories or []:
        sid = s.get("id")
        cov, have, _ = _coverage(s.get("title", "") + " " + s.get("text", "")
                                 + " " + " ".join(s.get("skills", []) or []), analyzed)
        learned = 0.0
        for t in salient:
            learned += (assoc.get(t) or {}).get(sid, 0.0)
        learned = min(_ASSOC_CAP, learned)
        lbonus = _LESSON_BONUS if sid in boost_ids else 0.0
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
def record_tailor(jd_terms, model):
    """A job was tailored against -> fold its salient terms into the idf corpus."""
    df = model.setdefault("df", {})
    for t in set(jd_terms[:40]):
        df[t] = int(df.get(t, 0)) + 1
    model["n"] = int(model.get("n", 0)) + 1
    return model


def learn_associations(jd_terms, story_ids, model, amount=1.0):
    """Reinforce: the user kept/approved these stories for a job with these terms."""
    assoc = model.setdefault("assoc", {})
    for t in set(jd_terms[:_TOP_TERMS]):
        row = assoc.setdefault(t, {})
        for sid in story_ids or []:
            row[sid] = round(float(row.get(sid, 0.0)) + amount, 3)
    return model
