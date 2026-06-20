"""
gemini.py — OPTIONAL AI rewrite layer (Google Gemini / AI Studio via REST; no SDK).

The brain's core stays AI-free and self-training. This module is only used when the user
chooses "Write it for me" and supplies a key. It takes the DETERMINISTIC brain's plan as
grounding — the chosen résumé, the stories to feature, the keywords/phrasing to mirror, the
JD's voice ("symphony"), and the company research — and renders a finished, truthful tailored
résumé + cover letter. It never decides what's relevant (the brain already did); it only writes.

Calls hit a FIXED Google host (not a user URL), so a plain requests.post is fine here.
"""
import os
import re
import json

import requests

GEMINI_DEFAULT_MODEL = "gemini-3.5-flash"
_BASE = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent"
_LIST = "https://generativelanguage.googleapis.com/v1beta/models"
_HDR = {"Content-Type": "application/json"}


# ----------------------------- model discovery (404 fallback) -----------------------------
def _list_models(api_key):
    r = requests.get(_LIST, params={"key": api_key}, timeout=20)
    r.raise_for_status()
    return [(m.get("name") or "").split("/")[-1] for m in r.json().get("models", [])
            if "generateContent" in (m.get("supportedGenerationMethods") or [])]


def _discover(api_key):
    try:
        def ok(m):
            bad = ("vision", "tts", "image", "audio", "embedding", "exp",
                   "preview", "learnlm", "aqa", "gemma")
            return bool(m) and not any(b in m for b in bad)
        models = _list_models(api_key)
        flash = sorted([m for m in models if "flash" in m and ok(m)], reverse=True)
        pro = sorted([m for m in models if "pro" in m and ok(m)], reverse=True)
        return (flash or pro or [m for m in models if ok(m)] or [GEMINI_DEFAULT_MODEL])[0]
    except Exception:
        return GEMINI_DEFAULT_MODEL


# ----------------------------- core generate -----------------------------
_REQ_TIMEOUT = 30          # per-model timeout (s); on timeout we SHIFT to the next model
# Fallback chain tried (in order) after the configured/default model if it times out / errors / 404s.
_FALLBACK_MODELS = ["gemini-2.5-flash", "gemini-flash-latest", "gemini-2.0-flash",
                    "gemini-1.5-flash", "gemini-2.5-pro"]


def _generate(prompt, api_key, temperature=0.4, max_tokens=8192,
              think=True, json_mode=True, model=None, timeout=_REQ_TIMEOUT):
    """generateContent with automatic model fallback: try the configured model first; if it times
    out (default 30s), 404s, errors, or returns empty, SHIFT to the next model. Returns the text;
    raises RuntimeError only if every candidate fails."""
    if not api_key:
        raise RuntimeError("No Gemini API key provided.")
    primary = model or os.environ.get("GEMINI_MODEL") or GEMINI_DEFAULT_MODEL
    candidates = []
    for m in [primary] + _FALLBACK_MODELS:
        if m and m not in candidates:
            candidates.append(m)

    def _call(m, with_think):
        gen = {"maxOutputTokens": max_tokens, "temperature": temperature}
        if json_mode:
            gen["responseMimeType"] = "application/json"
        if with_think:
            gen["thinkingConfig"] = {"thinkingBudget": -1}
        body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": gen}
        return requests.post(_BASE % m, params={"key": api_key}, headers=_HDR,
                             json=body, timeout=timeout)

    last = ""
    for m in candidates:
        try:
            r = _call(m, think)
            if r.status_code == 400 and "think" in (r.text or "").lower():
                r = _call(m, False)                       # model rejects thinkingConfig
            if r.status_code == 404:
                last = "model %s not found" % m
                continue                                   # shift to next model
            if r.status_code >= 400:
                last = "%s %s" % (r.status_code, (r.text or "")[:100])
                continue
            cands = (r.json().get("candidates") or [])
            if not cands:
                last = "no output (blocked?)"
                continue
            parts = ((cands[0].get("content") or {}).get("parts")) or []
            text = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
            if text:
                return text
            last = "empty response"
        except requests.exceptions.Timeout:
            last = "timeout >%ss on %s" % (timeout, m)
            continue                                       # SHIFT to next model on slow response
        except Exception as e:
            last = str(e)[:100]
            continue
    raise RuntimeError("Gemini failed on all models [%s]. Last: %s" % (", ".join(candidates), last))


