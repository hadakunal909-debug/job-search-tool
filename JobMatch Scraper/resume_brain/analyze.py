"""
analyze.py — read a job description WITHOUT AI.

Deterministic extraction of: weighted keywords (via core.py), the requirement/"looking-for"
lines, culture signals, and the JD's "symphony" — its writing voice (tone words, you/we
focus, sentence length, formality, frequent action verbs). The brain feeds this into both
matching and the suggestions it shows you.
"""
import re
from collections import Counter

import core

# Tone/culture cue words -> the vibe a JD projects. Hand-built, extend freely.
_TONE = {
    "fast-paced": ("fast-paced", "fast paced", "move fast", "rapidly", "high-growth", "scrappy", "ambiguity"),
    "ownership": ("ownership", "own ", "end-to-end", "autonomy", "self-starter", "drive", "accountable"),
    "collaborative": ("collaborat", "cross-functional", "partner", "team", "together", "stakeholder"),
    "data-driven": ("data-driven", "data driven", "metrics", "kpi", "analytics", "measure", "experiment"),
    "mission-driven": ("mission", "impact", "purpose", "change the world", "customers' lives"),
    "customer-focused": ("customer", "client", "user", "customer-obsessed", "customer obsession"),
    "innovative": ("innovat", "cutting-edge", "pioneer", "bold", "reimagine", "build from scratch"),
    "structured": ("process", "framework", "governance", "compliance", "documentation", "rigor"),
}

# What the company is asking FOR (requirement cues) and culture-fit cues.
_REQ_CUE = re.compile(
    r"\b(you (?:have|are|will|'ll|bring)|we(?:'re| are) looking for|must have|required|"
    r"proven|experience (?:in|with)|ability to|track record|qualif|responsib)", re.I)
_CULTURE_CUE = re.compile(
    r"\b(culture|values|our team|who we are|what we offer|belong|inclusi|diversity|"
    r"mission|impact|grow|career|mentorship|collaborat)", re.I)

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_VERB_HINT = re.compile(r"\b(manage|lead|build|drive|deliver|coordinate|own|develop|design|"
                        r"analyze|improve|launch|partner|support|create|plan|execute|track|"
                        r"collaborate|communicate|implement|optimize|scale)\w*", re.I)


def _sentences(text):
    """Split into readable units, falling back to the JD renderer when there is no punctuation.

    A great many scraped descriptions arrive as one run with the bullet markers and full stops
    gone — the list was <li> elements and the text extractor joined them with spaces. Splitting
    THAT on [.!?] returns one enormous "sentence", which is why the tailor's right rail printed
    "…deliver recommendations in a data-driven manner Lead thoughtful and rigorous analysis
    across large data sets and synthesize insights…" as an undifferentiated block, while /job
    rendered the same description with proper headings, paragraphs and bullets.

    /job looks right because it goes through jdrender. jdrender.jd_flat_list exists for exactly
    this shape — it measures the full-stop density first and returns None when the text really is
    prose, so the ordinary path is unchanged.
    """
    text = text or ""
    try:
        import jdrender
        items = jdrender.jd_flat_list(text)
        if items:
            out = [re.sub(r"\s+", " ", s).strip() for s in items]
            out = [s for s in out if len(s) > 20]
            if len(out) > 1:
                return out
    except Exception:
        pass
    return [s.strip() for s in _SENT_SPLIT.split(text) if len(s.strip()) > 20]


def symphony(jd_text, sentences=None):
    """The JD's writing voice — so a tailored résumé can echo it."""
    low = (jd_text or "").lower()
    tones = [name for name, cues in _TONE.items() if any(c in low for c in cues)]
    sents = _sentences(jd_text) if sentences is None else sentences
    words = re.findall(r"[a-zA-Z']+", low)
    avg_len = round(len(words) / max(1, len(sents)), 1)
    you = len(re.findall(r"\byou\b|\byour\b|\byou'll\b", low))
    we = len(re.findall(r"\bwe\b|\bour\b|\bus\b", low))
    focus = "candidate-focused (you/your)" if you > we else ("company-focused (we/our)" if we > you else "balanced")
    contractions = len(re.findall(r"\b\w+'(?:re|ll|ve|s|t)\b", low))
    formality = "casual" if contractions >= 4 else "formal"
    verbs = [v.lower() for v in _VERB_HINT.findall(jd_text or "")]
    top_verbs = [w for w, _ in Counter(verbs).most_common(8)]
    return {"tones": tones, "avg_sentence_len": avg_len, "focus": focus,
            "formality": formality, "top_verbs": top_verbs}


def analyze(jd_text, idf=None):
    """Full deterministic JD analysis the brain uses everywhere."""
    jd_text = jd_text or ""
    analyzed = core.analyze_jd(jd_text, idf)           # weighted keyword model
    # salient JD terms, importance-sorted (used for matching + 'mirror these phrases')
    terms = sorted(analyzed["terms"], key=lambda t: -analyzed["weight"].get(t, 0))
    req_text = core._requirements_text(jd_text) or jd_text
    # Reuse this analysis's sentence list for culture and voice; when there is no
    # separate requirements section, it also supplies the requirement cues.
    sentences = _sentences(jd_text)
    req_sentences = sentences if req_text == jd_text else _sentences(req_text)
    looking_for = [s for s in req_sentences if _REQ_CUE.search(s)][:12]
    culture_lines = [s for s in sentences if _CULTURE_CUE.search(s)][:8]
    return {
        "analyzed": analyzed,
        "terms": terms,
        "looking_for": looking_for,
        "culture_lines": culture_lines,
        "symphony": symphony(jd_text, sentences=sentences),
    }
