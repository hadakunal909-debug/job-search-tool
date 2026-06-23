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


ANTHROPIC_DEFAULT_MODEL = "claude-sonnet-4-6"


def _generate_claude(prompt, api_key, max_tokens=8192, image_b64=None):
    """Anthropic Claude variant of _generate — used when the configured key is an `sk-ant-…` key (or
    AI_PROVIDER=claude). Same contract: returns the model's text. Vision via a base64 image block.
    Lazy-imports the SDK so Gemini-only installs don't need `anthropic`. Override the model with
    ANTHROPIC_MODEL (default claude-sonnet-4-6)."""
    from anthropic import Anthropic
    client = Anthropic(api_key=api_key)
    content = []
    if image_b64:
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": image_b64}})
    content.append({"type": "text", "text": prompt})
    msg = client.messages.create(
        model=os.environ.get("ANTHROPIC_MODEL") or ANTHROPIC_DEFAULT_MODEL,
        max_tokens=min(int(max_tokens or 4096), 8192),
        messages=[{"role": "user", "content": content}],
    )
    return "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text").strip()


def _generate(prompt, api_key, temperature=0.4, max_tokens=8192,
              think=True, json_mode=True, model=None, timeout=_REQ_TIMEOUT, image_b64=None):
    """generateContent with automatic model fallback: try the configured model first; if it times
    out (default 30s), 404s, errors, or returns empty, SHIFT to the next model. Returns the text;
    raises RuntimeError only if every candidate fails. Pass image_b64 (base64 PNG, no data: prefix)
    to send a screenshot alongside the prompt (multimodal — used by the vision form-fill fallback)."""
    if not api_key:
        raise RuntimeError("No AI API key provided.")
    if str(api_key).startswith("sk-ant-") or os.environ.get("AI_PROVIDER") == "claude":
        return _generate_claude(prompt, api_key, max_tokens=max_tokens, image_b64=image_b64)
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
        parts = [{"text": prompt}]
        if image_b64:
            parts.append({"inline_data": {"mime_type": "image/png", "data": image_b64}})
        body = {"contents": [{"parts": parts}], "generationConfig": gen}
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
        "\nReturn the result in EXACTLY this delimited format — no JSON, no markdown, no prose outside "
        "the sections, and include all three markers. Put the full résumé FIRST so it is never truncated:\n"
        "###RESUME###\n<full plain-text résumé>\n"
        "###COVER###\n<full plain-text cover letter>\n"
        "###NOTES###\n<one short note per line on what you emphasized or any honest gap>\n\n"
        + _ctx_block(ctx) + "\n\nNow output the three marked sections:"
    )


# ----------------------------- public call -----------------------------
def _parse_rewrite(text):
    """Parse the delimited rewrite output (###RESUME### / ###COVER### / ###NOTES###). Robust to
    truncation — a cut-off response just loses trailing sections, and the résumé comes first."""
    t = text or ""
    def section(name, stops):
        stop = "|".join(["###\\s*" + s + "\\s*###" for s in stops]) or r"\Z"
        m = re.search(r"###\s*" + name + r"\s*###(.*?)(?=" + stop + r"|\Z)", t, re.S | re.I)
        return m.group(1).strip() if m else ""
    resume = section("RESUME", ["COVER", "NOTES"])
    cover = section("COVER", ["NOTES"])
    notes = [ln.strip(" -•\t") for ln in section("NOTES", []).splitlines() if ln.strip()]
    if not resume and not cover:                       # no markers at all — treat the whole text as the résumé
        return {"tailored_resume": t.strip(), "cover_letter": "", "notes": [], "parse_warning": not bool(t.strip())}
    return {"tailored_resume": resume or t.strip(), "cover_letter": cover, "notes": notes, "parse_warning": False}


