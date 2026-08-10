"""
brain.py — the tailoring brain, merged into JobMatch (per-user, db-backed). No AI in the core.

run_tailor(username, ...) reasons over the user's COMPLETE profile (all résumés + all stories),
the job (analyze.py), and the company (research.py; shared cache) → a deterministic tailoring
PLAN. apply_feedback() makes lessons stick (per-user). The optional AI layer (ai.rewrite) renders
the plan into finished prose; export builds .docx. KB helpers here back the Teach CRUD routes.

Storage: résumés via db.resumes (per user), stories/lessons/model via db.brain_kb (per user),
company research via db.brain_companies (shared across users).
"""
import uuid
import datetime

import core
import db

from . import analyze, match, research


def _now():
    return datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"


# ---------------- KB CRUD (per user) ----------------
def list_resumes(username):
    return db.list_resumes(username)


def get_resume(username, rid):
    return next((r for r in db.list_resumes(username) if r.get("id") == rid), None)


def save_resume(username, rec):
    return db.save_resume(username, rec)[1]


def delete_resume(username, rid):
    db.delete_resume(username, rid)


def list_stories(username):
    return db.get_brain_kb(username).get("stories", [])


def get_story(username, sid):
    return next((s for s in list_stories(username) if s.get("id") == sid), None)


def save_story(username, rec):
    kb = db.get_brain_kb(username)
    rec = dict(rec)
    if rec.get("id"):
        for i, s in enumerate(kb["stories"]):
            if s.get("id") == rec["id"]:
                kb["stories"][i] = {**s, **rec}
                break
        else:
            kb["stories"].append(rec)
    else:
        rec["id"] = uuid.uuid4().hex
        rec.setdefault("created_at", _now())
        rec.setdefault("uses", 0)
        kb["stories"].append(rec)
    db.save_brain_kb(username, kb)
    return rec["id"]


def delete_story(username, sid):
    kb = db.get_brain_kb(username)
    kb["stories"] = [s for s in kb["stories"] if s.get("id") != sid]
    db.save_brain_kb(username, kb)


def list_lessons(username):
    return db.get_brain_kb(username).get("lessons", [])


def save_lesson(username, rec):
    kb = db.get_brain_kb(username)
    rec = dict(rec)
    rec.setdefault("id", uuid.uuid4().hex)
    rec.setdefault("created_at", _now())
    kb["lessons"].insert(0, rec)
    db.save_brain_kb(username, kb)
    return rec["id"]


def delete_lesson(username, lid):
    kb = db.get_brain_kb(username)
    kb["lessons"] = [l for l in kb["lessons"] if l.get("id") != lid]
    db.save_brain_kb(username, kb)


# ---------------- tailoring ----------------
def _profile_value_terms(resumes, stories):
    blob = " ".join(r.get("content", "") for r in resumes) + " " + " ".join(
        (s.get("title", "") + " " + s.get("text", "") + " " + " ".join(s.get("skills", []) or []))
        for s in stories)
    return set(core.extract_keywords(blob, top_n=80)) if blob.strip() else set()


def _culture_fit(company, resumes, stories):
    if not company:
        return {"shared": [], "gaps": []}
    mine = _profile_value_terms(resumes, stories)
    mine_low = " ".join(mine).lower()
    comp_terms = [t for t in (company.get("keywords", []) + company.get("values", [])) if t]
    shared = [t for t in comp_terms if t.lower() in mine or t.lower() in mine_low][:10]
    gaps = [v for v in company.get("values", []) if v.lower() not in mine_low][:8]
    return {"shared": shared, "gaps": gaps}