# ----------------------------- robust JSON parse -----------------------------
def parse_json(text):
    """Best-effort JSON object from a model reply. Ladder:
       raw -> strip ```json fences -> regex outer {...}. Returns dict or None."""
    if not text:
        return None
    candidates = [text]
    fenced = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(),
                    flags=re.IGNORECASE | re.MULTILINE)
    if fenced != text:
        candidates.append(fenced)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        candidates.append(m.group(0))
    # First *balanced* {...} object from the first '{' — tolerates trailing junk like a stray
    # extra '}' or prose after the JSON (a common cause of "didn't return clean JSON").
    start = text.find("{")
    if start != -1:
        depth, instr, esc = 0, False, False
        for i in range(start, len(text)):
            c = text[i]
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                instr = not instr
            elif not instr and c == "{":
                depth += 1
            elif not instr and c == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start:i + 1])
                    break
    for c in candidates:
        try:
            obj = json.loads(c, strict=False)   # strict=False tolerates raw control chars in strings
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    return None


# ----------------------------- prompt -----------------------------
_TRUTH_RULES = (
    "Hard rules (must follow):\n"
    "- Use ONLY the candidate's real material below (their résumé + the selected stories). "
    "NEVER invent or exaggerate employers, titles, dates, degrees, metrics, or skills.\n"
    "- You MAY reword, reorder, and merge the candidate's real bullets, and weave the selected "
    "stories in as résumé bullets where they genuinely fit. Keep real quantified results.\n"
    "- Company research describes the COMPANY only. Use it to choose emphasis, mirror wording, "
    "and write the cover letter's 'why this company' — NEVER to imply the candidate did anything "
    "for that company.\n"
    "- Only include keywords from the 'mirror'/'missing' lists if the candidate's material "
    "actually supports them.\n"
    "- Honor the candidate's saved preferences (lessons) listed below.\n"
)


def _ctx_block(ctx):
    comp = ctx.get("company") or {}
    stories = "\n".join("• %s: %s" % (s.get("title", ""), s.get("text", ""))
                        for s in (ctx.get("stories") or [])) or "(none selected)"
    symph = ctx.get("symphony") or {}
    lessons = "\n".join("- " + l for l in (ctx.get("lessons") or []) if l) or "(none)"
    return (
        "=== TARGET JOB DESCRIPTION ===\n%s\n\n"
        "=== COMPANY (research; about the company only) ===\n"
        "Name: %s\nWhat they do: %s\nMission: %s\nAbout: %s\nCore values: %s\n"
        "Tech they use: %s\nCurrent initiatives/projects: %s\nWhat they emphasize: %s\n\n"
        "=== CANDIDATE ===\nName: %s\nEmail: %s\n\n"
        "=== CANDIDATE'S BASE RÉSUMÉ (real) ===\n%s\n\n"
        "=== STORIES TO FEATURE (real; weave these in) ===\n%s\n\n"
        "=== JOB'S VOICE (mirror where genuine) ===\n"
        "Tones: %s | Focus: %s | Formality: %s | Signature verbs: %s\n"
        "Phrasing to mirror: %s\nKeywords to add only-if-true: %s\n\n"
        "=== CANDIDATE PREFERENCES (lessons — honor these) ===\n%s\n"
        % (ctx.get("jd_text", ""), ctx.get("company_name", ""),
           comp.get("what_they_do", ""), comp.get("mission", ""), comp.get("about", ""),
           ", ".join(comp.get("values", []) or []),
           ", ".join(comp.get("tech_stack", []) or []),
           "; ".join((comp.get("initiatives", []) or [])[:4]),
           "; ".join((comp.get("looking_for", []) or [])[:5]),
           ctx.get("name", ""), ctx.get("email", ""),
           ctx.get("base_resume", ""), stories,
           ", ".join(symph.get("tones", []) or []), symph.get("focus", ""),
           symph.get("formality", ""), ", ".join(symph.get("top_verbs", []) or []),
           ", ".join((ctx.get("mirror_terms", []) or [])[:12]),
           ", ".join((ctx.get("missing", []) or [])[:12]), lessons)
    )