def rewrite(ctx, api_key):
    """Turn the brain's plan into finished prose. Returns {tailored_resume, cover_letter, notes[],
    parse_warning}. Uses a DELIMITED (not JSON) output so large résumés never fail to parse — the old
    JSON envelope truncated/garbled under load (esp. a near-quota Gemini response). Raises RuntimeError
    only on API failure."""
    text = _generate(_rewrite_prompt(ctx), api_key, temperature=0.45, max_tokens=8192,
                     think=False, json_mode=False, timeout=90)
    return _parse_rewrite(text)


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


def vision_fill_plan(profile, resume, elements, screenshot_b64, api_key, company=""):
    """VISION FALLBACK (used only when the normal deterministic+answer pass parks). Given a SCREENSHOT
    of the application page plus an enumerated list of its interactive elements (each: index, type,
    label, options, current value), the model decides a value for every element that still needs one
    and identifies the button that advances the form. The screenshot lets it solve forms whose labels
    the DOM heuristics couldn't read. Returns {"sets":[{"index":int,"value":str}], "submit_index":int|None}.
    Acts on enumerated elements (not raw pixel clicks), so the deterministic filler still does the work."""
    if not elements:
        return {"sets": [], "submit_index": None}
    prof = profile or {}
    prof_txt = "\n".join("%s: %s" % (k, v) for k, v in prof.items()
                         if v and k not in ("username", "updated_at", "extra", "application_defaults"))
    lines = []
    for e in elements[:60]:
        opts = e.get("options") or []
        optstr = ("\n    OPTIONS: " + " | ".join(str(o)[:60] for o in opts[:40])) if opts else ""
        cur = (" | current=%r" % e.get("value")) if e.get("value") else ""
        lines.append("- index=%s | type=%s | label=%s%s%s"
                     % (e.get("index"), e.get("type", "text"), (e.get("label") or "")[:160], cur, optstr))
    prompt = (
        "You are completing a job application form. You are shown a SCREENSHOT of the page and a list "
        "of its interactive elements (indexed). Use ONLY the candidate's real data below. Rules:\n"
        "- Decide a value for each element that still needs one to submit. Skip elements already "
        "correctly filled (current shown).\n"
        "- If an element lists OPTIONS, the value MUST be EXACTLY one of those option strings (verbatim), "
        "or \"\" if none truly fit.\n"
        "- NEVER invent employers, titles, dates, degrees, numbers, salaries, or any fact not in the "
        "data. When unsure, omit the element.\n"
        "- Do NOT fill voluntary demographic fields unless the candidate profile gives the value.\n"
        "- Work authorization: authorized=%s; needs visa sponsorship=%s.\n"
        "- Identify submit_index = the index of the button that ADVANCES/SUBMITS the form (Submit/"
        "Continue/Next/Review), NOT Back/Cancel/Save-draft. Use null if none is visible.\n\n"
        "=== CANDIDATE PROFILE ===\n%s\n\n=== RÉSUMÉ ===\n%s\n\n=== TARGET COMPANY ===\n%s\n\n"
        "=== ELEMENTS ===\n%s\n\n"
        "Return ONLY JSON: {\"sets\":[{\"index\":<int>,\"value\":\"<string>\"}],\"submit_index\":<int or null>}\n"
        "=== JSON ==="
        % (prof.get("work_authorized", ""), prof.get("needs_sponsorship", ""),
           prof_txt or "(none)", (resume or "")[:4000], company or "(unknown)", "\n".join(lines))
    )
    try:
        text = _generate(prompt, api_key, temperature=0.2, max_tokens=4096, think=False,
                         json_mode=True, image_b64=screenshot_b64)
    except Exception:
        return {"sets": [], "submit_index": None}
    obj = parse_json(text) or {}
    sets = []
    for s in (obj.get("sets") or []):
        if isinstance(s, dict) and s.get("index") is not None and s.get("value") not in (None, ""):
            try:
                sets.append({"index": int(s["index"]), "value": str(s["value"])})
            except (TypeError, ValueError):
                pass
    si = obj.get("submit_index")
    try:
        si = int(si) if si is not None else None
    except (TypeError, ValueError):
        si = None
    return {"sets": sets, "submit_index": si}
