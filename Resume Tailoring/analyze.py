"""
analyze.py — read a job description WITHOUT AI.

Deterministic extraction of: weighted keywords (via ats.py), the requirement/"looking-for"
lines, culture signals, and the JD's "symphony" — its writing voice (tone words, you/we
focus, sentence length, formality, frequent action verbs). The brain feeds this into both
matching and the suggestions it shows you.
"""
import re
from collections import Counter

import ats

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
    return [s.strip() for s in _SENT_SPLIT.split(text or "") if len(s.strip()) > 20]


def symphony(jd_text):
    """The JD's writing voice — so a tailored résumé can echo it."""
    low = (jd_text or "").lower()
    tones = [name for name, cues in _TONE.items() if any(c in low for c in cues)]
    sents = _sentences(jd_text)
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
    analyzed = ats.analyze_jd(jd_text, idf)            # weighted keyword model
    # salient JD terms, importance-sorted (used for matching + 'mirror these phrases')
    terms = sorted(analyzed["terms"], key=lambda t: -analyzed["weight"].get(t, 0))
    req_text = ats._requirements_text(jd_text) or jd_text
    looking_for = [s for s in _sentences(req_text) if _REQ_CUE.search(s)][:12]
    culture_lines = [s for s in _sentences(jd_text) if _CULTURE_CUE.search(s)][:8]
    return {
        "analyzed": analyzed,
        "terms": terms,
        "looking_for": looking_for,
        "culture_lines": culture_lines,
        "symphony": symphony(jd_text),
    }