def run_tailor(username, jd_text="", job_url="", company_name="", company_url="",
               force_research=False, record=True):
    notes = []
    kb = db.get_brain_kb(username)
    model = kb["model"]
    idf = core.load_idf()

    jd = (jd_text or "").strip()
    if not jd and job_url:
        jd = research.fetch_jd_url(job_url)
        if not jd:
            notes.append("Couldn't read that job link. Paste the description text instead.")

    analysis = analyze.analyze(jd, idf) if jd else None

    # Company research (SHARED across users; crawl when missing or refresh asked).
    domain = research.resolve_domain(company_name, company_url)
    company = None if force_research else db.get_brain_company(domain)
    if domain and company is None:
        pages = research.crawl_company(domain)
        if pages:
            company = research.extract_company_knowledge(pages, company_name)
            company["domain"] = domain
            db.put_brain_company(domain, company)
        else:
            notes.append("Couldn't reach the company site, so this is tailored from the job text only.")

    resumes = db.list_resumes(username)
    stories = kb.get("stories", [])
    lessons = kb.get("lessons", [])
    if not resumes and not stories:
        notes.append("Add a résumé or some stories below so the brain has material to match.")

    result = None
    if analysis:
        ranked_resumes = match.rank_resumes(resumes, analysis["analyzed"])
        best = ranked_resumes[0] if ranked_resumes else None
        ranked_stories = match.rank_stories(stories, analysis["analyzed"], analysis["terms"],
                                            model, lessons, top_k=6)
        applied = match.applicable_lessons(analysis["terms"], lessons)
        before = best["score"] if best else 0
        combined = (best["resume"].get("content", "") if best else "") + " " \
            + " ".join(rs["story"].get("text", "") + " " + rs["story"].get("title", "")
                       for rs in ranked_stories[:3])
        after = core.score_against(combined.lower(), analysis["analyzed"])[0] if combined.strip() else before
        if record:
            match.record_tailor(analysis["terms"], model)
            db.save_brain_kb(username, kb)
        result = {
            "best_resume": best,
            "ranked_resumes": ranked_resumes,
            "stories": ranked_stories,
            "applied_lessons": applied,
            "before": before,
            "after": after,
            "missing": (best["missing"][:18] if best else []),
            "mirror_terms": analysis["terms"][:14],
            "symphony": analysis["symphony"],
            "looking_for": analysis["looking_for"],
            "culture_fit": _culture_fit(company, resumes, stories),
            "jd_terms": analysis["terms"][:18],
        }
    elif jd == "":
        notes.append("Add the job description, either by pasting it or picking a job from the feed, then tailor.")

    return {"jd": jd, "domain": domain, "company": company, "company_name": company_name,
            "notes": notes, "result": result,
            "have_resumes": bool(resumes), "have_stories": bool(stories)}


def apply_feedback(username, jd_terms, story_ids, feedback_text, company_name=""):
    """Make a lesson STICK (per user): reinforce term→story associations + save a lesson."""
    kb = db.get_brain_kb(username)
    if story_ids:
        match.learn_associations(jd_terms, story_ids, kb["model"])
        idset = set(story_ids)
        for s in kb["stories"]:
            if s.get("id") in idset:
                s["uses"] = int(s.get("uses", 0)) + 1
    if (feedback_text or "").strip() or story_ids:
        kb["lessons"].insert(0, {
            "id": uuid.uuid4().hex, "created_at": _now(),
            "text": (feedback_text or "").strip() or "Feature the selected stories for jobs like this.",
            "triggers": list(jd_terms or [])[:12],
            "boost_story_ids": list(story_ids or []),
            "boost_terms": [], "weight": 1.0,
            "source": "feedback" + (" · " + company_name if company_name else "")})
    db.save_brain_kb(username, kb)
    return True


def build_rewrite_context(username, data, story_ids=None):
    """Assemble the grounding the OPTIONAL AI rewrite layer needs from a run_tailor result."""
    r = (data or {}).get("result")
    if not r:
        return None
    ids = set(story_ids or [])
    chosen = [s["story"] for s in r["stories"] if s["story"]["id"] in ids] if ids else []
    if not chosen:
        chosen = [s["story"] for s in r["stories"][:3]]
    prof, getp = {}, getattr(db, "get_profile", None)
    if getp:
        try:
            prof = getp(username) or {}
        except Exception:
            prof = {}
    return {
        "name": prof.get("name", "") or username,
        "email": prof.get("email", "") or "",
        "company_name": data.get("company_name", ""),
        "company": data.get("company") or {},
        "jd_text": data.get("jd", ""),
        "base_resume": (r["best_resume"]["resume"].get("content", "") if r["best_resume"] else ""),
        "stories": [{"title": s.get("title", ""), "text": s.get("text", "")} for s in chosen],
        "mirror_terms": r.get("mirror_terms", []),
        "missing": r.get("missing", []),
        "symphony": r.get("symphony", {}),
        "lessons": [l.get("text", "") for l in r.get("applied_lessons", [])],
    }