def _rewrite_prompt(ctx):
    return (
        "You are an expert résumé writer and career coach. Using the grounding below, produce a "
        "finished, ATS-friendly TAILORED RÉSUMÉ (plain text, standard sections, strong action "
        "verbs, real quantified results, lead with the most relevant experience) and a concise "
        "COVER LETTER (3 short paragraphs: a specific hook tied to the company, 1-2 proof points "
        "from the candidate's real stories, and a close). Mirror the job's voice where the "
        "candidate genuinely fits.\n\n"
        + _TRUTH_RULES +
        "\nOutput ONLY JSON of this exact shape (no markdown, no prose outside JSON):\n"
        '{"tailored_resume": "full plain-text résumé",\n'
        ' "cover_letter": "full plain-text cover letter",\n'
        ' "notes": ["short note on what you emphasized or any honest gap"]}\n\n'
        + _ctx_block(ctx) + "\n=== JSON ==="
    )


# ----------------------------- public call -----------------------------
def rewrite(ctx, api_key):
    """Turn the brain's plan into finished prose. Returns
    {tailored_resume, cover_letter, notes[], parse_warning}. Raises RuntimeError on API failure."""
    text = _generate(_rewrite_prompt(ctx), api_key, temperature=0.45, max_tokens=8192,
                     think=True, json_mode=True, timeout=90)   # résumé writing is heavier than form-fill
    obj = parse_json(text)
    if not obj or not obj.get("tailored_resume"):
        return {"tailored_resume": (obj or {}).get("tailored_resume") or text,
                "cover_letter": (obj or {}).get("cover_letter", "") or "",
                "notes": [], "parse_warning": True}
    return {"tailored_resume": obj.get("tailored_resume", ""),
            "cover_letter": obj.get("cover_letter", "") or "",
            "notes": obj.get("notes", []) or [], "parse_warning": False}


def answer_fields(profile, resume, fields, api_key, company=""):
    """Map the candidate's real data to application form fields. `fields` is a list of
    {key,label,type,options?}. Returns {key: answer_string} (answer is "" when the data
    doesn't support it). For fields with OPTIONS the answer is exactly one option. Never invents."""
    if not fields:
        return {}
    prof = profile or {}
    lines = []
    for f in (fields or [])[:35]:
        opts = f.get("options") or []
        opt = ("\n    OPTIONS: " + " | ".join(str(o) for o in opts[:40])) if opts else ""
        lines.append("- key=%s | type=%s | label=%s%s"
                     % (f.get("key"), f.get("type", "text"), (f.get("label") or "")[:200], opt))
    prof_txt = "\n".join("%s: %s" % (k, v) for k, v in prof.items()
                         if v and k not in ("username", "updated_at", "extra", "application_defaults"))
    prompt = (
        "You fill out job application form fields for a candidate. Use ONLY the candidate's real "
        "data below. Rules:\n"
        "- If a field lists OPTIONS, answer with EXACTLY one of those option strings (verbatim), or "
        "\"\" if none truly fit.\n"
        "- For free-text fields, give the value, or \"\" if the data doesn't contain it.\n"
        "- NEVER invent employers, titles, dates, degrees, numbers, salaries, clearances, or any "
        "fact not present in the data. When unsure, return \"\".\n"
        "- Work authorization: candidate authorized to work = %s; needs visa sponsorship = %s. "
        "Answer Yes/No (or the matching option) accordingly.\n"
        "- You MAY infer obvious values from the résumé (e.g. years of experience, most recent "
        "employer, city/state) when clearly supported.\n\n"
        "=== CANDIDATE PROFILE ===\n%s\n\n=== RÉSUMÉ ===\n%s\n\n=== TARGET COMPANY ===\n%s\n\n"
        "=== FIELDS TO ANSWER ===\n%s\n\n"
        "Return ONLY a JSON object mapping each key to its answer string. No prose, no markdown.\n"
        "=== JSON ==="
        % (prof.get("work_authorized", ""), prof.get("needs_sponsorship", ""),
           prof_txt or "(none)", (resume or "")[:6000], company or "(unknown)", "\n".join(lines))
    )
    try:
        text = _generate(prompt, api_key, temperature=0.2, max_tokens=4096, think=False, json_mode=True)
    except Exception:
        return {}
    obj = parse_json(text) or {}
    return {str(k): ("" if v is None else str(v)) for k, v in obj.items() if isinstance(k, str)}
