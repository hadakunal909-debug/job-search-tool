"""
brain.py — the tailoring brain. No AI; deterministic + self-training.

run_tailor() reasons over three sources and returns a tailoring PLAN:
  • You      — your résumé library + story bank (the KB), ranked for THIS job.
  • The job  — keywords, requirements, and its writing "symphony" (analyze.py).
  • Company  — researched live from its site, accumulated in the KB (research.py).
It produces: best-matching résumé, the stories to feature (and why), keyword gaps to close,
a culture-fit read, suggestions that echo the JD's voice, and the lessons it applied.

It LEARNS: every run folds the job into the IDF corpus; your feedback (handled in web.py via
match.learn_associations + a saved lesson) reinforces which stories fit which jobs, so the
ranking gets smarter the more you use it.
"""
import ats
import analyze
import match
import research
import store


def _culture_fit(company, resumes, stories):
    """Light, deterministic read: where your material already speaks the company's language,
    and which stated values you don't visibly reflect yet."""
    if not company:
        return {"shared": [], "gaps": []}
    mine = set()
    blob = " ".join((r.get("content", "") for r in resumes)) + " " \
        + " ".join((s.get("title", "") + " " + s.get("text", "") + " " + " ".join(s.get("skills", []) or []))
                   for s in stories)
    mine = set(ats.extract_keywords(blob, top_n=80)) if blob.strip() else set()
    mine_low = " ".join(mine).lower()
    comp_terms = [t for t in (company.get("keywords", []) + company.get("values", [])) if t]
    shared = [t for t in comp_terms if t.lower() in mine or t.lower() in mine_low][:10]
    gaps = [v for v in company.get("values", []) if v.lower() not in mine_low][:8]
    return {"shared": shared, "gaps": gaps}


def run_tailor(jd_text="", job_url="", company_name="", company_url="", force_research=False,
               record=True):
    notes = []
    model = store.load_model()
    idf = match.idf_from_model(model)

    # 1) Job description (paste wins; else fetch the link, SSRF-safe).
    jd = (jd_text or "").strip()
    if not jd and job_url:
        jd = research.fetch_jd_url(job_url)
        if not jd:
            notes.append("Couldn't read that job link — paste the description text instead.")

    analysis = analyze.analyze(jd, idf) if jd else None

    # 2) Company research (accumulated per domain; crawl when missing or refresh asked).
    domain = research.resolve_domain(company_name, company_url)
    company = None if force_research else store.get_company(domain)
    if domain and company is None:
        pages = research.crawl_company(domain)
        if pages:
            company = research.extract_company_knowledge(pages, company_name)
            company["domain"] = domain
            store.put_company(domain, company)
        else:
            notes.append("Couldn't reach the company site — tailoring from the job text only.")

    # 3) Rank the KB against the job.
    resumes = store.list_resumes()
    stories = store.list_stories()
    lessons = store.list_lessons()
    if not resumes:
        notes.append("Add at least one résumé in Teach so the brain has something to match.")

    result = None
    if analysis:
        ranked_resumes = match.rank_resumes(resumes, analysis["analyzed"])
        best = ranked_resumes[0] if ranked_resumes else None
        ranked_stories = match.rank_stories(stories, analysis["analyzed"], analysis["terms"],
                                            model, lessons, top_k=6)
        applied = match.applicable_lessons(analysis["terms"], lessons)

        # potential lift if the recommended stories are woven in
        before = best["score"] if best else 0
        combined = (best["resume"].get("content", "") if best else "") + " " \
            + " ".join(rs["story"].get("text", "") + " " + rs["story"].get("title", "")
                       for rs in ranked_stories[:3])
        after = ats.coverage(combined, jd, idf)["score"] if combined.strip() else before

        # 4) self-training: fold this job into the IDF corpus + log the event for feedback.
        # Skipped when record=False (e.g. the AI rewrite re-derives the plan — don't double-count).
        event_id = ""
        if record:
            match.record_tailor(analysis["terms"], model)
            store.save_model(model)
            event_id = store.add_event({
                "company": company_name, "title": "", "domain": domain,
                "jd_terms": analysis["terms"][:18],
                "resume_id": best["resume"]["id"] if best else "",
                "story_ids": [rs["story"]["id"] for rs in ranked_stories],
            })

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
            "event_id": event_id,
        }
    elif jd == "":
        notes.append("Add the job description — paste it or give a job link — then tailor.")

    return {
        "jd": jd, "domain": domain, "company": company,
        "company_name": company_name, "notes": notes, "result": result,
        "have_resumes": bool(resumes), "have_stories": bool(stories),
    }


def build_rewrite_context(data, story_ids=None):
    """Assemble the grounding the OPTIONAL AI rewrite layer needs from a run_tailor result.
    Picks the user-checked stories (or the top 3 the brain recommended). Returns None if there's
    no usable plan. No AI here — just packaging the deterministic plan + your real material."""
    r = (data or {}).get("result")
    if not r:
        return None
    ids = set(story_ids or [])
    chosen = [s["story"] for s in r["stories"] if s["story"]["id"] in ids] if ids else []
    if not chosen:
        chosen = [s["story"] for s in r["stories"][:3]]
    prof = store.get_profile()
    return {
        "name": prof.get("name", ""), "email": prof.get("email", ""),
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


def apply_feedback(jd_terms, story_ids, feedback_text, company_name=""):
    """Make a lesson STICK: reinforce term→story associations and save a durable lesson."""
    model = store.load_model()
    if story_ids:
        match.learn_associations(jd_terms, story_ids, model)
        store.save_model(model)
        store.bump_story_uses(story_ids)
    if (feedback_text or "").strip() or story_ids:
        store.save_lesson({
            "text": (feedback_text or "").strip() or "Feature the selected stories for jobs like this.",
            "triggers": list(jd_terms or [])[:12],
            "boost_story_ids": list(story_ids or []),
            "boost_terms": [],
            "weight": 1.0,
            "source": "feedback" + (" · " + company_name if company_name else ""),
        })
    return True
